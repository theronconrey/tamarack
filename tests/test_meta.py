# SPDX-License-Identifier: GPL-3.0-or-later
"""The tk-meta layout record, written to ordinary files standing in for partitions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tamarack.layout import Layout, layout_from_plan
from tamarack.meta import BLOCK, MetaError, compact, decode, encode, main, read, write
from tamarack.planner import make_plan

TB = 10**12
LAYOUT = layout_from_plan(make_plan([4 * TB, 4 * TB], ["/dev/sda", "/dev/sdb"]))


def fake_partition(tmp_path: Path, name: str = "sda1", label: str = "tk-meta") -> Path:
    device = tmp_path / "dev" / name
    device.parent.mkdir(parents=True, exist_ok=True)
    device.write_bytes(b"\xff" * (64 * 1024))  # old contents
    uevent = tmp_path / "sys" / "class" / "block" / name / "uevent"
    uevent.parent.mkdir(parents=True)
    uevent.write_text(f"MAJOR=8\nMINOR=1\nDEVNAME={name}\nPARTN=1\nPARTNAME={label}\n")
    return device


def test_record_round_trip_and_padding() -> None:
    record = encode(compact(LAYOUT))
    assert len(record) % BLOCK == 0
    assert record.startswith(b"TAMARACK-META 1 ")
    assert Layout.from_json(decode(record + b"\xff" * 100)) == LAYOUT


@pytest.mark.parametrize(
    "damage",
    [
        lambda r: b"garbage" + r,
        lambda r: r.replace(b"raid1", b"raid5", 1),
        lambda r: r.replace(b"TAMARACK-META 1", b"TAMARACK-META 9", 1),
        lambda r: r[:40],
    ],
)
def test_damaged_records_are_refused(damage: object) -> None:
    with pytest.raises(MetaError):
        decode(damage(encode(compact(LAYOUT))))  # type: ignore[operator]


def test_compact_is_one_canonical_line() -> None:
    text = compact(LAYOUT)
    assert "\n" not in text and " " not in text
    assert json.loads(text) == LAYOUT.to_dict()


def test_write_then_read_a_partition(tmp_path: Path) -> None:
    device = fake_partition(tmp_path)
    write(str(device), compact(LAYOUT), sys_root=str(tmp_path))
    assert Layout.from_json(read(str(device))) == LAYOUT
    assert device.stat().st_size == 64 * 1024  # written in place, not truncated


def test_only_tk_meta_partitions_are_written(tmp_path: Path) -> None:
    device = fake_partition(tmp_path, "sda2", "tk-l1")
    before = device.read_bytes()
    with pytest.raises(MetaError, match="not a tk-meta partition"):
        write(str(device), compact(LAYOUT), sys_root=str(tmp_path))
    assert device.read_bytes() == before


def test_partition_name_from_udev_when_the_kernel_has_none(tmp_path: Path) -> None:
    # parted adds partitions with BLKPG, so the kernel's uevent has no PARTNAME.
    device = fake_partition(tmp_path, "sdb1", "")
    db = tmp_path / "run" / "udev" / "data" / "b8:1"
    db.parent.mkdir(parents=True)
    db.write_text("S:disk/by-partlabel/tk-meta\nE:ID_PART_ENTRY_NAME=tk-meta\n")
    write(str(device), compact(LAYOUT), sys_root=str(tmp_path))
    assert Layout.from_json(read(str(device))) == LAYOUT
    db.write_text("E:ID_PART_ENTRY_NAME=tk-l1\n")
    with pytest.raises(MetaError, match="not a tk-meta partition"):
        write(str(device), compact(LAYOUT), sys_root=str(tmp_path))


def test_only_valid_layouts_are_written(tmp_path: Path) -> None:
    device = fake_partition(tmp_path)
    with pytest.raises(ValueError):
        write(str(device), '{"format":"something-else"}', sys_root=str(tmp_path))


def test_cli_refuses_paths_outside_dev(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["write", "/etc/passwd", compact(LAYOUT)]) == 1
    assert "not a device path" in capsys.readouterr().err
