# SPDX-License-Identifier: GPL-3.0-or-later
"""Plan changes to an existing layout: the Drobo "just add a drive" path.

Supported so far: one drive -> two drives.

A single drive holds two mirrored copies on itself (partitions 2 and 3).
When a second drive arrives:

1. The new drive gets the same two-slice partition layout.
2. ``mdadm --replace`` copies the drive-1 partition-3 copy onto the new
   drive. The old copy stays in the array until the new one is complete,
   so protection never drops below what it was.
3. The freed partition 3 on drive 1 is paired with partition 3 on the new
   drive as a second mirror layer.
4. LVM and Btrfs grow onto the new layer.

The result is two mirror layers across two drives: every byte now survives
the loss of either drive, and usable space doubles. Nothing is resized in
place, so no dm-integrity device or md array ever shrinks or reshapes.

Like the planner, this module only produces text. Nothing runs.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field

from .commands import (
    FS_LABEL,
    LINUX_FS_TYPE,
    LINUX_RAID_TYPE,
    LV_NAME,
    META_PARTLABEL,
    MOUNT_POINT,
    VG_NAME,
    integrity_device_name,
    md_device,
    partition_path,
)
from .model import Drive, Layer, Level, Plan, Protection
from .planner import END_RESERVE, META_PARTITION, START_RESERVE, make_plan, usable_space
from .units import MiB, format_size


class UnsupportedChange(ValueError):
    """The requested change is valid in principle but not implemented yet."""


@dataclass(frozen=True)
class Step:
    title: str
    commands: tuple[str, ...]
    note: str = ""


@dataclass
class GrowPlan:
    before: Plan
    drives: list[Drive]
    layers: list[Layer]
    steps: list[Step]
    warnings: list[str] = field(default_factory=list)

    @property
    def usable_before(self) -> int:
        return self.before.usable_nominal

    @property
    def usable_after(self) -> int:
        return sum(layer.usable_nominal for layer in self.layers)

    @property
    def drive_failures_tolerated(self) -> int:
        used = [layer for layer in self.layers if layer.level is not Level.UNUSED]
        return min(layer.drive_failures_tolerated for layer in used)


def minimum_size_for(slice_size: int, slices: int) -> int:
    """Smallest raw drive size that holds ``slices`` partitions of ``slice_size``."""
    return START_RESERVE + META_PARTITION + END_RESERVE + slices * slice_size


def plan_add_drive(
    current_sizes: list[int],
    current_devices: list[str],
    new_size: int,
    new_device: str,
) -> GrowPlan:
    """Plan adding one drive to a layout Tamarack created from ``current_*``."""
    if new_device in current_devices:
        raise ValueError(f"{new_device} is already part of the layout")
    current = make_plan(current_sizes, current_devices)
    if current.protection is Protection.SAME_DRIVE_MIRROR:
        return _single_to_mirror(current, new_size, new_device)
    raise UnsupportedChange(
        f"adding a drive to a {len(current.drives)}-drive layout is not supported yet; "
        "supported so far: 1 drive -> 2 drives"
    )


def _single_to_mirror(current: Plan, new_size: int, new_device: str) -> GrowPlan:
    old = current.drives[0]
    half = current.layers[0].slice_size
    # Same-capacity drives have identical sector counts (IDEMA LBA1-03),
    # so a matching drive always fits two slices.
    minimum = minimum_size_for(half, 2)
    if new_size < minimum:
        raise ValueError(
            f"{new_device} ({format_size(new_size)}) is too small: it needs at least "
            f"{format_size(minimum)} to mirror {old.device}"
        )
    usable = usable_space(new_size)
    new = Drive(device=new_device, size=new_size, usable=usable, bucket=usable)
    layers = [
        Layer(index=1, level=Level.RAID1, slice_size=half, members=(0, 1)),
        Layer(index=2, level=Level.RAID1, slice_size=half, members=(0, 1)),
    ]

    warnings: list[str] = []
    spare = usable - 2 * half
    if spare >= 1024 * MiB:
        warnings.append(
            f"{format_size(spare)} on {new_device} stays unused until another drive "
            "at least that large is added."
        )

    a, b = shlex.quote(old.device), shlex.quote(new_device)
    a3 = f"/dev/mapper/{integrity_device_name(0, 3)}"
    b2_part = shlex.quote(partition_path(new_device, 2))
    b3_part = shlex.quote(partition_path(new_device, 3))
    b2 = f"/dev/mapper/{integrity_device_name(1, 2)}"
    b3 = f"/dev/mapper/{integrity_device_name(1, 3)}"
    l1, l2 = md_device(1), md_device(2)
    slice_mib = half // MiB

    steps = [
        Step(
            "Check the existing mirror is healthy",
            (f"mdadm --detail --test {l1}",),
            "Stops here if the array is degraded: replace a failing drive before growing.",
        ),
        Step(
            f"Partition {new_device} to match",
            (
                f"sgdisk --zap-all {b}",
                f"sgdisk -n 1:0:+16M -t 1:{LINUX_FS_TYPE} -c 1:{META_PARTLABEL} {b}",
                f"sgdisk -n 2:0:+{slice_mib}M -t 2:{LINUX_RAID_TYPE} -c 2:tk-l1 {b}",
                f"sgdisk -n 3:0:+{slice_mib}M -t 3:{LINUX_RAID_TYPE} -c 3:tk-l2 {b}",
                f"partprobe {b}",
            ),
        ),
        Step(
            "dm-integrity on the new partitions",
            (
                f"integritysetup format --batch-mode --integrity crc32c {b2_part}",
                f"integritysetup open --integrity crc32c {b2_part} {integrity_device_name(1, 2)}",
                f"integritysetup format --batch-mode --integrity crc32c {b3_part}",
                f"integritysetup open --integrity crc32c {b3_part} {integrity_device_name(1, 3)}",
            ),
        ),
        Step(
            "Move one copy onto the new drive",
            (
                f"mdadm {l1} --add {b2}",
                f"mdadm {l1} --replace {a3} --with {b2}",
                f"mdadm --wait {l1}",
                f"mdadm {l1} --remove {a3}",
            ),
            "The old copy stays active until the new one is complete, so protection "
            "never drops during the move.",
        ),
        Step(
            f"Free the second half of {old.device}",
            (
                f"mdadm --zero-superblock {a3}",
                f"sgdisk -c 2:tk-l1 -c 3:tk-l2 {a}",
            ),
        ),
        Step(
            "Mirror the freed halves as layer 2",
            (
                f"mdadm --create {l2} --run --metadata=1.2 --level=1 --raid-devices=2 "
                f"--bitmap=internal {a3} {b3}",
            ),
        ),
        Step(
            "Grow the volume and file system onto layer 2",
            (
                f"pvcreate {l2}",
                f"vgextend {VG_NAME} {l2}",
                f"lvextend --extents +100%FREE {VG_NAME}/{LV_NAME}",
                f"btrfs filesystem resize max {MOUNT_POINT}",
            ),
        ),
        Step(
            f"Update layout metadata on both {META_PARTLABEL} partitions",
            (),
            "Not implemented yet.",
        ),
    ]
    return GrowPlan(
        before=current, drives=[old, new], layers=layers, steps=steps, warnings=warnings
    )


def render_grow(plan: GrowPlan) -> list[str]:
    lines = [
        "# tamarack dry run: nothing below has been executed.",
        f"# DESTRUCTIVE for {plan.drives[-1].device}: it will be erased.",
        f"# Volume group {VG_NAME}, file system label {FS_LABEL}, mounted at {MOUNT_POINT}.",
        "set -euo pipefail",
    ]
    for number, step in enumerate(plan.steps, start=1):
        lines += ["", f"# {number}. {step.title}"]
        if step.note:
            lines.append(f"#    {step.note}")
        lines += list(step.commands)
    return lines
