# SPDX-License-Identifier: GPL-3.0-or-later
"""Data types shared by the planner, the command generator and the CLI."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Level(StrEnum):
    """How one layer is protected. Never shown to users as a choice."""

    RAID1 = "raid1"  # mirror (2-way, or 3-way under two-drive protection)
    RAID5 = "raid5"  # single parity
    RAID6 = "raid6"  # dual parity
    UNUSED = "unused"  # slice exists on only one drive: left unallocated


class Protection(StrEnum):
    """Redundancy target picked automatically from the drive count."""

    SAME_DRIVE_MIRROR = "same-drive-mirror"  # 1 drive: two partitions mirrored
    ONE_DRIVE = "one-drive"  # survives one drive failure
    TWO_DRIVE = "two-drive"  # survives two drive failures

    @property
    def drive_failures_tolerated(self) -> int:
        return {"same-drive-mirror": 0, "one-drive": 1, "two-drive": 2}[self.value]


@dataclass(frozen=True)
class Drive:
    device: str  # e.g. /dev/disk/by-id/ata-WDC_..., or a placeholder
    size: int  # bytes, as reported by the drive
    usable: int  # bytes available for layers after reserved space
    bucket: int  # usable size after grouping near-equal drives


@dataclass(frozen=True)
class Layer:
    index: int  # 1-based, smallest slice first
    level: Level
    slice_size: int  # bytes per member partition
    members: tuple[int, ...]  # drive indexes (into Plan.drives)
    copies_on_same_drive: bool = False  # single-drive mirror

    @property
    def member_count(self) -> int:
        return 2 if self.copies_on_same_drive else len(self.members)

    @property
    def usable_nominal(self) -> int:
        """Usable bytes before dm-integrity overhead."""
        m = self.member_count
        if self.level is Level.RAID1:
            return self.slice_size
        if self.level is Level.RAID5:
            return (m - 1) * self.slice_size
        if self.level is Level.RAID6:
            return (m - 2) * self.slice_size
        return 0

    @property
    def drive_failures_tolerated(self) -> int:
        if self.copies_on_same_drive or self.level is Level.UNUSED:
            return 0
        if self.level is Level.RAID1:
            return self.member_count - 1
        if self.level is Level.RAID5:
            return 1
        return 2


@dataclass
class Plan:
    drives: list[Drive]
    protection: Protection
    layers: list[Layer]
    integrity_overhead: float
    warnings: list[str] = field(default_factory=list)

    @property
    def used_layers(self) -> list[Layer]:
        return [layer for layer in self.layers if layer.level is not Level.UNUSED]

    @property
    def raw_total(self) -> int:
        return sum(d.size for d in self.drives)

    @property
    def usable_nominal(self) -> int:
        return sum(layer.usable_nominal for layer in self.layers)

    @property
    def usable_estimate(self) -> int:
        """Usable bytes after the estimated dm-integrity overhead."""
        return int(self.usable_nominal * (1 - self.integrity_overhead))

    @property
    def unallocated(self) -> int:
        """Bytes on drives that no protected layer uses (growth headroom)."""
        used = 0
        for layer in self.used_layers:
            used += layer.slice_size * layer.member_count
        return self.raw_total - used

    @property
    def drive_failures_tolerated(self) -> int:
        used = self.used_layers
        if not used:
            return 0
        return min(layer.drive_failures_tolerated for layer in used)
