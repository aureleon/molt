# Configuration

[Project README](../README.md) · [Usage and options](usage.md)

## Configuration Files

### capture.toml

Defines what to capture. Each `[tar.*]` section becomes a separate archive.

```toml
[tarball]
compress = "gzip"       # Compression: gzip, bzip2, xz, or "" (none)
checksum = "sha256"     # Checksums are always sha256 (the only supported value)
rollback = ".bak"       # Extension for backing up existing files during restore

[tar.ssh-keys]
root = "$HOME"
dirs = [".ssh"]

[tar.dotfiles]
root = "$HOME"
files = [".*rc", ".gitconfig"]     # Glob patterns supported

[tar.config]
root = "$HOME"
dirs = [".config/*"]               # Glob: all subdirs in .config
exclude = ["node_modules", "*.log", "*/Cache/*"] # Left out of the archive

[tar.myapp]
root = "/opt/myapp"
link = "$HOME/myapp"               # Creates symlink: ~/myapp -> /opt/myapp
dirs = ["bin", "lib", "config"]

[scripts]
# before = ["scripts/pre-capture.sh"]
after = ["scripts/capture-brew.sh"]
```

Key fields:
- `root` (required): Base directory for paths in `dirs`/`files`
- `dirs`/`files`: Paths relative to `root`, supports glob patterns
- `link` (optional): Creates a symlink pointing to `root` during restore
- `exclude` (optional): Patterns for paths to leave out of the archive (see
  [Excludes](#excludes))
- `encrypt` (optional): `true` encrypts the archive with age, for the recipients in the
  `[age]` table (see [Encryption](#encryption))

`[install] draft = true` (optional) saves a draft [install.toml](#installtoml) of what the
machine has installed in each snapshot (`[capture.install]` in migrate.toml).

### restore.toml

Defines which archives to restore and when to run scripts. Names and patterns match the
`[tar.<name>]` tables in the snapshot's `snapshot.toml`; an archive file in the snapshot
directory that `snapshot.toml` doesn't list is never restored.

```toml
[tar]
# Restore specific archives (exact or glob patterns)
archives = ["ssh*", "dotfiles", "*-config"]

# Or restore all available
# archives = ["*"]

# Or skip archive restoration (scripts only)
# archives = []

[scripts]
# before = ["scripts/pre-restore.sh"]
after = ["scripts/restore-brew.sh"]

[age]
# Needed only to restore encrypted archives (see Encryption)
# identity = "~/.config/age/key.txt"
```

### migrate.toml

Uses `[capture.*]` and `[restore.*]` namespaces. Captures on the source machine, then restores on the destination. `[capture.scripts]` run on the source, `[restore.scripts]` run on the destination.

```toml
[tarball]
compress = "gzip"
checksum = "sha256"
rollback = ".bak"

[capture.tar.ssh-keys]
root = "$HOME"
dirs = [".ssh"]

[capture.tar.dotfiles]
root = "$HOME"
files = [".*rc", ".gitconfig"]

[capture.scripts]
after = ["scripts/dump-database.sh"]     # Runs on source

[restore.tar]
archives = ["*"]

[restore.scripts]
before = ["scripts/fix-permissions.sh"]  # Runs on destination
```

For encryption, `[capture.age]` holds the recipients and `[restore.age]` the identity, as
`[age]` does in `capture.toml` and `restore.toml`.

### install.toml

Lists what `molt install` installs on a machine. Every section is optional, and one file
can cover macOS and Linux machines (see [Systems](#systems) below). Start from a draft of
what a machine has: `molt install --draft draft.toml`.

```toml
[install]
order = ["custom", "brew"]   # optional; the sections it leaves out follow, in default order

[apt]                        # Debian, Ubuntu
packages = ["build-essential", "curl", "git"]
update = true                # 'apt-get update' first (the default)

[dnf]                        # Fedora, RHEL, Amazon Linux
packages = ["gcc", "curl", "git"]

[pacman]                     # Arch
packages = ["base-devel", "git"]
update = false               # true: 'pacman -Syu' with the install, which upgrades the system

[zypper]                     # openSUSE, SLES
packages = ["git"]

[apk]                        # Alpine
packages = ["git", "bash"]

[flatpak]
apps = ["org.gimp.GIMP"]
remote = "flathub"           # the default; flathub is added when it is missing
user = true                  # the default: --user, no root needed

[snapd]
packages = ["hello"]
classic = ["nvim"]           # snaps that need --classic

[brew]
taps = ["powershell/tap"]
formulae = ["jj", "ripgrep", "powershell/tap/powershell"]
casks = ["ghostty", "utm"]

[mas.apps]                   # App Store apps: name = id (the number in the App Store URL)
Xcode = 497799835

[mise]
global = true                # 'mise use --global' (the default); false: 'mise install' only

[mise.tools]
node = "latest"
python = ["3.12", "3.11"]    # the first one is the default

[npm]
packages = ["firebase-tools"]

[cargo]
crates = ["cargo-fuzz"]

[uv]
tools = ["ruff"]

[vscode]
extensions = ["ms-python.python"]

[[custom]]                   # one thing: installed when 'check' exits non-zero
name = "rustup"
check = "command -v rustup"
install = "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y"

[[custom]]                   # a list of things, each checked and installed on its own
name = "rust targets"
requires = "rustup"          # skipped while rustup is missing
items = ["aarch64-apple-darwin", "x86_64-apple-darwin"]
list = "rustup target list --installed"   # or a 'check' with {item}
install = "rustup target add {item}"

[[manual]]                   # only checked, then listed as left to install by hand
name = "Slack"
app = "Slack.app"            # in /Applications or ~/Applications, or a path; or a 'check'
source = "Self Service"      # 'source', 'url' and 'note' say how to get it
```

Sections install in this order, unless `[install] order` sets another: `apt`, `dnf`,
`pacman`, `zypper`, `apk`, `brew`, `mas`, `flatpak`, `snapd`, `custom`, `mise`, `npm`,
`cargo`, `uv`, `vscode`, `manual`. The system package managers go first, since on Linux
they install what Homebrew and the language tools build with; Homebrew comes next, as it
installs most other tools; `custom` comes before the language tools, so it can install
their managers.

- The system package managers (`apt` through `apk`) and `snapd` run as root: directly
  when molt runs as root, else through sudo. Each one is skipped on machines that don't
  have it, so a config can list packages for several distros.
- `[brew]` and `[mas]` install through `brew bundle install --no-upgrade`, with only the
  missing items in its Brewfile. A cask whose app is already in `/Applications` is
  adopted. App Store apps install with mas, which brew installs first if needed; the App
  Store must be signed in, and an app must have been bought or claimed before. Homebrew
  won't run as root, so as root both are skipped.
- `[[custom]]` commands run with bash. Without bash (a small Alpine, say) the entries
  are skipped, and so are `[[manual]]` entries with a `check`. `check` exits 0 when the
  thing is installed. With `items`, each item gets its own check and install, with
  `{item}` replaced by the item, shell-quoted. A `list` command that prints the installed
  items, one per line, takes the place of the checks, and lets `--status` show the ones
  not in the config. `requires` names a command the entry needs; while it is missing,
  the entry is skipped.
- `[[manual]]` is for what no command should install: apps your device management pushes
  or offers in a self-service portal, or installers without a cask.

A typo is an error: an unknown table or key stops the run before anything is installed.

#### Systems

Any table, `[[custom]]` entry or `[[manual]]` entry can take an `os` key: a system name,
or an array of them. It is then only used on those systems, and left out elsewhere (shown
only with `--verbose`). The names are `macos`, `linux`, and on Linux the distro's `ID` and
`ID_LIKE` from `/etc/os-release`, so `"debian"` also covers Ubuntu and `"fedora"` covers
Amazon Linux.

To list different items per system, write a section as an array of tables instead of one
table, each with its own `os` (TOML doesn't allow `[brew]` and `[[brew]]` in one file):

```toml
[[brew]]
formulae = ["jj", "ripgrep"]

[[brew]]
os = "macos"
casks = ["ghostty"]

[[brew]]
os = "linux"
formulae = ["gcc"]

[[custom]]
name = "rustup"
os = ["macos", "linux"]
check = "command -v rustup"
install = "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y"
```

Some parts are macOS only without an `os` key: `[mas]`, casks (on Linux a table's casks
are left out, and its formulae still install), and `[[manual]]` entries whose `app` is a
name in `/Applications`. `[[manual]]` entries with a `check` work everywhere.

## Encryption

An archive whose `[tar.<name>]` table sets `encrypt = true` is encrypted with the
[age](https://age-encryption.org) CLI, which must be installed (`brew install age`, or
`apt install age`). The capture config's `[age]` table names who can decrypt it, with
age public keys, a file of them, or both:

```toml
[age]
recipients = ["age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p"]
recipients_file = "~/.config/age/recipients.txt"   # expanded like a root

[tar.ssh-keys]
root = "$HOME"
dirs = [".ssh"]
encrypt = true
```

The archive is created in the private staging directory, encrypted there with
`age -e -r ... -R ... -o ssh-keys.tar.gz.age ssh-keys.tar.gz`, and the plaintext is
removed; only `ssh-keys.tar.gz.age` goes into the snapshot. `snapshot.toml` marks the
archive with `encrypted = true`, and its checksum is over the `.age` file, so a restore
verifies it before decrypting. A category with `encrypt` but no recipients is a config
error, and so is a missing `age` (the capture stops before creating any archive).

To restore it, the restore config's `[age]` table (in `migrate.toml`, `[restore.age]`)
names your identity (private key) file:

```toml
[age]
identity = "~/.config/age/key.txt"
```

The selected encrypted archives are decrypted with `age -d -i <identity>` into a new
private (`0700`) temp directory, which is always removed when the restore ends. The
restore then reads the plaintext copies like any other archive; in output they are named
without `.age` (`ssh-keys.tar.gz`). Selection and globs in `archives` use the archive's
name as usual. An encrypted archive that is not selected needs no identity. A dry run
shows the `age` commands (`run: age -d ...`) and never decrypts, so it can't list an
encrypted archive's paths.

On hosts: the host that runs the capture (`capture --from`, `migrate --from`) needs `age`,
and reads `recipients_file` there. The host that runs the restore (`restore --to`,
`migrate --to`) needs `age` and the identity file at the configured path on that host;
molt.py sends only that path, never a key. When a restore re-runs itself with sudo, `age`
must be on root's `PATH` too.

## Glob Patterns

### In Capture Configuration (files/dirs)

Patterns are relative to the `root` directory:

```toml
[tar.dotfiles]
root = "$HOME"
files = [".*rc"]                # Matches .bashrc, .zshrc, etc.
dirs = [".config/*"]            # All subdirs in .config

[tar.logs]
root = "/var/log"
files = ["*.log", "nginx/*.log"]
```

Standard glob syntax: `*` (any chars), `?` (single char), `[seq]` (char set), `**` (recursive).

### Excludes

A `[tar.<name>]` table's `exclude` key lists fnmatch patterns for paths to leave out. A
pattern excludes a path when it matches the path relative to `root`, or any single part
of it. An excluded directory is left out with everything below it.

```toml
[tar.projects]
root = "$HOME"
dirs = ["src"]
exclude = [
    "node_modules",    # anything named node_modules, at any depth
    "*.log",           # any name ending in .log, at any depth
    "src/build/*",     # everything below src/build (the empty dir is kept)
    "src/tmp",         # this one path
]
```

In fnmatch patterns `*` also matches `/`, so `src/build/*` matches `src/build/a/b` too.
`--verbose` lists each excluded path with the pattern that matched it
(`exclude: src/app/debug.log (pattern '*.log')`), in a dry run too.

On macOS, captures also leave out the metadata files `.DS_Store` and `._*` (AppleDouble
files) at any depth.

### In Restore Configuration (archives)

```toml
archives = ["ssh*"]              # All archives starting with "ssh"
archives = ["*-config"]          # All archives ending with "-config"
archives = ["*"]                 # Restore all
archives = []                    # Skip restoration
```

## Symlinks

The `link` field in capture config creates symlinks during restore:

```toml
[tar.workspace]
root = "/Volumes/workplace"
link = "$HOME/workplace"    # Creates: ~/workplace -> /Volumes/workplace
dirs = ["projects"]
```

Symlinks are created after extraction, only for the archives restored in that run (not for
archives that were not selected or were declined at the prompt). If the symlink already
exists and points to the correct target, it's skipped. Errors if the path exists but points
elsewhere.
