#!/usr/bin/env python3
"""
Snapshot and restore utility: molt captures a machine's files into snapshots, restores
them, and installs the packages and apps the machine needs.
"""

__version__ = "0.1"

import sys

# Require Python 3.11+ for tomllib
if sys.version_info < (3, 11):
    print(
        f"Error: molt.py requires Python 3.11 or later (found {sys.version.split()[0]})",
        file=sys.stderr,
    )
    sys.exit(1)

import argparse
import contextlib
import errno
import fnmatch
import hashlib
import json
import os
import platform
import plistlib
import re
import select
import shlex
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
import threading
import tomllib

import concurrent.futures as ccft

from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath

# tqdm is optional: without it, dtqdm() hands out no-op bars, so the sudo re-run and
# remote children also run where tqdm isn't installed
try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

# Global flags

__dry_run__ = False
__verbose__ = False

__script__ = Path(__file__).resolve()


def printd(*args, **kwargs):
    """Adds [DRY-RUN] to output if __dry_run__=True."""
    if not __dry_run__:
        print(*args, **kwargs)
        return
    if not args:
        print("[DRY-RUN]", **kwargs)
        return
    space = {"\r", "\n", "\r\n"}
    nargs = [("[DRY-RUN]" if arg in space else f"[DRY-RUN] {arg}") for arg in args]
    if "sep" not in kwargs:
        kwargs["sep"] = "\n"
    print(*nargs, **kwargs)


# --- Output Helpers --- #

INDENT = "  "
BAR_FORMAT = "  {desc}: {n}/{total} [{elapsed}]"

# Dry-run stand-ins for paths and values a real run would create
DRY_RUN_DIR = "/tmp/dry-run"
DRY_RUN_WORK_DIR = "/tmp/molt-work.dry-run"  # a work directory on a host
DRY_RUN_CHECKSUM = "abcd123"

# Display names for the dry-run stand-ins, most specific first
DRY_RUN_NAMES = [
    (DRY_RUN_WORK_DIR, "<work dir>"),
    (f"{DRY_RUN_DIR}/remote-capture", "<pulled snapshot>"),
    (f"{DRY_RUN_DIR}/remote-restore", "<pulled snapshot>"),
    (f"{DRY_RUN_DIR}/decrypted", "<decrypted dir>"),
    (DRY_RUN_DIR, "<staging dir>"),
    ("YYYY/MM-DD/latest", "<latest>"),
    (DRY_RUN_CHECKSUM, "<checksum>"),
]

COMPRESS_HELP = "supported: gzip, bzip2, xz, '' (no compression)"

# Non-fatal Error lines printed so far; finish() reports them
_reported = {"errors": 0}

# emit()'s default stream, sys.stdout at call time; an explicit None (a closed fd) drops text
_STDOUT = object()

# Keeps each message block whole when worker threads print. tqdm's own lock exists only
# after the first bar, and making it earlier would start a multiprocessing helper process
_output_lock = threading.RLock()

# Set, under _output_lock, by the first rsync_run copy that fails; when parallel copies
# fail together, only that one prints its output and Error block
_copy_failed = threading.Event()


def flush_stream(stream, strict=False):
    """Flush a stream before other output goes to the same terminal or pipe.

    Most flushes only keep stdout and stderr in order, so a reader that went away is
    ignored there. A strict flush is one print() and tqdm already made (a bar starting,
    the sudo re-run), so a broken stream still stops the run at that point.
    """
    if stream is None:
        return  # The fd was closed at startup; there is nothing to flush
    if strict:
        stream.flush()
        return
    try:
        stream.flush()
    except (OSError, ValueError):
        pass  # The next strict flush, a full buffer or the exit reports the broken pipe


def shares_stdout(file):
    """Check if text for file could show up ahead of buffered stdout text.

    That happens when stdout is a terminal, or when both streams go to the same file or
    pipe. When the fds can't be checked, assume they share, so the order is kept.
    """
    stdout = sys.stdout
    if stdout is None:
        return False  # The fd was closed at startup; nothing is buffered
    try:
        if stdout.isatty():
            return True
        return os.path.samestat(os.fstat(stdout.fileno()), os.fstat(file.fileno()))
    except (AttributeError, OSError, ValueError):
        return True  # No usable fd (a StringIO, a closed fd); the flush is harmless


def emit(text="", level=0, file=_STDOUT):
    """Print text through printd, one indented line at a time, without breaking live bars.

    A closed stream (None) drops the text, as print() does. Write and encoding errors
    stop the run, as they did from print(). Terminals and stderr are line-buffered; a
    piped stdout stays block-buffered. It is flushed before stderr lines only when the
    order shows (a terminal, or one file or pipe for both), and before bars, prompts
    and child processes.
    """
    if file is _STDOUT:
        file = sys.stdout  # looked up per call, so redirects and pytest capsys work
    if file is None:
        return  # The fd was closed at startup; print() drops the text too
    if file is not sys.stdout and shares_stdout(file):
        flush_stream(sys.stdout)  # keep the order when stdout and stderr share a pipe

    lines = [line.rstrip() for line in str(text).split("\n")]

    # tqdm has its lock only after the first bar; before that there is no bar to clear
    if tqdm is not None and hasattr(tqdm, "_lock"):
        bar_safe = tqdm.external_write_mode(file=file)
    else:
        bar_safe = contextlib.nullcontext()

    with _output_lock, bar_safe:
        for line in lines:
            if line:
                printd(INDENT * level + line, file=file)
            else:
                printd(file=file)


def plural(count, word, many=None):
    """Format a count with its noun: '1 archive', '2 archives', '3 entries'."""
    if count != 1:
        word = many or word + "s"
    return f"{count} {word}"


def labeled(label, value):
    """Format 'label value' for a detail line, aligning further lines of value under it."""
    first, *rest = str(value).split("\n")
    pad = " " * (len(label) + 1)
    return "\n".join([f"{label} {first}", *(pad + line for line in rest)])


def shown(value):
    """Return a value for display, naming dry-run stand-ins instead of showing them."""
    text = str(value)
    if __dry_run__:
        for placeholder, name in DRY_RUN_NAMES:
            text = text.replace(placeholder, name)
    return text


def is_placeholder(path):
    """Check if a path is a dry-run stand-in for something a real run would create."""
    return __dry_run__ and str(path).startswith(DRY_RUN_DIR)


def here():
    """Name this machine for From/To lines."""
    return f"{platform.node() or 'localhost'} (this machine)"


def banner(noun):
    """Print a run's first line: 'Starting <noun>[ as root][ (no changes will be made)]'."""
    title = f"Starting {noun}"
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        title += " as root"
    if __dry_run__:
        title += " (no changes will be made)"
    emit(title)


def context(source, dest):
    """Print the From/To lines under the banner."""
    emit(f"From: {shown(source)}", 1)
    emit(f"To:   {shown(dest)}", 1)


def step(title):
    """Start a step: one blank line, then '<title>...' at column 0."""
    emit()
    emit(f"{title}...")


def step_skipped(what, reason):
    """Print a skipped step: one blank line, then 'Skipping <what> (<reason>)'."""
    emit()
    emit(f"Skipping {what} ({reason})")


def note(msg):
    """Print a 'Note:' line on stdout after one blank line."""
    emit()
    emit(f"Note: {msg}")


def say(msg, level=1):
    """Print an item (level 1) or a detail (level 2) on stdout."""
    emit(msg, level)


def echo(argv, level=1, verbose=False, runs=False):
    """Show a command as 'run: <shell line>'.

    A dry run shows every command it skips. runs=True marks a read-only command that runs
    in a dry run too; it shows only as in a real run. verbose=True shows it with --verbose.
    """
    skipped = __dry_run__ and not runs
    if skipped or (verbose and __verbose__):
        emit("run: " + shlex.join(shown(arg) for arg in argv), level)


def ok(msg, level=1, dry=None):
    """Print '✓ msg' in a real run; in a dry run print `dry` without the mark, or nothing."""
    if not __dry_run__:
        emit(f"✓ {msg}", level)
    elif dry:
        emit(dry, level)


def message_block(first, details):
    """Join a message line and its details, each detail line indented one level."""
    lines = [first]
    for detail in details:
        lines += [INDENT + line for line in str(detail).split("\n")]
    return "\n".join(lines)


def warn(msg, *details):
    """Print 'Warning: msg' and indented details on stderr; the run continues.

    The block is one emit(), so a live bar is cleared once for it.
    """
    emit(message_block(f"Warning: {msg}", details), file=sys.stderr)


def error(msg, *details):
    """Print 'Error: msg' and indented details on stderr, and count it for finish().

    The block is one emit(), so a live bar is cleared once for it.
    """
    with _output_lock:
        _reported["errors"] += 1
        emit(message_block(f"Error: {msg}", details), file=sys.stderr)


def fatal(msg, *details):
    """Print an error and exit with status 1."""
    error(msg, *details)
    sys.exit(1)


def dry_run_stop(msg, *details):
    """End a dry run where a real run would stop: the Error, then the dry run's final line.

    Like every dry run it exits 0; the final line counts the error.
    """
    error(msg, *details)
    finish("")
    sys.exit(0)


def compress_error(value, source, stop=True):
    """Report an unknown [tarball] compress value and where it came from."""
    report = fatal if stop else error
    report(f"Unsupported compression '{value}' in {source}", COMPRESS_HELP)


def relay(text, level=2, file=_STDOUT):
    """Print captured child output after it ends, indented; CR redraws keep their last state."""
    if isinstance(text, bytes):
        text = text.decode(errors="replace")  # TimeoutExpired output can be bytes
    text = (text or "").rstrip()
    if not text:
        return

    lines = []
    for raw in text.split("\n"):
        line = raw.rstrip("\r").rsplit("\r", 1)[-1]
        if line.strip() or "\r" not in raw:
            lines.append(line)

    # Leading blank lines would separate the output from the line that introduced it
    while lines and not lines[0].strip():
        lines.pop(0)
    emit("\n".join(lines), level, file)


def ask(question, level=2):
    """Ask a yes/no question; only 'y' or 'yes' (any case) is yes, EOF and Ctrl-C are no."""
    prompt = f"{question} [y/N]: "
    if __dry_run__:
        emit(f"{prompt}y (assumed in a dry run)", level)
        return True

    # A terminal echoes an answer typed before the prompt appears, so input() won't
    # show it after the prompt
    typed_ahead = False
    try:
        if sys.stdin.isatty():
            typed_ahead = bool(select.select([sys.stdin], [], [], 0)[0])
    except (AttributeError, OSError, ValueError):
        pass  # No usable stdin; input() below reports it

    flush_stream(sys.stdout)
    flush_stream(sys.stderr)
    try:
        answer = input(INDENT * level + prompt)
    except (EOFError, KeyboardInterrupt):
        emit()  # end the prompt line
        emit("skip: no answer", level)
        return False
    except RuntimeError as e:
        # input() lost stdin, stdout or stderr ('input(): lost sys.stdout'); this exits 1
        # where the traceback did
        stream = "stdin"
        for name in ("stdout", "stderr"):
            if name in str(e):
                stream = name
        fatal(f"Cannot ask '{question}': {stream} is closed")

    # Only a terminal on both ends shows the typed answer after the prompt; otherwise
    # (piped input, output to a pipe or log, or an answer typed ahead) echo it so the
    # line ends and logs show it
    if typed_ahead or not (sys.stdin.isatty() and sys.stdout.isatty()):
        emit(answer.strip())
    if answer.strip().lower() in ("y", "yes"):
        return True
    emit("skip: declined", level)
    return False


def finish(msg, success=True):
    """Print the command's one final line after a blank line."""
    emit()
    errors = _reported["errors"]
    if __dry_run__:
        extra = f" with {plural(errors, 'error')}" if errors else ""
        emit(f"Dry run completed{extra}; no changes were made")
    elif errors:
        emit(f"{msg} ({plural(errors, 'error')} reported above)")
    elif success:
        emit(f"✓ {msg}")
    else:
        emit(msg)


# --- Subprocess Runners --- #

SSH_ALIVE_INTERVAL = 10
SSH_CONNECT_TIMEOUT = 30

# Wall-clock limit for short ssh commands (mktemp, mkdir, ls, rm). The remote capture and
# restore, scripts and rsync take as long as their data does, so they get none
COMMAND_TIMEOUT = 300

# rsync's own --timeout: a copy stops after this many seconds with no data moving
RSYNC_TIMEOUT = 300


def ssh_argv(host, command, tty=False):
    """Build the ssh argv that runs one shell command line on a host."""
    argv = [
        "ssh",
        "-o",
        f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
        "-o",
        f"ServerAliveInterval={SSH_ALIVE_INTERVAL}",
    ]
    if tty:
        argv.append("-t")
    return argv + [host, command]


def ssh_run(
    host,
    *commands,
    check=True,
    stdin_data=None,
    tty=False,
    quiet=False,
    desc="Remote command",
    level=1,
    timeout=COMMAND_TIMEOUT,
    read_only=False,
):
    """Execute commands on remote host with timeout and proper error handling.

    Returns (returncode, stdout, stderr). desc names the action in error lines; level
    indents the run: echo and relayed output. timeout=None sets no wall-clock limit, for
    commands that take as long as their data (ConnectTimeout and ServerAliveInterval
    still apply). A dry run only shows the command and returns (0, '', ''), unless
    read_only marks a query that changes nothing, which runs in a dry run too.
    """
    ssh_args = ssh_argv(host, " && ".join(commands), tty)

    if not quiet:
        echo(ssh_args, level, verbose=True, runs=read_only)
    if __dry_run__ and not read_only:
        return (0, "", "")

    try:
        # For TTY operations, don't capture output (interactive)
        if tty:
            # The child writes to the terminal directly; print everything before it first
            flush_stream(sys.stdout)
            flush_stream(sys.stderr)
            result = subprocess.run(
                ssh_args,
                check=check,
                timeout=timeout,
                input=stdin_data,
                text=stdin_data is not None,
            )
            return (result.returncode, "", "")

        # For non-TTY, capture output
        result = subprocess.run(
            ssh_args,
            check=check,
            timeout=timeout,
            input=stdin_data,
            text=True,
            capture_output=True,
        )
        return (result.returncode, result.stdout, result.stderr)
    except subprocess.TimeoutExpired as e:
        # Print what the command wrote before it hung, then the error
        relay(e.stdout, level)
        relay(e.stderr, level, sys.stderr)
        fatal(f"{desc} timed out on {host} after {timeout}s")
    except subprocess.CalledProcessError as e:
        # Print captured output on error before exiting
        relay(e.stdout, level)
        relay(e.stderr, level, sys.stderr)
        if e.returncode == 255:
            fatal(f"{desc} failed on {host}: ssh error (exit status 255)")
        fatal(f"{desc} failed on {host} (exit status {e.returncode})")


def rsync_run(source, dest, check=True, desc=None, options=(), read_only=False):
    """Execute rsync with its inactivity timeout.

    Returns tuple: (returncode, stdout, stderr), or None in a dry run. desc names the copy
    in error lines; options go before the paths. There is no wall-clock limit, so a large
    archive takes as long as it needs: rsync's --timeout stops a copy that moves no data
    for RSYNC_TIMEOUT seconds. read_only marks a copy that only reads a host into a local
    temp dir, which runs in a dry run too.
    """
    if desc is None:
        desc = Path(source).name

    # No --info=progress2 with --verbose: macOS openrsync rejects it, and rsync's stdout
    # is never shown
    rsync_cmd = ["rsync", "-az", f"--timeout={RSYNC_TIMEOUT}", *options, source, dest]

    if __dry_run__ and not read_only:
        echo(rsync_cmd)
        return None

    try:
        result = subprocess.run(
            rsync_cmd,
            check=check,
            capture_output=True,
            text=True,
        )
        return (result.returncode, result.stdout, result.stderr)
    except subprocess.CalledProcessError as e:
        # Print rsync's errors before exiting (without -v its stdout is empty). One block,
        # since this can run in an rsync_parallel worker; only the first failed copy
        # prints it, and the others exit quietly
        with _output_lock:
            if _copy_failed.is_set():
                sys.exit(1)
            _copy_failed.set()
            relay(e.stderr, 1, sys.stderr)
            fatal(
                f"Copying {desc} failed (rsync exit status {e.returncode})",
                labeled("from:", source),
                labeled("to:  ", dest),
            )


def dtqdm(total, desc="", unit="item", autorefresh=None, **kwargs):
    """Return a tqdm progress bar, or a no-op one in a dry run or without tqdm.

    For unknown totals (total=None) or when autorefresh=True, starts a background
    thread to refresh the display every second so elapsed time updates even during
    long operations.
    """

    class _dtqdm:
        """No-op progress bar for a dry run, or when tqdm isn't installed."""

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def update(self, n=1):
            pass

        def write(self, msg, file=_STDOUT):
            emit(msg, file=file)

    class _AutoRefreshProgress:
        """Wrapper that auto-refreshes progress bar."""

        def __init__(self, pbar):
            self.pbar = pbar
            self.stop_event = threading.Event()
            self.thread = None

        def __enter__(self):
            # Start refresh thread
            self.thread = threading.Thread(target=self._refresh_loop, daemon=True)
            self.thread.start()
            return self

        def __exit__(self, *args):
            self.stop_event.set()
            if self.thread:
                self.thread.join(timeout=2)
            return self.pbar.__exit__(*args)

        def _refresh_loop(self):
            """Refresh display every second to show elapsed time."""
            while not self.stop_event.wait(1.0):
                self.pbar.refresh()

        def update(self, n=1):
            return self.pbar.update(n)

        def write(self, msg, file=_STDOUT):
            emit(msg, file=file)

    if __dry_run__:
        return _dtqdm()

    kwargs.setdefault("bar_format", BAR_FORMAT)
    kwargs.setdefault("leave", False)  # a finished bar clears its line
    kwargs.setdefault("disable", None)  # draw only when stderr is a terminal
    # tqdm drops bar_format on a terminal that reports 0 columns; shutil falls back to 80
    kwargs.setdefault("ncols", shutil.get_terminal_size().columns)

    # Starting a bar flushes stderr and stdout, as a drawn tqdm bar always has, even when
    # no bar is drawn; a broken stream stops the run here, as it always has
    flush_stream(sys.stderr, strict=True)
    flush_stream(sys.stdout, strict=True)
    if tqdm is None:
        return _dtqdm()
    pbar = tqdm(total=total, desc=desc, unit=unit, **kwargs)

    # Auto-refresh if requested or for unknown totals
    if autorefresh or (autorefresh is None and total is None):
        return _AutoRefreshProgress(pbar)

    return pbar


def rsync_parallel(transfers):
    """Execute multiple rsync operations in parallel."""
    if not transfers:
        return 0

    # A failed copy exits the run, so this starts a new group of copies
    _copy_failed.clear()

    # A dry run shows each rsync command it skips
    if __dry_run__:
        for source, dest, desc in transfers:
            rsync_run(source, dest, desc=desc)
        return len(transfers)

    if __verbose__:
        for source, dest, desc in transfers:
            say(f"{desc}: {source} -> {dest}")
    success_count = 0

    with dtqdm(len(transfers), "Copying files", autorefresh=True) as pbar:

        def transfer_with_desc(transfer):
            source, dest, desc = transfer
            try:
                _, _, stderr = rsync_run(source, dest, desc=desc)
                # Print any problems after the transfer completes, as one block
                if stderr:
                    with _output_lock:
                        warn(f"rsync reported problems copying {desc}")
                        relay(stderr, 1, sys.stderr)
                return True
            except Exception as e:
                error(f"Cannot copy {desc}: {e}")
                return False

        with ccft.ThreadPoolExecutor(max_workers=len(transfers)) as exc:
            futures = {exc.submit(transfer_with_desc, t): t for t in transfers}
            for future in ccft.as_completed(futures):
                if future.result():
                    success_count += 1
                pbar.update(1)

    # Per-file results print after the bar closes, in transfer order
    if __verbose__:
        for future, (_, _, desc) in futures.items():
            if future.result():
                ok(f"{desc} copied")

    return success_count


def rsync_copy(source, dest, desc, label=None):
    """Copy one path with rsync_run; --verbose lists it like rsync_parallel.

    desc names the copy in error lines; label names it in the verbose lines.
    """
    label = label or desc
    if __verbose__ and not __dry_run__:
        say(labeled(f"{label}:", f"{source} -> {dest}"))
    _copy_failed.clear()  # a single copy always reports its own failure
    result = rsync_run(source, dest, desc=desc)
    if result is not None and result[0] == 0 and result[2].strip():
        warn(f"rsync reported problems copying {desc}")
        relay(result[2], 1, sys.stderr)
    if __verbose__ and result is not None and result[0] == 0:
        ok(f"{label} copied")
    return result


def run_scripts(root_dir, config, when, working_dir, phase=None):
    """Run a config's before or after scripts on this machine, in working_dir.

    On a host, molt.py runs there and runs the scripts itself (--run-scripts is passed
    to it), so they run as in a local restore or capture. phase is display only; it
    names the migration phase ('capture before scripts').
    """
    what = f"{when} scripts"
    if phase:
        what = f"{phase} {what}"

    scripts = None
    if config and "scripts" in config and when in config["scripts"]:
        scripts = config["scripts"][when]
    if not scripts:
        step_skipped(what, "none configured")
        return

    # With every script missing there is nothing to run under a 'Running' step
    try:
        missing = [path for path in scripts if not (root_dir / path).exists()]
    except (TypeError, OSError):
        missing = []  # A bad entry fails in the loop below, where it always has
    if missing and len(missing) == len(scripts):
        step_skipped(what, "no scripts found")
        for run_script_path in missing:
            warn(f"Script {run_script_path} not found in {root_shown(root_dir)}; skipping it")
        return

    step(f"Running {what}")

    for run_script_path in scripts:
        script_path = root_dir / run_script_path
        if not script_path.exists():
            warn(f"Script {run_script_path} not found in {root_shown(root_dir)}; skipping it")
            continue
        script_name = script_path.name
        say(script_name)

        # Run in the capture directory
        script_cmd = ["bash", str(script_path)]
        if __dry_run__:
            echo(script_cmd, 2)
            if working_dir:
                say(f"cwd: {shown(working_dir)}", 2)
            continue
        try:
            # A script takes as long as it needs: no wall-clock limit
            result = subprocess.run(
                script_cmd,
                check=True,
                cwd=working_dir,
                capture_output=True,
                text=True,
            )
            # Print script output after completion
            relay(result.stdout, 2)
            relay(result.stderr, 2, sys.stderr)
            ok(f"{script_name} completed")
        except subprocess.CalledProcessError as e:
            # Print captured output on error
            relay(e.stdout, 2)
            relay(e.stderr, 2, sys.stderr)
            fatal(f"Script {script_name} failed (exit status {e.returncode})")


# --- Molt Configuration --- #

DEFAULT_ROOT = "~/.molt"

# The root from before molt was renamed from snap; used when only it exists
LEGACY_ROOT = "~/.snap"

# A host's molt root when no path is given, relative to its login directory
DEFAULT_REMOTE_ROOT = ".molt"

DEFAULT_CONFIG_CAPTURE = "configs/capture.toml"
DEFAULT_CONFIG_DEPLOY = "configs/deploy.toml"
DEFAULT_CONFIG_INSTALL = "configs/install.toml"
DEFAULT_CONFIG_RESTORE = "configs/restore.toml"
DEFAULT_CONFIG_MIGRATE = "configs/migrate.toml"

DEFAULT_SNAPSHOT_TOML = "snapshot.toml"


def parse_remote_arg(value, flag=None):
    """Parse a [user@]host:[path], user@host, or local path argument into (host, path).

    Returns (None, None) for empty values, (None, path) for local paths,
    or (host, path) for remote. 'user@host' and 'host:' return (host, None).
    A local path containing '@' needs a '/' (e.g. './name@tag').
    flag names the option the value came from in error lines.
    """
    if not value:
        return None, None

    # Match [user@]host: or [user@]host:path
    match = re.fullmatch(r"((?:[^/:]+@)?[^/:]+):(.*)", value)
    if match:
        host, path = match.group(1), match.group(2)
    elif re.fullmatch(r"[^/:]+@[^/:]+", value):
        # user@host with no colon
        host, path = value, ""
    else:
        return None, Path(value)

    # Reject empty parts and hosts that ssh/rsync would parse as options
    where = f"{flag} '{value}'" if flag else f"'{value}'"
    user, at, hostname = host.rpartition("@")
    if not hostname or hostname.startswith("-") or user.startswith("-") or (at and not user):
        fatal(
            f"Invalid host '{host}' in {where}",
            "Use [user@]host[:path]; the user and host cannot be empty or start with '-'",
        )

    problem = remote_path_problem(path) if path else None
    if problem:
        fatal(f"Invalid path '{path}' in {where}: {problem}", REMOTE_PATH_HINT)

    return host, Path(path) if path else None


