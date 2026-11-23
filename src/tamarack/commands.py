# SPDX-License-Identifier: GPL-3.0-or-later
"""Turn a Plan into the steps that would build it.

Steps hold structured commands (see ``ops``); ``render`` shows them as a dry
run. This module only produces data and text. Nothing here runs a command: execution,
the step journal and resume logic come later, behind a mandatory dry run.
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import Layer, Level, Plan
from .ops import Command, Step, cmd, render_steps
from .planner import META_PARTITION, START_RESERVE
from .units import MiB

VG_NAME = "tamarack"
LV_NAME = "data"
MOUNT_POINT = "/srv/tamarack"
MOUNT_OPTIONS = "compress=zstd:1,noatime"
MOUNT_UNIT = "srv-tamarack.mount"  # data/systemd/srv-tamarack.mount
SCRUB_TIMER = "tamarack-scrub.timer"  # data/systemd/tamarack-scrub.timer
FS_LABEL = "tamarack"
META_PARTLABEL = "tk-meta"


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


def integrity_device_name(drive: int, number: int) -> str:
    """dm-integrity mapping name for partition ``number`` on drive index ``drive``."""
    return f"tk-d{drive + 1}-p{number}"


def integrity_name(part: Partition) -> str:
    return integrity_device_name(part.drive, part.number)


def md_device(layer_index: int) -> str:
    return f"/dev/md/tk-l{layer_index}"


def md_path(layer: Layer) -> str:
    return md_device(layer.index)


# Partitioning uses parted, which Fedora's SELinux policy runs confined in
# fsadm_t; the engine itself never gets raw access to disks.
FIRST_LAYER_START = START_RESERVE + META_PARTITION  # bytes from the start of a drive


def new_partition_table(device: str) -> Command:
    """A fresh, empty GPT, replacing whatever the drive held."""
    return cmd("parted", "--script", device, "mklabel", "gpt")


def make_partition(device: str, number: int, label: str, start: int, size: int) -> Command:
    """Partition ``number`` from ``start`` for ``size`` bytes (both MiB-aligned)."""
    actions = ["mkpart", label, f"{start // MiB}MiB", f"{(start + size) // MiB}MiB"]
    if label != META_PARTLABEL:
        actions += ["set", str(number), "raid", "on"]
    return cmd("parted", "--script", device, *actions)


def build_steps(plan: Plan) -> list[Step]:
    """The steps, as structured commands, that would build the plan."""
    parts = partitions(plan)
    partition: list[Command] = []
    for i, drive in enumerate(plan.drives):
        dev = drive.device
        partition.append(new_partition_table(dev))
        start = START_RESERVE
        for part in sorted((p for p in parts if p.drive == i), key=lambda p: p.number):
            partition.append(make_partition(dev, part.number, part.label, start, part.size))
            start += part.size
        partition.append(cmd("partprobe", dev))

    integrity: list[Command] = []
    for part in (p for p in parts if p.layer):
        path = partition_path(plan.drives[part.drive].device, part.number)
        integrity += [
            cmd("integritysetup", "format", "--batch-mode", "--integrity", "crc32c", path),
            cmd("integritysetup", "open", "--integrity", "crc32c", path, integrity_name(part)),
        ]

    arrays: list[Command] = []
    for layer in plan.used_layers:
        members = [f"/dev/mapper/{integrity_name(p)}" for p in parts if p.layer == layer.index]
        level = layer.level.value.removeprefix("raid")
        if layer.level is Level.RAID5:
            safety = "--consistency-policy=ppl"  # closes the RAID5 write hole
        else:
            safety = "--bitmap=internal"  # fast resync after a crash
        arrays.append(
            cmd(
                "mdadm",
                "--create",
                md_path(layer),
                "--run",
                "--metadata=1.2",
                f"--level={level}",
                f"--raid-devices={layer.member_count}",
                safety,
                *members,
            )
        )

    mds = [md_path(layer) for layer in plan.used_layers]
    lv = f"/dev/{VG_NAME}/{LV_NAME}"
    return [
        Step("Partition each drive", tuple(partition)),
        Step(
            "dm-integrity on every layer partition "
            "(journal mode is integritysetup's default; bitmap mode stays off)",
            tuple(integrity),
        ),
        Step("One md array per layer", tuple(arrays)),
        Step(
            "LVM joins the layers into one volume",
            (
                cmd("pvcreate", *mds),
                cmd("vgcreate", VG_NAME, *mds),
                cmd("lvcreate", "--yes", "--extents", "100%FREE", "--name", LV_NAME, VG_NAME),
            ),
        ),
        Step(
            "Btrfs on top",
            (
                cmd("mkfs.btrfs", "--label", FS_LABEL, "--metadata", "dup", lv),
                # systemd mounts it, in the host's namespace and at every boot from
                # now on: a mount made inside the engine's sandbox would stay there.
                cmd("systemctl", "enable", "--now", MOUNT_UNIT, SCRUB_TIMER),
                cmd("btrfs", "subvolume", "create", f"{MOUNT_POINT}/shares"),
                cmd("btrfs", "subvolume", "create", f"{MOUNT_POINT}/snapshots"),
            ),
        ),
        Step(
            f"Write the layout to every {META_PARTLABEL} partition",
            _metadata(plan),
            "Each drive carries the whole layout, so the box can be read from any of them.",
        ),
    ]


def _metadata(plan: Plan) -> tuple[Command, ...]:
    from .layout import layout_from_plan
    from .steps import metadata_commands

    return metadata_commands(layout_from_plan(plan))


def render(plan: Plan) -> list[str]:
    """Return the command lines, with comments, that would build the plan."""
    warning = "# DESTRUCTIVE: these commands erase every listed drive."
    return render_steps([warning], build_steps(plan))
