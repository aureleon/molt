"""Regression tests for molt.py.

Run with: python3 -m pytest tests/

End-to-end tests run molt.py in a subprocess with HOME pointed at a temp dir and a
fake `sudo` first on PATH, so they never touch the real home or escalate privileges.
"""

import argparse
import errno
import io
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import time
import tomllib

from datetime import datetime, timedelta
from pathlib import Path

import pytest
import tqdm

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import molt  # noqa: E402

MOLT = REPO / "molt.py"


# --- Helpers --- #


def write_files(root, files):
    """Create files under root from a {relative path: content} dict."""
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def make_archive(tmp_path, name, root, paths):
    """Create a gzip category archive with molt.archive_create."""
    outdir = tmp_path / "archives"
    outdir.mkdir(exist_ok=True)
    archive, _, _ = molt.archive_create(name, str(root), paths, outdir)
    return archive


def list_tree(root):
    """List every path under root, relative and sorted."""
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def stat_mode(path):
    """Return a path's permission bits, without following a symlink."""
    return stat.S_IMODE(os.lstat(path).st_mode)


def backup_files(tmp_path, name):
    """Find a file name in any rollback dir next to tmp_path/home."""
    return [p for p in tmp_path.glob("home.bak*/**/*") if p.name == name]


@pytest.fixture(autouse=True)
def reset_copy_failure():
    """Clear the module-level copy-failure flag so tests stay order-independent."""
    yield
    molt._copy_failed.clear()


unprivileged = pytest.mark.skipif(os.geteuid() == 0, reason="needs an unprivileged user")
unprivileged_only = unprivileged


@pytest.fixture
def env(tmp_path):
    """A temp HOME, molt root, and fake sudo for end-to-end runs."""
    home = tmp_path / "home"
    home.mkdir()
    root = tmp_path / "moltroot"
    (root / "configs").mkdir(parents=True)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    sudo = bin_dir / "sudo"
    sudo.write_text('#!/bin/sh\necho "$@" > "$SUDO_MARKER"\nexit 1\n')
    sudo.chmod(0o755)

    # Keep tqdm importable even when it lives in the real user site-packages
    tqdm_path = str(Path(tqdm.__file__).resolve().parent.parent)
    run_env = dict(
        os.environ,
        HOME=str(home),
        PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        PYTHONPATH=tqdm_path,
        SUDO_MARKER=str(tmp_path / "sudo-called"),
    )

    # Never fall through to the real sudo
    if shutil.which("sudo", path=run_env["PATH"]) != str(sudo):
        pytest.skip("fake sudo is not first on PATH")
    try:
        subprocess.run([str(sudo)], env=run_env, check=False)
    except OSError:
        pytest.skip("fake sudo cannot run from the temp dir")
    if not (tmp_path / "sudo-called").exists():
        pytest.skip("fake sudo cannot run from the temp dir")
    (tmp_path / "sudo-called").unlink()

    def run(*args, extra_env=None, answers=None):
        # answers is the text typed at the prompts; without it stdin is empty
        stdin = {"input": answers} if answers is not None else {"stdin": subprocess.DEVNULL}
        return subprocess.run(
            [sys.executable, str(MOLT), *args],
            cwd=tmp_path,
            env=dict(run_env, **(extra_env or {})),
            capture_output=True,
            text=True,
            timeout=120,
            **stdin,
        )

    return argparse.Namespace(
        tmp=tmp_path, home=home, root=root, run=run, sudo=sudo, env=run_env,
        sudo_marker=tmp_path / "sudo-called",
    )


def write_configs(root, categories, rollback=".bak", restore_extra=""):
    """Write capture.toml and an all-archives restore.toml."""
    lines = ["[tarball]", 'compress = "gzip"', 'checksum = "sha256"']
    if rollback:
        lines.append(f'rollback = "{rollback}"')
    for name, (cat_root, dirs) in categories.items():
        dirs_toml = ", ".join(f'"{d}"' for d in dirs)
        lines += ["", f"[tar.{name}]", f'root = "{cat_root}"', f"dirs = [{dirs_toml}]"]
    (root / "configs" / "capture.toml").write_text("\n".join(lines) + "\n")
    restore_toml = '[tar]\narchives = ["*"]\n' + restore_extra
    (root / "configs" / "restore.toml").write_text(restore_toml)


# --- Fix: restore only replaces captured paths --- #


def test_archive_entries_returns_captured_paths(tmp_path):
    write_files(tmp_path / "src", {".config/nvim/init.lua": "x", ".zshrc": "y"})
    archive = make_archive(tmp_path, "cat", tmp_path / "src", [".config/nvim", ".zshrc"])

    with tarfile.open(archive) as tar:
        assert list(molt.archive_entries(tar)) == [".config/nvim", ".zshrc"]


def test_archive_entries_skips_members_below_symlinks(tmp_path):
    archive = tmp_path / "cat.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        link = tarfile.TarInfo(".config")
        link.type = tarfile.SYMTYPE
        link.linkname = "dotfiles/config"
        tar.addfile(link)
        info = tarfile.TarInfo(".config/nvim/init.lua")
        info.size = 3
        tar.addfile(info, io.BytesIO(b"new"))

    with tarfile.open(archive) as tar:
        groups = molt.archive_entries(tar)
    assert {entry: [m.name for m in members] for entry, members in groups.items()} == {
        ".config": [".config"],
    }


@pytest.mark.parametrize("name", ["../outside", "/etc/outside"])
def test_archive_entries_rejects_unsafe_paths(tmp_path, name):
    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo(name)
        info.size = 4
        tar.addfile(info, io.BytesIO(b"evil"))

    with tarfile.open(archive) as tar:
        with pytest.raises(ValueError):
            molt.archive_entries(tar)


def test_restore_keeps_uncaptured_siblings(tmp_path):
    src = tmp_path / "src"
    home = tmp_path / "home"
    write_files(src, {".config/nvim/init.lua": "new", ".config/fish/config.fish": "fish"})
    write_files(home, {
        ".config/gh/hosts.yml": "token",
        ".config/git/config": "git",
        ".config/nvim/old.lua": "old",
    })
    nvim = make_archive(tmp_path, "nvim", src, [".config/nvim"])
    fish = make_archive(tmp_path, "fish", src, [".config/fish"])

    assert molt.restore_category(nvim, str(home), ".bak")
    assert molt.restore_category(fish, str(home), ".bak")

    assert list_tree(home) == [
        ".config",
        ".config/fish",
        ".config/fish/config.fish",
        ".config/gh",
        ".config/gh/hosts.yml",
        ".config/git",
        ".config/git/config",
        ".config/nvim",
        ".config/nvim/init.lua",
    ]
    # The replaced path is backed up, not lost
    assert [p.read_text() for p in backup_files(tmp_path, "old.lua")] == ["old"]


