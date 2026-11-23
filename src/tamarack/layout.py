# SPDX-License-Identifier: GPL-3.0-or-later
"""The layout that exists on disk: drives, layers and which partition is where.

A Plan describes what a set of drives *should* look like. A Layout records
what was actually built, including history the drive sizes alone cannot
reveal (a two-drive layout grown from one drive has two mirror layers; a
fresh two-drive layout has one). Grow operations read a Layout and produce
the next one.

The JSON form is versioned. It is what will be written to every tk-meta
partition so a box can be rebuilt or inspected from its drives alone.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .model import Level, Plan, Protection
from .planner import check_identities, usable_space
from .policy import protection_for

FORMAT_VERSION = 1


@dataclass(frozen=True)
class Member:
    drive: int  # index into Layout.drives
    partition: int  # GPT partition number on that drive


@dataclass(frozen=True)
class LayoutLayer:
    index: int
    level: Level
    slice_size: int
    members: tuple[Member, ...]

    @property
    def drive_indexes(self) -> set[int]:
        return {m.drive for m in self.members}

    @property
    def copies_on_same_drive(self) -> bool:
        return len(self.drive_indexes) < len(self.members)

    @property
    def member_count(self) -> int:
        return len(self.members)

    @property
    def usable_nominal(self) -> int:
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
        if self.copies_on_same_drive:
            return 0
        if self.level is Level.RAID1:
            return self.member_count - 1
        if self.level is Level.RAID5:
            return 1
        if self.level is Level.RAID6:
            return 2
        return 0


@dataclass(frozen=True)
class Disk:
    device: str
    size: int
    identity: str = ""  # wwn:... or serial:..., see tamarack.identity

    @property
    def usable(self) -> int:
        return usable_space(self.size)


@dataclass
class Layout:
    drives: list[Disk]
    layers: list[LayoutLayer] = field(default_factory=list)

    @property
    def protection(self) -> Protection:
        return protection_for(len(self.drives))

    @property
    def usable_nominal(self) -> int:
        return sum(layer.usable_nominal for layer in self.layers)

    @property
    def drive_failures_tolerated(self) -> int:
        if not self.layers:
            return 0
        return min(layer.drive_failures_tolerated for layer in self.layers)

    def allocated(self, drive: int) -> int:
        return sum(
            layer.slice_size
            for layer in self.layers
            for member in layer.members
            if member.drive == drive
        )

    def free(self, drive: int) -> int:
        return self.drives[drive].usable - self.allocated(drive)

    def next_partition(self, drive: int) -> int:
        numbers = [m.partition for layer in self.layers for m in layer.members if m.drive == drive]
        return max(numbers, default=1) + 1  # partition 1 is tk-meta

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": "tamarack-layout",
            "version": FORMAT_VERSION,
            "drives": [_disk_dict(d) for d in self.drives],
            "layers": [
                {
                    "index": layer.index,
                    "level": layer.level.value,
                    "slice_bytes": layer.slice_size,
                    "members": [
                        {"drive": m.drive, "partition": m.partition} for m in layer.members
                    ],
                }
                for layer in self.layers
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Layout:
        if data.get("format") != "tamarack-layout":
            raise ValueError("not a tamarack layout file")
        if data.get("version") != FORMAT_VERSION:
            raise ValueError(f"unsupported layout version: {data.get('version')!r}")
        drives = [
            Disk(device=str(d["device"]), size=int(d["size"]), identity=str(d.get("id", "")))
            for d in data["drives"]
        ]
        layers = [
            LayoutLayer(
                index=int(layer["index"]),
                level=Level(layer["level"]),
                slice_size=int(layer["slice_bytes"]),
                members=tuple(
                    Member(drive=int(m["drive"]), partition=int(m["partition"]))
                    for m in layer["members"]
                ),
            )
            for layer in data["layers"]
        ]
        layout = cls(drives=drives, layers=layers)
        layout.validate()
        return layout

    @classmethod
    def from_json(cls, text: str) -> Layout:
        return cls.from_dict(json.loads(text))

    def validate(self) -> None:
        if not self.drives:
            raise ValueError("layout has no drives")
        devices = [d.device for d in self.drives]
        if len(set(devices)) != len(devices):
            raise ValueError("layout lists a device twice")
        check_identities([d.identity for d in self.drives])
        seen: set[tuple[int, int]] = set()
        for layer in self.layers:
            if layer.level is Level.UNUSED:
                raise ValueError(f"layer {layer.index}: unused slices are not stored")
            for m in layer.members:
                if not 0 <= m.drive < len(self.drives):
                    raise ValueError(f"layer {layer.index}: no drive {m.drive}")
                if m.partition < 2:
                    raise ValueError(f"layer {layer.index}: partition {m.partition} is reserved")
                if (m.drive, m.partition) in seen:
                    raise ValueError(f"layer {layer.index}: partition used twice")
                seen.add((m.drive, m.partition))
        for i in range(len(self.drives)):
            if self.free(i) < 0:
                raise ValueError(f"drive {self.drives[i].device} is over-allocated")


def _disk_dict(disk: Disk) -> dict[str, Any]:
    # "id" is optional within format version 1: files written before drive
    # identities existed stay valid, and files without identities stay as they were.
    out: dict[str, Any] = {"device": disk.device, "size": disk.size}
    if disk.identity:
        out["id"] = disk.identity
    return out


def layout_from_plan(plan: Plan) -> Layout:
    """The layout ``tamarack plan --commands`` would create."""
    from .commands import partitions  # local import: commands imports model only

    parts = partitions(plan)
    layers = [
        LayoutLayer(
            index=layer.index,
            level=layer.level,
            slice_size=layer.slice_size,
            members=tuple(Member(p.drive, p.number) for p in parts if p.layer == layer.index),
        )
        for layer in plan.used_layers
    ]
    drives = [Disk(device=d.device, size=d.size, identity=d.identity) for d in plan.drives]
    return Layout(drives=drives, layers=layers)
