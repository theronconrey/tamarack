# SPDX-License-Identifier: GPL-3.0-or-later
"""Building blocks shared by every change to an existing layout.

A change (grow, replace) is a list of Steps, each a titled group of commands
with an optional note. ``expand`` works out how a drive with free space joins
existing layers and pairs leftover space into new layers; the grow and replace
planners both use it so the two never disagree about layout.

Everything here produces text. Nothing runs.
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
from .layout import Layout, LayoutLayer, Member
from .model import Level, Protection
from .planner import END_RESERVE, META_PARTITION, START_RESERVE, bucket_tolerance
from .policy import level_for, protection_for
from .units import GiB, MiB, align_down, format_size

# (partition number, size in bytes, layer index)
PartSpec = tuple[int, int, int]


class UnsupportedChange(ValueError):
    """The requested change is valid in principle but not implemented yet."""


@dataclass(frozen=True)
class Step:
    title: str
    commands: tuple[str, ...]
    note: str = ""


@dataclass
class ChangePlan:
    before: Layout
    after: Layout
    steps: list[Step]
    warnings: list[str] = field(default_factory=list)
    erases: str = ""  # the one device this change wipes

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


def minimum_size_for_bytes(allocated: int) -> int:
    return START_RESERVE + META_PARTITION + END_RESERVE + allocated


def mapper(drive: int, partition: int, suffix: str = "") -> str:
    return f"/dev/mapper/{integrity_device_name(drive, partition)}{suffix}"


def member_path(member: Member, suffix: str = "") -> str:
    return mapper(member.drive, member.partition, suffix)


# --- expansion ---------------------------------------------------------------


@dataclass
class Expansion:
    """How one drive's free space is put to work."""

    after: Layout
    joined: list[tuple[LayoutLayer, LayoutLayer]]  # (before, after)
    created: list[LayoutLayer]
    kept: list[LayoutLayer]  # layers the drive is not in and couldn't join

    def partitions_on(self, drive: int) -> list[PartSpec]:
        parts = [
            (w.members[-1].partition, w.slice_size, w.index)
            for _, w in self.joined
            if w.members[-1].drive == drive
        ]
        parts += [
            (m.partition, layer.slice_size, layer.index)
            for layer in self.created
            for m in layer.members
            if m.drive == drive
        ]
        return sorted(parts)

    @property
    def changed(self) -> bool:
        return bool(self.joined or self.created)


def expand(layout: Layout, drive: int) -> Expansion:
    """Put ``drive``'s free space to work.

    The drive joins layers it is not yet in, in order, while it has room: a
    two-drive mirror becomes single parity, a single-parity layer widens. Then
    leftover space on any drives is paired into new layers, SHR style.
    """
    if protection_for(len(layout.drives)) is Protection.TWO_DRIVE:
        raise UnsupportedChange("layouts of 5 or more drives are not supported yet")
    remaining = layout.free(drive)
    next_part = layout.next_partition(drive)
    joined: list[tuple[LayoutLayer, LayoutLayer]] = []
    kept: list[LayoutLayer] = []
    for layer in layout.layers:
        if drive in layer.drive_indexes:
            continue
        can_join = layer.level in (Level.RAID1, Level.RAID5) and not layer.copies_on_same_drive
        if can_join and remaining >= layer.slice_size:
            member = Member(drive, next_part)
            next_part += 1
            remaining -= layer.slice_size
            widened = LayoutLayer(
                layer.index, Level.RAID5, layer.slice_size, (*layer.members, member)
            )
            joined.append((layer, widened))
        else:
            kept.append(layer)

    free = {i: layout.free(i) for i in range(len(layout.drives))}
    free[drive] = remaining
    next_parts = {i: layout.next_partition(i) for i in range(len(layout.drives))}
    next_parts[drive] = next_part
    first_index = max((layer.index for layer in layout.layers), default=0) + 1
    created = layers_from_free_space(free, next_parts, first_index, len(layout.drives))

    widened_by_index = {w.index: w for _, w in joined}
    layers = [widened_by_index.get(layer.index, layer) for layer in layout.layers] + created
    after = Layout(drives=list(layout.drives), layers=layers)
    after.validate()
    return Expansion(after=after, joined=joined, created=created, kept=kept)


def layers_from_free_space(
    free: dict[int, int], next_parts: dict[int, int], first_index: int, drive_count: int
) -> list[LayoutLayer]:
    """SHR-style layers over leftover space shared by two or more drives."""
    sizes = {d: align_down(f) for d, f in free.items() if f >= GiB}
    if len(sizes) < 2:
        return []
    buckets: dict[int, int] = {}
    base: int | None = None
    for d in sorted(sizes, key=lambda d: sizes[d]):
        if base is None or sizes[d] - base > bucket_tolerance(base):
            base = sizes[d]
        buckets[d] = base
    layers: list[LayoutLayer] = []
    previous, index = 0, first_index
    numbers = dict(next_parts)
    protection = protection_for(drive_count)
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


