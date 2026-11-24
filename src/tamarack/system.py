# SPDX-License-Identifier: GPL-3.0-or-later
"""The drives the running system lives on, from /proc and /sys.

A drive that holds the operating system must never join the pool, even
when nothing on it looks "in use" to UDisks2: an LVM physical volume or a
LUKS container under the root filesystem, or the second device of a
multi-device btrfs root, shows no mount point of its own. So the engine
starts from the system's mount points and swap, and follows each one down
through device-mapper, md and partitions (``/sys/class/block/X/slaves``
and the partition's parent) to whole drives.

Results are device numbers (``os.makedev``), the same as UDisks2's
``Block.DeviceNumber``, so they compare exactly whatever path names the
drive.
"""

from __future__ import annotations

import os

# Where the system lives. /srv/tamarack (the pool) is deliberately absent.
SYSTEM_MOUNTS = frozenset(
    {"/", "/boot", "/boot/efi", "/efi", "/sysroot", "/usr", "/var", "/etc", "/home"}
)


def _read(path: str) -> str:
    try:
        with open(path, encoding="ascii", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""


def _unescape(field: str) -> str:
    """mountinfo escapes space, tab, newline and backslash as octal."""
    out, i = [], 0
    while i < len(field):
        digits = field[i + 1 : i + 4]
        if field[i] == "\\" and len(digits) == 3 and all(c in "01234567" for c in digits):
            out.append(chr(int(digits, 8)))
            i += 4
        else:
            out.append(field[i])
            i += 1
    return "".join(out)


def system_sources(root: str = "/") -> set[tuple[str, str]]:
    """(filesystem type, source device path) of every system mount, plus swap."""
    found: set[tuple[str, str]] = set()
    for line in _read(os.path.join(root, "proc/self/mountinfo")).splitlines():
        before, _, after = line.partition(" - ")
        fields, tail = before.split(), after.split()
        if len(fields) < 5 or len(tail) < 2:
            continue
        if _unescape(fields[4]) in SYSTEM_MOUNTS:
            found.add((tail[0], _unescape(tail[1])))
    for line in _read(os.path.join(root, "proc/swaps")).splitlines()[1:]:
        if line.split():
            found.add(("swap", line.split()[0]))
    return found


def _block_name(root: str, source: str) -> str:
    """The kernel name (sda2, dm-3, nvme0n1p1) of a device path, or ""."""
    if not source.startswith("/dev/"):
        return ""
    name = os.path.basename(os.path.realpath(os.path.join(root, source.lstrip("/"))))
    return name if os.path.isdir(os.path.join(root, "sys/class/block", name)) else ""


def _whole_disks(root: str, name: str, seen: set[str]) -> set[str]:
    """Whole drives under block device ``name``: through slaves and partitions."""
    if name in seen:
        return set()
    seen.add(name)
    node = os.path.join(root, "sys/class/block", name)
    slaves_dir = os.path.join(node, "slaves")
    try:
        slaves = os.listdir(slaves_dir)
    except OSError:
        slaves = []
    if slaves:
        disks: set[str] = set()
        for slave in slaves:
            disks |= _whole_disks(root, slave, seen)
        return disks
    if os.path.exists(os.path.join(node, "partition")):
        parent = os.path.basename(os.path.dirname(os.path.realpath(node)))
        return _whole_disks(root, parent, seen)
    return {name}


def _btrfs_devices(root: str, name: str) -> set[str]:
    """Every device of the btrfs filesystem that ``name`` belongs to."""
    base = os.path.join(root, "sys/fs/btrfs")
    try:
        filesystems = os.listdir(base)
    except OSError:
        return {name}
    for fsid in filesystems:
        try:
            members = set(os.listdir(os.path.join(base, fsid, "devices")))
        except OSError:
            continue
        if name in members:
            return members
    return {name}


def _dev_number(root: str, name: str) -> int | None:
    text = _read(os.path.join(root, "sys/class/block", name, "dev"))
    major, _, minor = text.partition(":")
    if not (major.isdigit() and minor.isdigit()):
        return None
    return os.makedev(int(major), int(minor))


def system_disks(root: str = "/") -> set[int]:
    """Device numbers of every whole drive the running system uses."""
    disks: set[str] = set()
    for fstype, source in system_sources(root):
        name = _block_name(root, source)
        if not name:
            continue
        names = _btrfs_devices(root, name) if fstype == "btrfs" else {name}
        for member in names:
            disks |= _whole_disks(root, member, set())
    return {n for n in (_dev_number(root, d) for d in disks) if n is not None}
