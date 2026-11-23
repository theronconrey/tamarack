# SPDX-License-Identifier: GPL-3.0-or-later
import json
from pathlib import Path

import pytest

from tamarack.cli import main
from tamarack.grow import (
    GrowPlan,
    UnsupportedChange,
    minimum_size_for,
    plan_add_drive,
    render_grow,
)
from tamarack.layout import Layout, Member, layout_from_plan
from tamarack.model import Level
from tamarack.planner import make_plan

TB = 10**12


def fresh(*sizes_tb: int) -> Layout:
    devices = [f"/dev/sd{chr(ord('a') + i)}" for i in range(len(sizes_tb))]
    return layout_from_plan(make_plan([s * TB for s in sizes_tb], devices))


def add(layout: Layout, size_tb: float, device: str | None = None) -> GrowPlan:
    device = device or f"/dev/sd{chr(ord('a') + len(layout.drives))}"
    return plan_add_drive(layout, int(size_tb * TB), device)


def first(lines: list[str], prefix: str) -> int:
    """First line starting with ``prefix``, past the health check's waits."""
    checks = {i - 1 for i, line in enumerate(lines) if line.startswith("mdadm --detail --test")}
    return next(i for i, line in enumerate(lines) if line.startswith(prefix) and i not in checks)


# --- 1 -> 2 ---


def test_single_drive_becomes_two_mirror_layers() -> None:
    plan = add(fresh(4), 4)
    assert [layer.level for layer in plan.after.layers] == [Level.RAID5, Level.RAID5]
    assert all(layer.drive_indexes == {0, 1} for layer in plan.after.layers)
    assert plan.before.drive_failures_tolerated == 0
    assert plan.drive_failures_tolerated == 1


def test_single_to_mirror_doubles_space_and_keeps_slices() -> None:
    plan = add(fresh(4), 4)
    half = plan.before.layers[0].slice_size
    assert plan.usable_after == 2 * plan.usable_before
    assert {layer.slice_size for layer in plan.after.layers} == {half}


def test_copy_moves_before_the_old_half_is_freed() -> None:
    lines = render_grow(add(fresh(4), 4))
    order = [
        first(lines, prefix)
        for prefix in (
            "mdadm --detail --test",
            "parted --script /dev/sdb mklabel gpt",
            "mdadm /dev/md/tk-l1 --replace /dev/mapper/tk-d1-p3 --with /dev/mapper/tk-d2-p2",
            "mdadm --wait /dev/md/tk-l1",
            "mdadm --zero-superblock /dev/mapper/tk-d1-p3",
            "mdadm --create /dev/md/tk-l2",
            "vgextend tamarack /dev/md/tk-l2",
            "btrfs filesystem resize max",
        )
    ]
    assert order == sorted(order)


def test_smaller_second_drive_is_rejected_with_minimum_size() -> None:
    with pytest.raises(ValueError, match="needs at least"):
        add(fresh(4), 2)
    half = fresh(4).layers[0].slice_size
    exact = plan_add_drive(fresh(4), minimum_size_for(half, 2), "/dev/sdb")
    assert exact.after.layers[0].slice_size == half


def test_larger_second_drive_warns_about_unused_space() -> None:
    plan = add(fresh(4), 8)
    assert any("unused" in w for w in plan.warnings)


# --- 2 -> 3 and 3 -> 4 ---


def test_two_copies_widen_to_single_parity() -> None:
    plan = add(fresh(4, 4), 4)
    (layer,) = plan.after.layers
    assert layer.level is Level.RAID5 and layer.member_count == 3
    assert plan.usable_after == 2 * plan.usable_before


def test_conversion_order_is_safe() -> None:
    lines = render_grow(add(fresh(4, 4), 4))
    order = [
        first(lines, prefix)
        for prefix in (
            "mdadm --detail --test /dev/md/tk-l1",
            "integritysetup format --batch-mode --integrity crc32c /dev/sdc2",
            "mdadm /dev/md/tk-l1 --add /dev/mapper/tk-d3-p2",
            "mdadm --grow /dev/md/tk-l1 --consistency-policy=resync",
            "mdadm --grow /dev/md/tk-l1 --raid-devices=3",
            "mdadm --wait /dev/md/tk-l1",
            "mdadm --grow /dev/md/tk-l1 --consistency-policy=ppl",
            "pvresize /dev/md/tk-l1",
            "lvextend",
            "btrfs filesystem resize max",
        )
    ]
    assert order == sorted(order)


