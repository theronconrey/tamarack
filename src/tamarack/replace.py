# SPDX-License-Identifier: GPL-3.0-or-later
"""Plan swapping one drive for another: upgrades and failed drives.

Two cases:

* **Upgrade** (the old drive still works). ``mdadm --replace`` copies each of
  the old drive's partitions to the new drive while the old copy stays
  active, so protection never drops. The new mappings are opened under a
  temporary ``-new`` name and renamed once the old ones are closed.
* **Failed drive** (``failed=True``). The failed members are removed and the
  new drive's partitions are added, so mdadm rebuilds them from the other
  drives. Protection is reduced until the rebuild finishes.

Either way the new drive must be large enough for every slice the old one
held. Any extra space then goes to work through ``steps.expand``: the drive
joins layers it wasn't in, and leftover space pairs into new layers. Replacing
the smaller drives in a box one at a time therefore ends at the same layout a
fresh build of the larger drives would have.

A single drive that fails cannot be rebuilt: both copies were on it.
Like the other planners, this only produces data and text.
"""

from __future__ import annotations

from .commands import integrity_device_name, md_device
from .layout import Disk, Layout
from .model import Protection
from .ops import Command, Step, cmd
from .steps import (
    ChangePlan,
    expand,
    expansion_steps,
    health_check,
    integrity_open,
    leftover_warnings,
    mapper,
    metadata_step,
    minimum_size_for_bytes,
    partition_drive,
)
from .units import format_size

NEW_SUFFIX = "-new"


def plan_replace(
    layout: Layout,
    old_device: str,
    new_size: int,
    new_device: str,
    failed: bool = False,
) -> ChangePlan:
    """Plan replacing ``old_device`` with ``new_device``. Pure: never touches a disk."""
    devices = [d.device for d in layout.drives]
    if old_device not in devices:
        raise ValueError(f"{old_device} is not part of the layout")
    if new_device in devices and new_device != old_device:
        raise ValueError(f"{new_device} is already part of the layout")
    if new_device == old_device and not failed:
        raise ValueError("an upgrade needs the new drive attached alongside the old one")
    index = devices.index(old_device)
    if failed and layout.protection is Protection.SAME_DRIVE_MIRROR:
        raise ValueError(
            "a single drive cannot be rebuilt after it fails: both copies were on it. "
            "Restore from backup onto a new drive."
        )
    needed = layout.allocated(index)
    minimum = minimum_size_for_bytes(needed)
    if new_size < minimum:
        raise ValueError(
            f"{new_device} ({format_size(new_size)}) is too small: it needs at least "
            f"{format_size(minimum)} to hold everything {old_device} held"
        )

    drives = list(layout.drives)
    drives[index] = Disk(new_device, new_size)
    base = Layout(drives=drives, layers=layout.layers)
    exp = expand(base, index)

    # The old drive's slices, in partition order, with their layer.
    held = sorted(
        (m.partition, layer.slice_size, layer.index)
        for layer in layout.layers
        for m in layer.members
        if m.drive == index
    )
    own_new = exp.partitions_on(index)
    affected = sorted({layer for _, _, layer in held})
    suffix = "" if failed else NEW_SUFFIX

    check = health_check(layout, skip=set(affected) if failed else None)
    steps: list[Step] = [check] if check.commands else []
    if failed:
        steps.append(_remove_failed(layout, index, held))
    steps.append(partition_drive(new_device, sorted(held + own_new), wipe=True))
    steps.append(
        Step(
            "dm-integrity on the new partitions",
            tuple(
                integrity_open(new_device, index, [n for n, _, _ in held], suffix)
                + integrity_open(new_device, index, [n for n, _, _ in own_new])
            ),
        )
    )
    if failed:
        steps += [_rebuild(index, n, layer) for n, _, layer in held]
    else:
        steps += [_copy(index, n, layer) for n, _, layer in held]
        steps.append(_retire_old(index, [n for n, _, _ in held]))
    steps += expansion_steps(base, exp, index)
    steps.append(metadata_step())

    warnings = leftover_warnings(exp.after)
    if failed:
        warnings.insert(
            0,
            "Until the rebuild finishes, the affected layers have no spare protection: "
            "another drive failure in that time loses data. Keep backups current.",
        )
    return ChangePlan(
        before=layout, after=exp.after, steps=steps, warnings=warnings, erases=new_device
    )


def _copy(drive: int, partition: int, layer: int) -> Step:
    md = md_device(layer)
    old, new = mapper(drive, partition), mapper(drive, partition, NEW_SUFFIX)
    return Step(
        f"Layer {layer}: copy onto the new drive",
        (
            cmd("mdadm", md, "--add", new),
            cmd("mdadm", md, "--replace", old, "--with", new),
            cmd("mdadm", "--wait", md),
            cmd("mdadm", md, "--remove", old),
        ),
        "The old copy stays active until the new one is complete, so protection never drops.",
    )


def _retire_old(drive: int, partitions: list[int]) -> Step:
    commands: list[Command] = []
    for n in partitions:
        name = integrity_device_name(drive, n)
        commands.append(cmd("integritysetup", "close", name))
        commands.append(cmd("dmsetup", "rename", f"{name}{NEW_SUFFIX}", name))
    return Step(
        "Retire the old drive",
        tuple(commands),
        "Closes the old drive's mappings and gives the new ones their permanent names. "
        "The old drive can be removed after this step.",
    )


def _remove_failed(layout: Layout, drive: int, held: list[tuple[int, int, int]]) -> Step:
    commands: list[Command] = []
    for layer in sorted({layer for _, _, layer in held}):
        md = md_device(layer)
        commands.append(cmd("mdadm", md, "--remove", "failed"))
        commands.append(cmd("mdadm", md, "--remove", "detached"))
    for n, _, _ in held:
        # The mapping may already be gone with the drive; that is fine.
        name = integrity_device_name(drive, n)
        commands.append(cmd("integritysetup", "close", name, tolerate_failure=True))
    return Step(
        f"Remove the failed {layout.drives[drive].device} from its layers",
        tuple(commands),
    )


def _rebuild(drive: int, partition: int, layer: int) -> Step:
    md = md_device(layer)
    return Step(
        f"Layer {layer}: rebuild onto the new drive",
        (cmd("mdadm", md, "--add", mapper(drive, partition)), cmd("mdadm", "--wait", md)),
        "mdadm rebuilds this slice from the other drives. The layer is unprotected "
        "until it finishes.",
    )
