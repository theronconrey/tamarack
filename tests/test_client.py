# SPDX-License-Identifier: GPL-3.0-or-later
"""``tamarack engine …`` against the engine on a private bus (fake runner)."""

from __future__ import annotations

import argparse
import io
from collections.abc import Iterator
from pathlib import Path

import pytest
from fake_runner import FakeRunner
from test_service import Harness  # skips this module without PyGObject or dbus-daemon

from tamarack import client
from tamarack.cli import build_parser


@pytest.fixture
def h(bus: str, tmp_path: Path) -> Iterator[Harness]:
    state = tmp_path / "state"
    state.mkdir()
    harness = Harness(bus, state, runner=FakeRunner())
    yield harness
    harness.stop()


def run(h: Harness, argv: list[str], typed: str = "") -> tuple[int, str]:
    args = build_parser().parse_args(["engine", *argv])
    out = io.StringIO()
    asked: list[str] = []

    def ask(prompt: str) -> str:
        asked.append(prompt)
        return typed

    code = client.run(args, client.Engine(h.client), ask=ask, out=out)
    return code, out.getvalue()


def test_status_without_a_pool(h: Harness) -> None:
    code, text = run(h, ["status"])
    assert code == 0
    assert "Engine 0.1.0: idle" in text and "Pool: none yet" in text
    assert "Backup last confirmed: never" in text


def test_dry_run_shows_the_plan_and_discards_it(h: Harness) -> None:
    code, text = run(h, ["create", "/dev/sda", "/dev/sdb", "--dry-run", "--commands"])
    assert code == 0
    assert "1. Partition each drive" in text and "sgdisk --zap-all /dev/sda" in text
    assert "Plan digest: sha256:" in text and "discarded" in text
    assert h.service.engine.plan_ids() == []
    assert h.service.engine.runner.calls == []  # type: ignore[union-attr]


def test_wrong_confirmation_changes_nothing(h: Harness) -> None:
    with pytest.raises(client.ClientError, match="not confirmed"):
        run(h, ["create", "/dev/sda", "/dev/sdb", "--no-ups"], typed="/dev/sda")
    assert h.service.engine.plan_ids() == []
    assert h.service.engine.runner.calls == []  # type: ignore[union-attr]


def test_create_runs_and_follows_the_job(h: Harness) -> None:
    code, text = run(h, ["create", "/dev/sda", "/dev/sdb", "--no-ups"], typed="/dev/sdb /dev/sda")
    assert code == 0, text
    assert "These drives will be ERASED:" in text and "Started job 1." in text
    assert "[completed]" in text
    code, text = run(h, ["status"])
    assert "Pool: 2 drives" in text and "survives 1 drive failure" in text


def test_engine_refusals_are_plain_messages(h: Harness) -> None:
    with pytest.raises(client.ClientError, match="NoUps"):
        run(h, ["create", "/dev/sda"], typed="/dev/sda")
    with pytest.raises(client.ClientError, match=r"^NoSuchDrive: /dev/sdq: no such block device$"):
        run(h, ["create", "/dev/sdq", "--dry-run"])
    with pytest.raises(client.ClientError, match=r"^NoLayout: nothing has been built yet"):
        run(h, ["add", "/dev/sdc", "--dry-run"])


def test_polkit_denial(h: Harness) -> None:
    h.fakes.denied.add("com.borealislogistics.tamarack.confirm-backup")
    with pytest.raises(client.ClientError, match="not authorized"):
        run(h, ["confirm-backup"])


def test_missing_engine_is_explained(bus: str) -> None:
    from test_service import connect

    args = argparse.Namespace(engine_command="status")
    with pytest.raises(client.ClientError, match="not installed or not running"):
        client.run(args, client.Engine(connect(bus)), out=io.StringIO())


def test_job_paths() -> None:
    assert client.job_path("3") == "/com/borealislogistics/Tamarack1/Jobs/3"
    assert client.job_path("/x/Jobs/3") == "/x/Jobs/3"


def test_snapshots_command(h: Harness) -> None:
    code, text = run(h, ["snapshots"])
    assert code == 0 and text == "Snapshots kept: 24 hourly, 7 daily, 4 weekly, 6 monthly\n"
    run(h, ["create", "/dev/sda", "/dev/sdb", "--no-ups"], typed="/dev/sda /dev/sdb")
    code, text = run(h, ["snapshots", "--keep", "12", "7", "0", "3"])
    assert text == "Snapshots kept: 12 hourly, 7 daily, 0 weekly, 3 monthly\n"
    code, text = run(h, ["snapshots", "--off"])
    assert text == "Snapshots: off\n"
