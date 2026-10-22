#!/usr/bin/env bash
#
# Install molt.py: its molt root (molt.py, configs/, scripts/, captures/) and a command on PATH.
#
# Usage: ./install.sh [--yes] [--name NAME] [--bin-dir DIR] [install_dir]
#
# Re-running it is safe: it updates molt.py and the command link, and never overwrites a
# config or script that already exists.

set -Eeuo pipefail

usage() {
    cat <<'EOF'
Usage: ./install.sh [options] [install_dir]

Install molt.py into install_dir (default: ~/.molt) and link a command to it.

Options:
  -y, --yes        Don't ask; use the defaults (the command goes in ~/.local/bin)
  --name NAME      Command name (default: molt, or molt-cli when another molt is on PATH)
  --bin-dir DIR    Directory for the command, instead of asking
  -h, --help       Show this help and exit

Existing configs and scripts in install_dir are never overwritten.
EOF
}

# --- Output --- #

say() { printf '  %s\n' "$*"; }
detail() { printf '    %s\n' "$*"; }
ok() { printf '  ✓ %s\n' "$*"; }
step() { printf '\n%s...\n' "$*"; }
note() { printf '\nNote: %s\n' "$*"; }

warn() {
    printf 'Warning: %s\n' "$1" >&2
    shift
    local line
    for line in "$@"; do
        printf '  %s\n' "$line" >&2
    done
}

fatal() {
    printf 'Error: %s\n' "$1" >&2
    shift
    local line
    for line in "$@"; do
        printf '  %s\n' "$line" >&2
    done
    exit 1
}

# Relay a command's captured output below the current item, one level deeper
relay() {
    local line
    while IFS= read -r line; do
        printf '    %s\n' "$line"
    done <<<"$1"
}

# Never stop without a word: set -e exits on an unexpected failure, so name it. Failures in
# $(...) subshells are handled (or reported) by the command that ran them
on_error() {
    local status=$?
    [ "$BASH_SUBSHELL" -eq 0 ] || return "$status"
    printf 'Error: install.sh failed at line %s (exit status %s)\n' "$1" "$status" >&2
    exit "$status"
}
trap 'on_error $LINENO' ERR

# Run a command, with sudo when the first argument is "sudo"
as_root() {
    local sudo="$1"
    shift
    if [ -n "$sudo" ]; then
        sudo "$@"
    else
        "$@"
    fi
}

# --- Arguments --- #

ASSUME_YES=0
CMD_NAME=""
BIN_DIR=""
INSTALL_DIR=""

while [ $# -gt 0 ]; do
    case "$1" in
        -y|--yes) ASSUME_YES=1 ;;
        --name)
            [ $# -ge 2 ] || fatal "--name needs a value" "Usage: ./install.sh --name NAME"
            CMD_NAME="$2"
            shift
            ;;
        --name=*) CMD_NAME="${1#--name=}" ;;
        --bin-dir)
            [ $# -ge 2 ] || fatal "--bin-dir needs a value" "Usage: ./install.sh --bin-dir DIR"
            BIN_DIR="$2"
            shift
            ;;
        --bin-dir=*) BIN_DIR="${1#--bin-dir=}" ;;
        -h|--help) usage; exit 0 ;;
        --) shift; break ;;
        -*) fatal "Unknown option '$1'" "Run ./install.sh --help for the options" ;;
        *)
            [ -z "$INSTALL_DIR" ] || fatal "Too many arguments: '$INSTALL_DIR' and '$1'" \
                "Usage: ./install.sh [options] [install_dir]"
            INSTALL_DIR="$1"
            ;;
    esac
    shift