# Characters a remote path may hold. rsync before 3.2.4, and macOS's /usr/bin/rsync,
# pass remote paths to the remote shell unescaped, so any other character could be
# split or expanded there
REMOTE_PATH_CHARS = re.compile(r"[A-Za-z0-9._/~+=,%@-]*")
REMOTE_PATH_HINT = "Remote paths can use letters, digits and . _ / ~ + = , % @ -"


def remote_path_problem(path):
    """Return why a path can't be used on a remote host, or None if it can."""
    if not REMOTE_PATH_CHARS.fullmatch(str(path)):
        return "it has characters rsync can't pass to a host safely"
    parts = normalize_remote_path(path).parts
    if parts and parts[0].startswith("-"):
        return "it starts with '-', which ssh and rsync would read as an option"
    return None


def expand_path(path_str):
    """Expand environment variables and user home in a path string."""
    return Path(os.path.expandvars(path_str)).expanduser()


def normalize_remote_path(path):
    """Normalize a path on a remote host: '~' -> '.', '~/x' -> 'x', others as given.

    ssh commands and rsync both resolve a relative path against the remote login
    directory, so the path never depends on how a remote shell expands '~'.
    """
    parts = PurePosixPath(path).parts
    if parts and parts[0] == "~":
        parts = parts[1:]
    return PurePosixPath(*parts)


def remote_molt_root(args, host):
    """Return the molt root on a host: the -r path for the -r host, else .molt."""
    if host and host == getattr(args, "root_host", None):
        return args.root_path
    return PurePosixPath(DEFAULT_REMOTE_ROOT)


# A remote molt root (-r host:path) is read from a local copy of its configs/ and
# scripts/; messages name that copy by the host:path it came from
_root_names = {}

# rsync filter rules that copy only a molt root's configs/ and scripts/ (captures/ and
# anything else stay on the host). Either may be missing
ROOT_COPY_FILTER = [
    "--include=/configs/",
    "--include=/configs/**",
    "--include=/scripts/",
    "--include=/scripts/**",
    "--exclude=*",
]


def root_shown(path):
    """Return a path for messages, naming a remote molt root's copy by its host:path."""
    text = str(path)
    for copy, name in _root_names.items():
        if text == copy or text.startswith(copy + os.sep):
            return name + text[len(copy):]
    return text


def root_fetch(host, path, local_dir):
    """Copy a remote molt root's configs/ and scripts/ into local_dir.

    This only reads the host, so a dry run copies too and can read the real configs.
    The caller creates and removes local_dir.
    """
    source = f"{host}:{path}/"
    result = rsync_run(
        source,
        f"{local_dir}/",
        check=False,
        desc=f"the molt root from {host}",
        options=ROOT_COPY_FILTER,
        read_only=True,
    )
    if result[0] != 0:
        relay(result[2], 1, sys.stderr)
        details = [labeled("from:", source)]
        if str(path) == DEFAULT_REMOTE_ROOT:
            details.append(
                f"Create a {DEFAULT_REMOTE_ROOT} directory on {host}, "
                f"or pass -r/--molt-root {host}:<path> (an older install's is .snap)"
            )
        fatal(
            f"Copying the molt root from {host} failed (rsync exit status {result[0]})",
            *details,
        )
    _root_names[str(local_dir)] = f"{host}:{path}"


def root_display(root):
    """Expand an archive root for display, or show it as given if it can't be expanded.

    Display only: the action that uses the root reports the real error.
    """
    try:
        return expand_path(root) if root else Path("/")
    except (RuntimeError, KeyError, OSError, TypeError):
        return Path(str(root))


def resolve_root(specified_root):
    """Resolve the local molt root directory (main() handles a remote one).

    Search order:
    1. Use specified_root if provided (and it's not the default)
    2. Check for .molt in current directory
    3. Check for ~/.molt
    4. Check for ~/.snap, the root from before the rename, with a warning
    5. Error if none exist
    """
    # Resolve both paths for comparison
    default_resolved = Path(DEFAULT_ROOT).expanduser().resolve()
    specified_resolved = Path(specified_root).expanduser().resolve()

    # If user specified a root explicitly (not the default), use it (don't validate)
    if specified_resolved != default_resolved:
        return specified_resolved

    # Check PWD for .molt
    pwd_root = Path.cwd() / ".molt"
    if pwd_root.exists() and pwd_root.is_dir():
        return pwd_root.resolve()

    # Check home directory for .molt
    home_root = Path(DEFAULT_ROOT).expanduser()
    if home_root.exists() and home_root.is_dir():
        return home_root.resolve()

    legacy_root = Path(LEGACY_ROOT).expanduser()
    if legacy_root.is_dir() and not home_root.exists():
        warn(
            f"Using {legacy_root}, the root from before snap was renamed molt",
            f"Move it with: mv {legacy_root} {home_root}",
        )
        return legacy_root.resolve()

    # None found - error
    fatal(
        "No .molt directory found",
        f"searched: {pwd_root}",
        f"searched: {home_root.resolve()}",
        "Create a .molt directory, or pass -r/--molt-root <path>",
    )


def resolve_snapshot_root(captures_dir):
    """Find the most recent capture snapshot under captures/.

    Tries today first, then scans all YYYY/MM-DD directories for the latest.
    """
    if not captures_dir.exists():
        fatal(
            f"Captures directory not found: {captures_dir}",
            "Run 'molt.py capture' first, or pass --from <snapshot>",
        )

    # Try today first
    now = datetime.now()
    today_path = captures_dir / now.strftime("%Y") / now.strftime("%m-%d")
    if today_path.exists() and any(today_path.iterdir()):
        return today_path

    # Scan all YYYY/MM-DD directories and find the most recent
    latest = None
    for year_dir in sorted(captures_dir.iterdir(), reverse=True):
        if not year_dir.is_dir():
            continue
        for date_dir in sorted(year_dir.iterdir(), reverse=True):
            if not date_dir.is_dir():
                continue
            if any(date_dir.iterdir()):
                latest = date_dir
                break
        if latest:
            break

    if not latest:
        fatal(
            f"No snapshots found in {captures_dir}",
            "Run 'molt.py capture' first, or pass --from <snapshot>",
        )

    return latest


# A snapshot's snapshot.toml below a captures directory: YYYY/MM-DD/<id>/snapshot.toml.
# Stray files and dirs elsewhere don't match
SNAPSHOT_TOML_GLOB = f"[0-9][0-9][0-9][0-9]/[0-9][0-9]-[0-9][0-9]/*/{DEFAULT_SNAPSHOT_TOML}"


def resolve_remote_snapshot(host, captures_dir):
    """Find the latest snapshot on a host: the one whose snapshot.toml is newest.

    Returns the snapshot directory's path on the host. The listing runs under sh, so the
    glob and 'ls -t' work alike on BSD and GNU hosts whatever the login shell. It only
    reads, so a dry run looks too.
    """
    captures = PurePosixPath(captures_dir)
    listing = (
        f"ls -1t -- {shlex.quote(str(captures))}/{SNAPSHOT_TOML_GLOB} 2>/dev/null | head -1"
    )

    step(f"Finding the latest snapshot on {host}")
    code, stdout, stderr = ssh_run(
        host,
        f"sh -c {shlex.quote(listing)}",
        check=False,
        desc="Finding the latest snapshot",
        read_only=True,
    )
    if code != 0:
        relay(stderr, 1, sys.stderr)
        if code == 255:
            fatal(f"Finding the latest snapshot failed on {host}: ssh error (exit status 255)")
        fatal(f"Finding the latest snapshot failed on {host} (exit status {code})")

    lines = stdout.strip().splitlines()
    if not lines:
        fatal(f"No snapshots found in {host}:{captures}")

    latest = PurePosixPath(lines[0]).parent
    problem = remote_path_problem(latest)
    if problem:
        fatal(f"Cannot use snapshot {latest} on {host}: {problem}", REMOTE_PATH_HINT)
    try:
        say(f"latest: {latest.relative_to(captures)}")
    except ValueError:
        say(f"latest: {latest}")
    return latest


def load_config(root_dir, config_name):
    """Load a TOML config file from the root directory."""
    config_file = root_dir / config_name

    try:
        with open(config_file, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        fatal(f"Config file not found: {root_shown(config_file)}")
    except tomllib.TOMLDecodeError as e:
        fatal(f"Cannot parse {root_shown(config_file)}: {e}")


def toml_string(value):
    """Format a string as a TOML basic string."""
    # JSON escapes quotes, backslashes, and control characters the way TOML does,
    # except DEL, which TOML also requires to be escaped
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def toml_value(value):
    """Format a string, number, bool, or list of them as a TOML value."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return toml_string(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    raise TypeError(f"Unsupported TOML value: {value!r}")


def toml_key(key):
    """Format a key or table name: bare if it has only letters, digits and '_', else quoted."""
    if re.fullmatch(r"[A-Za-z0-9_]+", key):
        return key
    return toml_string(key)


def toml_dumps(config):
    """Format a dict as TOML: its plain values first, then each dict value as a table.

    Nested dicts get dotted headers ([tar."ssh-keys"]). A table that holds only tables
    gets no header of its own; an empty table keeps its header, so it still exists.
    Raises TypeError for values toml_value() can't format.
    """
    lines = []

    def add_table(path, table):
        values = {key: value for key, value in table.items() if not isinstance(value, dict)}
        tables = {key: value for key, value in table.items() if isinstance(value, dict)}

        if path and (values or not tables):
            if lines:
                lines.append("")
            lines.append("[" + ".".join(toml_key(name) for name in path) + "]")
        for key, value in values.items():
            lines.append(f"{toml_key(key)} = {toml_value(value)}")

        for name, subtable in tables.items():
            add_table([*path, name], subtable)

    add_table([], config)
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def verify_capture_config(config, config_name, table="tar"):
    """Verify capture configuration structure.

    table names the [tar] table in error lines (migrate.toml calls it [capture.tar]).
    """
    if "tar" not in config:
        fatal(f"{config_name}: missing required [{table}] table")

    tar_config = config["tar"]
    if not tar_config or not isinstance(tar_config, dict):
        fatal(f"{config_name}: [{table}] must be a non-empty table")

    # Verify each category has required fields
    for category, data in tar_config.items():
        if category in ("", ".", "..") or "/" in category:
            fatal(f"{config_name}: [{table}] table name '{category}' can't name an archive")
        if not isinstance(data, dict):
            fatal(f"{config_name}: [{table}.{category}] must be a table")

        if "root" not in data:
            fatal(f"{config_name}: [{table}.{category}] is missing the required 'root' key")

        if "dirs" not in data and "files" not in data:
            fatal(f"{config_name}: [{table}.{category}] needs a 'dirs' or 'files' key")

        exclude = data.get("exclude", [])
        if not isinstance(exclude, list) or not all(isinstance(p, str) for p in exclude):
            fatal(
                f"{config_name}: the 'exclude' key in [{table}.{category}] must be an "
                "array of patterns"
            )

        if not isinstance(data.get("encrypt", False), bool):
            fatal(
                f"{config_name}: the 'encrypt' key in [{table}.{category}] must be "
                "true or false"
            )

    # [install] draft = true writes a draft install.toml into the snapshot
    install_table = table[: -len("tar")] + "install"
    install = config.get("install", {})
    if (
        not isinstance(install, dict)
        or any(key != "draft" for key in install)
        or not isinstance(install.get("draft", False), bool)
    ):
        fatal(f"{config_name}: [{install_table}] takes only 'draft = true' or 'draft = false'")

    verify_age_recipients(config, config_name, table)


def verify_age_recipients(config, config_name, table="tar"):
    """Verify the [age] table of a capture config: every encrypted archive needs recipients.

    table names the [tar] table in error lines; the [age] table next to it is named the
    same way ('capture.age' in migrate.toml).
    """
    age_table = table[: -len("tar")] + "age"
    age_config = config.get("age", {})
    if not isinstance(age_config, dict):
        fatal(f"{config_name}: [{age_table}] must be a table")

    recipients = age_config.get("recipients", [])
    if not isinstance(recipients, list) or not all(
        isinstance(recipient, str) and recipient for recipient in recipients
    ):
        fatal(
            f"{config_name}: the 'recipients' key in [{age_table}] must be an array of "
            "age public keys"
        )
    recipients_file = age_config.get("recipients_file")
    if recipients_file is not None and not (isinstance(recipients_file, str) and recipients_file):
        fatal(f"{config_name}: the 'recipients_file' key in [{age_table}] must be a path")

    encrypted = [name for name, data in config["tar"].items() if data.get("encrypt") is True]
    if encrypted and not recipients and not recipients_file:
        fatal(
            f"{config_name}: [{table}.{encrypted[0]}] sets 'encrypt', but [{age_table}] has "
            "no 'recipients' or 'recipients_file' key",
            f"Add your age public key: [{age_table}] recipients = [\"age1...\"]",
        )


def verify_restore_config(config, config_name):
    """Verify restore/deploy configuration structure."""
    if "tar" not in config:
        fatal(f"{config_name}: missing required [tar] table")

    tar_config = config["tar"]
    if not isinstance(tar_config, dict):
        fatal(f"{config_name}: [tar] must be a table")

    if "archives" not in tar_config:
        fatal(f"{config_name}: [tar] is missing the required 'archives' key")

    archives = tar_config["archives"]
    if archives is not None and not isinstance(archives, list):
        fatal(f"{config_name}: the 'archives' key in [tar] must be an array of names or patterns")


def verify_snapshot(capture_dir, label=None):
    """Validate that a capture directory contains required files.

    label names the snapshot in error lines (defaults to capture_dir).
    """
    if label is None:
        label = capture_dir

    if not capture_dir.exists():
        if is_placeholder(capture_dir):
            return None
        if __dry_run__:
            error(f"Snapshot directory not found: {label}", "A real run stops here")
            return None
        fatal(
            f"Snapshot directory not found: {label}",
            "Pass --from a snapshot directory (captures/YYYY/MM-DD/<checksum>), "
            "a date directory, or [user@]host: for a remote snapshot",
        )

    toml_path = capture_dir / DEFAULT_SNAPSHOT_TOML
    if not toml_path.exists():
        fatal(
            f"{DEFAULT_SNAPSHOT_TOML} not found in {label}",
            "A snapshot directory looks like captures/YYYY/MM-DD/<checksum>",
        )

    # Load compression type from snapshot TOML
    try:
        with open(toml_path, "rb") as f:
            snapshot_config = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        fatal(f"Cannot parse {label}/{DEFAULT_SNAPSHOT_TOML}: {e}")

    compress_type = snapshot_config.get("tarball", {}).get("compress", "gzip")
    if compress_type not in COMPRESS_MAP:
        compress_error(compress_type, DEFAULT_SNAPSHOT_TOML)

    ext, _ = COMPRESS_MAP[compress_type]

    # Check that at least one tar archive exists (encrypted ones end in .age)
    archives = list(capture_dir.glob(f"*{ext}")) + list(capture_dir.glob(f"*{ext}{AGE_EXT}"))
    if not archives:
        fatal(f"No archives found in {label}")

    return toml_path


# --- Archive Handlers --- #


# Compression type mappings: extension, write mode
# Read mode is always 'r:*' for auto-detection
COMPRESS_MAP = {
    "gzip": (".tar.gz", "w:gz"),
    "bzip2": (".tar.bz2", "w:bz2"),
    "xz": (".tar.xz", "w:xz"),
    "": (".tar", "w"),
}


def archive_classify(paths, truthy, falsey, cond):
    """
    Formats `paths` into two lists of text, 'truthy' and 'falsey',
    decided based on lambda `cond(path)` for each path in `paths`.
    Each group is one count line, or one sorted line per path with --verbose.
    """
    groups = {truthy: [], falsey: []}
    for path in paths:
        if cond(path):
            groups[truthy] += [path]
        else:
            groups[falsey] += [path]

    lines = {truthy: [], falsey: []}
    for prefix, group in groups.items():
        if __verbose__:
            lines[prefix] = [f"{prefix}: {path}" for path in sorted(group)]
        elif group:
            lines[prefix] = [f"{prefix}: {plural(len(group), 'path')}"]
    return lines[truthy], lines[falsey]


def metadata_file(name):
    """Check if a member path names a macOS metadata file ('._*' or .DS_Store, any depth).

    Only on macOS; elsewhere such files are ordinary data.
    """
    if sys.platform != "darwin":
        return False
    basename = PurePosixPath(name).name
    return basename == ".DS_Store" or basename.startswith("._")


def archive_filter(tarinfo):
    """Filters metadata files from archives."""
    if metadata_file(tarinfo.name):
        return None
    return tarinfo


def exclude_match(name, patterns):
    """Return the first exclude pattern that matches a member path, or None.

    name is relative to the archive root. A pattern (an fnmatch glob) matches the whole
    path or any single component of it, so 'node_modules', '*.log' and '.cache/*' work.
    """
    path = PurePosixPath(name)
    for pattern in patterns:
        if fnmatch.fnmatch(str(path), pattern):
            return pattern
        if any(fnmatch.fnmatch(part, pattern) for part in path.parts):
            return pattern
    return None


def exclude_filter(patterns, excluded):
    """Return a tar.add filter that skips metadata files and members an exclude matches.

    Each excluded member is added to `excluded` as (name, pattern). An excluded dir is
    skipped with everything below it.
    """

    def member_filter(tarinfo):
        if metadata_file(tarinfo.name):
            return None
        pattern = exclude_match(tarinfo.name, patterns)
        if pattern:
            excluded.append((str(PurePosixPath(tarinfo.name)), pattern))
            return None
        return tarinfo

    return member_filter


def excluded_members(root_path, paths, patterns):
    """List the (name, pattern) pairs a capture excludes below paths, without archiving.

    It walks the paths as tar.add does (symlinks are not followed), for a dry run.
    """
    found = []

    def visit(full_path, name):
        if metadata_file(name):
            return
        pattern = exclude_match(name, patterns)
        if pattern:
            found.append((name, pattern))
            return
        if full_path.is_dir() and not full_path.is_symlink():
            try:
                children = sorted(os.listdir(full_path))
            except OSError:
                return  # A real run reports it when it reads the dir
            for child in children:
                visit(full_path / child, f"{name}/{child}")

    for path in paths:
        full_path = root_path / path
        if os.path.lexists(full_path):
            visit(full_path, str(PurePosixPath(path)))
    return found


def archive_expand(patterns, root_path):
    """Expand glob patterns in file/directory lists."""
    matched_paths = set()
    expanded, warnings = [], []
    for pattern in patterns:
        # Check if pattern contains glob characters
        if any(char in pattern for char in ["*", "?", "[", "]"]):
            # Use glob to find matches relative to root
            matches = list(root_path.glob(pattern))
            if matches:
                for match in matches:
                    # Store relative path from root
                    rel_path = match.relative_to(root_path)
                    if str(rel_path) not in matched_paths:
                        expanded.append(str(rel_path))
                        matched_paths.add(str(rel_path))
            else:
                warnings += [f"skip: pattern '{pattern}' (no match)"]
        else:
            # Literal path - add as-is
            if pattern not in matched_paths:
                expanded.append(pattern)
                matched_paths.add(pattern)
    return expanded, warnings


def archive_create(name, root, paths, outdir, compress="gzip", exclude=()):
    """Create a compressed tar archive for a category.

    exclude holds fnmatch patterns for members to leave out (see exclude_match).
    Returns (archive_path, lines, warnings): the item header, include and (with
    --verbose) exclude lines, then the skip lines, for the caller to print.
    """
    if compress not in COMPRESS_MAP:
        compress_error(compress, "the capture config's [tarball] table")

    ext, mode = COMPRESS_MAP[compress]
    archive_path = outdir / f"{name}{ext}"
    root_path = expand_path(root)

    # Expand any glob patterns in the paths
    expanded_paths, expand_warnings = archive_expand(paths, root_path)

    # A listed path that an exclude matches is an exclude, not an include
    excluded = []
    kept = []
    for path in expanded_paths:
        pattern = exclude_match(path, exclude) if exclude else None
        if pattern and (root_path / path).exists():
            excluded.append((str(PurePosixPath(path)), pattern))
        else:
            kept.append(path)

    lines = [f"{archive_path.name} (root: {root_path})"]
    includes, skips = archive_classify(
        kept,
        "include",
        "skip",
        cond=lambda path: (root_path / path).exists(),
    )
    lines += includes
    warnings = [f"{line} (not found)" for line in skips] + expand_warnings

    if __dry_run__:
        if __verbose__ and exclude:
            excluded += excluded_members(root_path, kept, exclude)
            lines += exclude_lines(excluded)
        return archive_path, lines, warnings

    # Don't show individual progress bars during parallel creation
    # (avoids terminal corruption and empty lines)
    member_filter = exclude_filter(exclude, excluded)
    with tarfile.open(archive_path, mode) as tar:
        for path in kept:
            full_path = root_path / path
            if not full_path.exists():
                continue
            tar.add(full_path, arcname=str(path), filter=member_filter)
    # Private to the user; the staging dir it is written in is 0700 already
    os.chmod(archive_path, PRIVATE_FILE_MODE)

    if __verbose__:
        lines += exclude_lines(excluded)
    return archive_path, lines, warnings


def exclude_lines(excluded):
    """Format --verbose 'exclude:' detail lines, one per excluded member, sorted."""
    return [f"exclude: {name} (pattern '{pattern}')" for name, pattern in sorted(set(excluded))]


def create_archives(tasks, compress="gzip"):
    """Create multiple archives in parallel.

    Args:
        tasks: List of (category, root, paths, outdir[, exclude patterns]) tuples
        compress: Compression type to use for all archives
    """
    if not tasks:
        return []

    step(f"Creating {plural(len(tasks), 'archive')}")

    # Check once here, so the workers don't each print the same error
    if compress not in COMPRESS_MAP:
        compress_error(compress, "the capture config's [tarball] table")
    ext, _ = COMPRESS_MAP[compress]

    results, buffer, failed = [], {}, set()
    with ccft.ThreadPoolExecutor(max_workers=len(tasks)) as exc:
        futures = {}
        for task in tasks:
            category, root, paths, outdir = task[:4]
            exclude = task[4] if len(task) > 4 else ()
            future = exc.submit(archive_create, category, root, paths, outdir, compress, exclude)
            futures[future] = (category, root)
        with dtqdm(len(tasks), "Creating archives", autorefresh=True) as pbar:
            for future in ccft.as_completed(futures):
                task = futures[future]
                category = task[0]
                try:
                    archive_path, lines, warnings = future.result()
                    buffer[archive_path.name] = [*lines, *warnings]
                    results.append(archive_path)
                except Exception as e:
                    # capture() stops after the list below, without saving the snapshot
                    arcname = f"{category}{ext}"
                    error(f"Cannot create {arcname}: {e}")
                    # A failed archive keeps its place in the list below
                    failed.add(arcname)
                    buffer[arcname] = [f"{arcname} (root: {root_display(task[1])})"]
                pbar.update(1)

    # Print each archive's block after the bar closes, in name order
    for arcname in sorted(buffer):
        header, *lines = buffer[arcname]
        say(header)
        if arcname in failed:
            say("failed (see the error above)", 2)
            continue
        empty = not any(line.startswith("include:") for line in lines)
        if empty:
            lines.insert(0, "include: 0 paths")
        for line in lines:
            say(line, 2)
        if empty:
            warn(f"{arcname}: none of its paths exist; the archive is empty")
    return results


# --- Encryption --- #

# An encrypted archive is <name><ext>.age, written by the age CLI (https://age-encryption.org)
AGE_EXT = ".age"
AGE_INSTALL_HINT = "Install age (brew install age, or apt install age)"


def archive_file(category, ext, encrypted=False):
    """Return an archive's file name in a snapshot: <name><ext>, plus .age when encrypted."""
    name = f"{category}{ext}"
    if encrypted:
        name += AGE_EXT
    return name


def snapshot_files(directory, ext):
    """List the archive files in a directory: <name><ext> and encrypted <name><ext>.age."""
    return sorted(directory.glob(f"*{ext}")) + sorted(directory.glob(f"*{ext}{AGE_EXT}"))


def age_missing(names, verb):
    """Stop with an Error when the age CLI is not on PATH; names are the archive file names."""
    if shutil.which("age"):
        return
    fatal(f"Cannot {verb} {', '.join(names)}: age not found", AGE_INSTALL_HINT)


def age_recipient_options(config, names):
    """Return age's -r/-R options from a capture config's [age] table.

    names are the archives to encrypt, for the Error when age is missing. Recipients are
    public keys, so nothing here is secret. recipients_file is expanded like a root.
    """
    age_missing(names, "encrypt")
    age_config = config.get("age", {})
    options = []
    for recipient in age_config.get("recipients", []):
        options += ["-r", recipient]
    recipients_file = age_config.get("recipients_file")
    if recipients_file:
        path = expand_path(recipients_file)
        if not path.is_file():
            fatal(
                f"Recipients file not found: {path}",
                "Set 'recipients_file' in the [age] table to a file of age public keys",
            )
        options += ["-R", str(path)]
    return options


def encrypt_archives(archives, options):
    """Encrypt archives in parallel with age, each to <archive>.age, removing the plaintext.

    A dry run only shows the age commands. Any failure stops the capture; the caller
    removes the staging dir, and the plaintext with it.
    """
    count = plural(len(archives), "archive")
    step(f"Encrypting {count}")

    commands = []
    for archive in archives:
        target = archive.with_name(archive.name + AGE_EXT)
        commands.append((archive, ["age", "-e", *options, "-o", str(target), str(archive)]))
    for _, argv in commands:
        echo(argv, verbose=True)
    if __dry_run__:
        return

    def encrypt(command):
        archive, argv = command
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True)
        if result.returncode == 0:
            archive.unlink()
        return result

    failures = []
    with dtqdm(len(commands), "Encrypting archives", autorefresh=True) as pbar:
        with ccft.ThreadPoolExecutor(max_workers=len(commands)) as exc:
            futures = {exc.submit(encrypt, command): command[0] for command in commands}
            for future in ccft.as_completed(futures):
                try:
                    result = future.result()
                    if result.returncode != 0:
                        failures.append((futures[future], result))
                except OSError as e:
                    failures.append((futures[future], e))
                pbar.update(1)

    if not failures:
        ok(f"{count} encrypted")
        return
    for archive, result in sorted(failures, key=lambda failure: failure[0].name):
        if isinstance(result, OSError):
            error(f"Cannot encrypt {archive.name}: {result}")
            continue
        with _output_lock:
            relay(result.stderr, 1, sys.stderr)
            error(f"Encrypting {archive.name} failed (exit status {result.returncode})")
    fatal(
        f"Capture failed: {len(failures)} of {count} could not be encrypted; "
        "no snapshot was saved"
    )


