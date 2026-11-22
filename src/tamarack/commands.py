# SPDX-License-Identifier: GPL-3.0-or-later
"""Render a Plan as the shell commands that would build it.

This module only produces text. Nothing here runs a command: execution,
the step journal and resume logic come later, behind a mandatory dry run.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

from .model import Layer, Level, Plan
from .units import MiB

VG_NAME = "tamarack"
LV_NAME = "data"
MOUNT_POINT = "/srv/tamarack"
FS_LABEL = "tamarack"
META_PARTLABEL = "tk-meta"
LINUX_RAID_TYPE = "fd00"
LINUX_FS_TYPE = "8300"


@dataclass(frozen=True)
class Partition:
    drive: int  # index into plan.drives
    number: int  # GPT partition number
    size: int  # bytes
    label: str  # GPT partition label
    layer: int  # layer index, 0 for the metadata partition


def partition_path(device: str, number: int) -> str:
    """Path of partition ``number`` on ``device``.

    by-id / by-path links use ``-partN``; devices ending in a digit
    (nvme0n1, mmcblk0) use ``pN``; everything else (sda) appends N.
    """
    if device.startswith("/dev/disk/"):
        return f"{device}-part{number}"
    if device[-1].isdigit():
        return f"{device}p{number}"
    return f"{device}{number}"


def partitions(plan: Plan) -> list[Partition]:
    parts: list[Partition] = []
    next_number = {i: 2 for i in range(len(plan.drives))}
    for i in range(len(plan.drives)):
        parts.append(Partition(i, 1, 16 * MiB, META_PARTLABEL, 0))
    for layer in plan.used_layers:
        copies = 2 if layer.copies_on_same_drive else 1
        for drive in layer.members:
            for copy in range(copies):
                suffix = "ab"[copy] if copies == 2 else ""
                parts.append(
                    Partition(
                        drive,
                        next_number[drive],
                        layer.slice_size,
                        f"tk-l{layer.index}{suffix}",
                        layer.index,
                    )
                )
                next_number[drive] += 1
    return parts


def integrity_name(part: Partition) -> str:
    return f"tk-d{part.drive + 1}-p{part.number}"


def md_path(layer: Layer) -> str:
    return f"/dev/md/tk-l{layer.index}"


def _q(value: str) -> str:
    return shlex.quote(value)


def render(plan: Plan) -> list[str]:
    """Return the command lines, with comments, that would build the plan."""
    parts = partitions(plan)
    lines: list[str] = [
        "# tamarack dry run: nothing below has been executed.",
        "# DESTRUCTIVE: these commands erase every listed drive.",
        "set -euo pipefail",
        "",
        "# 1. Partition each drive",
    ]
    for i, drive in enumerate(plan.drives):
        dev = _q(drive.device)
        lines.append(f"sgdisk --zap-all {dev}")
        for part in (p for p in parts if p.drive == i):
            code = LINUX_FS_TYPE if part.layer == 0 else LINUX_RAID_TYPE
            lines.append(
                f"sgdisk -n {part.number}:0:+{part.size // MiB}M "
                f"-t {part.number}:{code} -c {part.number}:{part.label} {dev}"
            )
        lines.append(f"partprobe {dev}")

    lines += [
        "",
        "# 2. dm-integrity on every layer partition "
        "(journal mode is integritysetup's default; bitmap mode stays off)",
    ]
    for part in (p for p in parts if p.layer):
        path = _q(partition_path(plan.drives[part.drive].device, part.number))
        lines.append(f"integritysetup format --batch-mode --integrity crc32c {path}")
        lines.append(f"integritysetup open --integrity crc32c {path} {integrity_name(part)}")

    lines += ["", "# 3. One md array per layer"]
    for layer in plan.used_layers:
        members = " ".join(
            f"/dev/mapper/{integrity_name(p)}" for p in parts if p.layer == layer.index
        )
        level = layer.level.value.removeprefix("raid")
        if layer.level is Level.RAID5:
            safety = "--consistency-policy=ppl"  # closes the RAID5 write hole
        else:
            safety = "--bitmap=internal"  # fast resync after a crash
        lines.append(
            f"mdadm --create {md_path(layer)} --run --metadata=1.2 "
            f"--level={level} --raid-devices={layer.member_count} "
            f"{safety} {members}"
        )

    mds = " ".join(md_path(layer) for layer in plan.used_layers)
    lines += [
        "",
        "# 4. LVM joins the layers into one volume",
        f"pvcreate {mds}",
        f"vgcreate {VG_NAME} {mds}",
        f"lvcreate --yes --extents 100%FREE --name {LV_NAME} {VG_NAME}",
        "",
        "# 5. Btrfs on top",
        f"mkfs.btrfs --label {FS_LABEL} --metadata dup /dev/{VG_NAME}/{LV_NAME}",
        f"mkdir -p {MOUNT_POINT}",
        f"mount -o compress=zstd:1,noatime /dev/{VG_NAME}/{LV_NAME} {MOUNT_POINT}",
        f"btrfs subvolume create {MOUNT_POINT}/shares",
        f"btrfs subvolume create {MOUNT_POINT}/snapshots",
        "",
        "# 6. Layout metadata to the tk-meta partitions (not implemented yet)",
    ]
    return lines
