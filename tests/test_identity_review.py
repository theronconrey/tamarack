# SPDX-License-Identifier: GPL-3.0-or-later
"""Drive identities, the review document and its digest."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from tamarack.cli import main
from tamarack.grow import plan_add_drive
from tamarack.identity import (
    DriveChanged,
    ExpectedDrive,
    from_nvme_namespace,
    from_udisks,
    validate_identity,
    verify,
)
from tamarack.layout import Disk, Layout, layout_from_plan
from tamarack.model import PlanKind
from tamarack.ops import cmd
from tamarack.planner import make_plan
from tamarack.replace import plan_replace
from tamarack.review import canonical, digest, document, expected_drives

TB = 10**12


def layout(*sizes: int) -> Layout:
    n = len(sizes)
    devices = [f"/dev/sd{'abcdefgh'[i]}" for i in range(n)]
    ids = [f"wwn:0x500{i}" for i in range(n)]
    return layout_from_plan(make_plan([s * TB for s in sizes], devices, ids))


# --- identities --------------------------------------------------------------


@pytest.mark.parametrize(
    "good", ["wwn:0x5000c500a1b2c3d4", "serial:WD-WCC7K1234567", "serial:A/B+c"]
)
def test_valid_identities(good: str) -> None:
    assert validate_identity(good) == good


@pytest.mark.parametrize(
    "bad",
    ["", "0x5000", "wwn:", "uuid:abc", "serial:has space", "serial:a\nb", "serial:" + "x" * 129],
)
def test_invalid_identities(bad: str) -> None:
    with pytest.raises(ValueError):
        validate_identity(bad)


def test_from_udisks_prefers_wwn_and_falls_back_to_serial() -> None:
    assert from_udisks("0x5000C500A1B2C3D4", "ZL2ABC") == "wwn:0x5000c500a1b2c3d4"
    assert from_udisks("", " ZL2ABC ") == "serial:ZL2ABC"
    assert from_udisks("", "") == ""


def test_verify_accepts_drives_where_expected() -> None:
    verify([ExpectedDrive("/dev/sda", "wwn:0x1")], {"/dev/sda": "wwn:0x1", "/dev/sdb": "wwn:0x2"})


@pytest.mark.parametrize(
    ("expected", "current", "message"),
    [
        (ExpectedDrive("/dev/sda", "wwn:0x1"), {}, "missing"),
        (ExpectedDrive("/dev/sda", "wwn:0x1"), {"/dev/sda": "wwn:0x2"}, "is now wwn:0x2"),
        (ExpectedDrive("/dev/sda", ""), {"/dev/sda": "wwn:0x1"}, "no identity"),
    ],
)
def test_verify_refuses(expected: ExpectedDrive, current: dict[str, str], message: str) -> None:
    with pytest.raises(DriveChanged, match=message):
        verify([expected], current)


def test_one_drive_under_two_paths_is_refused() -> None:
    with pytest.raises(ValueError, match="listed twice"):
        make_plan([4 * TB, 4 * TB], ["/dev/sda", "/dev/sdb"], ["wwn:0x1", "wwn:0x1"])


def test_layout_json_keeps_identities_and_omits_empty_ones() -> None:
    with_ids = layout(4, 4)
    assert Layout.from_json(with_ids.to_json()) == with_ids
    assert with_ids.to_dict()["drives"][0]["id"] == "wwn:0x5000"
    without = layout_from_plan(make_plan([4 * TB, 4 * TB], ["/dev/sda", "/dev/sdb"]))
    assert "id" not in without.to_dict()["drives"][0]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("id", "wwn:0x5000", "listed twice"),
        ("id", "bogus", "invalid drive identity"),
        ("device", "/dev/sda", "device twice"),
    ],
)
def test_layout_file_with_bad_drives_is_refused(field: str, value: str, message: str) -> None:
    data = layout(4, 4).to_dict()
    data["drives"][1][field] = value
    with pytest.raises(ValueError, match=message):
        Layout.from_dict(data)


def test_new_drive_must_be_a_different_physical_drive() -> None:
    with pytest.raises(ValueError, match="already in the layout"):
        plan_add_drive(layout(4, 4), 4 * TB, "/dev/sdx", "wwn:0x5001")
    with pytest.raises(ValueError, match="same drive"):
        plan_replace(layout(4, 4, 4), "/dev/sdb", 4 * TB, "/dev/sdb", True, "wwn:0x5001")
    with pytest.raises(ValueError, match="invalid drive identity"):
        plan_add_drive(layout(4, 4), 4 * TB, "/dev/sdx", "nope")


# --- review document ---------------------------------------------------------


def roles(doc: dict[str, object]) -> list[tuple[str, str, str]]:
    return [(d["device"], d["id"], d["role"]) for d in doc["drives"]]  # type: ignore[attr-defined]


def test_create_document_erases_every_drive() -> None:
    plan = make_plan([4 * TB, 8 * TB], ["/dev/sda", "/dev/sdb"], ["wwn:0x1", "serial:B"])
    doc = document(plan)
    assert doc["kind"] == "create"
    assert doc["before"] is None
    assert doc["erases"] == ["/dev/sda", "/dev/sdb"]
    assert roles(doc) == [("/dev/sda", "wwn:0x1", "erase"), ("/dev/sdb", "serial:B", "erase")]
    assert doc["steps"][0]["commands"][0] == {
        "argv": ["parted", "--script", "/dev/sda", "mklabel", "gpt"],
        "tolerate_failure": False,
        "resume": "rerun",
    }


def test_add_document_roles() -> None:
    plan = plan_add_drive(layout(4, 4), 4 * TB, "/dev/sdc", "wwn:0x9")
    assert plan.kind is PlanKind.ADD
    doc = document(plan)
    assert roles(doc)[-1] == ("/dev/sdc", "wwn:0x9", "erase")
    assert {r for _, _, r in roles(doc)[:-1]} == {"keep"}
    assert doc["erases"] == ["/dev/sdc"]


def test_upgrade_needs_the_old_drive_until_it_is_retired() -> None:
    plan = plan_replace(layout(4, 4, 4), "/dev/sdb", 8 * TB, "/dev/sdd", new_identity="wwn:0x9")
    assert plan.kind is PlanKind.REPLACE
    doc = document(plan)
    assert ("/dev/sdb", "wwn:0x5001", "leave") in roles(doc)
    assert ("/dev/sdd", "wwn:0x9", "erase") in roles(doc)
    assert ExpectedDrive("/dev/sdb", "wwn:0x5001") in expected_drives(doc)


def test_rebuild_does_not_expect_the_failed_drive() -> None:
    plan = plan_replace(layout(4, 4, 4), "/dev/sdb", 4 * TB, "/dev/sdb", True, "wwn:0x9")
    assert plan.kind is PlanKind.REBUILD
    doc = document(plan)
    assert ("/dev/sdb", "wwn:0x5001", "failed") in roles(doc)
    expected = expected_drives(doc)
    assert ExpectedDrive("/dev/sdb", "wwn:0x9") in expected
    assert all(e.identity != "wwn:0x5001" for e in expected)
    # The old drive back in the slot is caught before anything runs.
    current = {e.device: e.identity for e in expected} | {"/dev/sdb": "wwn:0x5001"}
    with pytest.raises(DriveChanged):
        verify(expected, current)


def test_rebuild_without_identities_still_marks_the_slot() -> None:
    fresh = layout_from_plan(make_plan([4 * TB] * 3, ["/dev/sda", "/dev/sdb", "/dev/sdc"]))
    doc = document(plan_replace(fresh, "/dev/sdb", 4 * TB, "/dev/sdb", failed=True))
    assert [r for _, _, r in roles(doc)] == ["keep", "failed", "erase", "keep"]


# --- digest ------------------------------------------------------------------


def test_digest_is_stable_and_canonical() -> None:
    plan = plan_add_drive(layout(4, 4), 4 * TB, "/dev/sdc", "wwn:0x9")
    again = plan_add_drive(layout(4, 4), 4 * TB, "/dev/sdc", "wwn:0x9")
    assert digest(document(plan)) == digest(document(again))
    assert digest(document(plan)).startswith("sha256:")
    text = canonical(document(plan)).decode("ascii")
    doc = document(plan)
    assert text == json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    assert text.startswith('{"after":')


def test_canonical_form_is_ascii_whatever_the_paths() -> None:
    plan = make_plan([4 * TB], ["/dev/disk/by-id/ata-Disque_été"])
    canonical(document(plan)).decode("ascii")


def test_digest_changes_with_any_reviewed_detail() -> None:
    plan = plan_add_drive(layout(4, 4), 4 * TB, "/dev/sdc", "wwn:0x9")
    base = digest(document(plan))
    step = plan.steps[1]
    changed_argv = replace(step, commands=(*step.commands[:-1], cmd("partprobe", "/dev/sdx")))
    variants = [
        replace(plan, steps=[plan.steps[0], changed_argv, *plan.steps[2:]]),
        replace(plan, warnings=[*plan.warnings, "extra"]),
        replace(plan, erases="/dev/sdx"),
        plan_add_drive(layout(4, 4), 4 * TB, "/dev/sdc", "wwn:0xa"),
    ]
    assert len({base, *(digest(document(v)) for v in variants)}) == len(variants) + 1


def test_disk_identity_is_part_of_equality() -> None:
    assert Disk("/dev/sda", TB, "wwn:0x1") != Disk("/dev/sda", TB, "wwn:0x2")


# --- CLI ---------------------------------------------------------------------


def test_cli_review_prints_document_and_digest(capsys: pytest.CaptureFixture[str]) -> None:
    args = ["grow", "/dev/sda=4TB", "/dev/sdb=4TB", "--add", "/dev/sdc=4TB"]
    ids = ["--id", "/dev/sda=wwn:0x5000", "--id", "/dev/sdb=wwn:0x5001", "--new-id", "wwn:0x9"]
    assert main([*args, *ids, "--review"]) == 0
    out = json.loads(capsys.readouterr().out)
    expected = document(plan_add_drive(layout(4, 4), 4 * TB, "/dev/sdc", "wwn:0x9"))
    assert out == {"digest": digest(expected), "plan": expected}


def test_cli_refuses_id_for_a_drive_it_was_not_given(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["plan", "/dev/sda=4TB", "--id", "/dev/sdz=wwn:0x1"]) == 2
    assert "--id for /dev/sdz" in capsys.readouterr().err


def test_cli_id_needs_device_and_identity(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["plan", "/dev/sda=4TB", "--id", "wwn:0x1"]) == 2
    assert "DEVICE=IDENTITY" in capsys.readouterr().err


def test_nvme_identity_comes_from_the_namespace() -> None:
    # Drive.WWN is empty for NVMe and the serial belongs to the controller.
    assert from_nvme_namespace("eui.0025385B71B07E2F", "", "", "S6P", 1) == (
        "wwn:eui.0025385b71b07e2f"
    )
    nguid = "e8238fa6bf530001001b448b45d8b4d1"
    assert from_nvme_namespace("", nguid.upper(), "", "S6P", 1) == f"wwn:nguid.{nguid}"
    assert from_nvme_namespace("", "0" * 32, "0025385b71b07e2f", "S6P", 1) == (
        "wwn:eui.0025385b71b07e2f"
    )
    # Nothing unique set: two namespaces on one controller still differ.
    assert from_nvme_namespace("", "", "0000000000000000", "S6P", 1) == "serial:S6P:n1"
    assert from_nvme_namespace("", "", "", "S6P", 2) == "serial:S6P:n2"
    assert from_nvme_namespace("", "", "", "", 1) == ""
