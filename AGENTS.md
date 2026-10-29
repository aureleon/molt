# Working on molt.py

molt.py captures sets of files into tar archives (a *snapshot*), and restores them on this
machine or on another one over SSH. `molt install` installs the packages and apps an
install.toml lists. `README.md` is the user documentation; this file is
for changing the code.

## Repository

```
molt.py                  The whole program: one file, stdlib + optional tqdm
install.sh               Installer: molt root, example configs, command link
check.sh                 ruff + shellcheck + pytest (the only CI there is)
pyproject.toml           ruff and pytest settings, the 'slow' marker
configs/example-*.toml   Installed as configs/capture|restore|migrate|install.toml;
                         example-snapshot.toml only documents the snapshot format
scripts/example-*.sh     Installed as scripts/capture-brew.sh, restore-brew.sh
tests/test_molt.py       molt.py tests (unit tests in-process, end-to-end in a subprocess)
tests/test_install.py    install.sh tests, with shims for brew, apt-get, sudo, python3
tests/conftest.py        Marks the end-to-end tests 'slow'
Brewfile, packages.txt   System packages install.sh installs (rsync)
requirements.txt         tqdm, installed by install.sh when pip allows it
```

## Data flow

The *molt root* (`-r`, default `./.molt`, then `~/.molt`) holds `configs/`, `scripts/` and
`captures/`. With `-r host[:path]`, `main()` copies the host's `configs/` and `scripts/`
into a local temp dir and uses that as the root.

**capture** (`cmd_capture` -> `capture`):
1. Load and check the config (`verify_capture_config`, `verify_age_recipients`).
2. Before scripts (`--run-scripts`), in the staging dir.
3. `create_archives`: one `<name>.tar.gz` per `[tar.<name>]` table, in parallel, in a
   private (0700) staging dir from `mkdtemp`. Paths are stored relative to `root`;
   `exclude` patterns and macOS metadata (`._*`, `.DS_Store`) are filtered out. If any
   archive fails, no snapshot is saved.
4. `encrypt_archives`: `encrypt = true` archives become `<name>.tar.gz.age` (age CLI);
   the plaintext is removed.
5. `generate_snapshot_toml`: `snapshot.toml` with `[tarball]` (compress when not gzip,
   rollback, checksum) and one `[tar.<name>]` per archive (root, link, encrypted,
   sha256 checksum of the file as stored). The snapshot id is the first 7 characters of
   the sha256 of the concatenated checksums; the snapshot dir is named after it.
6. After scripts, in the snapshot dir (so a Brewfile they write goes with it), then
   `snapshot_private` makes every file 0600 and every dir 0700.
7. Move to `captures/YYYY/MM-DD/<id>` (new dirs 0700), or rsync to `--to host:path`.

**restore** (`cmd_restore` -> `restore_snapshot`):
1. Find the snapshot: `--from` path, a date dir, a host, or the newest
   `captures/*/*/*/snapshot.toml`. A host's snapshot is copied to a local temp dir first.
2. `verify_archives_from_toml`: checksums against snapshot.toml. Only archives that
   snapshot.toml lists are ever restored.
3. Select with `[tar] archives` globs from restore.toml; decrypt selected `.age` archives
   into a private temp dir with `[age] identity`.
4. `restore_needs_sudo`: if any target needs root, `sudo_restore` re-runs molt.py as
   `sudo env <vars> <this python> -s molt.py restore ...` on the local copy (only the
   allowed env vars; see `SUDO_ENV_BLOCKED`) and exits with the child's status.
5. `extract_archives` -> `restore_category` per archive: the root's backup dir
   `<root><rollback>` is rotated once per run to `<root><rollback>_<timestamp>`; each
   captured path that exists is *renamed* into `<root><rollback>/<archive>/` (a copy
   only across filesystems), then the archive is extracted. On failure the archive's
   backups are renamed back (`restore_rollback`). Without rollback, each archive is
   confirmed with a prompt instead.
6. `create_symlinks` for the restored archives' `link` keys; after scripts.

**migrate** splits `migrate.toml` (`split_migrate_config`) into the `[capture.*]` and
`[restore.*]` halves, captures on `--from`, restores on `--to`.

**list / prune** read `captures/` and the backup dirs of the roots the snapshots name;
prune deletes only after a `[y/N]` prompt (or `--yes`), never with sudo.

**check** prints `name: checksum` lines; those stdout lines are a stable format.

