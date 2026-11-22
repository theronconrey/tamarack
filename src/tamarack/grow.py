# SPDX-License-Identifier: GPL-3.0-or-later
"""Plan adding a drive to an existing layout: the Drobo "just add a drive" path.

Supported:

* **1 -> 2 drives.** A single drive holds two mirrored copies on itself.
  ``mdadm --replace`` moves one copy to the new drive (the old copy stays
  active until the new one is complete), and the freed half pairs with the
  new drive as a second mirror layer.
* **2 -> 3 and 3 -> 4 drives.** The new drive joins existing layers in order
  while it has room: a two-drive mirror converts to single parity and widens
  to three drives; a single-parity layer widens by one. Space left over on
  the new drive and on existing drives (slices that were waiting for a
  partner) is laid out as new layers, SHR style.

Not yet: growing into two-drive protection (5+ drives), replacing a drive
with a larger one, removing drives.

Nothing is shrunk. Widening is an mdadm reshape: it runs in the background
with the data online and protected, and resumes from mdadm's checkpoint
after a power cut. Like the planner, this module only produces text.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field

from .commands import (
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
from .layout import Disk, Layout, LayoutLayer, Member
from .model import Level, Protection
from .planner import END_RESERVE, META_PARTITION, START_RESERVE, bucket_tolerance
from .policy import level_for, protection_for
from .units import GiB, MiB, align_down, format_size


class UnsupportedChange(ValueError):
    """The requested change is valid in principle but not implemented yet."""


@dataclass(frozen=True)
class Step:
    title: str
    commands: tuple[str, ...]
    note: str = ""


@dataclass
class GrowPlan:
    before: Layout
    after: Layout
    steps: list[Step]
    warnings: list[str] = field(default_factory=list)

    @property
    def new_device(self) -> str:
        return self.after.drives[-1].device

    @property
    def usable_before(self) -> int:
        return self.before.usable_nominal

    @property
    def usable_after(self) -> int:
        return self.after.usable_nominal

    @property
    def drive_failures_tolerated(self) -> int:
        return self.after.drive_failures_tolerated


def minimum_size_for(slice_size: int, slices: int) -> int:
    """Smallest raw drive size that holds ``slices`` partitions of ``slice_size``."""
    return START_RESERVE + META_PARTITION + END_RESERVE + slices * slice_size


def _mapper(drive: int, partition: int) -> str:
    return f"/dev/mapper/{integrity_device_name(drive, partition)}"


def _member_path(member: Member) -> str:
    return _mapper(member.drive, member.partition)


def plan_add_drive(layout: Layout, new_size: int, new_device: str) -> GrowPlan:
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
    return _widen(layout, new_size, new_device)


# --- 1 -> 2 drives ---------------------------------------------------------


def _single_to_mirror(layout: Layout, new_size: int, new_device: str) -> GrowPlan:
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

    warnings: list[str] = []
    spare = new.usable - 2 * half
    if spare >= GiB:
        warnings.append(
            f"{format_size(spare)} on {new_device} stays unused until another drive "
            "at least that large is added."
        )

    a, a3 = shlex.quote(old.device), _member_path(a_second)
    b2, b3 = _member_path(b_first), _member_path(b_second)
    steps = [
        _health_check(layout),
        _partition_new_drive(new_device, [(2, half, layer.index), (3, half, layer.index + 1)]),
        _integrity_new_drive(new_device, 1, [2, 3]),
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
                f"sgdisk -c {a_first.partition}:tk-l{layer.index} "
                f"-c {a_second.partition}:tk-l{layer.index + 1} {a}",
            ),
        ),
        Step(
            "Mirror the freed halves as a new layer",
            (
                f"mdadm --create {l2} --run --metadata=1.2 --level=1 --raid-devices=2 "
                f"--bitmap=internal {a3} {b3}",
            ),
        ),
        _grow_volume(grown=[], created=[l2]),
        _metadata_step(),
    ]
    return GrowPlan(before=layout, after=after, steps=steps, warnings=warnings)


# --- 2 -> 3 and 3 -> 4 drives ----------------------------------------------


def _widen(layout: Layout, new_size: int, new_device: str) -> GrowPlan:
    new_index = len(layout.drives)
    new = Disk(new_device, new_size)
    remaining = new.usable
    next_part = 2
    new_parts: list[tuple[int, int, int]] = []  # (partition, size, layer index)
    joined: list[tuple[LayoutLayer, LayoutLayer]] = []  # (before, after)
    kept: list[LayoutLayer] = []

    for layer in layout.layers:
        if remaining >= layer.slice_size and layer.level in (Level.RAID1, Level.RAID5):
            member = Member(new_index, next_part)
            new_parts.append((next_part, layer.slice_size, layer.index))
            next_part += 1
            remaining -= layer.slice_size
            widened = LayoutLayer(
                layer.index, Level.RAID5, layer.slice_size, (*layer.members, member)
            )
            joined.append((layer, widened))
        else:
            kept.append(layer)

    drives = [*layout.drives, new]
    free = {i: layout.free(i) for i in range(len(layout.drives))}
    free[new_index] = remaining
    next_parts = {i: layout.next_partition(i) for i in range(len(layout.drives))}
    next_parts[new_index] = next_part
    first_new_index = max((layer.index for layer in layout.layers), default=0) + 1
    created = _layers_from_free_space(free, next_parts, first_new_index)
    new_parts += [
        (m.partition, layer.slice_size, layer.index)
        for layer in created
        for m in layer.members
        if m.drive == new_index
    ]

    if not joined and not created:
        smallest = min(layer.slice_size for layer in layout.layers)
        raise ValueError(
            f"{new_device} ({format_size(new_size)}) is too small: it needs at least "
            f"{format_size(minimum_size_for(smallest, 1))} to join the existing layers"
        )

    after_layers = sorted([w for _, w in joined] + kept + created, key=lambda layer: layer.index)
    after = Layout(drives=drives, layers=after_layers)
    after.validate()

    warnings = _leftover_warnings(after)
    if kept:
        warnings.append(
            "Layer"
            + ("s " if len(kept) > 1 else " ")
            + ", ".join(str(layer.index) for layer in kept)
            + f" stay{'' if len(kept) > 1 else 's'} as before: {new_device} has no room "
            "left for them."
        )

    steps = [
        _health_check(layout),
        _partition_new_drive(new_device, sorted(new_parts)),
        _integrity_new_drive(new_device, new_index, [p for p, _, _ in sorted(new_parts)]),
    ]
    steps += [_widen_step(before, after_layer) for before, after_layer in joined]
    steps += _existing_drive_partitions(layout, created, new_index)
    steps += [_create_step(layer) for layer in created]
    steps.append(
        _grow_volume(
            grown=[md_device(w.index) for _, w in joined],
            created=[md_device(layer.index) for layer in created],
        )
    )
    steps.append(_metadata_step())
    return GrowPlan(before=layout, after=after, steps=steps, warnings=warnings)


def _layers_from_free_space(
    free: dict[int, int], next_parts: dict[int, int], first_index: int
) -> list[LayoutLayer]:
    """SHR-style layers over leftover space shared by two or more drives."""
    sizes = {d: align_down(f) for d, f in free.items() if f >= GiB}
    if len(sizes) < 2:
        return []
    # Group near-equal free sizes like the planner groups drives.
    buckets: dict[int, int] = {}
    base: int | None = None
    for drive in sorted(sizes, key=lambda d: sizes[d]):
        if base is None or sizes[drive] - base > bucket_tolerance(base):
            base = sizes[drive]
        buckets[drive] = base
    layers: list[LayoutLayer] = []
    previous, index = 0, first_index
    numbers = dict(next_parts)
    protection = protection_for(len(free))
    for step in sorted(set(buckets.values())):
        drives = [d for d in sorted(buckets) if buckets[d] >= step]
        level = level_for(len(drives), protection)
        if level is not Level.UNUSED:
            members = []
            for d in drives:
                members.append(Member(d, numbers[d]))
                numbers[d] += 1
            layers.append(LayoutLayer(index, level, step - previous, tuple(members)))
            index += 1
        previous = step
    return layers


# --- steps ------------------------------------------------------------------


def _health_check(layout: Layout) -> Step:
    return Step(
        "Check every layer is healthy",
        tuple(f"mdadm --detail --test {md_device(layer.index)}" for layer in layout.layers),
        "Stops here if any array is degraded: replace a failing drive before growing.",
    )


def _partition_new_drive(device: str, parts: list[tuple[int, int, int]]) -> Step:
    dev = shlex.quote(device)
    commands = [
        f"sgdisk --zap-all {dev}",
        f"sgdisk -n 1:0:+16M -t 1:{LINUX_FS_TYPE} -c 1:{META_PARTLABEL} {dev}",
    ]
    commands += [
        f"sgdisk -n {n}:0:+{size // MiB}M -t {n}:{LINUX_RAID_TYPE} -c {n}:tk-l{layer} {dev}"
        for n, size, layer in parts
    ]
    commands.append(f"partprobe {dev}")
    return Step(f"Partition {device}", tuple(commands))


def _integrity_new_drive(device: str, drive: int, numbers: list[int]) -> Step:
    commands: list[str] = []
    for n in numbers:
        path = shlex.quote(partition_path(device, n))
        commands.append(f"integritysetup format --batch-mode --integrity crc32c {path}")
        commands.append(
            f"integritysetup open --integrity crc32c {path} {integrity_device_name(drive, n)}"
        )
    return Step("dm-integrity on the new partitions", tuple(commands))


def _widen_step(before: LayoutLayer, after: LayoutLayer) -> Step:
    md = md_device(before.index)
    added = _member_path(after.members[-1])
    width = after.member_count
    if before.level is Level.RAID1:
        return Step(
            f"Layer {before.index}: mirror -> single parity across {width} drives",
            (
                f"mdadm --grow {md} --bitmap=none",
                f"mdadm --grow {md} --level=5",
                f"mdadm {md} --add {added}",
                f"mdadm --grow {md} --raid-devices={width}",
                f"mdadm --wait {md}",
                f"mdadm --grow {md} --consistency-policy=ppl",
            ),
            "A two-drive mirror converts to single parity in place, then reshapes onto "
            "the new drive. The data stays online and protected throughout; mdadm "
            "resumes from its checkpoint after a power cut. Large layers take hours to days.",
        )
    return Step(
        f"Layer {before.index}: widen single parity to {width} drives",
        (
            f"mdadm --grow {md} --consistency-policy=resync",
            f"mdadm {md} --add {added}",
            f"mdadm --grow {md} --raid-devices={width}",
            f"mdadm --wait {md}",
            f"mdadm --grow {md} --consistency-policy=ppl",
        ),
        "PPL is paused for the reshape and switched back on afterwards. The data stays "
        "online and protected; mdadm resumes from its checkpoint after a power cut.",
    )


def _existing_drive_partitions(layout: Layout, created: list[LayoutLayer], new: int) -> list[Step]:
    steps: list[Step] = []
    for drive_index, disk in enumerate(layout.drives):
        parts = [
            (m.partition, layer.slice_size, layer.index)
            for layer in created
            for m in layer.members
            if m.drive == drive_index
        ]
        if not parts:
            continue
        dev = shlex.quote(disk.device)
        commands = [
            f"sgdisk -n {n}:0:+{size // MiB}M -t {n}:{LINUX_RAID_TYPE} -c {n}:tk-l{layer} {dev}"
            for n, size, layer in parts
        ]
        commands.append(f"partprobe {dev}")
        for n, _, _ in parts:
            path = shlex.quote(partition_path(disk.device, n))
            commands.append(f"integritysetup format --batch-mode --integrity crc32c {path}")
            commands.append(
                f"integritysetup open --integrity crc32c {path} "
                f"{integrity_device_name(drive_index, n)}"
            )
        steps.append(
            Step(
                f"Use unallocated space on {disk.device}",
                tuple(commands),
                "Adds partitions in free space only; existing partitions are untouched.",
            )
        )
    return steps


def _create_step(layer: LayoutLayer) -> Step:
    members = " ".join(_member_path(m) for m in layer.members)
    level = layer.level.value.removeprefix("raid")
    safety = "--consistency-policy=ppl" if layer.level is Level.RAID5 else "--bitmap=internal"
    return Step(
        f"Create layer {layer.index} from leftover space",
        (
            f"mdadm --create {md_device(layer.index)} --run --metadata=1.2 --level={level} "
            f"--raid-devices={layer.member_count} {safety} {members}",
        ),
    )


def _grow_volume(grown: list[str], created: list[str]) -> Step:
    commands = [f"pvresize {md}" for md in grown]
    for md in created:
        commands += [f"pvcreate {md}", f"vgextend {VG_NAME} {md}"]
    commands += [
        f"lvextend --extents +100%FREE {VG_NAME}/{LV_NAME}",
        f"btrfs filesystem resize max {MOUNT_POINT}",
    ]
    return Step("Grow the volume and file system", tuple(commands))


def _metadata_step() -> Step:
    return Step(
        f"Write the new layout to every {META_PARTLABEL} partition",
        (),
        "Not implemented yet; `tamarack grow --layout-out FILE` saves it meanwhile.",
    )


def _leftover_warnings(layout: Layout) -> list[str]:
    warnings = []
    for i, disk in enumerate(layout.drives):
        left = layout.free(i)
        if left >= GiB:
            warnings.append(
                f"{format_size(left)} on {disk.device} stays unused until another drive "
                "can pair with it."
            )
    return warnings


def render_grow(plan: GrowPlan) -> list[str]:
    lines = [
        "# tamarack dry run: nothing below has been executed.",
        f"# DESTRUCTIVE for {plan.new_device}: it will be erased.",
        "set -euo pipefail",
    ]
    for number, step in enumerate(plan.steps, start=1):
        lines += ["", f"# {number}. {step.title}"]
        if step.note:
            lines.append(f"#    {step.note}")
        lines += list(step.commands)
    return lines
