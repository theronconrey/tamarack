# SPDX-License-Identifier: GPL-3.0-or-later
"""The step journal and resume logic, against a fake runner."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from fake_runner import FakeRunner, PowerCut

from tamarack.grow import plan_add_drive
from tamarack.job import Resolution, Result, cancel, resume, start, validate_document
from tamarack.journal import JournalError, JournalWriter, journal_path, load, read_records
from tamarack.layout import Layout, layout_from_plan
from tamarack.ops import cmd
from tamarack.planner import make_plan
from tamarack.recovery import PROBE_PROGRAMS, Resume, rule_for
from tamarack.replace import plan_replace
from tamarack.review import document

TB = 10**12


def clock() -> str:
    return "2026-11-22T12:00:00Z"


def layout(*sizes: int) -> Layout:
    devices = [f"/dev/sd{'abcdefgh'[i]}" for i in range(len(sizes))]
    ids = [f"wwn:0x500{i}" for i in range(len(sizes))]
    return layout_from_plan(make_plan([s * TB for s in sizes], devices, ids))


PLANS: dict[str, Any] = {
    "create": lambda: document(
        make_plan([4 * TB, 4 * TB, 8 * TB], ["/dev/sda", "/dev/sdb", "/dev/sdc"])
    ),
    "add-1-2": lambda: document(plan_add_drive(layout(4), 4 * TB, "/dev/sdb", "wwn:0x9")),
    "add-3-4": lambda: document(plan_add_drive(layout(4, 4, 8), 8 * TB, "/dev/sdd", "wwn:0x9")),
    "replace": lambda: document(
        plan_replace(layout(4, 4, 4), "/dev/sdb", 8 * TB, "/dev/sdd", new_identity="wwn:0x9")
    ),
    "rebuild": lambda: document(
        plan_replace(layout(4, 4, 4), "/dev/sdb", 4 * TB, "/dev/sdb", True, "wwn:0x9")
    ),
}


def commands(doc: dict[str, Any]) -> list[tuple[str, ...]]:
    return [tuple(c["argv"]) for step in doc["steps"] for c in step["commands"]]


def run_fresh(tmp_path: Path, doc: dict[str, Any], runner: FakeRunner, **kw: Any) -> Any:
    return start(doc, "1", str(tmp_path / "1.jsonl"), runner, now=clock, **kw)


# --- whole runs --------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(PLANS))
def test_a_job_runs_every_command_once_in_order(tmp_path: Path, name: str) -> None:
    doc = PLANS[name]()
    runner = FakeRunner()
    report = run_fresh(tmp_path, doc, runner)
    assert report.state == "completed"
    assert runner.calls == commands(doc)
    state = load(str(tmp_path / "1.jsonl"))
    assert state.result == "completed" and state.finished_all_steps


def test_journal_is_private_and_line_per_record(tmp_path: Path) -> None:
    run_fresh(tmp_path, PLANS["add-1-2"](), FakeRunner())
    path = tmp_path / "1.jsonl"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    lines = path.read_bytes().split(b"\n")
    assert lines[-1] == b""
    assert [json.loads(line)["t"] for line in lines[:2]] == ["job", "step-start"]
    assert json.loads(lines[-2])["t"] == "job-end"


def test_an_existing_journal_is_never_overwritten(tmp_path: Path) -> None:
    (tmp_path / "1.jsonl").write_text("")
    with pytest.raises(FileExistsError):
        run_fresh(tmp_path, PLANS["create"](), FakeRunner())


def test_a_failed_command_ends_the_job(tmp_path: Path) -> None:
    doc = PLANS["add-3-4"]()
    bad = commands(doc)[3]
    runner = FakeRunner({bad: Result(5, "mdadm: device busy\n")})
    report = run_fresh(tmp_path, doc, runner)
    assert report.state == "failed"
    assert "exited 5" in report.message and "device busy" in report.message
    assert runner.calls == commands(doc)[:4]
    with pytest.raises(JournalError, match="already ended"):
        resume(str(tmp_path / "1.jsonl"), FakeRunner(), authorized=True)


def test_a_tolerated_failure_does_not_stop_the_job(tmp_path: Path) -> None:
    doc = PLANS["rebuild"]()
    closes = [a for a in commands(doc) if a[:2] == ("integritysetup", "close")]
    assert closes
    report = run_fresh(tmp_path, doc, FakeRunner({closes[0]: Result(4, "no such device")}))
    assert report.state == "completed"


def test_a_missing_program_fails_the_job(tmp_path: Path) -> None:
    class Missing(FakeRunner):
        def run(self, argv: tuple[str, ...]) -> Result:
            raise FileNotFoundError(2, "No such file or directory", argv[0])

    report = run_fresh(tmp_path, PLANS["create"](), Missing())
    assert report.state == "failed" and "exited 127" in report.message


# --- pausing between steps ---------------------------------------------------


def test_gate_pauses_before_a_step_and_resume_continues(tmp_path: Path) -> None:
    doc = PLANS["add-3-4"]()
    path = str(tmp_path / "1.jsonl")
    first = FakeRunner()
    report = run_fresh(tmp_path, doc, first, gate=lambda n: "UPS on battery" if n == 3 else None)
    assert (report.state, report.step, report.message) == ("paused", 3, "UPS on battery")
    done = len(doc["steps"][0]["commands"]) + len(doc["steps"][1]["commands"])
    assert first.calls == commands(doc)[:done]

    idle = FakeRunner()
    assert resume(path, idle, authorized=False, now=clock).state == "paused"
    assert idle.calls == []

    second = FakeRunner()
    assert resume(path, second, authorized=True, now=clock).state == "completed"
    assert first.calls + second.calls == commands(doc)


def test_cancel_between_steps(tmp_path: Path) -> None:
    path = str(tmp_path / "1.jsonl")
    run_fresh(tmp_path, PLANS["add-3-4"](), FakeRunner(), gate=lambda n: "stop" if n == 2 else None)
    assert cancel(path, now=clock).state == "cancelled"
    assert load(path).result == "cancelled"


def test_cancel_is_refused_inside_a_step(tmp_path: Path) -> None:
    path = str(tmp_path / "1.jsonl")
    with pytest.raises(PowerCut):
        run_fresh(tmp_path, PLANS["add-3-4"](), FakeRunner(cut=lambda i, a: i == 2))
    with pytest.raises(JournalError, match="between steps"):
        cancel(path, now=clock)


# --- power cuts --------------------------------------------------------------


def cases() -> list[tuple[str, int]]:
    return [(name, i) for name in sorted(PLANS) for i in range(len(commands(PLANS[name]())))]


@pytest.mark.parametrize(("name", "cut_at"), cases())
def test_power_cut_at_every_command(tmp_path: Path, name: str, cut_at: int) -> None:
    """Cut power during each command in turn, then resume as an admin would."""
    doc = PLANS[name]()
    planned = commands(doc)
    path = str(tmp_path / "1.jsonl")
    with pytest.raises(PowerCut):
        run_fresh(tmp_path, doc, FakeRunner(cut=lambda i, a: i == cut_at))

    state = load(path)
    assert state.in_doubt
    flat = [c for step in doc["steps"] for c in step["commands"]]
    cut = cmd(*planned[cut_at], tolerate_failure=flat[cut_at]["tolerate_failure"])
    rule = rule_for(cut)
    # Probes report "not done": the command must run again.
    after = FakeRunner({rule.probe: Result(1)} if rule.probe else {})
    report = resume(path, after, authorized=True, now=clock)

    if rule.resume is Resume.ASK:
        assert report.state == "paused" and cut.shell() in report.message
        assert after.calls == []
        report = resume(path, after, authorized=True, resolution=Resolution.RERUN, now=clock)
    assert report.state == "completed"
    expected = planned[cut_at:]
    if rule.resume is Resume.PROBE:
        expected = [rule.probe, *expected]
    assert after.calls == expected
    assert load(path).result == "completed"


def test_a_probe_that_finds_the_work_done_skips_the_command(tmp_path: Path) -> None:
    doc = PLANS["create"]()
    planned = commands(doc)
    index = next(i for i, a in enumerate(planned) if a[:2] == ("mdadm", "--create"))
    path = str(tmp_path / "1.jsonl")
    with pytest.raises(PowerCut):
        run_fresh(tmp_path, doc, FakeRunner(cut=lambda i, a: i == index))
    after = FakeRunner()  # every probe exits 0
    assert resume(path, after, authorized=True, now=clock).state == "completed"
    assert after.calls == [("mdadm", "--detail", planned[index][2]), *planned[index + 1 :]]


def test_resolution_done_skips_the_command(tmp_path: Path) -> None:
    doc = PLANS["replace"]()
    planned = commands(doc)
    index = next(i for i, a in enumerate(planned) if "--replace" in a)
    path = str(tmp_path / "1.jsonl")
    with pytest.raises(PowerCut):
        run_fresh(tmp_path, doc, FakeRunner(cut=lambda i, a: i == index))
    after = FakeRunner()
    report = resume(path, after, authorized=True, resolution=Resolution.DONE, now=clock)
    assert report.state == "completed"
    assert after.calls == planned[index + 1 :]


def test_wait_is_reattached_without_authorization(tmp_path: Path) -> None:
    doc = PLANS["add-1-2"]()
    planned = commands(doc)
    index = planned.index(next(a for a in planned if a[:2] == ("mdadm", "--wait")))
    path = str(tmp_path / "1.jsonl")
    with pytest.raises(PowerCut):
        run_fresh(tmp_path, doc, FakeRunner(cut=lambda i, a: i == index))

    boot = FakeRunner()
    report = resume(path, boot, authorized=False, now=clock)
    assert report.state == "paused" and "waiting for Resume" in report.message
    assert boot.calls == [planned[index]]

    admin = FakeRunner()
    assert resume(path, admin, authorized=True, now=clock).state == "completed"
    assert admin.calls == planned[index + 1 :]


def test_other_cut_commands_wait_for_authorization(tmp_path: Path) -> None:
    path = str(tmp_path / "1.jsonl")
    with pytest.raises(PowerCut):
        run_fresh(tmp_path, PLANS["create"](), FakeRunner(cut=lambda i, a: i == 1))
    boot = FakeRunner()
    assert resume(path, boot, authorized=False, now=clock).state == "paused"
    assert boot.calls == []


def test_cut_between_commands_continues_with_the_next(tmp_path: Path) -> None:
    doc = PLANS["create"]()
    path = str(tmp_path / "1.jsonl")
    with pytest.raises(PowerCut):
        run_fresh(tmp_path, doc, FakeRunner(cut=lambda i, a: i == 3))
    # Pretend command 3 finished and its cmd-end reached the disk.
    with JournalWriter.reopen(path) as w:
        w.append({"t": "cmd-end", "step": 1, "cmd": 3, "exit": 0, "ok": True, "stderr_tail": ""})
    after = FakeRunner()
    assert resume(path, after, authorized=True, now=clock).state == "completed"
    assert after.calls == commands(doc)[4:]


def test_torn_last_line_is_ignored_and_cut_off(tmp_path: Path) -> None:
    doc = PLANS["add-3-4"]()
    path = str(tmp_path / "1.jsonl")
    run_fresh(tmp_path, doc, FakeRunner(), gate=lambda n: "stop" if n == 2 else None)
    whole = Path(path).read_bytes()
    with open(path, "ab") as f:
        f.write(b'{"t":"step-st')
    assert load(path).step == 2
    assert resume(path, FakeRunner(), authorized=True, now=clock).state == "completed"
    assert Path(path).read_bytes().startswith(whole)
    assert all(r["t"] for r in read_records(path))


# --- refusing bad journals ---------------------------------------------------


def rewrite(path: str, change: Any) -> None:
    records = read_records(path)
    change(records)
    with open(path, "w") as f:
        f.writelines(json.dumps(r) + "\n" for r in records)


def paused_job(tmp_path: Path) -> str:
    run_fresh(tmp_path, PLANS["add-3-4"](), FakeRunner(), gate=lambda n: "stop" if n == 3 else None)
    return str(tmp_path / "1.jsonl")


def test_a_changed_plan_is_refused(tmp_path: Path) -> None:
    path = paused_job(tmp_path)

    def change(records: list[dict[str, Any]]) -> None:
        records[0]["plan"]["steps"][-2]["commands"][0]["argv"][-1] = "/dev/sdz"

    rewrite(path, change)
    with pytest.raises(JournalError, match="digest"):
        resume(path, FakeRunner(), authorized=True)


@pytest.mark.parametrize(
    ("index", "record", "message"),
    [
        (1, {"t": "step-start", "step": 2}, "out of order"),
        (2, {"t": "cmd-start", "step": 1, "cmd": 1, "argv": []}, "expected step 1 command 0"),
        (2, {"t": "cmd-start", "step": 1, "cmd": 0, "argv": ["rm"]}, "differs from the plan"),
        (2, {"t": "cmd-end", "step": 1, "cmd": 0, "exit": 0, "ok": True}, "not started"),
        (2, {"t": "job-end", "result": "completed"}, "steps left"),
        (2, {"t": "surprise"}, "unknown record"),
    ],
)
def test_out_of_order_journals_are_refused(
    tmp_path: Path, index: int, record: dict[str, Any], message: str
) -> None:
    path = paused_job(tmp_path)
    rewrite(path, lambda records: records.insert(index, record))
    with pytest.raises(JournalError, match=message):
        load(path)


def test_garbage_in_the_middle_is_refused(tmp_path: Path) -> None:
    path = paused_job(tmp_path)
    lines = Path(path).read_bytes().split(b"\n")
    lines[2] = b"not json"
    Path(path).write_bytes(b"\n".join(lines))
    with pytest.raises(JournalError, match="line 3"):
        load(path)


def test_documents_with_foreign_commands_or_rules_are_refused() -> None:
    doc = PLANS["create"]()
    doc["steps"][0]["commands"][0]["argv"] = ["sh", "-c", "true"]
    with pytest.raises(ValueError, match="not a program"):
        validate_document(doc)
    doc = PLANS["create"]()
    doc["steps"][0]["commands"][0]["resume"] = "ask"
    with pytest.raises(JournalError, match="resume rule"):
        validate_document(doc)


@pytest.mark.parametrize("job_id", ["", "../x", "1/2", "७"])
def test_journal_path_takes_plain_numbers(job_id: str) -> None:
    with pytest.raises(ValueError):
        journal_path(job_id)


def test_journal_path() -> None:
    assert journal_path("7") == "/var/lib/tamarack/jobs/7.jsonl"
    assert os.path.basename(journal_path("12", "/tmp")) == "12.jsonl"


# --- resume rules ------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "resume_rule", "probe"),
    [
        (("sgdisk", "--zap-all", "/dev/sda"), "rerun", ()),
        (("sgdisk", "-n", "2:0:+10M", "-t", "2:fd00", "-c", "2:tk-l1", "/dev/sda"), "ask", ()),
        (("sgdisk", "-c", "2:tk-l1", "-c", "3:tk-l2", "/dev/sda"), "rerun", ()),
        (
            ("integritysetup", "open", "--integrity", "crc32c", "/dev/sda2", "tk-d1-p2"),
            "probe",
            ("dmsetup", "info", "tk-d1-p2"),
        ),
        (("integritysetup", "close", "tk-d1-p2"), "ask", ()),
        (
            ("mdadm", "--create", "/dev/md/tk-l1", "--run"),
            "probe",
            ("mdadm", "--detail", "/dev/md/tk-l1"),
        ),
        (("mdadm", "/dev/md/tk-l1", "--add", "/dev/mapper/x"), "ask", ()),
        (("mdadm", "--grow", "/dev/md/tk-l1", "--level=5"), "ask", ()),
        (("mdadm", "--wait", "/dev/md/tk-l1"), "rerun", ()),
        (("vgextend", "tamarack", "/dev/md/tk-l2"), "ask", ()),
        (("lvextend", "--extents", "+100%FREE", "tamarack/data"), "ask", ()),
        (
            ("lvcreate", "--yes", "--extents", "100%FREE", "--name", "data", "tamarack"),
            "probe",
            ("lvs", "tamarack/data"),
        ),
        (
            ("mount", "-o", "noatime", "/dev/tamarack/data", "/srv/tamarack"),
            "probe",
            ("findmnt", "--mountpoint", "/srv/tamarack"),
        ),
    ],
)
def test_resume_rules(argv: tuple[str, ...], resume_rule: str, probe: tuple[str, ...]) -> None:
    rule = rule_for(cmd(*argv))
    assert (rule.resume.value, rule.probe) == (resume_rule, probe)


def test_tolerated_commands_rerun() -> None:
    assert (
        rule_for(cmd("integritysetup", "close", "x", tolerate_failure=True)).resume is Resume.RERUN
    )


def test_every_probe_is_a_read_only_program() -> None:
    read_only = {
        ("blkid", "--match-token"),
        ("btrfs", "subvolume", "show"),
        ("dmsetup", "info"),
        ("findmnt", "--mountpoint"),
        ("lvs",),
        ("mdadm", "--detail"),
        ("pvs",),
        ("vgs",),
    }
    for doc in (make() for make in PLANS.values()):
        for step in doc["steps"]:
            for c in step["commands"]:
                if "probe" in c:
                    probe = tuple(c["probe"])
                    assert probe[0] in PROBE_PROGRAMS
                    assert any(probe[: len(p)] == p for p in read_only), probe
