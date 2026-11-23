# SPDX-License-Identifier: GPL-3.0-or-later
"""Drives as UDisks2 sees them.

The engine never probes block devices itself: UDisks2 already tracks every
drive, its size, its WWN and serial, and what is using it. ``find_drive``
works on the result of one ``GetManagedObjects`` call, so the rules can be
tested with plain dictionaries; ``UDisksDrives`` makes that call over GDBus.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .engine import DriveInfo, InvalidArgument, NoSuchDrive
from .identity import from_udisks

BUS_NAME = "org.freedesktop.UDisks2"
ROOT = "/org/freedesktop/UDisks2"
BLOCK = "org.freedesktop.UDisks2.Block"
DRIVE = "org.freedesktop.UDisks2.Drive"
PARTITION = "org.freedesktop.UDisks2.Partition"
FILESYSTEM = "org.freedesktop.UDisks2.Filesystem"
SWAPSPACE = "org.freedesktop.UDisks2.Swapspace"
ENCRYPTED = "org.freedesktop.UDisks2.Encrypted"
PHYSICAL_VOLUME = "org.freedesktop.UDisks2.PhysicalVolume"  # UDisks2 LVM2 module

Objects = Mapping[str, Mapping[str, Mapping[str, Any]]]


def _path(value: Iterable[int] | bytes) -> str:
    """A UDisks2 ``ay`` path (NUL-terminated bytes) as a string."""
    return bytes(value).rstrip(b"\0").decode("utf-8", errors="replace")


def _paths(block: Mapping[str, Any]) -> set[str]:
    paths = {_path(block.get("Device", b""))}
    paths |= {_path(s) for s in block.get("Symlinks", [])}
    return paths - {""}


def _in_use(interfaces: Mapping[str, Mapping[str, Any]]) -> str:
    """Why a block is in use, or "" if it is not."""
    block = interfaces[BLOCK]
    if interfaces.get(FILESYSTEM, {}).get("MountPoints"):
        return "has a mounted file system"
    if interfaces.get(SWAPSPACE, {}).get("Active"):
        return "is active swap"
    if block.get("MDRaidMember", "/") != "/":
        return "is part of an md array"
    if interfaces.get(ENCRYPTED, {}).get("CleartextDevice", "/") != "/":
        return "is an unlocked encrypted volume"
    if PHYSICAL_VOLUME in interfaces:
        return "is an LVM physical volume"
    return ""


def find_drive(objects: Objects, device: str, unused: bool) -> DriveInfo:
    """The whole drive at ``device`` in a ``GetManagedObjects`` result."""
    matches = [(p, i) for p, i in objects.items() if BLOCK in i and device in _paths(i[BLOCK])]
    if not matches:
        raise NoSuchDrive(f"{device}: no such block device")
    path, interfaces = matches[0]
    block = interfaces[BLOCK]
    if PARTITION in interfaces:
        raise InvalidArgument(f"{device} is a partition; give the whole drive")
    drive_path = block.get("Drive", "/")
    if drive_path == "/" or DRIVE not in objects.get(drive_path, {}):
        raise InvalidArgument(f"{device} is not a physical drive")
    if block.get("ReadOnly"):
        raise InvalidArgument(f"{device} is read-only")
    drive = objects[drive_path][DRIVE]
    if unused:
        # The drive itself and every partition on it must be idle.
        members = [(path, interfaces)] + [
            (p, i) for p, i in objects.items() if i.get(PARTITION, {}).get("Table") == path
        ]
        for member_path, member in members:
            why = _in_use(member)
            if why:
                name = _path(member[BLOCK].get("Device", b"")) or member_path
                raise InvalidArgument(f"{device} is in use: {name} {why}")
    identity = from_udisks(str(drive.get("WWN", "")), str(drive.get("Serial", "")))
    return DriveInfo(device=device, size=int(block.get("Size", 0)), identity=identity)


def find_partition_uuids(objects: Objects, device: str) -> dict[int, str]:
    """GPT partition number -> partition UUID for the drive at ``device``."""
    tables = [p for p, i in objects.items() if BLOCK in i and device in _paths(i[BLOCK])]
    if not tables:
        raise NoSuchDrive(f"{device}: no such block device")
    uuids = {}
    for interfaces in objects.values():
        part = interfaces.get(PARTITION, {})
        if part.get("Table") == tables[0] and part.get("UUID"):
            uuids[int(part["Number"])] = str(part["UUID"])
    return uuids


def find_present(objects: Objects, devices: Iterable[str]) -> dict[str, str]:
    """Identity of the whole drive at each of ``devices`` now; absent paths are left out."""
    present = {}
    for device in devices:
        try:
            info = find_drive(objects, device, unused=False)
        except (NoSuchDrive, InvalidArgument):
            continue
        if info.identity:
            present[device] = info.identity
    return present


def find_meta_partitions(objects: Objects) -> list[str]:
    """Device paths of every partition named tk-meta, as UDisks2 reports them."""
    from .commands import META_PARTLABEL

    found = []
    for interfaces in objects.values():
        if interfaces.get(PARTITION, {}).get("Name") == META_PARTLABEL and BLOCK in interfaces:
            device = _path(interfaces[BLOCK].get("Device", b""))
            if device:
                found.append(device)
    return sorted(found)


class UDisksDrives:
    """``DriveSource`` backed by UDisks2 on the given GDBus connection."""

    def __init__(self, connection: Any, timeout_ms: int = 10_000) -> None:
        self.connection = connection
        self.timeout_ms = timeout_ms

    def objects(self) -> Objects:
        from gi.repository import Gio, GLib

        try:
            reply = self.connection.call_sync(
                BUS_NAME,
                ROOT,
                "org.freedesktop.DBus.ObjectManager",
                "GetManagedObjects",
                None,
                GLib.VariantType("(a{oa{sa{sv}}})"),
                Gio.DBusCallFlags.NONE,
                self.timeout_ms,
                None,
            )
        except GLib.Error as exc:
            raise NoSuchDrive(f"UDisks2 is not available: {exc.message}") from exc
        objects: Objects = reply.unpack()[0]
        return objects

    def lookup(self, device: str, unused: bool) -> DriveInfo:
        return find_drive(self.objects(), device, unused)

    def present(self, devices: list[str]) -> dict[str, str]:
        return find_present(self.objects(), devices)

    def partition_uuids(self, device: str) -> dict[int, str]:
        return find_partition_uuids(self.objects(), device)


class DriveRecords:
    """``MetaSource``: tk-meta partitions found by UDisks2, read by tamarack-meta."""

    def __init__(self, drives: UDisksDrives) -> None:
        self.drives = drives

    def partitions(self) -> list[str]:
        return find_meta_partitions(self.drives.objects())

    def read(self, device: str) -> str:
        from .runner import read_meta

        return read_meta(device)
