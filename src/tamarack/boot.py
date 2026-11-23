# SPDX-License-Identifier: GPL-3.0-or-later
"""Bringing the pool back at boot: the Tamarack lines in /etc/integritytab.

Every layer partition sits under dm-integrity, and nothing in the kernel
opens those mappings by itself. systemd does, from ``/etc/integritytab``:
``systemd-integritysetup-generator`` turns each line into a unit that opens
the mapping early in boot. Then mdadm's udev rules assemble the arrays from
their superblocks, LVM activates the ``tamarack`` volume group, and
``srv-tamarack.mount`` mounts the volume.

Partitions are named by GPT partition UUID, which never changes when a
drive moves to another port. The engine rewrites its own lines after every
completed job and at start, and leaves every other line in the file alone.
"""

from __future__ import annotations

import re

from .commands import integrity_device_name
from .layout import Layout

INTEGRITYTAB = "/etc/integritytab"
BEGIN = "# --- Tamarack (written by tamarack-engine; do not edit) ---"
END = "# --- end of Tamarack ---"
OPTIONS = "integrity-algorithm=crc32c"
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


class BootError(ValueError):
    pass


def members(layout: Layout) -> list[tuple[int, int]]:
    """(drive index, partition number) of every layer partition, in order."""
    return sorted({(m.drive, m.partition) for layer in layout.layers for m in layer.members})


def lines(layout: Layout, partuuids: dict[tuple[int, int], str]) -> list[str]:
    """One integritytab line per layer partition: name, device, no key, options."""
    out = []
    for drive, number in members(layout):
        uuid = partuuids.get((drive, number), "")
        if not _UUID.fullmatch(uuid):
            device = layout.drives[drive].device
            raise BootError(f"no partition UUID for partition {number} of {device}")
        name = integrity_device_name(drive, number)
        out.append(f"{name} PARTUUID={uuid.lower()} - {OPTIONS}")
    return out


def merge(existing: str, ours: list[str]) -> str:
    """The file with our block replaced (or added, or removed if ``ours`` is empty)."""
    kept: list[str] = []
    inside = False
    for line in existing.splitlines():
        if line == BEGIN:
            inside = True
        elif line == END:
            inside = False
        elif not inside:
            kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    if ours:
        if kept:
            kept.append("")
        kept += [BEGIN, *ours, END]
    return "\n".join(kept) + "\n" if kept else ""
