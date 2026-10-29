# Usage and options

[Project README](../README.md) · [Configuration](configuration.md)

Use this page for installation details, examples, command options, and restore behavior.

- [Installation](#installation)
- [Local and remote usage](#usage)
- [Command line options](#command-line-options)
- [Rollback protection](#rollback-protection)
- [Troubleshooting](#troubleshooting)
- [Development](#development)

## Requirements

- Python 3.11+ (as `python3` on `PATH`)
- Optional: `tqdm` for progress bars (install with `pip install tqdm`); without it molt.py
  runs the same, with no bars
- rsync (for remote operations)
- SSH access (for remote operations)
- sudo (for restore operations that write to system locations)
- Optional: the [age](https://age-encryption.org) CLI, only for archives with
  `encrypt = true` (see [Encryption](configuration.md#encryption))
- On a remote host: `python3` 3.11+ on the login `PATH` and rsync (tqdm is optional
  there too), and sudo only for restores that write where the login user can't

## Installation

### Quick Install

```bash
./install.sh            # asks where to put the command
./install.sh --yes      # asks nothing: the command goes in ~/.local/bin
```

Run it from a checkout of this repository. The installer:

1. Checks for `python3` 3.11 or later, and stops if it is missing or older.
2. Installs `tqdm` with `python3 -m pip install --user` when it is not installed yet.
   tqdm is optional: when pip fails (for example on a Python that is "externally
   managed", PEP 668), the installer prints a warning and continues. It never uses
   `--break-system-packages`; install `python3-tqdm` with your package manager instead.
3. Installs the system packages: on macOS the `Brewfile` (rsync) with
   `brew bundle install --no-upgrade`, with `HOMEBREW_NO_INSTALL_UPGRADE` and
   `HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK` set, so it installs what is missing and doesn't
   upgrade what you already have;
   on Linux the commands in `packages.txt` that are not on `PATH`, with apt-get, dnf, yum,
   pacman or zypper (with sudo when you are not root). A package manager that fails stops
   the install with an `Error:` line. On macOS without Homebrew it uses the system rsync
   and prints a warning.
4. Creates the molt root `~/.molt/` with `configs/`, `scripts/` and `captures/`. New
   directories are private (`0700`); directories that already exist keep their mode.
5. Copies `molt.py` into the molt root (a re-run updates it).
6. Installs the example configs as `configs/capture.toml`, `configs/restore.toml` and
   `configs/migrate.toml`, and the example scripts as `scripts/capture-brew.sh` and
   `scripts/restore-brew.sh` (the names the configs use). It copies each one only when
   that file does not exist, so it never overwrites your configs or scripts.
7. Links the command to `molt.py`: `~/.local/bin/molt -> ~/.molt/molt.py`. It asks
   where to put it (`~/.local/bin`, `/usr/local/bin` with sudo when that is not writable,
   or nowhere), and tells you when the directory is not on your `PATH`.

Besides these and the bin directory (when it is missing), the installer creates nothing
(older versions also linked `~/Snapshots` to
`captures/`; a re-run of those created a `captures/captures` link, which this installer
reports but leaves for you to remove).

Running it again is safe: it updates `molt.py` and the link, prints
`skip: configs/capture.toml (exists)` for each of your files, and creates no new
directories or links.

Options:

```
./install.sh [options] [install_dir]

install_dir      Molt root to install into (default: ~/.molt). It must be new, empty,
                 or a molt root already (with molt.py, configs/ or captures/)
-y, --yes        Don't ask; use the defaults (the command goes in ~/.local/bin)
--name NAME      Command name (default: molt, or molt-cli when another molt is on PATH)
--bin-dir DIR    Directory for the command, instead of asking
-h, --help       Show the help
```

The command is named `molt` unless another program named `molt` is on your `PATH` or
in the bin directory: then it is named `molt-cli`, and the installer says so. `--name` picks another name. The installer never replaces a file that
is not molt.py; an older install's copy of molt.py is replaced with the link.

Without a terminal (for example in a script, or with stdin from `/dev/null`) the
installer never waits for an answer: it uses the defaults, as with `--yes`, and says so.

### Custom Installation Directory

```bash
./install.sh /path/to/custom/location
```

molt.py looks for its molt root in `./.molt`, then `~/.molt`, so with another directory
pass `-r` to each command:

```bash
molt capture -r /path/to/custom/location
molt restore -r /path/to/custom/location --dry-run
```

or add a shell function to your `~/.zshrc` or `~/.bashrc` that adds it:

```bash
molt() {
    case "$1" in
        capture|restore|migrate|list|prune) command molt "$1" -r /path/to/custom/location "${@:2}" ;;
        *) command molt "$@" ;;
    esac
}
```

### Manual Install

```bash
pip install --user -r requirements.txt   # optional: progress bars

mkdir -p -m 700 ~/.molt ~/.molt/configs ~/.molt/scripts ~/.molt/captures
cp molt.py ~/.molt/
cp configs/example-capture.toml ~/.molt/configs/capture.toml
cp configs/example-restore.toml ~/.molt/configs/restore.toml
cp configs/example-migrate.toml ~/.molt/configs/migrate.toml
cp scripts/example-capture-brew.sh ~/.molt/scripts/capture-brew.sh
cp scripts/example-restore-brew.sh ~/.molt/scripts/restore-brew.sh
chmod +x ~/.molt/molt.py

# Optional: put it on PATH
ln -s ~/.molt/molt.py ~/.local/bin/molt
```

## Directory Structure

```
~/.molt/                             # Default root directory (the molt root)
├── molt.py                          # The script (~/.local/bin/molt links to it)
├── configs/                         # Configuration files
│   ├── capture.toml                 # Capture configuration
│   ├── restore.toml                 # Restore configuration
│   ├── migrate.toml                 # Migrate configuration
│   └── install.toml                 # What 'molt install' installs
├── scripts/                         # Optional scripts (run with --run-scripts)
│   ├── capture-brew.sh              # Saves a Brewfile in the snapshot
│   └── restore-brew.sh              # Installs the snapshot's Brewfile
└── captures/                        # Snapshot storage
    └── 2024/01-15/                  # Date-based directories (YYYY/MM-DD)
        └── a1b2c3d/                 # Checksum prefix (first 7 chars)
            ├── category.tar.gz      # Compressed archive per category
            ├── secrets.tar.gz.age   # An archive encrypted with age (encrypt = true)
            ├── install.toml         # Draft of what was installed ([install] draft = true)
            └── snapshot.toml        # Metadata with checksums
```

In this repository, `configs/example-*.toml` and `scripts/example-*.sh` are the files the
installer copies under those names; `configs/example-snapshot.toml` shows the format of
a `snapshot.toml` and is not installed.

### Root Directory Resolution

The script searches for the `.molt` directory in this order:
1. Current working directory (`.molt/`)
2. Home directory (`~/.molt/`)
3. Exit with error if neither exists

Override with `-r/--molt-root <path>`.

With `-r [user@]host[:path]` the molt root is on a host (default `.molt` in its login
directory). molt.py copies its `configs/` and `scripts/` into a local temp directory at
startup (also in a dry run, so the dry run reads the real config) and removes the copy
at exit. Everything else runs as usual, with that host's configs and scripts:

- `capture` without `--to` saves to `host:<path>/captures/YYYY/MM-DD`.
- `restore` without `--from` restores the latest snapshot from `host:<path>/captures`
  (on this machine, or on the `--to` host).
- `--from`/`--to` still name where to capture and restore; the `-r` host is only where
  the configs, scripts and captures live. molt.py only reads its molt root, except that
  `capture` saves its snapshot in `captures/` there.

## Usage

### Create Local Snapshot

```bash
molt capture
```

Creates snapshot at `~/.molt/captures/YYYY/MM-DD/CHECKSUM/`.

### Create Snapshot and Send to Remote

```bash
molt capture --to user@remote.host          # .molt/captures/YYYY/MM-DD on the host
molt capture --to user@remote.host:~/snaps  # snaps/<checksum> in the login directory
```

The whole snapshot directory is copied, so files that after-capture scripts write into it
(for example a `Brewfile`) go with it.

### Capture on a Remote Machine

```bash
molt capture --from user@source.host                         # saved here
molt capture --from user@source.host --to user@backup.host   # saved on another host
```

molt.py runs the capture on the source host (see [Remote Hosts](#remote-hosts)) and copies
the snapshot back, named after its snapshot id. With `--to` another host, the snapshot is
copied through this machine.

### Restore Latest Snapshot

```bash
# Auto-selects most recent snapshot
molt restore
```

### Restore from Specific Path

```bash
molt restore --from ~/.molt/captures/2024/01-15
```

### Pull Snapshot from Remote and Restore

```bash
molt restore --from user@remote.host
```

Restores the host's latest snapshot: the one in `.molt/captures/YYYY/MM-DD/<checksum>`
whose `snapshot.toml` is newest. `--from user@remote.host:<path>` names a snapshot
directory instead.

### Deploy Snapshot to Remote Machine

```bash
molt restore --from ~/.molt/captures/2024/01-15 --to user@target.host
```

molt.py copies the snapshot to the target host and restores it there with molt.py (see
[Remote Hosts](#remote-hosts)).

### Deploy from One Remote to Another

```bash
molt restore --from user@source.host --to user@target.host
```

rsync can't copy from one host to another, so the snapshot is copied from the source host
into a local temp directory, then to the target host. The local copy is removed when the
restore ends, also when it fails.

### Migrate Local to Remote

```bash
molt migrate --to user@target.host
```

Captures locally, then restores on the target host.

### Migrate Remote to Local

```bash
molt migrate --from user@source.host
```

Captures on the source host, pulls the result, then restores locally.

### Migrate Remote to Remote

```bash
molt migrate --from user@source.host --to user@target.host
```

The snapshot goes through this machine, as with `restore --from A --to B`.

In a migration, `[capture.*]` goes to the capture host and `[restore.*]` to the restore
host, each written as that host's config; `[restore.tar] archives` selects the archives
there as it does locally.

### Run Scripts with Capture/Restore

```bash
molt capture --run-scripts
molt restore --run-scripts

# With custom config
molt capture --run-scripts -t production.toml
```

`-t` replaces the command's default config (`configs/capture.toml`, `configs/restore.toml`
or `configs/migrate.toml`) for everything the command reads from it: capture takes its
`[tar.*]` tables, `[tarball]` options and scripts from the `-t` file.

### Calculate File Checksums

```bash
molt check file1.tar.gz file2.tar.gz
molt check --short-hash file.tar.gz
molt check --no-path file.tar.gz
```

### List Snapshots and Backup Directories

```bash
molt list
```

```
Snapshots in /Users/u/.molt/captures, newest first:
  2026-09-22  fad09cc  4 archives  12.3 MB  1 encrypted
  2026-09-01  d5b6730  3 archives  11.0 MB

Backup dirs, newest first per root:
  /Users/u.bak                  2.1 MB  current
  /Users/u.bak_20260922_230644  2.0 MB  2026-09-22 23:06:44

2 snapshots, 2 backup dirs
```

`list` shows every snapshot in the molt root's `captures/` (its date, snapshot id, number
of archives, size and how many of its archives are encrypted), then the backup directories
of the roots those snapshots name: the current `<root><rollback>` and the rotated
`<root><rollback>_<timestamp>` ones. It only reads.

### Prune Old Snapshots

```bash
molt prune --keep 5                      # keep the newest 5 snapshots
molt prune --older-than 30               # delete snapshots from before the last 30 days
molt prune --keep 5 --older-than 30      # delete only what both rules let go
molt prune --keep 3 --backups            # also prune rotated backup dirs
molt prune --keep 5 --dry-run            # show the plan only
```

`prune` never deletes anything without asking: it lists what it will delete, then asks
`Delete 2 snapshots and 1 backup dir? [y/N]` (`--yes` skips the question). A snapshot is
deleted only when no rule you gave keeps it: `--keep N` keeps the newest N, and
`--older-than DAYS` keeps the snapshots dated in the last DAYS days. So `--keep 5
--older-than 30` deletes snapshots older than 30 days, but always keeps the newest 5. Date
directories left empty are removed; `captures/` itself and anything that is not a
snapshot (a directory without `snapshot.toml`) are never touched.

With `--backups`, the same rules apply to each root's rotated backup directories, by the
timestamp in their name (`--keep 3` keeps each root's 3 newest). The current
`<root><rollback>` is never deleted. A directory prune can't delete without root (for
example a backup of `/etc`) is kept and reported with the command to run yourself:

```
Warning: /etc.bak_20250101_120000 needs root to delete; it is kept
  Run: sudo rm -rf /etc.bak_20250101_120000
```

`list` and `prune` work on a local molt root only; for a molt root on a host, run them on
that host.

### Install Packages and Apps

```bash
molt install --draft draft.toml          # write a config of what this machine has
molt install --status                    # what is missing, and what isn't in the config
molt install --dry-run                   # show what a real run would install
molt install                             # install what is missing
molt install --only apt,brew             # only some sections
molt install --brewfile Brewfile         # the [brew] and [mas] items as a Brewfile
molt install -t /path/to/snapshot/install.toml --dry-run
```

`install` reads `configs/install.toml` (see [install.toml](configuration.md#installtoml))
and installs what this machine is missing, section by section: system packages (apt,
dnf, pacman, zypper, or apk), Homebrew taps, formulae and casks, App Store apps, Flatpak
apps and snaps, `[[custom]]` commands, mise runtimes, npm, cargo and uv tools, and VS
Code extensions. It never upgrades what is already installed. After each section it
checks again, and reports any item that still isn't installed as an error (the run then
exits 1). A section whose tool is missing (`cargo not found`) is skipped, so a later run
picks it up once an earlier section, or you, installed the tool. `[[manual]]` entries are
only checked; the ones missing are listed at the end, with where to get them.

One config can cover macOS and Linux: tables meant for another system are left out
quietly (`--verbose` lists them). That is a table whose `os` key doesn't name this one,
a system package manager that isn't on this machine (`[dnf]` on Ubuntu), and the macOS
only parts on Linux (App Store apps, casks, and `[[manual]]` apps in `/Applications`).

The system package managers run as root: directly when molt runs as root (in a
container, say), else through sudo. Homebrew refuses to run as root, so as root its
sections are skipped. Flatpak apps install for your user by default, without root.

Installers can prompt (a sudo password, an App Store sign-in, a `curl | sh` installer),
so install commands run on the terminal, and their output is not indented. Without a
terminal, their stdin is `/dev/null`, so a prompt fails instead of waiting (sudo then
needs NOPASSWD). Commands are found on `PATH`, then in `/opt/homebrew/bin`,
`/usr/local/bin`, `/home/linuxbrew/.linuxbrew/bin`, `~/.cargo/bin`, `~/.local/bin`,
`/snap/bin`, and the VS Code app, so a tool one section installs is found by the next one
before a new shell has added it to `PATH`.

`--status` changes nothing. Per section, it shows how many items are installed, each
missing one, and what that tool has installed that the config doesn't list: formulae
installed on request, casks, global npm packages, and for system packages the ones that
were asked for and that nothing else depends on (with apt and dnf5; dnf4 can't tell, so
it lists every package a user installed). A section the config leaves out is shown only
when its tool has installed something.

`--draft FILE` writes a config of what this machine has installed, from the same lists:
system packages, taps, formulae and casks, App Store apps (by their Spotlight ids, so
neither mas nor a sign-in is needed), Flatpak apps and snaps, the global mise tools, npm,
cargo and uv tools, VS Code extensions, and on macOS a `[[manual]]` entry for each app
that came from neither a cask, the App Store, nor Apple. Review it before installing from
it. With `[install] draft = true` in capture.toml, each capture saves one in the snapshot
as `install.toml`.

`--draft` and `--brewfile` write `-` to stdout, and never replace an existing file
without `--force`. `install` works on a local molt root only.

## Command Line Options

### Molt and Output Options (capture, restore, migrate)

```
-r, --molt-root [[user@]host:]root  Molt root with configs, scripts and captures
                                    (default: ./.molt, then ~/.molt; on a host,
                                    .molt in its login directory)
-t, --config-toml <config>          Config file, relative to the molt root
--run-scripts                       Run the config's before/after scripts
                                    (on a host, molt.py there runs them)
--dry-run                           Show what would be done without changing anything
--verbose                           Show per-path details, each copied file and the commands run
--help                              Show help message
```

### Host Options (capture, restore, migrate)

```
--from [user@]host[:path]       capture, migrate: host to capture on (default: this machine)
                                restore: snapshot, date directory or host to restore from
                                (default: latest in <root>/captures)
--to [user@]host[:path]         capture: where to save the snapshot, a local path or host:path
                                (default: <root>/captures/YYYY/MM-DD)
                                restore, migrate: host to restore on (default: this machine)
```

`migrate` needs at least one of `--from` and `--to` to be a host.

A host is `user@host`, `host:`, or either followed by a path (`host:/path`). Anything
else is a local path; a local path containing `@` needs a `/` (e.g. `./name@tag`).
`--from` for capture/migrate and `--to` for restore/migrate must name a remote host.

A path on a host is relative to its login directory unless it is absolute (`~` and
`~/x` mean the same). A host with no path uses `.molt/captures` there, or the `-r` path's
`captures` when it is the `-r` host; the local molt root's path is never used on a host.

### Restore/Migrate Options

```
--disable-rollback              Do not back up existing files; ask before restoring each archive
```

### List and Prune Options

```
-r, --molt-root <root>          Local molt root (default: ./.molt, then ~/.molt)
--keep N                        prune: keep the newest N snapshots (with --backups, also
                                the newest N rotated backup dirs of each root)
--older-than DAYS               prune: keep only what is from the last DAYS days
--backups                       prune: also delete rotated backup dirs
--yes                           prune: delete without asking
--dry-run, --verbose            prune: show the plan only; list every kept path
```

### Install Options

```
-r, --molt-root <root>          Local molt root (default: ./.molt, then ~/.molt)
-t, --config-toml <config>      Install config, relative to the molt root or absolute
                                (default: configs/install.toml)
--status                        Show what is missing and what isn't in the config
--brewfile <file>               Write the [brew] and [mas] items as a Brewfile ('-': stdout)
--draft <file>                  Write a draft config of what is installed ('-': stdout)
--only <sections>               Only these sections, comma-separated
--skip <sections>               Leave out these sections, comma-separated
--force                         Let --brewfile and --draft replace an existing file
--dry-run, --verbose            Show the install commands only; show every query too
```

Sections: `apt`, `dnf`, `pacman`, `zypper`, `apk`, `brew`, `mas`, `flatpak`, `snapd`, `custom`,
`mise`, `npm`, `cargo`, `uv`, `vscode`, `manual`.

### Check Options

```
--ignore-invalid                Skip missing files instead of printing 'null'
--short-hash                    Show only the first 7 characters of each checksum
--full-path                     Show each path as given instead of just the file name
--no-path                       Show only the checksum (overrides --full-path)
```

`check` prints one `name: checksum` line per file on stdout (`null` for a missing file),
so its output can be parsed. Problems go to stderr (`Warning: x not found`).

## Rollback Protection

By default, restore operations back up existing files before overwriting. This is controlled by the `rollback` field in `[tarball]`:

```toml
[tarball]
rollback = ".bak"    # Backs up replaced files under /opt/myapp to /opt/myapp.bak/<archive>
```

Each root gets one backup directory, `<root><rollback>`, with one subdirectory per archive
(named after the archive without its extension). For example, with root `$HOME` and
`rollback = ".bak"`, restoring `dotfiles.tar.gz` backs up `~/.zshrc` to
`/Users/me.bak/dotfiles/.zshrc`. The restore output names the directory in each archive's
header: `dotfiles.tar.gz (root: /Users/me, backup: /Users/me.bak/dotfiles)`.

A backup is a move (a rename), not a copy, so the backed-up files keep every attribute
exactly: permissions, ownership, timestamps, xattrs, ACLs and hardlinks. A rollback moves
them back. Only when the backup directory is on another filesystem than a path (a mount
point below the root) is the path copied instead, keeping its mode, timestamps, symlinks,
ownership (as root) and, on Linux, xattrs; the original is then removed. That copy is not
exact: on macOS it drops xattrs, ACLs and file flags, and hardlinks inside the path become
separate files, so a rollback from it brings back that copy. Keep the backup directory on
the same filesystem as what you restore (or use `--disable-rollback`) where that matters.

Archives themselves store mode, ownership and timestamps, but not xattrs or ACLs, so
restored files come back without them. A move needs write
access to the path's parent directory (and to a directory itself), not read access to its
contents, so molt.py re-runs the restore with sudo only when those, or a copy, need root.

At the start of each restore run, a root's existing backup directory is moved aside once, to
`<root><rollback>_<timestamp>` (with `_<n>` added when that name is taken), before the first
archive with that root is restored. Archives that share a root then write into their own
subdirectories of the fresh directory, so they never move each other's backups aside.

If an archive fails mid-restore, its backed-up files are restored automatically. The
restore tries the remaining archives, then stops with exit status 1; the symlinks and after
scripts are not run. Use `--disable-rollback` to skip backups (falls back to interactive
confirmation per archive). Without backups, an archive that can't be read or extracted also
stops the restore this way; declining a prompt does not.

## How It Works

### Capture Process

1. Reads capture.toml configuration (or the `-t` config)
2. For each `[tar.*]` category, creates a compressed archive. If any archive can't be
   created, the capture stops with exit status 1 and no snapshot is saved
3. Generates snapshot.toml with per-archive SHA-256 checksums
4. Moves archives into a checksum-named subdirectory
5. Optionally runs scripts and/or sends to remote host

Snapshots are private to your user: the snapshot directory, and any `captures/YYYY` and
`MM-DD` directories a capture creates, are `0700`, and the archives, `snapshot.toml` and
any other file in the snapshot (such as a Brewfile an after script writes) are `0600`.
Directories that already exist, such as your `captures/`, keep their modes. Copies to a
host keep these modes, and the directories created there are created under `umask 077`.
Restored files get the modes stored in the archive.

### Restore Process

1. Verifies archive integrity using snapshot.toml checksums
2. Loads restore config to determine which of the archives listed in snapshot.toml to extract
3. Re-executes with sudo if needed for system paths
4. For each selected archive (with rollback enabled):
   - Moves existing files to `<root><rollback>/<archive>`
   - Extracts new files from archive
   - Rolls back on failure
5. Stops with exit status 1 if any archive failed
6. Creates symlinks for the restored archives
7. Runs scripts if enabled

### Remote Hosts

`capture --from`, `restore --to` and `migrate` run molt.py itself on the host, so the host
needs `python3` 3.11+ on its login `PATH` (this machine's interpreter path is never used
there) and rsync (and `age` for encrypted archives, see [Encryption](configuration.md#encryption)). Paths on a host are relative to its login directory, and can only use
letters, digits and `. _ / ~ + = , % @ -` (older rsync, including macOS's
`/usr/bin/rsync`, passes remote paths to the host's shell unescaped).

1. Creates a private work directory on the host with `mktemp -d` (under its `$TMPDIR`,
   or `/tmp`)
2. Copies what molt.py there needs into it, and nothing else:
   - `molt.py`
   - the config this command selected (`-t`; else `configs/deploy.toml` for
     `restore --to` when it exists, then the default; in a migration, the
     `[capture.*]` or `[restore.*]` half of `migrate.toml`) as `configs/capture.toml` or
     `configs/restore.toml`
   - with `--run-scripts` only, the config's scripts, at their paths in the molt root
     (`scripts/restore-brew.sh`); a script path outside the molt root stops the run
   - for a restore, the whole snapshot directory, as `snapshot/`
3. Runs molt.py there with the work directory as its molt root, passing `--verbose`,
   `--disable-rollback` and `--run-scripts` through:
   - capture: `python3 molt.py capture -r <dir> -t configs/capture.toml --to <dir>/out`,
     then copies the snapshot in `<dir>/out` back
   - restore, over `ssh -t` (so sudo and the prompts can ask on your terminal):
     `python3 molt.py restore --from <dir>/snapshot -r <dir> -t configs/restore.toml`.
     Like a local restore, it re-runs itself with sudo only when a restore target needs
     root (keeping the login user's `HOME`), and it runs the before and after scripts
     itself, as the login user or, after that re-run, as root
4. Removes the work directory, also when a step fails or on Ctrl-C

## Output

Every run prints the same shape: a `Starting <command>` line, `From:` and `To:` lines,
then one step per stage (`Creating 4 archives...`, `Restoring 4 archives...`) with its
items indented below, and one final line.

```
Starting restore
  From: /Users/u/.molt/captures/2026/09-22/cd555e5 (latest)
  To:   mymac (this machine)

Verifying 4 archives...
  ✓ 4 archives verified

Restoring 4 archives...
  dotfiles.tar.gz (root: /Users/u, backup: /Users/u.bak/dotfiles)
    replace: 1 path
    add: 1 path
  ✓ dotfiles.tar.gz restored
  ...

Creating symlinks...
  ✓ /Users/u/sysroot-link -> /sysroot

✓ Restore completed: 4 archives restored, 1 symlink created
```

- Progress, steps and results go to stdout. `Error:` and `Warning:` lines, and progress
  bars, go to stderr. Bars show only on a terminal.
- `✓` marks success. The final line has no `✓` when nothing selected was restored, or when
  an error was reported during the run (`(1 error reported above)`).
- Output from scripts and from molt.py running on a remote host is indented under the
  step that ran it. Remote restores and sudo re-runs print their own `Starting restore`
  and final lines (`Starting restore as root` when run with sudo).

## Dry-Run and Verbose Modes

```bash
# Preview operations
molt capture --dry-run
molt restore --dry-run

# Detailed progress
molt capture --verbose

# Combine both
molt capture --dry-run --verbose
```

Dry-run prints the same steps as a real run with `[DRY-RUN]` in front of every line and
makes no changes. Each command it skips is shown as `run: <command>` (ssh, rsync, bash),
paths that only a real run would create are shown by name (`<staging dir>`, `<checksum>`,
`<work dir>`, `<decrypted dir>`), and `Note:` lines name work it cannot preview. Queries that only read a
host still run: copying a remote molt root's configs and finding the latest snapshot. It
ends with `Dry run completed; no changes were made`.

```
[DRY-RUN] Starting capture (no changes will be made)
[DRY-RUN]   From: mymac (this machine)
[DRY-RUN]   To:   user@host:.molt/captures/2026/09-22
[DRY-RUN]
[DRY-RUN] Creating 3 archives...
[DRY-RUN]   dotfiles.tar.gz (root: /Users/u)
[DRY-RUN]     include: 2 paths
[DRY-RUN]   ...
[DRY-RUN]
[DRY-RUN] Writing snapshot.toml...
[DRY-RUN]
[DRY-RUN] Note: A real run would copy the snapshot to user@host:.molt/captures/2026/09-22/<checksum>
[DRY-RUN]
[DRY-RUN] Dry run completed; no changes were made
```

Verbose only adds lines: one line per path instead of counts (`include: .zshrc`), the
excluded paths (`exclude: ...`), the archives being verified, backup rotations, the transfer list with a `✓ <file> copied` line
per file, and the ssh and sudo commands that run.

## Security Considerations

- Archives store relative paths to prevent arbitrary file overwrites
- Extraction uses tarfile's `tar` filter (Python 3.11.4+) against path traversal, and
  refuses archives with absolute or `..` member paths
- User prompted before each archive extraction (when rollback is disabled)
- Sudo required for system file restoration, on a host too; molt.py asks for it only
  when a restore target needs root, and the root run starts Python with `-s` (no user
  site-packages)
- Snapshots and the directories a capture creates for them are private to your user
  (`0700` directories, `0600` files)
- Archives with `encrypt = true` are encrypted with age before they leave the private
  staging directory; restores decrypt them into a private temp directory that is always
  removed, and an identity (private key) is never copied to a host
- `prune` asks before deleting (unless `--yes`), never follows symlinks out of
  `captures/`, never deletes a current backup directory, and never uses sudo
- SSH commands use proper shell escaping via `shlex.quote()`, and remote paths are
  limited to characters no remote shell treats specially
- `-r host[:path]` trusts that host fully: its configs choose which local files a capture
  sends to it, and its snapshots choose what a restore writes (with sudo, anywhere)
- The sudo re-run gets `HOME` and the variables `root`/`link` paths use, but never
  `PATH`, `IFS`, `ENV`, `BASH_ENV`, `PYTHON*`, `LD_*` or `DYLD_*`
- Only molt.py, the selected config, the snapshot and (with `--run-scripts`) the config's
  scripts are copied to a host, into a private `mktemp -d` work directory

## Troubleshooting

### "Snapshot verification failed"
Archive corrupted during transfer or storage. Capture a new snapshot, or pick another
with `--from`. `--verbose` shows the expected and actual checksums.

### "... timed out on host after 300s" or "ssh error (exit status 255)"
Check network connectivity and SSH access. Only short ssh commands (mktemp, mkdir, ls,
rm) have a wall-clock limit. The remote capture and restore, scripts and rsync copies
run as long as they need; rsync stops a copy that moves no data for 300s (its
`--timeout`), and ssh's `ServerAliveInterval` notices a dead connection. The constants
are in molt.py.

### "Note: Restoring x.tar.gz needs root"
Some restore target is not writable by you. molt.py re-runs the restore with sudo
(`Re-running the restore with sudo...`), then prints the sudo run's own output.

### Remote restore or capture fails
Verify: SSH access works, `python3 --version` on the host (over ssh) is 3.11 or later,
rsync is installed on both machines, and sudo works on the host when the restore needs
root. molt.py on the host prints its own `Error:` lines first.

### No snapshots found
Check `~/.molt/captures/` or use `--from` to specify a path directly.

### "Error: brew bundle install failed" (or apt-get, dnf, ...) during install
The package manager could not install rsync. Its last lines of output are shown above
the error. Fix that (or install rsync yourself), then run `./install.sh` again; a re-run
keeps everything that was already installed.

### "Error: Cannot install 2 items from brew"

The section's tool ran, but those items still aren't installed. Its output, above the
Error line, says why (a cask that needs a newer macOS, an App Store app that isn't signed
in or bought). The Error lines list each item and the command's exit status; fix the
cause and run `molt install` again: it only retries what is missing.

### `molt` runs another program
The installer names the command `molt-cli` when it finds another `molt`; run
`./install.sh --name NAME` for another name.

## Development

Everything is checked locally (there is no hosted CI):

```bash
./check.sh          # ruff, shellcheck and all tests
./check.sh --fast   # the same without the slow end-to-end tests (a few seconds)
./check.sh --fast -k exclude   # other arguments go to pytest
```

`check.sh` runs `ruff check molt.py tests/`, `shellcheck install.sh check.sh
scripts/*.sh` and `python3 -m pytest`, runs all three even when one fails, and exits 1
when any of them failed. Their settings are in `pyproject.toml` (ruff: line length 100,
rules F, E, W and B; pytest: `testpaths`, `-p no:cacheprovider` and the `slow` marker).
It needs ruff, shellcheck and pytest (`brew install ruff shellcheck`,
`python3 -m pip install --user pytest`).

Tests marked `slow` run molt.py or install.sh in a subprocess: every test that uses the
`env` fixture in `tests/test_molt.py` (marked in `tests/conftest.py`) and all of
`tests/test_install.py`. They run in temp directories with a temp `HOME` and fake `sudo`,
`ssh`, `rsync`, `age`, `brew`, `apt-get` and `pip` (a `python3` shim), so they never use
real sudo, never contact a host and never install anything. See [AGENTS.md](../AGENTS.md) for how the
code is organized.

## Snapshot Rotation

Use [`molt prune`](#prune-old-snapshots), for example `molt prune --keep 7 --backups`.

## Limitations

- No incremental snapshots (always full capture)
- Encryption is per archive (`encrypt = true`); `snapshot.toml` itself, with the roots and
  links, is not encrypted
- Remote operations require SSH and rsync