def age_identity(restore_config, names, table="tar"):
    """Return the age identity file from a restore config's [age] table, checked.

    names are the encrypted archives, for the Error lines; table names the restore
    config's [tar] table ('restore.tar' in migrate.toml), and so its [age] table.
    """
    age_table = table[: -len("tar")] + "age"
    age_config = restore_config.get("age", {})
    identity = age_config.get("identity") if isinstance(age_config, dict) else None
    if not identity:
        fatal(
            f"Cannot decrypt {', '.join(names)}: no age identity is set",
            f"Set 'identity' in the restore config's [{age_table}] table to your age "
            "identity (private key) file",
        )
    if not isinstance(identity, str):
        fatal(f"The 'identity' key in [{age_table}] must be a path")
    age_missing(names, "decrypt")

    path = expand_path(identity)
    if not path.is_file():
        fatal(
            f"Age identity not found: {path}",
            f"Set 'identity' in the restore config's [{age_table}] table to your age "
            "identity file",
        )
    return path


def decrypt_archives(archives, identity, scratch):
    """Decrypt the .age archives among `archives` into a new private (0700) temp dir.

    Returns the list with each one replaced by its plaintext copy, <name><ext>, which
    the restore reads like any other archive. The temp dir is added to `scratch`; the
    caller always removes it. A dry run only shows the age commands, and names
    placeholders a real run would create. They run one at a time, since age may ask for
    the identity's passphrase on the terminal.
    """
    encrypted = [archive for archive in archives if archive.name.endswith(AGE_EXT)]
    count = plural(len(encrypted), "archive")
    step(f"Decrypting {count}")

    if __dry_run__:
        plain_dir = Path(DRY_RUN_DIR) / "decrypted"
    else:
        plain_dir = Path(tempfile.mkdtemp(prefix=f"{__script__.stem}-decrypt-"))
        scratch.append(plain_dir)

    plain = {}
    for archive in encrypted:
        target = plain_dir / archive.name[: -len(AGE_EXT)]
        argv = ["age", "-d", "-i", str(identity), "-o", str(target), str(archive)]
        echo(argv, verbose=True)
        plain[archive] = target
        if __dry_run__:
            continue

        flush_stream(sys.stdout)
        flush_stream(sys.stderr)
        try:
            result = subprocess.run(argv, capture_output=True, text=True)
        except OSError as e:
            fatal(f"Cannot decrypt {archive.name}: {e}")
        if result.returncode != 0:
            relay(result.stderr, 1, sys.stderr)
            fatal(
                f"Decrypting {archive.name} failed (exit status {result.returncode})",
                "Check that the [age] identity matches a recipient the capture used",
            )

    ok(f"{count} decrypted")
    return [plain.get(archive, archive) for archive in archives]


def generate_snapshot_toml(
    outdir, compress="gzip", category_meta=None, roll_ext=None
):
    """Generate snapshot TOML with checksums for all archives."""
    if category_meta is None:
        category_meta = {}

    # Find archives based on compression type
    if compress not in COMPRESS_MAP:
        compress_error(compress, "the capture config's [tarball] table")

    ext, _ = COMPRESS_MAP[compress]

    toml_path = outdir / DEFAULT_SNAPSHOT_TOML

    # The caller prints the step; a dry run has no archives to checksum
    if __dry_run__:
        return toml_path, DRY_RUN_CHECKSUM

    archives = snapshot_files(outdir, ext)

    # Calculate checksums for all archives; an encrypted archive's is over its .age file
    checksums, encrypted = {}, set()
    with dtqdm(len(archives), "Computing checksums", autorefresh=True) as pbar:
        for archive in archives:
            category = archive_category(archive)
            checksum = calculate_file_checksum(archive, show=False)
            checksums[category] = checksum
            if archive.name.endswith(AGE_EXT):
                encrypted.add(category)
            pbar.update(1)

    # [tarball]: the compression (when not gzip), the rollback extension and the digest.
    # root, link and rollback are written as strings, as the capture config's values
    # always were
    tarball = {}
    if compress != "gzip":
        tarball["compress"] = compress
    if roll_ext:
        tarball["rollback"] = str(roll_ext)
    tarball["checksum"] = "sha256"

    # One [tar.<name>] table per archive, in name order
    tar_tables = {}
    for category in sorted(checksums.keys()):
        meta = category_meta.get(category, {})
        table = {}
        if "root" in meta:
            table["root"] = str(meta["root"])
        if meta.get("link"):
            table["link"] = str(meta["link"])
        if category in encrypted:
            table["encrypted"] = True
        table["checksum"] = checksums[category]
        tar_tables[category] = table

    snapshot = {"tarball": tarball}
    if tar_tables:
        snapshot["tar"] = tar_tables

    # tomllib reads TOML as UTF-8, whatever the locale. The file is private (0600)
    fd = os.open(toml_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("# Snapshot written by molt.py capture\n")
        f.write(toml_dumps(snapshot))
    os.chmod(toml_path, PRIVATE_FILE_MODE)

    # Combined checksum from all archive checksums
    combined = "".join(checksums[cat] for cat in sorted(checksums))
    return toml_path, calculate_checksum(combined.encode("utf-8"))


# --- Archive Extraction Handlers --- #


def archive_category(archive):
    """Return an archive's [tar.<name>] name: its file name without the tar extension.

    An encrypted archive loses its .age extension too.
    """
    name = Path(archive).name
    if name.endswith(AGE_EXT) and len(name) > len(AGE_EXT):
        name = name[: -len(AGE_EXT)]
    # Longest extensions first, so 'x.tar.gz' loses '.tar.gz', not just '.gz'
    for ext in sorted((ext for ext, _ in COMPRESS_MAP.values()), key=len, reverse=True):
        if name.endswith(ext) and len(name) > len(ext):
            return name[: -len(ext)]
    return Path(name).stem


def archive_entries(tar, report=True):
    """Group archive members by captured path, in archive order.

    A captured path is a member with no ancestor member: capturing '.config/nvim'
    yields '.config/nvim', not '.config', so restore never touches its siblings.
    Members below a symlink member are skipped, since extracting them would write
    through the link into paths that were never backed up. report prints one Warning
    per archive for them (their names with --verbose).
    Raises ValueError for absolute or '..' member paths.
    """
    members = tar.getmembers()
    names, links = set(), set()
    for member in members:
        path = Path(member.name)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"unsafe path '{member.name}'")
        if path.parts:
            names.add(path)
            if member.issym():
                links.add(path)

    groups, skipped = {}, []
    for member in members:
        path = Path(member.name)
        if not path.parts:
            continue

        # Ancestors from shallowest to deepest, without '.'
        ancestors = list(reversed(path.parents))[1:]
        if any(ancestor in links for ancestor in ancestors):
            skipped.append(member.name)
            continue

        entry = next((a for a in ancestors if a in names), path)
        groups.setdefault(str(entry), []).append(member)

    if report and skipped:
        archive_name = Path(tar.name or "archive").name
        entries = plural(len(skipped), "entry", "entries")
        warn(
            f"{archive_name}: skipping {entries} below a symlink in the archive",
            *(skipped if __verbose__ else []),
        )
    return groups


def archive_confirm(archive, compress="gzip", root=None):
    """Show what restoring an archive changes, then ask before it is extracted.

    Returns True for yes, False for no (declined or no answer), and None when the
    archive can't be read or is unsafe (the error is reported here).
    """
    if compress not in COMPRESS_MAP:
        compress_error(compress, DEFAULT_SNAPSHOT_TOML)

    # The item header comes first, so an error opening the archive appears under it. A
    # root that can't be expanded shows as given; archive_extract reports the error
    root_path = root_display(root)
    say(f"{archive.name} (root: {root_path})")

    # A dry run doesn't decrypt, so an encrypted archive's paths are unknown
    if is_placeholder(archive):
        say("encrypted; a dry run does not list its paths", 2)
        return ask(f"Restore {archive.name}?")

    # Get captured paths from the archive (auto-detect compression)
    try:
        with tarfile.open(archive, "r:*") as tar:
            entries = archive_entries(tar)
    except (tarfile.TarError, ValueError, OSError) as e:
        error(f"Cannot restore {archive.name}: {e}")
        say("failed (see the error above)", 2)
        return None

    # Show each path the prompt would overwrite, then each it would add, relative to
    # the root in the header
    if not entries:
        say("empty archive, no files to replace or add", 2)
    names = sorted(entries)
    existing = {name for name in names if os.path.lexists(root_path / name)}
    for name in names:
        if name in existing:
            say(f"replace: {name}", 2)
    for name in names:
        if name not in existing:
            say(f"add: {name}", 2)

    # Every archive asks once; only 'y' or 'yes' restores, and a dry run assumes yes
    if not entries:
        question = f"Restore {archive.name}? It has no files"
    elif existing:
        question = f"Restore {archive.name}? Existing files will be overwritten"
    else:
        question = f"Restore {archive.name}?"
    return ask(question)


def snapshot_archives(capture_dir, snapshot_config, ext):
    """List the archives a snapshot can restore: its [tar.<name>] tables whose file exists.

    Archive files that snapshot.toml doesn't list are never restored, so they can't skip
    the checksum check or extract without a root.
    """
    available = []
    for name, table in snapshot_config.get("tar", {}).items():
        # A name with a '/' would point outside the snapshot dir, and '.' or '..' would
        # put its backups outside the backup directory
        if name in ("", ".", ".."):
            continue
        encrypted = isinstance(table, dict) and table.get("encrypted") is True
        archive = capture_dir / archive_file(name, ext, encrypted)
        if archive.parent == capture_dir and archive.is_file():
            available.append(archive)
    return available


def archive_select(available, patterns):
    """Select the archives to restore from `available` by name or glob pattern.

    An empty pattern list selects every archive. Returns (archives, unmatched
    patterns); the caller reports the unmatched ones.
    """
    if not patterns:
        return available, []

    names = {archive_category(archive): archive for archive in available}
    archives_to_restore = []
    matched_names = set()
    unmatched = []

    for pattern in patterns:
        # An exact name first, then a glob over every archive in the snapshot; a match
        # already selected by an earlier pattern still counts
        if pattern in names:
            matches = [pattern]
        else:
            matches = [name for name in names if fnmatch.fnmatch(name, pattern)]
        if not matches:
            unmatched.append(pattern)

        for name in matches:
            if name not in matched_names:
                archives_to_restore.append(names[name])
                matched_names.add(name)

    return archives_to_restore, unmatched


def copy_with_ownership(src, dst):
    """Copy a file, symlink, or directory tree, preserving ownership when running as root.

    Modes and timestamps are kept, and xattrs where Python can copy them (Linux).
    Symlinks are copied as links, never followed.
    """
    src_path = Path(src)
    dst_path = Path(dst)
    make_parents(dst_path)

    if src_path.is_symlink():
        os.symlink(os.readlink(src_path), dst_path)
        try:
            shutil.copystat(src_path, dst_path, follow_symlinks=False)
        except OSError:
            pass  # Not every platform can set a link's own times or mode
    elif src_path.is_dir():
        shutil.copytree(src_path, dst_path, symlinks=True)
    else:
        shutil.copy2(src_path, dst_path)

    if not (hasattr(os, "geteuid") and os.geteuid() == 0):
        return

    # Preserve ownership of every copied path without following symlinks
    copied = [dst_path]
    if dst_path.is_dir() and not dst_path.is_symlink():
        for root, dirs, files in os.walk(dst_path):
            copied += [Path(root) / name for name in dirs + files]
    for path in copied:
        src_stat = (src_path / path.relative_to(dst_path)).lstat()
        os.chown(path, src_stat.st_uid, src_stat.st_gid, follow_symlinks=False)


def move_path(src, dst):
    """Move a path to dst, creating dst's missing parents (see make_parents).

    A rename keeps the same inode, so every attribute stays exactly as it was: mode,
    ownership, timestamps, xattrs, ACLs and hardlinks. Only when dst is on another
    filesystem (EXDEV) is src copied instead (copy_with_ownership); the caller then
    removes src. Returns True for a rename, False for a copy.
    """
    make_parents(dst)
    try:
        os.rename(src, dst)
        return True
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
    copy_with_ownership(src, dst)
    return False


def make_parents(path, created=None):
    """Create the missing parent dirs of a path and return them, deepest first.

    Each new dir is added to `created` as soon as it exists, so a caller can remove
    it on rollback even if a later mkdir fails. When running as root, new dirs get
    the owner of their nearest existing ancestor, so restoring into a user's home
    doesn't leave root-owned dirs.
    """
    if created is None:
        created = []
    parent = Path(path).parent
    missing = []
    while not os.path.lexists(parent) and parent.parent != parent:
        missing.append(parent)
        parent = parent.parent

    owner = parent.stat()
    for directory in reversed(missing):
        directory.mkdir()
        created.insert(0, directory)
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            os.chown(directory, owner.st_uid, owner.st_gid)
    return created


def remove_path(path):
    """Remove a file, symlink, or directory tree if it exists.

    Read-only dirs inside the tree are made writable so their contents can go.
    """
    path = Path(path)
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():

        def retry(func, failed, error):
            parent = Path(failed).parent
            inside = parent == path or path in parent.parents
            if func not in (os.unlink, os.rmdir) or not inside:
                raise error
            if not isinstance(error, PermissionError):
                raise error
            os.chmod(parent, stat.S_IMODE(parent.lstat().st_mode) | stat.S_IRWXU)
            func(failed)

        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=retry)
        else:
            shutil.rmtree(path, onerror=lambda func, failed, info: retry(func, failed, info[1]))
    elif path.exists():
        path.unlink()


def extract_members(tar, path, members):
    """Extract archive members under path, with the 'tar' safety filter when available."""
    try:
        tar.extractall(path, members=members, filter="tar")
    except TypeError:
        # Python < 3.11.4 doesn't support the filter parameter
        tar.extractall(path, members=members)


def archive_extract(archive, compress="gzip", root=None):
    """Extract an archive without prompting (assumes already confirmed)."""
    if compress not in COMPRESS_MAP:
        compress_error(compress, DEFAULT_SNAPSHOT_TOML)

    # Determine extraction path
    if root:
        extract_path = expand_path(root)
    else:
        # Legacy: extract to / for absolute paths
        extract_path = Path("/")

    if __dry_run__:
        return True

    try:
        # Don't show individual progress bars during parallel extraction
        # (avoids terminal corruption and empty lines)
        # Use 'r:*' to auto-detect compression type
        with tarfile.open(archive, "r:*") as tar:
            # Same members as a rollback restore (nothing below a symlink), in archive
            # order; when running as root, tarfile preserves ownership from the archive
            kept = {id(m) for group in archive_entries(tar, report=False).values() for m in group}
            members = [m for m in tar.getmembers() if id(m) in kept]
            extract_members(tar, extract_path, members)
        return True
    except Exception as e:
        # Runs in a worker thread; error() is safe while the bar is live
        error(f"Cannot extract {archive.name}: {e}")
        return False


def backup_rotation_path(rollbackd):
    """Pick the unused name an existing backup dir moves to: <dir>_<timestamp>[_<n>]."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prevbackd = Path(f"{rollbackd}_{timestamp}")
    count = 1
    while prevbackd.exists():
        prevbackd = Path(f"{rollbackd}_{timestamp}_{count}")
        count += 1
    return prevbackd


def backup_dir(root_path, roll_ext):
    """Return a root's backup directory, <root><roll_ext>; each archive gets a subdirectory."""
    return Path(str(root_path) + roll_ext)


def restore_category(archive, root, roll_ext, compress="gzip", rotated=None):
    """Transactionally restore a single category with rollback on failure.

    Replaces only the paths captured in the archive, one at a time:
    1. Backs up the existing path by moving it to <root><roll_ext>/<archive name>
       (a rename, so it keeps every attribute; a copy only across filesystems)
    2. Removes what is left of the original (only after a copy)
    3. Extracts the captured path from the archive
    4. On any error or Ctrl-C: moves every touched path back from backup

    rotated is the set of backup directories already moved aside in this restore run.
    The first archive with a given root moves that root's old backup directory aside;
    later ones reuse the fresh one, so they never rotate each other's backups. None
    makes this call a run of its own.
    """
    if compress not in COMPRESS_MAP:
        compress_error(compress, DEFAULT_SNAPSHOT_TOML, stop=False)
        return False
    if rotated is None:
        rotated = set()

    root_path = expand_path(root)
    backups = backup_dir(root_path, roll_ext)
    rollbackd = backups / archive_category(archive)

    # The item header comes first, so an error opening the archive appears under it
    say(f"{archive.name} (root: {root_path}, backup: {rollbackd})")

    if is_placeholder(archive):
        # A dry run doesn't decrypt, so an encrypted archive's paths are unknown
        say("encrypted; a dry run does not list its paths", 2)
        groups = {}
    else:
        # Always open archive and classify entries
        try:
            with tarfile.open(archive, "r:*") as tar:
                groups = archive_entries(tar)
        except (tarfile.TarError, ValueError, OSError) as e:
            error(f"Cannot restore {archive.name}: {e}")
            say("failed (see the error above)", 2)
            return False

        # Classify entries: replace (existing) vs add (new)
        replaces, adds = archive_classify(
            list(groups),
            "replace",
            "add",
            cond=lambda entry: os.path.lexists(root_path / entry),
        )

        if not groups:
            say("empty archive, no files to replace or add", 2)
        for line in replaces + adds:
            say(line, 2)

    # The same root can be spelled two ways (a symlink, '..'); rotate it once all the same
    rotation_key = os.path.realpath(backups)
    rotate = rotation_key not in rotated
    if __dry_run__:
        # Show the rotation a real run would make; nothing is renamed
        rotated.add(rotation_key)
        if __verbose__ and rotate:
            try:
                if backups.exists():
                    say(f"rotate: {backups} -> {backup_rotation_path(backups)}", 2)
            except OSError:
                pass  # A real run reports it when it creates the backup directory
        return True

    # Move the root's old backup directory aside (once per run), then create this
    # archive's subdirectory in the fresh one
    try:
        if rotate:
            if backups.exists():
                prevbackd = backup_rotation_path(backups)
                backups.rename(prevbackd)
                if __verbose__:
                    say(f"rotate: {backups} -> {prevbackd}", 2)
            rotated.add(rotation_key)
        # Creates rollbackd and any missing parents with their parent's owner
        make_parents(rollbackd / "entry")
    except OSError as e:
        error(f"Cannot create backup directory {rollbackd}: {e.strerror or e}")
        say("failed (see the error above)", 2)
        return False

    touched = []  # (entry, has_backup, created parent dirs) for every changed path

    try:
        with tarfile.open(archive, "r:*") as tar:
            links = []
            for entry, members in groups.items():
                entry_path = root_path / entry
                has_backup = os.path.lexists(entry_path)

                # Back up the existing path by moving it; a copy across filesystems
                # leaves the original, which goes once the backup is recorded
                # Ctrl-C waits until the move is recorded, so the rollback always
                # knows about a path it moved
                created = []
                with sigint_deferred():
                    if has_backup:
                        move_path(entry_path, rollbackd / entry)
                    touched.append((entry, has_backup, created))
                remove_path(entry_path)
                make_parents(entry_path, created)

                # Extract this entry's members from the archive
                extract_members(tar, root_path, [m for m in members if not m.islnk()])
                links += [m for m in members if m.islnk()]

            # Hardlinks last, so they link to restored files instead of old ones
            extract_members(tar, root_path, links)

        ok(f"{archive.name} restored")
        return True

    except (Exception, KeyboardInterrupt) as e:
        # Ignore further Ctrl-C so the rollback itself can't be cut short
        try:
            previous_sigint = signal.signal(signal.SIGINT, signal.SIG_IGN)
        except ValueError:
            previous_sigint = None  # Not in the main thread
        try:
            restore_rollback(archive.name, root_path, rollbackd, touched, e)
        finally:
            if previous_sigint is not None:
                signal.signal(signal.SIGINT, previous_sigint)

        if isinstance(e, KeyboardInterrupt):
            raise
        return False


@contextlib.contextmanager
def sigint_deferred():
    """Hold a Ctrl-C until the block ends, then raise it, so it can't split a step."""
    received = []
    try:
        previous = signal.signal(signal.SIGINT, lambda signum, frame: received.append(signum))
    except ValueError:
        yield  # Not in the main thread; signals only reach the main thread anyway
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)
        if received:
            raise KeyboardInterrupt


def restore_rollback(name, root_path, rollbackd, touched, cause):
    """Delete touched paths and move them back from backup; exit if that fails.

    name is the archive file name, used only in messages; cause is the exception.
    """
    # Roll back before printing anything, so an output error can't skip the rollback
    failures = []
    for entry, has_backup, created in reversed(touched):
        entry_path = root_path / entry
        try:
            remove_path(entry_path)
            if has_backup:
                # A copy back (across filesystems) leaves the backup in place too
                move_path(rollbackd / entry, entry_path)
        except OSError as rollback_error:
            failures.append(f"{entry}: {rollback_error}")
            continue

        # Remove parent dirs that were created for the extraction
        for directory in created:
            try:
                directory.rmdir()
            except OSError:
                break

    reason = "interrupted" if isinstance(cause, KeyboardInterrupt) else cause
    error(f"Cannot restore {name}: {reason}")
    if touched:
        say(f"rollback: {plural(len(touched), 'path')} from {rollbackd}", 2)
    else:
        say("rollback: nothing was changed", 2)

    if failures:
        # Stop before a later category rotates these backups away
        fatal(
            f"Rollback of {name} is incomplete; backups are in {rollbackd}",
            *failures,
            "Stopping the restore so the backups stay in place",
        )

    ok(f"{name} rolled back")


def extract_archives(
    archives, skip_confirm=False, root_map=None, compress="gzip", restored=None, failed=None
):
    """Extract multiple archives in parallel (after confirmation); returns how many.

    When the caller passes lists, `restored` gets each extracted archive and `failed`
    each one that can't be read or extracted, in the given order. Declined archives
    are in neither.
    """
    if restored is None:
        restored = []
    if failed is None:
        failed = []
    if not archives:
        return 0

    if root_map is None:
        root_map = {}

    # First, collect confirmations sequentially (unless skipped)
    confirmed, unreadable = [], set()
    if skip_confirm:
        confirmed = list(archives)
    else:
        for archive in archives:
            category = archive_category(archive)
            root = root_map.get(category)
            answer = archive_confirm(archive, compress, root)
            if answer is None:
                unreadable.add(archive)
            elif answer:
                confirmed.append(archive)

    # Extract confirmed archives in parallel (the caller printed the step)
    extracted = set()
    if confirmed:
        with ccft.ThreadPoolExecutor(max_workers=len(confirmed)) as exc:
            # Build futures with root for each archive
            futures = {}
            for archive in confirmed:
                category = archive_category(archive)
                root = root_map.get(category)
                futures[exc.submit(archive_extract, archive, compress, root)] = archive

            with dtqdm(len(confirmed), "Extracting archives", autorefresh=True) as pbar:
                for future in ccft.as_completed(futures):
                    archive = futures[future]
                    try:
                        if future.result():
                            extracted.add(archive)
                    except Exception as e:
                        error(f"Cannot extract {archive.name}: {e}")
                    pbar.update(1)

        # Step result: the prompts above have no results of their own
        total = plural(len(confirmed), "archive")
        if len(extracted) == len(confirmed):
            ok(f"{total} extracted")
        else:
            say(f"{len(extracted)} of {total} extracted")

    for archive in archives:
        if archive in extracted:
            restored.append(archive)
        elif archive in unreadable or archive in confirmed:
            failed.append(archive)
    return len(extracted)


