# SPDX-License-Identifier: GPL-3.0-or-later
import pytest

from tamarack.cli import main
from tamarack.grow import (
    GrowPlan,
    UnsupportedChange,
    minimum_size_for,
    plan_add_drive,
    render_grow,
)
from tamarack.model import Level

TB = 10**12


def grow(old: int, new: int) -> GrowPlan:
    return plan_add_drive([old], ["/dev/sda"], new, "/dev/sdb")


def test_single_drive_becomes_two_mirror_layers() -> None:
    plan = grow(4 * TB, 4 * TB)
    assert [layer.level for layer in plan.layers] == [Level.RAID1, Level.RAID1]
    assert all(layer.members == (0, 1) for layer in plan.layers)
    assert plan.drive_failures_tolerated == 1
    assert plan.before.drive_failures_tolerated == 0


def test_usable_space_doubles() -> None:
    plan = grow(4 * TB, 4 * TB)
    assert plan.usable_after == 2 * plan.usable_before


def test_both_layers_keep_the_original_slice_size() -> None:
    plan = grow(4 * TB, 4 * TB)
    half = plan.before.layers[0].slice_size
    assert {layer.slice_size for layer in plan.layers} == {half}


def test_copy_moves_before_the_old_half_is_freed() -> None:
    lines = render_grow(grow(4 * TB, 4 * TB))
    order = [
        next(i for i, line in enumerate(lines) if line.startswith(prefix))
        for prefix in (
            "mdadm --detail --test",
            "sgdisk --zap-all /dev/sdb",
            "mdadm /dev/md/tk-l1 --replace /dev/mapper/tk-d1-p3 --with /dev/mapper/tk-d2-p2",
            "mdadm --wait /dev/md/tk-l1",
            "mdadm --zero-superblock /dev/mapper/tk-d1-p3",
            "mdadm --create /dev/md/tk-l2",
            "vgextend tamarack /dev/md/tk-l2",
            "btrfs filesystem resize max",
        )
    ]
    assert order == sorted(order)


def test_never_erases_the_existing_drive() -> None:
    lines = render_grow(grow(4 * TB, 4 * TB))
    assert not any(line.startswith("sgdisk --zap-all /dev/sda") for line in lines)
    assert not any("integritysetup format" in line and "/dev/sda" in line for line in lines)


def test_new_layer_is_a_bitmap_mirror() -> None:
    lines = render_grow(grow(4 * TB, 4 * TB))
    create = next(line for line in lines if line.startswith("mdadm --create"))
    assert "--level=1" in create and "--bitmap=internal" in create
    assert create.endswith("/dev/mapper/tk-d1-p3 /dev/mapper/tk-d2-p3")


def test_smaller_drive_is_rejected_with_minimum_size() -> None:
    with pytest.raises(ValueError, match="needs at least"):
        grow(4 * TB, 2 * TB)


def test_minimum_size_is_exactly_enough() -> None:
    plan = grow(4 * TB, 4 * TB)
    half = plan.before.layers[0].slice_size
    assert grow(4 * TB, minimum_size_for(half, 2)).layers[0].slice_size == half
    with pytest.raises(ValueError):
        grow(4 * TB, minimum_size_for(half, 2) - 1)


def test_larger_drive_warns_about_unused_space() -> None:
    plan = grow(4 * TB, 8 * TB)
    assert any("unused" in w for w in plan.warnings)
    assert plan.usable_after == 2 * plan.usable_before


def test_unsupported_transition_is_explicit() -> None:
    with pytest.raises(UnsupportedChange, match="1 drive -> 2 drives"):
        plan_add_drive([4 * TB, 4 * TB], ["/dev/sda", "/dev/sdb"], 4 * TB, "/dev/sdc")


def test_duplicate_device_is_rejected() -> None:
    with pytest.raises(ValueError, match="already part"):
        plan_add_drive([4 * TB], ["/dev/sda"], 4 * TB, "/dev/sda")


def test_cli_grow_summary(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["grow", "/dev/sda=4TB", "--add", "/dev/sdb=4TB"]) == 0
    out = capsys.readouterr().out
    assert "survives 1 drive failure" in out
    assert "Move one copy onto the new drive" in out


def test_cli_grow_commands(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["grow", "/dev/sda=4TB", "--add", "/dev/sdb=4TB", "--commands"]) == 0
    assert capsys.readouterr().out.startswith("# tamarack dry run")


def test_cli_grow_too_small(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["grow", "/dev/sda=4TB", "--add", "/dev/sdb=1TB"]) == 2
    assert "too small" in capsys.readouterr().err
