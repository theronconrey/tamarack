# SPDX-License-Identifier: GPL-3.0-or-later
"""The engine core and the UDisks2 rules, without a bus."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from fake_runner import FakeRunner

from tamarack.engine import (
    PLAN_LIFETIME,
    Busy,
    DriveChanged,
    DriveInfo,
    Engine,
    InvalidArgument,
    LayoutExists,
    LayoutUnreadable,
    NoLayout,
    NoSuchDrive,
    NoSuchPlan,
    TooManyPlans,
    validate_device_path,
)
from tamarack.job import start
from tamarack.layout import layout_from_plan
from tamarack.planner import make_plan
from tamarack.review import digest, document
from tamarack.udisks import find_drive

TB = 10**12


class FakeDrives:
    def __init__(self, drives: dict[str, tuple[int, str]]) -> None:
        self.drives = drives
        self.lookups: list[tuple[str, bool]] = []

    def lookup(self, device: str, unused: bool) -> DriveInfo:
        self.lookups.append((device, unused))
        if device not in self.drives:
            raise NoSuchDrive(f"{device}: no such block device")
        size, identity = self.drives[device]
        return DriveInfo(device, size, identity)


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


DRIVES = {
    "/dev/sda": (4 * TB, "wwn:0x1"),
    "/dev/sdb": (4 * TB, "wwn:0x2"),
    "/dev/sdc": (8 * TB, "serial:C"),
    "/dev/sdd": (8 * TB, "serial:D"),
    "/dev/blank": (4 * TB, ""),
}


def engine(tmp_path: Path, layout_of: list[str] | None = None, **kw: Any) -> Engine:
    if layout_of:
        plan = make_plan(
            [DRIVES[d][0] for d in layout_of], layout_of, [DRIVES[d][1] for d in layout_of]
        )
        (tmp_path / "layout.json").write_text(layout_from_plan(plan).to_json())
    return Engine(str(tmp_path), FakeDrives(dict(DRIVES)), clock=kw.pop("clock", Clock()), **kw)


# --- device paths ------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["/dev/sda", "/dev/nvme0n1", "/dev/disk/by-id/ata-WDC_WD40EFRX-68N32N0_WD-WCC7K1"]
)
def test_device_paths_accepted(path: str) -> None:
    assert validate_device_path(path) == path


@pytest.mark.parametrize(
    "path",
    [
        "sda",
        "/dev/",
        "/dev/../etc/passwd",
        "/dev//sda",
        "/dev/sda/",
        "/etc/passwd",
        "/dev/a b",
        "/dev/sda\n",
        "/dev/" + "x" * 300,
        "/dev/./sda",
    ],
)
def test_device_paths_refused(path: str) -> None:
    with pytest.raises(InvalidArgument):
        validate_device_path(path)


# --- planning ----------------------------------------------------------------


def test_create_plan_is_stored_with_digest_and_expiry(tmp_path: Path) -> None:
    clock = Clock()
    e = engine(tmp_path, clock=clock)
    plan = e.plan_create(["/dev/sda", "/dev/sdb", "/dev/sdc"])
    expected = document(
        make_plan(
            [4 * TB, 4 * TB, 8 * TB],
            ["/dev/sda", "/dev/sdb", "/dev/sdc"],
            ["wwn:0x1", "wwn:0x2", "serial:C"],
        )
    )
    assert plan.digest == digest(expected)
    assert plan.erases == ["/dev/sda", "/dev/sdb", "/dev/sdc"]
    assert plan.expires_at == int(clock.now) + PLAN_LIFETIME
    assert plan.steps[0][0] == "Partition each drive"
    assert plan.steps[0][2][0] == "sgdisk --zap-all /dev/sda"
    assert plan.summary["after"]["drive_failures_tolerated"] == 1
    assert e.drives.lookups == [(d, True) for d in ("/dev/sda", "/dev/sdb", "/dev/sdc")]  # type: ignore[attr-defined]


def test_plans_expire(tmp_path: Path) -> None:
    clock = Clock()
    e = engine(tmp_path, clock=clock)
    plan = e.plan_create(["/dev/sda"])
    clock.now += PLAN_LIFETIME - 1
    assert e.plan_ids() == [plan.id]
    clock.now += 1
    assert e.plan_ids() == []
    with pytest.raises(NoSuchPlan):
        e.plan(plan.id)


def test_discard_and_plan_limit(tmp_path: Path) -> None:
    e = engine(tmp_path)
    plans = [e.plan_create(["/dev/sda"]) for _ in range(16)]
    with pytest.raises(TooManyPlans):
        e.plan_create(["/dev/sda"])
    e.discard(plans[0].id)
    assert e.plan_create(["/dev/sda"]).id == 17
    with pytest.raises(NoSuchPlan):
        e.discard(plans[0].id)


def test_create_refused_when_a_layout_exists(tmp_path: Path) -> None:
    with pytest.raises(LayoutExists):
        engine(tmp_path, ["/dev/sda", "/dev/sdb"]).plan_create(["/dev/sdc"])


def test_change_refused_without_a_layout(tmp_path: Path) -> None:
    with pytest.raises(NoLayout):
        engine(tmp_path).plan_add("/dev/sdc")


def test_unreadable_layout_blocks_all_planning(tmp_path: Path) -> None:
    (tmp_path / "layout.json").write_text("{not json")
    e = engine(tmp_path)
    for attempt in (lambda: e.plan_create(["/dev/sda"]), lambda: e.plan_add("/dev/sda")):
        with pytest.raises(LayoutUnreadable):
            attempt()


def test_add_plan(tmp_path: Path) -> None:
    e = engine(tmp_path, ["/dev/sda", "/dev/sdb"])
    plan = e.plan_add("/dev/sdc")
    assert plan.kind.value == "add"
    assert plan.erases == ["/dev/sdc"]
    assert plan.summary["before"]["drives"] == ["/dev/sda", "/dev/sdb"]
    assert e.usable_bytes > 0 and e.drive_failures_tolerated == 1
    assert '"tamarack-layout"' in e.layout_json


def test_replace_checks_the_old_drive_is_the_one_in_the_layout(tmp_path: Path) -> None:
    e = engine(tmp_path, ["/dev/sda", "/dev/sdb", "/dev/sdc"])
    plan = e.plan_replace("/dev/sdb", "/dev/sdd")
    assert plan.kind.value == "replace"
    assert ("/dev/sdb", False) in e.drives.lookups  # type: ignore[attr-defined]
    e.drives.drives["/dev/sdb"] = (4 * TB, "wwn:0xother")  # type: ignore[attr-defined]
    with pytest.raises(DriveChanged):
        e.plan_replace("/dev/sdb", "/dev/sdd")


def test_rebuild_does_not_need_the_failed_drive(tmp_path: Path) -> None:
    e = engine(tmp_path, ["/dev/sda", "/dev/sdb", "/dev/sdc"])
    del e.drives.drives["/dev/sdb"]  # type: ignore[attr-defined]
    plan = e.plan_rebuild("/dev/sdb", "/dev/sdd")
    assert plan.kind.value == "rebuild"


@pytest.mark.parametrize(
    ("call", "error"),
    [
        (lambda e: e.plan_add("/dev/nope"), NoSuchDrive),
        (lambda e: e.plan_add("/dev/blank"), NoSuchDrive),
        (lambda e: e.plan_add("/dev/../sdc"), InvalidArgument),
        (lambda e: e.plan_add("/dev/sda"), InvalidArgument),
        (lambda e: e.plan_replace("/dev/sdz", "/dev/sdd"), InvalidArgument),
        (lambda e: e.plan_rebuild("/dev/sda", "/dev/sda"), InvalidArgument),
    ],
)
def test_planning_errors(tmp_path: Path, call: Any, error: type[Exception]) -> None:
    with pytest.raises(error):
        call(engine(tmp_path, ["/dev/sda", "/dev/sdb"]))


def test_unfinished_job_makes_the_engine_busy(tmp_path: Path) -> None:
    e = engine(tmp_path, ["/dev/sda", "/dev/sdb"])
    assert e.state == "idle"
    plan = e.plan_add("/dev/sdc")
    (tmp_path / "jobs").mkdir()
    start(
        plan.document, "1", str(tmp_path / "jobs" / "1.jsonl"), FakeRunner(), gate=lambda n: "stop"
    )
    assert e.state == "paused" and e.unfinished_jobs() == ["1"]
    with pytest.raises(Busy, match="job 1"):
        e.plan_add("/dev/sdd")


def test_unreadable_journal_counts_as_unfinished(tmp_path: Path) -> None:
    (tmp_path / "jobs").mkdir()
    (tmp_path / "jobs" / "3.jsonl").write_text("garbage\n")
    (tmp_path / "jobs" / "notes.txt").write_text("ignored")
    assert engine(tmp_path).unfinished_jobs() == ["3"]


# --- status ------------------------------------------------------------------


def test_degraded_layers_from_sysfs(tmp_path: Path) -> None:
    state, root = tmp_path / "state", tmp_path / "root"
    state.mkdir()
    e = engine(state, ["/dev/sda", "/dev/sdb", "/dev/sdc", "/dev/sdd"], root=str(root))
    (root / "dev" / "md").mkdir(parents=True)
    assert e.degraded_layers() == [1, 2]  # nothing assembled
    for layer, md, missing in ((1, "md127", "0"), (2, "md126", "1")):
        os.symlink(f"../{md}", root / "dev" / "md" / f"tk-l{layer}")
        (root / "dev" / md).touch()
        (root / "sys" / "block" / md / "md").mkdir(parents=True)
        (root / "sys" / "block" / md / "md" / "degraded").write_text(missing + "\n")
    assert e.degraded_layers() == [2] and e.degraded


def test_last_backup_confirmed(tmp_path: Path) -> None:
    e = engine(tmp_path)
    assert e.last_backup_confirmed == 0
    (tmp_path / "backup-confirmed").write_text("1800000000\n")
    assert e.last_backup_confirmed == 1_800_000_000
    (tmp_path / "backup-confirmed").write_text("soon")
    assert e.last_backup_confirmed == 0


def test_on_battery_comes_from_the_source(tmp_path: Path) -> None:
    assert engine(tmp_path, on_battery=lambda: True).on_battery


# --- UDisks2 rules -----------------------------------------------------------

B, D, P, F = (
    "org.freedesktop.UDisks2.Block",
    "org.freedesktop.UDisks2.Drive",
    "org.freedesktop.UDisks2.Partition",
    "org.freedesktop.UDisks2.Filesystem",
)


def udisks(**extra: Any) -> dict[str, Any]:
    sda = "/org/freedesktop/UDisks2/block_devices/sda"
    objects: dict[str, Any] = {
        "/org/freedesktop/UDisks2/drives/WDC_1": {
            D: {"WWN": "0x50014EE2B1C2D3E4", "Serial": "WD-1", "Size": 4 * TB}
        },
        sda: {
            B: {
                "Device": b"/dev/sda\0",
                "Symlinks": [
                    b"/dev/disk/by-id/ata-WDC_1\0",
                    b"/dev/disk/by-id/wwn-0x50014ee2b1c2d3e4\0",
                ],
                "Size": 4 * TB,
                "Drive": "/org/freedesktop/UDisks2/drives/WDC_1",
                "ReadOnly": False,
                "MDRaidMember": "/",
            }
        },
        f"{sda}1": {
            B: {"Device": list(b"/dev/sda1\0"), "Drive": "/org/freedesktop/UDisks2/drives/WDC_1"},
            P: {"Table": sda, "Number": 1},
            F: {"MountPoints": extra.get("mounts", [])},
        },
        "/org/freedesktop/UDisks2/block_devices/md127": {
            B: {"Device": b"/dev/md127\0", "Drive": "/", "Size": TB}
        },
    }
    return objects


def test_udisks_finds_a_drive_by_symlink() -> None:
    info = find_drive(udisks(), "/dev/disk/by-id/ata-WDC_1", unused=True)
    assert info == DriveInfo("/dev/disk/by-id/ata-WDC_1", 4 * TB, "wwn:0x50014ee2b1c2d3e4")


@pytest.mark.parametrize(
    ("device", "objects", "message"),
    [
        ("/dev/sdq", udisks(), "no such block device"),
        ("/dev/sda1", udisks(), "is a partition"),
        ("/dev/md127", udisks(), "not a physical drive"),
        ("/dev/sda", udisks(mounts=[b"/boot\0"]), "/dev/sda1 has a mounted file system"),
    ],
)
def test_udisks_refusals(device: str, objects: dict[str, Any], message: str) -> None:
    with pytest.raises((NoSuchDrive, InvalidArgument), match=message):
        find_drive(objects, device, unused=True)


def test_udisks_in_use_rules_only_apply_to_drives_to_be_erased() -> None:
    objects = udisks(mounts=[b"/srv\0"])
    assert find_drive(objects, "/dev/sda", unused=False).identity == "wwn:0x50014ee2b1c2d3e4"


@pytest.mark.parametrize(
    ("iface", "props", "message"),
    [
        (B, {"MDRaidMember": "/org/freedesktop/UDisks2/mdraid/x"}, "md array"),
        ("org.freedesktop.UDisks2.Swapspace", {"Active": True}, "swap"),
        ("org.freedesktop.UDisks2.Encrypted", {"CleartextDevice": "/o/dm_0"}, "encrypted"),
        ("org.freedesktop.UDisks2.PhysicalVolume", {}, "LVM"),
    ],
)
def test_udisks_other_uses(iface: str, props: dict[str, Any], message: str) -> None:
    objects = udisks()
    sda = objects["/org/freedesktop/UDisks2/block_devices/sda"]
    sda.setdefault(iface, {}).update(props)
    with pytest.raises(InvalidArgument, match=message):
        find_drive(objects, "/dev/sda", unused=True)