def create_symlinks(snapshot_config, restored):
    """Create symlinks from link -> root for the restored archives with a 'link' key.

    restored holds the [tar.<name>] names of the archives this run restored (in a dry
    run, the ones it would restore); other archives' links are left alone. Returns the
    number of links created.
    """
    tar_sections = snapshot_config.get("tar", {})
    if not tar_sections:
        return 0

    symlinks_created = []
    started = False
    for category, section_data in tar_sections.items():
        if category not in restored:
            continue
        link = section_data.get("link")
        root = section_data.get("root")

        if not link or not root:
            continue

        # Expand environment variables
        link_path = expand_path(link)
        root_path = expand_path(root)

        # Nothing to do when the link already points at the root
        if os.path.islink(link_path) and link_path.resolve() == root_path.resolve():
            continue

        # The step header prints before the first link that needs work
        if not started:
            step("Creating symlinks")
            started = True

        # Never replace an existing file or a link that points elsewhere
        if os.path.islink(link_path):
            fatal(
                f"Cannot create symlink {link_path}: it points to {link_path.resolve()}, "
                f"not {root_path.resolve()}",
                f"Set by the 'link' key in the [tar.{category}] table of "
                f"{DEFAULT_SNAPSHOT_TOML}; fix or remove the existing link, then restore again",
            )
        if os.path.lexists(link_path):
            fatal(
                f"Cannot create symlink {link_path}: a file or directory is already there",
                "Move it aside, then restore again",
            )

        if not __dry_run__:
            make_parents(link_path)
            link_path.symlink_to(root_path)
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                owner = link_path.parent.stat()
                os.chown(link_path, owner.st_uid, owner.st_gid, follow_symlinks=False)

        symlinks_created.append(str(link_path))
        ok(f"{link_path} -> {root_path}", dry=f"{link_path} -> {root_path}")

    return len(symlinks_created)


# --- Checksum Handlers --- #

CHECKSUM_CHUNK_SIZE = 8192

UNVERIFIED_WARNING = (
    f"{DEFAULT_SNAPSHOT_TOML} sets no [tarball] checksum; skipping archive verification"
)


def sha256_digest(data):
    """Calculate a SHA-256 digest."""
    sha256 = hashlib.sha256()
    sha256.update(data)
    return sha256.hexdigest()


def sha256_file_digest(filepath, show=False):
    """Calculate a SHA-256 digest from a file.

    show is accepted for existing callers and ignored; callers show their own bar.
    """
    sha256 = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(CHECKSUM_CHUNK_SIZE), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def calculate_checksum(data):
    """Calculate checksum digest."""
    # Only SHA-256 for now
    return sha256_digest(data)


def calculate_file_checksum(filepath, show=False):
    # Only SHA-256 for now
    return sha256_file_digest(filepath, show)


def verify_archives_from_toml(capture_dir, warn_unverified=True):
    """Verify all archives using tarball TOML checksums.

    warn_unverified=False leaves the Warning about a snapshot with no checksum to the
    caller, so a sudo re-run doesn't print it twice.
    """
    toml_path = capture_dir / DEFAULT_SNAPSHOT_TOML

    # verify_snapshot already reported a missing path; a pulled snapshot doesn't exist yet
    # (restore() adds a Note where the restore would run)
    if __dry_run__ and not toml_path.exists():
        return {}

    if not toml_path.exists():
        fatal(f"{DEFAULT_SNAPSHOT_TOML} not found in {capture_dir}")

    # Load snapshot TOML
    with open(toml_path, "rb") as f:
        snapshot_config = tomllib.load(f)

    # Get tarball options
    tarball_opts = snapshot_config.get("tarball", {})
    compress_type = tarball_opts.get("compress", "gzip")
    digest_type = tarball_opts.get("checksum")

    # Verify compression type
    if compress_type not in COMPRESS_MAP:
        compress_error(compress_type, DEFAULT_SNAPSHOT_TOML)

    archive_ext, _ = COMPRESS_MAP[compress_type]

    # If no checksum specified in options, skip verification
    if not digest_type:
        if warn_unverified:
            warn(UNVERIFIED_WARNING)
        return snapshot_config

    if digest_type != "sha256":
        fatal(
            f"Unsupported checksum '{digest_type}' in {DEFAULT_SNAPSHOT_TOML}",
            "supported: sha256",
        )

    # Get all tar sections
    tar_sections = snapshot_config.get("tar", {})

    if not tar_sections:
        fatal(f"{DEFAULT_SNAPSHOT_TOML} lists no archives (no [tar.<name>] tables)")

    count = plural(len(tar_sections), "archive")
    step(f"Verifying {count}")

    # List archives to verify
    if __verbose__:
        for category in sorted(tar_sections.keys()):
            encrypted = tar_sections[category].get("encrypted") is True
            say(f"verify: {archive_file(category, archive_ext, encrypted)}")

    if __dry_run__:
        say("checksums are not checked in a dry run")
        return snapshot_config

    # Verify each archive; problems print as one Error block after the bar
    problems = []
    bad = 0
    with dtqdm(len(tar_sections), "Verifying archives", autorefresh=True) as pbar:
        for category, section_data in tar_sections.items():
            # An encrypted archive's checksum is over its .age file
            encrypted = section_data.get("encrypted") is True
            archive_path = capture_dir / archive_file(category, archive_ext, encrypted)

            if not archive_path.exists():
                problems.append(f"{archive_path.name}: not found")
                bad += 1
                pbar.update(1)
                continue

            # Rejoin checksum chunks
            checksum_chunks = section_data.get("checksum", [])
            if not checksum_chunks:
                problems.append(f"{archive_path.name}: no checksum in {DEFAULT_SNAPSHOT_TOML}")
                bad += 1
                pbar.update(1)
                continue

            expected_checksum = "".join(checksum_chunks)

            # Calculate actual checksum
            actual_checksum = calculate_file_checksum(archive_path, show=False)

            if actual_checksum != expected_checksum:
                problems.append(f"{archive_path.name}: checksum mismatch")
                if __verbose__:
                    problems.append(f"  expected: {expected_checksum}")
                    problems.append(f"  actual:   {actual_checksum}")
                bad += 1

            pbar.update(1)

    if problems:
        fatal(
            f"Snapshot verification failed for {bad} of {count}",
            *problems,
            "The snapshot may be corrupted; capture a new one or pick another with --from",
        )

    ok(f"{count} verified")
    return snapshot_config


# --- Remote Work Directories --- #


def remote_workdir(host, purpose, subdirs=()):
    """Create a private work directory on a host, with its subdirectories; returns its path.

    mktemp -d makes it 0700 with an unpredictable name under the host's $TMPDIR (or /tmp),
    and prints the path. subdirs are created in it with one mkdir -p. A dry run shows the
    commands and returns a named placeholder. The caller removes the directory with
    remote_workdir_remove() in a finally block.
    """
    step(f"Creating work directory on {host}")
    template = f"${{TMPDIR:-/tmp}}/molt-{purpose}.XXXXXXXX"
    _, stdout, _ = ssh_run(host, f'mktemp -d "{template}"', desc="Creating the work directory")

    if __dry_run__:
        work_dir = PurePosixPath(DRY_RUN_WORK_DIR)
    else:
        # The path is the last line (a login script may print before it). Anything but
        # an absolute molt-<purpose>.* path is never used, so rm -rf only gets mktemp's
        lines = stdout.strip().splitlines()
        work_dir = PurePosixPath(lines[-1].strip() if lines else "")
        usable = work_dir.is_absolute() and work_dir.name.startswith(f"molt-{purpose}.")
        if not usable or remote_path_problem(work_dir):
            fatal(
                f"Cannot create a work directory on {host}: mktemp printed no usable path",
                labeled("output:", stdout.strip() or "(none)"),
            )

    created = False
    try:
        if subdirs:
            paths = " ".join(shlex.quote(str(work_dir / name)) for name in subdirs)
            ssh_run(host, f"mkdir -p {paths}", desc="Creating the work directory")
        created = True
    finally:
        if not created:
            remote_workdir_remove(host, work_dir, report=False)
    return work_dir


def remote_stop_command(work_dir, command):
    """Build a shell line that stops 'molt.py <command> -r <work_dir>' on a host.

    pkill -f matches the molt.py run and the shell that started it. The '[.]' keeps the
    pattern from matching the shell that runs this pkill. It sends SIGINT, like Ctrl-C,
    so molt.py there removes its own staging dir (which holds plaintext archives) before
    it exits. It waits a moment when it stopped something, and its exit status is never
    used.
    """
    escaped = re.sub(r"([.+])", r"\\\1", str(work_dir))
    pattern = f"molt[.]py {command} -r {escaped}( |$)"
    return f"pkill -INT -f {shlex.quote(pattern)} && sleep 2"


def remote_workdir_remove(host, work_dir, report=True, running=None):
    """Remove a work directory from a host, best effort.

    After a run that went well (report=True) it prints its step, and a failed rm is a
    Warning. After a failure or Ctrl-C (report=False) it prints nothing, so the run still
    ends with its own Error block, and never raises for ssh problems. running names the
    molt.py command that may still run in the work dir then ('capture'); it is stopped
    first, so it can't recreate files there.
    """
    command = f"rm -rf {shlex.quote(str(work_dir))}"

    if report:
        step(f"Removing work directory from {host}")
        code, _, stderr = ssh_run(
            host, command, check=False, desc="Removing the work directory"
        )
        if code != 0:
            relay(stderr, 1, sys.stderr)
            cause = "ssh error (exit status 255)" if code == 255 else f"exit status {code}"
            warn(f"Cannot remove work directory {work_dir} from {host}: {cause}")
        return

    if __dry_run__:
        return  # Nothing was created
    if running:
        command = f"{remote_stop_command(work_dir, running)}; {command}"
    try:
        subprocess.run(
            ssh_argv(host, command),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=COMMAND_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass  # Best effort; the run is already failing


# --- Remote molt.py Runs --- #

# molt.py's name in a host's work directory
REMOTE_SCRIPT = "molt.py"


def remote_scripts(root_dir, config, host):
    """List the before and after scripts to copy to a host for --run-scripts.

    Returns their paths relative to the molt root. Each is copied to the same path in the
    work directory, so molt.py there (-r <work dir>) finds it where the config says.
    Missing scripts are left out; molt.py on the host warns about them. A path outside the
    molt root (absolute, or with '..') has no place in the work directory, so it stops
    the run before the host is touched.
    """
    table = config.get("scripts") if isinstance(config, dict) else None
    if not isinstance(table, dict):
        return []

    scripts = []
    for when in ("before", "after"):
        entries = table.get(when) or []
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, str):
                continue
            path = PurePosixPath(entry)
            if path.is_absolute() or ".." in path.parts:
                fatal(
                    f"Cannot copy script {entry} to {host}: it is outside the molt root",
                    f"Move it into the molt root (for example scripts/{path.name}) "
                    "and list that path",
                )
            if path not in scripts and (root_dir / path).is_file():
                scripts.append(path)
    return scripts


def remote_layout(base, scripts):
    """Return a work directory's subdirectories: base, then the scripts' parent dirs."""
    subdirs = list(base)
    for script in scripts:
        parent = str(script.parent)
        if parent != "." and parent not in subdirs:
            subdirs.append(parent)
    return subdirs


def remote_copies(root_dir, host, work_dir, config_src, config_name, scripts):
    """List the copies (source, dest, desc) that give a work directory what molt.py needs.

    That is molt.py, the config (config_src, copied to config_name) and the scripts at
    their paths relative to the molt root.
    """
    dest = f"{host}:{work_dir}"
    transfers = [
        (str(__script__), f"{dest}/{REMOTE_SCRIPT}", REMOTE_SCRIPT),
        (str(config_src), f"{dest}/{config_name}", config_name),
    ]
    for script in scripts:
        transfers.append((str(root_dir / script), f"{dest}/{script}", str(script)))
    return transfers


def remote_child(work_dir, command, *options, private=False):
    """Build the shell line that runs 'molt.py <command> [options]' in a host's work dir.

    python3 comes from the host's PATH (this machine's interpreter path means nothing
    there). --verbose passes through; the caller passes the command's other flags.
    private=True runs it under umask 077, so what it writes stays private to the user,
    even if it outlives an interrupted run and recreates the work directory.
    """
    argv = ["python3", REMOTE_SCRIPT, command, *(str(option) for option in options)]
    if __verbose__:
        argv.append("--verbose")
    line = f"cd {shlex.quote(str(work_dir))} && {shlex.join(argv)}"
    if private:
        line = f"umask 077 && {line}"
    return line


def restore_child_config(restore_config, table="tar"):
    """Return what restore() reads from a restore config, for a molt.py run with -t.

    That is the [tar] 'archives' key (a missing or empty one becomes [], which skips the
    archive restore just the same), the before and after [scripts], and the [age]
    identity's path. table is the name the user's config gives the [tar] table
    ('restore.tar' in migrate.toml); it goes in [molt] so the other run's messages name
    it the same way. A missing 'archives' key is noted in [molt] too, so the other run
    reports it as a local run does.
    """
    tar_table = restore_config.get("tar", {})
    archives = tar_table.get("archives") or []
    child_config = {"tar": {"archives": archives}}
    molt_table = {}
    if table != "tar":
        molt_table["table"] = table
    if "archives" not in tar_table:
        molt_table["no_archives_key"] = True
    if molt_table:
        child_config["molt"] = molt_table
    if "scripts" in restore_config:
        scripts = restore_config["scripts"]
        child_config["scripts"] = {
            when: scripts[when] for when in ("before", "after") if when in scripts
        }
    # Only the identity's path: the key itself never leaves the machine it is on
    age_config = restore_config.get("age")
    if isinstance(age_config, dict) and "identity" in age_config:
        child_config["age"] = {"identity": age_config["identity"]}
    return child_config


def write_config_toml(config, config_name, purpose):
    """Write a config to a temporary TOML file and return its path.

    config_name and purpose ('the remote capture') name it in the error line.
    """
    try:
        text = toml_dumps(config)
    except TypeError as e:
        fatal(f"Cannot write {config_name} for {purpose}: {e}")

    fd, tmp = tempfile.mkstemp(prefix=f"{Path(config_name).stem}-", suffix=".toml")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    return Path(tmp)


# --- Private Snapshots --- #

# Everything capture writes is private to the user: new dirs 0700, files 0600
PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600

# The umask for shell commands that create a snapshot's dirs on a host
PRIVATE_UMASK = "umask 077"


def mkdir_private(path):
    """Create a dir and its missing parents, each 0700; existing dirs are left as they are.

    Raises FileExistsError when a file is where the dir should be, as Path.mkdir does.
    """
    path = Path(path)
    missing = []
    existing = path
    while not os.path.lexists(existing) and existing.parent != existing:
        missing.append(existing)
        existing = existing.parent
    if not missing and not existing.is_dir():
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(path))

    for directory in reversed(missing):
        directory.mkdir(mode=PRIVATE_DIR_MODE)
        os.chmod(directory, PRIVATE_DIR_MODE)  # the umask can't widen it, but be exact


def snapshot_private(snapshot_dir):
    """Make a snapshot dir 0700 and everything in it private: files 0600, dirs 0700.

    That covers files after-capture scripts wrote there (a Brewfile). Symlinks are left
    alone, and never followed.
    """
    os.chmod(snapshot_dir, PRIVATE_DIR_MODE)
    for parent, dirs, files in os.walk(snapshot_dir):
        for name in dirs:
            path = os.path.join(parent, name)
            if not os.path.islink(path):
                os.chmod(path, PRIVATE_DIR_MODE)
        for name in files:
            path = os.path.join(parent, name)
            if not os.path.islink(path):
                os.chmod(path, PRIVATE_FILE_MODE)


# --- Capture Logic --- #


def capture(args, root_dir, outdir, config, config_name=DEFAULT_CONFIG_CAPTURE, phase=None):
    """Create archives in specified directory.

    Display only: config_name names the config in messages; phase ('capture' in a
    migration) prefixes the script steps.
    """
    # Get compression type and rollback extension from config
    tarball_opts = config.get("tarball", {})
    compress = tarball_opts.get("compress", "gzip")
    roll_ext = tarball_opts.get("rollback")

    # Archives to encrypt: age and its recipients are checked before anything runs
    tar_config = config.get("tar", {})
    ext = COMPRESS_MAP[compress][0] if compress in COMPRESS_MAP else ""
    encrypted = [
        name for name, data in tar_config.items()
        if isinstance(data, dict) and data.get("encrypt") is True
    ]
    age_options = []
    if encrypted:
        age_options = age_recipient_options(config, [f"{name}{ext}" for name in encrypted])

    # Run before scripts
    if args.run_scripts:
        run_scripts(root_dir, config, "before", working_dir=outdir, phase=phase)

    # Collect all archive tasks and build category metadata
    tasks = []
    category_meta = {}
    for category, data in tar_config.items():
        if not isinstance(data, dict):
            continue

        root = data.get("root")
        if not root:
            continue

        # Combine dirs and files
        paths = data.get("dirs", []) + data.get("files", [])
        if not paths:
            continue

        tasks.append((category, root, paths, outdir, data.get("exclude", [])))

        # Store root and link for tarball TOML
        category_meta[category] = {"root": root, "link": data.get("link")}

    # Create archives in parallel; a snapshot missing any of them is never saved (the
    # caller removes the staging dir)
    if not tasks:
        warn(f"{config_name}: lists no paths; the snapshot has no archives")
    created = create_archives(tasks, compress)
    if len(created) < len(tasks):
        count = f"{len(tasks) - len(created)} of {plural(len(tasks), 'archive')}"
        if __dry_run__:
            dry_run_stop(
                f"Capture would fail: {count} cannot be created; a real run saves no snapshot"
            )
        fatal(f"Capture failed: {count} could not be created; no snapshot was saved")

    # Encrypt in the private staging dir; only the .age files go into the snapshot
    to_encrypt = [archive for archive in created if archive_category(archive) in encrypted]
    if to_encrypt:
        encrypt_archives(sorted(to_encrypt), age_options)

    # Generate snapshot TOML with checksum
    step(f"Writing {DEFAULT_SNAPSHOT_TOML}")
    toml_path, checksum = generate_snapshot_toml(
        outdir, compress, category_meta, roll_ext
    )
    if not __dry_run__:
        say(f"snapshot id: {checksum[:7]}")

    # Create checksum-named subdirectory and move files
    dst_dir = outdir.parent / outdir.name / checksum[:7]

    if not __dry_run__:
        mkdir_private(dst_dir)

        # Move all tar archives, encrypted ones included, to checksum directory
        for archive in snapshot_files(outdir, ext):
            archive.rename(dst_dir / archive.name)

        # Move snapshot TOML to checksum directory
        toml_path.rename(dst_dir / DEFAULT_SNAPSHOT_TOML)

    # A draft of what is installed goes with the snapshot, ahead of the after scripts
    if config.get("install", {}).get("draft") is True:
        install_draft_write(dst_dir / DRAFT_INSTALL_TOML)

    # Run after-capture scripts from the same config as the archives and before scripts
    if args.run_scripts:
        run_scripts(root_dir, config, "after", working_dir=dst_dir, phase=phase)

    # Everything in the snapshot is private, the files those scripts wrote included
    if not __dry_run__:
        snapshot_private(dst_dir)

    return dst_dir


def capture_deploy(capture_dir, dest_host, intended_path):
    """Copy a snapshot directory, with every file in it, to intended_path on a host.

    Files that after-capture scripts wrote into the snapshot (a Brewfile) go with it.
    """
    if not (capture_dir / DEFAULT_SNAPSHOT_TOML).exists():
        fatal(f"{DEFAULT_SNAPSHOT_TOML} not found in {capture_dir}")

    step(f"Copying snapshot to {dest_host}")

    # rsync creates only the last directory of its destination. New dirs are private
    # (0700); rsync -a gives the snapshot's own dir and files their modes (0700, 0600)
    escaped_path = shlex.quote(str(intended_path))
    ssh_run(
        dest_host,
        f"{PRIVATE_UMASK} && mkdir -p {escaped_path}",
        desc="Creating the snapshot directory",
    )
    rsync_copy(
        f"{capture_dir}/",
        f"{dest_host}:{intended_path}/",
        f"the snapshot to {dest_host}",
        label="snapshot",
    )


def write_capture_toml(capture_config):
    """Write capture_config to a temporary capture.toml file and return its path."""
    return write_config_toml(capture_config, DEFAULT_CONFIG_CAPTURE, "the remote capture")


def remote_snapshot_id(stdout, out_dir, host):
    """Return the snapshot id from 'ls -d <out_dir>/*/' output on a host.

    out/ holds only the snapshot the capture saved, so exactly one directory is listed.
    Its name becomes a local directory name, so it must be a plain name.
    """
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    found = [PurePosixPath(line) for line in lines if PurePosixPath(line).parent == out_dir]
    if len(found) != 1 or not re.fullmatch(r"\w[\w.-]*", found[0].name):
        fatal(
            f"Cannot find the snapshot on {host}: {out_dir} should hold one snapshot directory",
            labeled("output:", "\n".join(lines) or "(none)"),
        )
    return found[0].name


def remote_capture(args, root_dir, source_host, capture_config=None):
    """Run molt.py capture on a host, then copy the snapshot it saved into a local temp dir.

    Without capture_config the host gets the capture config from -t or the default in
    root_dir; migrate passes its [capture.*] half, which is written to a temp TOML. With
    --run-scripts the host also gets the config's scripts, and molt.py there runs them.
    Returns (the snapshot copy, named after the host's snapshot id; the temp dir to
    remove, or None in a dry run). The work directory on the host is removed however the
    capture ends.
    """
    # The config is read first, so a config problem stops before the host is touched
    if capture_config is None:
        config_name = getattr(args, "config_toml", None) or DEFAULT_CONFIG_CAPTURE
        capture_config = load_config(root_dir, config_name)
        verify_capture_config(capture_config, config_name)
        config_src = root_dir / config_name
    else:
        config_src = None  # migrate: written to a temp TOML below
    scripts = []
    if args.run_scripts:
        scripts = remote_scripts(root_dir, capture_config, source_host)

    tmp_capture_toml = None
    work_dir = None
    tmpdir = None
    done = False
    try:
        if config_src is None:
            tmp_capture_toml = write_capture_toml(capture_config)
            config_src = tmp_capture_toml

        # Set up remote working directory
        layout = remote_layout(["configs", "scripts"], scripts)
        work_dir = remote_workdir(source_host, "capture", layout)

        transfers = remote_copies(
            root_dir, source_host, work_dir, config_src, DEFAULT_CONFIG_CAPTURE, scripts
        )
        step(f"Copying {plural(len(transfers), 'file')} to {source_host}")
        success_count = rsync_parallel(transfers)
        if success_count < len(transfers):
            failed = len(transfers) - success_count
            fatal(
                f"Copying to {source_host} failed for {failed} of "
                f"{plural(len(transfers), 'file')}"
            )

        # molt.py on the host saves the snapshot in out/, and runs the scripts itself.
        # It takes as long as the archives do: no wall-clock limit
        out_dir = work_dir / "out"
        options = ["-r", work_dir, "-t", DEFAULT_CONFIG_CAPTURE, "--to", out_dir]
        if args.run_scripts:
            options.append("--run-scripts")
        step(f"Running capture on {source_host}")
        code, stdout, stderr = ssh_run(
            source_host,
            remote_child(work_dir, "capture", *options, private=True),
            check=False,
            desc="Capture",
            timeout=None,
        )

        # Show the remote capture's own output: nested under its host when it succeeded
        # (its Warnings first, so its final line comes last), or ahead of the Error line
        # when it failed
        if code == 0:
            if stdout.strip() or stderr.strip():
                say(f"output from {source_host}:")
            relay(stderr, 2, sys.stderr)
            relay(stdout, 2)
        else:
            relay(stdout, 1)
            relay(stderr, 1, sys.stderr)
        if code == 255:
            fatal(f"Capture failed on {source_host}: ssh error (exit status 255)")
        if code != 0:
            fatal(f"Capture failed on {source_host} (exit status {code})")

        # Find the snapshot in out/; a dry run shows the listing it would make
        step(f"Copying snapshot from {source_host}")
        _, stdout, _ = ssh_run(
            source_host,
            f"ls -d {shlex.quote(str(out_dir))}/*/",
            desc="Finding the snapshot",
        )
        if __dry_run__:
            snapshot_id = DRY_RUN_CHECKSUM
        else:
            snapshot_id = remote_snapshot_id(stdout, out_dir, source_host)
        tmpdir = remote_pull_dir("remote-capture")

        # Copy the whole snapshot directory, with any files the after scripts wrote
        snapshot = tmpdir / snapshot_id
        rsync_copy(
            f"{source_host}:{out_dir / snapshot_id}/",
            f"{snapshot}/",
            f"the snapshot from {source_host}",
            label="snapshot",
        )
        # molt.py on the host made it private and rsync -a kept the modes; make sure
        if not __dry_run__ and snapshot.is_dir():
            snapshot_private(snapshot)
        done = True
    finally:
        if tmp_capture_toml and tmp_capture_toml.exists():
            tmp_capture_toml.unlink()
        # A failed capture leaves no pulled copy behind
        if not done and tmpdir and not __dry_run__ and tmpdir.exists():
            shutil.rmtree(tmpdir)
        if work_dir is not None:
            remote_workdir_remove(source_host, work_dir, report=done, running="capture")

    return snapshot, tmpdir if not __dry_run__ else None


