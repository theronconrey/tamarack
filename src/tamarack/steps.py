# SPDX-License-Identifier: GPL-3.0-or-later
"""Building blocks shared by every change to an existing layout.

A change (grow, replace) is a list of Steps, each a titled group of commands
with an optional note. ``expand`` works out how a drive with free space joins
existing layers and pairs leftover space into new layers; the grow and replace
planners both use it so the two never disagree about layout.

Everything here produces data and text. Nothing runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .commands import (
    FIRST_LAYER_START,
    LV_NAME,
    MD_DATA_OFFSET,
    META_PARTLABEL,
    MOUNT_POINT,
    VG_NAME,
    integrity_device_name,
    make_partition,
    md_device,
    new_partition_table,
    partition_path,
)
from .layout import Layout, LayoutLayer, Member
from .model import Level, PlanKind, Protection
from .ops import Command, Step, cmd, render_steps
from .planner import END_RESERVE, META_PARTITION, START_RESERVE, bucket_tolerance
from .policy import level_for, protection_for
from .units import GiB, align_down, format_size

# (partition number, size in bytes, layer index)
PartSpec = tuple[int, int, int]


class UnsupportedChange(ValueError):
    """The requested change is valid in principle but not implemented yet."""


@dataclass
class ChangePlan:
    kind: PlanKind
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


def wait_idle(md: str) -> Command:
    """Wait for md's resync, recovery or reshape to finish.

    md refuses to reshape, replace or drop the bitmap of an array that is
    still syncing. ``mdadm --wait`` exits 1 when there was nothing to wait
    for, which is not a failure here.
    """
    return cmd("mdadm", "--wait", md, tolerate_failure=True)


def health_check(layout: Layout, skip: set[int] | None = None) -> Step:
    indexes = [layer.index for layer in layout.layers if not (skip and layer.index in skip)]
    return Step(
        "Check the other layers are healthy" if skip else "Check every layer is healthy",
        tuple(
            c
            for i in indexes
            for c in (wait_idle(md_device(i)), cmd("mdadm", "--detail", "--test", md_device(i)))
        ),
        "Waits for any resync still running (a new array's first one, say), then stops "
        "here if an array is degraded: a second problem must be fixed first.",
    )


def partition_drive(
    device: str, parts: list[PartSpec], wipe: bool, start: int = FIRST_LAYER_START
) -> Step:
    """Partitions in order from ``start``, right after the drive's existing ones."""
    commands: list[Command] = []
    if wipe:
        commands += [
            new_partition_table(device),
            make_partition(device, 1, META_PARTLABEL, START_RESERVE, META_PARTITION),
        ]
    for n, size, layer in sorted(parts):
        commands.append(make_partition(device, n, f"tk-l{layer}", start, size))
        start += size
    commands.append(cmd("partprobe", device))
    if wipe:
        return Step(f"Partition {device}", tuple(commands))
    return Step(
        f"Add partitions in free space on {device}",
        tuple(commands),
        "Existing partitions are untouched.",
    )


def integrity_open(device: str, drive: int, numbers: list[int], suffix: str = "") -> list[Command]:
    commands: list[Command] = []
    for n in numbers:
        path = partition_path(device, n)
        name = f"{integrity_device_name(drive, n)}{suffix}"
        commands.append(
            cmd("integritysetup", "format", "--batch-mode", "--integrity", "crc32c", path)
        )
        commands.append(cmd("integritysetup", "open", "--integrity", "crc32c", path, name))
    return commands