def test_single_parity_widens_with_ppl_paused() -> None:
    lines = render_grow(add(fresh(4, 4, 4), 4))
    order = [
        first(lines, prefix)
        for prefix in (
            # Added while PPL is on, so the new member gets room for it.
            "mdadm /dev/md/tk-l1 --add",
            "mdadm --grow /dev/md/tk-l1 --consistency-policy=resync",
            "mdadm --grow /dev/md/tk-l1 --raid-devices=4",
            "mdadm --grow /dev/md/tk-l1 --consistency-policy=ppl",
        )
    ]
    assert order == sorted(order)


def test_walk_from_one_to_four_drives_matches_a_fresh_build() -> None:
    layout = fresh(4)
    for _ in range(3):
        layout = add(layout, 4).after
    assert len(layout.drives) == 4
    assert layout.usable_nominal == pytest.approx(fresh(4, 4, 4, 4).usable_nominal, rel=1e-6)
    assert all(layer.level is Level.RAID5 for layer in layout.layers)
    assert layout.drive_failures_tolerated == 1


def test_waiting_slice_pairs_with_the_new_drive() -> None:
    # 4 + 4 + 8: the top 4 TB of the 8 TB drive waits for a partner.
    plan = add(fresh(4, 4, 8), 8)
    assert [layer.level for layer in plan.after.layers] == [Level.RAID5, Level.RAID5]
    assert plan.after.layers[1].drive_indexes == {2, 3}
    assert plan.usable_after == pytest.approx(fresh(4, 4, 8, 8).usable_nominal, rel=1e-6)
    lines = render_grow(plan)
    assert first(lines, "parted --script /dev/sdc mkpart") < first(
        lines, "mdadm --create /dev/md/tk-l2"
    )


def test_existing_drives_are_never_erased() -> None:
    for plan in (add(fresh(4), 4), add(fresh(4, 4), 4), add(fresh(4, 4, 8), 8)):
        for line in render_grow(plan):
            if line.startswith("parted") and "mklabel" in line:
                assert line.split()[2] == plan.erases


def test_small_new_drive_joins_only_what_fits() -> None:
    # Two layers of 2 TB slices (grown from one drive); 2.1 TB fits one.
    two = add(fresh(4), 4).after
    plan = add(two, 2.1)
    assert sum(1 for layer in plan.after.layers if layer.member_count == 3) == 1
    assert any("stays as before" in w for w in plan.warnings)


def test_too_small_to_join_anything() -> None:
    with pytest.raises(ValueError, match="needs at least"):
        add(fresh(4, 4), 2)


def test_five_drives_not_supported_yet() -> None:
    with pytest.raises(UnsupportedChange, match="5 or more"):
        add(fresh(4, 4, 4, 4), 4)


def test_after_layout_is_valid_and_round_trips() -> None:
    plan = add(fresh(4, 4, 8), 8)
    plan.after.validate()
    assert Layout.from_json(plan.after.to_json()) == plan.after


def test_new_partitions_are_numbered_after_existing_ones() -> None:
    plan = add(fresh(4, 4, 8), 8)
    assert Member(2, 3) in plan.after.layers[1].members  # sdc: p1 meta, p2 layer 1


def test_duplicate_device_is_rejected() -> None:
    with pytest.raises(ValueError, match="already part"):
        add(fresh(4), 4, "/dev/sda")


# --- CLI ---


def test_cli_grow_summary(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["grow", "/dev/sda=4TB", "--add", "/dev/sdb=4TB"]) == 0
    out = capsys.readouterr().out
    assert "survives 1 drive failure" in out
    assert "Move one copy onto the new drive" in out


def test_cli_layout_files_chain(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    one, two, three = (tmp_path / f"{n}.json" for n in (1, 2, 3))
    assert main(["plan", "/dev/sda=4TB", "--layout"]) == 0
    one.write_text(capsys.readouterr().out)
    for src, dst, dev in ((one, two, "/dev/sdb=4TB"), (two, three, "/dev/sdc=4TB")):
        assert main(["grow", "--from", str(src), "--add", dev, "--layout-out", str(dst)]) == 0
    data = json.loads(three.read_text())
    assert [layer["level"] for layer in data["layers"]] == ["raid5", "raid5"]


def test_cli_grow_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["grow", "--add", "/dev/sdb=4TB"]) == 2
    assert "current drives" in capsys.readouterr().err
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    assert main(["grow", "--from", str(bad), "--add", "/dev/sdb=4TB"]) == 2
    assert "not a tamarack layout" in capsys.readouterr().err
    assert main(["grow", "/dev/sda=4TB", "--add", "/dev/sdb=1TB"]) == 2
    assert "too small" in capsys.readouterr().err