# --- Restore Logic --- #


def path_writable(path):
    """Check if a path, or its nearest existing parent when missing, is writable."""
    return os.access(nearest_existing(path), os.W_OK)


def nearest_existing(path):
    """Return a path, or its nearest existing parent when it is missing."""
    path = Path(path)
    while not os.path.lexists(path) and path.parent != path:
        path = path.parent
    return path


def path_device(path):
    """Return the device (filesystem) of a path, or of its nearest existing parent."""
    return nearest_existing(path).stat().st_dev


def entry_movable(path, backup_device):
    """Check if the current user can move an existing path into the backup dir and back.

    A rename needs write and search on the path's parent (and, in a sticky dir such as
    /tmp, owning the path or the dir); a dir also needs write on itself, for its '..'.
    The backup dir is inside a fresh dir this user creates, so it is writable. When
    the backup dir is on another filesystem (backup_device differs), the path is copied
    and then deleted instead, which needs read access to everything below it.
    """
    path = Path(path)
    parent = path.parent
    if not os.access(parent, os.W_OK | os.X_OK):
        return False
    try:
        info = path.lstat()
        parent_info = parent.stat()
    except OSError:
        return False
    euid = os.geteuid() if hasattr(os, "geteuid") else None
    if parent_info.st_mode & stat.S_ISVTX and euid not in (info.st_uid, parent_info.st_uid):
        return False

    if info.st_dev != backup_device:
        return tree_replaceable(path)
    if stat.S_ISDIR(info.st_mode) and not os.access(path, os.W_OK):
        return False
    return True


def tree_replaceable(path):
    """Check if the current user can back up and delete a path and everything below it."""
    path = Path(path)
    if not os.access(path.parent, os.W_OK | os.X_OK):
        return False
    if path.is_symlink():
        return True
    if not path.is_dir():
        return os.access(path, os.R_OK)

    errors = []
    for root, _, files in os.walk(path, onerror=errors.append):
        if not os.access(root, os.R_OK | os.W_OK | os.X_OK):
            return False
        for name in files:
            file_path = os.path.join(root, name)
            if not os.path.islink(file_path) and not os.access(file_path, os.R_OK):
                return False
    return not errors


def archive_writable(archive, root, roll_ext):
    """Check if the current user can restore an archive without sudo."""
    root_path = expand_path(root) if root else Path("/")
    try:
        with tarfile.open(archive, "r:*") as tar:
            groups = archive_entries(tar, report=False)
    except (tarfile.TarError, ValueError):
        return True  # The restore itself reports the error
    except OSError:
        return False  # Unreadable archive, which root can read

    if roll_ext:
        # Rotating and creating <root><roll_ext> happen in its parent; each archive's
        # subdirectory is then created inside the fresh one
        backups = backup_dir(root_path, roll_ext)
        if not path_writable(backups.parent):
            return False
        try:
            # The fresh backup dir is created in its parent (an old one is moved aside)
            backup_device = path_device(backups.parent)
        except OSError:
            return False  # An unsearchable parent, which root can search
        for entry in groups:
            entry_path = root_path / entry
            if os.path.lexists(entry_path):
                if not entry_movable(entry_path, backup_device):
                    return False
            elif not path_writable(entry_path.parent):
                return False
        return True

    # Extraction without rollback rewrites existing files in place and creates the rest
    for members in groups.values():
        for member in members:
            target = root_path / member.name
            if os.path.lexists(target) and not target.is_symlink():
                if target.is_file() and not os.access(target, os.W_OK):
                    return False
            elif not path_writable(target.parent):
                return False
    return True


def restore_needs_sudo(selected, root_map, roll_ext, snapshot_config):
    """Check if restoring the selected archives and links needs sudo.

    Returns what needs it, for messages (an archive file name or 'symlink <path>'), or False.
    """
    for archive in selected:
        root = root_map.get(archive_category(archive))
        # Rollback restore skips categories without a root; a dry run has no plaintext
        # for an encrypted archive
        if (roll_ext and not root) or is_placeholder(archive):
            continue
        if not archive_writable(archive, root, roll_ext):
            return archive.name

    # create_symlinks() writes the links of the archives it restores, at most these
    tar_tables = snapshot_config.get("tar", {})
    for archive in selected:
        table = tar_tables.get(archive_category(archive), {})
        link, root = table.get("link"), table.get("root")
        if link and root:
            link_path = expand_path(link)
            if not os.path.lexists(link_path) and not path_writable(link_path.parent):
                return f"symlink {link_path}"
    return False


# Variables never passed to the sudo run (see sudo_restore)
SUDO_ENV_BLOCKED = re.compile(r"PATH|IFS|ENV|BASH_ENV|SHELLOPTS|PYTHON\w*|LD_\w*|DYLD_\w*")


def sudo_restore(args, root_dir, capture_dir, restore_config, snapshot_config, table="tar"):
    """Run this restore again under sudo, then exit with its status.

    The child restores from the local capture_dir with the same restore config, so
    remote pulls and captures aren't repeated as root and the caller's temp dirs
    still get cleaned up. Its molt root is the local root_dir (for a remote molt root,
    the local copy), so it never reads a host as root. sudo resets the environment, so
    HOME and the variables used by roots and links are passed through, or '$HOME' and
    '~' would expand to root's home.
    """
    names = {"HOME"}
    for section in snapshot_config.get("tar", {}).values():
        for key in ("root", "link"):
            names.update(re.findall(r"\$\{?(\w+)", str(section.get(key, ""))))
    # Never pass variables that change what the root run executes or loads, even if
    # snapshot.toml (which may come from another host) names them
    names = {name for name in names if not SUDO_ENV_BLOCKED.fullmatch(name)}
    env = {name: os.environ[name] for name in names if name in os.environ}
    env.setdefault("HOME", str(Path.home()))
    env_args = [f"{name}={value}" for name, value in sorted(env.items())]

    # Pass what restore() reads from the config through a temp file (it may come
    # from migrate.toml)
    child_config = restore_child_config(restore_config, table)

    # The root run is on this machine: give it the identity's expanded path, so it
    # doesn't depend on variables sudo doesn't pass
    identity = child_config.get("age", {}).get("identity")
    if isinstance(identity, str) and identity:
        child_config["age"]["identity"] = str(expand_path(identity))

    fd, config_path = tempfile.mkstemp(prefix=f"{__script__.stem}-", suffix=".toml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            try:
                f.write(toml_dumps(child_config))
            except (TypeError, AttributeError) as e:
                fatal(
                    "Cannot re-run the restore with sudo: the restore config holds an "
                    f"unsupported value: {e}"
                )

        # -s: no user site-packages in the root run (PYTHONPATH is never passed either)
        cmd = ["sudo", "env", *env_args, sys.executable, "-s", str(__script__), "restore"]
        cmd += ["--from", str(capture_dir), "-r", str(root_dir), "-t", config_path]
        if getattr(args, "disable_rollback", False):
            cmd.append("--disable-rollback")
        if getattr(args, "run_scripts", False):
            cmd.append("--run-scripts")
        if __verbose__:
            cmd.append("--verbose")

        # The child writes to the terminal directly; print everything before it first.
        # A closed or broken stdout or stderr stops the run here, before sudo, as it
        # always has
        echo(cmd, verbose=True)
        if sys.stdout is None or sys.stderr is None:
            fatal("Cannot re-run the restore with sudo: stdout or stderr is closed")
        flush_stream(sys.stdout, strict=True)
        flush_stream(sys.stderr, strict=True)
        child = subprocess.Popen(cmd)
        while True:
            try:
                returncode = child.wait()
                break
            except KeyboardInterrupt:
                # The child got the same Ctrl-C and is rolling back; wait for it
                continue
    finally:
        os.unlink(config_path)

    # A child killed by a signal reports -N; exit like a shell would (128 + N)
    code = 128 - returncode if returncode < 0 else returncode

    # The child printed its own output and final line; add the parent's view of a
    # failure. A broken stderr drops these lines but does not change the exit code
    action = "Re-running the restore with sudo"
    try:
        if returncode == -signal.SIGINT:
            error(f"{action} was interrupted")
        elif returncode < 0:
            try:
                name = signal.Signals(-returncode).name
            except ValueError:
                name = f"signal {-returncode}"
            error(f"{action} failed (killed by {name})")
        elif returncode != 0:
            error(f"{action} failed (exit status {returncode})")
    except (OSError, ValueError):
        # Send the unwritten text to /dev/null, so Python's exit-time flush can't fail
        # and replace the child's exit code with 120
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stderr.fileno())
        except (AttributeError, OSError, ValueError):
            pass
    sys.exit(code)


def restore_stop(args, selected, done, failed, checked):
    """Stop a restore in which archives failed, with one Error block (exit 1).

    done and failed are the restored and failed archives. checked says whether the
    snapshot has checksums, which a real run checks first and a dry run doesn't.
    """
    not_run = "not run: symlinks"
    stops = "stop before the symlinks"
    if args.run_scripts:
        not_run += ", after scripts"
        stops += ", after scripts"

    count = f"{len(failed)} of {plural(len(selected), 'archive')}"
    failed_names = ", ".join(archive.name for archive in failed)

    # A dry run only previewed the others, so it claims nothing was restored; a real
    # run restores the others first, then stops where this one does
    if __dry_run__:
        if done:
            hint = f"a real run would restore the others, then {stops}"
        else:
            hint = f"a real run would {stops}"
        if checked:
            hint = f"If the checksums match, {hint}"
        else:
            hint = hint[0].upper() + hint[1:]
        dry_run_stop(f"{count} cannot be restored: {failed_names}", hint)

    fatal(
        f"{count} failed to restore: {failed_names}",
        f"restored: {', '.join(archive.name for archive in done) or 'none'}",
        not_run,
    )


def restore(args, root_dir, **options):
    """Restore from local capture directory (see restore_snapshot).

    The temp dirs holding decrypted archives are removed however the restore ends,
    after a sudo re-run too.
    """
    scratch = []
    try:
        return restore_snapshot(args, root_dir, scratch=scratch, **options)
    finally:
        for directory in scratch:
            shutil.rmtree(directory, ignore_errors=True)


def restore_snapshot(
    args,
    root_dir,
    capture_dir=None,
    restore_config=None,
    label=None,
    table="tar",
    phase=None,
    scratch=None,
):
    """Restore from local capture directory.

    label names the snapshot in error lines (defaults to capture_dir); table names the
    restore config's [tar] table in messages (migrate.toml calls it [restore.tar]);
    phase ('restore' in a migration) prefixes the script steps. Encrypted archives are
    decrypted into temp dirs added to scratch, which the caller removes.
    Returns (summary, success) for the command's final line.
    """
    if scratch is None:
        scratch = []
    if capture_dir is None:
        capture_dir = args.capture_dir if __dry_run__ else args.capture_dir.resolve()

    verify_snapshot(capture_dir, label)

    # Verify all archives using snapshot TOML and load config. Its Warnings print after
    # the sudo check below, so a sudo re-run doesn't print them twice
    snapshot_config = verify_archives_from_toml(capture_dir, warn_unverified=False)

    # Only a dry run gets here without the snapshot dir (a pulled snapshot, or a missing
    # one already reported); a pulled one takes the verify step's place with a Note
    snapshot_found = capture_dir.exists()
    if not snapshot_found and is_placeholder(capture_dir):
        note("A real run would verify and restore the copied snapshot")

    # Get compression type and rollback extension from snapshot TOML
    tarball_opts = snapshot_config.get("tarball", {})
    compress = tarball_opts.get("compress", "gzip")
    roll_ext = tarball_opts.get("rollback")

    # Build root_map from tar sections
    root_map = {}
    tar_sections = snapshot_config.get("tar", {})
    for category, section_data in tar_sections.items():
        root = section_data.get("root")
        if root:
            root_map[category] = root

    # Load restore config (use provided or load from file)
    if restore_config is None:
        config_name = args.config_toml if args.config_toml else DEFAULT_CONFIG_RESTORE
        restore_config = load_config(root_dir, config_name)
        verify_restore_config(restore_config, config_name)
        # A config written for this run by another molt.py names the user's table, and
        # says when the user's config had no 'archives' key
        table = restore_config.get("molt", {}).get("table", table)

    # Get archives from config
    # Semantics: None/[] = skip restore, ["*"] = restore all, [patterns...] = restore matching
    # Patterns support glob matching: ["ssh*", "dotfiles", "*-config"]
    tar_table = restore_config.get("tar", {})
    archives = tar_table.get("archives", None)
    if not archives or archives == []:
        archives = None  # Empty list in config means don't restore
    elif archives == ["*"] or "*" in archives:
        archives = []  # Wildcard means restore all available

    # Match the archives snapshot.toml lists (any other archive file in the snapshot dir
    # is ignored); without the snapshot dir there is nothing to match
    selected, available, unmatched = [], [], []
    if archives is not None and snapshot_found:
        ext, _ = COMPRESS_MAP[compress]
        available = snapshot_archives(capture_dir, snapshot_config, ext)
        selected, unmatched = archive_select(available, archives)

    # Decrypt the selected .age archives into a private temp dir; the rest of the
    # restore (the sudo check included) reads the plaintext copies
    encrypted = [archive.name for archive in selected if archive.name.endswith(AGE_EXT)]
    if encrypted:
        identity = age_identity(restore_config, encrypted, table)
        selected = decrypt_archives(selected, identity, scratch)

    # Re-run with sudo if any restore target is not writable (Unix only)
    use_rollback = bool(roll_ext) and not getattr(args, "disable_rollback", False)
    can_sudo = hasattr(os, "geteuid") and os.geteuid() != 0
    needs_root = False
    if can_sudo:
        needs_root = restore_needs_sudo(
            selected, root_map, roll_ext if use_rollback else None, snapshot_config
        )
    if needs_root:
        # Symlinks are created, archives are restored
        action = "Creating" if needs_root.startswith("symlink ") else "Restoring"
        if __dry_run__:
            note(f"{action} {needs_root} needs root; a real run would re-run with sudo")
        else:
            note(f"{action} {needs_root} needs root")
            step("Re-running the restore with sudo")
            sudo_restore(
                args, root_dir, capture_dir, restore_config, snapshot_config, table
            )

    # Past the sudo re-run, which exits after the child printed these itself
    if snapshot_found and not tarball_opts.get("checksum"):
        warn(UNVERIFIED_WARNING)
    for pattern in unmatched:
        warn(f"[{table}] archives pattern '{pattern}' matches no archive in the snapshot")

    # Run before scripts
    if args.run_scripts:
        run_scripts(root_dir, restore_config, "before", working_dir=capture_dir, phase=phase)

    # Name the selection in the step header: '4 archives' or '2 of 4 archives'
    if len(selected) == len(available):
        selection = plural(len(selected), "archive")
    else:
        selection = f"{len(selected)} of {plural(len(available), 'archive')}"

    # Restore archives; done and failed collect the archives restored and failed
    done, failed = [], []
    no_archives_key = restore_config.get("molt", {}).get("no_archives_key", False)
    if archives is None:
        if "archives" in tar_table and not no_archives_key:
            step_skipped("archive restore", f"[{table}] archives is empty")
        else:
            step_skipped("archive restore", f"[{table}] has no 'archives' key")
    elif not selected:
        # A dry run without the snapshot dir already printed a Note or an Error
        if snapshot_found:
            step_skipped("archive restore", f"no archive matches [{table}] archives")
    elif getattr(args, "disable_rollback", False):
        # No rollback protection - use existing extract_archives with confirmation
        step(f"Restoring {selection} without backups")
        extract_archives(
            selected, root_map=root_map, compress=compress, restored=done, failed=failed
        )
    elif not roll_ext:
        # Transactional restore needs a rollback extension; ask per archive instead
        warn(
            f"{DEFAULT_SNAPSHOT_TOML} sets no [tarball] rollback; existing files are not "
            "backed up",
            "Each archive asks before it is restored; set 'rollback' in the capture "
            "config's [tarball] table to get backups",
        )
        step(f"Restoring {selection} without backups")
        extract_archives(
            selected, root_map=root_map, compress=compress, restored=done, failed=failed
        )
    else:
        # Transactional restore with rollback support; each root's old backup directory
        # is moved aside once in this run
        step(f"Restoring {selection}")
        rotated = set()
        for archive in selected:
            category = archive_category(archive)
            root = root_map.get(category)
            if not root:
                say(archive.name)
                say(f"skip: no 'root' key in {DEFAULT_SNAPSHOT_TOML}", 2)
                continue
            if restore_category(archive, root, roll_ext, compress, rotated):
                done.append(archive)
            else:
                failed.append(archive)

    # A failed archive stops the restore before the symlinks and after scripts. Declined
    # archives are not failures
    if failed:
        restore_stop(args, selected, done, failed, checked=bool(tarball_opts.get("checksum")))
    restored = len(done)

    # Create symlinks from link -> root, only for the archives restored
    links = create_symlinks(snapshot_config, {archive_category(archive) for archive in done})

    # Run after scripts
    if args.run_scripts:
        run_scripts(root_dir, restore_config, "after", working_dir=capture_dir, phase=phase)

    # Summary for the final line; no ✓ when selected archives ended with none restored
    success = True
    if archives is None:
        summary = "no archives selected"
    elif restored == 0:
        summary = "no archives restored"
        success = False
    elif restored == len(selected):
        summary = f"{plural(restored, 'archive')} restored"
    else:
        summary = f"{restored} of {plural(len(selected), 'archive')} restored"
    if links:
        summary += f", {plural(links, 'symlink')} created"
    return summary, success


def remote_pull_dir(name):
    """Return a new local temp dir for a snapshot copied from a host (a stand-in in a dry run)."""
    if __dry_run__:
        return Path(DRY_RUN_DIR) / name
    return Path(tempfile.mkdtemp(prefix=f"{__script__.stem}-"))


def snapshot_pull(host, path, local_dir):
    """Copy a snapshot directory on a host, with every file in it, into local_dir."""
    step(f"Copying snapshot from {host}")
    rsync_copy(f"{host}:{path}/", f"{local_dir}/", f"the snapshot from {host}", label="snapshot")


def remote_restore(args, root_dir, source_host):
    """Copy a snapshot from a host into a local temp dir and restore it here.

    The snapshot is args.capture_dir on the host. Returns restore()'s result; the temp
    dir is removed however the restore ends.
    """
    capture_dir_path = str(args.capture_dir)
    tmpdir = remote_pull_dir("remote-restore")
    try:
        snapshot_pull(source_host, capture_dir_path, tmpdir)

        # Errors name what the user asked for, not the temp copy
        label = f"{source_host}:{capture_dir_path}"
        return restore(args, root_dir, capture_dir=tmpdir, label=label)

    finally:
        if not __dry_run__:
            shutil.rmtree(tmpdir, ignore_errors=True)


def remote_relay(args, root_dir, source_host, dest_host):
    """Restore a snapshot from one host on another, through a local temp dir.

    rsync can't copy from one host to another, so the snapshot (args.capture_dir on
    source_host) is copied here first, then to dest_host. The local copy is removed
    however the restore ends.
    """
    capture_dir_path = str(args.capture_dir)
    tmpdir = remote_pull_dir("remote-restore")
    try:
        snapshot_pull(source_host, capture_dir_path, tmpdir)
        label = f"{source_host}:{capture_dir_path}"
        remote_deploy(args, root_dir, dest_host, tmpdir, label=label)
    finally:
        if not __dry_run__:
            shutil.rmtree(tmpdir, ignore_errors=True)


def remote_deploy(args, root_dir, dest_host, local_capture, deploy_config=None, label=None):
    """Copy a local snapshot to a host and restore it there with molt.py.

    molt.py on the host restores its copy as a local restore does: it re-runs itself with
    sudo only when the restore needs root, and with --run-scripts it runs the config's
    scripts. Without deploy_config the restore config comes from -t or the default in
    root_dir (a remote molt root's local copy included); migrate passes its [restore.*]
    half, written to a temp TOML. label names the snapshot in error lines. The work
    directory on dest_host is removed however the restore ends.
    """
    if label is None:
        label = local_capture

    # A dry run reports a missing snapshot dir here, where a real run stops (and passes
    # over a stand-in for one a real run would create)
    if not __dry_run__ or not local_capture.exists():
        verify_snapshot(local_capture, label)
    elif not (local_capture / DEFAULT_SNAPSHOT_TOML).exists():
        error(
            f"{DEFAULT_SNAPSHOT_TOML} not found in {label}",
            "A real run stops before creating the work directory",
        )

    # The config is read first, so a config problem stops before the host is touched
    if deploy_config is None:
        if args.config_toml:
            config_name = args.config_toml
        elif (root_dir / DEFAULT_CONFIG_DEPLOY).exists():
            config_name = DEFAULT_CONFIG_DEPLOY
        else:
            config_name = DEFAULT_CONFIG_RESTORE
        deploy_config = load_config(root_dir, config_name)
        verify_restore_config(deploy_config, config_name)
        config_src = root_dir / config_name
    else:
        config_src = None  # migrate: written to a temp TOML below
    scripts = []
    if args.run_scripts:
        scripts = remote_scripts(root_dir, deploy_config, dest_host)

    tmp_config = None
    work_dir = None
    done = False
    try:
        if config_src is None:
            tmp_config = write_config_toml(
                restore_child_config(deploy_config, "restore.tar"),
                DEFAULT_CONFIG_RESTORE,
                f"the restore on {dest_host}",
            )
            config_src = tmp_config

        layout = remote_layout(["configs", "scripts", "snapshot"], scripts)
        work_dir = remote_workdir(dest_host, "restore", layout)

        # The whole snapshot directory, with any files after-capture scripts wrote into
        # it, then molt.py, the config and the scripts; all in parallel
        transfers = remote_copies(
            root_dir, dest_host, work_dir, config_src, DEFAULT_CONFIG_RESTORE, scripts
        )
        files = plural(len(transfers), "file")
        transfers.insert(0, (f"{local_capture}/", f"{dest_host}:{work_dir}/snapshot/", "snapshot"))
        step(f"Copying the snapshot and {files} to {dest_host}")
        success_count = rsync_parallel(transfers)
        if success_count < len(transfers):
            failed = len(transfers) - success_count
            fatal(f"Copying to {dest_host} failed for {failed} of {len(transfers)} copies")

        # molt.py on the host decides whether it needs sudo, and asks for a password (or,
        # with --disable-rollback, for each archive) on the terminal. It takes as long as
        # the archives do: no wall-clock limit
        options = ["--from", work_dir / "snapshot", "-r", work_dir, "-t", DEFAULT_CONFIG_RESTORE]
        if getattr(args, "disable_rollback", False):
            options.append("--disable-rollback")
        if args.run_scripts:
            options.append("--run-scripts")
        step(f"Running restore on {dest_host}")
        code, _, _ = ssh_run(
            dest_host,
            remote_child(work_dir, "restore", *options),
            check=False,
            tty=True,
            desc="Restore",
            timeout=None,
        )
        # With ssh -t, Ctrl-C reaches molt.py on the host, which rolls back and exits
        # 130 (or dies from SIGINT); report and exit like an interrupted local restore
        if code == 130 or code == -signal.SIGINT:
            error(f"Restore on {dest_host} was interrupted")
            sys.exit(130)
        if code == 255:
            fatal(f"Restore failed on {dest_host}: ssh error (exit status 255)")
        if code != 0:
            fatal(f"Restore failed on {dest_host} (exit status {code})")
        done = True
    finally:
        if tmp_config and tmp_config.exists():
            tmp_config.unlink()
        if work_dir is not None:
            remote_workdir_remove(dest_host, work_dir, report=done)


