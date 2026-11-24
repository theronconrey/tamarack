# SPDX-License-Identifier: GPL-3.0-or-later
import pytest

from tamarack.model import Level, Protection
from tamarack.planner import make_plan, usable_space
from tamarack.policy import level_for, protection_for
from tamarack.units import GiB, MiB, parse_size

TB = 10**12


def near(value: float) -> object:
    # Reserved space (18 MiB per drive) and alignment shave a little off.
    return pytest.approx(value, rel=1e-4)


@pytest.mark.parametrize(
    "count, expected",
    [
        (1, Protection.SAME_DRIVE_MIRROR),
        (2, Protection.ONE_DRIVE),
        (3, Protection.ONE_DRIVE),
        (4, Protection.ONE_DRIVE),
        (5, Protection.TWO_DRIVE),
        (8, Protection.TWO_DRIVE),
    ],
)
def test_policy_table(count: int, expected: Protection) -> None:
    assert protection_for(count) is expected


def test_policy_rejects_zero_drives() -> None:
    with pytest.raises(ValueError):
        protection_for(0)


@pytest.mark.parametrize(
    "members, protection, level",
    [
        (1, Protection.ONE_DRIVE, Level.UNUSED),
        (2, Protection.ONE_DRIVE, Level.RAID5),
        (3, Protection.ONE_DRIVE, Level.RAID5),
        (4, Protection.ONE_DRIVE, Level.RAID5),
        (2, Protection.TWO_DRIVE, Level.UNUSED),  # never weaker than the pool
        (3, Protection.TWO_DRIVE, Level.UNUSED),
        (4, Protection.TWO_DRIVE, Level.RAID6),
    ],
)
def test_level_for(members: int, protection: Protection, level: Level) -> None:
    assert level_for(members, protection) is level


def test_single_drive_is_mirrored_on_itself() -> None:
    plan = make_plan([2 * TB])
    assert plan.protection is Protection.SAME_DRIVE_MIRROR
    (layer,) = plan.layers
    assert layer.level is Level.RAID5 and layer.copies_on_same_drive
    assert layer.member_count == 2
    assert plan.usable_nominal == near(1 * TB)
    assert plan.drive_failures_tolerated == 0
    assert any("Single drive" in w for w in plan.warnings)


def test_two_equal_drives_keep_two_copies_as_parity() -> None:
    plan = make_plan([4 * TB, 4 * TB])
    assert [layer.level for layer in plan.layers] == [Level.RAID5]
    assert plan.usable_nominal == near(4 * TB)
    assert plan.drive_failures_tolerated == 1


def test_mixed_four_drives_shr_style() -> None:
    plan = make_plan([4 * TB, 4 * TB, 8 * TB, 8 * TB])
    assert [layer.level for layer in plan.layers] == [Level.RAID5, Level.RAID5]
    assert [len(layer.members) for layer in plan.layers] == [4, 2]
    assert plan.usable_nominal == near(16 * TB)
    assert not plan.warnings


def test_one_large_drive_leaves_space_unused() -> None:
    plan = make_plan([4 * TB, 4 * TB, 4 * TB, 8 * TB])
    assert [layer.level for layer in plan.layers] == [Level.RAID5, Level.UNUSED]
    assert plan.usable_nominal == near(12 * TB)
    assert any("unused" in w for w in plan.warnings)


def test_five_drives_switch_to_two_drive_protection() -> None:
    plan = make_plan([4 * TB] * 5)
    assert plan.protection is Protection.TWO_DRIVE
    assert [layer.level for layer in plan.layers] == [Level.RAID6]
    assert plan.usable_nominal == near(12 * TB)
    assert plan.drive_failures_tolerated == 2


def test_two_drive_protection_leaves_thin_layers_waiting() -> None:
    plan = make_plan([4 * TB, 4 * TB, 4 * TB, 8 * TB, 8 * TB])
    assert [layer.level for layer in plan.layers] == [Level.RAID6, Level.UNUSED]
    assert plan.drive_failures_tolerated == 2  # the whole pool, not just most of it
    assert any("until four drives at least that large" in w for w in plan.warnings)
    # A fourth 8 TB-class drive would let the top slices join as RAID6.
    bigger = make_plan([4 * TB, 4 * TB, 8 * TB, 8 * TB, 8 * TB, 8 * TB])
    assert [layer.level for layer in bigger.layers] == [Level.RAID6, Level.RAID6]


def test_near_equal_drives_are_grouped() -> None:
    # Two "4 TB" drives from different vendors differ by a few MB.
    plan = make_plan([4 * TB, 4 * TB + 300 * 10**6, 4 * TB - 200 * 10**6])
    assert len(plan.layers) == 1
    assert plan.layers[0].level is Level.RAID5


def test_layer_slices_add_up_to_largest_bucket() -> None:
    sizes = [2 * TB, 4 * TB, 6 * TB, 6 * TB]
    plan = make_plan(sizes)
    assert sum(layer.slice_size for layer in plan.layers) == max(d.bucket for d in plan.drives)
    for layer in plan.layers:
        assert layer.slice_size % MiB == 0


def test_rebuild_risk_warning_for_large_drives() -> None:
    plan = make_plan([18 * TB, 18 * TB])
    assert any("rebuilds can take days" in w for w in plan.warnings)


def test_estimate_is_below_nominal() -> None:
    plan = make_plan([4 * TB, 4 * TB, 4 * TB])
    assert plan.usable_estimate < plan.usable_nominal


def test_bad_inputs() -> None:
    with pytest.raises(ValueError):
        make_plan([])
    with pytest.raises(ValueError):
        make_plan([4 * TB, 4 * TB], ["/dev/sda", "/dev/sda"])
    with pytest.raises(ValueError):
        usable_space(512 * MiB)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("4TB", 4 * TB),
        ("4T", 4 * TB),
        ("3.84TB", int(3.84 * TB)),
        ("500G", 500 * 10**9),
        ("1TiB", 1024 * GiB),
        ("4096", 4096),
    ],
)
def test_parse_size(text: str, expected: int) -> None:
    assert parse_size(text) == expected


@pytest.mark.parametrize("text", ["", "four", "4XB", "0TB", "-1TB"])
def test_parse_size_rejects(text: str) -> None:
    with pytest.raises(ValueError):
        parse_size(text)
