#!/usr/bin/env bash
#
# Run molt.py's checks: ruff, shellcheck and pytest. Exits 1 when any of them fails.
#
# Usage: ./check.sh [--fast] [pytest args...]
#   --fast  Skip the slow end-to-end tests (pytest -m "not slow")
#   Other arguments go to pytest, e.g. ./check.sh --fast -k exclude
#
# Settings for ruff and pytest are in pyproject.toml.

set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1

usage() {
    sed -n '3,9s/^# \{0,1\}//p' "${BASH_SOURCE[0]}"
}

fast=0
pytest_args=()
while [ $# -gt 0 ]; do
    case "$1" in
        --fast) fast=1 ;;
        -h|--help) usage; exit 0 ;;
        *) pytest_args+=("$1") ;;
    esac
    shift
done

failed=""

# Run one check; a failure is recorded and the next check still runs
check() {
    local name="$1"
    shift
    printf '\nRunning %s...\n' "$name"
    if "$@"; then
        printf '  ✓ %s passed\n' "$name"
    else
        printf 'Error: %s failed\n' "$name" >&2
        failed="$failed $name"
    fi
}

missing() {
    printf 'Error: %s not found\n  %s\n' "$1" "$2" >&2
    return 1
}

run_ruff() {
    if command -v ruff >/dev/null 2>&1; then
        ruff check molt.py tests/
    elif python3 -m ruff --version >/dev/null 2>&1; then
        python3 -m ruff check molt.py tests/
    else
        missing ruff "Install it with: brew install ruff (or pipx install ruff)"
    fi
}

run_shellcheck() {
    if ! command -v shellcheck >/dev/null 2>&1; then
        missing shellcheck "Install it with: brew install shellcheck (or apt install shellcheck)"
        return
    fi
    shellcheck install.sh check.sh scripts/*.sh
}

run_pytest() {
    if ! python3 -m pytest --version >/dev/null 2>&1; then
        missing pytest "Install it with: python3 -m pip install --user pytest"
        return
    fi
    local marker=()
    if [ "$fast" -eq 1 ]; then
        marker=(-m "not slow")
    fi
    # "${a[@]}" of an empty array fails under set -u in bash < 4.4 (macOS /bin/bash)
    python3 -m pytest -q ${marker[@]+"${marker[@]}"} ${pytest_args[@]+"${pytest_args[@]}"}
}

printf 'Starting checks%s\n' "$([ "$fast" -eq 1 ] && printf ' (fast: no slow tests)')"
check ruff run_ruff
check shellcheck run_shellcheck
check pytest run_pytest

if [ -n "$failed" ]; then
    printf '\nChecks failed:%s\n' "$failed"
    exit 1
fi
printf '\n✓ All checks passed\n'
