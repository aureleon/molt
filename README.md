# molt

Moves a machine's setup to another one. molt captures files into tar archives (a
*snapshot*), restores them locally or on another machine over SSH, and installs the packages
and apps the new machine is missing. Every snapshot has a `snapshot.toml` with SHA-256
checksums of its archives, so a restore only extracts what it can verify.

Snapshots are full captures, not incremental backups. On top of that, molt can encrypt
archives with [age](https://age-encryption.org), back up every file a restore replaces, and
run your own scripts before and after a capture or restore.

## Requirements

- Python 3.11+ as `python3` (restore needs 3.11.4+ for the tar extraction filter)
- SSH and rsync for anything remote, on both ends (hosts need Python 3.11+ too)
- sudo, only when a restore target needs root
- Optional: `tqdm` for progress bars, and age for encryption

## Installation

From a checkout of this repo:

```bash
./install.sh          # asks where the command goes
./install.sh --yes    # no prompts: ~/.molt, and the command in ~/.local/bin
```

The installer creates `~/.molt/` with example configs, installs rsync and tqdm when they are
missing, and links `molt` into the bin dir. Re-running it updates `molt.py`, but never
overwrites a config or script you already have. If another `molt` is already on `PATH`, the
command is named `molt-cli` instead.

For a root somewhere else, run `./install.sh /path/to/root`, then pass `-r /path/to/root`
to each command. See [installation](docs/usage.md#installation) for the other options.

## Capture and restore

`~/.molt/configs/capture.toml` says what to capture. Each `[tar.<name>]` table becomes one
archive, e.g. a small first capture:

```toml
[tarball]
compress = "gzip"
checksum = "sha256"
rollback = ".bak"

[tar.dotfiles]
root = "$HOME"
files = [".bashrc", ".gitconfig"]
```

Then preview it, and capture:

```bash
molt capture --dry-run
molt capture
```

Snapshots go in `~/.molt/captures/YYYY/MM-DD/<id>/`. A `./.molt/` in the current directory
takes priority over `~/.molt/`, and `-r` picks any other root.

`~/.molt/configs/restore.toml` selects the archives to restore, by name or glob:

```toml
[tar]
archives = ["dotfiles"]
```

```bash
molt restore --dry-run    # the latest snapshot
molt restore
```

Note, a restore replaces files at the roots recorded in the snapshot, so only restore
snapshots you trust. By default the files it replaces are moved into a backup dir first
(`$HOME.bak/dotfiles/` for the example above), and a failed archive is rolled back. See
[rollback protection](docs/usage.md#rollback-protection) for where backups go.

## Remote hosts

`--from` captures on a host or restores a snapshot from one, and `--to` sends a capture to a
host or restores on one:

```bash
molt capture --from user@host
molt capture --to user@host
molt restore --from user@host --dry-run
molt restore --to user@host --dry-run
molt migrate --to user@host    # capture here, restore there
```

molt copies itself and the config to the host for the run, and removes them after. See
[usage](docs/usage.md#usage) for host paths, remote roots, and remote to remote.

## Installing packages and apps

`molt install` installs what `~/.molt/configs/install.toml` lists and the machine doesn't
have yet: Homebrew taps, formulae, and casks, App Store apps, mise runtimes, npm, cargo, and
uv tools, and VS Code extensions. Anything else goes in a `[[custom]]` entry as a shell
`check` and `install` command. Apps that only IT or a vendor's installer can provide go in
`[[manual]]`, which molt only checks for, and lists at the end of a run with where to get
them.

Rather than writing the list by hand, start from a draft of what this machine has:

```bash
molt install --draft ~/.molt/configs/install.toml
molt install --status     # what is missing, and what is installed but not listed
molt install --dry-run
molt install
```

It never upgrades what is already installed, and only retries what is still missing, so
re-running it is safe. With `[install] draft = true` in capture.toml, every capture also saves
a draft `install.toml` in the snapshot, for the next machine.

For now install covers macOS. Linux package managers (apt, dnf, pacman, and friends) are TODO,
but `[[custom]]` entries already work there.

See [install.toml](docs/configuration.md#installtoml) and
[installing packages](docs/usage.md#install-packages-and-apps) for every section and option.

## Common commands

```bash
molt list                              # snapshots, and the backup dirs of their roots
molt prune --keep 5 --dry-run          # what pruning to the newest 5 would delete
molt restore --from <snapshot dir>     # a specific snapshot instead of the latest
molt capture --run-scripts             # also run the config's before/after scripts
molt install --brewfile Brewfile       # the [brew] and [mas] items as a Brewfile
molt <command> --help
```

Every command except `list` and `check` takes `--dry-run`. A dry run still does the read-only
parts (configs, checksums, what is installed), and shows each command it skips.

## Encryption

Set `encrypt = true` on an archive and add your age recipients to capture.toml. A restore then
needs your age identity file, which never leaves the machine it is on. Note, `snapshot.toml`
itself stays unencrypted. See [encryption](docs/configuration.md#encryption).

## Documentation

- [Usage and options](docs/usage.md): installation, local and remote use, every flag, backups,
  troubleshooting, and development
- [Configuration](docs/configuration.md): capture, restore, migrate, and install configs, globs,
  excludes, encryption, and symlinks
