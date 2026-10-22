#!/bin/bash
# Save the installed Homebrew packages as a Brewfile.
# install.sh installs this as scripts/capture-brew.sh; capture.toml runs it as an after
# script ('molt capture --run-scripts'), in the new snapshot directory, so the Brewfile
# goes with the snapshot.

set -euo pipefail

if ! command -v brew &>/dev/null; then
    echo "Warning: Homebrew not found; no Brewfile saved" >&2
    exit 0
fi

echo "Saving Homebrew packages..."

# Save to the current directory (the snapshot directory)
brew bundle dump --force --file=Brewfile

echo "✓ Homebrew packages saved to Brewfile"