done
if [ $# -gt 0 ]; then
    [ -z "$INSTALL_DIR" ] && [ $# -eq 1 ] || fatal "Too many arguments" \
        "Usage: ./install.sh [options] [install_dir]"
    INSTALL_DIR="$1"
fi

if [ -n "$CMD_NAME" ]; then
    case "$CMD_NAME" in
        */*|.|..|-*) fatal "Invalid command name '$CMD_NAME'" "Use a plain name, e.g. --name molt-cli" ;;
    esac
fi

# Prompts need a terminal; without one (or with --yes) the defaults are used
INTERACTIVE=0
if [ "$ASSUME_YES" -eq 0 ] && [ -t 0 ]; then
    INTERACTIVE=1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
DEFAULT_INSTALL_DIR="$HOME/.molt"

# An absolute path; '~' is expanded as the shell would
absolute_path() {
    local path="$1"
    case "$path" in
        \~) path="$HOME" ;;
        \~/*) path="$HOME/${path#\~/}" ;;
    esac
    case "$path" in
        /*) ;;
        *) path="$PWD/$path" ;;
    esac
    # Drop a trailing slash (but keep "/")
    while [ "${#path}" -gt 1 ] && [ "${path%/}" != "$path" ]; do
        path="${path%/}"
    done
    printf '%s\n' "$path"
}

INSTALL_DIR="$(absolute_path "${INSTALL_DIR:-$DEFAULT_INSTALL_DIR}")"
if [ -n "$BIN_DIR" ]; then
    BIN_DIR="$(absolute_path "$BIN_DIR")"
fi

# molt was called snap, with its root in ~/.snap. When that is all there is, it moves to
# ~/.molt (--yes moves it, a terminal asks first); otherwise it is updated where it is,
# since molt falls back to ~/.snap only while ~/.molt doesn't exist
LEGACY_DIR="$HOME/.snap"
LEGACY_MOVED=0
if [ "$INSTALL_DIR" = "$DEFAULT_INSTALL_DIR" ] && [ -d "$LEGACY_DIR" ] \
    && [ ! -L "$LEGACY_DIR" ] && [ ! -e "$INSTALL_DIR" ] && [ ! -L "$INSTALL_DIR" ]; then
    move=0
    if [ "$ASSUME_YES" -eq 1 ]; then
        move=1
    elif [ "$INTERACTIVE" -eq 1 ]; then
        printf '%s is the root from before snap was renamed molt\n' "$LEGACY_DIR"
        printf 'Move it to %s? [Y/n]: ' "$INSTALL_DIR"
        read -r answer || { printf '\n'; answer="n"; }
        case "$answer" in ""|[yY]|[yY][eE][sS]) move=1 ;; esac
    fi
    if [ "$move" -eq 1 ]; then
        mv "$LEGACY_DIR" "$INSTALL_DIR" || fatal "Cannot move $LEGACY_DIR to $INSTALL_DIR"
        LEGACY_MOVED=1
    else
        INSTALL_DIR="$LEGACY_DIR"
    fi
fi

printf 'Starting install\n'
say "From: $SCRIPT_DIR"
say "To:   $INSTALL_DIR"
if [ "$LEGACY_MOVED" -eq 1 ]; then
    ok "$LEGACY_DIR moved to $INSTALL_DIR"
elif [ "$INSTALL_DIR" = "$LEGACY_DIR" ] && [ ! -e "$DEFAULT_INSTALL_DIR" ]; then
    say "keeping $LEGACY_DIR (move it to $DEFAULT_INSTALL_DIR with --yes, or by hand)"
fi

# --- Checks --- #

[ -f "$SCRIPT_DIR/molt.py" ] || fatal "molt.py not found in $SCRIPT_DIR" \
    "Run install.sh from a molt.py checkout"

# Compare physical paths, so a symlink to the checkout is caught too
INSTALL_DIR_PHYSICAL="$INSTALL_DIR"
if [ -d "$INSTALL_DIR" ]; then
    INSTALL_DIR_PHYSICAL="$(cd "$INSTALL_DIR" && pwd -P)"
fi
if [ "$INSTALL_DIR_PHYSICAL" = "$SCRIPT_DIR" ]; then
    fatal "Cannot install into the checkout itself: $INSTALL_DIR" \
        "Pick another directory, e.g. ./install.sh ~/.molt"
fi

# An existing directory must be empty or look like a molt root, so that a typo such as
# './install.sh ~' doesn't spread configs/, scripts/ and captures/ into it
if [ -e "$INSTALL_DIR" ] && [ ! -d "$INSTALL_DIR" ]; then
    fatal "Cannot install into $INSTALL_DIR: it is not a directory"
fi
if [ -d "$INSTALL_DIR" ] && [ -n "$(ls -A "$INSTALL_DIR")" ] \
    && [ ! -e "$INSTALL_DIR/molt.py" ] && [ ! -d "$INSTALL_DIR/configs" ] \
    && [ ! -d "$INSTALL_DIR/captures" ]; then
    fatal "$INSTALL_DIR is not empty and is not a molt root (no molt.py, configs/ or captures/)" \
        "Pick a new or empty directory, e.g. ./install.sh ~/.molt"
fi

step "Checking requirements"

case "$(uname -s)" in
    Darwin) PLATFORM="macos" ;;
    Linux) PLATFORM="linux" ;;
    *) fatal "Unsupported operating system: $(uname -s)" "molt.py supports macOS and Linux" ;;
esac

command -v python3 >/dev/null 2>&1 || fatal "python3 not found" \
    "Install Python 3.11 or later, then run install.sh again"
PYTHON_VERSION="$(python3 -c 'import platform; print(platform.python_version())')" \
    || fatal "Cannot run python3" "Install Python 3.11 or later, then run install.sh again"
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
    fatal "molt.py needs Python 3.11 or later (found $PYTHON_VERSION)" \
        "Install Python 3.11 or later as python3, then run install.sh again"
fi
ok "python3 $PYTHON_VERSION"

# tqdm only adds progress bars: a failed pip install (e.g. PEP 668's externally managed
# Python) is a warning, and --break-system-packages is never used
if python3 -c 'import tqdm' >/dev/null 2>&1; then
    ok "tqdm (optional) already installed"
else
    say "tqdm (optional, for progress bars)"
    detail "run: python3 -m pip install --user -r $SCRIPT_DIR/requirements.txt"
    if pip_output="$(python3 -m pip install --user -r "$SCRIPT_DIR/requirements.txt" 2>&1)"; then
        ok "tqdm installed"
    else
        relay "$(printf '%s\n' "$pip_output" | tail -n 3)"
        warn "Cannot install tqdm with pip; molt runs without progress bars" \
            "To get them, install tqdm with your package manager (apt install python3-tqdm," \
            "brew install tqdm) or in a virtual environment"
    fi
fi

# System packages (rsync, for copies to and from hosts). A package manager that fails
# fails the install
if [ "$PLATFORM" = "macos" ]; then
    if ! command -v brew >/dev/null 2>&1; then
        if command -v rsync >/dev/null 2>&1; then
            warn "Homebrew not found; using $(command -v rsync)" \
                "Install Homebrew (https://brew.sh) and run install.sh again for a newer rsync"
        else
            fatal "Homebrew not found, and rsync is not installed" \
                "Install Homebrew (https://brew.sh) or rsync, then run install.sh again"
        fi
    elif brew bundle check --no-upgrade --file="$SCRIPT_DIR/Brewfile" >/dev/null 2>&1; then
        ok "Brewfile packages already installed"
    else
        say "Brewfile packages"
        # --no-upgrade: install what is missing, never upgrade what the user already has
        detail "run: brew bundle install --no-upgrade --file=$SCRIPT_DIR/Brewfile"
        if brew_output="$(HOMEBREW_NO_INSTALL_UPGRADE=1 HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK=1 \
            brew bundle install --no-upgrade --file="$SCRIPT_DIR/Brewfile" 2>&1)"
        then
            ok "Brewfile packages installed"
        else
            relay "$(printf '%s\n' "$brew_output" | tail -n 5)"
            fatal "brew bundle install failed" \
                "Fix the problem above (or install rsync yourself), then run install.sh again"
        fi
    fi
else
    missing=""
    checked=""
    while IFS= read -r pkg || [ -n "$pkg" ]; do
        case "$pkg" in ""|"#"*) continue ;; esac
        checked="$checked $pkg"
        if ! command -v "$pkg" >/dev/null 2>&1; then
            missing="$missing $pkg"
        fi
    done <"$SCRIPT_DIR/packages.txt"
    missing="${missing# }"

    if [ -z "$missing" ]; then
        ok "system packages already installed:$checked"
    else
        if [ "$(id -u)" -eq 0 ]; then
            AS_ROOT=""
        elif command -v sudo >/dev/null 2>&1; then
            AS_ROOT="sudo"
        else
            fatal "Missing packages: $missing; sudo not found to install them" \
                "Install them as root, then run install.sh again"
        fi
        if command -v apt-get >/dev/null 2>&1; then
            install_cmds=("apt-get update" "apt-get install -y $missing")
        elif command -v dnf >/dev/null 2>&1; then
            install_cmds=("dnf install -y $missing")
        elif command -v yum >/dev/null 2>&1; then
            install_cmds=("yum install -y $missing")
        elif command -v pacman >/dev/null 2>&1; then
            install_cmds=("pacman -S --noconfirm $missing")
        elif command -v zypper >/dev/null 2>&1; then
            install_cmds=("zypper install -y $missing")
        else
            fatal "Missing packages: $missing; no supported package manager found" \
                "Install them yourself, then run install.sh again"
        fi
        say "system packages: $missing"
        for install_cmd in "${install_cmds[@]}"; do
            shown_cmd="${AS_ROOT:+$AS_ROOT }$install_cmd"
            detail "run: $shown_cmd"
            # The commands are plain words (package names from packages.txt): split them
            # shellcheck disable=SC2086
            if ! as_root "$AS_ROOT" $install_cmd; then
                fatal "Cannot install $missing: '$shown_cmd' failed" \
                    "Fix the problem above (or install them yourself), then run install.sh again"
            fi
        done
        ok "system packages installed"
    fi
fi

# --- Files --- #

step "Installing files"

# New directories are private (0700) like the snapshots in them; existing ones keep their mode
make_dir() {
    if [ ! -d "$1" ]; then
        mkdir -m 700 "$1"
        ok "${1#"$INSTALL_DIR"/}/ created"
    fi
}

if [ ! -d "$INSTALL_DIR" ]; then
    mkdir -p "$(dirname "$INSTALL_DIR")"
    mkdir -m 700 "$INSTALL_DIR"
    ok "$INSTALL_DIR created"
fi
make_dir "$INSTALL_DIR/configs"
make_dir "$INSTALL_DIR/scripts"
make_dir "$INSTALL_DIR/captures"

# molt.py is the program: re-running the installer updates it (but not through a link
# someone made, which may point into a checkout)
if [ -L "$INSTALL_DIR/molt.py" ]; then
    say "skip: molt.py (a link to $(readlink "$INSTALL_DIR/molt.py"))"
elif [ ! -e "$INSTALL_DIR/molt.py" ]; then
    cp "$SCRIPT_DIR/molt.py" "$INSTALL_DIR/molt.py"
    chmod 755 "$INSTALL_DIR/molt.py"
    ok "molt.py installed"
elif cmp -s "$SCRIPT_DIR/molt.py" "$INSTALL_DIR/molt.py"; then
    say "molt.py: up to date"
else
    cp "$SCRIPT_DIR/molt.py" "$INSTALL_DIR/molt.py"
    chmod 755 "$INSTALL_DIR/molt.py"
    ok "molt.py updated"
fi

# Copy an example as a user file, only when that file doesn't exist yet
install_example() {
    local src="$1" rel="$2" mode="$3"
    local dst="$INSTALL_DIR/$rel"
    if [ -e "$dst" ] || [ -L "$dst" ]; then
        say "skip: $rel (exists)"
        return
    fi
    cp "$src" "$dst"
    chmod "$mode" "$dst"
    ok "$rel installed (from ${src#"$SCRIPT_DIR"/})"
}

for name in capture restore migrate install; do
    install_example "$SCRIPT_DIR/configs/example-$name.toml" "configs/$name.toml" 600
done
for src in "$SCRIPT_DIR"/scripts/example-*.sh; do
    [ -e "$src" ] || continue
    base="$(basename "$src")"
    install_example "$src" "scripts/${base#example-}" 700
done

# Older install.sh versions linked ~/Snapshots to captures/ with 'ln -sf', and a re-run then
# created captures/captures. Point that out; it's the user's to remove
if [ -L "$INSTALL_DIR/captures/captures" ]; then
    warn "$INSTALL_DIR/captures/captures is a link an older install.sh created" \
        "Remove it with: rm $INSTALL_DIR/captures/captures"
fi

# --- Command --- #

step "Installing the command"

# True when a path runs a molt.py, or the snap.py it was called before (this one, or a
# copy an older install made)
is_molt_py() {
    head -n 5 "$1" 2>/dev/null | grep -q '^Snapshot and restore utility'
}

# The command name that is free for a bin dir: the given one, else molt, else molt-cli
pick_name() {
    local bin_dir="$1" name found
    if [ -n "$CMD_NAME" ]; then
        printf '%s\n' "$CMD_NAME"
        return
    fi
    for name in molt molt-cli; do
        found="$(command -v "$name" 2>/dev/null || true)"
        if [ -n "$found" ] && [ "${found#/}" != "$found" ] && ! is_molt_py "$found"; then
            continue
        fi
        if { [ -e "$bin_dir/$name" ] || [ -L "$bin_dir/$name" ]; } \
            && ! is_molt_py "$bin_dir/$name"; then
            continue
        fi
        printf '%s\n' "$name"
        return
    done
    return 1
}

if [ -z "$BIN_DIR" ]; then
    choice=1
    if [ "$INTERACTIVE" -eq 1 ]; then
        say "Where should the command go?"
        detail "1) $HOME/.local/bin (default)"
        detail "2) /usr/local/bin (may need sudo)"
        detail "3) nowhere; run $INSTALL_DIR/molt.py directly"
        printf '    Choose 1-3 [1]: '
        read -r choice || { printf '\n'; choice=""; }
        choice="${choice:-1}"
    elif [ "$ASSUME_YES" -eq 0 ]; then
        say "no terminal to ask on; using $HOME/.local/bin (choose another with --bin-dir)"
    fi
    case "$choice" in
        1) BIN_DIR="$HOME/.local/bin" ;;
        2) BIN_DIR="/usr/local/bin" ;;
        3) BIN_DIR="" ;;
        *) fatal "Invalid choice '$choice'" "Run install.sh again and choose 1, 2 or 3" ;;
    esac
fi

LINKED=""
if [ -z "$BIN_DIR" ]; then
    say "skip: command (run $INSTALL_DIR/molt.py directly)"
else
    if ! name="$(pick_name "$BIN_DIR")"; then
        fatal "Both 'molt' and 'molt-cli' are other programs on PATH or in $BIN_DIR" \
            "Pick another name with --name NAME"
    fi
    target="$BIN_DIR/$name"

    if [ -z "$CMD_NAME" ] && [ "$name" != "molt" ]; then
        other="$(command -v molt 2>/dev/null || true)"
        [ -n "$other" ] || other="$BIN_DIR/molt"
        say "'molt' is another program ($other); using the name '$name'"
        detail "pass --name NAME to pick another name"
    fi

    # Never replace a file that isn't a molt.py, even with --name
    if { [ -e "$target" ] || [ -L "$target" ]; } && ! is_molt_py "$target"; then
        fatal "$target exists and is not molt.py; not replacing it" \
            "Pick another name with --name NAME, or remove $target"
    fi
    if [ -n "$CMD_NAME" ]; then
        found="$(command -v "$name" 2>/dev/null || true)"
        if [ -n "$found" ] && [ "$found" != "$target" ] && ! is_molt_py "$found"; then
            warn "Another '$name' is on PATH ($found); whichever comes first on PATH runs"
        fi
    fi

    # sudo only when the bin dir (or the nearest existing parent it is created in) isn't ours
    AS_ROOT=""
    existing="$BIN_DIR"
    while [ ! -e "$existing" ]; do
        existing="$(dirname "$existing")"
    done
    if [ ! -w "$existing" ]; then
        AS_ROOT="sudo"
    fi
    if [ -n "$AS_ROOT" ]; then
        detail "run: sudo ln -sfn $INSTALL_DIR/molt.py $target"
    fi
    as_root "$AS_ROOT" mkdir -p "$BIN_DIR" || fatal "Cannot create $BIN_DIR"
    as_root "$AS_ROOT" ln -sfn "$INSTALL_DIR/molt.py" "$target" || fatal "Cannot create $target"
    ok "$target -> $INSTALL_DIR/molt.py"
    LINKED="$name"

    case ":$PATH:" in
        *":$BIN_DIR:"*) ;;
        *)
            note "$BIN_DIR is not on your PATH; add this line to your ~/.zshrc or ~/.bashrc:"
            say "export PATH=\"$BIN_DIR:\$PATH\""
            ;;
    esac
fi

# --- Next steps --- #

run_as="${LINKED:-$INSTALL_DIR/molt.py}"
root_opt=""
if [ "$INSTALL_DIR" != "$DEFAULT_INSTALL_DIR" ] && [ "$INSTALL_DIR" != "$LEGACY_DIR" ]; then
    root_opt=" -r $INSTALL_DIR"
    note "molt looks for ./.molt, then ~/.molt; pass -r to use $INSTALL_DIR"
    say "$run_as capture -r $INSTALL_DIR"
    say "or see 'Custom Installation Directory' in README.md for a shell function"
fi
note "Review $INSTALL_DIR/configs/capture.toml, then try a capture without changes:"
say "$run_as capture$root_opt --dry-run"
note "To list what this machine installs, start from a draft of what it has now:"
say "$run_as install$root_opt --draft draft.toml"

# What the snap install left behind: its snap.py, and command links to it
if [ -f "$INSTALL_DIR/snap.py" ] && is_molt_py "$INSTALL_DIR/snap.py"; then
    note "$INSTALL_DIR/snap.py is the script from before the rename; remove it with:"
    say "rm $INSTALL_DIR/snap.py"
fi
for old in "$HOME/.local/bin/snap" "/usr/local/bin/snap"; do
    stale=0
    if [ -L "$old" ]; then
        case "$(readlink "$old")" in
            "$LEGACY_DIR/snap.py"|"$INSTALL_DIR/snap.py") stale=1 ;;
        esac
    elif [ -f "$old" ] && is_molt_py "$old"; then
        stale=1  # a copy an older install made
    fi
    if [ "$stale" -eq 1 ]; then
        note "$old is the command from before the rename; remove it with:"
        say "rm $old"
    fi
done

printf '\n✓ Install completed in %s\n' "$INSTALL_DIR"
