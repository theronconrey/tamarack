# SPDX-License-Identifier: GPL-3.0-or-later
"""Plan adding a drive to an existing layout: the Drobo "just add a drive" path.

Supported:

* **1 -> 2 drives.** A single drive holds two mirrored copies on itself.
  ``mdadm --replace`` moves one copy to the new drive (the old copy stays
  active until the new one is complete), and the freed half pairs with the
  new drive as a second mirror layer.
* **2 -> 3 and 3 -> 4 drives.** The new drive joins existing layers in order
  while it has room (see ``steps.expand``), and leftover space is paired into
  new layers, SHR style.

Not yet: growing into two-drive protection (5+ drives).

Nothing is shrunk. Widening is an mdadm reshape: it runs in the background
with the data online and protected, and resumes from mdadm's checkpoint after
a power cut. Like the planner, this module only produces data and text.
"""

from __future__ import annotations

from .commands import md_device
from .layout import Disk, Layout, LayoutLayer, Member
from .model import Level, Protection
from .ops import Step, cmd
from .policy import protection_for
from .steps import (
    ChangePlan,
    UnsupportedChange,
    create_array,
    expand,
    expansion_steps,
    grow_volume,
    health_check,
    integrity_open,
    leftover_warnings,
    member_path,
    metadata_step,
    minimum_size_for,
    partition_drive,
    render,
)
from .units import GiB, format_size

# Names kept for callers that imported them from grow.
GrowPlan = ChangePlan
render_grow = render
__all__ = [
    "GrowPlan",
    "UnsupportedChange",
    "minimum_size_for",
    "plan_add_drive",
    "render_grow",
]


def plan_add_drive(layout: Layout, new_size: int, new_device: str) -> ChangePlan:
    """Plan adding one drive to ``layout``. Pure: never touches a disk."""
    if new_device in {d.device for d in layout.drives}:
        raise ValueError(f"{new_device} is already part of the layout")
    if new_size < minimum_size_for(GiB, 1):
        raise ValueError(f"{new_device} is too small to use")
    if layout.protection is Protection.SAME_DRIVE_MIRROR:
        return _single_to_mirror(layout, new_size, new_device)
    if protection_for(len(layout.drives) + 1) is Protection.TWO_DRIVE:
        raise UnsupportedChange(
            "growing to 5 or more drives (two-drive protection) is not supported yet; "
            "supported so far: 1 -> 2, 2 -> 3 and 3 -> 4 drives"
        )
    return _add_to_parity(layout, new_size, new_device)


def _single_to_mirror(layout: Layout, new_size: int, new_device: str) -> ChangePlan:
    (layer,) = layout.layers
    old = layout.drives[0]
    half = layer.slice_size
    # Same-capacity drives have identical sector counts (IDEMA LBA1-03),
    # so a matching drive always fits two slices.
    minimum = minimum_size_for(half, 2)
    if new_size < minimum:
        raise ValueError(
            f"{new_device} ({format_size(new_size)}) is too small: it needs at least "
            f"{format_size(minimum)} to mirror {old.device}"
        )
    new = Disk(new_device, new_size)
    a_first, a_second = sorted(layer.members, key=lambda m: m.partition)
    b_first, b_second = Member(1, 2), Member(1, 3)
    l1, l2 = md_device(layer.index), md_device(layer.index + 1)
    after = Layout(
        drives=[old, new],
        layers=[
            LayoutLayer(layer.index, Level.RAID1, half, (a_first, b_first)),
            LayoutLayer(layer.index + 1, Level.RAID1, half, (a_second, b_second)),
        ],
    )
    a, a3 = old.device, member_path(a_second)
    b2, b3 = member_path(b_first), member_path(b_second)
    steps = [
        health_check(layout),
        partition_drive(new_device, [(2, half, layer.index), (3, half, layer.index + 1)], True),
        Step("dm-integrity on the new partitions", tuple(integrity_open(new_device, 1, [2, 3]))),
        Step(
            "Move one copy onto the new drive",
            (
                cmd("mdadm", l1, "--add", b2),
                cmd("mdadm", l1, "--replace", a3, "--with", b2),
                cmd("mdadm", "--wait", l1),
                cmd("mdadm", l1, "--remove", a3),
            ),
            "The old copy stays active until the new one is complete, so protection "
            "never drops during the move.",
        ),
        Step(
            f"Free the second half of {old.device}",
            (
                cmd("mdadm", "--zero-superblock", a3),
                cmd(
                    "sgdisk",
                    *("-c", f"{a_first.partition}:tk-l{layer.index}"),
                    *("-c", f"{a_second.partition}:tk-l{layer.index + 1}"),
                    a,
                ),
            ),
        ),
        Step(
            "Mirror the freed halves as a new layer",
            (create_array(l2, Level.RAID1, [a3, b3]),),
        ),
        grow_volume(grown=[], created=[l2]),
        metadata_step(),
    ]
    return ChangePlan(
        before=layout,
        after=after,
        steps=steps,
        warnings=leftover_warnings(after),
        erases=new_device,
    )


def _add_to_parity(layout: Layout, new_size: int, new_device: str) -> ChangePlan:
    new_index = len(layout.drives)
    base = Layout(drives=[*layout.drives, Disk(new_device, new_size)], layers=layout.layers)
    exp = expand(base, new_index)
    if not exp.changed:
        smallest = min(layer.slice_size for layer in layout.layers)
        raise ValueError(
            f"{new_device} ({format_size(new_size)}) is too small: it needs at least "
            f"{format_size(minimum_size_for(smallest, 1))} to join the existing layers"
        )
    own = exp.partitions_on(new_index)
    steps = [
        health_check(layout),
        partition_drive(new_device, own, wipe=True),
        Step(
            "dm-integrity on the new partitions",
            tuple(integrity_open(new_device, new_index, [n for n, _, _ in own])),
        ),
        *expansion_steps(base, exp, new_index),
        metadata_step(),
    ]
    warnings = leftover_warnings(exp.after)
    if exp.kept:
        names = ", ".join(str(layer.index) for layer in exp.kept)
        plural = len(exp.kept) > 1
        warnings.append(
            f"Layer{'s' if plural else ''} {names} stay{'' if plural else 's'} as before: "
            f"{new_device} has no room left for {'them' if plural else 'it'}."
        )
    return ChangePlan(
        before=layout, after=exp.after, steps=steps, warnings=warnings, erases=new_device
    )
