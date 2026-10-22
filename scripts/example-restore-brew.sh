#!/bin/bash
# Install the Homebrew packages from the snapshot's Brewfile (saved by capture-brew.sh).
# install.sh installs this as scripts/restore-brew.sh; restore.toml runs it as an after
# script ('molt restore --run-scripts'), in the snapshot directory.

set -euo pipefail

BREWFILE="$PWD/Brewfile"

if [[ ! -f "$BREWFILE" ]]; then
    echo "Warning: no Brewfile in $PWD; no Homebrew packages installed" >&2
    exit 0
fi
if ! command -v brew &>/dev/null; then
    echo "Warning: Homebrew not found; install it from https://brew.sh, then run" >&2
    echo "  brew bundle --file=$BREWFILE" >&2
    exit 0
fi

# Install what is missing; never upgrade what is already installed (env passes these
# through sudo, which would otherwise drop them)
BREW=(env HOMEBREW_NO_INSTALL_UPGRADE=1 HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK=1 brew)

# Homebrew refuses to run as root, which is how this runs when the restore re-ran itself
# with sudo; run it as the user who ran sudo instead
if [[ "$(id -u)" -eq 0 ]]; then
    if [[ -z "${SUDO_USER:-}" || "$SUDO_USER" == "root" ]]; then
        echo "Warning: running as root without SUDO_USER; Homebrew can't run as root" >&2
        echo "  Run as your user: brew bundle install --no-upgrade --file=$BREWFILE" >&2
        exit 0
    fi
    BREW=(sudo -u "$SUDO_USER" -H "${BREW[@]}")
fi

echo "Installing Homebrew packages..."

"${BREW[@]}" bundle install --no-upgrade --file="$BREWFILE"

# Remove quarantine attributes on macOS
if [[ "$(uname)" == "Darwin" ]] && [[ -d /opt/homebrew/bin ]]; then
    find /opt/homebrew/bin -type f -exec xattr -d com.apple.quarantine "{}" \; 2>/dev/null || true
fi

echo "✓ Homebrew packages installed"
