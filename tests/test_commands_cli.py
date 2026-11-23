# SPDX-License-Identifier: GPL-3.0-or-later
import json

import pytest

from tamarack.cli import main
from tamarack.commands import partition_path, partitions, render
from tamarack.planner import make_plan

TB = 10**12


@pytest.mark.parametrize(
    "device, number, expected",
    [
        ("/dev/sda", 2, "/dev/sda2"),
        ("/dev/nvme0n1", 3, "/dev/nvme0n1p3"),
        ("/dev/disk/by-id/ata-WDC_X", 2, "/dev/disk/by-id/ata-WDC_X-part2"),
    ],
)
def test_partition_path(device: str, number: int, expected: str) -> None:
    assert partition_path(device, number) == expected


def test_partitions_follow_layers() -> None:
    plan = make_plan([4 * TB, 4 * TB, 8 * TB, 8 * TB], ["/dev/a", "/dev/b", "/dev/c", "/dev/d"])
    parts = partitions(plan)
    per_drive = {i: [p.label for p in parts if p.drive == i] for i in range(4)}
    assert per_drive[0] == ["tk-meta", "tk-l1"]
    assert per_drive[2] == ["tk-meta", "tk-l1", "tk-l2"]


def test_unused_layer_gets_no_partition() -> None:
    plan = make_plan([4 * TB, 8 * TB])
    assert all(p.layer != 2 for p in partitions(plan))


def test_single_drive_gets_two_copies() -> None:
    plan = make_plan([2 * TB], ["/dev/sda"])
    labels = [p.label for p in partitions(plan)]
    assert labels == ["tk-meta", "tk-l1a", "tk-l1b"]
    text = "\n".join(render(plan))
    assert "--level=5 --raid-devices=2 --force --consistency-policy=ppl" in text
    assert "/dev/mapper/tk-d1-p2 /dev/mapper/tk-d1-p3" in text


def test_every_layer_is_raid5_with_ppl() -> None:
    # Two-member layers too: they keep two copies and can keep PPL (policy.py).
    plan = make_plan([4 * TB, 4 * TB, 8 * TB, 8 * TB])
    lines = [line for line in render(plan) if line.startswith("mdadm --create")]
    assert len(lines) == 2
    for line in lines:
        assert "--level=5" in line and "--consistency-policy=ppl" in line
        assert "--bitmap" not in line


def test_render_is_marked_as_dry_run_and_quotes_paths() -> None:
    plan = make_plan([4 * TB, 4 * TB], ["/dev/disk/by-id/odd name", "/dev/sdb"])
    lines = render(plan)
    assert lines[0].startswith("# tamarack dry run")
    assert any("'/dev/disk/by-id/odd name'" in line for line in lines)


def test_cli_table(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["plan", "4TB", "4TB", "8TB", "8TB"]) == 0
    out = capsys.readouterr().out
    assert "survives one drive failure" in out
    assert "single parity" in out and "mirror" not in out


def test_cli_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["plan", "/dev/sda=4TB", "/dev/sdb=8TB", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["layers"][0]["level"] == "raid5"
    assert data["layers"][1]["level"] == "unused"
    assert data["drives"][0]["device"] == "/dev/sda"


def test_cli_bad_size(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["plan", "lots"]) == 2
    assert "not a size" in capsys.readouterr().err