# --- Migrate Logic --- #


def split_migrate_config(config, config_name):
    """Split migrate config into separate capture and restore configs.

    Migrate config has [capture.*] and [restore.*] namespaces with shared
    [tarball] options. Returns (capture_config, restore_config) dicts
    compatible with existing capture() and restore() functions.
    """
    tarball = config.get("tarball", {})
    capture_section = config.get("capture", {})
    restore_section = config.get("restore", {})

    if "tar" not in capture_section:
        fatal(f"{config_name}: missing required [capture.tar] table")

    capture_config = {"tarball": tarball, "tar": capture_section.get("tar", {})}
    for key in ("scripts", "age", "install"):
        if key in capture_section:
            capture_config[key] = capture_section[key]

    restore_config = {"tarball": tarball}
    for key in ("tar", "scripts", "age"):
        if key in restore_section:
            restore_config[key] = restore_section[key]

    return capture_config, restore_config


# --- Snapshot List and Prune --- #

# A rotated backup dir's name: <root><rollback>_YYYYmmdd_HHMMSS, plus _<n> when taken
ROTATED_SUFFIX = re.compile(r"_(\d{8}_\d{6})(?:_\d+)?")


def snapshots_find(captures_dir):
    """List the snapshots in a captures directory, newest first.

    A snapshot is a captures/YYYY/MM-DD/<id> dir with a snapshot.toml, and a real date.
    Symlinked dirs are left out, so nothing outside captures_dir is ever listed or
    pruned. Each is a Namespace: path, rel (YYYY/MM-DD/<id>), date, config (the parsed
    snapshot.toml, or None when it can't be read). Newest is the latest date, then the
    latest snapshot.toml.
    """
    if not captures_dir.is_dir():
        return []

    snapshots = []
    for toml_path in captures_dir.glob(SNAPSHOT_TOML_GLOB):
        path = toml_path.parent
        if any(os.path.islink(p) for p in (path, path.parent, path.parent.parent)):
            continue
        try:
            date = datetime.strptime(f"{path.parent.parent.name}-{path.parent.name}", "%Y-%m-%d")
            mtime = toml_path.stat().st_mtime
        except (ValueError, OSError):
            continue
        try:
            with open(toml_path, "rb") as f:
                config = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            config = None
        rel = path.relative_to(captures_dir).as_posix()
        snapshots.append(
            argparse.Namespace(path=path, rel=rel, date=date.date(), mtime=mtime, config=config)
        )
    snapshots.sort(key=lambda found: (found.date, found.mtime, found.rel), reverse=True)
    return snapshots


def snapshot_tables(snapshot):
    """Return a snapshot's [tar.<name>] tables (empty when snapshot.toml can't be read)."""
    tables = (snapshot.config or {}).get("tar", {})
    if not isinstance(tables, dict):
        return {}
    return {name: table for name, table in tables.items() if isinstance(table, dict)}


def backup_dirs_find(snapshots):
    """List the backup dirs of the roots the snapshots name, one Namespace per root.

    Each has base (<root><rollback>), current (base when it exists) and rotated, the
    <base>_<timestamp>[_n] dirs as (path, timestamp), newest first. Only absolute roots
    count, and symlinks are never listed.
    """
    bases = {}
    for snapshot in snapshots:
        tarball = (snapshot.config or {}).get("tarball", {})
        roll_ext = tarball.get("rollback") if isinstance(tarball, dict) else None
        if not isinstance(roll_ext, str) or not roll_ext or "/" in roll_ext:
            continue
        for table in snapshot_tables(snapshot).values():
            root = table.get("root")
            if not isinstance(root, str) or not root:
                continue
            root_path = expand_path(root)
            if not root_path.is_absolute() or root_path == Path(root_path.anchor):
                continue  # An unset variable, or '/': no backup dir molt.py would use
            base = backup_dir(root_path, roll_ext)
            bases.setdefault(os.path.realpath(base), base)

    found = []
    for base in bases.values():
        current = base if base.is_dir() and not base.is_symlink() else None
        rotated = []
        try:
            names = os.listdir(base.parent)
        except OSError:
            names = []
        for name in names:
            match = ROTATED_SUFFIX.fullmatch(name[len(base.name):])
            if not name.startswith(base.name) or not match:
                continue
            path = base.parent / name
            if path.is_symlink() or not path.is_dir():
                continue
            try:
                stamp = datetime.strptime(match.group(1), "%Y%m%d_%H%M%S")
            except ValueError:
                continue
            rotated.append((path, stamp))
        rotated.sort(key=lambda item: (item[1], item[0].name), reverse=True)
        if current or rotated:
            found.append(argparse.Namespace(base=base, current=current, rotated=rotated))
    found.sort(key=lambda backups: str(backups.base))
    return found


def tree_size(path):
    """Return the total size of the files below a path (symlinks not followed).

    The second value is False when part of the tree can't be read, so the size is low.
    """
    errors = []
    total = 0
    for parent, dirs, files in os.walk(path, onerror=errors.append):
        for name in files + [d for d in dirs if os.path.islink(os.path.join(parent, d))]:
            try:
                total += os.lstat(os.path.join(parent, name)).st_size
            except OSError as e:
                errors.append(e)
    return total, not errors


def format_size(size):
    """Format a byte count for display: '512 B', '4.0 KB', '12.3 MB'."""
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    unit = 0
    while value >= 1024 and unit < len(units) - 1:
        value /= 1024
        unit += 1
    if unit == 0:
        return f"{size} B"
    return f"{value:.1f} {units[unit]}"


def size_shown(path):
    """Return a dir's size for display, marked when part of it can't be read."""
    size, complete = tree_size(path)
    if complete:
        return format_size(size)
    return f"{format_size(size)}+ (partly unreadable)"


def tree_removable(path):
    """Check if the current user can delete a dir and everything below it without sudo.

    That needs write and search on its parent (and, in a sticky dir, owning it or the
    parent), and every dir inside readable, writable and searchable, or owned (so
    remove_path can make it writable).
    """
    path = Path(path)
    parent = path.parent
    if not os.access(parent, os.W_OK | os.X_OK):
        return False
    euid = os.geteuid() if hasattr(os, "geteuid") else None
    try:
        info, parent_info = path.lstat(), parent.stat()
    except OSError:
        return False
    if parent_info.st_mode & stat.S_ISVTX and euid not in (info.st_uid, parent_info.st_uid):
        return False

    errors = []
    for directory, _, _ in os.walk(path, onerror=errors.append):
        if os.access(directory, os.R_OK | os.W_OK | os.X_OK):
            continue
        try:
            if os.lstat(directory).st_uid != euid:
                return False
        except OSError:
            return False
    return not errors


def prune_select(items, keep, cutoff):
    """Pick the items prune deletes, from (date, item) pairs listed newest first.

    --keep N keeps the newest N; --older-than keeps those dated on or after cutoff. An
    item goes only when no rule that was given keeps it.
    """
    doomed = []
    for index, (day, item) in enumerate(items):
        if keep is not None and index < keep:
            continue
        if cutoff is not None and day >= cutoff:
            continue
        doomed.append(item)
    return doomed


def columns(rows):
    """Format rows of cells as lines, each column padded to its widest cell."""
    widths = {}
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths.get(index, 0), len(cell))
    lines = []
    for row in rows:
        cells = [cell.ljust(widths[index]) for index, cell in enumerate(row)]
        lines.append("  ".join(cells).rstrip())
    return lines


def removable_or_warn(path):
    """Check that a dir can be deleted without sudo; if not, warn with the command to run."""
    if tree_removable(path):
        return True
    warn(
        f"{path} needs root to delete; it is kept",
        f"Run: sudo rm -rf {shlex.quote(str(path))}",
    )
    return False


def prune_snapshots(snapshots, keep, cutoff):
    """Print the snapshot part of a prune plan; return the snapshots to delete."""
    step(f"Checking {plural(len(snapshots), 'snapshot')}")
    doomed = prune_select([(snapshot.date, snapshot) for snapshot in snapshots], keep, cutoff)
    deletable = []
    for snapshot in doomed:
        if removable_or_warn(snapshot.path):
            say(f"delete: {snapshot.rel} ({size_shown(snapshot.path)})")
            deletable.append(snapshot)

    kept = [snapshot for snapshot in snapshots if snapshot not in deletable]
    if __verbose__:
        for snapshot in kept:
            say(f"keep: {snapshot.rel}")
    elif kept:
        say(f"keep: {plural(len(kept), 'snapshot')}")
    return deletable


def prune_backups(backups, keep, cutoff):
    """Print the backup part of a prune plan; return the rotated backup dirs to delete.

    The rules apply to each root's rotated dirs by their timestamp; the current
    <root><rollback> is always kept.
    """
    rotated = sum(len(root.rotated) for root in backups)
    step(f"Checking {plural(rotated, 'rotated backup dir')}")
    deletable, kept = [], []
    for root in backups:
        if root.current:
            kept.append(f"{root.current} (current)")
        items = [(stamp.date(), path) for path, stamp in root.rotated]
        doomed = prune_select(items, keep, cutoff)
        for path, _ in root.rotated:
            if path in doomed and removable_or_warn(path):
                say(f"delete: {path} ({size_shown(path)})")
                deletable.append(path)
            else:
                kept.append(str(path))

    if __verbose__:
        for line in kept:
            say(f"keep: {line}")
    elif kept:
        say(f"keep: {plural(len(kept), 'backup dir')}")
    return deletable


def prune_counts(snapshots, backups, with_backups):
    """Name what a prune deletes: '2 snapshots and 1 backup dir'."""
    text = plural(snapshots, "snapshot")
    if with_backups:
        text += f" and {plural(backups, 'backup dir')}"
    return text


def prune_delete(captures_dir, snapshots, backups):
    """Delete the planned snapshots and backup dirs; return how many of each went.

    A dir that can't be deleted is an Error, and the rest still go. Date dirs that the
    deleted snapshots leave empty are removed too; captures_dir itself stays.
    """
    deleted_snapshots = 0
    for snapshot in snapshots:
        try:
            remove_path(snapshot.path)
            deleted_snapshots += 1
            ok(f"{snapshot.rel} deleted")
        except OSError as e:
            error(f"Cannot delete {snapshot.path}: {e.strerror or e}")

        # The MM-DD dir, then the YYYY dir, when nothing is left in them
        for directory in (snapshot.path.parent, snapshot.path.parent.parent):
            if directory == captures_dir or captures_dir not in directory.parents:
                break
            try:
                directory.rmdir()
            except OSError:
                break  # Not empty (or already gone)
            if __verbose__:
                say(f"removed: {directory.relative_to(captures_dir).as_posix()} (empty)", 2)

    deleted_backups = 0
    for path in backups:
        try:
            remove_path(path)
            deleted_backups += 1
            ok(f"{path} deleted")
        except OSError as e:
            error(f"Cannot delete {path}: {e.strerror or e}")
    return deleted_snapshots, deleted_backups


# --- Install Logic --- #

# The sections of install.toml, in the order they install unless [install] order sets
# another. Homebrew goes first, since it installs most other tools; custom goes before
# the language tools, so its commands can install their managers (rustup, uv); manual
# goes last, as the list of what is left to install by hand
INSTALL_SECTIONS = ["brew", "mas", "custom", "mise", "npm", "cargo", "uv", "vscode", "manual"]

# Where package managers put their commands on a new machine, before a shell has put them
# on PATH. Install commands get the ones that exist after PATH's own entries, so a tool
# that one section installs is found by the next
INSTALL_PATH_DIRS = [
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "~/.cargo/bin",
    "~/.local/bin",
    "/Applications/Visual Studio Code.app/Contents/Resources/app/bin",
]

# Homebrew installs what is missing and never upgrades what is already installed
INSTALL_BREW_ENV = {
    "HOMEBREW_NO_INSTALL_UPGRADE": "1",
    "HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK": "1",
}

# Where apps are looked for: a [[manual]] 'app', App Store apps, and the apps a draft lists
INSTALL_APP_DIRS = ["/Applications", "~/Applications"]

# Taps every Homebrew has, and global npm packages that come with node; drafts skip them
BREW_DEFAULT_TAPS = {"homebrew/core", "homebrew/cask"}
NPM_BUNDLED = {"npm", "corepack"}

# The file a draft is written to in a snapshot, with 'draft = true' in capture.toml
DRAFT_INSTALL_TOML = "install.toml"


class InstallItem:
    """One thing a section installs: key matches what is installed, label names it."""

    def __init__(self, key, label, value=None):
        self.key = key
        self.label = label
        self.value = key if value is None else value


class InstallState:
    """What is installed of a group: the installed keys, and labels of what isn't in the config."""

    def __init__(self, installed, extra=()):
        self.installed = set(installed)
        self.extra = list(extra)


def install_env():
    """Return the environment install commands run in: PATH gains INSTALL_PATH_DIRS."""
    paths = [path for path in os.environ.get("PATH", "").split(os.pathsep) if path]
    for entry in INSTALL_PATH_DIRS:
        path = str(Path(entry).expanduser())
        if path not in paths and os.path.isdir(path):
            paths.append(path)
    return dict(os.environ, PATH=os.pathsep.join(paths), **INSTALL_BREW_ENV)


def install_which(tool):
    """Find a command on the install PATH; return its path or None."""
    return shutil.which(tool, path=install_env()["PATH"])