def test_restore_rejects_unsafe_archive_without_deleting(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    write_files(outside, {"keep.txt": "keep"})

    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(outside, arcname="../outside")

    assert not molt.restore_category(archive, str(home), ".bak")
    assert (outside / "keep.txt").read_text() == "keep"
    assert not (tmp_path / "home.bak").exists()


def fail_on_extract(monkeypatch, name, error):
    """Make tar extraction raise `error` for members under the top-level dir `name`."""
    original_extract = tarfile.TarFile.extract
    original_extractall = tarfile.TarFile.extractall

    def check(member):
        member_name = member if isinstance(member, str) else member.name
        if member_name.split("/")[0] == name:
            raise error

    def extract(self, member, *args, **kwargs):
        check(member)
        return original_extract(self, member, *args, **kwargs)

    def extractall(self, path=".", members=None, **kwargs):
        for member in members or []:
            check(member)
        return original_extractall(self, path, members=members, **kwargs)

    monkeypatch.setattr(tarfile.TarFile, "extract", extract)
    monkeypatch.setattr(tarfile.TarFile, "extractall", extractall)


def test_rollback_restores_the_failing_entry(tmp_path, monkeypatch):
    src = tmp_path / "src"
    home = tmp_path / "home"
    write_files(src, {"a/new.txt": "new", "b/new.txt": "new"})
    write_files(home, {"a/orig.txt": "a", "b/orig.txt": "b"})
    archive = make_archive(tmp_path, "cat", src, ["a", "b"])

    fail_on_extract(monkeypatch, "b", OSError("disk full"))

    assert not molt.restore_category(archive, str(home), ".bak")
    assert list_tree(home) == ["a", "a/orig.txt", "b", "b/orig.txt"]


def test_rollback_removes_new_paths_and_created_parents(tmp_path, monkeypatch):
    src = tmp_path / "src"
    home = tmp_path / "home"
    write_files(src, {"a/new.txt": "new", "deep/er/n.txt": "n", "zz/new.txt": "new"})
    write_files(home, {"a/orig.txt": "a"})
    archive = make_archive(tmp_path, "cat", src, ["a", "deep/er/n.txt", "zz"])

    fail_on_extract(monkeypatch, "zz", OSError("disk full"))

    assert not molt.restore_category(archive, str(home), ".bak")
    assert list_tree(home) == ["a", "a/orig.txt"]


def test_incomplete_rollback_stops_restore(tmp_path, monkeypatch, capsys):
    src = tmp_path / "src"
    home = tmp_path / "home"
    write_files(src, {"a/new.txt": "new", "zz/new.txt": "new"})
    write_files(home, {"a/orig.txt": "a"})
    archive = make_archive(tmp_path, "cat", src, ["a", "zz"])

    fail_on_extract(monkeypatch, "zz", OSError("disk full"))
    original_move = molt.move_path

    def move(src_path, dst_path):
        if ".bak" in str(src_path):
            raise OSError("read-only file system")
        return original_move(src_path, dst_path)

    monkeypatch.setattr(molt, "move_path", move)

    with pytest.raises(SystemExit):
        molt.restore_category(archive, str(home), ".bak")
    assert "backups are in" in capsys.readouterr().err
    assert (tmp_path / "home.bak" / "cat" / "a" / "orig.txt").read_text() == "a"


def test_rollback_runs_even_if_reporting_fails(tmp_path, monkeypatch):
    src = tmp_path / "src"
    home = tmp_path / "home"
    write_files(src, {"a/new.txt": "new", "b/new.txt": "new"})
    write_files(home, {"a/orig.txt": "a", "b/orig.txt": "b"})
    archive = make_archive(tmp_path, "cat", src, ["a", "b"])

    fail_on_extract(monkeypatch, "b", OSError("disk full"))

    def broken_stderr(*args, **kwargs):
        raise BrokenPipeError

    monkeypatch.setattr(molt, "error", broken_stderr)

    with pytest.raises(BrokenPipeError):
        molt.restore_category(archive, str(home), ".bak")
    assert list_tree(home) == ["a", "a/orig.txt", "b", "b/orig.txt"]


def test_rollback_runs_on_keyboard_interrupt(tmp_path, monkeypatch):
    src = tmp_path / "src"
    home = tmp_path / "home"
    write_files(src, {"a/new.txt": "new"})
    write_files(home, {"a/orig.txt": "a"})
    archive = make_archive(tmp_path, "cat", src, ["a"])

    fail_on_extract(monkeypatch, "a", KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        molt.restore_category(archive, str(home), ".bak")
    assert list_tree(home) == ["a", "a/orig.txt"]


@unprivileged_only
def test_rollback_removes_read_only_dirs_from_archive(tmp_path, monkeypatch):
    src = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    write_files(src, {"a/ro/f.txt": "new", "zz/new.txt": "new"})
    (src / "a" / "ro").chmod(0o555)
    archive = make_archive(tmp_path, "cat", src, ["a", "zz"])
    (src / "a" / "ro").chmod(0o755)

    fail_on_extract(monkeypatch, "zz", OSError("disk full"))

    assert not molt.restore_category(archive, str(home), ".bak")
    assert list_tree(home) == []


def test_cross_group_hardlink_links_to_restored_file(tmp_path):
    src = tmp_path / "src"
    home = tmp_path / "home"
    write_files(src, {"a/x": "new", "b/z": "z"})
    (src / "b" / "y").hardlink_to(src / "a" / "x")
    write_files(home, {"a/x": "old", "b/y": "old"})
    archive = make_archive(tmp_path, "cat", src, ["b/z", "a", "b"])

    assert molt.restore_category(archive, str(home), ".bak")

    assert (home / "a" / "x").read_text() == "new"
    assert (home / "b" / "y").read_text() == "new"
    assert (home / "a" / "x").samefile(home / "b" / "y")


def record_chown(monkeypatch):
    """Pretend to be root and record chown calls instead of making them."""
    calls = {}

    def chown(path, uid, gid, *, follow_symlinks=True):
        calls[Path(path)] = (uid, gid, follow_symlinks)

    monkeypatch.setattr(molt.os, "geteuid", lambda: 0)
    monkeypatch.setattr(molt.os, "chown", chown)
    return calls


def test_new_dirs_get_nearest_ancestor_owner_as_root(tmp_path, monkeypatch):
    src = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    write_files(src, {"fonts/a.ttf": "font"})
    archive = make_archive(tmp_path, "local", src, ["fonts"])
    calls = record_chown(monkeypatch)
    home_owner = (home.stat().st_uid, home.stat().st_gid, True)

    assert molt.restore_category(archive, str(home / ".local" / "share"), ".bak")

    for new_dir in [".local", ".local/share", ".local/share.bak"]:
        assert calls[home / new_dir] == home_owner


def test_copy_with_ownership_never_follows_links_as_root(tmp_path, monkeypatch):
    src = tmp_path / "src"
    write_files(src, {"dir/f.txt": "x"})
    (src / "dir" / "link").symlink_to("/etc/hosts")
    calls = record_chown(monkeypatch)

    molt.copy_with_ownership(src / "dir", tmp_path / "copy")

    assert set(calls) == {tmp_path / "copy", tmp_path / "copy/f.txt", tmp_path / "copy/link"}
    assert all(follow is False for _, _, follow in calls.values())


def test_restore_over_symlinked_dir_keeps_link_target(tmp_path):
    src = tmp_path / "src"
    home = tmp_path / "home"
    dotfiles = tmp_path / "dotfiles"
    write_files(src, {".config/nvim/init.lua": "new"})
    write_files(dotfiles, {"nvim/init.lua": "stow"})
    (home / ".config").mkdir(parents=True)
    (home / ".config" / "nvim").symlink_to(dotfiles / "nvim")
    archive = make_archive(tmp_path, "nvim", src, [".config/nvim"])

    assert molt.restore_category(archive, str(home), ".bak")

    restored = home / ".config" / "nvim"
    assert not restored.is_symlink()
    assert (restored / "init.lua").read_text() == "new"
    assert (dotfiles / "nvim" / "init.lua").read_text() == "stow"
    assert (tmp_path / "home.bak" / "nvim" / ".config" / "nvim").is_symlink()


def test_repeated_restores_rotate_backups_without_collision(tmp_path, monkeypatch):
    src = tmp_path / "src"
    home = tmp_path / "home"
    write_files(src, {"a.txt": "new"})
    write_files(home, {"a.txt": "old"})
    archive = make_archive(tmp_path, "cat", src, ["a.txt"])

    # Freeze the clock so every rotation gets the same timestamp
    frozen = molt.datetime(2026, 1, 2, 3, 4, 5)
    monkeypatch.setattr(molt, "datetime", type("D", (), {"now": staticmethod(lambda: frozen)}))

    for _ in range(3):
        assert molt.restore_category(archive, str(home), ".bak")

    backups = [p.read_text() for p in sorted(tmp_path.glob("home.bak*/cat/a.txt"))]
    assert sorted(backups) == ["new", "new", "old"]


def test_e2e_restore_keeps_files_created_after_capture(env):
    write_files(env.home, {".config/nvim/init.lua": "v1", ".config/gh/hosts.yml": "token"})
    write_configs(env.root, {"nvim": ("$HOME", [".config/nvim"])})

    result = env.run("capture", "-r", str(env.root))
    assert result.returncode == 0, result.stderr

    write_files(env.home, {".config/nvim/init.lua": "edited", ".config/git/config": "git"})
    result = env.run("restore", "-r", str(env.root))
    assert result.returncode == 0, result.stdout + result.stderr

    assert (env.home / ".config/nvim/init.lua").read_text() == "v1"
    assert (env.home / ".config/gh/hosts.yml").read_text() == "token"
    assert (env.home / ".config/git/config").read_text() == "git"
    assert not env.sudo_marker.exists()


# --- Fix: dry-run never extracts --- #


def test_dry_run_archive_extract_changes_nothing(tmp_path, monkeypatch):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    write_files(src, {"a.txt": "new"})
    write_files(dst, {"a.txt": "old"})
    archive = make_archive(tmp_path, "cat", src, ["a.txt"])

    monkeypatch.setattr(molt, "__dry_run__", True)
    assert molt.archive_extract(archive, root=str(dst))
    assert molt.extract_archives([archive], root_map={"cat": str(dst)}) == 1

    assert (dst / "a.txt").read_text() == "old"


def test_extract_archives_skips_members_below_symlinks(tmp_path):
    dst = tmp_path / "dst"
    dotfiles = dst / "dotfiles"
    write_files(dotfiles, {"nvim/init.lua": "user edit"})
    (dst / ".config").symlink_to("dotfiles")

    archive = tmp_path / "cat.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        link = tarfile.TarInfo(".config")
        link.type = tarfile.SYMTYPE
        link.linkname = "dotfiles"
        tar.addfile(link)
        info = tarfile.TarInfo(".config/nvim/init.lua")
        info.size = 3
        tar.addfile(info, io.BytesIO(b"new"))

    assert molt.archive_extract(archive, root=str(dst))
    assert (dotfiles / "nvim" / "init.lua").read_text() == "user edit"


@pytest.mark.parametrize("flags", [[], ["--disable-rollback"]])
def test_e2e_dry_run_restore_changes_nothing(env, flags):
    write_files(env.home, {".zshrc": "v1"})
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"])})
    assert env.run("capture", "-r", str(env.root)).returncode == 0

    write_files(env.home, {".zshrc": "edited"})
    result = env.run("restore", "-r", str(env.root), "--dry-run", *flags)
    assert result.returncode == 0, result.stdout + result.stderr

    assert (env.home / ".zshrc").read_text() == "edited"
    assert list_tree(env.tmp / "home") == [".zshrc"]
    assert not list(env.tmp.glob("home.bak*"))


# --- Output problems stop a run where print() and tqdm always did --- #


def test_e2e_restore_to_closed_stdout_pipe_changes_nothing(env):
    write_files(env.home, {".zshrc": "v1"})
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"])})
    assert env.run("capture", "-r", str(env.root)).returncode == 0
    write_files(env.home, {".zshrc": "edited"})

    # The pipe's reader is gone before molt.py writes anything
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    try:
        result = subprocess.run(
            [sys.executable, str(MOLT), "restore", "-r", str(env.root)],
            cwd=env.tmp, env=env.env, stdin=subprocess.DEVNULL, stdout=write_fd,
            stderr=subprocess.PIPE, text=True, timeout=120,
        )
    finally:
        os.close(write_fd)

    # The flush at the first bar fails, before any file changes
    assert result.returncode == 120, result.stderr
    assert (env.home / ".zshrc").read_text() == "edited"
    assert not list(env.tmp.glob("home.bak*"))


def test_e2e_restore_with_latin1_stdout_changes_nothing(env):
    write_files(env.home, {".zshrc": "v1"})
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"])})
    assert env.run("capture", "-r", str(env.root)).returncode == 0
    write_files(env.home, {".zshrc": "edited"})

    # '✓ 1 archive verified' cannot be encoded, before any file changes
    result = env.run("restore", "-r", str(env.root), extra_env={"PYTHONIOENCODING": "latin-1"})
    assert result.returncode == 1, result.stdout + result.stderr
    assert (env.home / ".zshrc").read_text() == "edited"
    assert not list(env.tmp.glob("home.bak*"))


# --- Fix: sudo only when needed, with the user's HOME --- #


def test_path_writable_checks_nearest_existing_parent(tmp_path):
    assert molt.path_writable(tmp_path / "missing" / "deeper")
    if os.geteuid() != 0:
        locked = tmp_path / "locked"
        locked.mkdir(mode=0o555)
        assert not molt.path_writable(locked / "missing")


@unprivileged
def test_path_writable_below_unsearchable_dir(tmp_path):
    locked = tmp_path / "locked"
    (locked / "sub").mkdir(parents=True)
    locked.chmod(0)
    try:
        assert not molt.path_writable(locked / "sub" / "missing")
    finally:
        locked.chmod(0o755)


def needs_sudo(tmp_path, root, paths, roll_ext=".bak", snapshot_config=None):
    """Capture paths under root and check if restoring them back needs sudo."""
    archive = make_archive(tmp_path, "cat", root, paths)
    return molt.restore_needs_sudo([archive], {"cat": str(root)}, roll_ext, snapshot_config or {})


def other_backup_device(monkeypatch):
    """Make every backup dir look like it is on another filesystem than its root."""
    real_device = molt.path_device
    monkeypatch.setattr(molt, "path_device", lambda path: real_device(path) + 1)


@unprivileged
def test_needs_sudo_for_locked_dir_inside_captured_path(tmp_path, monkeypatch):
    home = tmp_path / "home"
    write_files(home, {"fonts/a.txt": "a", "fonts/locked/b.txt": "b"})
    (home / "fonts" / "locked").chmod(0o555)
    try:
        # Moving fonts aside only needs write on home and on fonts itself
        assert not needs_sudo(tmp_path, home, ["fonts"])
        # Merging without rollback only rewrites files, which stay writable
        assert not needs_sudo(tmp_path, home, ["fonts"], roll_ext=None)
        # A backup dir on another filesystem means a copy and a delete, which can't
        # empty the locked dir
        other_backup_device(monkeypatch)
        assert needs_sudo(tmp_path, home, ["fonts"])
    finally:
        (home / "fonts" / "locked").chmod(0o755)


@unprivileged
def test_needs_sudo_to_move_a_locked_dir(tmp_path):
    home = tmp_path / "home"
    write_files(home, {"fonts/a.txt": "a"})
    (home / "fonts").chmod(0o555)
    try:
        # A dir that moves to another parent needs write on itself (for its '..')
        assert needs_sudo(tmp_path, home, ["fonts"])
    finally:
        (home / "fonts").chmod(0o755)


def test_needs_sudo_in_a_sticky_dir_for_others_paths(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    write_files(shared, {"a.txt": "a"})
    shared.chmod(0o1777)
    try:
        assert not needs_sudo(tmp_path, shared, ["a.txt"])
        # In a sticky dir only the owner of a path (or of the dir) can rename it
        monkeypatch.setattr(molt.os, "geteuid", lambda: os.getuid() + 1)
        assert needs_sudo(tmp_path, shared, ["a.txt"])
    finally:
        shared.chmod(0o755)


@unprivileged
def test_needs_sudo_for_unwritable_backup_parent(tmp_path):
    parent = tmp_path / "opt"
    write_files(parent / "app", {"a.txt": "a"})
    parent.chmod(0o555)
    try:
        assert needs_sudo(tmp_path, parent / "app", ["a.txt"])
        assert not needs_sudo(tmp_path, parent / "app", ["a.txt"], roll_ext=None)
    finally:
        parent.chmod(0o755)


@unprivileged
def test_needs_sudo_for_unwritable_link_parent(tmp_path):
    home = tmp_path / "home"
    locked = tmp_path / "locked"
    write_files(home, {"a.txt": "a"})
    locked.mkdir(mode=0o555)
    config = {"tar": {"cat": {"root": str(home), "link": str(locked / "link")}}}
    try:
        assert needs_sudo(tmp_path, home, ["a.txt"], snapshot_config=config)
    finally:
        locked.chmod(0o755)


@unprivileged
def test_needs_sudo_for_unreadable_file_inside_captured_path(tmp_path, monkeypatch):
    home = tmp_path / "home"
    write_files(home, {"gcloud/config": "a", "gcloud/credentials.db": "secret"})
    archive = make_archive(tmp_path, "cat", home, ["gcloud"])
    (home / "gcloud" / "credentials.db").chmod(0)
    try:
        # A rename never reads the file
        assert not molt.restore_needs_sudo([archive], {"cat": str(home)}, ".bak", {})
        # A copy to a backup dir on another filesystem does
        other_backup_device(monkeypatch)
        assert molt.restore_needs_sudo([archive], {"cat": str(home)}, ".bak", {})
    finally:
        (home / "gcloud" / "credentials.db").chmod(0o644)


@unprivileged
def test_needs_sudo_for_unreadable_archive(tmp_path):
    home = tmp_path / "home"
    write_files(home, {"a.txt": "a"})
    archive = make_archive(tmp_path, "cat", home, ["a.txt"])
    archive.chmod(0)
    try:
        assert molt.restore_needs_sudo([archive], {"cat": str(home)}, ".bak", {})
    finally:
        archive.chmod(0o644)


def test_no_sudo_for_writable_home(tmp_path):
    home = tmp_path / "home"
    write_files(home, {".config/nvim/init.lua": "x"})
    assert not needs_sudo(tmp_path, home, [".config/nvim"])


def test_sudo_restore_runs_child_on_local_copy(tmp_path, monkeypatch):
    calls = []

    class FakePopen:
        def __init__(self, cmd):
            self.waits = 0
            config_path = cmd[cmd.index("-t") + 1]
            calls.append((cmd, Path(config_path).read_text(), config_path))

        def wait(self):
            # The first Ctrl-C reaches the parent too; it must keep waiting
            self.waits += 1
            if self.waits == 1:
                raise KeyboardInterrupt
            assert Path(calls[0][2]).exists()
            return 3

    monkeypatch.setattr(molt.subprocess, "Popen", FakePopen)
    monkeypatch.setenv("HOME", "/Users/someone")
    monkeypatch.setenv("XDG_DATA_HOME", "/Users/someone/.local/share")
    args = argparse.Namespace(root_host=None, disable_rollback=True, run_scripts=True)
    restore_config = {
        "tarball": {"rollback": ".bak"},
        "tar": {"archives": ["dots*", "🚀\x7f"]},
        "scripts": {"after": ["s.sh"], "notes": {"a": 1}},
    }
    snapshot_config = {"tar": {
        "data": {"root": "${XDG_DATA_HOME}/app"},
        "dots": {"root": "$HOME", "link": "$HOME/link"},
    }}

    with pytest.raises(SystemExit) as exit_info:
        molt.sudo_restore(args, tmp_path / "root", tmp_path / "cap", restore_config,
                          snapshot_config)

    assert exit_info.value.code == 3
    [(cmd, config_text, config_path)] = calls
    assert cmd == [
        "sudo", "env",
        "HOME=/Users/someone",
        "XDG_DATA_HOME=/Users/someone/.local/share",
        sys.executable, "-s", str(molt.__script__), "restore",
        "--from", str(tmp_path / "cap"), "-r", str(tmp_path / "root"), "-t", config_path,
        "--disable-rollback", "--run-scripts",
    ]
    assert molt.tomllib.loads(config_text) == {
        "tar": {"archives": ["dots*", "🚀\x7f"]},
        "scripts": {"after": ["s.sh"]},
    }
    assert not Path(config_path).exists()


@pytest.mark.parametrize("archives", [None, []])
def test_sudo_restore_keeps_skip_restore_semantics(tmp_path, monkeypatch, archives):
    configs = []

    class FakePopen:
        def __init__(self, cmd):
            configs.append(Path(cmd[cmd.index("-t") + 1]).read_text())

        def wait(self):
            return -2

    monkeypatch.setattr(molt.subprocess, "Popen", FakePopen)
    args = argparse.Namespace(root_host=None)

    with pytest.raises(SystemExit) as exit_info:
        molt.sudo_restore(args, tmp_path, tmp_path, {"tar": {"archives": archives}}, {})

    assert exit_info.value.code == 130  # killed by SIGINT, like a shell reports it
    assert molt.tomllib.loads(configs[0]) == {"tar": {"archives": []}}


SITECUSTOMIZE = """
import os
if os.environ.get("MOLT_TEST_FAKE_ROOT"):
    os.geteuid = lambda: 0
elif os.environ.get("MOLT_TEST_FORCE_SUDO"):
    real_access = os.access

    def access(path, mode, *args, **kwargs):
        if "force-sudo" in str(path) and mode & os.W_OK:
            return False
        return real_access(path, mode, *args, **kwargs)

    os.access = access
"""


def test_e2e_sudo_child_restores_from_local_copy(env):
    root = env.tmp / "force-sudo-root"
    write_files(root, {"f.txt": "v1"})
    write_configs(env.root, {"system": (str(root), ["f.txt"])})
    assert env.run("capture", "-r", str(env.root)).returncode == 0
    write_files(root, {"f.txt": "edited"})

    # The parent sees the root as unwritable; the "sudo" child pretends to be root
    site = env.tmp / "site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(SITECUSTOMIZE)
    env.sudo.write_text(
        '#!/bin/sh\necho "$@" > "$SUDO_MARKER"\n'
        "export MOLT_TEST_FAKE_ROOT=1\nunset MOLT_TEST_FORCE_SUDO\nexec \"$@\"\n"
    )
    tmpdir = env.tmp / "tmpdir"
    tmpdir.mkdir()

    result = env.run("restore", "-r", str(env.root), extra_env={
        "MOLT_TEST_FORCE_SUDO": "1",
        "PYTHONPATH": f"{site}{os.pathsep}{env.env['PYTHONPATH']}",
        "TMPDIR": str(tmpdir),
    })

    assert result.returncode == 0, result.stdout + result.stderr
    assert env.sudo_marker.exists()
    assert (root / "f.txt").read_text() == "v1"
    assert list(tmpdir.iterdir()) == []


@unprivileged
def test_e2e_sudo_only_for_unwritable_roots(env):
    locked = env.tmp / "locked"
    write_files(locked, {"f.txt": "v1"})
    locked.chmod(0o555)
    write_configs(env.root, {"system": (str(locked), ["f.txt"])})
    assert env.run("capture", "-r", str(env.root)).returncode == 0

    # Dry-run reports the need for sudo but never runs it
    result = env.run("restore", "-r", str(env.root), "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "would re-run with sudo" in result.stdout
    assert not env.sudo_marker.exists()

    # A real run escalates, keeping the invoking user's HOME
    env.run("restore", "-r", str(env.root))
    sudo_args = env.sudo_marker.read_text().split()
    assert sudo_args[:2] == ["env", f"HOME={env.home}"]
    assert sudo_args[sudo_args.index("restore") + 1] == "--from"
    locked.chmod(0o755)


@unprivileged
def test_e2e_locked_dir_is_moved_to_backup_without_sudo(env):
    fonts = env.home / ".local/share/fonts"
    write_files(fonts, {"a.txt": "v1", "locked/b.txt": "b"})
    (fonts / "locked").chmod(0o555)
    write_configs(env.root, {"local": ("$HOME/.local/share", ["fonts"])})
    assert env.run("capture", "-r", str(env.root)).returncode == 0
    (fonts / "locked").chmod(0o755)
    write_files(fonts, {"a.txt": "edited"})
    (fonts / "locked").chmod(0o555)

    result = env.run("restore", "-r", str(env.root))

    # The move needs no read or write access inside fonts, so no sudo
    assert result.returncode == 0, result.stdout + result.stderr
    assert not env.sudo_marker.exists()
    assert (fonts / "a.txt").read_text() == "v1"
    backup = env.home / ".local/share.bak/local/fonts"
    assert (backup / "a.txt").read_text() == "edited"
    assert stat_mode(backup / "locked") == 0o555
    for path in (fonts / "locked", backup / "locked"):
        path.chmod(0o755)


def test_e2e_no_sudo_for_unselected_locked_category(env):
    locked = env.tmp / "locked"
    write_files(locked, {"f.txt": "v1"})
    write_files(env.home, {".zshrc": "v1"})
    write_configs(env.root, {
        "system": (str(locked), ["f.txt"]),
        "dotfiles": ("$HOME", [".zshrc"]),
    })
    assert env.run("capture", "-r", str(env.root)).returncode == 0
    (env.root / "configs" / "restore.toml").write_text('[tar]\narchives = ["dotfiles"]\n')
    locked.chmod(0o555)

    result = env.run("restore", "-r", str(env.root))

    locked.chmod(0o755)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not env.sudo_marker.exists()


@unprivileged
def test_e2e_dry_run_on_unsearchable_root_does_not_crash(env):
    prot = env.tmp / "prot"
    write_files(prot, {"sub/f.txt": "v1"})
    write_configs(env.root, {"priv": (str(prot), ["sub"])})
    assert env.run("capture", "-r", str(env.root)).returncode == 0
    prot.chmod(0)

    result = env.run("restore", "-r", str(env.root), "--dry-run")

    prot.chmod(0o755)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "would re-run with sudo" in result.stdout
    assert not env.sudo_marker.exists()


def test_e2e_dry_run_does_not_run_scripts(env):
    marker = env.tmp / "script-ran"
    (env.root / "scripts").mkdir()
    (env.root / "scripts" / "touch.sh").write_text(f"touch {marker}\n")
    write_files(env.home, {".zshrc": "v1"})
    write_configs(
        env.root,
        {"dotfiles": ("$HOME", [".zshrc"])},
        restore_extra='\n[scripts]\nbefore = ["scripts/touch.sh"]\nafter = ["scripts/touch.sh"]\n',
    )
    assert env.run("capture", "-r", str(env.root)).returncode == 0

    result = env.run("restore", "-r", str(env.root), "--dry-run", "--run-scripts")

    assert result.returncode == 0, result.stdout + result.stderr
    assert not marker.exists()


# --- Fix: user@host without a colon is a remote host --- #


@pytest.mark.parametrize("value, expected", [
    (None, (None, None)),
    ("user@host", ("user@host", None)),
    ("user@host:", ("user@host", None)),
    ("host:", ("host", None)),
    ("host:/srv/molt", ("host", Path("/srv/molt"))),
    ("user@host:~/molt", ("user@host", Path("~/molt"))),
    ("./name@tag", (None, Path("./name@tag"))),
    ("/abs/path", (None, Path("/abs/path"))),
    ("relative/path", (None, Path("relative/path"))),
])
def test_parse_remote_arg(value, expected):
    assert molt.parse_remote_arg(value) == expected


@pytest.mark.parametrize("value", [
    "-oProxyCommand=evil:",
    "user@-oProxyCommand=evil:/path",
    "user@-x",
    "@-x:p",
    "@host:",
])
def test_parse_remote_arg_rejects_bad_hosts(value):
    with pytest.raises(SystemExit):
        molt.parse_remote_arg(value)


@pytest.mark.parametrize("root, expected_host, expected_root", [
    ("user@host", "user@host", ".molt"),
    ("host:", "host", ".molt"),
    ("host:~", "host", "."),
    ("host:~/molt", "host", "molt"),
    ("host:/srv/molt", "host", "/srv/molt"),
])
def test_main_remote_molt_root(monkeypatch, root, expected_host, expected_root):
    # B6: a remote molt root is kept as (host, path), and args.root is a local copy of its
    # configs and scripts that main() removes at exit
    seen, fetched = [], []
    monkeypatch.setattr(molt, "__dry_run__", False)
    monkeypatch.setattr(molt, "__verbose__", False)
    monkeypatch.setattr(molt, "root_fetch", lambda *a: fetched.append(a))
    monkeypatch.setattr(molt, "cmd_capture", lambda args: seen.append(
        (args.root_host, str(args.root_path), args.root, args.root.is_dir())
    ))
    monkeypatch.setattr(molt.sys, "argv", ["molt", "capture", "-r", root, "--dry-run"])

    molt.main()

    [(host, path, local_copy, existed)] = seen
    assert (host, path) == (expected_host, expected_root)
    assert fetched == [(expected_host, molt.PurePosixPath(expected_root), local_copy)]
    assert existed and not local_copy.exists()


def test_restore_to_user_at_host_deploys_remotely(tmp_path, monkeypatch):
    capture_dir = tmp_path / "snapshot"
    capture_dir.mkdir()
    (capture_dir / "snapshot.toml").write_text("")
    calls = []
    monkeypatch.setattr(molt, "remote_deploy", lambda *a, **k: calls.append("remote"))
    monkeypatch.setattr(molt, "restore", lambda *a, **k: calls.append("local"))

    args = argparse.Namespace(
        root=tmp_path, root_host=None, src=str(capture_dir), dst="user@target.host"
    )
    molt.cmd_restore(args)

    assert calls == ["remote"]


@pytest.mark.parametrize("args", [
    ["migrate", "--from", "source.host", "--to", "user@target.host"],
    ["migrate", "--to", "./dir"],
    ["migrate", "--from", "user@source.host", "--to", ""],
    ["capture", "--from", "source.host"],
    ["restore", "--to", ""],
])
def test_e2e_non_host_from_or_to_is_rejected(env, args):
    result = env.run(*args, "-r", str(env.root), "--dry-run")

    assert result.returncode == 1
    assert "is not a remote host" in result.stderr


def test_e2e_restore_to_non_host_is_rejected(env):
    write_files(env.home, {".zshrc": "v1"})
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"])})
    assert env.run("capture", "-r", str(env.root)).returncode == 0
    write_files(env.home, {".zshrc": "edited"})

    result = env.run("restore", "-r", str(env.root), "--to", "target.host")

    assert result.returncode == 1
    assert "is not a remote host" in result.stderr
    assert (env.home / ".zshrc").read_text() == "edited"


# --- Output: display code never crashes, and each failure prints once --- #


NO_SUCH_USER_ROOT = "~molt-test-no-such-user/root"


def unexpandable_root():
    """Return a root that expand_path() cannot expand (an unknown user's home)."""
    try:
        molt.expand_path(NO_SUCH_USER_ROOT)
    except RuntimeError:
        return NO_SUCH_USER_ROOT
    pytest.skip("the test user name exists on this machine")


def test_create_archives_failure_shows_unexpandable_root(tmp_path, capsys):
    root = unexpandable_root()
    outdir = tmp_path / "out"
    outdir.mkdir()

    assert molt.create_archives([("cat", root, [".zshrc"], outdir)]) == []

    captured = capsys.readouterr()
    assert "Error: Cannot create cat.tar.gz: " in captured.err
    assert f"  cat.tar.gz (root: {root})\n    failed (see the error above)\n" in captured.out
    assert list(outdir.iterdir()) == []


def test_archive_confirm_shows_unexpandable_root(tmp_path, monkeypatch, capsys):
    root = unexpandable_root()
    write_files(tmp_path / "src", {".zshrc": "x"})
    archive = make_archive(tmp_path, "cat", tmp_path / "src", [".zshrc"])
    monkeypatch.setattr(molt, "__dry_run__", True)

    # The header shows the root as given; the extraction reports the real error
    assert molt.extract_archives([archive], root_map={"cat": root}) == 0

    captured = capsys.readouterr()
    assert f"cat.tar.gz (root: {root})" in captured.out
    assert "add: .zshrc" in captured.out
    assert "Error: Cannot extract cat.tar.gz: " in captured.err


def test_archive_confirm_prompt_matches_the_targets(tmp_path, monkeypatch, capsys):
    write_files(tmp_path / "src", {".zshrc": "x", ".vimrc": "y"})
    archive = make_archive(tmp_path, "cat", tmp_path / "src", [".zshrc", ".vimrc"])
    empty = make_archive(tmp_path, "empty", tmp_path / "src", [".missing"])
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(molt, "__dry_run__", True)

    # Every entry is new: no overwrite warning, and paths relative to the root
    assert molt.archive_confirm(archive, root=str(home))
    out = capsys.readouterr().out
    assert "     add: .vimrc\n" in out
    assert "     add: .zshrc\n" in out
    assert "Restore cat.tar.gz? [y/N]: y (assumed in a dry run)" in out
    assert "Existing files" not in out

    # One entry exists
    write_files(home, {".zshrc": "old"})
    assert molt.archive_confirm(archive, root=str(home))
    out = capsys.readouterr().out
    assert "     replace: .zshrc\n" in out
    assert "     add: .vimrc\n" in out
    assert "Restore cat.tar.gz? Existing files will be overwritten [y/N]: " in out

    # No entries at all
    assert molt.archive_confirm(empty, root=str(home))
    assert "Restore empty.tar.gz? It has no files [y/N]: " in capsys.readouterr().out


def test_rsync_parallel_reports_only_the_first_failed_copy(tmp_path, monkeypatch, capsys):
    def failing_rsync(cmd, **kwargs):
        raise subprocess.CalledProcessError(23, cmd, output="", stderr=f"rsync: bad {cmd[-2]}\n")

    monkeypatch.setattr(molt.subprocess, "run", failing_rsync)
    monkeypatch.setattr(molt, "__dry_run__", False)
    transfers = [(str(tmp_path / f"f{i}"), "host:/tmp/work/", f"f{i}") for i in range(4)]

    with pytest.raises(SystemExit) as exit_info:
        molt.rsync_parallel(transfers)

    assert exit_info.value.code == 1
    err = capsys.readouterr().err
    assert err.count("Error: ") == 1
    assert err.count("rsync: bad ") == 1

    # The next group of copies reports its own failure again
    with pytest.raises(SystemExit):
        molt.rsync_parallel(transfers[:1])
    assert capsys.readouterr().err.count("Error: Copying f0 failed (rsync exit status 23)") == 1


def test_run_scripts_skips_the_step_when_every_script_is_missing(tmp_path, capsys):
    (tmp_path / "scripts").mkdir()
    config = {"scripts": {"before": ["scripts/a.sh", "scripts/b.sh"]}}

    molt.run_scripts(tmp_path, config, "before", working_dir=tmp_path)

    captured = capsys.readouterr()
    assert "Skipping before scripts (no scripts found)" in captured.out
    assert "Running" not in captured.out
    assert captured.err.count("Warning: Script scripts/") == 2


def test_run_scripts_names_the_migration_phase(tmp_path, monkeypatch, capsys):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "a.sh").write_text("true\n")
    config = {"scripts": {"before": ["scripts/a.sh"]}}
    monkeypatch.setattr(molt, "__dry_run__", True)

    molt.run_scripts(tmp_path, config, "before", working_dir=tmp_path, phase="capture")
    molt.run_scripts(tmp_path, config, "after", working_dir=tmp_path, phase="restore")

    out = capsys.readouterr().out
    assert "Running capture before scripts..." in out
    assert "Skipping restore after scripts (none configured)" in out


def test_sudo_restore_exit_code_survives_a_broken_stderr(tmp_path, monkeypatch):
    class FakePopen:
        def __init__(self, cmd):
            pass

        def wait(self):
            # The child ran; now the parent's stderr is gone
            broken = io.StringIO()
            broken.close()
            monkeypatch.setattr(molt.sys, "stderr", broken)
            return 3

    monkeypatch.setattr(molt.subprocess, "Popen", FakePopen)

    with pytest.raises(SystemExit) as exit_info:
        molt.sudo_restore(argparse.Namespace(root_host=None), tmp_path, tmp_path, {}, {})

    assert exit_info.value.code == 3


def test_stdout_is_flushed_before_stderr_only_when_they_share(tmp_path, monkeypatch):
    with open(tmp_path / "out", "w") as out, open(tmp_path / "err", "w") as err:
        monkeypatch.setattr(molt.sys, "stdout", out)
        assert not molt.shares_stdout(err)
        with open(tmp_path / "out", "a") as same:
            assert molt.shares_stdout(same)


# --- Fix A1: failures exit non-zero --- #


def fake_remote_tools(env):
    """Put ssh and rsync stand-ins first on PATH that record each call and fail."""
    calls = env.tmp / "remote-calls"
    for tool in ("ssh", "rsync"):
        script = env.tmp / "bin" / tool
        script.write_text(f'#!/bin/sh\necho "{tool} $*" >> "{calls}"\nexit 255\n')
        script.chmod(0o755)
    return calls


def snapshot_dir(env):
    """Return the one snapshot under the molt root's captures directory."""
    [snapshot] = env.root.glob("captures/*/*/*")
    return snapshot


def test_e2e_capture_fails_when_an_archive_cannot_be_created(env):
    bad_root = unexpandable_root()
    write_files(env.home, {".zshrc": "v1"})
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"]), "broken": (bad_root, [".x"])})
    tmpdir = env.tmp / "tmpdir"
    tmpdir.mkdir()

    result = env.run("capture", "-r", str(env.root), extra_env={"TMPDIR": str(tmpdir)})

    assert result.returncode == 1, result.stdout + result.stderr
    assert "Error: Cannot create broken.tar.gz: " in result.stderr
    assert result.stderr.rstrip().endswith(
        "Error: Capture failed: 1 of 2 archives could not be created; no snapshot was saved"
    )
    assert "Snapshot saved" not in result.stdout
    assert not list(env.root.glob("captures/*/*/*"))
    assert list(tmpdir.iterdir()) == []  # the staging dir is gone

    # A dry run stops at the same point, in the present tense, and ends like any dry run
    result = env.run("capture", "-r", str(env.root), "--dry-run")
    assert result.returncode == 0
    assert "Capture would fail: 1 of 2 archives cannot be created" in result.stderr
    assert result.stdout.rstrip().endswith(
        "Dry run completed with 2 errors; no changes were made"
    )
    assert "Writing snapshot.toml" not in result.stdout


def test_e2e_migrate_capture_failure_stops_before_the_host(env):
    bad_root = unexpandable_root()
    calls = fake_remote_tools(env)
    write_files(env.home, {".zshrc": "v1"})
    (env.root / "configs" / "migrate.toml").write_text(
        '[tarball]\nchecksum = "sha256"\n\n'
        '[capture.tar.dotfiles]\nroot = "$HOME"\nfiles = [".zshrc"]\n\n'
        f'[capture.tar.broken]\nroot = "{bad_root}"\nfiles = [".x"]\n\n'
        '[restore.tar]\narchives = ["*"]\n'
    )
    tmpdir = env.tmp / "tmpdir"
    tmpdir.mkdir()

    result = env.run(
        "migrate", "-r", str(env.root), "--to", "user@target.host",
        extra_env={"TMPDIR": str(tmpdir)},
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "Capture failed: 1 of 2 archives could not be created" in result.stderr
    assert not calls.exists()  # nothing was sent to the host
    assert list(tmpdir.iterdir()) == []


def capture_without_backups(env):
    """Capture a dotfiles archive and a system archive whose restore has no backups."""
    system = env.tmp / "sysroot"
    write_files(env.home, {".zshrc": "v1"})
    write_files(system, {"etc/app": "v1"})
    write_configs(
        env.root,
        {"dotfiles": ("$HOME", [".zshrc"]), "system": (str(system), ["etc"])},
        rollback="",
    )
    assert env.run("capture", "-r", str(env.root)).returncode == 0
    return system


def test_e2e_restore_without_backups_stops_on_an_extraction_error(env):
    system = capture_without_backups(env)
    write_files(env.home, {".zshrc": "edited"})
    (system / "etc" / "app").unlink()
    (system / "etc" / "app" / "sub").mkdir(parents=True)  # a file can't replace a dir

    result = env.run("restore", "-r", str(env.root), "--run-scripts", answers="y\ny\n")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "Error: Cannot extract system.tar.gz: " in result.stderr
    assert "  1 of 2 archives extracted" in result.stdout
    assert (
        "Error: 1 of 2 archives failed to restore: system.tar.gz\n"
        "  restored: dotfiles.tar.gz\n"
        "  not run: symlinks, after scripts\n"
    ) in result.stderr
    assert "Restore completed" not in result.stdout
    assert (env.home / ".zshrc").read_text() == "v1"


def test_e2e_restore_without_backups_stops_on_an_unreadable_archive(env):
    capture_without_backups(env)
    write_files(env.home, {".zshrc": "edited"})

    # Replace the archive with junk that still matches its checksum
    snapshot = snapshot_dir(env)
    archive = snapshot / "system.tar.gz"
    old_digest = molt.sha256_file_digest(archive)
    archive.write_bytes(b"not a tar archive")
    toml_path = snapshot / "snapshot.toml"
    toml_path.write_text(
        toml_path.read_text().replace(old_digest, molt.sha256_file_digest(archive))
    )

    result = env.run("restore", "-r", str(env.root), answers="y\n")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "Error: Cannot restore system.tar.gz: " in result.stderr
    assert "Error: 1 of 2 archives failed to restore: system.tar.gz" in result.stderr
    assert (env.home / ".zshrc").read_text() == "v1"


def test_e2e_declined_prompts_are_not_failures(env):
    capture_without_backups(env)
    write_files(env.home, {".zshrc": "edited"})

    for answers in ("n\nn\n", None):  # declined, then no answer at all
        result = env.run("restore", "-r", str(env.root), answers=answers)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Restore completed: no archives restored" in result.stdout
    assert (env.home / ".zshrc").read_text() == "edited"


def test_archive_confirm_tells_a_bad_archive_from_a_declined_one(tmp_path, monkeypatch):
    write_files(tmp_path / "src", {".zshrc": "x"})
    good = make_archive(tmp_path, "good", tmp_path / "src", [".zshrc"])
    bad = tmp_path / "archives" / "bad.tar.gz"
    bad.write_bytes(b"junk")
    monkeypatch.setattr("builtins.input", lambda prompt: "n")

    assert molt.archive_confirm(bad, root=str(tmp_path)) is None
    assert molt.archive_confirm(good, root=str(tmp_path)) is False

    restored, failed = [], []
    assert molt.extract_archives(
        [good, bad], root_map={"good": str(tmp_path), "bad": str(tmp_path)},
        restored=restored, failed=failed,
    ) == 0
    assert (restored, failed) == ([], [bad])


# --- Fix A2: symlinks only for the archives restored --- #


def capture_linked_archives(env):
    """Capture two archives whose snapshot.toml tables each set a 'link'."""
    write_files(env.home, {".zshrc": "v1", "work/notes.txt": "n"})
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"]), "work": ("$HOME/work", ["."])})
    capture_toml = env.root / "configs" / "capture.toml"
    text = capture_toml.read_text()
    text = text.replace('[tar.dotfiles]\n', '[tar.dotfiles]\nlink = "$HOME/dotfiles-link"\n')
    text = text.replace('[tar.work]\n', '[tar.work]\nlink = "$HOME/work-link"\n')
    capture_toml.write_text(text)
    assert env.run("capture", "-r", str(env.root)).returncode == 0


def test_e2e_symlinks_only_for_selected_archives(env):
    capture_linked_archives(env)
    (env.root / "configs" / "restore.toml").write_text('[tar]\narchives = ["dotfiles"]\n')

    result = env.run("restore", "-r", str(env.root), "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "dotfiles-link ->" in result.stdout
    assert "work-link" not in result.stdout

    result = env.run("restore", "-r", str(env.root))
    assert result.returncode == 0, result.stdout + result.stderr
    assert (env.home / "dotfiles-link").is_symlink()
    assert not os.path.lexists(env.home / "work-link")
    assert "1 archive restored, 1 symlink created" in result.stdout


def test_e2e_no_symlinks_for_declined_or_unselected_archives(env):
    capture_linked_archives(env)

    # Declined at the prompt: no link
    result = env.run("restore", "-r", str(env.root), "--disable-rollback", answers="y\nn\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (env.home / "dotfiles-link").is_symlink()
    assert not os.path.lexists(env.home / "work-link")

    # No archive selected: no links at all
    (env.home / "dotfiles-link").unlink()
    (env.root / "configs" / "restore.toml").write_text("[tar]\narchives = []\n")
    result = env.run("restore", "-r", str(env.root))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Creating symlinks" not in result.stdout
    assert not os.path.lexists(env.home / "dotfiles-link")


@unprivileged
def test_no_sudo_for_links_of_unselected_archives(tmp_path):
    home = tmp_path / "home"
    locked = tmp_path / "locked"
    write_files(home, {"a.txt": "a"})
    locked.mkdir(mode=0o555)
    config = {"tar": {
        "cat": {"root": str(home)},
        "other": {"root": str(home), "link": str(locked / "link")},
    }}
    try:
        assert not needs_sudo(tmp_path, home, ["a.txt"], snapshot_config=config)
    finally:
        locked.chmod(0o755)


# --- Fix A3: one backup rotation per root per run, one subdirectory per archive --- #


def test_restore_category_backs_up_archives_sharing_a_root(tmp_path):
    src = tmp_path / "src"
    home = tmp_path / "home"
    write_files(src, {"a.txt": "new", "b.txt": "new"})
    write_files(home, {"a.txt": "old a", "b.txt": "old b"})
    write_files(tmp_path / "home.bak", {"earlier.txt": "earlier run"})
    first = make_archive(tmp_path, "first", src, ["a.txt"])
    second = make_archive(tmp_path, "second", src, ["b.txt"])

    rotated = set()
    assert molt.restore_category(first, str(home), ".bak", rotated=rotated)
    assert molt.restore_category(second, str(home), ".bak", rotated=rotated)

    # One rotation for the root; each archive has its own subdirectory
    [previous] = tmp_path.glob("home.bak_*")
    assert list_tree(previous) == ["earlier.txt"]
    assert list_tree(tmp_path / "home.bak") == ["first", "first/a.txt", "second", "second/b.txt"]
    assert (tmp_path / "home.bak" / "first" / "a.txt").read_text() == "old a"
    assert (tmp_path / "home.bak" / "second" / "b.txt").read_text() == "old b"


def test_backup_subdirectories_get_the_ancestor_owner_as_root(tmp_path, monkeypatch):
    src = tmp_path / "src"
    home = tmp_path / "home"
    home.mkdir()
    write_files(src, {"fonts/a.ttf": "font"})
    archive = make_archive(tmp_path, "local", src, ["fonts"])
    calls = record_chown(monkeypatch)
    home_owner = (home.stat().st_uid, home.stat().st_gid, True)

    assert molt.restore_category(archive, str(home / ".local" / "share"), ".bak")

    assert calls[home / ".local/share.bak/local"] == home_owner


def test_e2e_archives_sharing_a_root_keep_their_backups(env):
    write_files(env.home, {".zshrc": "v1", ".vimrc": "v1"})
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"]), "vim": ("$HOME", [".vimrc"])})
    assert env.run("capture", "-r", str(env.root)).returncode == 0
    backups = env.tmp / "home.bak"

    write_files(env.home, {".zshrc": "edit 1", ".vimrc": "edit 1"})
    result = env.run("restore", "-r", str(env.root))
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"dotfiles.tar.gz (root: {env.home}, backup: {backups}/dotfiles)" in result.stdout
    assert f"vim.tar.gz (root: {env.home}, backup: {backups}/vim)" in result.stdout
    assert (backups / "dotfiles" / ".zshrc").read_text() == "edit 1"
    assert (backups / "vim" / ".vimrc").read_text() == "edit 1"

    # The next run moves the whole backup directory aside once, with both archives in it
    write_files(env.home, {".zshrc": "edit 2", ".vimrc": "edit 2"})
    result = env.run("restore", "-r", str(env.root), "--verbose")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("rotate: ") == 1
    [previous] = env.tmp.glob("home.bak_*")
    assert (previous / "dotfiles" / ".zshrc").read_text() == "edit 1"
    assert (previous / "vim" / ".vimrc").read_text() == "edit 1"
    assert (backups / "dotfiles" / ".zshrc").read_text() == "edit 2"
    assert (backups / "vim" / ".vimrc").read_text() == "edit 2"


# --- Fix A4: restore only the archives snapshot.toml lists --- #


def test_archive_select_matches_only_available_archives(tmp_path):
    available = [tmp_path / "dotfiles.tar.gz", tmp_path / "ssh-keys.tar.gz"]
    (tmp_path / "unlisted.tar.gz").write_bytes(b"")

    assert molt.archive_select(available, []) == (available, [])
    assert molt.archive_select(available, ["unlisted", "ssh*", "dotfiles", "ssh-keys"]) == (
        [available[1], available[0]],
        ["unlisted"],
    )


def test_snapshot_archives_lists_existing_tables_only(tmp_path):
    snapshot = tmp_path / "snapshot"
    for rel in ("dotfiles.tar.gz", "unlisted.tar.gz", "sub/x.tar.gz", "../outside.tar.gz"):
        (snapshot / rel).parent.mkdir(parents=True, exist_ok=True)
        (snapshot / rel).write_bytes(b"")
    config = {"tar": {"dotfiles": {}, "missing": {}, "sub/x": {}, "../outside": {}}}

    assert molt.snapshot_archives(snapshot, config, ".tar.gz") == [snapshot / "dotfiles.tar.gz"]


def test_archive_category_strips_only_the_tar_extension():
    assert molt.archive_category(Path("dotfiles.tar.gz")) == "dotfiles"
    assert molt.archive_category(Path("my.tar.files.tar.xz")) == "my.tar.files"
    assert molt.archive_category(Path("plain.tar")) == "plain"


@pytest.mark.parametrize("archives", ['["*"]', '["evil", "dotfiles"]'])
def test_e2e_restore_ignores_archives_not_in_snapshot_toml(env, archives):
    write_files(env.home, {".zshrc": "v1"})
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"])}, rollback="")
    assert env.run("capture", "-r", str(env.root)).returncode == 0

    # An archive dropped into the snapshot dir, listed nowhere, with no root
    evil_src = env.tmp / "evil-src"
    write_files(evil_src, {"evil.txt": "pwned"})
    with tarfile.open(snapshot_dir(env) / "evil.tar.gz", "w:gz") as tar:
        tar.add(evil_src / "evil.txt", arcname="evil.txt")
    (env.root / "configs" / "restore.toml").write_text(f"[tar]\narchives = {archives}\n")

    result = env.run("restore", "-r", str(env.root), "--dry-run")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "evil.tar.gz" not in result.stdout
    assert "Restoring 1 archive without backups..." in result.stdout
    if "evil" in archives:
        assert "archives pattern 'evil' matches no archive in the snapshot" in result.stderr


# --- Fix A5: capture -t uses that config for the archives too --- #


def test_e2e_capture_uses_the_config_given_with_t(env):
    write_files(env.home, {".zshrc": "v1", ".other": "o"})
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"])})
    (env.root / "scripts").mkdir()
    (env.root / "scripts" / "after.sh").write_text('echo "$PWD" > "$AFTER_MARKER"\n')
    (env.root / "configs" / "other.toml").write_text(
        '[tarball]\nchecksum = "sha256"\n\n'
        '[tar.other]\nroot = "$HOME"\nfiles = [".other"]\n\n'
        '[scripts]\nafter = ["scripts/after.sh"]\n'
    )
    marker = env.tmp / "after-ran"

    result = env.run(
        "capture", "-r", str(env.root), "-t", "configs/other.toml", "--run-scripts",
        extra_env={"AFTER_MARKER": str(marker)},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    snapshot = snapshot_dir(env)
    assert sorted(p.name for p in snapshot.iterdir()) == ["other.tar.gz", "snapshot.toml"]
    # The after script ran in the staged snapshot, before it moved to captures/
    assert Path(marker.read_text().strip()).name == snapshot.name


def test_capture_after_scripts_use_the_capture_config(tmp_path, monkeypatch):
    # A migration's capture config comes from migrate.toml's [capture.*] tables, not a file
    ran = []
    monkeypatch.setattr(molt, "__dry_run__", True)
    monkeypatch.setattr(molt, "run_scripts", lambda root, config, when, **kw: ran.append(
        (when, config.get("scripts"))
    ))
    config = {"tar": {}, "scripts": {"after": ["scripts/dump.sh"]}}
    args = argparse.Namespace(run_scripts=True, config_toml="configs/migrate.toml")

    molt.capture(args, tmp_path, tmp_path / "out", config)

    assert ran == [("before", config["scripts"]), ("after", config["scripts"])]


# --- Fix A6: TOML files are written with toml_dumps --- #


def test_toml_dumps_writes_nested_tables():
    config = {
        "tarball": {"compress": "xz", "checksum": "sha256"},
        "tar": {
            "ssh-keys": {"root": "$HOME", "dirs": [".ssh"]},
            "dot.files": {"root": 'C:\\Users\\"me"', "link": "~/l\x7f🚀"},
            "empty": {},
        },
        "scripts": {"after": ["scripts/a.sh"], "count": 2, "on": True},
    }

    text = molt.toml_dumps(config)

    assert '[tar."ssh-keys"]\n' in text
    assert '[tar."dot.files"]\n' in text
    assert "[tar.empty]\n" in text
    assert "\n[tar]\n" not in text  # a table holding only tables needs no header
    assert molt.tomllib.loads(text) == config
    assert molt.toml_dumps({}) == ""


def old_snapshot_toml(compress, roll_ext, tables):
    """The snapshot.toml text generate_snapshot_toml built by hand before toml_dumps."""
    lines = ["#", "# Capture Configuration TOML", "#", "[tarball]"]
    if compress != "gzip":
        lines += [f'compress = "{compress}"  # tar compression type']
    if roll_ext:
        lines += [f'rollback = "{roll_ext}"  # rollback intermediate extension']
    lines += ['checksum = "sha256"  # checksum digest type', ""]
    for name, (root, link, checksum) in sorted(tables.items()):
        lines += [f"[tar.{name}]", f'root = "{root}"']
        if link:
            lines += [f'link = "{link}"']
        lines += [f'checksum = "{checksum}"', ""]
    return "\n".join(lines)


@pytest.mark.parametrize("compress, roll_ext", [("gzip", ".bak"), ("xz", None), ("", "~")])
def test_snapshot_toml_reads_like_the_old_format(tmp_path, compress, roll_ext):
    src = tmp_path / "src"
    write_files(src, {".zshrc": "z", ".ssh/id": "k"})
    ext, _ = molt.COMPRESS_MAP[compress]
    outdir = tmp_path / "out"
    outdir.mkdir()
    for name, paths in (("dotfiles", [".zshrc"]), ("ssh-keys", [".ssh"])):
        molt.archive_create(name, str(src), paths, outdir, compress)
    meta = {"dotfiles": {"root": "$HOME", "link": None},
            "ssh-keys": {"root": str(src), "link": "$HOME/keys"}}

    toml_path, checksum = molt.generate_snapshot_toml(outdir, compress, meta, roll_ext)

    digests = {name: molt.sha256_file_digest(outdir / f"{name}{ext}")
               for name in ("dotfiles", "ssh-keys")}
    old = old_snapshot_toml(compress, roll_ext, {
        "dotfiles": ("$HOME", None, digests["dotfiles"]),
        "ssh-keys": (str(src), "$HOME/keys", digests["ssh-keys"]),
    })
    with open(toml_path, "rb") as f:
        assert molt.tomllib.load(f) == molt.tomllib.loads(old)
    # The snapshot id is still the digest of the archive digests, in name order
    assert checksum == molt.sha256_digest(
        (digests["dotfiles"] + digests["ssh-keys"]).encode("utf-8")
    )


def test_write_capture_toml_keeps_every_value(tmp_path):
    config = {
        "tarball": {"compress": "gzip", "rollback": ".bak"},
        "tar": {"ssh-keys": {"root": 'C:\\"quoted"', "dirs": [".ssh"], "files": [], "n": 3}},
        "scripts": {"after": ["scripts/dump.sh"]},
    }

    path = molt.write_capture_toml(config)
    try:
        with open(path, "rb") as f:
            assert molt.tomllib.load(f) == config
    finally:
        path.unlink()


# --- Fix A7: tqdm is optional --- #


def test_dtqdm_without_tqdm_is_a_no_op_bar(monkeypatch, capsys):
    monkeypatch.setattr(molt, "tqdm", None)
    monkeypatch.setattr(molt, "__dry_run__", False)

    with molt.dtqdm(2, "Creating archives", autorefresh=True) as pbar:
        pbar.update(1)
        pbar.write("hello")
    molt.say("after the bar")

    assert capsys.readouterr().out == "hello\n  after the bar\n"


def test_e2e_capture_and_sudo_restore_run_without_tqdm(env):
    no_tqdm = env.tmp / "no-tqdm"
    no_tqdm.mkdir()
    (no_tqdm / "tqdm.py").write_text('raise ImportError("tqdm is not installed")\n')
    root = env.tmp / "force-sudo-root"
    write_files(root, {"f.txt": "v1"})
    write_configs(env.root, {"system": (str(root), ["f.txt"])})
    no_tqdm_env = {"PYTHONPATH": str(no_tqdm)}

    result = env.run("capture", "-r", str(env.root), extra_env=no_tqdm_env)
    assert result.returncode == 0, result.stdout + result.stderr
    write_files(root, {"f.txt": "edited"})

    # The sudo child also runs without tqdm
    site = env.tmp / "site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(SITECUSTOMIZE)
    env.sudo.write_text(
        '#!/bin/sh\necho "$@" > "$SUDO_MARKER"\n'
        "export MOLT_TEST_FAKE_ROOT=1\nunset MOLT_TEST_FORCE_SUDO\nexec \"$@\"\n"
    )
    result = env.run("restore", "-r", str(env.root), extra_env={
        "MOLT_TEST_FORCE_SUDO": "1",
        "PYTHONPATH": f"{site}{os.pathsep}{no_tqdm}",
    })

    assert result.returncode == 0, result.stdout + result.stderr
    assert env.sudo_marker.exists()
    assert "Starting restore as root" in result.stdout
    assert (root / "f.txt").read_text() == "v1"


# --- Fix A8: no rsync --info=progress2 under --verbose --- #


def test_rsync_run_verbose_adds_no_progress_option(tmp_path, monkeypatch):
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(molt.subprocess, "run", fake_run)
    monkeypatch.setattr(molt, "__dry_run__", False)
    monkeypatch.setattr(molt, "__verbose__", True)

    molt.rsync_run(str(tmp_path / "f"), "host:/tmp/work/")

    [cmd] = commands
    assert cmd[:2] == ["rsync", "-az"]
    assert not any(arg.startswith("--info") for arg in cmd)


# --- Remote hosts, emulated on this machine --- #

# ssh and rsync stand-ins. Each host (the part after 'user@') is a pair of local dirs,
# <hosts>/<host>/home and <hosts>/<host>/tmp; nothing ever leaves this machine
FAKE_HOST_TOOLS = r'''
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

HOSTS = Path(os.environ["FAKE_HOSTS"])


def log(line):
    with open(HOSTS / "calls.log", "a") as f:
        f.write(line.replace("\n", "\\n") + "\n")


def host_dirs(host):
    name = host.rpartition("@")[2]
    home, tmp = HOSTS / name / "home", HOSTS / name / "tmp"
    home.mkdir(parents=True, exist_ok=True)
    tmp.mkdir(parents=True, exist_ok=True)
    return name, home, tmp


def refused(name):
    if name in os.environ.get("FAKE_HOSTS_DOWN", "").split():
        print(f"ssh: connect to host {name} port 22: Connection refused", file=sys.stderr)
        return True
    return False


def ssh(args):
    # ssh [-o option]... [-t] host command...: bash -c runs the command in the host's home
    while args and args[0].startswith("-"):
        if args.pop(0) == "-o":
            args.pop(0)
    host, command = args[0], " ".join(args[1:])
    log(f"ssh {host} {command}")
    name, home, tmp = host_dirs(host)
    if refused(name):
        return 255
    fail = os.environ.get("FAKE_SSH_FAIL")
    if fail and fail in command:
        print(f"fake ssh: '{fail}' fails on {name}", file=sys.stderr)
        return int(os.environ.get("FAKE_SSH_FAIL_STATUS", "1"))
    env = dict(os.environ, HOME=str(home), TMPDIR=str(tmp), FAKE_HOST=name)
    env["PATH"] = os.pathsep.join([str(HOSTS / "bin"), env["PATH"]])
    return subprocess.run(["bash", "-c", command], cwd=home, env=env).returncode


def rsync(args):
    # host:path becomes the host's home + path (relative) or the path itself (absolute)
    log("rsync " + shlex.join(args))
    mapped = []
    for arg in args:
        match = None if arg.startswith("-") else re.fullmatch(r"([^/:]+):(.*)", arg, re.S)
        if match:
            name, home, _ = host_dirs(match.group(1))
            if refused(name):
                return 255
            path = match.group(2)
            arg = path if path.startswith("/") else os.path.join(home, path)
        elif not arg.startswith("-") and ("::" in arg or arg.startswith("rsync:")):
            print(f"fake rsync: refusing '{arg}'", file=sys.stderr)
            return 1
        mapped.append(arg)
    # --rsh=false: the real rsync can never open a connection of its own
    real = os.environ["FAKE_REAL_RSYNC"]
    return subprocess.run([real, "--rsh=false", *mapped]).returncode


if __name__ == "__main__":
    tool, *args = sys.argv[1:]
    sys.exit(ssh(args) if tool == "ssh" else rsync(args))
'''

# A host's sudo: logs the call, then runs the command as this user, which molt.py takes
# for root (SITECUSTOMIZE). A nested sudo fails, so a restore can never loop
FAKE_HOST_SUDO = """\
echo "sudo $FAKE_HOST $*" >> "$FAKE_HOSTS/calls.log"
if [ -n "$FAKE_SUDO_ACTIVE" ]; then
    echo "fake sudo: nested sudo" >&2
    exit 1
fi
export FAKE_SUDO_ACTIVE=1 MOLT_TEST_FAKE_ROOT=1
export PYTHONPATH="$FAKE_HOSTS/site${PYTHONPATH:+:$PYTHONPATH}"
exec "$@"
"""


def write_script(path, body):
    """Write an executable /bin/sh script."""
    path.write_text("#!/bin/sh\n" + body.rstrip("\n") + "\n")
    path.chmod(0o755)


@pytest.fixture
def hosts(env):
    """Emulate remote hosts on this machine, with fake ssh and rsync first on PATH.

    ssh runs its command with bash -c in <tmp>/hosts/<host>/home, with HOME there,
    TMPDIR=<tmp>/hosts/<host>/tmp, and python3 (the test's interpreter) and a fake sudo
    first on PATH. rsync maps host:path to that home (a relative path) or to the path
    itself (an absolute one), then runs the real rsync locally. Each ssh, rsync and host
    sudo call is logged. With FAKE_SSH_FAIL=<text>, ssh fails (exit 1) for commands that
    contain the text; FAKE_HOSTS_DOWN lists hosts that refuse connections (exit 255).
    """
    real_rsync = shutil.which("rsync")  # looked up before the fakes are on PATH
    if not real_rsync or not shutil.which("bash"):
        pytest.skip("rsync and bash are needed to emulate remote hosts")

    base = env.tmp / "hosts"
    (base / "bin").mkdir(parents=True)
    (base / "site").mkdir()
    (base / "site" / "sitecustomize.py").write_text(SITECUSTOMIZE)
    (base / "fakehost.py").write_text(FAKE_HOST_TOOLS)
    for tool in ("ssh", "rsync"):
        write_script(
            env.tmp / "bin" / tool, f'exec "{sys.executable}" "{base / "fakehost.py"}" {tool} "$@"'
        )
    write_script(base / "bin" / "python3", f'exec "{sys.executable}" "$@"')
    write_script(base / "bin" / "sudo", FAKE_HOST_SUDO)
    env.env.update(FAKE_HOSTS=str(base), FAKE_REAL_RSYNC=real_rsync)

    # Never fall through to the real ssh or rsync
    for tool in ("ssh", "rsync"):
        if shutil.which(tool, path=env.env["PATH"]) != str(env.tmp / "bin" / tool):
            pytest.skip(f"fake {tool} is not first on PATH")

    log = base / "calls.log"

    def calls():
        """Return the logged calls, one 'ssh <host> <command>' or 'rsync <args>' each."""
        return log.read_text().splitlines() if log.exists() else []

    def ssh_commands(host):
        """Return the commands ssh ran on host, in order."""
        prefix = f"ssh {host} "
        return [line[len(prefix):] for line in calls() if line.startswith(prefix)]

    def remote_args():
        """Return every path or command a host was given: ssh commands, rsync host:paths."""
        found = []
        for line in calls():
            tool, _, rest = line.partition(" ")
            if tool == "ssh":
                found.append(rest.partition(" ")[2])
            elif tool == "rsync":
                found += [arg for arg in shlex.split(rest) if re.match(r"[^/:-][^/:]*:", arg)]
        return found

    def clear():
        if log.exists():
            log.unlink()

    return argparse.Namespace(
        base=base,
        home=lambda host: base / host / "home",
        tmp=lambda host: base / host / "tmp",
        calls=calls,
        ssh_commands=ssh_commands,
        remote_args=remote_args,
        clear=clear,
    )


def remote_molt_root(hosts, host, categories, restore_toml='[tar]\narchives = ["*"]\n'):
    """Give an emulated host a .molt in its home with capture.toml and restore.toml."""
    root = hosts.home(host) / ".molt"
    (root / "configs").mkdir(parents=True)
    write_configs(root, categories)
    (root / "configs" / "restore.toml").write_text(restore_toml)
    return root


def work_dir_of(commands, purpose):
    """Return the work directory the 'mkdir -p' after mktemp created, from ssh commands."""
    [mkdir] = [c for c in commands if c.startswith("mkdir -p ") and f"/molt-{purpose}." in c]
    first = shlex.split(mkdir)[2]
    return first.rsplit("/", 1)[0]


# --- Fix B1: remote paths are relative to the host's login directory --- #


@pytest.mark.parametrize("path, expected", [
    ("~", "."),
    ("~/", "."),
    ("~/snaps", "snaps"),
    ("~/snaps/2026/", "snaps/2026"),
    (".molt/captures", ".molt/captures"),
    ("/srv/molt", "/srv/molt"),
    ("~other/x", "~other/x"),
    (Path("~/molt"), "molt"),
])
def test_normalize_remote_path(path, expected):
    assert str(molt.normalize_remote_path(path)) == expected


def test_remote_molt_root_is_the_r_path_only_on_its_host():
    args = argparse.Namespace(root_host="user@web-01", root_path=molt.PurePosixPath("/srv/x"))
    assert str(molt.remote_molt_root(args, "user@web-01")) == "/srv/x"
    assert str(molt.remote_molt_root(args, "user@web-02")) == ".molt"
    assert str(molt.remote_molt_root(argparse.Namespace(), "web-01")) == ".molt"


def test_e2e_remote_paths_are_relative_to_the_login_directory(env, hosts):
    write_files(env.home, {".zshrc": "v1"})
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"])})
    remote = hosts.home("web-01")

    # host:~/path is relative to the login directory
    result = env.run("capture", "-r", str(env.root), "--to", "user@web-01:~/snaps")
    assert result.returncode == 0, result.stdout + result.stderr
    [snapshot] = remote.glob("snaps/*")
    assert (snapshot / "dotfiles.tar.gz").is_file()
    assert "  To:   user@web-01:snaps\n" in result.stdout
    assert f"Snapshot saved to user@web-01:snaps/{snapshot.name}\n" in result.stdout

    # A host with no path gets .molt/captures/YYYY/MM-DD there, never the local root
    result = env.run("capture", "-r", str(env.root), "--to", "user@web-01")
    assert result.returncode == 0, result.stdout + result.stderr
    [default] = remote.glob(".molt/captures/*/*/*")
    assert (default / "snapshot.toml").is_file()
    assert f"To:   user@web-01:{default.parent.relative_to(remote)}\n" in result.stdout

    # restore --from host:~/path pulls from the same place
    write_files(env.home, {".zshrc": "edited"})
    result = env.run(
        "restore", "-r", str(env.root), "--from", f"user@web-01:~/snaps/{snapshot.name}"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"  From: user@web-01:snaps/{snapshot.name}\n" in result.stdout
    assert (env.home / ".zshrc").read_text() == "v1"

    # No host was given a local path or a '~' to expand
    given = hosts.remote_args()
    assert given
    for arg in given:
        assert str(env.root) not in arg and str(env.home) not in arg and "~" not in arg


# --- Fix B2: work directories come from mktemp and are always removed --- #


def fake_ssh_runs(monkeypatch, outputs=None):
    """Replace subprocess.run: record (argv, timeout) and answer mktemp like a host."""
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs.get("timeout")))
        command = cmd[-1]
        stdout = ""
        for text, output in (outputs or {}).items():
            if text in command:
                stdout = output
        if command.startswith("mktemp -d"):
            purpose = re.search(r"molt-(\w+)\.X", command).group(1)
            stdout = stdout or f"/tmp/molt-{purpose}.Ab3dEf9h\n"
        return subprocess.CompletedProcess(cmd, 0, stdout, "")

    monkeypatch.setattr(molt.subprocess, "run", run)
    monkeypatch.setattr(molt, "__dry_run__", False)
    monkeypatch.setattr(molt, "__verbose__", False)
    return calls


def test_remote_workdir_uses_mktemp_and_one_mkdir(monkeypatch, capsys):
    calls = fake_ssh_runs(monkeypatch)

    work_dir = molt.remote_workdir("user@web-01", "restore", ["configs", "scripts", "snapshot"])

    assert str(work_dir) == "/tmp/molt-restore.Ab3dEf9h"
    assert [cmd[-2:] for cmd, _ in calls] == [
        ["user@web-01", 'mktemp -d "${TMPDIR:-/tmp}/molt-restore.XXXXXXXX"'],
        ["user@web-01", "mkdir -p /tmp/molt-restore.Ab3dEf9h/configs "
                        "/tmp/molt-restore.Ab3dEf9h/scripts "
                        "/tmp/molt-restore.Ab3dEf9h/snapshot"],
    ]
    assert capsys.readouterr().out == "\nCreating work directory on user@web-01...\n"


@pytest.mark.parametrize("output", ["", "/tmp/other-dir\n", "molt-restore.x\n"])
def test_remote_workdir_never_uses_an_odd_mktemp_path(monkeypatch, capsys, output):
    calls = fake_ssh_runs(monkeypatch, {"mktemp": output or "\n"})

    with pytest.raises(SystemExit):
        molt.remote_workdir("user@web-01", "restore", ["configs"])

    # Nothing but mktemp ran: no mkdir, and no rm -rf of a path mktemp didn't make
    assert [cmd[-1].split()[0] for cmd, _ in calls] == ["mktemp"]
    assert "Cannot create a work directory on user@web-01" in capsys.readouterr().err


def test_remote_workdir_is_removed_when_its_layout_fails(monkeypatch):
    calls = fake_ssh_runs(monkeypatch)
    real_run = molt.subprocess.run

    def run(cmd, **kwargs):
        if cmd[-1].startswith("mkdir"):
            calls.append((cmd, kwargs.get("timeout")))
            raise subprocess.CalledProcessError(1, cmd, "", "mkdir: No space left\n")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(molt.subprocess, "run", run)

    with pytest.raises(SystemExit):
        molt.remote_workdir("user@web-01", "capture", ["configs"])
    assert calls[-1][0][-1] == "rm -rf /tmp/molt-capture.Ab3dEf9h"


def test_remote_capture_removes_its_work_dir_on_ctrl_c(tmp_path, monkeypatch, capsys):
    (tmp_path / "configs").mkdir()
    write_configs(tmp_path, {"dotfiles": ("$HOME", [".zshrc"])})
    calls = fake_ssh_runs(monkeypatch)

    def interrupted(transfers):
        raise KeyboardInterrupt

    monkeypatch.setattr(molt, "rsync_parallel", interrupted)
    args = argparse.Namespace(config_toml=None, run_scripts=False)

    with pytest.raises(KeyboardInterrupt):
        molt.remote_capture(args, tmp_path, "user@web-01")

    # molt.py still running there is stopped first, so it can't recreate files
    assert calls[-1][0][-1] == (
        "pkill -INT -f 'molt[.]py capture -r /tmp/molt-capture\\.Ab3dEf9h( |$)' && sleep 2; "
        "rm -rf /tmp/molt-capture.Ab3dEf9h"
    )
    # The cleanup after a failure prints nothing of its own
    assert "Removing work directory" not in capsys.readouterr().out


@pytest.mark.parametrize("command, purpose, fail", [
    (["capture", "--from", "user@web-01"], "capture", "molt.py capture"),
    (["restore", "--to", "user@web-01"], "restore", "molt.py restore"),
    (["migrate", "--to", "user@web-01"], "restore", "molt.py restore"),
])
def test_e2e_remote_work_dir_is_removed_when_the_remote_step_fails(
    env, hosts, command, purpose, fail
):
    write_files(env.home, {".zshrc": "v1"})
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"])})
    (env.root / "configs" / "migrate.toml").write_text(
        '[tarball]\nchecksum = "sha256"\n\n'
        '[capture.tar.dotfiles]\nroot = "$HOME"\nfiles = [".zshrc"]\n\n'
        '[restore.tar]\narchives = ["*"]\n'
    )
    if command[0] == "restore":
        assert env.run("capture", "-r", str(env.root)).returncode == 0
    tmpdir = env.tmp / "tmpdir"
    tmpdir.mkdir()

    result = env.run(
        *command, "-r", str(env.root),
        extra_env={"FAKE_SSH_FAIL": fail, "TMPDIR": str(tmpdir)},
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert f"fake ssh: '{fail}' fails on web-01" in result.stderr
    # The run still ends with its own Error block
    assert result.stderr.rstrip().splitlines()[-1].startswith("Error: ")
    assert "Removing work directory" not in result.stdout

    # mktemp made a private work dir on the host; it is gone, and so are local temp dirs
    commands = hosts.ssh_commands("user@web-01")
    assert commands[0] == f'mktemp -d "${{TMPDIR:-/tmp}}/molt-{purpose}.XXXXXXXX"'
    work_dir = work_dir_of(commands, purpose)
    assert Path(work_dir).parent == hosts.tmp("web-01")
    if purpose == "capture":
        stop = molt.remote_stop_command(work_dir, "capture")
        assert commands[-1] == f"{stop}; rm -rf {work_dir}"
    else:
        assert commands[-1] == f"rm -rf {work_dir}"
    assert list(hosts.tmp("web-01").iterdir()) == []
    assert list(tmpdir.iterdir()) == []


# --- Fix B5: the latest remote snapshot has the newest snapshot.toml --- #


def test_e2e_latest_remote_snapshot_has_the_newest_snapshot_toml(env, hosts):
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"])})
    snapshots = {}
    for content in ("older", "newer"):
        write_files(env.home, {".zshrc": content})
        result = env.run("capture", "-r", str(env.root), "--to", str(env.tmp / content))
        assert result.returncode == 0, result.stdout + result.stderr
        [snapshots[content]] = (env.tmp / content).iterdir()

    # The newer snapshot sits in the older-looking date dir; stray entries are newer still
    captures = hosts.home("web-01") / ".molt" / "captures"
    newer = captures / "2025" / "01-01" / snapshots["newer"].name
    older = captures / "2026" / "12-31" / snapshots["older"].name
    shutil.copytree(snapshots["newer"], newer)
    shutil.copytree(snapshots["older"], older)
    write_files(captures, {
        "2026/12-31/stray/notes.txt": "not a snapshot",
        "notes/12-31/abc1234/snapshot.toml": "not a date dir",
        "2026/1231/abc1234/snapshot.toml": "not a date dir",
        "2026/12-31/snapshot.toml": "not in a snapshot dir",
    })
    now = molt.datetime.now().timestamp()
    os.utime(older / "snapshot.toml", (now - 3600, now - 3600))
    os.utime(newer / "snapshot.toml", (now - 60, now - 60))
    write_files(env.home, {".zshrc": "edited"})

    # A dry run looks too (a read-only query), and shows the copy it would make
    result = env.run("restore", "-r", str(env.root), "--from", "user@web-01", "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"latest: 2025/01-01/{newer.name}\n" in result.stdout
    assert f"user@web-01:.molt/captures/2025/01-01/{newer.name}/" in result.stdout
    assert (env.home / ".zshrc").read_text() == "edited"

    result = env.run("restore", "-r", str(env.root), "--from", "user@web-01")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "  From: user@web-01:.molt/captures (latest)\n" in result.stdout
    assert f"latest: 2025/01-01/{newer.name}\n" in result.stdout
    assert (env.home / ".zshrc").read_text() == "newer"

    # The listing is one portable ls -t under sh
    [listing] = set(hosts.ssh_commands("user@web-01"))
    assert listing == (
        "sh -c 'ls -1t -- .molt/captures/[0-9][0-9][0-9][0-9]/[0-9][0-9]-[0-9][0-9]/*/"
        "snapshot.toml 2>/dev/null | head -1'"
    )


def test_e2e_remote_without_snapshots(env, hosts):
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"])})
    write_files(hosts.home("web-01") / ".molt" / "captures" / "2026", {"README": "x"})

    result = env.run("restore", "-r", str(env.root), "--from", "user@web-01")
    assert result.returncode == 1
    assert result.stderr.endswith("Error: No snapshots found in user@web-01:.molt/captures\n")

    result = env.run(
        "restore", "-r", str(env.root), "--from", "user@web-01",
        extra_env={"FAKE_HOSTS_DOWN": "web-01"},
    )
    assert result.returncode == 1
    assert (
        "  ssh: connect to host web-01 port 22: Connection refused\n"
        "Error: Finding the latest snapshot failed on user@web-01: ssh error (exit status 255)\n"
    ) in result.stderr


# --- Fix B6: -r host[:path] reads configs and scripts from the host --- #


def test_e2e_capture_and_restore_with_a_remote_molt_root(env, hosts):
    write_files(env.home, {".zshrc": "v1", ".vimrc": "v1"})
    remote_root = remote_molt_root(
        hosts, "web-01", {"dotfiles": ("$HOME", [".zshrc"]), "vim": ("$HOME", [".vimrc"])},
        restore_toml='[tar]\narchives = ["dotfiles"]\n',
    )
    tmpdir = env.tmp / "tmpdir"
    tmpdir.mkdir()
    local_tmp = {"TMPDIR": str(tmpdir)}

    # A dry run reads the host's real capture.toml, and writes nothing there
    result = env.run("capture", "-r", "user@web-01", "--dry-run", extra_env=local_tmp)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"  dotfiles.tar.gz (root: {env.home})\n" in result.stdout
    assert f"  vim.tar.gz (root: {env.home})\n" in result.stdout
    assert "Note: A real run would copy the snapshot to user@web-01:.molt/captures/" in (
        result.stdout
    )
    assert not (remote_root / "captures").exists()

    # capture saves to the host's captures directory
    result = env.run("capture", "-r", "user@web-01", extra_env=local_tmp)
    assert result.returncode == 0, result.stdout + result.stderr
    [snapshot] = remote_root.glob("captures/*/*/*")
    assert sorted(p.name for p in snapshot.iterdir()) == [
        "dotfiles.tar.gz", "snapshot.toml", "vim.tar.gz",
    ]
    rel = snapshot.relative_to(hosts.home("web-01"))
    assert f"  To:   user@web-01:{rel.parent}\n" in result.stdout
    assert f"✓ Snapshot saved to user@web-01:{rel}\n" in result.stdout
    assert list(tmpdir.iterdir()) == []  # the local copy of the molt root is gone

    # restore takes the host's latest snapshot and its restore.toml ('dotfiles' only)
    write_files(env.home, {".zshrc": "edited", ".vimrc": "edited"})
    hosts.clear()
    result = env.run("restore", "-r", "user@web-01", extra_env=local_tmp)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "  From: user@web-01:.molt/captures (latest)\n" in result.stdout
    assert f"latest: {snapshot.relative_to(remote_root / 'captures')}\n" in result.stdout
    assert "Warning" not in result.stderr
    assert (env.home / ".zshrc").read_text() == "v1"
    assert (env.home / ".vimrc").read_text() == "edited"
    assert list(tmpdir.iterdir()) == []
    assert snapshot.is_dir()  # the host keeps its snapshot

    # Only reads reached the host: the molt root copy, the listing and the pull
    for line in hosts.calls():
        assert line.startswith(("rsync ", "ssh user@web-01 sh -c 'ls -1t ")), line
    assert not (env.home / ".molt").exists()


def test_e2e_remote_molt_root_errors_name_the_host(env, hosts):
    tmpdir = env.tmp / "tmpdir"
    tmpdir.mkdir()

    # No .molt on the host: stops before the banner, like a missing local .molt
    result = env.run("capture", "-r", "user@web-01", extra_env={"TMPDIR": str(tmpdir)})
    assert result.returncode == 1
    assert "Starting capture" not in result.stdout
    assert (
        "Error: Copying the molt root from user@web-01 failed (rsync exit status 23)\n"
        "  from: user@web-01:.molt/\n"
        "  Create a .molt directory on user@web-01, or pass -r/--molt-root "
        "user@web-01:<path> (an older install's is .snap)\n"
    ) in result.stderr
    assert list(tmpdir.iterdir()) == []

    # A missing config is named by where it lives
    (hosts.home("web-01") / "molt" / "scripts").mkdir(parents=True)
    result = env.run("capture", "-r", "user@web-01:~/molt", "--dry-run")
    assert result.returncode == 1
    assert "Error: Config file not found: user@web-01:molt/configs/capture.toml\n" in (
        result.stderr
    )


def test_e2e_remote_molt_root_scripts_run_from_the_local_copy(env, hosts):
    remote_root = remote_molt_root(hosts, "web-01", {"dotfiles": ("$HOME", [".zshrc"])})
    write_files(env.home, {".zshrc": "v1"})
    write_files(remote_root, {"scripts/after.sh": 'echo "after in $PWD"\n'})
    capture_toml = remote_root / "configs" / "capture.toml"
    scripts = '\n[scripts]\nafter = ["scripts/after.sh"]\n'
    capture_toml.write_text(capture_toml.read_text() + scripts)

    result = env.run("capture", "-r", "user@web-01", "--run-scripts")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "✓ after.sh completed" in result.stdout
    assert "    after in " in result.stdout


def test_sudo_restore_passes_the_local_copy_of_a_remote_molt_root(tmp_path, monkeypatch):
    commands = []

    class FakePopen:
        def __init__(self, cmd):
            commands.append(cmd)

        def wait(self):
            return 0

    monkeypatch.setattr(molt.subprocess, "Popen", FakePopen)
    args = argparse.Namespace(root_host="user@web-01", root_path=molt.PurePosixPath(".molt"))

    with pytest.raises(SystemExit):
        molt.sudo_restore(args, tmp_path / "copy", tmp_path / "cap", {}, {})

    [cmd] = commands
    assert cmd[cmd.index("-r") + 1] == str(tmp_path / "copy")
    assert not any("web-01" in arg for arg in cmd)


# --- Fix B7: only short ssh commands have a wall-clock timeout --- #


def test_long_running_commands_have_no_wall_clock_timeout(tmp_path, monkeypatch):
    (tmp_path / "configs").mkdir()
    write_configs(tmp_path, {"dotfiles": ("$HOME", [".zshrc"])})
    calls = fake_ssh_runs(monkeypatch, {"ls -d": "/tmp/molt-capture.Ab3dEf9h/out/abc1234/\n"})
    args = argparse.Namespace(config_toml=None, run_scripts=False)

    pulled, tmpdir = molt.remote_capture(args, tmp_path, "user@web-01")
    assert pulled == tmpdir / "abc1234"
    shutil.rmtree(tmpdir)

    timeouts = {}
    for cmd, timeout in calls:
        name = cmd[0] if cmd[0] == "rsync" else cmd[-1].split()[0]
        if cmd[0] == "ssh" and "molt.py capture" in cmd[-1]:
            name = "remote capture"
        timeouts.setdefault(name, set()).add(timeout)
    assert timeouts == {
        "mktemp": {molt.COMMAND_TIMEOUT},
        "mkdir": {molt.COMMAND_TIMEOUT},
        "rsync": {None},
        "remote capture": {None},
        "ls": {molt.COMMAND_TIMEOUT},
        "rm": {molt.COMMAND_TIMEOUT},
    }
    # rsync keeps its own inactivity limit
    assert all(f"--timeout={molt.RSYNC_TIMEOUT}" in cmd for cmd, _ in calls if cmd[0] == "rsync")


def test_scripts_have_no_wall_clock_timeout(tmp_path, monkeypatch):
    # B3: scripts on a host are run by molt.py there, which runs them like this
    write_files(tmp_path, {"scripts/a.sh": "true\n"})
    calls = fake_ssh_runs(monkeypatch)
    config = {"scripts": {"before": ["scripts/a.sh"]}}

    molt.run_scripts(tmp_path, config, "before", working_dir=tmp_path)

    assert [timeout for _, timeout in calls] == [None]


# --- Fix B8: dry runs of remote flows run nothing that writes --- #


def test_ssh_run_in_a_dry_run_returns_a_placeholder_result(monkeypatch, capsys):
    calls = fake_ssh_runs(monkeypatch, {"ls": "2026/09-23/abc1234/snapshot.toml\n"})
    monkeypatch.setattr(molt, "__dry_run__", True)

    assert molt.ssh_run("user@web-01", "mkdir -p x") == (0, "", "")
    listing = molt.ssh_run("user@web-01", "ls", read_only=True)
    assert listing == (0, "2026/09-23/abc1234/snapshot.toml\n", "")

    # Only the read-only query ran; only the skipped command is shown
    assert [cmd[-1] for cmd, _ in calls] == ["ls"]
    assert capsys.readouterr().out == (
        "[DRY-RUN]   run: ssh -o ConnectTimeout=30 -o ServerAliveInterval=10 "
        "user@web-01 'mkdir -p x'\n"
    )


@pytest.mark.parametrize("command", [
    ["restore", "--to", "user@web-01", "--run-scripts"],
    ["restore", "--from", "user@web-02:.molt/captures/2026/09-23/abc1234",
     "--to", "user@web-01", "--run-scripts"],
    ["capture", "--from", "user@web-01", "--run-scripts"],
    ["migrate", "--to", "user@web-01", "--run-scripts"],
])
def test_e2e_remote_dry_runs_change_nothing(env, hosts, command):
    write_files(env.home, {".zshrc": "v1"})
    write_files(env.root, {"scripts/before.sh": "touch \"$HOME/before-ran\"\n"})
    scripts = '\n[scripts]\nbefore = ["scripts/before.sh"]\nafter = ["scripts/before.sh"]\n'
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"])}, restore_extra=scripts)
    (env.root / "configs" / "migrate.toml").write_text(
        '[tarball]\nchecksum = "sha256"\n\n'
        '[capture.tar.dotfiles]\nroot = "$HOME"\nfiles = [".zshrc"]\n\n'
        '[restore.tar]\narchives = ["*"]\n\n'
        '[restore.scripts]\nbefore = ["scripts/before.sh"]\n'
    )
    assert env.run("capture", "-r", str(env.root)).returncode == 0
    hosts.clear()

    result = env.run(*command, "-r", str(env.root), "--dry-run")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Traceback" not in result.stderr
    assert result.stdout.endswith("[DRY-RUN] Dry run completed; no changes were made\n")
    # Every command is shown, the work dir by name, and nothing ran on a host
    assert "run: ssh -o ConnectTimeout=30 -o ServerAliveInterval=10 user@web-01 " in (
        result.stdout
    )
    assert "<work dir>" in result.stdout
    assert hosts.calls() == []
    assert not (env.home / "before-ran").exists()


# --- Fix B3: molt.py runs on the host, with python3 from its PATH and no hard-coded sudo --- #


def child_command(work_dir, command, *options):
    """Return the shell line molt.py sends a host to run 'molt.py <command>' there.

    A capture runs under umask 077, so what it writes stays private.
    """
    argv = ["python3", "molt.py", command, *options]
    line = f"cd {shlex.quote(str(work_dir))} && {shlex.join(argv)}"
    if command == "capture":
        line = f"umask 077 && {line}"
    return line


def rsync_hosts(hosts):
    """Return the host:path arguments of each logged rsync call."""
    found = []
    for line in hosts.calls():
        if line.startswith("rsync "):
            args = shlex.split(line)[1:]
            found.append([arg for arg in args if re.match(r"[^/:-][^/:]*:", arg)])
    return found


def local_tmpdir(env):
    """Return an empty dir to use as this machine's TMPDIR, so leftovers show."""
    tmpdir = env.tmp / "tmpdir"
    tmpdir.mkdir(exist_ok=True)
    return tmpdir


def read_member(archive, name):
    """Return one file's text from a tar archive."""
    with tarfile.open(archive) as tar:
        return tar.extractfile(name).read().decode()


# A before or after script's cwd is the snapshot dir, so these see what travelled with it
BREW_SCRIPT = 'echo "brew from $(basename "$HOME")" > Brewfile\n'
SEEN_SCRIPT = (
    'cp Brewfile "$HOME/Brewfile-seen"\n'
    'cp ../configs/restore.toml "$HOME/restore-config-seen" 2>/dev/null || true\n'
    'echo "${MOLT_TEST_FAKE_ROOT:-user} in $PWD" > "$HOME/seen-by"\n'
)


def write_migrate_toml(root):
    """Write a migrate.toml with two archives, of which the restore selects one."""
    (root / "configs" / "migrate.toml").write_text(
        '[tarball]\nchecksum = "sha256"\nrollback = ".bak"\n\n'
        '[capture.tar.dotfiles]\nroot = "$HOME"\nfiles = [".zshrc"]\n\n'
        '[capture.tar.vim]\nroot = "$HOME"\nfiles = [".vimrc"]\n\n'
        '[capture.scripts]\nafter = ["scripts/sub/brew.sh"]\n\n'
        '[restore.tar]\narchives = ["dotfiles"]\n\n'
        '[restore.scripts]\nbefore = ["scripts/seen.sh"]\n'
    )
    write_files(root, {"scripts/sub/brew.sh": BREW_SCRIPT, "scripts/seen.sh": SEEN_SCRIPT})


def test_e2e_capture_from_host_runs_molt_py_there(env, hosts):
    # The -t config is the one the host gets, with its script at the same relative path
    write_files(env.root, {"scripts/sub/brew.sh": BREW_SCRIPT})
    write_configs(env.root, {"vim": ("$HOME", [".vimrc"])})
    (env.root / "configs" / "custom.toml").write_text(
        '[tarball]\nchecksum = "sha256"\n\n'
        '[tar.dotfiles]\nroot = "$HOME"\nfiles = [".zshrc"]\n\n'
        '[scripts]\nafter = ["scripts/sub/brew.sh"]\n'
    )
    write_files(hosts.home("web-01"), {".zshrc": "on web-01"})
    tmpdir = local_tmpdir(env)

    result = env.run(
        "capture", "-r", str(env.root), "-t", "configs/custom.toml", "--from", "user@web-01",
        "--run-scripts", "--verbose", extra_env={"TMPDIR": str(tmpdir)},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    commands = hosts.ssh_commands("user@web-01")
    work_dir = work_dir_of(commands, "capture")
    assert child_command(
        work_dir, "capture", "-r", work_dir, "-t", "configs/capture.toml",
        "--to", f"{work_dir}/out", "--run-scripts", "--verbose",
    ) in commands
    assert f"ls -d {work_dir}/out/*/" in commands
    assert not any(sys.executable in c or "sudo" in c for c in commands)
    [script_copy] = [line for line in hosts.calls() if "brew.sh" in line]
    assert script_copy.endswith(f" user@web-01:{work_dir}/scripts/sub/brew.sh")

    # The local snapshot is named after the host's snapshot id, and holds the Brewfile
    # the host's after script wrote into it
    [snapshot_id] = re.findall(r"    ✓ Snapshot saved to \S+/out/(\w+)\n", result.stdout)
    [snapshot] = env.root.glob("captures/*/*/*")
    assert snapshot.name == snapshot_id
    assert sorted(p.name for p in snapshot.iterdir()) == [
        "Brewfile", "dotfiles.tar.gz", "snapshot.toml",
    ]
    assert (snapshot / "Brewfile").read_text() == "brew from home\n"
    assert read_member(snapshot / "dotfiles.tar.gz", ".zshrc") == "on web-01"
    assert f"✓ Snapshot saved to {snapshot}\n" in result.stdout

    # Nothing is left behind on either side
    assert list(hosts.tmp("web-01").iterdir()) == []
    assert list(tmpdir.iterdir()) == []


def test_e2e_restore_to_host_runs_molt_py_there(env, hosts):
    write_files(env.home, {".zshrc": "v1"})
    write_files(env.root, {"scripts/sub/brew.sh": BREW_SCRIPT, "scripts/seen.sh": SEEN_SCRIPT})
    write_configs(
        env.root, {"dotfiles": ("$HOME", [".zshrc"])},
        restore_extra='\n[scripts]\nafter = ["scripts/seen.sh"]\n',
    )
    capture_toml = env.root / "configs" / "capture.toml"
    capture_toml.write_text(
        capture_toml.read_text() + '\n[scripts]\nafter = ["scripts/sub/brew.sh"]\n'
    )
    assert env.run("capture", "-r", str(env.root), "--run-scripts").returncode == 0
    remote = hosts.home("web-01")
    write_files(remote, {".zshrc": "old"})
    tmpdir = local_tmpdir(env)

    result = env.run(
        "restore", "-r", str(env.root), "--to", "user@web-01", "--run-scripts",
        extra_env={"TMPDIR": str(tmpdir)},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (remote / ".zshrc").read_text() == "v1"
    assert (remote.parent / "home.bak" / "dotfiles" / ".zshrc").read_text() == "old"

    # molt.py there restores without sudo (nothing needs root) and runs the after script
    # itself, in its snapshot copy, which holds the Brewfile written at capture
    commands = hosts.ssh_commands("user@web-01")
    work_dir = work_dir_of(commands, "restore")
    assert child_command(
        work_dir, "restore", "--from", f"{work_dir}/snapshot", "-r", work_dir,
        "-t", "configs/restore.toml", "--run-scripts",
    ) in commands
    assert not any("sudo" in c or sys.executable in c for c in commands)
    assert not any(line.startswith("sudo ") for line in hosts.calls())
    assert (remote / "Brewfile-seen").read_text() == "brew from home\n"
    assert (remote / "seen-by").read_text() == f"user in {work_dir}/snapshot\n"
    assert molt.tomllib.loads((remote / "restore-config-seen").read_text()) == (
        molt.tomllib.loads((env.root / "configs" / "restore.toml").read_text())
    )
    assert not (env.home / "seen-by").exists()

    # The host's own restore output sits between the parent's steps
    assert "\nRunning restore on user@web-01...\nStarting restore\n" in result.stdout
    assert "\n✓ Restore completed: 1 archive restored\n\nRemoving work directory" in (
        result.stdout
    )
    assert result.stdout.endswith("\nRestore on user@web-01 finished; see its summary above\n")
    assert list(hosts.tmp("web-01").iterdir()) == []
    assert list(tmpdir.iterdir()) == []


def test_e2e_restore_to_host_re_runs_with_sudo_there_only_when_needed(env, hosts):
    write_files(env.home, {"force-sudo-root/f.txt": "v1"})
    seen = 'echo "${MOLT_TEST_FAKE_ROOT:-user}" > "$HOME/seen"\n'
    write_files(env.root, {"scripts/seen.sh": seen})
    write_configs(
        env.root, {"system": ("$HOME/force-sudo-root", ["f.txt"])},
        restore_extra='\n[scripts]\nbefore = ["scripts/seen.sh"]\n',
    )
    assert env.run("capture", "-r", str(env.root)).returncode == 0
    remote = hosts.home("web-01")
    write_files(remote, {"force-sudo-root/f.txt": "old"})

    # molt.py on the host sees the root as unwritable; the host's sudo makes it "root"
    site = hosts.base / "site"
    result = env.run(
        "restore", "-r", str(env.root), "--to", "user@web-01", "--run-scripts",
        extra_env={
            "MOLT_TEST_FORCE_SUDO": "1",
            "PYTHONPATH": f"{site}{os.pathsep}{env.env['PYTHONPATH']}",
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (remote / "force-sudo-root" / "f.txt").read_text() == "v1"
    assert "Note: Restoring system.tar.gz needs root\n" in result.stdout
    assert "Re-running the restore with sudo...\nStarting restore as root\n" in result.stdout
    assert not env.sudo_marker.exists()  # never this machine's sudo

    # The host's sudo ran molt.py (keeping the login HOME), which ran the script as root
    [sudo_call] = [line for line in hosts.calls() if line.startswith("sudo ")]
    assert sudo_call.startswith(f"sudo web-01 env HOME={remote} ")
    assert " restore --from " in sudo_call and sudo_call.endswith(" --run-scripts")
    assert not any("sudo" in c for c in hosts.ssh_commands("user@web-01"))
    assert (remote / "seen").read_text() == "1\n"


def test_e2e_capture_to_host_copies_the_whole_snapshot(env, hosts):
    write_files(env.home, {".zshrc": "v1"})
    write_files(env.root, {"scripts/sub/brew.sh": BREW_SCRIPT})
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"])})
    capture_toml = env.root / "configs" / "capture.toml"
    capture_toml.write_text(
        capture_toml.read_text() + '\n[scripts]\nafter = ["scripts/sub/brew.sh"]\n'
    )

    result = env.run("capture", "-r", str(env.root), "--to", "user@web-01", "--run-scripts")

    assert result.returncode == 0, result.stdout + result.stderr
    [snapshot] = hosts.home("web-01").glob(".molt/captures/*/*/*")
    assert sorted(p.name for p in snapshot.iterdir()) == [
        "Brewfile", "dotfiles.tar.gz", "snapshot.toml",
    ]
    # One copy of the whole directory
    rel = snapshot.relative_to(hosts.home("web-01"))
    assert rsync_hosts(hosts) == [[f"user@web-01:{rel}/"]]


# --- Fix B4: remote-to-remote copies go through this machine --- #


def test_e2e_restore_from_one_host_on_another(env, hosts):
    write_files(env.home, {".zshrc": "v1"})
    write_files(env.root, {"scripts/sub/brew.sh": BREW_SCRIPT, "scripts/seen.sh": SEEN_SCRIPT})
    write_configs(
        env.root, {"dotfiles": ("$HOME", [".zshrc"])},
        restore_extra='\n[scripts]\nbefore = ["scripts/seen.sh"]\n',
    )
    capture_toml = env.root / "configs" / "capture.toml"
    capture_toml.write_text(
        capture_toml.read_text() + '\n[scripts]\nafter = ["scripts/sub/brew.sh"]\n'
    )
    result = env.run("capture", "-r", str(env.root), "--to", "user@web-01", "--run-scripts")
    assert result.returncode == 0, result.stdout + result.stderr
    hosts.clear()
    tmpdir = local_tmpdir(env)

    result = env.run(
        "restore", "-r", str(env.root), "--from", "user@web-01", "--to", "user@web-02",
        "--run-scripts", extra_env={"TMPDIR": str(tmpdir)},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    target = hosts.home("web-02")
    assert (target / ".zshrc").read_text() == "v1"
    assert (target / "Brewfile-seen").read_text() == "brew from home\n"
    assert result.stdout.endswith("\nRestore on user@web-02 finished; see its summary above\n")

    # Copied from web-01 to this machine, then from here to web-02; never host to host
    copies = rsync_hosts(hosts)
    assert all(len(found) == 1 for found in copies), copies
    assert [found[0].split(":")[0] for found in copies][:2] == ["user@web-01", "user@web-02"]
    assert list(tmpdir.iterdir()) == []
    assert list(hosts.tmp("web-02").iterdir()) == []

    # A failed restore on web-02 still removes the local copy and the work dir
    result = env.run(
        "restore", "-r", str(env.root), "--from", "user@web-01", "--to", "user@web-02",
        extra_env={"TMPDIR": str(tmpdir), "FAKE_SSH_FAIL": "molt.py restore"},
    )
    assert result.returncode == 1
    assert result.stderr.endswith("Error: Restore failed on user@web-02 (exit status 1)\n")
    assert list(tmpdir.iterdir()) == []
    assert list(hosts.tmp("web-02").iterdir()) == []


def test_e2e_capture_from_one_host_to_another(env, hosts):
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"])})
    write_files(hosts.home("web-01"), {".zshrc": "on web-01"})
    tmpdir = local_tmpdir(env)

    result = env.run(
        "capture", "-r", str(env.root), "--from", "user@web-01", "--to", "user@web-02:~/keep",
        extra_env={"TMPDIR": str(tmpdir)},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    [snapshot] = hosts.home("web-02").glob("keep/*")
    assert read_member(snapshot / "dotfiles.tar.gz", ".zshrc") == "on web-01"
    assert f"✓ Snapshot saved to user@web-02:keep/{snapshot.name}\n" in result.stdout
    assert all(len(found) == 1 for found in rsync_hosts(hosts))
    assert list(tmpdir.iterdir()) == []
    assert list(hosts.tmp("web-01").iterdir()) == []


def test_e2e_migrate_to_host_keeps_the_restore_selection(env, hosts):
    write_files(env.home, {".zshrc": "v1", ".vimrc": "v1"})
    write_migrate_toml(env.root)
    remote = hosts.home("web-01")
    tmpdir = local_tmpdir(env)

    result = env.run(
        "migrate", "-r", str(env.root), "--to", "user@web-01", "--run-scripts",
        extra_env={"TMPDIR": str(tmpdir)},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    # [restore.tar] archives picks dotfiles only; the host got it as its restore.toml
    assert (remote / ".zshrc").read_text() == "v1"
    assert not (remote / ".vimrc").exists()
    assert molt.tomllib.loads((remote / "restore-config-seen").read_text()) == {
        "tar": {"archives": ["dotfiles"]},
        "scripts": {"before": ["scripts/seen.sh"]},
        "molt": {"table": "restore.tar"},  # so its messages name [restore.tar]
    }
    # The capture script ran here; its Brewfile travelled in the snapshot to the host
    assert (remote / "Brewfile-seen").read_text() == f"brew from {env.home.name}\n"
    assert result.stdout.endswith(
        "\nMigration to user@web-01 finished; see the restore summary above\n"
    )
    assert list(tmpdir.iterdir()) == []
    assert list(hosts.tmp("web-01").iterdir()) == []


def test_e2e_migrate_from_host(env, hosts):
    write_files(env.home, {".zshrc": "v1", ".vimrc": "v1"})
    write_migrate_toml(env.root)
    write_files(hosts.home("web-01"), {".zshrc": "on web-01", ".vimrc": "on web-01"})
    tmpdir = local_tmpdir(env)

    result = env.run(
        "migrate", "-r", str(env.root), "--from", "user@web-01", "--run-scripts",
        extra_env={"TMPDIR": str(tmpdir)},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (env.home / ".zshrc").read_text() == "on web-01"
    assert (env.home / ".vimrc").read_text() == "v1"  # not in [restore.tar] archives
    # The capture script ran on the host (by molt.py there), the restore script here
    assert (env.home / "Brewfile-seen").read_text() == "brew from home\n"
    commands = hosts.ssh_commands("user@web-01")
    work_dir = work_dir_of(commands, "capture")
    assert child_command(
        work_dir, "capture", "-r", work_dir, "-t", "configs/capture.toml",
        "--to", f"{work_dir}/out", "--run-scripts",
    ) in commands
    assert result.stdout.endswith("\n✓ Migration completed: 1 archive restored\n")
    assert list(tmpdir.iterdir()) == []
    assert list(hosts.tmp("web-01").iterdir()) == []


def test_e2e_migrate_from_one_host_to_another(env, hosts):
    write_migrate_toml(env.root)
    write_files(hosts.home("web-01"), {".zshrc": "on web-01", ".vimrc": "on web-01"})
    tmpdir = local_tmpdir(env)

    result = env.run(
        "migrate", "-r", str(env.root), "--from", "user@web-01", "--to", "user@web-02",
        "--run-scripts", extra_env={"TMPDIR": str(tmpdir)},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    target = hosts.home("web-02")
    assert (target / ".zshrc").read_text() == "on web-01"
    assert not (target / ".vimrc").exists()
    assert (target / "Brewfile-seen").read_text() == "brew from home\n"
    assert all(len(found) == 1 for found in rsync_hosts(hosts))
    assert result.stdout.endswith(
        "\nMigration to user@web-02 finished; see the restore summary above\n"
    )
    assert list(tmpdir.iterdir()) == []
    assert list(hosts.tmp("web-01").iterdir()) == []
    assert list(hosts.tmp("web-02").iterdir()) == []


def test_e2e_remote_molt_root_with_remote_capture_and_restore(env, hosts):
    # -r names where configs, scripts and captures live; --from/--to where the work runs
    remote_root = remote_molt_root(hosts, "web-01", {"dotfiles": ("$HOME", [".zshrc"])})
    write_files(hosts.home("web-02"), {".zshrc": "on web-02"})
    tmpdir = local_tmpdir(env)
    local_tmp = {"TMPDIR": str(tmpdir)}

    result = env.run("capture", "-r", "user@web-01", "--from", "user@web-02", extra_env=local_tmp)
    assert result.returncode == 0, result.stdout + result.stderr
    [snapshot] = remote_root.glob("captures/*/*/*")
    assert read_member(snapshot / "dotfiles.tar.gz", ".zshrc") == "on web-02"

    result = env.run("restore", "-r", "user@web-01", "--to", "user@web-03", extra_env=local_tmp)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "  From: user@web-01:.molt/captures (latest)\n" in result.stdout
    assert (hosts.home("web-03") / ".zshrc").read_text() == "on web-02"

    # molt.py never ran on the molt root's host
    assert not any("molt.py" in c for c in hosts.ssh_commands("user@web-01"))
    assert list(tmpdir.iterdir()) == []
    for host in ("web-01", "web-02", "web-03"):
        assert list(hosts.tmp(host).iterdir()) == []


def tree_state(root):
    """Return every path under root with its file content, to compare before and after."""
    state = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        state[rel] = path.read_bytes() if path.is_file() and not path.is_symlink() else None
    return state


@pytest.mark.parametrize("scripts", [[], ["--run-scripts"]])
@pytest.mark.parametrize("command", [
    ["capture", "--to", "user@web-01"],
    ["capture", "--from", "user@web-01"],
    ["capture", "--from", "user@web-01", "--to", "user@web-02"],
    ["restore", "--to", "user@web-01"],
    ["restore", "--from", "user@web-01"],
    ["restore", "--from", "user@web-01", "--to", "user@web-02"],
    ["migrate", "--to", "user@web-01"],
    ["migrate", "--from", "user@web-01"],
    ["migrate", "--from", "user@web-01", "--to", "user@web-02"],
    ["capture", "-r", "user@web-01"],
    ["capture", "-r", "user@web-01", "--from", "user@web-02"],
    ["restore", "-r", "user@web-01"],
    ["restore", "-r", "user@web-01", "--to", "user@web-02"],
])
def test_e2e_every_remote_flow_has_a_dry_run(env, hosts, command, scripts):
    marker = 'touch "$HOME/script-ran"\n'
    write_files(env.home, {".zshrc": "v1"})
    write_files(env.root, {"scripts/mark.sh": marker})
    both = '\n[scripts]\nbefore = ["scripts/mark.sh"]\nafter = ["scripts/mark.sh"]\n'
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"])}, restore_extra=both)
    capture_toml = env.root / "configs" / "capture.toml"
    capture_toml.write_text(capture_toml.read_text() + both)
    write_migrate_toml(env.root)
    remote_root = remote_molt_root(hosts, "web-01", {"dotfiles": ("$HOME", [".zshrc"])})
    write_files(remote_root, {"scripts/mark.sh": marker})
    assert env.run("capture", "-r", str(env.root)).returncode == 0
    assert env.run("capture", "-r", str(env.root), "--to", "user@web-01").returncode == 0
    for host in ("web-01", "web-02"):
        write_files(hosts.home(host), {".zshrc": f"on {host}"})
    hosts.clear()
    before = {"local": tree_state(env.tmp / "home"), "hosts": tree_state(hosts.base)}

    if "-r" not in command:
        command = [*command, "-r", str(env.root)]
    result = env.run(*command, *scripts, "--dry-run")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Traceback" not in result.stderr
    assert "Error" not in result.stderr
    assert result.stdout.endswith("[DRY-RUN] Dry run completed; no changes were made\n")
    # Only read-only queries reached a host: the molt root copy and the latest lookup
    for line in hosts.calls():
        assert (
            line.startswith("rsync ") and "--include=/configs/" in line
            or re.match(r"ssh user@web-0\d sh -c 'ls -1t ", line)
        ), line
    after = {"local": tree_state(env.tmp / "home"), "hosts": tree_state(hosts.base)}
    after["hosts"].pop("calls.log", None)
    assert after == before


# --- Remote runs: unit checks --- #


def test_remote_scripts_keep_their_paths_in_the_molt_root(tmp_path):
    write_files(tmp_path, {"scripts/a.sh": "", "scripts/sub/b.sh": "", "top.sh": ""})
    config = {"scripts": {
        "before": ["scripts/a.sh", "./scripts/sub/b.sh", "scripts/missing.sh"],
        "after": ["top.sh", "scripts/a.sh"],
    }}

    scripts = molt.remote_scripts(tmp_path, config, "user@web-01")

    assert [str(s) for s in scripts] == ["scripts/a.sh", "scripts/sub/b.sh", "top.sh"]
    assert molt.remote_layout(["configs", "scripts"], scripts) == [
        "configs", "scripts", "scripts/sub",
    ]


@pytest.mark.parametrize("path", ["../outside.sh", "/usr/local/bin/setup.sh", "a/../../b.sh"])
def test_remote_scripts_outside_the_molt_root_stop_the_run(tmp_path, capsys, path):
    with pytest.raises(SystemExit):
        molt.remote_scripts(tmp_path, {"scripts": {"after": [path]}}, "user@web-01")
    assert f"Error: Cannot copy script {path} to user@web-01: it is outside" in (
        capsys.readouterr().err
    )


def test_remote_child_runs_python3_from_the_path(monkeypatch):
    monkeypatch.setattr(molt, "__verbose__", True)
    work_dir = molt.PurePosixPath("/tmp/molt-restore.Ab3dEf9h")

    line = molt.remote_child(work_dir, "restore", "--from", work_dir / "snapshot")

    assert line == (
        "cd /tmp/molt-restore.Ab3dEf9h && python3 molt.py restore "
        "--from /tmp/molt-restore.Ab3dEf9h/snapshot --verbose"
    )


@pytest.mark.parametrize("output", [
    "",
    "/tmp/w/configs/\n",
    "/tmp/w/out/a/\n/tmp/w/out/b/\n",
    "/tmp/w/out/../\n",
])
def test_remote_snapshot_id_needs_one_plain_directory(capsys, output):
    with pytest.raises(SystemExit):
        molt.remote_snapshot_id(output, molt.PurePosixPath("/tmp/w/out"), "user@web-01")
    assert "Error: Cannot find the snapshot on user@web-01" in capsys.readouterr().err

    motd = "Welcome to web-01\n/tmp/w/out/abc1234/\n"
    out_dir = molt.PurePosixPath("/tmp/w/out")
    assert molt.remote_snapshot_id(motd, out_dir, "web-01") == "abc1234"


# --- Review fixes: remote paths, sudo env, backups, messages --- #


@pytest.mark.parametrize("value", [
    "host:a b", "host:$(id)", "host:x;y", "host:`id`", "host:x'y", "host:-x", "host:~/-x",
])
def test_parse_remote_arg_rejects_paths_rsync_cannot_pass_safely(value):
    with pytest.raises(SystemExit):
        molt.parse_remote_arg(value, "--to")


def test_parse_remote_arg_keeps_ordinary_remote_paths():
    assert molt.parse_remote_arg("user@host:~/snaps/v1.2_x+y=z,%@-") == (
        "user@host", Path("~/snaps/v1.2_x+y=z,%@-")
    )


def test_remote_workdir_never_uses_a_path_with_shell_characters(monkeypatch, capsys):
    calls = fake_ssh_runs(monkeypatch, {"mktemp": "/tmp/my dir/molt-restore.Ab3dEf9h\n"})

    with pytest.raises(SystemExit):
        molt.remote_workdir("user@web-01", "restore", ["configs"])

    assert [cmd[-1].split()[0] for cmd, _ in calls] == ["mktemp"]
    assert "Cannot create a work directory on user@web-01" in capsys.readouterr().err


def test_latest_snapshot_with_shell_characters_is_refused(monkeypatch, capsys):
    fake_ssh_runs(monkeypatch, {"ls -1t": "caps/2026/09-23/a$(id)/snapshot.toml\n"})

    with pytest.raises(SystemExit):
        molt.resolve_remote_snapshot("user@web-01", "caps")

    assert "Cannot use snapshot caps/2026/09-23/a$(id) on user@web-01" in (
        capsys.readouterr().err
    )


def test_sudo_restore_never_passes_variables_that_change_what_runs(tmp_path, monkeypatch):
    commands = []

    class FakePopen:
        def __init__(self, cmd):
            commands.append(cmd)

        def wait(self):
            return 0

    monkeypatch.setattr(molt.subprocess, "Popen", FakePopen)
    for name in ("PYTHONPATH", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "BASH_ENV", "XDG_DATA_HOME"):
        monkeypatch.setenv(name, "/evil" if name != "XDG_DATA_HOME" else "/data")
    snapshot_config = {"tar": {
        "a": {"root": "${PYTHONPATH}/a"},
        "b": {"root": "$LD_PRELOAD", "link": "$DYLD_INSERT_LIBRARIES/b"},
        "c": {"root": "$BASH_ENV/c"},
        "d": {"root": "$PATH/d"},
        "e": {"root": "$XDG_DATA_HOME/e"},
    }}
    args = argparse.Namespace(root_host=None)

    with pytest.raises(SystemExit):
        molt.sudo_restore(args, tmp_path, tmp_path, {"tar": {"archives": ["*"]}}, snapshot_config)

    env_args = [arg for arg in commands[0] if "=" in arg and not arg.startswith("-")]
    assert "XDG_DATA_HOME=/data" in env_args
    assert not any(arg.split("=")[0] in (
        "PYTHONPATH", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "BASH_ENV", "PATH"
    ) for arg in env_args)


def test_sudo_restore_keeps_the_users_table_name(tmp_path, monkeypatch):
    configs = []

    class FakePopen:
        def __init__(self, cmd):
            configs.append(Path(cmd[cmd.index("-t") + 1]).read_text())

        def wait(self):
            return 0

    monkeypatch.setattr(molt.subprocess, "Popen", FakePopen)
    args = argparse.Namespace(root_host=None)

    with pytest.raises(SystemExit):
        molt.sudo_restore(
            args, tmp_path, tmp_path, {"tar": {"archives": ["x"]}}, {}, table="restore.tar"
        )

    assert molt.tomllib.loads(configs[0]) == {
        "tar": {"archives": ["x"]}, "molt": {"table": "restore.tar"},
    }


def test_same_root_spelled_two_ways_is_rotated_once(tmp_path):
    src = tmp_path / "src"
    home = tmp_path / "home"
    write_files(src, {"a.txt": "new a", "b.txt": "new b"})
    write_files(home, {"a.txt": "old a", "b.txt": "old b"})
    (tmp_path / "home.bak").mkdir()  # from an earlier run
    (tmp_path / "alias").symlink_to(home)
    alpha = make_archive(tmp_path, "alpha", src, ["a.txt"])
    beta = make_archive(tmp_path, "beta", src, ["b.txt"])
    rotated = set()

    assert molt.restore_category(alpha, str(home), ".bak", rotated=rotated)
    assert molt.restore_category(beta, str(tmp_path / "alias" / ".." / "home"), ".bak",
                                 rotated=rotated)

    assert (tmp_path / "home.bak" / "alpha" / "a.txt").read_text() == "old a"
    assert (tmp_path / "home.bak" / "beta" / "b.txt").read_text() == "old b"
    assert len(list(tmp_path.glob("home.bak_*"))) == 1


@pytest.mark.parametrize("name", [".", ".."])
def test_dot_archive_names_are_never_restored_or_captured(tmp_path, name):
    (tmp_path / f"{name}.tar.gz").write_bytes(b"")
    assert molt.snapshot_archives(tmp_path, {"tar": {name: {"root": "/x"}}}, ".tar.gz") == []

    with pytest.raises(SystemExit):
        molt.verify_capture_config({"tar": {name: {"root": "/x", "dirs": ["a"]}}}, "c.toml")


def test_e2e_restore_to_host_passes_disable_rollback(env, hosts):
    write_files(env.home, {".zshrc": "v1"})
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"])})
    assert env.run("capture", "-r", str(env.root)).returncode == 0

    result = env.run("restore", "-r", str(env.root), "--to", "user@web-01", "--disable-rollback")

    assert result.returncode == 0, result.stdout + result.stderr
    commands = hosts.ssh_commands("user@web-01")
    work_dir = work_dir_of(commands, "restore")
    assert child_command(
        work_dir, "restore", "--from", f"{work_dir}/snapshot", "-r", work_dir,
        "-t", "configs/restore.toml", "--disable-rollback",
    ) in commands
    # Its prompt got no answer, so nothing was restored; the final line claims no success
    assert result.stdout.endswith("\nRestore on user@web-01 finished; see its summary above\n")


def test_e2e_interrupted_restore_on_host_exits_130(env, hosts):
    write_files(env.home, {".zshrc": "v1"})
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"])})
    assert env.run("capture", "-r", str(env.root)).returncode == 0

    result = env.run(
        "restore", "-r", str(env.root), "--to", "user@web-01",
        extra_env={"FAKE_SSH_FAIL": "molt.py restore", "FAKE_SSH_FAIL_STATUS": "130"},
    )

    assert result.returncode == 130
    assert "Error: Restore on user@web-01 was interrupted" in result.stderr
    assert list(hosts.tmp("web-01").iterdir()) == []


def test_e2e_remote_capture_runs_under_a_private_umask(env, hosts):
    write_files(hosts.home("web-01"), {".zshrc": "on web-01"})
    write_configs(env.root, {
        "dotfiles": ("$HOME", [".zshrc"]),
        "empty": ("$HOME", [".nothing-here"]),
    })

    result = env.run("capture", "-r", str(env.root), "--from", "user@web-01")

    assert result.returncode == 0, result.stdout + result.stderr
    commands = hosts.ssh_commands("user@web-01")
    assert any(c.startswith("umask 077 && cd ") for c in commands)


# --- Round 3 P1: snapshots are private to the user --- #


def test_e2e_capture_writes_a_private_snapshot(env):
    write_files(env.home, {".zshrc": "v1"})
    write_files(env.root, {"scripts/sub/brew.sh": BREW_SCRIPT})
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"])})
    capture_toml = env.root / "configs" / "capture.toml"
    capture_toml.write_text(
        capture_toml.read_text() + '\n[scripts]\nafter = ["scripts/sub/brew.sh"]\n'
    )
    # A captures/ dir from before is left as it is
    captures = env.root / "captures"
    captures.mkdir()
    captures.chmod(0o755)

    result = env.run("capture", "-r", str(env.root), "--run-scripts")

    assert result.returncode == 0, result.stdout + result.stderr
    [snapshot] = captures.glob("*/*/*")
    assert stat_mode(captures) == 0o755
    for directory in (snapshot.parent.parent, snapshot.parent, snapshot):
        assert stat_mode(directory) == 0o700, directory
    assert sorted(p.name for p in snapshot.iterdir()) == [
        "Brewfile", "dotfiles.tar.gz", "snapshot.toml",
    ]
    for path in snapshot.iterdir():
        assert stat_mode(path) == 0o600, path

    # A new --to path gets private dirs too
    result = env.run("capture", "-r", str(env.root), "--to", str(env.tmp / "out" / "deep"))
    assert result.returncode == 0, result.stdout + result.stderr
    [snapshot] = (env.tmp / "out" / "deep").iterdir()
    for directory in (env.tmp / "out", env.tmp / "out" / "deep", snapshot):
        assert stat_mode(directory) == 0o700, directory
    assert {stat_mode(path) for path in snapshot.iterdir()} == {0o600}


def test_e2e_capture_to_host_is_private_there(env, hosts):
    write_files(env.home, {".zshrc": "v1"})
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"])})

    result = env.run("capture", "-r", str(env.root), "--to", "user@web-01")

    assert result.returncode == 0, result.stdout + result.stderr
    commands = hosts.ssh_commands("user@web-01")
    assert any(c.startswith("umask 077 && mkdir -p .molt/captures/") for c in commands)
    [snapshot] = hosts.home("web-01").glob(".molt/captures/*/*/*")
    directory = snapshot
    while directory != hosts.home("web-01"):
        assert stat_mode(directory) == 0o700, directory
        directory = directory.parent
    assert {stat_mode(path) for path in snapshot.iterdir()} == {0o600}


def test_e2e_capture_from_host_is_private_here(env, hosts):
    write_files(hosts.home("web-01"), {".zshrc": "on web-01"})
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"])})

    result = env.run("capture", "-r", str(env.root), "--from", "user@web-01")

    assert result.returncode == 0, result.stdout + result.stderr
    [snapshot] = (env.root / "captures").glob("*/*/*")
    for directory in (env.root / "captures", snapshot.parent.parent, snapshot.parent, snapshot):
        assert stat_mode(directory) == 0o700, directory
    assert {stat_mode(path) for path in snapshot.iterdir()} == {0o600}


# --- Round 3 P2: backups are moves, so they keep every attribute --- #


def set_xattr(path, value):
    """Set a test xattr on a path; return False where the platform or filesystem can't."""
    if hasattr(os, "setxattr"):
        try:
            os.setxattr(path, "user.molt-test", value.encode())
            return True
        except OSError:
            return False
    if sys.platform == "darwin" and shutil.which("xattr"):
        argv = ["xattr", "-w", "user.molt-test", value, str(path)]
        return subprocess.run(argv, capture_output=True).returncode == 0
    return False


def get_xattr(path):
    """Return a path's test xattr, or None."""
    if hasattr(os, "getxattr"):
        try:
            return os.getxattr(path, "user.molt-test").decode()
        except OSError:
            return None
    argv = ["xattr", "-p", "user.molt-test", str(path)]
    result = subprocess.run(argv, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


MTIME = 1_000_000_000


def old_files(home):
    """Give home an 'a' dir with a 0640 file (old mtime, an xattr) and a hardlink to it.

    Returns whether the xattr could be set.
    """
    write_files(home, {"a/f.txt": "old", "a/sub/g.txt": "g"})
    f = home / "a" / "f.txt"
    (home / "a" / "h.txt").hardlink_to(f)
    (home / "a" / "link").symlink_to("f.txt")
    f.chmod(0o640)
    has_xattr = set_xattr(f, "kept")
    os.utime(f, (MTIME, MTIME))
    return has_xattr


def test_backup_is_the_same_inode_with_every_attribute(tmp_path):
    src = tmp_path / "src"
    home = tmp_path / "home"
    write_files(src, {"a/f.txt": "new"})
    has_xattr = old_files(home)
    before = {name: (home / "a" / name).lstat() for name in ("f.txt", "sub", "link")}
    archive = make_archive(tmp_path, "cat", src, ["a"])

    assert molt.restore_category(archive, str(home), ".bak")

    backup = tmp_path / "home.bak" / "cat" / "a"
    for name, info in before.items():
        after = (backup / name).lstat()
        assert (after.st_dev, after.st_ino) == (info.st_dev, info.st_ino), name
    kept = (backup / "f.txt").lstat()
    assert stat.S_IMODE(kept.st_mode) == 0o640
    assert kept.st_mtime == MTIME
    assert (backup / "f.txt").samefile(backup / "h.txt")
    assert (backup / "link").is_symlink()
    if has_xattr:
        assert get_xattr(backup / "f.txt") == "kept"
    assert list_tree(home) == ["a", "a/f.txt"]
    assert (home / "a" / "f.txt").read_text() == "new"


def test_rollback_moves_the_same_inode_back(tmp_path, monkeypatch):
    src = tmp_path / "src"
    home = tmp_path / "home"
    write_files(src, {"a/new.txt": "new", "b/new.txt": "new"})
    has_xattr = old_files(home)
    write_files(home, {"b/orig.txt": "b"})
    before = {name: (home / name).lstat().st_ino for name in ("a", "a/f.txt", "b")}
    archive = make_archive(tmp_path, "cat", src, ["a", "b"])

    fail_on_extract(monkeypatch, "b", OSError("disk full"))

    assert not molt.restore_category(archive, str(home), ".bak")
    assert {name: (home / name).lstat().st_ino for name in before} == before
    assert stat_mode(home / "a" / "f.txt") == 0o640
    assert (home / "a" / "f.txt").stat().st_mtime == MTIME
    assert (home / "a" / "f.txt").samefile(home / "a" / "h.txt")
    if has_xattr:
        assert get_xattr(home / "a" / "f.txt") == "kept"


def cross_device_backups(monkeypatch):
    """Make every rename into or out of home.bak/ fail as it does across filesystems."""
    real_rename = os.rename

    def rename(src, dst, *args, **kwargs):
        if f"{os.sep}home.bak{os.sep}" in f"{src} {dst}":
            raise OSError(errno.EXDEV, os.strerror(errno.EXDEV))
        return real_rename(src, dst, *args, **kwargs)

    monkeypatch.setattr(molt.os, "rename", rename)


def test_backup_across_filesystems_is_a_copy_with_attributes(tmp_path, monkeypatch):
    src = tmp_path / "src"
    home = tmp_path / "home"
    write_files(src, {"a/f.txt": "new"})
    has_xattr = old_files(home)
    cross_device_backups(monkeypatch)
    archive = make_archive(tmp_path, "cat", src, ["a"])

    assert molt.restore_category(archive, str(home), ".bak")

    backup = tmp_path / "home.bak" / "cat" / "a"
    assert (backup / "f.txt").read_text() == "old"
    assert stat_mode(backup / "f.txt") == 0o640
    assert (backup / "f.txt").stat().st_mtime == MTIME
    assert (backup / "link").is_symlink()
    assert os.readlink(backup / "link") == "f.txt"
    # Python copies xattrs only where the os module can (Linux)
    if has_xattr and hasattr(os, "getxattr"):
        assert get_xattr(backup / "f.txt") == "kept"
    # The original is gone once the copy is complete
    assert list_tree(home) == ["a", "a/f.txt"]
    assert (home / "a" / "f.txt").read_text() == "new"


def test_rollback_across_filesystems_copies_back(tmp_path, monkeypatch):
    src = tmp_path / "src"
    home = tmp_path / "home"
    write_files(src, {"a/new.txt": "new", "b/new.txt": "new"})
    old_files(home)
    write_files(home, {"b/orig.txt": "b"})
    cross_device_backups(monkeypatch)
    archive = make_archive(tmp_path, "cat", src, ["a", "b"])

    fail_on_extract(monkeypatch, "b", OSError("disk full"))

    assert not molt.restore_category(archive, str(home), ".bak")
    assert list_tree(home) == [
        "a", "a/f.txt", "a/h.txt", "a/link", "a/sub", "a/sub/g.txt", "b", "b/orig.txt",
    ]
    assert stat_mode(home / "a" / "f.txt") == 0o640
    assert (home / "a" / "f.txt").stat().st_mtime == MTIME


def test_move_path_copies_only_across_filesystems(tmp_path, monkeypatch):
    write_files(tmp_path, {"src/f.txt": "x"})

    def rename(src, dst):
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(molt.os, "rename", rename)

    with pytest.raises(PermissionError):
        molt.move_path(tmp_path / "src", tmp_path / "bak" / "src")
    assert (tmp_path / "src" / "f.txt").exists()


# --- Round 3 P3: excludes and the macOS metadata filter --- #


def archive_names(archive):
    """Return the member names of a tar archive, sorted."""
    with tarfile.open(archive) as tar:
        return sorted(tar.getnames())


def test_capture_excludes_matching_members(tmp_path, monkeypatch):
    src = tmp_path / "src"
    write_files(src, {
        "proj/main.py": "x",
        "proj/node_modules/m/i.js": "x",
        "proj/debug.log": "x",
        "proj/sub/a.log": "x",
        ".cache/data": "x",
        "top.log": "x",
    })
    monkeypatch.setattr(molt, "__verbose__", True)
    exclude = ["node_modules", "*.log", ".cache/*"]
    paths = ["proj", ".cache", "top.log"]

    outdir = tmp_path / "out"
    outdir.mkdir()
    archive, lines, _ = molt.archive_create("cat", str(src), paths, outdir, exclude=exclude)

    assert archive_names(archive) == [".cache", "proj", "proj/main.py", "proj/sub"]
    assert lines[1:] == [
        "include: .cache",
        "include: proj",
        "exclude: .cache/data (pattern '.cache/*')",
        "exclude: proj/debug.log (pattern '*.log')",
        "exclude: proj/node_modules (pattern 'node_modules')",
        "exclude: proj/sub/a.log (pattern '*.log')",
        "exclude: top.log (pattern '*.log')",
    ]

    # A dry run shows the same lines, and writes nothing
    monkeypatch.setattr(molt, "__dry_run__", True)
    dry_out = tmp_path / "dry"
    _, dry_lines, _ = molt.archive_create("cat", str(src), paths, dry_out, exclude=exclude)
    assert dry_lines == lines
    assert not dry_out.exists()

    # Without --verbose there are no exclude lines
    monkeypatch.setattr(molt, "__verbose__", False)
    _, quiet_lines, _ = molt.archive_create("cat", str(src), paths, dry_out, exclude=exclude)
    assert quiet_lines[1:] == ["include: 2 paths"]


@pytest.mark.parametrize("exclude", ["*.log", [1], {"a": "b"}])
def test_exclude_must_be_an_array_of_patterns(capsys, exclude):
    config = {"tar": {"cat": {"root": "/x", "dirs": ["a"], "exclude": exclude}}}

    with pytest.raises(SystemExit):
        molt.verify_capture_config(config, "configs/capture.toml")
    assert (
        "Error: configs/capture.toml: the 'exclude' key in [tar.cat] must be an array "
        "of patterns"
    ) in capsys.readouterr().err


def test_e2e_capture_excludes_and_verbose_lists_them(env):
    write_files(env.home, {".config/app/settings": "v1", ".config/app/cache.log": "log"})
    write_configs(env.root, {"app": ("$HOME", [".config/app"])})
    capture_toml = env.root / "configs" / "capture.toml"
    capture_toml.write_text(capture_toml.read_text() + 'exclude = ["*.log"]\n')

    result = env.run("capture", "-r", str(env.root), "--verbose")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "    exclude: .config/app/cache.log (pattern '*.log')\n" in result.stdout
    [archive] = (env.root / "captures").glob("*/*/*/app.tar.gz")
    assert archive_names(archive) == [".config/app", ".config/app/settings"]

    result = env.run("capture", "-r", str(env.root), "--dry-run", "--verbose")
    assert "[DRY-RUN]     exclude: .config/app/cache.log (pattern '*.log')\n" in result.stdout

    result = env.run("capture", "-r", str(env.root), "--dry-run")
    assert "exclude:" not in result.stdout


def test_metadata_filter_skips_macos_files_at_any_depth(tmp_path, monkeypatch):
    src = tmp_path / "src"
    write_files(src, {
        "d/._x": "",
        "d/e/.DS_Store": "",
        "._top": "",
        ".DS_Store": "",
        "d/keep._x": "",
        "d/xDS_Store": "",
        "d/f.txt": "",
    })
    paths = ["d", "._top", ".DS_Store"]

    monkeypatch.setattr(molt.sys, "platform", "darwin")
    archive = make_archive(tmp_path, "mac", src, paths)
    assert archive_names(archive) == ["d", "d/e", "d/f.txt", "d/keep._x", "d/xDS_Store"]

    monkeypatch.setattr(molt.sys, "platform", "linux")
    archive = make_archive(tmp_path, "linux", src, paths)
    assert len(archive_names(archive)) == 9


# --- Round 3 P8: leftovers --- #


def test_remote_stop_command_stops_only_that_capture(tmp_path):
    if not shutil.which("pkill") or not shutil.which("bash"):
        pytest.skip("pkill and bash are needed")
    work = tmp_path / "molt-capture.Ab3d.f9h"
    other = tmp_path / "molt-capture.Ab3dXf9h"  # '.' in the pattern must not match 'X'
    procs = []
    try:
        for directory in (work, other):
            directory.mkdir()
            (directory / "molt.py").write_text("import time\ntime.sleep(60)\n")
            procs.append(subprocess.Popen(
                [sys.executable, "molt.py", "capture", "-r", str(directory), "-t", "x.toml"],
                cwd=directory,
            ))
        time.sleep(0.5)  # let both start

        command = f"{molt.remote_stop_command(work, 'capture')}; rm -rf {shlex.quote(str(work))}"
        result = subprocess.run(["bash", "-c", command], timeout=30)

        # The cleanup's own shell survives and removes the dir
        assert result.returncode == 0
        assert not work.exists()
        assert procs[0].wait(timeout=10) != 0
        assert procs[1].poll() is None
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


def test_sudo_restore_runs_python_without_user_site_packages(tmp_path, monkeypatch):
    cmds = []

    class FakePopen:
        def __init__(self, cmd):
            cmds.append(cmd)

        def wait(self):
            return 0

    monkeypatch.setattr(molt.subprocess, "Popen", FakePopen)
    args = argparse.Namespace(root_host=None)

    with pytest.raises(SystemExit):
        molt.sudo_restore(args, tmp_path, tmp_path, {"tar": {"archives": ["x"]}}, {})

    [cmd] = cmds
    assert cmd[cmd.index(sys.executable):][:3] == [sys.executable, "-s", str(molt.__script__)]


def test_sudo_restore_keeps_a_missing_archives_key(tmp_path, monkeypatch):
    configs = []

    class FakePopen:
        def __init__(self, cmd):
            configs.append(Path(cmd[cmd.index("-t") + 1]).read_text())

        def wait(self):
            return 0

    monkeypatch.setattr(molt.subprocess, "Popen", FakePopen)
    args = argparse.Namespace(root_host=None)

    with pytest.raises(SystemExit):
        molt.sudo_restore(args, tmp_path, tmp_path, {"tarball": {}}, {}, table="restore.tar")

    assert molt.tomllib.loads(configs[0]) == {
        "tar": {"archives": []},
        "molt": {"table": "restore.tar", "no_archives_key": True},
    }


MIGRATE_WITHOUT_ARCHIVES = (
    '[tarball]\nchecksum = "sha256"\n\n'
    '[capture.tar.dotfiles]\nroot = "$HOME"\nfiles = [".zshrc"]\n'
)
NO_ARCHIVES_KEY = "Skipping archive restore ([restore.tar] has no 'archives' key)\n"


def test_e2e_child_config_reports_a_missing_archives_key(env):
    write_files(env.home, {".zshrc": "v1"})
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"])})
    assert env.run("capture", "-r", str(env.root)).returncode == 0
    # What sudo_restore writes for a migrate.toml without [restore.tar] 'archives'
    (env.root / "configs" / "child.toml").write_text(molt.toml_dumps(
        molt.restore_child_config({"tarball": {}}, "restore.tar")
    ))

    result = env.run("restore", "-r", str(env.root), "-t", "configs/child.toml")

    assert result.returncode == 0, result.stdout + result.stderr
    assert NO_ARCHIVES_KEY in result.stdout
    assert "archives is empty" not in result.stdout


def test_e2e_migrate_to_host_reports_a_missing_archives_key(env, hosts):
    write_files(env.home, {".zshrc": "v1"})
    (env.root / "configs" / "migrate.toml").write_text(MIGRATE_WITHOUT_ARCHIVES)

    result = env.run("migrate", "-r", str(env.root), "--to", "user@web-01")

    assert result.returncode == 0, result.stdout + result.stderr
    assert NO_ARCHIVES_KEY in result.stdout
    assert "archives is empty" not in result.stdout


# --- Round 3 P4: encryption with age --- #

# A stand-in for the age CLI. 'Encrypting' writes a header naming the recipients and the
# data XORed with 0x5A, so the plaintext is not in the file; decrypting needs an
# identity 'AGE-SECRET-KEY-<X>' whose recipient 'age1<x>' is in the header. Each call is
# logged as JSON with the mode of the output's dir. FAKE_AGE_FAIL makes every call fail
FAKE_AGE = r'''
import json, os, sys

args = sys.argv[1:]
mode = out = identity = source = None
recipients = []
i = 0
while i < len(args):
    arg = args[i]
    if arg in ("-e", "-d"):
        mode = arg
    elif arg in ("-r", "-R", "-i", "-o"):
        i += 1
        if arg == "-r":
            recipients.append(args[i])
        elif arg == "-R":
            with open(args[i]) as f:
                recipients += [line.strip() for line in f if line.strip()]
        elif arg == "-i":
            identity = args[i]
        else:
            out = args[i]
    else:
        source = arg
    i += 1

with open(os.environ["FAKE_AGE_LOG"], "a") as f:
    out_dir = os.path.dirname(out)
    f.write(json.dumps({"argv": args, "out_dir_mode": os.stat(out_dir).st_mode & 0o777}) + "\n")
if os.environ.get("FAKE_AGE_FAIL"):
    print("age: error: fake failure", file=sys.stderr)
    sys.exit(1)

with open(source, "rb") as f:
    data = f.read()
if mode == "-e":
    header = ("FAKEAGE " + " ".join(recipients) + "\n").encode()
    with open(out, "wb") as f:
        f.write(header + bytes(b ^ 0x5A for b in data))
    sys.exit(0)

with open(identity) as f:
    key = f.read().strip()
header, _, body = data.partition(b"\n")
public = "age1" + key[len("AGE-SECRET-KEY-"):].lower()
if public not in header.decode().split()[1:]:
    print("age: error: no identity matched any of the recipients", file=sys.stderr)
    sys.exit(1)
with open(out, "wb") as f:
    f.write(bytes(b ^ 0x5A for b in body))
'''

ENCRYPTED_CAPTURE_TOML = """\
[tarball]
checksum = "sha256"
rollback = ".bak"

[age]
recipients = ["age1one"]
recipients_file = "$HOME/recipients.txt"

[tar.dotfiles]
root = "$HOME"
files = [".zshrc"]

[tar.secrets]
root = "$HOME"
dirs = [".secrets"]
encrypt = true
"""

IDENTITY_RESTORE_TOML = '[tar]\narchives = ["*"]\n\n[age]\nidentity = "~/key.txt"\n'


@pytest.fixture
def fake_age(env):
    """Put the fake age first on PATH; returns a function listing its logged calls."""
    (env.tmp / "fakeage.py").write_text(FAKE_AGE)
    shim = env.tmp / "fakeage.py"
    write_script(env.tmp / "bin" / "age", f'exec "{sys.executable}" "{shim}" "$@"')
    log = env.tmp / "age-calls.log"
    env.env["FAKE_AGE_LOG"] = str(log)
    if shutil.which("age", path=env.env["PATH"]) != str(env.tmp / "bin" / "age"):
        pytest.skip("fake age is not first on PATH")

    def calls():
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    return calls


def encrypted_setup(env, identity="AGE-SECRET-KEY-TWO"):
    """Files in HOME, a capture config that encrypts 'secrets', and a restore identity."""
    write_files(env.home, {
        ".zshrc": "zshrc v1",
        ".secrets/token": "top secret v1",
        "recipients.txt": "age1two\n",
        "key.txt": identity + "\n",
    })
    (env.root / "configs" / "capture.toml").write_text(ENCRYPTED_CAPTURE_TOML)
    (env.root / "configs" / "restore.toml").write_text(IDENTITY_RESTORE_TOML)


def option_values(argv, option):
    """Return the values that follow each `option` in an argv."""
    return [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == option]


@pytest.mark.parametrize("config, problem", [
    ({"tar": {"a": {"root": "/", "dirs": ["x"], "encrypt": True}}},
     "[tar.a] sets 'encrypt', but [age] has no 'recipients' or 'recipients_file' key"),
    ({"tar": {"a": {"root": "/", "dirs": ["x"], "encrypt": "yes"}}},
     "the 'encrypt' key in [tar.a] must be true or false"),
    ({"age": {"recipients": "age1x"}, "tar": {"a": {"root": "/", "dirs": ["x"]}}},
     "the 'recipients' key in [age] must be an array of age public keys"),
    ({"age": {"recipients_file": 3}, "tar": {"a": {"root": "/", "dirs": ["x"]}}},
     "the 'recipients_file' key in [age] must be a path"),
])
def test_capture_config_checks_encryption(capsys, config, problem):
    with pytest.raises(SystemExit):
        molt.verify_capture_config(config, "configs/capture.toml")
    assert f"Error: configs/capture.toml: {problem}" in capsys.readouterr().err


def test_capture_config_names_the_migrate_age_table(capsys):
    config = {"tar": {"a": {"root": "/", "dirs": ["x"], "encrypt": True}}}
    with pytest.raises(SystemExit):
        molt.verify_capture_config(config, "configs/migrate.toml", table="capture.tar")
    assert "[capture.tar.a] sets 'encrypt', but [capture.age] has no" in capsys.readouterr().err


@pytest.mark.parametrize("age", [{"recipients": ["age1x"]}, {"recipients_file": "~/r.txt"}])
def test_capture_config_accepts_either_kind_of_recipients(age):
    config = {"age": age, "tar": {"a": {"root": "/", "dirs": ["x"], "encrypt": True}}}
    molt.verify_capture_config(config, "configs/capture.toml")


def test_archive_category_strips_the_age_extension():
    assert molt.archive_category("secrets.tar.gz.age") == "secrets"
    assert molt.archive_category("plain.tar.age") == "plain"
    assert molt.archive_file("secrets", ".tar.gz", encrypted=True) == "secrets.tar.gz.age"


def test_e2e_capture_encrypts_with_age(env, fake_age):
    encrypted_setup(env)

    result = env.run("capture", "-r", str(env.root))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "\nEncrypting 1 archive...\n  ✓ 1 archive encrypted\n" in result.stdout
    snapshot = snapshot_dir(env)
    assert sorted(p.name for p in snapshot.iterdir()) == [
        "dotfiles.tar.gz", "secrets.tar.gz.age", "snapshot.toml",
    ]
    encrypted = snapshot / "secrets.tar.gz.age"
    assert b"top secret" not in encrypted.read_bytes()
    assert stat_mode(encrypted) == 0o600

    # snapshot.toml marks the archive, and its checksum is over the .age file
    tables = molt.tomllib.loads((snapshot / "snapshot.toml").read_text())["tar"]
    assert tables["secrets"]["encrypted"] is True
    assert tables["secrets"]["checksum"] == molt.calculate_file_checksum(encrypted)
    assert "encrypted" not in tables["dotfiles"]

    # age -e with both kinds of recipients, in the private staging dir
    [call] = fake_age()
    argv = call["argv"]
    assert argv[0] == "-e"
    assert option_values(argv, "-r") == ["age1one"]
    assert option_values(argv, "-R") == [str(env.home / "recipients.txt")]
    assert option_values(argv, "-o")[0].endswith("/secrets.tar.gz.age")
    assert argv[-1].endswith("/secrets.tar.gz")
    assert call["out_dir_mode"] == 0o700
    assert not Path(argv[-1]).exists()


def test_e2e_restore_decrypts_into_a_private_temp_dir(env, fake_age):
    encrypted_setup(env)
    assert env.run("capture", "-r", str(env.root)).returncode == 0
    write_files(env.home, {".zshrc": "zshrc v2", ".secrets/token": "changed"})

    result = env.run("restore", "-r", str(env.root))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "\nDecrypting 1 archive...\n  ✓ 1 archive decrypted\n" in result.stdout
    assert "  secrets.tar.gz (root: " in result.stdout
    assert "✓ Restore completed: 2 archives restored" in result.stdout
    assert (env.home / ".secrets" / "token").read_text() == "top secret v1"
    assert (env.home / ".zshrc").read_text() == "zshrc v1"

    decrypt = fake_age()[-1]
    assert decrypt["argv"][:3] == ["-d", "-i", str(env.home / "key.txt")]
    assert decrypt["argv"][-1] == str(snapshot_dir(env) / "secrets.tar.gz.age")
    assert decrypt["out_dir_mode"] == 0o700
    plain = Path(option_values(decrypt["argv"], "-o")[0])
    assert plain.name == "secrets.tar.gz"
    assert not plain.parent.exists()


def test_e2e_restore_without_an_identity_changes_nothing(env, fake_age):
    encrypted_setup(env)
    assert env.run("capture", "-r", str(env.root)).returncode == 0
    (env.root / "configs" / "restore.toml").write_text('[tar]\narchives = ["*"]\n')
    write_files(env.home, {".secrets/token": "changed"})

    result = env.run("restore", "-r", str(env.root))

    assert result.returncode == 1
    assert "Error: Cannot decrypt secrets.tar.gz.age: no age identity is set\n" in result.stderr
    assert "Set 'identity' in the restore config's [age] table" in result.stderr
    assert (env.home / ".secrets" / "token").read_text() == "changed"
    assert len(fake_age()) == 1  # only the capture's age -e


def test_e2e_restore_with_the_wrong_identity_changes_nothing(env, fake_age):
    encrypted_setup(env, identity="AGE-SECRET-KEY-OTHER")
    assert env.run("capture", "-r", str(env.root)).returncode == 0
    write_files(env.home, {".secrets/token": "changed"})

    result = env.run("restore", "-r", str(env.root))

    assert result.returncode == 1
    assert "  age: error: no identity matched any of the recipients\n" in result.stderr
    assert "Error: Decrypting secrets.tar.gz.age failed (exit status 1)\n" in result.stderr
    assert (env.home / ".secrets" / "token").read_text() == "changed"
    plain = Path(option_values(fake_age()[-1]["argv"], "-o")[0])
    assert not plain.parent.exists()


def test_e2e_missing_identity_file_is_an_error(env, fake_age):
    encrypted_setup(env)
    assert env.run("capture", "-r", str(env.root)).returncode == 0
    (env.home / "key.txt").unlink()

    result = env.run("restore", "-r", str(env.root))

    assert result.returncode == 1
    assert f"Error: Age identity not found: {env.home / 'key.txt'}\n" in result.stderr


def test_e2e_missing_age_names_the_archive(env, fake_age):
    encrypted_setup(env)
    no_age = {"PATH": str(env.tmp / "empty-bin")}

    result = env.run("capture", "-r", str(env.root), extra_env=no_age)

    assert result.returncode == 1
    assert "Error: Cannot encrypt secrets.tar.gz: age not found\n" in result.stderr
    assert "  Install age (brew install age, or apt install age)\n" in result.stderr
    assert "Creating" not in result.stdout
    assert not (env.root / "captures").exists()

    assert env.run("capture", "-r", str(env.root)).returncode == 0
    result = env.run("restore", "-r", str(env.root), extra_env=no_age)
    assert result.returncode == 1
    assert "Error: Cannot decrypt secrets.tar.gz.age: age not found\n" in result.stderr


def test_e2e_failed_encryption_saves_no_snapshot(env, fake_age):
    encrypted_setup(env)

    result = env.run("capture", "-r", str(env.root), extra_env={"FAKE_AGE_FAIL": "1"})

    assert result.returncode == 1
    assert "  age: error: fake failure\nError: Encrypting secrets.tar.gz failed (exit status 1)\n" \
        in result.stderr
    assert "Error: Capture failed: 1 of 1 archive could not be encrypted" in result.stderr
    assert not list(env.root.glob("captures/*/*/*"))
    plain = Path(fake_age()[0]["argv"][-1])
    assert not plain.parent.exists()  # the staging dir, with the plaintext, is gone


def test_e2e_encrypted_dry_runs_create_no_plaintext(env, fake_age):
    encrypted_setup(env)

    result = env.run("capture", "-r", str(env.root), "--dry-run")

    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        "[DRY-RUN] Encrypting 1 archive...\n"
        "[DRY-RUN]   run: age -e -r age1one -R " + str(env.home / "recipients.txt")
        + " -o '<staging dir>/"
    ) in result.stdout
    assert "secrets.tar.gz.age' '<staging dir>/" in result.stdout
    assert fake_age() == []
    assert not (env.root / "captures").exists()

    assert env.run("capture", "-r", str(env.root)).returncode == 0
    write_files(env.home, {".secrets/token": "changed"})
    before = tree_state(env.home)
    snapshot = snapshot_dir(env)

    result = env.run("restore", "-r", str(env.root), "--dry-run")

    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        f"[DRY-RUN]   run: age -d -i {env.home / 'key.txt'} "
        f"-o '<decrypted dir>/secrets.tar.gz' {snapshot / 'secrets.tar.gz.age'}\n"
    ) in result.stdout
    assert "[DRY-RUN]     encrypted; a dry run does not list its paths\n" in result.stdout
    assert "✓" not in result.stdout
    assert len(fake_age()) == 1  # the real capture's; the dry run ran nothing
    assert tree_state(env.home) == before


def test_e2e_encrypted_dry_run_without_backups_asks_by_name(env, fake_age):
    encrypted_setup(env)
    assert env.run("capture", "-r", str(env.root)).returncode == 0

    result = env.run("restore", "-r", str(env.root), "--dry-run", "--disable-rollback")

    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        "[DRY-RUN]     encrypted; a dry run does not list its paths\n"
        "[DRY-RUN]     Restore secrets.tar.gz? [y/N]: y (assumed in a dry run)\n"
    ) in result.stdout


def test_e2e_tampered_encrypted_archive_fails_verification(env, fake_age):
    encrypted_setup(env)
    assert env.run("capture", "-r", str(env.root)).returncode == 0
    encrypted = snapshot_dir(env) / "secrets.tar.gz.age"
    encrypted.write_bytes(encrypted.read_bytes() + b"x")

    result = env.run("restore", "-r", str(env.root), "--verbose")

    assert result.returncode == 1
    assert "  verify: secrets.tar.gz.age\n" in result.stdout
    assert "  secrets.tar.gz.age: checksum mismatch\n" in result.stderr
    assert len(fake_age()) == 1  # never decrypted


def test_e2e_unselected_encrypted_archive_needs_no_identity(env, fake_age):
    encrypted_setup(env)
    assert env.run("capture", "-r", str(env.root)).returncode == 0
    (env.root / "configs" / "restore.toml").write_text('[tar]\narchives = ["dotfiles"]\n')

    result = env.run("restore", "-r", str(env.root))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Decrypting" not in result.stdout
    assert "✓ Restore completed: 1 archive restored" in result.stdout


def test_restore_child_config_ships_only_the_identity_path():
    config = {"tar": {"archives": ["*"]}, "age": {"identity": "~/key.txt", "other": "x"}}
    assert molt.restore_child_config(config)["age"] == {"identity": "~/key.txt"}
    assert "age" not in molt.restore_child_config({"tar": {"archives": ["*"]}})


def test_split_migrate_config_keeps_both_age_tables():
    config = {
        "capture": {"tar": {}, "age": {"recipients": ["age1x"]}},
        "restore": {"tar": {"archives": ["*"]}, "age": {"identity": "~/key.txt"}},
    }
    capture_config, restore_config = molt.split_migrate_config(config, "migrate.toml")
    assert capture_config["age"] == {"recipients": ["age1x"]}
    assert restore_config["age"] == {"identity": "~/key.txt"}


def test_sudo_restore_expands_the_identity_path(tmp_path, monkeypatch):
    configs = []

    class FakePopen:
        def __init__(self, cmd):
            configs.append(Path(cmd[cmd.index("-t") + 1]).read_text())

        def wait(self):
            return 0

    monkeypatch.setattr(molt.subprocess, "Popen", FakePopen)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KEYS", str(tmp_path / "keys"))
    args = argparse.Namespace(root_host=None)
    restore_config = {"tar": {"archives": ["*"]}, "age": {"identity": "$KEYS/key.txt"}}

    with pytest.raises(SystemExit):
        molt.sudo_restore(args, tmp_path, tmp_path, restore_config, {})

    assert molt.tomllib.loads(configs[0])["age"] == {"identity": str(tmp_path / "keys/key.txt")}


def test_e2e_restore_to_host_decrypts_there_with_its_identity(env, hosts, fake_age):
    encrypted_setup(env)
    assert env.run("capture", "-r", str(env.root)).returncode == 0
    (env.home / "key.txt").unlink()  # the key is only on the host
    host_home = hosts.home("web-01")
    write_files(host_home, {"key.txt": "AGE-SECRET-KEY-TWO\n", ".secrets/token": "old"})

    result = env.run("restore", "-r", str(env.root), "--to", "user@web-01")

    assert result.returncode == 0, result.stdout + result.stderr
    assert (host_home / ".secrets" / "token").read_text() == "top secret v1"
    decrypt = fake_age()[-1]
    assert option_values(decrypt["argv"], "-i") == [str(host_home / "key.txt")]
    assert "key.txt" not in " ".join(line for line in hosts.calls() if line.startswith("rsync"))
    assert list(hosts.tmp("web-01").iterdir()) == []


requires_age = pytest.mark.skipif(
    not (shutil.which("age") and shutil.which("age-keygen")), reason="age is not installed"
)


@requires_age
def test_e2e_capture_and_restore_with_real_age(env):
    write_files(env.home, {".zshrc": "zshrc v1", ".secrets/token": "top secret v1"})
    key = env.home / "key.txt"
    subprocess.run(["age-keygen", "-o", str(key)], check=True, capture_output=True)
    public = subprocess.run(
        ["age-keygen", "-y", str(key)], check=True, capture_output=True, text=True
    ).stdout.strip()
    (env.home / "recipients.txt").write_text(public + "\n")
    config = ENCRYPTED_CAPTURE_TOML.replace('recipients = ["age1one"]\n', "")
    (env.root / "configs" / "capture.toml").write_text(config)
    (env.root / "configs" / "restore.toml").write_text(IDENTITY_RESTORE_TOML)

    result = env.run("capture", "-r", str(env.root))
    assert result.returncode == 0, result.stdout + result.stderr
    encrypted = snapshot_dir(env) / "secrets.tar.gz.age"
    assert encrypted.read_bytes().startswith(b"age-encryption.org/v1")

    write_files(env.home, {".secrets/token": "changed"})
    result = env.run("restore", "-r", str(env.root))
    assert result.returncode == 0, result.stdout + result.stderr
    assert (env.home / ".secrets" / "token").read_text() == "top secret v1"


def test_e2e_migrate_to_host_encrypts_and_decrypts_there(env, hosts, fake_age):
    write_files(env.home, {".secrets/token": "top secret v1"})
    host_home = hosts.home("web-01")
    write_files(host_home, {"key.txt": "AGE-SECRET-KEY-ONE\n"})
    (env.root / "configs" / "migrate.toml").write_text(
        '[tarball]\nchecksum = "sha256"\nrollback = ".bak"\n\n'
        '[capture.age]\nrecipients = ["age1one"]\n\n'
        '[capture.tar.secrets]\nroot = "$HOME"\ndirs = [".secrets"]\nencrypt = true\n\n'
        '[restore.tar]\narchives = ["*"]\n\n'
        '[restore.age]\nidentity = "~/key.txt"\n'
    )

    result = env.run("migrate", "-r", str(env.root), "--to", "user@web-01")

    assert result.returncode == 0, result.stdout + result.stderr
    assert (host_home / ".secrets" / "token").read_text() == "top secret v1"
    assert [call["argv"][0] for call in fake_age()] == ["-e", "-d"]


# --- Round 3 P5: molt list and molt prune --- #


def days_ago(days):
    """Return the captures/YYYY/MM-DD path part for a day `days` before today."""
    day = datetime.now() - timedelta(days=days)
    return day.strftime("%Y/%m-%d")


def make_snapshot(root, rel, tables=None, rollback=".bak", size=100):
    """Write a snapshot dir under root/captures/<rel> with snapshot.toml and archives.

    tables maps archive names to their [tar.<name>] tables (default: one 'dotfiles'
    archive with root $HOME). Each archive file holds `size` bytes.
    """
    if tables is None:
        tables = {"dotfiles": {"root": "$HOME"}}
    path = root / "captures" / rel
    path.mkdir(parents=True)
    tarball = {"checksum": "sha256"}
    if rollback:
        tarball["rollback"] = rollback
    snapshot = {"tarball": tarball, "tar": {}}
    for name, table in tables.items():
        filename = molt.archive_file(name, ".tar.gz", table.get("encrypted", False))
        (path / filename).write_bytes(b"x" * size)
        snapshot["tar"][name] = dict(table, checksum="0" * 64)
    (path / "snapshot.toml").write_text(molt.toml_dumps(snapshot))
    return path


def rotated_backup(home, stamp, content="old"):
    """Create a rotated backup dir <home>.bak_<stamp> with one file."""
    path = Path(f"{home}.bak_{stamp}")
    write_files(path, {"dotfiles/.zshrc": content})
    return path


def columns_text(rows):
    """Join rows of cells into lines, to compare column output cell by cell."""
    return ["  ".join(row) for row in rows]


def test_prune_select_keeps_what_any_rule_keeps():
    today = datetime.now().date()
    items = [(today - timedelta(days=age), age) for age in (0, 10, 40, 100)]
    cutoff = today - timedelta(days=30)
    assert molt.prune_select(items, 2, None) == [40, 100]
    assert molt.prune_select(items, None, cutoff) == [40, 100]
    assert molt.prune_select(items, 3, cutoff) == [100]
    assert molt.prune_select(items, 1, today - timedelta(days=5)) == [10, 40, 100]
    assert molt.prune_select(items, 0, None) == [0, 10, 40, 100]


def test_e2e_list_shows_snapshots_newest_first_and_backup_dirs(env):
    make_snapshot(env.root, f"{days_ago(40)}/aaaaaaa", size=2048)
    make_snapshot(env.root, f"{days_ago(0)}/bbbbbbb", tables={
        "dotfiles": {"root": "$HOME"},
        "secrets": {"root": "$HOME", "encrypted": True},
        "system": {"root": str(env.tmp / "sysroot")},
    })
    write_files(env.home.parent / "home.bak", {"dotfiles/.zshrc": "current"})
    rotated = rotated_backup(env.home, "20250102_030405")
    # Not a snapshot (no snapshot.toml) and not a backup dir: never listed
    write_files(env.root / "captures" / "2024" / "01-01" / "stray", {"file": "x"})
    write_files(env.tmp, {"home.bak_notes/file": "x"})

    result = env.run("list", "-r", str(env.root))

    assert result.returncode == 0, result.stderr
    captures = env.root.resolve() / "captures"
    snapshots = [toml.parent for toml in captures.glob("*/*/*/snapshot.toml")]
    [new, old] = sorted(snapshots, key=lambda path: path.name, reverse=True)
    today = days_ago(0).replace("/", "-")
    assert [re.split(" {2,}", line) for line in result.stdout.split("\n")] == [
        re.split(" {2,}", line) for line in columns_text([
            [f"Snapshots in {captures}, newest first:"],
            [f"  {today}", "bbbbbbb", "3 archives", molt.size_shown(new), "1 encrypted"],
            [f"  {days_ago(40).replace('/', '-')}", "aaaaaaa", "1 archive", molt.size_shown(old)],
            [""],
            ["Backup dirs, newest first per root:"],
            [f"  {env.home}.bak", "7 B", "current"],
            [f"  {rotated}", "3 B", "2025-01-02 03:04:05"],
            [""],
            ["2 snapshots, 2 backup dirs"],
            [""],
        ])
    ]
    assert "2.2 KB" in molt.size_shown(old)


def test_e2e_list_without_snapshots(env):
    result = env.run("list", "-r", str(env.root))

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"No snapshots found in {env.root.resolve() / 'captures'}\n"


@pytest.mark.parametrize("command", [["list"], ["prune", "--keep", "1", "--yes"]])
def test_e2e_list_and_prune_need_a_local_molt_root(env, hosts, command):
    result = env.run(*command, "-r", "user@web-01:.molt")

    assert result.returncode == 1
    assert result.stderr == (
        f"Error: molt.py {command[0]} works only with a local molt root, "
        "not 'user@web-01:.molt'\n"
        f"  Run molt.py {command[0]} on user@web-01 itself\n"
    )
    assert hosts.calls() == []


def test_e2e_prune_needs_a_rule(env):
    result = env.run("prune", "-r", str(env.root))

    assert result.returncode == 1
    assert "Error: Nothing to prune by: pass --keep N, --older-than DAYS, or both" in result.stderr
    assert result.stdout == ""


def test_e2e_prune_asks_before_deleting(env):
    old = make_snapshot(env.root, f"{days_ago(40)}/aaaaaaa")
    older = make_snapshot(env.root, f"{days_ago(400)}/ccccccc")
    new = make_snapshot(env.root, f"{days_ago(0)}/bbbbbbb")
    stray = env.root / "captures" / "2001" / "01-01" / "stray"
    write_files(stray, {"file": "x"})

    result = env.run("prune", "-r", str(env.root), "--keep", "1", answers="n\n")

    assert result.returncode == 0, result.stderr
    assert (
        f"\nChecking 3 snapshots...\n"
        f"  delete: {days_ago(40)}/aaaaaaa ({molt.size_shown(old)})\n"
        f"  delete: {days_ago(400)}/ccccccc ({molt.size_shown(older)})\n"
        "  keep: 1 snapshot\n"
        "  Delete 2 snapshots? [y/N]: n\n"
        "  skip: declined\n"
        "\n"
        "Prune completed: nothing deleted\n"
    ) in result.stdout
    assert old.exists() and older.exists() and new.exists()

    result = env.run("prune", "-r", str(env.root), "--keep", "1", answers="y\n")

    assert result.returncode == 0, result.stderr
    assert f"  ✓ {days_ago(40)}/aaaaaaa deleted\n" in result.stdout
    assert result.stdout.endswith("\n✓ Prune completed: 2 snapshots deleted\n")
    assert not old.exists() and not older.exists() and new.exists()
    # Date dirs left empty are removed; captures/ and other dirs stay
    assert not old.parent.exists() and not older.parent.parent.exists()
    assert (env.root / "captures").is_dir() and (stray / "file").exists()


def test_e2e_prune_older_than_with_keep(env):
    snapshots = {}
    for age in (0, 10, 40, 100):
        snapshots[age] = make_snapshot(env.root, f"{days_ago(age)}/s{age:06d}")

    result = env.run("prune", "-r", str(env.root), "--older-than", "30", "--keep", "3", "--yes")

    assert result.returncode == 0, result.stderr
    assert "Delete" not in result.stdout  # --yes: no prompt
    assert [age for age, path in snapshots.items() if path.exists()] == [0, 10, 40]

    result = env.run("prune", "-r", str(env.root), "--older-than", "5", "--yes")
    assert result.returncode == 0, result.stderr
    assert [age for age, path in snapshots.items() if path.exists()] == [0]

    result = env.run("prune", "-r", str(env.root), "--older-than", "5", "--yes")
    assert result.stdout.endswith("\nPrune completed: nothing to delete\n")


def test_e2e_prune_backups_only_with_backups(env):
    make_snapshot(env.root, f"{days_ago(0)}/bbbbbbb")
    current = env.home.parent / "home.bak"
    write_files(current, {"dotfiles/.zshrc": "current"})
    newest = rotated_backup(env.home, datetime.now().strftime("%Y%m%d_%H%M%S"))
    old = rotated_backup(env.home, "20200101_000000")
    old_again = rotated_backup(env.home, "20200101_000000_1")

    result = env.run("prune", "-r", str(env.root), "--keep", "1", "--yes")

    assert result.returncode == 0, result.stderr
    assert "\nSkipping rotated backup dirs (pass --backups to prune them)\n" in result.stdout
    assert all(path.exists() for path in (current, newest, old, old_again))

    result = env.run("prune", "-r", str(env.root), "--keep", "1", "--backups", answers="y\n")

    assert result.returncode == 0, result.stderr
    assert (
        "\nChecking 3 rotated backup dirs...\n"
        f"  delete: {old_again} (3 B)\n"
        f"  delete: {old} (3 B)\n"
        "  keep: 2 backup dirs\n"
        "  Delete 0 snapshots and 2 backup dirs? [y/N]: y\n"
    ) in result.stdout
    assert result.stdout.endswith("✓ Prune completed: 0 snapshots and 2 backup dirs deleted\n")
    assert current.exists() and newest.exists()
    assert not old.exists() and not old_again.exists()

    # The current backup dir is never deleted, whatever the rules say
    result = env.run("prune", "-r", str(env.root), "--keep", "0", "--backups", "--yes")
    assert result.returncode == 0, result.stderr
    assert current.exists() and not newest.exists()


def test_e2e_prune_dry_run_changes_nothing(env):
    make_snapshot(env.root, f"{days_ago(40)}/aaaaaaa")
    make_snapshot(env.root, f"{days_ago(0)}/bbbbbbb")
    rotated_backup(env.home, "20200101_000000")
    before = tree_state(env.tmp)

    result = env.run("prune", "-r", str(env.root), "--older-than", "30", "--backups", "--dry-run")

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("[DRY-RUN] Starting prune (no changes will be made)\n")
    assert "[DRY-RUN]   delete: " in result.stdout
    assert (
        "[DRY-RUN]   Delete 1 snapshot and 1 backup dir? [y/N]: y (assumed in a dry run)\n"
        "[DRY-RUN]\n"
        "[DRY-RUN] Dry run completed; no changes were made\n"
    ) in result.stdout
    assert "✓" not in result.stdout and "Deleting" not in result.stdout
    assert tree_state(env.tmp) == before


@unprivileged
def test_e2e_prune_never_uses_sudo_for_root_owned_backup_dirs(env):
    locked = env.tmp / "locked"
    make_snapshot(env.root, f"{days_ago(0)}/bbbbbbb", tables={"sys": {"root": str(locked / "sys")}})
    rotated = rotated_backup(locked / "sys", "20200101_000000")
    locked.chmod(0o555)
    try:
        result = env.run("prune", "-r", str(env.root), "--older-than", "1", "--backups", "--yes")
    finally:
        locked.chmod(0o755)

    assert result.returncode == 0, result.stderr
    assert result.stderr == (
        f"Warning: {rotated} needs root to delete; it is kept\n"
        f"  Run: sudo rm -rf {rotated}\n"
    )
    assert result.stdout.endswith("\nPrune completed: nothing to delete\n")
    assert rotated.exists()
    assert not env.sudo_marker.exists()


def test_e2e_prune_never_follows_symlinked_snapshots(env):
    outside = make_snapshot(env.tmp / "elsewhere", "2001/01-01/aaaaaaa")
    year = env.root / "captures" / "2001"
    year.parent.mkdir(parents=True)
    year.symlink_to(outside.parent.parent)
    make_snapshot(env.root, f"{days_ago(0)}/bbbbbbb")

    result = env.run("prune", "-r", str(env.root), "--keep", "0", "--yes")

    assert result.returncode == 0, result.stderr
    assert "Checking 1 snapshot..." in result.stdout
    assert (outside / "snapshot.toml").exists() and year.is_symlink()


def test_e2e_list_after_a_real_capture_and_restore(env):
    write_files(env.home, {".zshrc": "v1"})
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"])})
    assert env.run("capture", "-r", str(env.root)).returncode == 0
    assert env.run("restore", "-r", str(env.root)).returncode == 0
    assert env.run("restore", "-r", str(env.root)).returncode == 0

    result = env.run("list", "-r", str(env.root))

    assert result.returncode == 0, result.stderr
    assert f"  {env.home}.bak " in result.stdout
    assert f"  {env.home}.bak_" in result.stdout
    assert result.stdout.endswith("\n1 snapshot, 2 backup dirs\n")


# --- Final review: signals and private moves --- #


def test_sigint_deferred_holds_ctrl_c_until_the_block_ends():
    reached_end = []
    with pytest.raises(KeyboardInterrupt):
        with molt.sigint_deferred():
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(0.1)
            reached_end.append(True)
    assert reached_end == [True]


def test_capture_move_into_captures_runs_under_a_private_umask(tmp_path, monkeypatch):
    home = tmp_path / "home"
    root = tmp_path / "moltroot"
    (root / "configs").mkdir(parents=True)
    write_files(home, {".zshrc": "v1"})
    write_configs(root, {"dotfiles": ("$HOME", [".zshrc"])})
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(molt, "__dry_run__", False)
    monkeypatch.setattr(molt, "__verbose__", False)
    monkeypatch.setattr(molt.sys, "argv", ["molt", "capture", "-r", str(root)])
    umasks = []
    real_move = molt.shutil.move

    def move(src, dst):
        current = os.umask(0o022)
        os.umask(current)
        umasks.append(current)
        return real_move(src, dst)

    monkeypatch.setattr(molt.shutil, "move", move)
    previous = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP)}
    try:
        molt.main()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)

    assert umasks == [0o077]
    assert list(root.glob("captures/*/*/*/snapshot.toml"))


@unprivileged
def test_e2e_sigterm_during_capture_removes_its_staging_dir(env):
    (env.root / "scripts").mkdir()
    (env.root / "scripts" / "slow.sh").write_text("sleep 20\n")
    write_files(env.home, {".zshrc": "v1"})
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"])})
    capture_toml = env.root / "configs" / "capture.toml"
    capture_toml.write_text(
        capture_toml.read_text() + '\n[scripts]\nbefore = ["scripts/slow.sh"]\n'
    )
    tmpdir = env.tmp / "tmpdir"
    tmpdir.mkdir()

    proc = subprocess.Popen(
        [sys.executable, str(MOLT), "capture", "-r", str(env.root), "--run-scripts"],
        cwd=env.tmp, env=dict(env.env, TMPDIR=str(tmpdir)),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        deadline = time.time() + 20
        while not list(tmpdir.iterdir()) and time.time() < deadline:
            time.sleep(0.1)
        assert list(tmpdir.iterdir()), "the staging dir never appeared"
        time.sleep(0.5)
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    assert proc.returncode != 0
    assert list(tmpdir.iterdir()) == []  # the plaintext staging dir is gone


# --- molt install --- #

# A fake brew: answers the queries from files in $FAKE_STATE, logs every call (with the
# no-upgrade env) to $FAKE_LOG, and 'installs' a Brewfile's brew/cask/tap lines into the
# state files, unless $FAKE_BUNDLE_FAIL is set
FAKE_BREW = r"""#!/bin/bash
echo "brew $* [no_install_upgrade=${HOMEBREW_NO_INSTALL_UPGRADE:-}]" >> "$FAKE_LOG"
state="$FAKE_STATE"
case "$1" in
    tap) cat "$state/taps" 2>/dev/null ;;
    list) [ "$2" = --cask ] && kind=casks || kind=formulae; cat "$state/$kind" 2>/dev/null ;;
    leaves) cat "$state/formulae" 2>/dev/null ;;
    info) echo '{"casks": []}' ;;
    bundle)
        file="${4#--file=}"
        cp "$file" "$state/last-brewfile"
        [ -n "${FAKE_BUNDLE_FAIL:-}" ] && { echo "bundle failed" >&2; exit 1; }
        sed -n 's/^brew "\(.*\)"$/\1/p' "$file" >> "$state/formulae"
        sed -n 's/^cask "\(.*\)"$/\1/p' "$file" >> "$state/casks"
        sed -n 's/^tap "\(.*\)"$/\1/p' "$file" >> "$state/taps"
        ;;
esac
exit 0
"""


def fake_tool(bin_dir, name, body):
    """Write an executable fake command into bin_dir."""
    path = bin_dir / name
    path.write_text(body)
    path.chmod(0o755)
    return path


@pytest.fixture
def inst(tmp_path, monkeypatch):
    """An in-process 'molt install' with fake tools only: PATH is the fakes plus the
    system dirs, the extra install dirs are off, and apps are looked for in a temp dir."""
    home = tmp_path / "home"
    home.mkdir()
    root = tmp_path / "root"
    (root / "configs").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    apps = tmp_path / "Applications"
    apps.mkdir()
    log = tmp_path / "calls.log"

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin")
    monkeypatch.setenv("FAKE_STATE", str(state))
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setattr(molt, "INSTALL_PATH_DIRS", [])
    monkeypatch.setattr(molt, "INSTALL_APP_DIRS", [str(apps)])
    monkeypatch.setattr(molt, "__dry_run__", False)
    monkeypatch.setattr(molt, "__verbose__", False)
    monkeypatch.setitem(molt._reported, "errors", 0)
    for name in ("brew", "mise", "npm", "cargo", "uv", "code", "mas"):
        if shutil.which(name, path=os.environ["PATH"]) and not (bin_dir / name).exists():
            # A real one in /usr/bin or /bin would answer instead of a missing tool
            pytest.skip(f"a real {name} is on the system PATH")

    def config(text):
        (root / "configs" / "install.toml").write_text(text)

    def installed(**files):
        for name, lines in files.items():
            (state / name).write_text("".join(f"{line}\n" for line in lines))

    def run(*args):
        monkeypatch.setattr(molt.sys, "argv", ["molt", "install", "-r", str(root), *args])
        try:
            molt.main()
            return 0
        except SystemExit as e:
            return e.code

    def calls():
        return log.read_text().splitlines() if log.exists() else []

    return argparse.Namespace(
        tmp=tmp_path, home=home, root=root, bin=bin_dir, state=state, apps=apps,
        config=config, installed=installed, run=run, calls=calls,
    )


def test_install_status_shows_missing_and_not_in_config(inst, capfd):
    fake_tool(inst.bin, "brew", FAKE_BREW)
    inst.installed(formulae=["jj", "wget"], casks=["ghostty"], taps=["homebrew/core"])
    inst.config('[brew]\nformulae = ["jj", "ripgrep"]\ncasks = ["ghostty"]\n')

    assert inst.run("--status") == 0
    out = capfd.readouterr().out.splitlines()
    assert "Checking brew..." in out
    assert "  installed: 2 of 3" in out
    assert "  missing: formula 'ripgrep'" in out
    assert "  not in config: formula 'wget'" in out
    assert not any("homebrew/core" in line for line in out)  # every Homebrew has it
    assert out[-1] == "Status: 1 item missing, 1 not in the config"
    assert not any(" bundle " in call for call in inst.calls())


def test_install_dry_run_installs_nothing(inst, capfd):
    fake_tool(inst.bin, "brew", FAKE_BREW)
    inst.installed(formulae=["jj"])
    inst.config('[brew]\nformulae = ["jj", "ripgrep"]\n')

    assert inst.run("--dry-run") == 0
    out = capfd.readouterr().out
    assert "[DRY-RUN] Installing brew..." in out
    assert "[DRY-RUN]   missing: formula 'ripgrep'" in out
    assert "run: brew bundle install --no-upgrade '--file=<staging dir>/Brewfile'" in out
    assert not any(" bundle " in call for call in inst.calls())
    assert out.rstrip().endswith("Dry run completed; no changes were made")


def test_install_bundles_only_missing_items_without_upgrades(inst, capfd):
    fake_tool(inst.bin, "brew", FAKE_BREW)
    inst.installed(formulae=["jj"], taps=["powershell/tap"])
    inst.config(
        '[brew]\ntaps = ["powershell/tap"]\nformulae = ["jj", "ripgrep", "powershell/tap/pwsh"]\n'
        'casks = ["Ghostty"]\n'
    )

    assert inst.run() == 0
    brewfile = (inst.state / "last-brewfile").read_text()
    assert brewfile == 'brew "ripgrep"\nbrew "powershell/tap/pwsh"\ncask "Ghostty"\n'
    [bundle] = [call for call in inst.calls() if " bundle " in call]
    assert bundle.startswith("brew bundle install --no-upgrade --file=")
    assert bundle.endswith("[no_install_upgrade=1]")

    out = capfd.readouterr().out.splitlines()
    assert "  ✓ formula 'ripgrep' installed" in out
    assert "  ✓ cask 'Ghostty' installed" in out  # cask names match in any case
    assert out[-1] == "✓ Install completed: 3 items installed"
    assert not list(inst.tmp.glob("**/*.Brewfile"))


def test_install_reports_what_is_still_missing_and_exits_1(inst, capfd, monkeypatch):
    fake_tool(inst.bin, "brew", FAKE_BREW)
    monkeypatch.setenv("FAKE_BUNDLE_FAIL", "1")
    inst.config('[brew]\nformulae = ["ripgrep"]\n')

    assert inst.run() == 1
    captured = capfd.readouterr()
    assert "Error: Cannot install 1 item from brew" in captured.err
    assert "  formula 'ripgrep'" in captured.err
    assert "brew bundle exit status 1" in captured.err
    assert captured.out.rstrip().endswith("(1 error reported above)")


def test_install_skips_a_section_whose_tool_is_missing(inst, capfd):
    inst.config('[cargo]\ncrates = ["cargo-edit"]\n')

    assert inst.run() == 0
    out = capfd.readouterr().out.splitlines()
    assert "Skipping cargo (cargo not found; 1 item not installed)" in out
    assert out[-1] == "Install completed: 0 items installed, 1 skipped"


def test_custom_items_are_shell_quoted(inst, capfd):
    made = inst.tmp / "made"
    made.mkdir()
    inst.config(
        "[[custom]]\n"
        'name = "things"\n'
        "items = [\"a b\", \"$(touch pwned)\", \"c'd\"]\n"
        f'check = "test -e {made}/{{item}}"\n'
        f'install = "touch {made}/{{item}}"\n'
    )

    assert inst.run() == 0
    assert sorted(p.name for p in made.iterdir()) == ["$(touch pwned)", "a b", "c'd"]
    assert not (inst.tmp / "pwned").exists()
    out = capfd.readouterr().out
    assert "✓ things 'a b' installed" in out

    # Installed now, so a second run has nothing to do
    assert inst.run() == 0
    out = capfd.readouterr().out.splitlines()
    assert "Skipping things (3 items already installed)" in out
    assert out[-1] == "✓ Install completed: nothing was missing"


def test_custom_list_command_replaces_the_checks(inst, capfd):
    fake_tool(inst.bin, "lister", "#!/bin/sh\nprintf 'one\\nextra\\n'\n")
    inst.config(
        '[[custom]]\nname = "listed"\nitems = ["one", "two"]\n'
        'list = "lister"\ninstall = "false"\nrequires = "lister"\n'
    )

    assert inst.run("--status") == 0
    out = capfd.readouterr().out.splitlines()
    assert "  installed: 1 of 2" in out
    assert "  missing: listed 'two'" in out
    assert "  not in config: listed 'extra'" in out


def test_custom_requires_skips_the_entry(inst, capfd):
    inst.config('[[custom]]\nname = "needs"\nrequires = "no-such-tool"\ncheck = "false"\n'
                'install = "touch should-not-exist"\n')

    assert inst.run() == 0
    out = capfd.readouterr().out
    assert "Skipping needs (no-such-tool not found; 1 item not installed)" in out
    assert not (inst.tmp / "should-not-exist").exists()


def test_manual_entries_are_only_listed(inst, capfd):
    (inst.apps / "Here.app").mkdir()
    inst.config(
        '[[manual]]\nname = "Here"\napp = "Here.app"\n\n'
        '[[manual]]\nname = "Slack"\napp = "Slack.app"\nsource = "Self Service"\n'
    )

    assert inst.run() == 0
    out = capfd.readouterr().out.splitlines()
    assert "Left to install by hand..." in out
    assert "  'Slack': Self Service" in out
    assert not any("'Here'" in line for line in out)
    assert out[-1] == "Install completed: 0 items installed, 1 left to install by hand"


@pytest.mark.parametrize("text, message", [
    ('[brw]\nformulae = ["jj"]\n', "unknown table [brw]"),
    ('[brew]\nformula = ["jj"]\n', "unknown key 'formula' in [brew]"),
    ('[brew]\nformulae = "jj"\n', "the 'formulae' key in [brew] must be an array of names"),
    ('[mas.apps]\nXcode = "497799835"\n', "[mas.apps] must map app names to App Store ids"),
    ('[mise.tools]\nnode = 22\n', "'node' in [mise.tools] must be a version"),
    ('[[custom]]\nname = "x"\ninstall = "true"\n', "[[custom]] 'x' needs a 'check' command"),
    ('[[custom]]\nname = "x"\nitems = ["a"]\ninstall = "true"\n', "needs a 'check' or 'list'"),
    ('[[custom]]\nname = "x"\ncheck = "true"\ninstall = "true"\n\n'
     '[[custom]]\nname = "x"\ncheck = "true"\ninstall = "true"\n',
     "two [[custom]] entries are named 'x'"),
    ('[[manual]]\nname = "x"\n', "[[manual]] 'x' needs an 'app' or a 'check'"),
    ('[custom]\nname = "x"\n', "custom must be an array of tables: [[custom]]"),
    ('[install]\norder = ["brew", "pip"]\n', "unknown section 'pip' in [install] order"),
])
def test_install_config_errors(inst, capfd, text, message):
    inst.config(text)
    assert inst.run("--status") == 1
    assert message in capfd.readouterr().err


def test_install_order_only_and_skip(inst):
    config = {
        "install": {"order": ["custom", "brew"]},
        "brew": {}, "cargo": {},
        "custom": [{"name": "c", "check": "true", "install": "true"}],
    }
    sections = [g.section for g in molt.install_groups(config, "install.toml")]
    assert sections == ["custom", "brew", "mas", "mise", "npm", "cargo", "uv", "vscode"]
    only = molt.install_groups(config, "install.toml", only=["cargo", "custom"])
    assert [g.section for g in only] == ["custom", "cargo"]
    skipped = molt.install_groups(config, "install.toml", skip=["custom", "brew"])
    assert "custom" not in [g.section for g in skipped]


def test_install_section_list_rejects_unknown_names():
    assert molt.install_section_list("brew, mas") == ["brew", "mas"]
    with pytest.raises(argparse.ArgumentTypeError, match="unknown section 'pip'"):
        molt.install_section_list("brew,pip")


@pytest.mark.parametrize("versions, requested, expected", [
    (["3.12.13"], "3.12", True),
    (["3.12.13"], "3.1", False),
    (["3.12.13"], "3.12.13", True),
    ([], "latest", False),
    (["24.1.0"], "latest", True),
    (["22.11.0"], "lts", True),
])
def test_mise_satisfied(versions, requested, expected):
    assert molt.mise_satisfied(versions, requested) is expected


def test_mise_use_sets_every_version_of_a_tool(inst):
    fake_tool(inst.bin, "mise", r"""#!/bin/bash
echo "mise $*" >> "$FAKE_LOG"
[ "$1" = ls ] && echo '{"python": [{"version": "3.12.1"}], "go": [{"version": "1.22"}]}'
exit 0
""")
    inst.config('[mise.tools]\npython = ["3.12", "3.11"]\nnode = "latest"\n')

    assert inst.run() == 1  # the fake never installs, so both are still missing
    assert "mise use --global python@3.12 python@3.11 node@latest" in inst.calls()


def test_brewfile_lists_brew_and_mas_and_keeps_existing_files(inst, capfd):
    inst.config(
        '[brew]\ntaps = ["a/b"]\nformulae = ["jj"]\ncasks = ["utm"]\n\n'
        '[mas.apps]\nXcode = 497799835\n"Odd #{name}" = 1\n'
    )
    target = inst.tmp / "Brewfile"

    assert inst.run("--brewfile", str(target)) == 0
    assert target.read_text() == (
        'tap "a/b"\nbrew "jj"\ncask "utm"\nbrew "mas"\n'
        'mas "Xcode", id: 497799835\nmas "Odd \\#{name}", id: 1\n'
    )
    assert inst.run("--brewfile", str(target)) == 1
    assert "Pass --force to replace it" in capfd.readouterr().err
    assert inst.run("--brewfile", str(target), "--force") == 0


def test_draft_is_a_valid_config_of_what_is_installed(inst, capfd):
    fake_tool(inst.bin, "brew", FAKE_BREW)
    fake_tool(inst.bin, "cargo", "#!/bin/sh\nprintf 'cargo-edit v0.1.0:\\n    cargo-add\\n'\n")
    fake_tool(inst.bin, "npm", "#!/bin/sh\necho '{\"dependencies\": {\"npm\": {}, \"tsx\": {}}}'\n")
    inst.installed(formulae=["jj"], casks=["utm"], taps=["homebrew/core", "a/b"])
    (inst.apps / "Handmade.app").mkdir()
    target = inst.tmp / "draft.toml"

    assert inst.run("--draft", str(target)) == 0
    draft = tomllib.loads(target.read_text())
    assert draft["brew"] == {"taps": ["a/b"], "formulae": ["jj"], "casks": ["utm"]}
    assert draft["cargo"] == {"crates": ["cargo-edit"]}
    assert draft["npm"] == {"packages": ["tsx"]}
    assert draft["manual"] == [{"name": "Handmade", "app": "Handmade.app"}]
    molt.install_groups(draft, "draft.toml")  # it verifies as an install config

    # Installing from the draft finds everything in place
    assert inst.run("-t", str(target), "--status") == 0
    assert capfd.readouterr().out.rstrip().endswith(
        "Everything in the config is installed, and nothing else"
    )


def test_folder_app_picks_the_main_app(tmp_path):
    for name in ["Cisco Secure Client - DART.app", "Cisco Secure Client.app",
                 "Uninstall Cisco Secure Client.app"]:
        (tmp_path / "Cisco" / name).mkdir(parents=True)
    assert molt.folder_app(tmp_path / "Cisco").name == "Cisco Secure Client.app"


def test_install_refuses_to_run_as_root(inst, capfd, monkeypatch):
    monkeypatch.setattr(molt.os, "geteuid", lambda: 0)
    inst.config("")
    assert inst.run("--status") == 1
    assert "Cannot run molt.py install as root" in capfd.readouterr().err


def test_capture_config_install_table_is_verified():
    config = {"tar": {"a": {"root": "/", "files": ["x"]}}, "install": {"draft": "yes"}}
    with pytest.raises(SystemExit):
        molt.verify_capture_config(config, "capture.toml")
    config["install"] = {"draft": True}
    molt.verify_capture_config(config, "capture.toml")


def test_e2e_capture_saves_an_install_draft(env):
    # The draft queries only the fakes here: they come first on PATH
    for name in ("mise", "npm", "cargo", "uv", "code"):
        fake_tool(env.tmp / "bin", name, "#!/bin/sh\nexit 1\n")
    state = env.tmp / "state"
    state.mkdir()
    (state / "formulae").write_text("jj\n")
    fake_tool(env.tmp / "bin", "brew", FAKE_BREW)
    write_files(env.home, {".zshrc": "v1"})
    write_configs(env.root, {"dotfiles": ("$HOME", [".zshrc"])})
    capture_toml = env.root / "configs" / "capture.toml"
    capture_toml.write_text(capture_toml.read_text() + "\n[install]\ndraft = true\n")

    result = env.run(
        "capture", "-r", str(env.root),
        extra_env={"FAKE_STATE": str(state), "FAKE_LOG": str(env.tmp / "calls.log")},
    )
    assert result.returncode == 0, result.stderr
    [draft] = list((env.root / "captures").glob("*/*/*/install.toml"))
    assert stat_mode(draft) == 0o600
    assert tomllib.loads(draft.read_text())["brew"]["formulae"] == ["jj"]
    assert "✓ install.toml written" in result.stdout


def test_resolve_root_falls_back_to_the_old_snap_root(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".snap").mkdir()
    assert molt.resolve_root(molt.DEFAULT_ROOT) == (tmp_path / ".snap").resolve()
    err = capsys.readouterr().err
    assert "the root from before snap was renamed molt" in err
    assert f"mv {tmp_path}/.snap {tmp_path}/.molt" in err

    # Once ~/.molt exists, it wins
    (tmp_path / ".molt").mkdir()
    assert molt.resolve_root(molt.DEFAULT_ROOT) == (tmp_path / ".molt").resolve()
