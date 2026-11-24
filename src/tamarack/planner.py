# SPDX-License-Identifier: GPL-3.0-or-later
"""Turn a set of drive sizes into a layered layout, SHR / BeyondRAID style.

Algorithm
---------
1. Reserve space on each drive: 1 MiB at the start (GPT + alignment), a
   16 MiB metadata partition, and 1 MiB at the end (backup GPT).
2. Group drives whose usable sizes are within a small tolerance, so two
   "4 TB" drives that differ by a few MB count as equal.
3. Cut the drives into layers at each distinct size step. Layer k uses every
   drive at least as large as step k; its slice is the size difference from
   the previous step.
4. Each layer gets a level from the policy (mirror, single or dual parity).
   A slice found on only one drive is left unallocated until a matching
   drive arrives.

One drive is the exception: it is split into two equal partitions that are
mirrored, protecting against bad sectors and bit rot but not drive loss.
"""

from __future__ import annotations

from collections.abc import Sequence

from .identity import validate_identity
from .model import Drive, Layer, Level, Plan, Protection
from .policy import REBUILD_RISK_DRIVE_SIZE, level_for, protection_for
from .units import GiB, MiB, align_down, format_size

START_RESERVE = 1 * MiB
META_PARTITION = 16 * MiB
END_RESERVE = 1 * MiB

# dm-integrity costs space for checksums (crc32c: 4 bytes per 4 KiB sector)
# plus its journal. 0.5% is a deliberately conservative estimate until the
# reference box gives measured numbers.
INTEGRITY_OVERHEAD_ESTIMATE = 0.005


def check_identities(identities: Sequence[str]) -> None:
    """Every identity well formed, and no physical drive listed twice."""
    known = [validate_identity(i) for i in identities if i]
    if len(set(known)) != len(known):
        raise ValueError("the same drive is listed twice (two paths, one identity)")


def bucket_tolerance(size: int) -> int:
    return max(1 * GiB, size // 200)  # 1 GiB or 0.5%, whichever is larger


def usable_space(size: int) -> int:
    usable = align_down(size - START_RESERVE - META_PARTITION - END_RESERVE)
    if usable < 1 * GiB:
        raise ValueError(f"drive too small: {format_size(size)}")
    return usable


def _bucketed(usables: Sequence[int]) -> list[int]:
    """Map each usable size to the smallest size in its near-equal group."""
    order = sorted(range(len(usables)), key=lambda i: usables[i])
    buckets = [0] * len(usables)
    base: int | None = None
    for i in order:
        size = usables[i]
        if base is None or size - base > bucket_tolerance(base):
            base = size
        buckets[i] = base
    return buckets


def make_plan(
    sizes: Sequence[int],
    devices: Sequence[str] | None = None,
    identities: Sequence[str] | None = None,
) -> Plan:
    """Build a plan. Pure function: it never touches a disk."""
    if not sizes:
        raise ValueError("at least one drive is required")
    if devices is None:
        devices = [f"disk{i + 1}" for i in range(len(sizes))]
    if len(devices) != len(sizes):
        raise ValueError("one device name is needed per drive size")
    if len(set(devices)) != len(devices):
        raise ValueError("each device may appear only once")
    if identities is None:
        identities = [""] * len(sizes)
    if len(identities) != len(sizes):
        raise ValueError("one identity (or an empty string) is needed per drive")
    check_identities(identities)

    usables = [usable_space(s) for s in sizes]
    buckets = _bucketed(usables)
    drives = [
        Drive(device=d, size=s, usable=u, bucket=b, identity=i)
        for d, s, u, b, i in zip(devices, sizes, usables, buckets, identities, strict=True)
    ]
    protection = protection_for(len(drives))

    if protection is Protection.SAME_DRIVE_MIRROR:
        layers = [
            Layer(
                index=1,
                level=Level.RAID5,  # two members: a copy, with PPL (policy.py)
                slice_size=align_down(drives[0].bucket // 2),
                members=(0,),
                copies_on_same_drive=True,
            )
        ]
    else:
        layers = _layers(drives, protection)

    plan = Plan(
        drives=drives,
        protection=protection,
        layers=layers,
        integrity_overhead=INTEGRITY_OVERHEAD_ESTIMATE,
    )
    plan.warnings.extend(_warnings(plan))
    return plan


def _layers(drives: list[Drive], protection: Protection) -> list[Layer]:
    steps = sorted({d.bucket for d in drives})
    layers: list[Layer] = []
    previous = 0
    for index, step in enumerate(steps, start=1):
        members = tuple(i for i, d in enumerate(drives) if d.bucket >= step)
        layers.append(
            Layer(
                index=index,
                level=level_for(len(members), protection),
                slice_size=step - previous,
                members=members,
            )
        )
        previous = step
    return layers


def _warnings(plan: Plan) -> list[str]:
    warnings: list[str] = []
    if plan.protection is Protection.SAME_DRIVE_MIRROR:
        warnings.append(
            "Single drive: data has two copies on the same drive. "
            "It is protected from bad sectors, not from losing the "
            "drive. Add a second drive to protect against that."
        )
    for layer in plan.layers:
        if layer.level is Level.UNUSED and len(layer.members) == 1:
            warnings.append(
                f"{format_size(layer.slice_size)} on "
                f"{plan.drives[layer.members[0]].device} is unused "
                "until another drive at least that large is added."
            )
        elif layer.level is Level.UNUSED:
            names = " and ".join(plan.drives[m].device for m in layer.members)
            warnings.append(
                f"{format_size(layer.slice_size)} on each of {names} is unused "
                "until four drives at least that large can share it, so that it "
                "survives two drive failures like the rest of the pool."
            )
    largest = max(d.size for d in plan.drives)
    if largest >= REBUILD_RISK_DRIVE_SIZE:
        warnings.append(
            f"Largest drive is {format_size(largest)}: rebuilds can "
            "take days. Keep backups current and replace failed "
            "drives promptly."
        )
    return warnings