def install_query(argv, what, any_status=False):
    """Run a read-only command and return its stdout, or None after an Error line.

    Queries change nothing, so they run in a dry run too. what names the query in the
    Error line ('list the Homebrew casks'). any_status accepts any exit status, for
    tools that exit 1 with a usable answer (npm ls with extraneous packages).
    """
    echo(argv, 1, verbose=True, runs=True)
    try:
        result = subprocess.run(
            argv,
            env=install_env(),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=COMMAND_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        error(f"Cannot {what}: {argv[0]} timed out after {COMMAND_TIMEOUT}s")
        return None
    except OSError as e:
        error(f"Cannot {what}: {e.strerror or e}")
        return None
    if result.returncode != 0 and not any_status:
        relay(result.stderr, 1, sys.stderr)
        error(f"Cannot {what} ({Path(argv[0]).name} exit status {result.returncode})")
        return None
    return result.stdout


def install_lines(text):
    """Split command output into its non-empty, stripped lines."""
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def install_check(command):
    """Run a check command with bash: exit status 0 means installed. It runs in a dry run too."""
    argv = ["bash", "-c", command]
    echo(argv, 1, verbose=True, runs=True)
    try:
        result = subprocess.run(
            argv,
            env=install_env(),
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=COMMAND_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        warn(f"Check '{command}' timed out after {COMMAND_TIMEOUT}s; counting it as missing")
        return False
    except OSError as e:
        warn(f"Cannot run check '{command}': {e.strerror or e}; counting it as missing")
        return False
    return result.returncode == 0


def install_run(argv):
    """Run one install command on the terminal; return its exit status.

    Installers ask for things (a sudo password for a cask's pkg, an App Store sign-in, a
    'curl | sh' installer's questions), so the command keeps the terminal, as the sudo
    re-run does. Without a terminal its stdin is /dev/null, so a prompt fails instead of
    waiting. A dry run only shows the command.
    """
    echo(argv, 1, verbose=True)
    if __dry_run__:
        return 0
    try:
        interactive = sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError):
        interactive = False
    flush_stream(sys.stdout)
    flush_stream(sys.stderr)
    try:
        result = subprocess.run(
            argv, env=install_env(), stdin=None if interactive else subprocess.DEVNULL
        )
    except OSError as e:
        error(f"Cannot run {argv[0]}: {e.strerror or e}")
        return 127
    return result.returncode


def install_names(table, key, where, config_name):
    """Return the array of names at table[key] (default empty), or stop on a bad one."""
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        fatal(f"{config_name}: the '{key}' key in {where} must be an array of names")
    return value


def install_keys(table, keys, where, config_name):
    """Stop on a key the table doesn't take, so a typo isn't silently ignored."""
    unknown = [key for key in table if key not in keys]
    if unknown:
        fatal(
            f"{config_name}: unknown key '{unknown[0]}' in {where}",
            f"{where} takes: {', '.join(keys)}",
        )


def folder_app(folder):
    """Pick the main app of a folder of apps (KiCad/KiCad.app), or None if it has none.

    That is the shortest name that starts with the folder's name, else the shortest
    name; uninstallers are never picked.
    """
    try:
        apps = [p for p in folder.iterdir() if p.suffix == ".app"]
    except OSError:
        return None
    apps = [app for app in apps if not app.name.startswith("Uninstall")]
    if not apps:
        return None
    named = [app for app in apps if app.name.lower().startswith(folder.name.lower())]
    return min(named or apps, key=lambda app: (len(app.name), app.name))


def app_bundles():
    """List the .app bundles in INSTALL_APP_DIRS, and the main app of each folder there."""
    apps = []
    for entry in INSTALL_APP_DIRS:
        base = Path(entry).expanduser()
        try:
            children = sorted(base.iterdir())
        except OSError:
            continue
        for child in children:
            if child.suffix == ".app":
                apps.append(child)
            elif child.is_dir() and not child.is_symlink():
                app = folder_app(child)
                if app:
                    apps.append(app)
    return apps


def app_find(app):
    """Check if an app is installed: a path, or a name looked for in INSTALL_APP_DIRS."""
    if not app.endswith(".app"):
        app += ".app"
    if app.startswith(("/", "~")):
        return expand_path(app).exists()
    return any((Path(entry).expanduser() / app).exists() for entry in INSTALL_APP_DIRS)


def app_store_ids(apps):
    """Map each app bundle with an App Store id to that id, from Spotlight's metadata."""
    if not apps or not shutil.which("mdls"):
        return {}
    argv = ["mdls", "-raw", "-nullMarker", "", "-name", "kMDItemAppStoreAdamID"]
    out = install_query(argv + [str(app) for app in apps], "read the App Store ids")
    if out is None:
        return {}
    ids = {}
    # mdls ends each value with NUL, so the split has one more part than there are apps
    for app, value in zip(apps, out.split("\0"), strict=False):
        if value.strip().isdigit():
            ids[app] = int(value.strip())
    return ids


def app_bundle_id(app):
    """Read an app's bundle id from its Info.plist, or '' if it can't be read."""
    try:
        with open(app / "Contents" / "Info.plist", "rb") as f:
            return str(plistlib.load(f).get("CFBundleIdentifier", ""))
    except Exception:
        # A broken Info.plist raises whatever its parser does (ExpatError, ValueError);
        # the app then just isn't known to be Apple's
        return ""


def ruby_string(value):
    """Format a string for a Brewfile (Ruby): a TOML basic string, with '#' escaped."""
    return toml_string(value).replace("#", "\\#")


def brew_short(name):
    """Name a formula or cask as 'brew list' does: without its tap, in lowercase."""
    return name.rsplit("/", 1)[-1].lower()


def brew_bundle(lines):
    """Install a Brewfile of lines with 'brew bundle install'; return failure details."""
    if __verbose__:
        say(labeled("Brewfile:", "\n".join(lines)))
    if __dry_run__:
        install_run(["brew", "bundle", "install", "--no-upgrade", f"--file={DRY_RUN_DIR}/Brewfile"])
        return []
    fd, path = tempfile.mkstemp(prefix=f"{__script__.stem}-", suffix=".Brewfile")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        code = install_run(["brew", "bundle", "install", "--no-upgrade", f"--file={path}"])
    finally:
        os.unlink(path)
    return [] if code == 0 else [f"brew bundle exit status {code}"]


def install_commands(argvs):
    """Run install commands in order; return a failure detail for each one that failed."""
    failures = []
    for argv in argvs:
        code = install_run(argv)
        if code != 0:
            failures.append(f"{shlex.join(argv)}: exit status {code}")
    return failures


class InstallGroup:
    """A unit of install.toml that installs together: a built-in section, or one [[custom]].

    A subclass lists its items, reports what is installed, and installs the rest. A new
    package manager is a subclass with a section name in INSTALL_SECTIONS and an entry
    in INSTALL_GROUPS; draft() lets 'install --draft' fill its section in.
    """

    section = ""
    tool = None
    keys = ()

    def __init__(self, table, config_name):
        self.table = table
        self.config_name = config_name
        self.title = self.section
        install_keys(table, self.keys, f"[{self.section}]", config_name)

    def unavailable(self):
        """Return why this group can't run here (its tool is missing), or None."""
        if self.tool and not install_which(self.tool):
            return f"{self.tool} not found"
        return None

    def items(self):
        """Return the InstallItems the config lists."""
        return []

    def present(self):
        """Return an InstallState, or None after an Error line."""
        return InstallState(())

    def install(self, missing):
        """Install the missing items; return failure details for the Error line."""
        return []

    @staticmethod
    def draft():
        """Return this section's table for a draft of what is installed, or None."""
        return None


class NamesGroup(InstallGroup):
    """A section that is one array of names, listed by one command and installed by another."""

    names_key = ""
    case_blind = False

    def key(self, name):
        return name.lower() if self.case_blind else name

    def items(self):
        names = install_names(self.table, self.names_key, f"[{self.section}]", self.config_name)
        return [InstallItem(self.key(name), f"'{name}'", name) for name in names]

    def listed(self):
        """Return the names installed on this machine, or None after an Error line."""
        return []

    def present(self):
        names = self.listed()
        if names is None:
            return None
        wanted = {item.key for item in self.items()}
        installed = {self.key(name) for name in names}
        extra = [f"'{name}'" for name in names if self.key(name) not in wanted]
        return InstallState(installed, extra)


class BrewGroup(InstallGroup):
    """[brew]: taps, formulae and casks, installed with 'brew bundle'."""

    section = "brew"
    tool = "brew"
    keys = ("taps", "formulae", "casks")

    # Config key, label, Brewfile keyword
    KINDS = [("taps", "tap", "tap"), ("formulae", "formula", "brew"), ("casks", "cask", "cask")]

    def items(self):
        items = []
        for key, kind, keyword in self.KINDS:
            for name in install_names(self.table, key, "[brew]", self.config_name):
                ident = name.lower() if kind == "tap" else brew_short(name)
                line = f"{keyword} {ruby_string(name)}"
                items.append(InstallItem(f"{kind}:{ident}", f"{kind} '{name}'", line))
        return items

    @staticmethod
    def query():
        """Return the taps, formulae, casks and leaves, or None after an Error line."""
        queries = [
            (["brew", "tap"], "list the Homebrew taps"),
            (["brew", "list", "--formula", "-1"], "list the Homebrew formulae"),
            (["brew", "list", "--cask", "-1"], "list the Homebrew casks"),
            (["brew", "leaves", "--installed-on-request"], "list the Homebrew leaves"),
        ]
        outputs = [install_query(argv, what) for argv, what in queries]
        if any(out is None for out in outputs):
            return None
        return [install_lines(out) for out in outputs]

    def present(self):
        found = self.query()
        if found is None:
            return None
        taps, formulae, casks, leaves = found
        installed = {f"tap:{tap.lower()}" for tap in taps}
        installed |= {f"formula:{brew_short(name)}" for name in formulae}
        installed |= {f"cask:{brew_short(name)}" for name in casks}

        # Not in the config: taps of its own, formulae installed on request, and casks
        wanted = {item.key for item in self.items()}
        extra = [f"tap '{tap}'" for tap in taps
                 if tap.lower() not in BREW_DEFAULT_TAPS and f"tap:{tap.lower()}" not in wanted]
        extra += [f"formula '{name}'" for name in leaves
                  if f"formula:{brew_short(name)}" not in wanted]
        extra += [f"cask '{name}'" for name in casks if f"cask:{brew_short(name)}" not in wanted]
        return InstallState(installed, extra)

    def install(self, missing):
        return brew_bundle([item.value for item in missing])

    @staticmethod
    def draft():
        found = BrewGroup.query() if install_which("brew") else None
        if found is None:
            return None
        taps, _, casks, leaves = found
        return {
            "taps": [tap for tap in taps if tap.lower() not in BREW_DEFAULT_TAPS],
            "formulae": leaves,
            "casks": casks,
        }


class MasGroup(InstallGroup):
    """[mas]: Mac App Store apps by name and id, installed with mas through 'brew bundle'.

    What is installed comes from Spotlight's App Store ids, so it needs neither mas nor
    an App Store sign-in; an app the index hasn't seen yet counts when its name is in an
    app folder.
    """

    section = "mas"
    tool = "brew"
    keys = ("apps",)

    def items(self):
        apps = self.table.get("apps", {})
        if not isinstance(apps, dict) or not all(
            isinstance(app_id, int) and not isinstance(app_id, bool) and app_id > 0
            for app_id in apps.values()
        ):
            fatal(
                f"{self.config_name}: [mas.apps] must map app names to App Store ids",
                'For example: Xcode = 497799835',
            )
        return [
            InstallItem(f"mas:{app_id}", f"app '{name}'", (name, app_id))
            for name, app_id in apps.items()
        ]

    def present(self):
        apps = app_bundles()
        ids = app_store_ids(apps)
        installed = {f"mas:{app_id}" for app_id in ids.values()}
        installed |= {item.key for item in self.items() if app_find(item.value[0])}
        wanted = {item.key for item in self.items()}
        extra = [f"app '{app.stem}'" for app, app_id in ids.items()
                 if f"mas:{app_id}" not in wanted]
        return InstallState(installed, extra)

    def install(self, missing):
        lines = [] if install_which("mas") else ['brew "mas"']
        lines += [f"mas {ruby_string(name)}, id: {app_id}" for name, app_id in
                  (item.value for item in missing)]
        return brew_bundle(lines)

    @staticmethod
    def draft():
        ids = app_store_ids(app_bundles())
        if not ids:
            return None
        return {"apps": {app.stem: app_id for app, app_id in sorted(ids.items())}}


def mise_satisfied(versions, requested):
    """Check if installed versions satisfy a requested one: '3.12' or '3.12.1' by prefix.

    An alias ('latest', 'lts') is satisfied by any installed version, since which
    version it names changes over time.
    """
    if not requested[:1].isdigit():
        return bool(versions)
    return any(v == requested or v.startswith(requested + ".") for v in versions)


class MiseGroup(InstallGroup):
    """[mise]: runtimes by tool and version. global = true (the default) also makes them the
    global versions ('mise use --global'); false only installs them ('mise install')."""

    section = "mise"
    tool = "mise"
    keys = ("global", "tools")

    def tools(self):
        tools = self.table.get("tools", {})
        if not isinstance(tools, dict):
            fatal(f"{self.config_name}: [mise.tools] must map tools to versions")
        result = {}
        for name, versions in tools.items():
            if isinstance(versions, str):
                versions = [versions]
            if not isinstance(versions, list) or not versions or not all(
                isinstance(v, str) and v for v in versions
            ):
                fatal(
                    f"{self.config_name}: '{name}' in [mise.tools] must be a version or an "
                    "array of versions"
                )
            result[name] = versions
        if not isinstance(self.table.get("global", True), bool):
            fatal(f"{self.config_name}: the 'global' key in [mise] must be true or false")
        return result

    def items(self):
        return [
            InstallItem(f"{name}@{version}", f"{name}@{version}", (name, version))
            for name, versions in self.tools().items()
            for version in versions
        ]

    def present(self):
        out = install_query(["mise", "ls", "--installed", "--json"], "list the mise tools")
        if out is None:
            return None
        try:
            found = json.loads(out or "{}")
            installed = {
                name: [entry["version"] for entry in entries if "version" in entry]
                for name, entries in found.items()
            }
        except (ValueError, TypeError, AttributeError) as e:
            error(f"Cannot read the mise tools: {e}")
            return None
        keys = {
            item.key for item in self.items()
            if mise_satisfied(installed.get(item.value[0], []), item.value[1])
        }
        tools = self.tools()
        extra = [f"tool '{name}'" for name in installed if name not in tools]
        return InstallState(keys, extra)

    def install(self, missing):
        tools = self.tools()
        if self.table.get("global", True):
            # 'mise use' sets a tool's whole version list, so every version of a tool goes
            # in, the first one as the default
            names = list(dict.fromkeys(item.value[0] for item in missing))
            specs = [f"{name}@{version}" for name in names for version in tools[name]]
            return install_commands([["mise", "use", "--global", *specs]])
        return install_commands([["mise", "install", *(item.label for item in missing)]])

    @staticmethod
    def draft():
        config = os.environ.get("MISE_GLOBAL_CONFIG_FILE", "~/.config/mise/config.toml")
        try:
            with open(Path(config).expanduser(), "rb") as f:
                tools = tomllib.load(f).get("tools", {})
        except (OSError, tomllib.TOMLDecodeError):
            return None
        kept = {}
        for name, value in tools.items():
            if isinstance(value, str) or (
                isinstance(value, list) and value and all(isinstance(v, str) for v in value)
            ):
                kept[name] = value
        return {"global": True, "tools": kept} if kept else None


def npm_name(spec):
    """Strip a version from an npm package spec: 'pkg@1' -> 'pkg', '@s/pkg@1' -> '@s/pkg'."""
    scoped = spec.startswith("@")
    name = spec[1:] if scoped else spec
    name = name.split("@", 1)[0]
    return "@" + name if scoped else name


class NpmGroup(NamesGroup):
    """[npm]: global npm packages. Without npm on PATH, npm runs through 'mise exec'."""

    section = "npm"
    keys = ("packages",)
    names_key = "packages"

    @staticmethod
    def command():
        if install_which("npm"):
            return ["npm"]
        if install_which("mise"):
            return ["mise", "exec", "--", "npm"]
        return None

    def unavailable(self):
        return None if self.command() else "npm not found (install node, e.g. in [mise])"

    def key(self, name):
        return npm_name(name)

    @staticmethod
    def query():
        command = NpmGroup.command()
        if not command:
            return None
        argv = command + ["ls", "--global", "--depth=0", "--json"]
        out = install_query(argv, "list the global npm packages", any_status=True)
        if out is None:
            return None
        try:
            return list(json.loads(out or "{}").get("dependencies", {}))
        except (ValueError, AttributeError) as e:
            error(f"Cannot read the global npm packages: {e}")
            return None

    def listed(self):
        names = self.query()
        return None if names is None else [n for n in names if n not in NPM_BUNDLED]

    def install(self, missing):
        return install_commands([self.command() + ["install", "--global", *(
            item.value for item in missing)]])

    @staticmethod
    def draft():
        names = NpmGroup.query()
        names = [name for name in names or [] if name not in NPM_BUNDLED]
        return {"packages": names} if names else None


class CargoGroup(NamesGroup):
    """[cargo]: crates installed with 'cargo install'."""

    section = "cargo"
    tool = "cargo"
    keys = ("crates",)
    names_key = "crates"

    @staticmethod
    def query():
        out = install_query(["cargo", "install", "--list"], "list the cargo crates")
        return None if out is None else re.findall(r"^(\S+) v\S*:", out, re.M)

    def listed(self):
        return self.query()

    def install(self, missing):
        return install_commands([["cargo", "install", *(item.value for item in missing)]])

    @staticmethod
    def draft():
        crates = CargoGroup.query() if install_which("cargo") else None
        return {"crates": crates} if crates else None


class UvGroup(NamesGroup):
    """[uv]: Python tools installed with 'uv tool install'."""

    section = "uv"
    tool = "uv"
    keys = ("tools",)
    names_key = "tools"

    @staticmethod
    def query():
        out = install_query(["uv", "tool", "list"], "list the uv tools")
        return None if out is None else re.findall(r"^([^\s-]\S*) v\S+", out, re.M)

    def listed(self):
        return self.query()

    def install(self, missing):
        return install_commands([["uv", "tool", "install", item.value] for item in missing])

    @staticmethod
    def draft():
        tools = UvGroup.query() if install_which("uv") else None
        return {"tools": tools} if tools else None


class VscodeGroup(NamesGroup):
    """[vscode]: Visual Studio Code extensions, by id."""

    section = "vscode"
    tool = "code"
    keys = ("extensions",)
    names_key = "extensions"
    case_blind = True

    @staticmethod
    def query():
        out = install_query(["code", "--list-extensions"], "list the VS Code extensions")
        return None if out is None else install_lines(out)

    def listed(self):
        return self.query()

    def install(self, missing):
        argv = ["code"]
        for item in missing:
            argv += ["--install-extension", item.value]
        return install_commands([argv])

    @staticmethod
    def draft():
        extensions = VscodeGroup.query() if install_which("code") else None
        return {"extensions": extensions} if extensions else None


def item_command(command, item):
    """Put an item, shell-quoted, in place of each {item} in a command."""
    return command.replace("{item}", shlex.quote(item))


class CustomGroup(InstallGroup):
    """One [[custom]] entry: shell commands that check for and install a thing, or each of
    its items ({item} in a command is the item, shell-quoted)."""

    section = "custom"
    keys = ("name", "install", "check", "list", "items", "requires")

    def __init__(self, table, config_name):
        if not isinstance(table, dict):
            fatal(f"{config_name}: each [[custom]] entry must be a table")
        name = table.get("name")
        if not isinstance(name, str) or not name.strip():
            fatal(f"{config_name}: a [[custom]] entry is missing its 'name'")
        self.name = name
        where = f"[[custom]] '{name}'"
        install_keys(table, self.keys, where, config_name)
        for key in ("install", "check", "list", "requires"):
            value = table.get(key)
            if value is not None and not (isinstance(value, str) and value.strip()):
                fatal(f"{config_name}: the '{key}' key in {where} must be a command")
        if not table.get("install"):
            fatal(f"{config_name}: {where} needs an 'install' command")
        if "items" in table:
            install_names(table, "items", where, config_name)
            if not table.get("check") and not table.get("list"):
                fatal(f"{config_name}: {where} has 'items', so it needs a 'check' or 'list'")
        elif table.get("list"):
            fatal(f"{config_name}: {where} has a 'list' command but no 'items'")
        elif not table.get("check"):
            fatal(f"{config_name}: {where} needs a 'check' command")
        super().__init__({}, config_name)
        self.table = table
        self.title = name

    def unavailable(self):
        requires = self.table.get("requires")
        if requires and not install_which(requires):
            return f"{requires} not found"
        return None

    def items(self):
        if "items" not in self.table:
            return [InstallItem(self.name, f"'{self.name}'", None)]
        return [InstallItem(item, f"{self.name} '{item}'", item) for item in self.table["items"]]

    def present(self):
        items = self.items()
        if self.table.get("list"):
            out = install_query(["bash", "-c", self.table["list"]], f"list '{self.name}'")
            if out is None:
                return None
            names = install_lines(out)
            wanted = {item.key for item in items}
            extra = [f"{self.name} '{name}'" for name in names if name not in wanted]
            return InstallState(names, extra)
        check = self.table["check"]
        if "items" not in self.table:
            return InstallState([self.name] if install_check(check) else [])
        return InstallState(
            item.key for item in items if install_check(item_command(check, item.value))
        )

    def install(self, missing):
        command = self.table["install"]
        if "items" not in self.table:
            return install_commands([["bash", "-c", command]])
        return install_commands(
            [["bash", "-c", item_command(command, item.value)] for item in missing]
        )


class ManualGroup(InstallGroup):
    """[[manual]]: things to install by hand (managed apps, installers without a cask).
    Install checks for them, by 'app' or 'check', and lists the missing ones with how."""

    section = "manual"
    keys = ("name", "app", "check", "source", "url", "note")

    def __init__(self, entries, config_name):
        super().__init__({}, config_name)
        names = set()
        for entry in entries:
            if not isinstance(entry, dict):
                fatal(f"{config_name}: each [[manual]] entry must be a table")
            name = entry.get("name")
            if not isinstance(name, str) or not name.strip():
                fatal(f"{config_name}: a [[manual]] entry is missing its 'name'")
            if name in names:
                fatal(f"{config_name}: two [[manual]] entries are named '{name}'")
            names.add(name)
            install_keys(entry, self.keys, f"[[manual]] '{name}'", config_name)
            for key in self.keys[1:]:
                value = entry.get(key)
                if value is not None and not (isinstance(value, str) and value.strip()):
                    fatal(f"{config_name}: the '{key}' key in [[manual]] '{name}' must be text")
            if not entry.get("app") and not entry.get("check"):
                fatal(f"{config_name}: [[manual]] '{name}' needs an 'app' or a 'check'")
        self.entries = entries

    def items(self):
        return [InstallItem(e["name"], f"'{e['name']}'", e) for e in self.entries]

    def present(self):
        installed = []
        for item in self.items():
            entry = item.value
            if entry.get("app"):
                found = app_find(entry["app"])
            else:
                found = install_check(entry["check"])
            if found:
                installed.append(item.key)
        return InstallState(installed)

    @staticmethod
    def how(entry):
        """Say how to install a manual entry: its source, url and note."""
        parts = [entry[key] for key in ("source", "url", "note") if entry.get(key)]
        return "; ".join(parts)

    @staticmethod
    def draft():
        """List the apps that came neither from a cask nor the App Store, nor with macOS."""
        apps = app_bundles()
        ids = app_store_ids(apps)
        cask_apps = set()
        if install_which("brew"):
            argv = ["brew", "info", "--cask", "--installed", "--json=v2"]
            out = install_query(argv, "list the apps of the Homebrew casks")
            try:
                for cask in json.loads(out or "{}").get("casks", []):
                    for artifact in cask.get("artifacts", []):
                        # An app, or a 'suite' folder of apps (kicad)
                        for kind in ("app", "suite"):
                            if isinstance(artifact, dict) and kind in artifact:
                                cask_apps |= {Path(str(a)).name for a in artifact[kind]}
            except (ValueError, AttributeError, TypeError) as e:
                error(f"Cannot read the apps of the Homebrew casks: {e}")
        entries = []
        names = set()
        for app in apps:
            from_cask = app.name in cask_apps or app.parent.name in cask_apps
            if app in ids or from_cask or app_bundle_id(app).startswith("com.apple."):
                continue
            # /Applications/X.app is 'X.app'; anything else keeps its folder
            main_dir = Path(INSTALL_APP_DIRS[0]).expanduser()
            if app.is_relative_to(main_dir):
                rel = str(app.relative_to(main_dir))
            elif app.is_relative_to(Path.home()):
                rel = "~/" + str(app.relative_to(Path.home()))
            else:
                rel = str(app)
            # Names are unique: the same app in ~/Applications is named by its folder
            name = app.stem if app.stem not in names else f"{app.stem} ({app.parent.name})"
            names.add(name)
            entries.append({"name": name, "app": rel})
        return entries or None


# The group class of each built-in section, in INSTALL_SECTIONS order
INSTALL_GROUPS = {
    "brew": BrewGroup,
    "mas": MasGroup,
    "custom": CustomGroup,
    "mise": MiseGroup,
    "npm": NpmGroup,
    "cargo": CargoGroup,
    "uv": UvGroup,
    "vscode": VscodeGroup,
    "manual": ManualGroup,
}


def install_order(config, config_name):
    """Return the section order: [install] order, then the sections it leaves out."""
    table = config.get("install", {})
    if not isinstance(table, dict):
        fatal(f"{config_name}: [install] must be a table")
    install_keys(table, ("order",), "[install]", config_name)
    order = install_names(table, "order", "[install]", config_name)
    unknown = [name for name in order if name not in INSTALL_SECTIONS]
    if unknown:
        fatal(
            f"{config_name}: unknown section '{unknown[0]}' in [install] order",
            f"Sections: {', '.join(INSTALL_SECTIONS)}",
        )
    if len(set(order)) != len(order):
        fatal(f"{config_name}: [install] order names a section twice")
    return order + [name for name in INSTALL_SECTIONS if name not in order]


def install_groups(config, config_name, only=None, skip=None):
    """Build and verify every group of an install config, in install order.

    A built-in section the config leaves out still gets an empty group, so a status can
    show what that tool has installed. only and skip select sections by name.
    """
    unknown = [key for key in config if key != "install" and key not in INSTALL_SECTIONS]
    if unknown:
        fatal(
            f"{config_name}: unknown table [{unknown[0]}]",
            f"Tables: install, {', '.join(INSTALL_SECTIONS)}",
        )

    groups = []
    for name in install_order(config, config_name):
        value = config.get(name)
        if name in ("custom", "manual"):
            if value is None:
                continue
            if not isinstance(value, list):
                fatal(f"{config_name}: {name} must be an array of tables: [[{name}]]")
            if name == "custom":
                titles = [entry.get("name") for entry in value if isinstance(entry, dict)]
                twice = [title for title in titles if titles.count(title) > 1]
                if twice:
                    fatal(f"{config_name}: two [[custom]] entries are named '{twice[0]}'")
                built = [CustomGroup(entry, config_name) for entry in value]
            else:
                built = [ManualGroup(value, config_name)]
        else:
            if value is None:
                value = {}
            if not isinstance(value, dict):
                fatal(f"{config_name}: [{name}] must be a table")
            built = [INSTALL_GROUPS[name](value, config_name)]
            built[0].items()  # verifies its values
        if (only and name not in only) or (skip and name in skip):
            continue
        groups += built
    return groups


def install_section_list(value):
    """Parse a comma-separated list of install sections, for argparse."""
    names = [name.strip() for name in value.split(",") if name.strip()]
    unknown = [name for name in names if name not in INSTALL_SECTIONS]
    if not names or unknown:
        shown_name = unknown[0] if unknown else value
        raise argparse.ArgumentTypeError(
            f"unknown section '{shown_name}' (sections: {', '.join(INSTALL_SECTIONS)})"
        )
    return names


def install_status(groups, config_name):
    """Show, per group, what is missing and what is installed but not in the config."""
    emit(f"Install status for {root_shown(config_name)}")
    missing_count = 0
    extra_count = 0
    skipped = 0
    for group in groups:
        items = group.items()
        reason = group.unavailable()
        if reason:
            if items:
                step_skipped(group.title, f"{reason}; {plural(len(items), 'item')} not checked")
                skipped += len(items)
            continue
        # A section the config leaves out shows only when its tool installed something
        if items:
            step(f"Checking {group.title}")
        state = group.present()
        if state is None:
            continue
        extra = state.extra
        if not items:
            if not extra:
                continue
            step(f"Checking {group.title} (not in the config)")
        missing = [item for item in items if item.key not in state.installed]
        if items:
            say(f"installed: {len(items) - len(missing)} of {len(items)}")
        for item in missing:
            how = ManualGroup.how(item.value) if group.section == "manual" else ""
            say(f"missing: {item.label}" + (f" ({how})" if how else ""))
        for label in extra:
            say(f"not in config: {label}")
        missing_count += len(missing)
        extra_count += len(extra)

    if not missing_count and not extra_count and not skipped:
        finish("Everything in the config is installed, and nothing else")
        return
    parts = [f"{plural(missing_count, 'item')} missing", f"{extra_count} not in the config"]
    if skipped:
        parts.append(f"{skipped} not checked")
    finish(f"Status: {', '.join(parts)}", False)


def install_all(groups, config_name):
    """Install what each group is missing, in order, then list what is left to do by hand."""
    banner("install")
    emit(f"Config: {root_shown(config_name)}", 1)

    installed = 0
    skipped = 0
    by_hand = []
    for group in groups:
        items = group.items()
        if not items:
            continue
        reason = group.unavailable()
        if reason:
            step_skipped(group.title, f"{reason}; {plural(len(items), 'item')} not installed")
            skipped += len(items)
            continue
        state = group.present()
        if state is None:
            continue
        missing = [item for item in items if item.key not in state.installed]
        if group.section == "manual":
            by_hand += missing
            continue
        if not missing:
            step_skipped(group.title, f"{plural(len(items), 'item')} already installed")
            continue

        step(f"Installing {group.title}")
        for item in missing:
            say(f"missing: {item.label}")
        failures = group.install(missing)
        if __dry_run__:
            continue

        # The tool's exit status isn't the whole story (brew bundle exits 1 for one bad
        # cask), so what is installed is looked up again
        after = group.present()
        done = [] if after is None else [item for item in missing if item.key in after.installed]
        still = [item for item in missing if item not in done]
        for item in done:
            ok(f"{item.label} installed")
        installed += len(done)
        if still:
            error(
                f"Cannot install {plural(len(still), 'item')} from {group.title}",
                *(item.label for item in still),
                *failures,
            )
        elif failures:
            warn(f"Some {group.title} commands failed, but everything is installed", *failures)

    if by_hand:
        step("Left to install by hand")
        for item in by_hand:
            how = ManualGroup.how(item.value)
            say(item.label + (f": {how}" if how else ""))

    parts = [f"{plural(installed, 'item')} installed"]
    if by_hand:
        parts.append(f"{len(by_hand)} left to install by hand")
    if skipped:
        parts.append(f"{skipped} skipped")
    if not installed and not by_hand and not skipped and not _reported["errors"]:
        parts = ["nothing was missing"]
    finish(f"Install completed: {', '.join(parts)}", success=not by_hand and not skipped)


def brewfile_text(groups):
    """Return a Brewfile of the [brew] and [mas] groups' items."""
    lines = []
    for group in groups:
        if group.section == "brew":
            lines += [item.value for item in group.items()]
        elif group.section == "mas":
            apps = [item.value for item in group.items()]
            if apps and 'brew "mas"' not in lines:
                lines.append('brew "mas"')
            lines += [f"mas {ruby_string(name)}, id: {app_id}" for name, app_id in apps]
    return "\n".join(lines) + "\n" if lines else ""


def draft_value_lines(key, value):
    """Format one key of a draft: short arrays on one line, longer ones one item a line."""
    if isinstance(value, list) and (len(value) > 3 or len(toml_value(value)) > 60):
        return [f"{toml_key(key)} = ["] + [f"    {toml_value(v)}," for v in value] + ["]"]
    return [f"{toml_key(key)} = {toml_value(value)}"]


def draft_table_lines(path, table):
    """Format a draft table: its values under [path], then its subtables."""
    lines = []
    values = {k: v for k, v in table.items() if not isinstance(v, dict)}
    if values:
        lines += ["", f"[{path}]"]
        for key, value in values.items():
            lines += draft_value_lines(key, value)
    for key, value in table.items():
        if isinstance(value, dict) and value:
            lines += draft_table_lines(f"{path}.{toml_key(key)}", value)
    return lines


def install_draft_text(only=None, skip=None):
    """Return (a draft install.toml of what is installed here, the number of items in it)."""
    lines = [
        f"# Draft of what is installed on {platform.node() or 'this machine'}, written by",
        f"# 'molt.py install --draft' on {datetime.now().strftime('%Y-%m-%d')}.",
        "# Review it before installing from it: molt.py install -t <this file> --dry-run",
        "# [[manual]] lists the apps that came from neither a cask nor the App Store: install",
        "# them by hand, or move them to [brew] casks. See configs/example-install.toml for",
        "# [[custom]] commands (installers, rustup targets) and [install] order.",
    ]
    count = 0
    for name in INSTALL_SECTIONS:
        if name == "custom" or (only and name not in only) or (skip and name in skip):
            continue
        table = INSTALL_GROUPS[name].draft()
        if not table:
            continue
        if name == "manual":
            for entry in table:
                lines += ["", "[[manual]]"]
                lines += [f"{key} = {toml_value(value)}" for key, value in entry.items()]
            count += len(table)
            continue
        lines += draft_table_lines(name, table)
        for value in table.values():
            count += len(value) if isinstance(value, (list, dict)) else 0
    return "\n".join(lines) + "\n", count


def install_output(path, text, what, force):
    """Write text to path ('-' is stdout); an existing file is replaced only with force."""
    if path == "-":
        sys.stdout.write(text)
        return False
    target = Path(path).expanduser()
    if (target.exists() or target.is_symlink()) and not force:
        fatal(f"Cannot write {what} to {target}: it already exists", "Pass --force to replace it")
    if __dry_run__:
        say(f"{what}: {target}")
        return True
    try:
        target.write_text(text, encoding="utf-8")
    except OSError as e:
        fatal(f"Cannot write {what} to {target}: {e.strerror or e}")
    return True


def install_draft_write(path):
    """Write a draft install.toml into a snapshot, for 'draft = true' in capture.toml."""
    step(f"Writing {DRAFT_INSTALL_TOML}")
    if __dry_run__:
        say(f"draft of what is installed: {shown(path)}")
        return
    text, count = install_draft_text()
    try:
        path.write_text(text, encoding="utf-8")
    except OSError as e:
        fatal(f"Cannot write {path}: {e.strerror or e}")
    ok(f"{DRAFT_INSTALL_TOML} written ({plural(count, 'item')})")


# --- Command Execution --- #


def require_remote(args, attr, host):
    """Exit if --from/--to was given but does not name a remote host."""
    value = getattr(args, attr, None)
    if value is not None and not host:
        flag = "--from" if attr == "src" else "--to"
        fatal(
            f"{flag} '{value}' is not a remote host",
            "Use [user@]host or [user@]host:path (a trailing colon marks a host)",
        )


def cmd_check(args):
    """Calculate and display checksums for files."""
    files = getattr(args, "files", []) or []

    if not files:
        fatal("No files given", "Usage: molt.py check [options] file [file ...]")

    ignore_invalid = getattr(args, "ignore_invalid", False)
    short_hash = getattr(args, "short_hash", False)
    full_path = getattr(args, "full_path", False)
    no_path = getattr(args, "no_path", False)

    # Calculate and display checksums; the stdout lines are data, problems go to stderr
    for file_path in files:
        path = Path(file_path)
        missing = not (path.exists() or path.is_file())
        if missing:
            if ignore_invalid:
                continue
            checksum = "null"
        else:
            try:
                checksum = calculate_file_checksum(path, show=False)
            except OSError as e:
                fatal(f"Cannot read {file_path}: {e.strerror or e}")
        if short_hash:
            checksum = checksum[:7]
        if no_path:
            print(checksum)
        elif full_path:
            print(f"{path}: {checksum}")
        else:
            print(f"{path.name}: {checksum}")
        if missing:
            warn(f"{file_path} not found")


def cmd_capture(args):
    """Execute capture command (local or remote source, local or remote destination)."""
    root_dir = args.root
    root_host = getattr(args, "root_host", None)

    from_host, _ = parse_remote_arg(getattr(args, "src", None), flag="--from")
    to_host, to_path = parse_remote_arg(getattr(args, "dst", None), flag="--to")
    require_remote(args, "src", from_host)

    # A remote molt root keeps its captures on its host (root_dir is only a local copy
    # of its configs and scripts)
    if root_host and not to_host and not to_path:
        to_host = root_host

    # Determine intended destination path; a host's paths are relative to its login
    # directory unless absolute
    now = datetime.now()
    year = now.strftime("%Y")
    month_day = now.strftime("%m-%d")
    if to_host and to_path:
        intended_path = normalize_remote_path(to_path)
    elif to_host:
        intended_path = remote_molt_root(args, to_host) / "captures" / year / month_day
    elif to_path:
        intended_path = to_path.resolve()
    else:
        intended_path = root_dir / "captures" / year / month_day

    banner("capture")
    if to_host:
        context(from_host or here(), f"{to_host}:{intended_path}")
    else:
        context(from_host or here(), intended_path)

    # The staging dir is removed however the capture ends
    tmpdir = None
    try:
        if from_host:
            # Run capture on remote host, pull results back
            dst_dir, tmpdir = remote_capture(args, root_dir, from_host)
        else:
            # Local capture, from -t or the default capture config
            config_name = getattr(args, "config_toml", None) or DEFAULT_CONFIG_CAPTURE
            config = load_config(root_dir, config_name)
            verify_capture_config(config, config_name)

            if __dry_run__:
                outdir = Path(DRY_RUN_DIR) / intended_path.name
            else:
                tmpdir = Path(tempfile.mkdtemp(prefix=f"{__script__.stem}-"))
                outdir = tmpdir / intended_path.name
                mkdir_private(outdir)

            dst_dir = capture(args, root_dir, outdir, config, config_name=config_name)

        if __dry_run__:
            final_dst = intended_path / dst_dir.name
        elif to_host:
            # Copy snapshot to remote destination
            final_dst = intended_path / dst_dir.name
            capture_deploy(dst_dir, to_host, final_dst)
        else:
            # Move snapshot to local destination; the dirs created for it are private
            # (0700), and existing ones are left as they are
            final_dst = intended_path / dst_dir.name
            try:
                mkdir_private(final_dst.parent)
            except FileExistsError:
                fatal(f"Cannot create {final_dst.parent}: a file is already there")
            except OSError as e:
                fatal(f"Cannot create {final_dst.parent}: {e.strerror or e}")

            if final_dst.exists():
                fatal(f"Cannot save the snapshot: {final_dst} already exists")

            # Across filesystems the move is a copy; umask 077 keeps it private while
            # it is written (copystat then sets the exact modes)
            previous_umask = os.umask(0o077)
            try:
                shutil.move(str(dst_dir), str(final_dst))
            finally:
                os.umask(previous_umask)
    finally:
        if tmpdir and tmpdir.exists():
            shutil.rmtree(tmpdir)

    if to_host:
        if __dry_run__:
            note(f"A real run would copy the snapshot to {to_host}:{intended_path}/<checksum>")
        finish(f"Snapshot saved to {to_host}:{final_dst}")
    else:
        finish(f"Snapshot saved to {final_dst}")


def cmd_list(args):
    """List the snapshots in the molt root's captures directory, and their backup dirs."""
    captures_dir = args.root / "captures"
    snapshots = snapshots_find(captures_dir)
    if not snapshots:
        emit(f"No snapshots found in {captures_dir}")
        return

    emit(f"Snapshots in {captures_dir}, newest first:")
    rows = []
    for snapshot in snapshots:
        row = [snapshot.date.isoformat(), snapshot.path.name]
        if snapshot.config is None:
            row += ["", size_shown(snapshot.path), f"{DEFAULT_SNAPSHOT_TOML} unreadable"]
        else:
            tables = snapshot_tables(snapshot)
            row += [plural(len(tables), "archive"), size_shown(snapshot.path)]
            encrypted = [table for table in tables.values() if table.get("encrypted") is True]
            if encrypted:
                row.append(f"{len(encrypted)} encrypted")
        rows.append(row)
    for line in columns(rows):
        say(line)

    backups = backup_dirs_find(snapshots)
    count = sum(bool(root.current) + len(root.rotated) for root in backups)
    emit()
    if not backups:
        emit("No backup dirs found for the roots in these snapshots")
    else:
        emit("Backup dirs, newest first per root:")
        rows = []
        for root in backups:
            if root.current:
                rows.append([str(root.current), size_shown(root.current), "current"])
            for path, stamp in root.rotated:
                rows.append([str(path), size_shown(path), stamp.strftime("%Y-%m-%d %H:%M:%S")])
        for line in columns(rows):
            say(line)

    finish(f"{plural(len(snapshots), 'snapshot')}, {plural(count, 'backup dir')}", False)


def cmd_prune(args):
    """Delete old snapshots and, with --backups, old rotated backup dirs, after asking."""
    keep = args.keep
    older_than = args.older_than
    if keep is None and older_than is None:
        fatal(
            "Nothing to prune by: pass --keep N, --older-than DAYS, or both",
            "Usage: molt.py prune [--keep N] [--older-than DAYS] [--backups] [--yes]",
        )

    banner("prune")
    cutoff = None
    if older_than is not None:
        cutoff = (datetime.now() - timedelta(days=older_than)).date()

    captures_dir = args.root / "captures"
    snapshots = snapshots_find(captures_dir)
    if snapshots:
        doomed = prune_snapshots(snapshots, keep, cutoff)
    else:
        emit()
        emit(f"No snapshots found in {captures_dir}")
        doomed = []

    # Backup dirs are found through the roots the snapshots name
    backups = backup_dirs_find(snapshots)
    doomed_backups = []
    if args.backups:
        doomed_backups = prune_backups(backups, keep, cutoff)
    elif any(root.rotated for root in backups):
        step_skipped("rotated backup dirs", "pass --backups to prune them")

    if not doomed and not doomed_backups:
        finish("Prune completed: nothing to delete", False)
        return

    what = prune_counts(len(doomed), len(doomed_backups), args.backups)
    if not args.yes and not ask(f"Delete {what}?", 1):
        finish("Prune completed: nothing deleted", False)
        return
    if __dry_run__:
        finish("")
        return

    step(f"Deleting {what}")
    deleted = prune_delete(captures_dir, doomed, doomed_backups)
    summary = prune_counts(*deleted, args.backups)
    finish(f"Prune completed: {summary} deleted", success=any(deleted))


def cmd_migrate(args):
    """Execute migrate command: capture on source, restore on destination."""
    root_dir = args.root

    from_host, _ = parse_remote_arg(getattr(args, "src", None), flag="--from")
    to_host, _ = parse_remote_arg(getattr(args, "dst", None), flag="--to")
    require_remote(args, "src", from_host)
    require_remote(args, "dst", to_host)

    if not from_host and not to_host:
        fatal(
            "At least one of --from or --to must be a remote host",
            "Use [user@]host or [user@]host:path",
        )

    banner("migration")
    context(from_host or here(), to_host or here())

    config_name = args.config_toml if args.config_toml else DEFAULT_CONFIG_MIGRATE
    config = load_config(root_dir, config_name)
    capture_config, restore_config = split_migrate_config(config, config_name)
    verify_capture_config(capture_config, config_name, table="capture.tar")

    # The staging dir is removed however the migration ends
    tmpdir = None
    try:
        # Phase 1: Capture on source
        if from_host:
            capture_dir, tmpdir = remote_capture(args, root_dir, from_host, capture_config)
        else:
            dir_name = datetime.now().strftime("%m-%d")
            if __dry_run__:
                outdir = Path(DRY_RUN_DIR) / dir_name
            else:
                tmpdir = Path(tempfile.mkdtemp(prefix=f"{__script__.stem}-"))
                outdir = tmpdir / dir_name
                mkdir_private(outdir)
            capture_dir = capture(
                args, root_dir, outdir, capture_config, config_name=config_name, phase="capture"
            )

        # Phase 2: Restore on destination
        args.capture_dir = capture_dir

        if to_host:
            # With --from too, the snapshot goes through this machine: the local copy
            # remote_capture made is copied on to the destination
            remote_deploy(args, root_dir, to_host, capture_dir, deploy_config=restore_config)
            finish(f"Migration to {to_host} finished; see the restore summary above", False)
        else:
            summary, success = restore(
                args,
                root_dir,
                capture_dir=capture_dir,
                restore_config=restore_config,
                table="restore.tar",
                phase="restore",
            )
            finish(f"Migration completed: {summary}", success)
    finally:
        if tmpdir and tmpdir.exists():
            shutil.rmtree(tmpdir)


def cmd_install(args):
    """Install what install.toml lists, or show its status, write its Brewfile or a draft."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        fatal(
            "Cannot run molt.py install as root: Homebrew refuses to run as root",
            "Run it as your user; installers ask for sudo when they need it",
        )
    only, skip = args.only, args.skip

    if args.draft:
        text, count = install_draft_text(only, skip)
        if install_output(args.draft, text, "the draft", args.force):
            finish(f"Draft of {plural(count, 'item')} written to {Path(args.draft).expanduser()}")
        return

    config_name = args.config_toml or DEFAULT_CONFIG_INSTALL
    config = load_config(args.root, config_name)
    groups = install_groups(config, config_name, only, skip)
    shown_config = args.root / config_name

    if args.brewfile:
        text = brewfile_text(groups)
        if not text:
            fatal(f"{config_name} lists nothing for a Brewfile (no [brew] or [mas] items)")
        if install_output(args.brewfile, text, "the Brewfile", args.force):
            finish(f"Brewfile written to {Path(args.brewfile).expanduser()}")
        return

    if args.status:
        install_status(groups, shown_config)
    else:
        install_all(groups, shown_config)
        if _reported["errors"] and not __dry_run__:
            sys.exit(1)


def cmd_restore(args):
    """Execute restore command (local or remote)."""
    root_dir = args.root
    root_host = getattr(args, "root_host", None)

    from_host, from_path = parse_remote_arg(getattr(args, "src", None), flag="--from")
    to_host, _ = parse_remote_arg(getattr(args, "dst", None), flag="--to")
    require_remote(args, "dst", to_host)

    # A remote molt root restores the latest snapshot in its own captures directory,
    # unless --from names another snapshot
    if root_host and not from_host and not from_path:
        from_host = root_host

    banner("restore")
    dest = to_host or here()

    # Determine snapshot source path; latest is set when molt.py chose the snapshot
    latest = False
    if from_host:
        # Remote source: the given path (relative to the login directory unless
        # absolute), or the latest snapshot in the host's captures directory
        if from_path:
            capture_path = normalize_remote_path(from_path)
            context(f"{from_host}:{capture_path}", dest)
        else:
            captures_dir = remote_molt_root(args, from_host) / "captures"
            context(f"{from_host}:{captures_dir} (latest)", dest)
            capture_path = resolve_remote_snapshot(from_host, captures_dir)
    elif from_path:
        capture_path = from_path.resolve()
    else:
        # Local default: find latest capture
        captures_dir = root_dir / "captures"
        capture_path = resolve_snapshot_root(captures_dir)
        latest = True

    # For local paths, auto-select latest checksum subdir if path is a date dir
    if not from_host and capture_path.exists() and capture_path.is_dir():
        toml_check = capture_path / DEFAULT_SNAPSHOT_TOML
        if not toml_check.exists():
            subdirs = [d for d in capture_path.iterdir() if d.is_dir()]
            if not subdirs:
                # A --from date dir needs a snapshot inside; otherwise there is none yet
                if getattr(args, "src", None):
                    hint = (
                        "Pass --from a snapshot directory (captures/YYYY/MM-DD/<checksum>) "
                        "or a date directory that holds one"
                    )
                else:
                    hint = "Run 'molt.py capture' first, or pass --from <snapshot>"
                fatal(f"No snapshots found in {capture_path}", hint)

            capture_path = max(subdirs, key=lambda d: d.stat().st_mtime)
            latest = True

    if not from_host:
        if latest:
            context(f"{capture_path} (latest)", dest)
        else:
            context(capture_path, dest)

    # Store capture path in args for subfunctions
    args.capture_dir = capture_path

    # Case 1: Remote to remote, through a local copy
    if from_host and to_host:
        remote_relay(args, root_dir, from_host, to_host)
        finish(f"Restore on {to_host} finished; see its summary above", False)
        return
    # Case 2: Local to remote (deploy)
    if to_host:
        remote_deploy(args, root_dir, to_host, capture_path)
        finish(f"Restore on {to_host} finished; see its summary above", False)
        return

    # Case 3: Remote to local (pull and restore)
    if from_host:
        result = remote_restore(args, root_dir, from_host)
    # Case 4: Local restore
    else:
        result = restore(args, root_dir)

    # A stubbed restore may return None
    summary, success = result or ("", True)
    if summary:
        finish(f"Restore completed: {summary}", success)
    else:
        finish("Restore completed", success)


# --- Argument Parsing --- #


def add_cli_options(parser):
    """Add common CLI options (dry-run, verbose, help)."""
    group = parser.add_argument_group("output options")
    group.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be done without changing anything",
    )
    group.add_argument(
        "--verbose", action="store_true",
        help="Show per-path details, each copied file and the commands run",
    )
    group.add_argument(
        "--help", action="help",
        help="Show this help message and exit",
    )


def add_root_option(group, local=False):
    """Add the -r/--molt-root option; local=True for commands that take only a local root."""
    if local:
        metavar = "root"
        where = ""
    else:
        metavar = "[[user@]host:]root"
        where = f"; on a host, {DEFAULT_REMOTE_ROOT} in its login directory"
    group.add_argument(
        "-r", "--molt-root", dest="root", metavar=metavar,
        default=DEFAULT_ROOT,
        help=(
            "Molt root with configs, scripts and captures "
            f"(default: ./.molt, then {DEFAULT_ROOT}{where})"
        ),
    )


def non_negative(value):
    """Parse a whole number of zero or more, for argparse."""
    try:
        number = int(value)
    except ValueError:
        number = -1
    if number < 0:
        raise argparse.ArgumentTypeError(f"'{value}' is not a whole number of 0 or more")
    return number


def add_molt_options(parser):
    """Add shared molt configuration options."""
    group = parser.add_argument_group("molt options")
    add_root_option(group)
    group.add_argument(
        "-t", "--config-toml", dest="config_toml", metavar="config",
        help="Config file, relative to the molt root, instead of the command default",
    )
    group.add_argument(
        "--run-scripts", action="store_true",
        help="Run the config's before/after scripts (on a host, molt.py there runs them)",
    )


def add_host_options(parser, from_help, to_help):
    """Add --from and --to host options."""
    group = parser.add_argument_group(
        "host options",
        "A local path that contains '@' needs a '/' (for example ./name@tag),\n"
        "or it is read as user@host",
    )
    group.add_argument(
        "--from", dest="src", metavar="[user@]host[:path]",
        help=from_help,
    )
    group.add_argument(
        "--to", dest="dst", metavar="[user@]host[:path]",
        help=to_help,
    )


def setup_parser():
    """Build the argument parser with all subcommands."""
    formatter = argparse.RawDescriptionHelpFormatter
    parser = argparse.ArgumentParser(
        add_help=False,
        formatter_class=formatter,
        description=(
            "Capture, restore and migrate file snapshots, locally or over SSH; "
            "list and prune them; install the packages a machine needs"
        ),
    )

    subparsers = parser.add_subparsers(
        dest="command", title="commands", metavar="<command>",
    )

    # check
    check = subparsers.add_parser(
        "check", formatter_class=formatter, add_help=False,
        help="Calculate and display file checksums",
    )
    check.add_argument(
        "files", nargs="*", metavar="file",
        help="Files to checksum",
    )
    chk = check.add_argument_group("checksum options")
    chk.add_argument(
        "--ignore-invalid", action="store_true",
        help="Skip missing files instead of printing 'null'",
    )
    chk.add_argument(
        "--short-hash", action="store_true",
        help="Show only the first 7 characters of each checksum",
    )
    chk_cli = check.add_argument_group("output options")
    chk_cli.add_argument(
        "--full-path", action="store_true",
        help="Show each path as given instead of just the file name",
    )
    chk_cli.add_argument(
        "--no-path", action="store_true",
        help="Show only the checksum (overrides --full-path)",
    )
    chk_cli.add_argument(
        "--help", action="help",
        help="Show this help message and exit",
    )

    # capture
    cap = subparsers.add_parser(
        "capture", formatter_class=formatter, add_help=False,
        help="Capture a snapshot (on this machine or a host)",
    )
    add_molt_options(cap)
    add_host_options(
        cap,
        from_help="Host to capture on (default: this machine)",
        to_help=(
            "Where to save the snapshot, a local path or host:path "
            "(default: <root>/captures/YYYY/MM-DD); a <checksum> subdirectory is created"
        ),
    )
    add_cli_options(cap)

    # restore
    rst = subparsers.add_parser(
        "restore", formatter_class=formatter, add_help=False,
        help="Restore a snapshot (on this machine or a host)",
    )
    add_molt_options(rst)
    add_host_options(
        rst,
        from_help=(
            "Snapshot, date directory or host to restore from "
            "(default: latest in <root>/captures)"
        ),
        to_help="Host to restore on (default: this machine)",
    )
    rst_group = rst.add_argument_group("restore options")
    rst_group.add_argument(
        "--disable-rollback", action="store_true",
        help="Do not back up existing files; ask before restoring each archive",
    )
    add_cli_options(rst)

    # migrate
    mig = subparsers.add_parser(
        "migrate", formatter_class=formatter, add_help=False,
        help="Capture on one machine and restore on another (at least one must be a host)",
    )
    add_molt_options(mig)
    add_host_options(
        mig,
        from_help="Host to capture on (default: this machine)",
        to_help="Host to restore on (default: this machine)",
    )
    mig_group = mig.add_argument_group("migrate options")
    mig_group.add_argument(
        "--disable-rollback", action="store_true",
        help="Do not back up existing files; ask before restoring each archive",
    )
    add_cli_options(mig)

    # list
    lst = subparsers.add_parser(
        "list", formatter_class=formatter, add_help=False,
        help="List the snapshots in the molt root and the backup dirs of their roots",
    )
    add_root_option(lst.add_argument_group("molt options"), local=True)
    lst.add_argument_group("output options").add_argument(
        "--help", action="help",
        help="Show this help message and exit",
    )

    # prune
    prn = subparsers.add_parser(
        "prune", formatter_class=formatter, add_help=False,
        help="Delete old snapshots (and old backup dirs), after asking",
        description=(
            "Delete old snapshots from the molt root's captures directory. A snapshot is\n"
            "deleted only when no rule you give keeps it: --keep N keeps the newest N, and\n"
            "--older-than DAYS keeps those from the last DAYS days. With --backups, the\n"
            "same rules apply to each root's rotated backup dirs (<root><rollback>_<time>),\n"
            "by their timestamp; the current <root><rollback> is never deleted. It lists\n"
            "what it will delete and asks first, unless --yes. Dirs that need root to\n"
            "delete are kept and reported with the sudo command to run."
        ),
    )
    add_root_option(prn.add_argument_group("molt options"), local=True)
    prn_group = prn.add_argument_group("prune options")
    prn_group.add_argument(
        "--keep", type=non_negative, metavar="N",
        help="Keep the newest N snapshots (and, with --backups, N rotated dirs per root)",
    )
    prn_group.add_argument(
        "--older-than", dest="older_than", type=non_negative, metavar="DAYS",
        help="Keep only what is from the last DAYS days; older ones are deleted",
    )
    prn_group.add_argument(
        "--backups", action="store_true",
        help="Also delete rotated backup dirs of the roots in the snapshots",
    )
    prn_group.add_argument(
        "--yes", action="store_true",
        help="Delete without asking",
    )
    add_cli_options(prn)

    # install
    ins = subparsers.add_parser(
        "install", formatter_class=formatter, add_help=False,
        help="Install the packages and apps install.toml lists, or show what is missing",
        description=(
            "Install what configs/install.toml lists and this machine is missing: Homebrew\n"
            "taps, formulae and casks, App Store apps, mise runtimes, npm, cargo and uv\n"
            "tools, VS Code extensions, and [[custom]] commands, in that order unless\n"
            "[install] order sets another. Nothing installed is upgraded. [[manual]]\n"
            "entries are only checked, and listed at the end as left to install by hand.\n"
            "--status shows what is missing and what is installed but not in the config;\n"
            "--draft writes a config of what is installed here, to start one from."
        ),
    )
    ins_molt = ins.add_argument_group("molt options")
    add_root_option(ins_molt, local=True)
    ins_molt.add_argument(
        "-t", "--config-toml", dest="config_toml", metavar="config",
        help=(
            f"Install config, relative to the molt root or absolute "
            f"(default: {DEFAULT_CONFIG_INSTALL})"
        ),
    )
    ins_group = ins.add_argument_group("install options")
    ins_mode = ins_group.add_mutually_exclusive_group()
    ins_mode.add_argument(
        "--status", action="store_true",
        help="Show what is missing and what is installed but not in the config; install nothing",
    )
    ins_mode.add_argument(
        "--brewfile", metavar="file",
        help="Write the config's [brew] and [mas] items as a Brewfile ('-' for stdout)",
    )
    ins_mode.add_argument(
        "--draft", metavar="file",
        help="Write a draft config of what is installed on this machine ('-' for stdout)",
    )
    ins_group.add_argument(
        "--only", type=install_section_list, metavar="sections",
        help=f"Only these sections, comma-separated ({', '.join(INSTALL_SECTIONS)})",
    )
    ins_group.add_argument(
        "--skip", type=install_section_list, metavar="sections",
        help="Leave out these sections, comma-separated",
    )
    ins_group.add_argument(
        "--force", action="store_true",
        help="Let --brewfile and --draft replace an existing file",
    )
    add_cli_options(ins)

    # Top-level help only
    parser.add_argument(
        "--help", action="help",
        help="Show this help message and exit",
    )

    # main() reports unknown flags with the subcommand's own usage
    parser.commands = {
        "check": check, "capture": cap, "restore": rst, "migrate": mig,
        "list": lst, "prune": prn, "install": ins,
    }
    return parser


def interrupt_on_signal(signum, frame):
    """Turn SIGTERM and SIGHUP into Ctrl-C, so cleanup and rollback run as they do for it."""
    raise KeyboardInterrupt


def main():
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, interrupt_on_signal)

    parser = setup_parser()
    args, extra = parser.parse_known_args()
    if extra:
        command_parser = parser.commands.get(args.command) or parser
        command_parser.error(f"unrecognized arguments: {' '.join(extra)}")

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "check":
        cmd_check(args)
        return

    global __dry_run__, __verbose__
    __dry_run__ = getattr(args, "dry_run", False)
    __verbose__ = getattr(args, "verbose", False)

    # Parse --molt-root for remote host support
    root_host, root_path = parse_remote_arg(args.root, flag="-r/--molt-root")
    args.root_host = root_host

    # Each command prints its own banner after checking its arguments
    commands = {
        "capture": cmd_capture,
        "restore": cmd_restore,
        "migrate": cmd_migrate,
        "list": cmd_list,
        "prune": cmd_prune,
        "install": cmd_install,
    }
    if root_host and args.command in ("list", "prune", "install"):
        fatal(
            f"molt.py {args.command} works only with a local molt root, not '{args.root}'",
            f"Run molt.py {args.command} on {root_host} itself",
        )
    if not root_host:
        args.root = resolve_root(args.root)
        commands[args.command](args)
        return

    # A remote molt root: (root_host, root_path) name it on its host, and args.root is a
    # local copy of its configs/ and scripts/ (made in a dry run too), removed at exit
    args.root_path = normalize_remote_path(root_path or DEFAULT_REMOTE_ROOT)
    args.root = Path(tempfile.mkdtemp(prefix=f"{__script__.stem}-root-"))
    try:
        root_fetch(root_host, args.root_path, args.root)
        commands[args.command](args)
    finally:
        _root_names.pop(str(args.root), None)
        shutil.rmtree(args.root, ignore_errors=True)


if __name__ == "__main__":
    main()
