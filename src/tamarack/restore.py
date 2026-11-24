# SPDX-License-Identifier: GPL-3.0-or-later
"""Browse snapper's snapshots of the shares, and restore from them.

A restore never overwrites anything: the file or folder comes back next to
the original as ``name (from 2026-11-20 14.00).ext``. The live shares are
changed by NFS (and later SMB) clients, so every path is walked one
component at a time with ``O_NOFOLLOW`` from a directory file descriptor:
a symlink anywhere on the way, in the snapshot or in the live tree, stops
the restore instead of leading the engine, which runs as root, somewhere
else. New files are made with ``O_EXCL``, so an existing name is never
written through. Symlinks inside a restored folder are copied as symlinks.

Data is cloned where the filesystem can (``FICLONE`` on btrfs, so a restore
takes no extra space until it is changed) and copied otherwise. Owner,
mode (without set-user-ID and set-group-ID on files), times and ``user.*``
extended attributes come along; mode and times are set before the owner,
so the engine never needs CAP_FOWNER.
"""

from __future__ import annotations

import errno
import fcntl
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from typing import Any

FICLONE = 0x40049409  # linux/fs.h: _IOW(0x94, 9, int)
MAX_PATH = 4096
MAX_ENTRIES = 5000  # a folder listing stops here and says so
MAX_ATTEMPTS = 100  # " 2" .. " 100" after the restored name
NAME_MAX = 255  # bytes
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class RestoreError(ValueError):
    pass


def split_path(path: str) -> list[str]:
    """A path inside the shares as its components; "" is the top."""
    if len(path) > MAX_PATH or "\0" in path:
        raise RestoreError("not a path inside the shares")
    parts = [p for p in path.split("/") if p]
    if path.startswith("/") or any(p in (".", "..") for p in parts):
        raise RestoreError("not a path inside the shares")
    if parts and parts[0] == ".snapshots":
        raise RestoreError("snapshots can't be restored from inside a snapshot")
    return parts


@contextmanager
def _opened(fd: int) -> Iterator[int]:
    try:
        yield fd
    finally:
        os.close(fd)


