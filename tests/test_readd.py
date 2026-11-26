# SPDX-License-Identifier: GPL-3.0-or-later
"""A pool drive that comes back is re-added, erasing nothing."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fake_runner import FakeRunner
from test_engine import DRIVES, TB, run_engine

from tamarack.journal import load
from tamarack.layout import layout_from_plan
from tamarack.model import PlanKind
from tamarack.planner import make_plan
from tamarack.readd import plan_readd
from tamarack.review import document


def layout3() -> Any:
    plan = make_plan(
        [4 * TB] * 3, ["/dev/sda", "/dev/sdb", "/dev/sdc"], ["wwn:1", "wwn:2", "wwn:3"]
    )
    return layout_from_plan(plan)


def commands(change: Any) -> list[str]:
    return [c.shell() for step in change.steps for c in step.commands]


def test_readd_opens_and_adds_without_formatting_or_partitioning() -> None:
    change = plan_readd(layout3(), 1, "/dev/sdb", [(2, 1)])
    assert change.kind is PlanKind.READD and change.erases == ""
    shell = commands(change)
    assert "integritysetup open --integrity crc32c /dev/sdb2 tk-d2-p2" in shell
    assert "mdadm /dev/md/tk-l1 --add /dev/mapper/tk-d2-p2" in shell
    assert not any(
        c.startswith(("parted", "wipefs", "integritysetup format", "mkfs")) for c in shell
    )
    assert not any("tamarack-meta" in c for c in shell)  # same path: the records still hold
    doc = document(change)
    assert doc["erases"] == [] and {d["role"] for d in doc["drives"]} == {"keep"}


def test_a_drive_back_under_another_path_moves_the_layout_with_it() -> None:
    change = plan_readd(layout3(), 1, "/dev/sdd", [(2, 1)])
    assert change.after.drives[1].device == "/dev/sdd"
    assert change.after.drives[1].identity == "wwn:2"
    assert any("tamarack-meta write" in c for c in commands(change))
    assert "integritysetup open --integrity crc32c /dev/sdd2 tk-d2-p2" in commands(change)


def test_nothing_to_readd_is_refused() -> None:
    with pytest.raises(ValueError):
        plan_readd(layout3(), 1, "/dev/sdb", [])


# --- noticing the drive is back ---------------------------------------------------


def degraded_without(root: Path, layer: int, members: list[str]) -> None:
    """Layer ``layer`` degraded, md holding only the dm mappings in ``members``."""
    md = f"md{127 - layer}"
    base = root / "sys" / "block" / md / "md"
    (base / "degraded").write_text("1\n")
    for i, name in enumerate(members):
        dm = f"dm-{layer * 10 + i}"
        (root / "sys" / "block" / dm / "dm").mkdir(parents=True)
        (root / "sys" / "block" / dm / "dm" / "name").write_text(name + "\n")
        (base / f"dev-{dm}").mkdir()
        (base / f"dev-{dm}" / "block").symlink_to(root / "sys" / "block" / dm)


def classify_by_identity(e: Any, where: dict[str, str]) -> None:
    """The fake drives report these identities at these paths."""
    fake = e.drives
    fake.verdicts = {
        device: {"device": device, "verdict": "refuse", "identity": identity}
        for device, identity in where.items()
    }
    fake.drives = {d: (4 * TB, i) for d, i in where.items()}


def test_a_returning_drive_is_found_by_identity_and_readded(tmp_path: Path) -> None:
    e = run_engine(tmp_path, ["/dev/sda", "/dev/sdb", "/dev/sdc"])
    root = Path(e.root)
    degraded_without(root, 1, ["tk-d1-p2", "tk-d3-p2"])
    ids = {d: DRIVES[d][1] for d in ("/dev/sda", "/dev/sdb", "/dev/sdc")}
    # /dev/sdb came back as /dev/sde.
    classify_by_identity(
        e, {"/dev/sda": ids["/dev/sda"], "/dev/sde": ids["/dev/sdb"], "/dev/sdc": ids["/dev/sdc"]}
    )
    assert e.returning_drive() == (1, "/dev/sde", [(2, 1)])
    run = e.readd_returning()
    assert run is not None
    report = run.run()
    assert report.state == "completed"
    e.job_finished(run.job_id, report)
    plan = load(str(tmp_path / "state" / "jobs" / f"{run.job_id}.jsonl")).plan
    assert plan["kind"] == "readd" and plan["erases"] == []
    assert e.layout is not None and e.layout.drives[1].device == "/dev/sde"
    assert ("mdadm", "/dev/md/tk-l1", "--add", "/dev/mapper/tk-d2-p2") in e.runner.calls


def test_nothing_is_readded_while_the_drive_is_still_away(tmp_path: Path) -> None:
    e = run_engine(tmp_path, ["/dev/sda", "/dev/sdb", "/dev/sdc"])
    degraded_without(Path(e.root), 1, ["tk-d1-p2", "tk-d3-p2"])
    ids = {d: DRIVES[d][1] for d in ("/dev/sda", "/dev/sdc")}
    classify_by_identity(e, ids)
    assert e.returning_drive() is None and e.readd_returning() is None


def test_nothing_is_readded_without_execution_or_during_a_job(tmp_path: Path) -> None:
    e = run_engine(tmp_path, ["/dev/sda", "/dev/sdb", "/dev/sdc"], runner=None)
    degraded_without(Path(e.root), 1, ["tk-d1-p2", "tk-d3-p2"])
    classify_by_identity(e, {d: DRIVES[d][1] for d in ("/dev/sda", "/dev/sdb", "/dev/sdc")})
    assert e.returning_drive() is not None
    assert e.readd_returning() is None  # execution is off
    e.runner = FakeRunner()
    e._running = "9"
    assert e.readd_returning() is None  # another job is running
