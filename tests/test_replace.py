# SPDX-License-Identifier: GPL-3.0-or-later
import json
from pathlib import Path

import pytest

from tamarack.cli import main
from tamarack.grow import plan_add_drive
from tamarack.layout import Layout, layout_from_plan
from tamarack.model import Level
from tamarack.planner import make_plan
from tamarack.replace import plan_replace
from tamarack.steps import ChangePlan, render

TB = 10**12


def fresh(*sizes_tb: int) -> Layout:
    devices = [f"/dev/sd{chr(ord('a') + i)}" for i in range(len(sizes_tb))]
    return layout_from_plan(make_plan([s * TB for s in sizes_tb], devices))


def upgrade(layout: Layout, old: str, new: str, size_tb: float) -> ChangePlan:
    return plan_replace(layout, old, int(size_tb * TB), new)


def first(lines: list[str], prefix: str) -> int:
    """First line starting with ``prefix``, past the health check's waits."""
    checks = {i - 1 for i, line in enumerate(lines) if line.startswith("mdadm --detail --test")}
    return next(i for i, line in enumerate(lines) if line.startswith(prefix) and i not in checks)


# --- upgrades ---


def test_one_larger_drive_keeps_capacity_and_warns() -> None:
    plan = upgrade(fresh(4, 4, 4, 4), "/dev/sda", "/dev/sde", 8)
    assert plan.usable_after == plan.usable_before
    assert plan.after.drives[0].device == "/dev/sde"
    assert any("unused" in w for w in plan.warnings)


def test_upgrading_every_drive_matches_fresh_builds() -> None:
    layout = fresh(4, 4, 4, 4)
    expected = {1: fresh(8, 4, 4, 4), 2: fresh(8, 8, 4, 4), 3: fresh(8, 8, 8, 4)}
    for step, (old, new) in enumerate(
        [
            ("/dev/sda", "/dev/sde"),
            ("/dev/sdb", "/dev/sdf"),
            ("/dev/sdc", "/dev/sdg"),
            ("/dev/sdd", "/dev/sdh"),
        ],
        start=1,
    ):
        layout = upgrade(layout, old, new, 8).after
        if step in expected:
            assert layout.usable_nominal == pytest.approx(expected[step].usable_nominal, rel=1e-6)
    assert layout.usable_nominal == pytest.approx(fresh(8, 8, 8, 8).usable_nominal, rel=1e-6)
    assert all(layer.level is Level.RAID5 for layer in layout.layers)
    assert layout.drive_failures_tolerated == 1


def test_second_upgrade_pairs_the_free_space() -> None:
    once = upgrade(fresh(4, 4, 4, 4), "/dev/sda", "/dev/sde", 8).after
    plan = upgrade(once, "/dev/sdb", "/dev/sdf", 8)
    new_layer = plan.after.layers[-1]
    assert new_layer.level is Level.RAID5 and new_layer.drive_indexes == {0, 1}
    lines = render(plan)
    # sde (drive 0) gains a partition in free space; it is never wiped.
    assert any(line.startswith("parted --script /dev/sde mkpart tk-l") for line in lines)
    assert "parted --script /dev/sde mklabel gpt" not in lines


def test_upgrade_copies_before_retiring_the_old_drive() -> None:
    lines = render(upgrade(fresh(4, 4, 4), "/dev/sdb", "/dev/sdd", 4))
    order = [
        first(lines, prefix)
        for prefix in (
            "mdadm --detail --test /dev/md/tk-l1",
            "parted --script /dev/sdd mklabel gpt",
            "integritysetup open --integrity crc32c /dev/sdd2 tk-d2-p2-new",
            "mdadm /dev/md/tk-l1 --add /dev/mapper/tk-d2-p2-new",
            "mdadm /dev/md/tk-l1 --replace /dev/mapper/tk-d2-p2 --with /dev/mapper/tk-d2-p2-new",
            "mdadm --wait /dev/md/tk-l1",
            "mdadm /dev/md/tk-l1 --remove /dev/mapper/tk-d2-p2",
            "integritysetup close tk-d2-p2",
            "dmsetup rename tk-d2-p2-new tk-d2-p2",
        )
    ]
    assert order == sorted(order)


def test_upgrade_never_touches_the_old_drive_or_others() -> None:
    plan = upgrade(fresh(4, 4, 4), "/dev/sdb", "/dev/sdd", 4)
    for line in render(plan):
        if line.startswith(("parted", "integritysetup format")):
            assert "/dev/sdd" in line


def test_single_drive_upgrade_copies_both_halves() -> None:
    plan = upgrade(fresh(2), "/dev/sda", "/dev/sdb", 4)
    lines = render(plan)
    assert sum(line.startswith("mdadm /dev/md/tk-l1 --replace") for line in lines) == 2
    assert plan.after.drives[0].device == "/dev/sdb"