def _walk(root: str, parts: list[str]) -> int:
    """An fd for root/parts, refusing a symlink or non-folder on the way."""
    fd = os.open(root, DIR_FLAGS)
    try:
        for part in parts:
            try:
                child = os.open(part, DIR_FLAGS, dir_fd=fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise RestoreError(f"{part} is not a folder") from exc
                if exc.errno == errno.ENOENT:
                    raise RestoreError(f"{part} is not there") from exc
                raise
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def _kind(mode: int) -> str:
    if stat.S_ISDIR(mode):
        return "folder"
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISLNK(mode):
        return "link"
    return "other"


def list_folder(snapshot_root: str, parts: list[str]) -> dict[str, Any]:
    """The entries of one folder in a snapshot: folders first, then by name."""
    with _opened(_walk(snapshot_root, parts)) as fd:
        entries: list[dict[str, Any]] = []
        truncated = False
        for name in os.listdir(fd):
            if len(entries) >= MAX_ENTRIES:
                truncated = True
                break
            if not parts and name == ".snapshots":
                continue
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            entries.append(
                {
                    "name": name,
                    "kind": _kind(st.st_mode),
                    "size": st.st_size if stat.S_ISREG(st.st_mode) else 0,
                    "modified": int(st.st_mtime),
                }
            )
    entries.sort(key=lambda e: (e["kind"] != "folder", e["name"].lower(), e["name"]))
    return {"entries": entries, "truncated": truncated}


def restored_name(name: str, label: str, attempt: int, is_folder: bool) -> str:
    """``photo (from 2026-11-20 14.00).jpg``, with " 2", " 3"… on later attempts."""
    stem, ext = (name, "") if is_folder else os.path.splitext(name)
    tag = f" (from {label}{'' if attempt == 1 else f' {attempt}'})"
    room = NAME_MAX - len((tag + ext).encode())
    stem = stem.encode()[:room].decode(errors="ignore")
    return stem + tag + ext


def _copy_xattrs(src: int, dst: int) -> None:
    try:
        names = os.listxattr(src)
    except OSError:
        return
    for attr in names:
        if attr.startswith("user."):
            # A filesystem without user xattrs: the data matters more.
            with suppress(OSError):
                os.setxattr(dst, attr, os.getxattr(src, attr))


def _copy_data(src: int, dst: int, size: int) -> None:
    try:
        fcntl.ioctl(dst, FICLONE, src)
        return
    except OSError:
        pass  # another filesystem, or a file btrfs won't clone: copy it
    copied = 0
    while copied < size:
        n = os.copy_file_range(src, dst, size - copied)
        if n == 0:
            break
        copied += n


def _finish(fd: int, st: os.stat_result, is_folder: bool) -> None:
    """Mode and times first, then the owner (see the module docstring)."""
    mode = stat.S_IMODE(st.st_mode)
    if not is_folder:
        mode &= ~(stat.S_ISUID | stat.S_ISGID)
    os.fchmod(fd, mode)
    os.utime(fd, ns=(st.st_atime_ns, st.st_mtime_ns))
    os.fchown(fd, st.st_uid, st.st_gid)


def _copy_entry(src_dir: int, name: str, dst_dir: int, new_name: str) -> None:
    st = os.stat(name, dir_fd=src_dir, follow_symlinks=False)
    if stat.S_ISREG(st.st_mode):
        src = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=src_dir)
        with _opened(src):
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
            dst = os.open(new_name, flags, 0o600, dir_fd=dst_dir)
            with _opened(dst):
                _copy_data(src, dst, st.st_size)
                _copy_xattrs(src, dst)
                _finish(dst, st, is_folder=False)
    elif stat.S_ISDIR(st.st_mode):
        os.mkdir(new_name, 0o700, dir_fd=dst_dir)
        with (
            _opened(os.open(name, DIR_FLAGS, dir_fd=src_dir)) as src,
            _opened(os.open(new_name, DIR_FLAGS, dir_fd=dst_dir)) as dst,
        ):
            for child in sorted(os.listdir(src)):
                _copy_entry(src, child, dst, child)
            _copy_xattrs(src, dst)
            _finish(dst, st, is_folder=True)
    elif stat.S_ISLNK(st.st_mode):
        os.symlink(os.readlink(name, dir_fd=src_dir), new_name, dir_fd=dst_dir)
        os.chown(new_name, st.st_uid, st.st_gid, dir_fd=dst_dir, follow_symlinks=False)
    # Sockets, pipes and device nodes are left out: nothing to restore.


def _live_folder(live_root: str, snapshot_fd_root: str, parts: list[str]) -> int:
    """The live folder for ``parts``; missing folders are made like the snapshot's."""
    fd = os.open(live_root, DIR_FLAGS)
    try:
        for i, part in enumerate(parts):
            try:
                child = os.open(part, DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                with _opened(_walk(snapshot_fd_root, parts[: i + 1])) as src:
                    st = os.fstat(src)
                os.mkdir(part, 0o700, dir_fd=fd)
                child = os.open(part, DIR_FLAGS, dir_fd=fd)
                _finish(child, st, is_folder=True)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise RestoreError(f"{part} in the share is not a folder any more") from exc
                raise
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def restore(snapshot_root: str, live_root: str, parts: list[str], label: str) -> str:
    """Copy ``parts`` from the snapshot next to the original; the new relative path."""
    if not parts:
        raise RestoreError("choose a file or folder to restore, not the whole snapshot")
    *folder, name = parts
    with _opened(_walk(snapshot_root, folder)) as src_dir:
        try:
            st = os.stat(name, dir_fd=src_dir, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise RestoreError(f"{name} is not in this snapshot") from exc
        if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
            raise RestoreError(f"{name} is not a file or folder")
        is_folder = stat.S_ISDIR(st.st_mode)
        with _opened(_live_folder(live_root, snapshot_root, folder)) as dst_dir:
            for attempt in range(1, MAX_ATTEMPTS + 1):
                new_name = restored_name(name, label, attempt, is_folder)
                try:
                    os.stat(new_name, dir_fd=dst_dir, follow_symlinks=False)
                except FileNotFoundError:
                    _copy_entry(src_dir, name, dst_dir, new_name)
                    return "/".join([*folder, new_name])
    raise RestoreError(f"{name} has been restored from this snapshot too many times")
