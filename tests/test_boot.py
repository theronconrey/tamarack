# SPDX-License-Identifier: GPL-3.0-or-later
"""Opening the pool at boot: the Tamarack lines in /etc/integritytab."""

from __future__ import annotations

from pathlib import Path

import pytest
from fake_runner import FakeRunner
from test_engine import run_engine

from tamarack.boot import BEGIN, END, BootError, lines, members, merge
from tamarack.layout import layout_from_plan
from tamarack.planner import make_plan

TB = 10**12
UUID = "0a1b2c3d-0000-4000-8000-{:012d}"


def layout(*sizes: int):  # type: ignore[no-untyped-def]
    devices = [f"/dev/sd{'abcd'[i]}" for i in range(len(sizes))]
    return layout_from_plan(make_plan([s * TB for s in sizes], devices))


def test_one_line_per_layer_partition() -> None:
    lay = layout(4, 4, 8, 8)
    uuids = {key: UUID.format(i) for i, key in enumerate(members(lay))}
    out = lines(lay, uuids)
    assert len(out) == len(members(lay)) == 6  # 4 in layer 1, 2 in layer 2
    assert out[0] == f"tk-d1-p2 PARTUUID={UUID.format(0)} - integrity-algorithm=crc32c"


def test_a_missing_uuid_is_an_error() -> None:
    with pytest.raises(BootError, match="partition 2 of /dev/sda"):
        lines(layout(4, 4), {})
    with pytest.raises(BootError):
        lines(layout(4, 4), dict.fromkeys(members(layout(4, 4)), "not-a-uuid"))


def test_merge_keeps_other_lines_and_replaces_ours() -> None:
    mine = ["tk-d1-p2 PARTUUID=x - integrity-algorithm=crc32c"]
    other = "# the admin's own\nscratch /dev/sdz1 - mode=bitmap\n"
    once = merge(other, mine)
    assert once.startswith(other) and BEGIN in once and END in once
    again = merge(once, ["tk-d1-p2 PARTUUID=y - integrity-algorithm=crc32c"])
    assert again.count(BEGIN) == 1 and "PARTUUID=x" not in again and "PARTUUID=y" in again
    assert merge(again, []) == other
    assert merge("", []) == ""
    assert merge("", mine) == f"{BEGIN}\n{mine[0]}\n{END}\n"


def test_engine_writes_integritytab_for_its_pool(tmp_path: Path) -> None:
    e = run_engine(tmp_path, ["/dev/sda", "/dev/sdb"])
    (Path(e.root) / "etc").mkdir()
    tab = Path(e.root) / "etc" / "integritytab"
    tab.write_text("keep me\n")
    assert e.sync_boot() is True
    text = tab.read_text()
    assert text.startswith("keep me\n") and text.count("integrity-algorithm=crc32c") == 2
    assert e.sync_boot() is False  # nothing changed


def test_a_missing_drive_leaves_the_file_alone(tmp_path: Path) -> None:
    e = run_engine(tmp_path, ["/dev/sda", "/dev/sdb"])
    (Path(e.root) / "etc").mkdir()
    tab = Path(e.root) / "etc" / "integritytab"
    e.sync_boot()
    before = tab.read_text()
    del e.drives.drives["/dev/sdb"]  # type: ignore[attr-defined]
    assert e.sync_boot() is False and tab.read_text() == before


def test_a_completed_job_updates_integritytab(tmp_path: Path) -> None:
    e = run_engine(tmp_path, runner=FakeRunner())
    (Path(e.root) / "etc").mkdir()
    plan = e.plan_create(["/dev/sda", "/dev/sdb"])
    run = e.execute(plan.id, plan.digest, {"confirm-erase": plan.erases, "no-ups": True})
    e.job_finished(run.job_id, run.run())
    assert (Path(e.root) / "etc" / "integritytab").read_text().count("tk-d") == 2


def test_udisks_partition_uuids() -> None:
    from test_engine import P, udisks

    from tamarack.udisks import find_partition_uuids

    objects = udisks()
    objects["/org/freedesktop/UDisks2/block_devices/sda1"][P]["UUID"] = UUID.format(1)
    assert find_partition_uuids(objects, "/dev/sda") == {1: UUID.format(1)}
