# SPDX-License-Identifier: GPL-3.0-or-later
import pytest

from tamarack.layout import Disk, Layout, LayoutLayer, Member, layout_from_plan
from tamarack.model import Level
from tamarack.planner import make_plan

TB = 10**12


def test_from_plan_records_partitions() -> None:
    layout = layout_from_plan(make_plan([4 * TB, 4 * TB, 8 * TB, 8 * TB]))
    assert [layer.level for layer in layout.layers] == [Level.RAID5, Level.RAID1]
    assert layout.layers[1].members == (Member(2, 3), Member(3, 3))


def test_unused_slices_are_not_stored_but_show_as_free() -> None:
    layout = layout_from_plan(make_plan([4 * TB, 8 * TB]))
    assert len(layout.layers) == 1
    assert layout.free(1) == pytest.approx(4 * TB, rel=1e-3)
    assert layout.free(0) == 0


def test_single_drive_layout_has_two_copies_on_one_drive() -> None:
    layout = layout_from_plan(make_plan([2 * TB]))
    (layer,) = layout.layers
    assert layer.copies_on_same_drive
    assert layout.drive_failures_tolerated == 0


def test_json_round_trip() -> None:
    layout = layout_from_plan(make_plan([4 * TB, 4 * TB, 8 * TB]))
    assert Layout.from_json(layout.to_json()) == layout


def test_next_partition() -> None:
    layout = layout_from_plan(make_plan([4 * TB, 4 * TB, 8 * TB, 8 * TB]))
    assert layout.next_partition(0) == 3
    assert layout.next_partition(3) == 4


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda d: d.update(format="other"), "not a tamarack layout"),
        (lambda d: d.update(version=99), "unsupported layout version"),
        (lambda d: d["layers"][0]["members"][0].update(drive=9), "no drive 9"),
        (lambda d: d["layers"][0]["members"][0].update(partition=1), "reserved"),
        (lambda d: d["layers"][0].update(slice_bytes=10 * TB), "over-allocated"),
    ],
)
def test_invalid_layouts_are_rejected(change, message: str) -> None:  # type: ignore[no-untyped-def]
    data = layout_from_plan(make_plan([4 * TB, 4 * TB])).to_dict()
    change(data)
    with pytest.raises(ValueError, match=message):
        Layout.from_dict(data)


def test_duplicate_partition_is_rejected() -> None:
    layer = LayoutLayer(1, Level.RAID1, 10**9, (Member(0, 2), Member(0, 2)))
    with pytest.raises(ValueError, match="used twice"):
        Layout([Disk("/dev/sda", 4 * TB)], [layer]).validate()
