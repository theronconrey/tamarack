# SPDX-License-Identifier: GPL-3.0-or-later
"""Plans are argument vectors, and the dry run text has not changed.

The files in tests/golden were captured from the CLI before steps held
structured commands; the dry run must stay byte for byte the same.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tamarack.cli import main
from tamarack.commands import build_steps
from tamarack.grow import plan_add_drive
from tamarack.layout import Layout, layout_from_plan
from tamarack.ops import ALLOWED_PROGRAMS, Command, Step, cmd
from tamarack.planner import make_plan
from tamarack.replace import plan_replace

TB = 10**12

GOLDEN = Path(__file__).parent / "golden"

CASES = {
    "create-1": ["plan", "/dev/sda=4TB"],
    "create-4-mixed": ["plan", "/dev/sda=4TB", "/dev/sdb=4TB", "/dev/sdc=8TB", "/dev/sdd=8TB"],
    "create-quoting": ["plan", "/dev/disk/by-id/ata-WDC odd=4TB", "/dev/nvme0n1=4TB"],
    "grow-1-2": ["grow", "/dev/sda=4TB", "--add", "/dev/sdb=4TB"],
    "grow-2-3": ["grow", "/dev/sda=4TB", "/dev/sdb=4TB", "--add", "/dev/sdc=4TB"],
    "grow-3-4-pair": ["grow", "/dev/sda=4TB", "/dev/sdb=4TB", "/dev/sdc=8TB"]
    + ["--add", "/dev/sdd=8TB"],
    "replace-upgrade": ["replace", "/dev/sda=4TB", "/dev/sdb=4TB", "/dev/sdc=4TB"]
    + ["--old", "/dev/sdb", "--with", "/dev/sdd=4TB"],
    "replace-failed": ["replace", "/dev/sda=4TB", "/dev/sdb=4TB", "/dev/sdc=4TB"]
    + ["--old", "/dev/sdb", "--with", "/dev/sdb=4TB", "--failed"],
}

SHELL_OPERATORS = {"|", "||", "&&", ";", "&", ">", ">>", "<", "$(", "`", ":"}


@pytest.mark.parametrize("name", sorted(CASES))
def test_dry_run_matches_golden(name: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([*CASES[name], "--commands"]) == 0
    assert capsys.readouterr().out == (GOLDEN / f"{name}.txt").read_text()


def test_upgrade_pairs_matches_golden(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    four = tmp_path / "four.json"
    one = tmp_path / "one.json"
    drives = ["/dev/sda=4TB", "/dev/sdb=4TB", "/dev/sdc=4TB", "/dev/sdd=4TB"]
    assert main(["plan", *drives, "--layout"]) == 0
    four.write_text(capsys.readouterr().out)
    first = ["--old", "/dev/sda", "--with", "/dev/sde=8TB", "--layout-out", str(one)]
    assert main(["replace", "--from", str(four), *first]) == 0
    capsys.readouterr()
    second = ["--old", "/dev/sdb", "--with", "/dev/sdf=8TB", "--commands"]
    assert main(["replace", "--from", str(one), *second]) == 0
    assert capsys.readouterr().out == (GOLDEN / "replace-upgrade-pairs.txt").read_text()


def _layout(*sizes: int) -> Layout:
    devices = [f"/dev/sd{chr(ord('a') + i)}" for i in range(len(sizes))]
    return layout_from_plan(make_plan([s * TB for s in sizes], devices))


def _all_steps() -> Iterator[Step]:
    yield from build_steps(
        make_plan([4 * TB, 4 * TB, 8 * TB], ["/dev/sda", "/dev/sdb", "/dev/x y"])
    )
    yield from plan_add_drive(_layout(4), 4 * TB, "/dev/sdb").steps
    yield from plan_add_drive(_layout(4, 4, 8), 8 * TB, "/dev/sdd").steps
    yield from plan_replace(_layout(4, 4, 4), "/dev/sdb", 8 * TB, "/dev/sdd").steps
    yield from plan_replace(_layout(4, 4, 4), "/dev/sdb", 4 * TB, "/dev/sdb", failed=True).steps


def test_every_command_is_an_allowed_argv_without_shell_syntax() -> None:
    commands = [c for step in _all_steps() for c in step.commands]
    assert commands
    for command in commands:
        assert isinstance(command, Command)
        assert command.program in ALLOWED_PROGRAMS
        assert not SHELL_OPERATORS & set(command.argv), command.argv


def test_device_with_space_stays_one_argument() -> None:
    steps = build_steps(make_plan([4 * TB], ["/dev/x y"]))
    argvs = [c.argv for c in steps[0].commands]
    assert ("parted", "--script", "/dev/x y", "mklabel", "gpt") in argvs


def test_only_cleanup_of_a_failed_drive_and_waiting_tolerate_failure() -> None:
    # mdadm --wait exits 1 when there was nothing to wait for; a failed
    # drive's members and mapping may already be gone.
    tolerant = [c for step in _all_steps() for c in step.commands if c.tolerate_failure]
    kinds = {
        c.argv[2]
        if c.argv[0] == "mdadm" and len(c.argv) > 2 and c.argv[1].startswith("/dev/md")
        else c.argv[1]
        for c in tolerant
    }
    assert kinds == {"close", "--wait", "--fail", "--remove"}


@pytest.mark.parametrize(
    "argv",
    [
        (),
        ("rm", "-rf", "/"),
        ("sh", "-c", "true"),
        ("mdadm", ""),
        ("mdadm", "a\0b"),
        ("mdadm", "a\nb"),
    ],
)
def test_command_rejects_bad_argv(argv: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        Command(argv)


def test_shell_rendering_quotes_and_marks_tolerated_failures() -> None:
    assert cmd("parted", "--script", "/dev/x y").shell() == "parted --script '/dev/x y'"
    assert cmd("integritysetup", "close", "tk-d1-p2", tolerate_failure=True).shell() == (
        "integritysetup close tk-d1-p2 || :"
    )