# --- steps -------------------------------------------------------------------


def health_check(layout: Layout, skip: set[int] | None = None) -> Step:
    indexes = [layer.index for layer in layout.layers if not (skip and layer.index in skip)]
    return Step(
        "Check the other layers are healthy" if skip else "Check every layer is healthy",
        tuple(f"mdadm --detail --test {md_device(i)}" for i in indexes),
        "Stops here if an array is degraded: a second problem must be fixed first.",
    )


def partition_drive(device: str, parts: list[PartSpec], wipe: bool) -> Step:
    dev = shlex.quote(device)
    commands = []
    if wipe:
        commands += [
            f"sgdisk --zap-all {dev}",
            f"sgdisk -n 1:0:+16M -t 1:{LINUX_FS_TYPE} -c 1:{META_PARTLABEL} {dev}",
        ]
    commands += [
        f"sgdisk -n {n}:0:+{size // MiB}M -t {n}:{LINUX_RAID_TYPE} -c {n}:tk-l{layer} {dev}"
        for n, size, layer in parts
    ]
    commands.append(f"partprobe {dev}")
    if wipe:
        return Step(f"Partition {device}", tuple(commands))
    return Step(
        f"Add partitions in free space on {device}",
        tuple(commands),
        "Existing partitions are untouched.",
    )


def integrity_open(device: str, drive: int, numbers: list[int], suffix: str = "") -> list[str]:
    commands: list[str] = []
    for n in numbers:
        path = shlex.quote(partition_path(device, n))
        commands.append(f"integritysetup format --batch-mode --integrity crc32c {path}")
        commands.append(
            f"integritysetup open --integrity crc32c {path} "
            f"{integrity_device_name(drive, n)}{suffix}"
        )
    return commands


def widen_step(before: LayoutLayer, after: LayoutLayer) -> Step:
    md = md_device(before.index)
    added = member_path(after.members[-1])
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
            "the drive. The data stays online and protected throughout; mdadm resumes "
            "from its checkpoint after a power cut. Large layers take hours to days.",
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


def create_layer_step(layer: LayoutLayer) -> Step:
    members = " ".join(member_path(m) for m in layer.members)
    level = layer.level.value.removeprefix("raid")
    safety = "--consistency-policy=ppl" if layer.level is Level.RAID5 else "--bitmap=internal"
    return Step(
        f"Create layer {layer.index} from leftover space",
        (
            f"mdadm --create {md_device(layer.index)} --run --metadata=1.2 --level={level} "
            f"--raid-devices={layer.member_count} {safety} {members}",
        ),
    )


def expansion_steps(layout: Layout, exp: Expansion, own_drive: int) -> list[Step]:
    """Steps after the drive's own partitions exist: widen, pair, grow volume."""
    steps = [widen_step(before, after) for before, after in exp.joined]
    for d, disk in enumerate(layout.drives):
        if d == own_drive:
            continue
        parts = exp.partitions_on(d)
        if parts:
            step = partition_drive(disk.device, parts, wipe=False)
            integrity = integrity_open(disk.device, d, [n for n, _, _ in parts])
            steps.append(Step(step.title, step.commands + tuple(integrity), step.note))
    steps += [create_layer_step(layer) for layer in exp.created]
    if exp.changed:
        steps.append(
            grow_volume(
                grown=[md_device(w.index) for _, w in exp.joined],
                created=[md_device(layer.index) for layer in exp.created],
            )
        )
    return steps


def grow_volume(grown: list[str], created: list[str]) -> Step:
    commands = [f"pvresize {md}" for md in grown]
    for md in created:
        commands += [f"pvcreate {md}", f"vgextend {VG_NAME} {md}"]
    commands += [
        f"lvextend --extents +100%FREE {VG_NAME}/{LV_NAME}",
        f"btrfs filesystem resize max {MOUNT_POINT}",
    ]
    return Step("Grow the volume and file system", tuple(commands))


def metadata_step() -> Step:
    return Step(
        f"Write the new layout to every {META_PARTLABEL} partition",
        (),
        "Not implemented yet; --layout-out FILE saves it meanwhile.",
    )


def leftover_warnings(layout: Layout) -> list[str]:
    warnings = []
    for i, disk in enumerate(layout.drives):
        left = layout.free(i)
        if left >= GiB:
            warnings.append(
                f"{format_size(left)} on {disk.device} stays unused until another drive "
                "can pair with it."
            )
    return warnings


def render(plan: ChangePlan) -> list[str]:
    lines = ["# tamarack dry run: nothing below has been executed."]
    if plan.erases:
        lines.append(f"# DESTRUCTIVE for {plan.erases}: it will be erased.")
    lines.append("set -euo pipefail")
    for number, step in enumerate(plan.steps, start=1):
        lines += ["", f"# {number}. {step.title}"]
        if step.note:
            lines.append(f"#    {step.note}")
        lines += list(step.commands)
    return lines