**install** (`cmd_install`, local molt root only):
1. `install_groups` builds and verifies one `InstallGroup` per table of install.toml
   (`[brew]`, or each `[[brew]]`), in `install_order` (`[install] order`, then
   `INSTALL_SECTIONS`); each `[[custom]]` entry is its own group. A built-in section the
   config leaves out gets an empty group, so `--status` can show what its tool installed.
2. Per group: `wrong_os()` (its `platforms` or `os` key leave out `install_os()`, or a
   `SystemGroup`'s package manager is missing: left out, shown only with --verbose),
   `unavailable()` (its tool is missing, or Homebrew as root: skipped), `items()`,
   `present()` (read-only queries, which run in a dry run too), then `install(missing)`
   and `present()` again; what is still missing is one Error. `[[manual]]` is only
   checked. System package managers run through `as_root()` (sudo unless root).
   `--status` lists `InstallState.found` keys that no group of the same `scope` asks for.
3. `--status` (`install_status`) and `--brewfile` (`brewfile_text`) change nothing;
   `--draft` (`install_draft_text`) builds a config from each group class's `draft()`.
   capture's `[install] draft = true` writes one into the snapshot (`install_draft_write`).
4. A new package manager is an `InstallGroup` subclass (`NamesGroup` for a plain array
   of names, `SystemGroup` for a Linux one run as root), a name in `INSTALL_SECTIONS`,
   and an entry in `INSTALL_GROUPS`.

## Remote model

- Only `ssh` and `rsync` touch hosts (`ssh_run`, `rsync_run`). A host is `user@host` or
  `host:`; host paths are relative to the login dir and limited to
  `REMOTE_PATH_CHARS`, because old rsync passes them to the remote shell unescaped.
- `capture --from`, `restore --to` and `migrate` run molt.py *on the host*: a private
  `mktemp -d` work dir (`remote_workdir`) gets molt.py itself (`__script__`), the one
  config this command uses (written as capture.toml or restore.toml), the scripts only
  with `--run-scripts`, and for a restore the snapshot. The work dir is removed in a
  `finally`, and a still-running remote molt.py is stopped first (`remote_stop_command`).
- A remote restore runs over `ssh -t` so sudo can prompt; it re-runs itself with sudo
  on the host exactly like a local restore. Nothing secret is sent: an age identity
  stays a path on the host.
- Copies to a host keep modes (`rsync -a`) and host dirs are created under `umask 077`.

## Safety rules when working on it

- Never run `restore`, `migrate` or `prune` without `--dry-run` outside a temp HOME: a
  real restore renames your files into backup dirs and may re-run itself with sudo.
- Try things in `mktemp -d` dirs with `HOME=<tmp>/home`, a molt root there, and
  `-r <tmp>/root`. Never touch `~/.molt` or real dotfiles.
- Never run `molt install` without `--dry-run` or `--status` outside a temp HOME with
  fake tools: it installs for real. Its tests (the `inst` fixture) set `PATH` to the fakes
  plus links to a few plain commands, empty `INSTALL_PATH_DIRS`, and fix `install_os()`
  (`inst.on("linux", ...)`), so no real package manager is ever found. Real Linux runs
  go in throwaway containers only.
- Never use real sudo, never contact real hosts. The tests put fake `sudo`, `ssh`,
  `rsync` and `age` first on `PATH` (see the `env` and `hosts` fixtures); do the same
  for manual runs.
- install.sh installs packages for real (brew, apt-get, pip). Run it only through
  `tests/test_install.py` or with the same shims first on `PATH`: its fixture builds a
  `PATH` of fakes plus links to the few real tools it needs. `brew bundle install`
  without the shim upgrades real Homebrew packages.
- Don't commit unless asked.

## Checks

```bash
./check.sh          # ruff, shellcheck, all tests (a few minutes)
./check.sh --fast   # skips tests marked slow (seconds)
./check.sh --fast -k exclude   # extra args go to pytest
```

- ruff: `ruff check molt.py tests/` with pyproject's settings (line length 100,
  F, E, W, B). shellcheck: install.sh, check.sh, scripts/*.sh.
- Tests using the `env` fixture run molt.py in a subprocess and are marked slow by
  `tests/conftest.py`; all of test_install.py is slow.
- Every behavior change gets a test. Update only the assertions whose behavior you
  changed.

## Conventions

### Structure

- Everything is in `molt.py`: no modules, no package. Sections are marked
  `# --- Section Name --- #`; add code to the section it belongs to.
- stdlib only, plus tqdm, which stays optional: `dtqdm()` returns a no-op bar without it
  (the sudo child and remote hosts may not have it).
- Python 3.11+ (tomllib; the tarfile `tar` filter needs 3.11.4). No type hints, no
  `typing`, no async (ThreadPoolExecutor for parallel I/O).
- Module-level flags `__dry_run__` and `__verbose__` are set once in `main()`.
- Constants in `UPPER_SNAKE_CASE` at the top of their section; functions are
  `noun_verb` or `verb_noun` (`archive_create`, `run_scripts`); commands are `cmd_*`.
- Short docstrings that say what a function does and why, not how. Readable code over
  clever code; no nested ternaries; lines under 100 characters.

### Output

- All output goes through the helpers built on `emit()`: `banner()`/`context()` open a
  run; `step()`, `step_skipped()`, `note()` start a section; `say()` prints items (level
  1) and details (level 2); `ok()` prints `✓` results; `warn()`, `error()`, `fatal()` go
  to stderr; `relay()` prints captured child output; `ask()` prompts; `finish()` prints
  the command's single final line. `print()` is used only by the Python-version guard,
  `printd()` and check's data lines; `install --brewfile -` and `--draft -` write their
  file to stdout directly.
- `✓` is the only symbol, only on stdout, only for success, never in a dry run. No
  emojis.
- Wording follows the style spec the output was built from: sentence case, no trailing
  period, `Error: Cannot <verb> <object>: <cause>`, counts through `plural()`, user
  values in single quotes, the word "archive" (not "category") in output.
- `emit()` is thread-safe; workers return their lines and the main thread prints them
  after the progress bar closes. Errors can print at once.
- Verbose only adds lines (`if __verbose__: say(...)`); it never changes wording.

### Dry run

- Every function that changes anything checks `__dry_run__` and changes nothing.
- `emit()` prefixes every line with `[DRY-RUN]` (through `printd`); don't call `printd`.
- A skipped command is shown with `echo(argv)` (`run: <shell_line(argv)>`, which is
  `shlex.join` with `$'...'` for arguments holding a tab or newline), once.
- Paths a real run would create use the stand-ins (`DRY_RUN_DIR`, `DRY_RUN_CHECKSUM`,
  ...) and are displayed with `shown()` (`<staging dir>`, `<checksum>`).
- Work a dry run can't preview gets a `note()`; read-only queries of hosts still run.
- A dry run never decrypts or writes plaintext, and never waits at a prompt (it shows
  `[y/N]: y (assumed in a dry run)`).

### Errors and processes

- `fatal()` for errors that stop the run (exit 1); `error()` for ones that don't (the
  final line then drops `✓`); `warn()` for the rest. try/except only where the code can
  recover or report better.
- Subprocesses capture their output and `relay()` it afterwards, except the ones that
  need the terminal: the sudo re-run, the `ssh -t` remote restore, and install commands
  (`install_run`: installers prompt; stdin is /dev/null without a terminal).
- Timeouts: short ssh commands have `COMMAND_TIMEOUT`; long ones (remote runs, scripts)
  have none; rsync has `--timeout`.
- Temp dirs are always removed in `finally`.

### Security

- Archives store relative paths; extraction uses the `tar` filter and
  `archive_entries()` refuses absolute or `..` members.
- `shlex.quote()` for everything in a remote shell command.
- Snapshots are private: dirs 0700, files 0600 (`mkdir_private`, `snapshot_private`);
  existing dirs keep their mode.
- Backups are renames, so every attribute (mode, owner, times, xattrs, ACLs, hardlinks)
  is kept; the copy fallback is only for another filesystem.
- sudo is used only when a target needs it, with `python3 -s` and a filtered env.
  prune never escalates; it prints the `sudo rm -rf` to run.

### install.sh and scripts

- bash, `set -Eeuo pipefail`, shellcheck clean, and it must run on macOS's bash 3.2
  (no associative arrays; guard empty arrays under `set -u`).
- Same output shape as molt.py (`Starting install`, steps, `✓` items, one final line,
  `Error:`/`Warning:` on stderr). Never overwrite a user's config, script or command;
  never hang without a terminal; a package-manager failure is an error, a pip failure
  (tqdm is optional) a warning.
- Example scripts must exit 0 with a warning when their tool (brew) is missing.