def widen_step(before: LayoutLayer, after: LayoutLayer) -> Step:
    md = md_device(before.index)
    added = member_path(after.members[-1])
    width = after.member_count
    if before.level is Level.RAID1:
        return Step(
            f"Layer {before.index}: mirror -> single parity across {width} drives",
            (
                cmd("mdadm", "--grow", md, "--bitmap=none"),
                cmd("mdadm", "--grow", md, "--level=5"),
                cmd("mdadm", md, "--add", added),
                cmd("mdadm", "--grow", md, f"--raid-devices={width}"),
                wait_idle(md),
                cmd("mdadm", "--grow", md, "--consistency-policy=ppl"),
            ),
            "A two-drive mirror converts to single parity in place, then reshapes onto "
            "the drive. The data stays online and protected throughout; mdadm resumes "
            "from its checkpoint after a power cut. Large layers take hours to days.",
        )
    return Step(
        f"Layer {before.index}: widen single parity to {width} drives",
        (
            cmd("mdadm", "--grow", md, "--consistency-policy=resync"),
            cmd("mdadm", md, "--add", added),
            cmd("mdadm", "--grow", md, f"--raid-devices={width}"),
            wait_idle(md),
            cmd("mdadm", "--grow", md, "--consistency-policy=ppl"),
        ),
        "PPL is paused for the reshape and switched back on afterwards. The data stays "
        "online and protected; mdadm resumes from its checkpoint after a power cut.",
    )


def create_array(md: str, level: Level, members: list[str]) -> Command:
    """``mdadm --create`` with Tamarack's write-safety option for the level."""
    safety = "--consistency-policy=ppl" if level is Level.RAID5 else "--bitmap=internal"
    return cmd(
        "mdadm",
        *("--create", md, "--run", "--metadata=1.2", MD_DATA_OFFSET),
        f"--level={level.value.removeprefix('raid')}",
        f"--raid-devices={len(members)}",
        safety,
        *members,
    )


def create_layer_step(layer: LayoutLayer) -> Step:
    members = [member_path(m) for m in layer.members]
    return Step(
        f"Create layer {layer.index} from leftover space",
        (create_array(md_device(layer.index), layer.level, members),),
    )


def expansion_steps(layout: Layout, exp: Expansion, own_drive: int) -> list[Step]:
    """Steps after the drive's own partitions exist: widen, pair, grow volume."""
    steps = [widen_step(before, after) for before, after in exp.joined]
    for d, disk in enumerate(layout.drives):
        if d == own_drive:
            continue
        parts = exp.partitions_on(d)
        if parts:
            start = FIRST_LAYER_START + layout.allocated(d)
            step = partition_drive(disk.device, parts, wipe=False, start=start)
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


def clear_signatures(md: str) -> Command:
    """Wipe old signatures from a new array before it becomes a PV.

    A new array reuses space that belonged to an older one, often at the
    same data offset, so the old PV label shows through and LVM sees two
    PVs with one ID.
    """
    return cmd("wipefs", "--all", md)


def grow_volume(grown: list[str], created: list[str]) -> Step:
    commands = [cmd("pvresize", md) for md in grown]
    for md in created:
        commands += [clear_signatures(md), cmd("pvcreate", md), cmd("vgextend", VG_NAME, md)]
    commands += [
        cmd("lvextend", "--extents", "+100%FREE", f"{VG_NAME}/{LV_NAME}"),
        cmd("btrfs", "filesystem", "resize", "max", MOUNT_POINT),
    ]
    return Step("Grow the volume and file system", tuple(commands))


def metadata_commands(layout: Layout) -> tuple[Command, ...]:
    """Write ``layout`` to the tk-meta partition of each of its drives."""
    from .meta import compact

    record = compact(layout)
    return tuple(
        cmd("tamarack-meta", "write", partition_path(disk.device, 1), record)
        for disk in layout.drives
    )


def metadata_step(after: Layout) -> Step:
    return Step(
        f"Write the new layout to every {META_PARTLABEL} partition",
        metadata_commands(after),
        "Each drive carries the whole layout, so the box can be read from any of them.",
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
    warnings = [f"# DESTRUCTIVE for {plan.erases}: it will be erased."] if plan.erases else []
    return render_steps(warnings, plan.steps)
