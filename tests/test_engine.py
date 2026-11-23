# SPDX-License-Identifier: GPL-3.0-or-later
"""The engine core and the UDisks2 rules, without a bus."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from fake_runner import FakeRunner

from tamarack.engine import (
    PLAN_LIFETIME,
    Busy,
    Degraded,
    DriveChanged,
    DriveInfo,
    Engine,
    EngineError,
    InvalidArgument,
    LayoutExists,
    LayoutUnreadable,
    NoLayout,
    NoRecentBackup,
    NoSuchDrive,
    NoSuchPlan,
    NotBetweenSteps,
    NotConfirmed,
    NotSupported,
    NoUps,
    OnBattery,
    StalePlan,
    TooManyPlans,
    parse_resume_options,
    validate_device_path,
)
from tamarack.job import Resolution, start
from tamarack.journal import load
from tamarack.layout import Layout, layout_from_plan
from tamarack.planner import make_plan
from tamarack.review import digest, document
from tamarack.udisks import find_drive

TB = 10**12


class FakeDrives:
    def __init__(self, drives: dict[str, tuple[int, str]]) -> None:
        self.drives = drives
        self.lookups: list[tuple[str, bool]] = []
        self.smart: dict[str, tuple[str, str]] = {}

    def present(self, devices: list[str]) -> dict[str, str]:
        return {d: self.drives[d][1] for d in devices if d in self.drives}

    def health(self, devices: list[str]) -> dict[str, tuple[str, str]]:
        return {
            d: self.smart.get(d, ("ok", "")) if d in self.drives else ("missing", "not attached")
            for d in devices
        }

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


# --- executing ---------------------------------------------------------------

CONFIRM_ALL = {"no-ups": True}


def healthy_sysfs(root: Path, layers: int) -> None:
    for layer in range(1, layers + 1):
        md = f"md{127 - layer}"
        (root / "dev" / "md").mkdir(parents=True, exist_ok=True)
        os.symlink(f"../{md}", root / "dev" / "md" / f"tk-l{layer}")
        (root / "dev" / md).touch()
        (root / "sys" / "block" / md / "md").mkdir(parents=True)
        (root / "sys" / "block" / md / "md" / "degraded").write_text("0\n")


def run_engine(tmp_path: Path, layout_of: list[str] | None = None, **kw: Any) -> Engine:
    state = tmp_path / "state"
    state.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    kw.setdefault("runner", FakeRunner())
    e = engine(state, layout_of, root=str(root), **kw)
    if e.layout:
        healthy_sysfs(root, len(e.layout.layers))
    return e


def opts(plan: Any, **extra: Any) -> dict[str, Any]:
    return {"confirm-erase": list(plan.erases), **CONFIRM_ALL, **extra}


def test_execute_needs_a_runner(tmp_path: Path) -> None:
    e = run_engine(tmp_path, runner=None)
    plan = e.plan_create(["/dev/sda"])
    with pytest.raises(NotSupported):
        e.authorize_execute(plan.id, plan.digest, opts(plan))


def test_execute_a_create_plan(tmp_path: Path) -> None:
    e = run_engine(tmp_path)
    plan = e.plan_create(["/dev/sda", "/dev/sdb"])
    action, details = e.authorize_execute(plan.id, plan.digest, opts(plan))
    assert action == "com.borealislogistics.tamarack.create"
    assert details == {"kind": "create", "erases": "/dev/sda /dev/sdb"}
    run = e.execute(plan.id, plan.digest, opts(plan))
    assert run.job_id == "1" and e.state == "running"
    with pytest.raises(Busy):
        e.plan_create(["/dev/sdc"])
    report = run.run()
    assert report.state == "completed"
    assert e.job_finished("1", report) == report
    assert e.state == "idle" and e.layout is not None
    assert [d.device for d in e.layout.drives] == ["/dev/sda", "/dev/sdb"]
    assert e.plan_ids() == []  # the plan was used up
    assert e.job_status("1").state == "completed"
    assert e.job_ids() == ["1"]


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (lambda e, p: ("sha256:other", opts(p)), StalePlan),
        (lambda e, p: (p.digest, {"confirm-erase": ["/dev/sda"], "no-ups": True}), NotConfirmed),
        (lambda e, p: (p.digest, {"no-ups": True}), NotConfirmed),
        (lambda e, p: (p.digest, opts(p, surprise=1)), InvalidArgument),
        (lambda e, p: (p.digest, {"confirm-erase": list(p.erases)}), NoUps),
    ],
)
def test_execute_refusals(tmp_path: Path, change: Any, error: type[Exception]) -> None:
    e = run_engine(tmp_path)
    plan = e.plan_create(["/dev/sda", "/dev/sdb"])
    plan_digest, options = change(e, plan)
    with pytest.raises(error):
        e.execute(plan.id, plan_digest, options)
    assert e.runner.calls == []  # type: ignore[union-attr]


def test_layout_changed_since_planning(tmp_path: Path) -> None:
    e = run_engine(tmp_path, ["/dev/sda", "/dev/sdb"])
    plan = e.plan_add("/dev/sdc")
    other = make_plan([4 * TB, 4 * TB], ["/dev/sda", "/dev/sdb"], ["wwn:0x1", "wwn:0x2"])
    changed = layout_from_plan(other).to_dict()
    changed["layers"][0]["slice_bytes"] -= 1 << 20
    (Path(e.state_dir) / "layout.json").write_text(json.dumps(changed))
    e.reload()
    with pytest.raises(StalePlan, match="layout changed"):
        e.execute(plan.id, plan.digest, opts(plan))


def test_drive_swapped_after_planning(tmp_path: Path) -> None:
    e = run_engine(tmp_path)
    plan = e.plan_create(["/dev/sda", "/dev/sdb"])
    e.drives.drives["/dev/sdb"] = (4 * TB, "wwn:0xbeef")  # type: ignore[attr-defined]
    with pytest.raises(DriveChanged, match="/dev/sdb"):
        e.execute(plan.id, plan.digest, opts(plan))


def test_on_battery_refuses_execute(tmp_path: Path) -> None:
    e = run_engine(tmp_path, on_battery=lambda: True)
    plan = e.plan_create(["/dev/sda"])
    with pytest.raises(OnBattery):
        e.execute(plan.id, plan.digest, opts(plan))


def test_a_ups_makes_no_ups_unnecessary(tmp_path: Path) -> None:
    e = run_engine(tmp_path, ups_present=lambda: True)
    plan = e.plan_create(["/dev/sda"])
    e.execute(plan.id, plan.digest, {"confirm-erase": ["/dev/sda"]})


def test_changes_need_a_recent_backup(tmp_path: Path) -> None:
    clock = Clock()
    e = run_engine(tmp_path, ["/dev/sda", "/dev/sdb"], clock=clock)
    plan = e.plan_add("/dev/sdc")
    action, _ = e.authorize_execute(plan.id, plan.digest, opts(plan))
    assert action == "com.borealislogistics.tamarack.add-drive"
    with pytest.raises(NoRecentBackup):
        e.execute(plan.id, plan.digest, opts(plan))
    e.confirm_backup()
    clock.now += 7 * 24 * 3600 + 1
    plan = e.plan_add("/dev/sdc")
    with pytest.raises(NoRecentBackup):
        e.execute(plan.id, plan.digest, opts(plan))
    e.confirm_backup()
    assert e.execute(plan.id, plan.digest, opts(plan)).job_id == "1"
    assert stat_mode(Path(e.state_dir) / "backup-confirmed") == 0o644


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_degraded_arrays_block_everything_but_their_rebuild(tmp_path: Path) -> None:
    # 4+4+8+8 TB: layer 1 spans all four drives, layer 2 only the 8 TB pair.
    e = run_engine(tmp_path, ["/dev/sda", "/dev/sdb", "/dev/sdc", "/dev/sdd"])
    e.confirm_backup()
    sysfs = Path(e.root) / "sys" / "block"
    (sysfs / "md126" / "md" / "degraded").write_text("1\n")  # layer 1
    drives = e.drives.drives  # type: ignore[attr-defined]
    drives["/dev/sde"] = (8 * TB, "wwn:0xe")
    upgrade = e.plan_replace("/dev/sda", "/dev/sde")
    with pytest.raises(Degraded, match="layer 1"):
        e.execute(upgrade.id, upgrade.digest, opts(upgrade))
    del drives["/dev/sdb"]
    rebuild = e.plan_rebuild("/dev/sdb", "/dev/sde")
    (sysfs / "md125" / "md" / "degraded").write_text("1\n")  # layer 2: not on sdb
    with pytest.raises(Degraded):
        e.execute(rebuild.id, rebuild.digest, opts(rebuild))
    (sysfs / "md125" / "md" / "degraded").write_text("0\n")
    assert e.execute(rebuild.id, rebuild.digest, opts(rebuild)).job_id == "1"


def test_gate_pauses_for_battery_and_resume_finishes(tmp_path: Path) -> None:
    battery = {"on": False}
    runner = FakeRunner(cut=lambda i, a: (i == 2 and battery.update(on=True)) or False)
    e = run_engine(tmp_path, runner=runner, on_battery=lambda: battery["on"])
    plan = e.plan_create(["/dev/sda", "/dev/sdb"])
    report = e.execute(plan.id, plan.digest, opts(plan)).run()
    assert report.state == "paused" and "battery" in report.message
    e.job_finished("1", report)
    assert e.state == "paused" and e.active_job == "1"
    assert e.job_status("1").state == "paused"
    with pytest.raises(Busy):
        e.plan_create(["/dev/sdc"])
    battery["on"] = True
    with pytest.raises(OnBattery):
        e.resume_job("1", {})
    battery["on"] = False
    runner.cut = None
    assert e.authorize_resume("1", {}) == "com.borealislogistics.tamarack.resume"
    report = e.resume_job("1", {}).run()
    assert report.state == "completed"
    e.job_finished("1", report)
    assert e.layout is not None


def test_cancel_running_job_stops_at_next_step(tmp_path: Path) -> None:
    holder: dict[str, Any] = {}
    runner = FakeRunner(cut=lambda i, a: i == 0 and holder["e"].cancel_job("1") is not None)
    e = run_engine(tmp_path, runner=runner)
    holder["e"] = e
    plan = e.plan_create(["/dev/sda", "/dev/sdb"])
    report = e.execute(plan.id, plan.digest, opts(plan)).run()
    assert report.state == "paused" and report.message == "cancel requested"
    final = e.job_finished("1", report)
    assert final is not None and final.state == "cancelled"
    assert e.layout is None and e.state == "idle"


def test_cancel_inside_a_step_is_refused(tmp_path: Path) -> None:
    from fake_runner import PowerCut

    e = run_engine(tmp_path, runner=FakeRunner(cut=lambda i, a: i == 1))
    plan = e.plan_create(["/dev/sda"])
    run = e.execute(plan.id, plan.digest, opts(plan))
    with pytest.raises(PowerCut):
        run.run()
    e.job_finished("1", None)
    with pytest.raises(NotBetweenSteps):
        e.cancel_job("1")
    assert e.authorize_cancel("1") == "com.borealislogistics.tamarack.cancel"


def test_reattach_only_picks_up_an_interrupted_wait(tmp_path: Path) -> None:
    from fake_runner import PowerCut

    e = run_engine(tmp_path, ["/dev/sda"])
    e.confirm_backup()
    plan = e.plan_add("/dev/sdb")
    commands = [tuple(c["argv"]) for s in plan.document["steps"] for c in s["commands"]]
    wait = next(i for i, a in enumerate(commands) if a[:2] == ("mdadm", "--wait"))
    e.runner = FakeRunner(cut=lambda i, a: i == wait)
    with pytest.raises(PowerCut):
        e.execute(plan.id, plan.digest, opts(plan)).run()
    e.job_finished("1", None)
    assert load(str(Path(e.state_dir) / "jobs" / "1.jsonl")).in_doubt
    e.runner = FakeRunner()
    (run,) = e.reattach_runs()
    assert e.job_status("1").state == "waiting"
    report = run.run()
    assert report.state == "paused" and e.runner.calls == [commands[wait]]  # type: ignore[attr-defined]
    e.job_finished("1", report)
    assert e.reattach_runs() == []


def test_a_retired_drive_may_be_pulled(tmp_path: Path) -> None:
    e = run_engine(tmp_path, ["/dev/sda", "/dev/sdb", "/dev/sdc", "/dev/sdd"])
    e.confirm_backup()
    e.drives.drives["/dev/sde"] = (8 * TB, "wwn:0xe")  # type: ignore[attr-defined]
    plan = e.plan_replace("/dev/sda", "/dev/sde")
    titles = [s["title"] for s in plan.document["steps"]]
    retire = titles.index("Retire the old drive")
    after_retire = sum(len(s["commands"]) for s in plan.document["steps"][: retire + 1])
    drives = e.drives.drives  # type: ignore[attr-defined]
    e.runner = FakeRunner(
        cut=lambda i, a: i == after_retire - 1 and drives.pop("/dev/sda") and False
    )
    report = e.execute(plan.id, plan.digest, opts(plan)).run()
    assert report.state == "completed", report.message


def test_a_missing_drive_pauses_the_job(tmp_path: Path) -> None:
    e = run_engine(tmp_path)
    plan = e.plan_create(["/dev/sda", "/dev/sdb"])
    drives = e.drives.drives  # type: ignore[attr-defined]
    e.runner = FakeRunner(cut=lambda i, a: i == 0 and drives.pop("/dev/sdb") and False)
    report = e.execute(plan.id, plan.digest, opts(plan)).run()
    assert report.state == "paused" and "/dev/sdb is missing" in report.message


def test_resume_options() -> None:
    assert parse_resume_options({}) is None
    assert parse_resume_options({"resolution": "done"}) is Resolution.DONE
    for bad in ({"resolution": "maybe"}, {"other": 1}):
        with pytest.raises(InvalidArgument):
            parse_resume_options(bad)


def test_jobs_need_to_exist(tmp_path: Path) -> None:
    e = run_engine(tmp_path)
    for attempt in (
        lambda: e.authorize_cancel("9"),
        lambda: e.authorize_resume("9", {}),
        lambda: e.cancel_job("../1"),
    ):
        with pytest.raises(EngineError):
            attempt()


# --- layout records on the drives --------------------------------------------


class FakeRecords:
    def __init__(self, records: dict[str, str | None]) -> None:
        self.records = records  # partition -> layout JSON, or None if unreadable

    def partitions(self) -> list[str]:
        return sorted(self.records)

    def read(self, device: str) -> str:
        text = self.records[device]
        if text is None:
            raise ValueError("the layout record is damaged (checksum mismatch)")
        return text


def two_drive_layout() -> Any:
    plan = make_plan([4 * TB, 4 * TB], ["/dev/sda", "/dev/sdb"], ["wwn:0x1", "wwn:0x2"])
    return layout_from_plan(plan)


def records_engine(tmp_path: Path, records: dict[str, str | None], file: Any = None) -> Engine:
    if file is not None:
        (tmp_path / "layout.json").write_text(file.to_json())
    return Engine(str(tmp_path), FakeDrives(dict(DRIVES)), clock=Clock(), meta=FakeRecords(records))


def test_layout_is_taken_from_the_drives_when_the_state_file_is_missing(tmp_path: Path) -> None:
    layout = two_drive_layout()
    e = records_engine(tmp_path, {"/dev/sda1": layout.to_json(), "/dev/sdb1": layout.to_json()})
    assert e.layout == layout
    assert Layout.from_json((tmp_path / "layout.json").read_text()) == layout
    with pytest.raises(LayoutExists):
        e.plan_create(["/dev/sdc"])  # never offered over an existing pool


def test_one_damaged_record_does_not_hide_the_others(tmp_path: Path) -> None:
    layout = two_drive_layout()
    e = records_engine(tmp_path, {"/dev/sda1": layout.to_json(), "/dev/sdb1": None})
    assert e.layout == layout


def test_drives_that_disagree_block_planning(tmp_path: Path) -> None:
    layout = two_drive_layout()
    other = layout_from_plan(make_plan([8 * TB, 8 * TB], ["/dev/sdc", "/dev/sdd"]))
    e = records_engine(tmp_path, {"/dev/sda1": layout.to_json(), "/dev/sdc1": other.to_json()})
    with pytest.raises(LayoutUnreadable, match="disagree.*/dev/sda1.*/dev/sdc1"):
        e.plan_create(["/dev/sdb"])


def test_unreadable_records_block_planning(tmp_path: Path) -> None:
    e = records_engine(tmp_path, {"/dev/sda1": None})
    with pytest.raises(LayoutUnreadable, match="none can be read"):
        e.plan_create(["/dev/sdb"])


def test_state_file_must_match_the_drives(tmp_path: Path) -> None:
    layout = two_drive_layout()
    other = layout_from_plan(make_plan([8 * TB, 8 * TB], ["/dev/sda", "/dev/sdb"]))
    e = records_engine(tmp_path, {"/dev/sda1": other.to_json()}, file=layout)
    with pytest.raises(LayoutUnreadable, match="differs"):
        e.plan_add("/dev/sdc")
    assert e.layout is None


def test_matching_state_file_and_drives(tmp_path: Path) -> None:
    layout = two_drive_layout()
    e = records_engine(tmp_path, {"/dev/sda1": layout.to_json()}, file=layout)
    assert e.layout == layout


def test_no_tk_meta_partitions_means_a_fresh_box(tmp_path: Path) -> None:
    e = records_engine(tmp_path, {})
    assert e.layout is None
    e.plan_create(["/dev/sda"])


def test_unfinished_job_skips_the_cross_check(tmp_path: Path) -> None:
    (tmp_path / "jobs").mkdir()
    (tmp_path / "jobs" / "1.jsonl").write_text("garbage\n")
    layout = two_drive_layout()
    other = layout_from_plan(make_plan([8 * TB, 8 * TB], ["/dev/sda", "/dev/sdb"]))
    e = records_engine(tmp_path, {"/dev/sda1": other.to_json()}, file=layout)
    assert e.layout == layout  # records may be mid-update; planning is Busy anyway
    with pytest.raises(Busy):
        e.plan_add("/dev/sdc")


def test_udisks_finds_tk_meta_partitions() -> None:
    from tamarack.udisks import find_meta_partitions

    objects = udisks()
    objects["/org/freedesktop/UDisks2/block_devices/sda1"][P]["Name"] = "tk-meta"
    assert find_meta_partitions(objects) == ["/dev/sda1"]


# --- health ------------------------------------------------------------------


def test_health_without_a_pool(tmp_path: Path) -> None:
    assert engine(tmp_path).health() == {"status": "none", "degraded_layers": [], "drives": []}


def test_health_of_a_good_pool_then_a_failing_and_a_missing_drive(tmp_path: Path) -> None:
    e = run_engine(tmp_path, ["/dev/sda", "/dev/sdb", "/dev/sdc"])
    health = e.health()
    assert health["status"] == "ok"
    assert [d["state"] for d in health["drives"]] == ["ok", "ok", "ok"]
    fake = e.drives  # type: ignore[assignment]
    fake.smart["/dev/sdb"] = ("failing", "12 reallocated or pending bad sectors")  # type: ignore[attr-defined]
    del fake.drives["/dev/sdc"]  # type: ignore[attr-defined]
    health = e.health()
    assert health["status"] == "warning"
    assert health["drives"][1] == {
        "device": "/dev/sdb",
        "state": "failing",
        "detail": "12 reallocated or pending bad sectors",
    }
    assert health["drives"][2]["state"] == "missing"


def test_health_reports_degraded_layers_first(tmp_path: Path) -> None:
    e = run_engine(tmp_path, ["/dev/sda", "/dev/sdb", "/dev/sdc", "/dev/sdd"])
    (Path(e.root) / "sys" / "block" / "md126" / "md" / "degraded").write_text("1\n")
    health = e.health()
    assert health["status"] == "degraded" and health["degraded_layers"] == [1]


def test_udisks_smart_health() -> None:
    from tamarack.udisks import ATA, NVME, find_health

    objects = udisks()
    drive = objects["/org/freedesktop/UDisks2/drives/WDC_1"]
    assert find_health(objects, ["/dev/sda", "/dev/sdz"]) == {
        "/dev/sda": ("unknown", "no SMART data"),
        "/dev/sdz": ("missing", "not attached"),
    }
    drive[ATA] = {"SmartSupported": True, "SmartEnabled": True, "SmartFailing": False}
    assert find_health(objects, ["/dev/sda"])["/dev/sda"] == ("ok", "")
    drive[ATA]["SmartNumBadSectors"] = 3
    assert find_health(objects, ["/dev/sda"])["/dev/sda"][0] == "failing"
    drive[ATA]["SmartFailing"] = True
    assert find_health(objects, ["/dev/sda"])["/dev/sda"] == (
        "failing",
        "SMART says the drive is failing",
    )
    del drive[ATA]
    drive[NVME] = {"SmartCriticalWarning": ["spare", "temperature"]}
    assert find_health(objects, ["/dev/sda"])["/dev/sda"] == ("failing", "spare, temperature")
    drive[NVME] = {"SmartCriticalWarning": []}
    assert find_health(objects, ["/dev/sda"])["/dev/sda"] == ("ok", "")