# --- failed drives ---


def test_failed_drive_is_rebuilt_not_copied() -> None:
    plan = plan_replace(fresh(4, 4, 4), "/dev/sdb", 4 * TB, "/dev/sdd", failed=True)
    lines = render(plan)
    assert not any("--replace" in line for line in lines)
    order = [
        first(lines, prefix)
        for prefix in (
            "mdadm /dev/md/tk-l1 --remove failed",
            "integritysetup close tk-d2-p2 || :",
            "parted --script /dev/sdd mklabel gpt",
            "integritysetup open --integrity crc32c /dev/sdd2 tk-d2-p2",
            "mdadm /dev/md/tk-l1 --add /dev/mapper/tk-d2-p2",
            "mdadm --wait /dev/md/tk-l1",
        )
    ]
    assert order == sorted(order)
    assert any("no spare protection" in w for w in plan.warnings)


def test_failed_drive_can_reuse_the_same_device_path() -> None:
    plan = plan_replace(fresh(4, 4), "/dev/sdb", 4 * TB, "/dev/sdb", failed=True)
    assert plan.after.drives[1].device == "/dev/sdb"


def test_failed_check_skips_only_the_affected_layers() -> None:
    layout = fresh(4, 4, 8, 8)  # layer 2 lives on sdc and sdd only
    lines = render(plan_replace(layout, "/dev/sda", 4 * TB, "/dev/sde", failed=True))
    assert "mdadm --detail --test /dev/md/tk-l2" in lines
    assert "mdadm --detail --test /dev/md/tk-l1" not in lines


def test_failed_larger_replacement_also_expands() -> None:
    layout = plan_add_drive(fresh(4, 4), 8 * TB, "/dev/sdc").after  # 4+4+8
    plan = plan_replace(layout, "/dev/sda", 8 * TB, "/dev/sdd", failed=True)
    assert plan.usable_after > plan.usable_before


def test_single_drive_failure_cannot_be_rebuilt() -> None:
    with pytest.raises(ValueError, match="restore from backup|Restore from backup"):
        plan_replace(fresh(4), "/dev/sda", 4 * TB, "/dev/sdb", failed=True)


# --- validation ---


def test_too_small_replacement_is_rejected_with_minimum() -> None:
    with pytest.raises(ValueError, match="needs at least"):
        upgrade(fresh(8, 8), "/dev/sda", "/dev/sdc", 4)


def test_unknown_and_duplicate_devices() -> None:
    with pytest.raises(ValueError, match="not part of the layout"):
        upgrade(fresh(4, 4), "/dev/sdz", "/dev/sdc", 4)
    with pytest.raises(ValueError, match="already part"):
        upgrade(fresh(4, 4), "/dev/sda", "/dev/sdb", 4)
    with pytest.raises(ValueError, match="alongside"):
        upgrade(fresh(4, 4), "/dev/sda", "/dev/sda", 4)


def test_after_layout_round_trips() -> None:
    plan = upgrade(
        upgrade(fresh(4, 4, 4), "/dev/sda", "/dev/sdd", 8).after, "/dev/sdb", "/dev/sde", 8
    )
    plan.after.validate()
    assert Layout.from_json(plan.after.to_json()) == plan.after


# --- CLI ---


def test_cli_replace_summary(capsys: pytest.CaptureFixture[str]) -> None:
    args = [
        "replace",
        "/dev/sda=4TB",
        "/dev/sdb=4TB",
        "--old",
        "/dev/sda",
        "--with",
        "/dev/sdc=8TB",
    ]
    assert main(args) == 0
    out = capsys.readouterr().out
    assert out.startswith("Replacing /dev/sda with /dev/sdc")
    assert "copy onto the new drive" in out


def test_cli_replace_failed_commands(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out_file = tmp_path / "after.json"
    args = [
        "replace",
        "/dev/sda=4TB",
        "/dev/sdb=4TB",
        "/dev/sdc=4TB",
        "--old",
        "/dev/sdb",
        "--with",
        "/dev/sdb=4TB",
        "--failed",
        "--commands",
        "--layout-out",
        str(out_file),
    ]
    assert main(args) == 0
    assert "--remove failed" in capsys.readouterr().out
    assert json.loads(out_file.read_text())["drives"][1]["device"] == "/dev/sdb"


def test_cli_replace_single_failed(capsys: pytest.CaptureFixture[str]) -> None:
    args = ["replace", "/dev/sda=4TB", "--old", "/dev/sda", "--with", "/dev/sdb=4TB", "--failed"]
    assert main(args) == 2
    assert "backup" in capsys.readouterr().err
