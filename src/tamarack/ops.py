# SPDX-License-Identifier: GPL-3.0-or-later
"""Structured commands and steps.

Every change Tamarack plans is a list of Steps, and every Step holds Commands:
argument vectors, never shell strings. The engine will run them with
``subprocess.run(argv)`` (no shell); the dry run renders them as shell text
only for people to read, quoting every argument.

Nothing here runs a command.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterable
from dataclasses import dataclass

# The only programs a plan may call. The engine's runner enforces the same list.
ALLOWED_PROGRAMS = frozenset(
    {
        "btrfs",
        "dmsetup",
        "firewall-cmd",
        "integritysetup",
        "lvcreate",
        "lvextend",
        "mdadm",
        "mkfs.btrfs",
        "parted",
        "partprobe",
        "pvcreate",
        "pvresize",
        "snapper",
        "systemctl",
        "tamarack-meta",
        "vgcreate",
        "vgextend",
    }
)


@dataclass(frozen=True)
class Command:
    """One program invocation.

    ``tolerate_failure`` marks commands whose failure is expected and harmless,
    such as closing a mapping that vanished with a failed drive.
    """

    argv: tuple[str, ...]
    tolerate_failure: bool = False

    def __post_init__(self) -> None:
        if not self.argv:
            raise ValueError("a command needs a program")
        if self.argv[0] not in ALLOWED_PROGRAMS:
            raise ValueError(f"{self.argv[0]!r} is not a program Tamarack may run")
        for arg in self.argv:
            if not isinstance(arg, str) or not arg or "\0" in arg or "\n" in arg:
                raise ValueError(f"invalid argument {arg!r} in {self.argv[0]} command")

    @property
    def program(self) -> str:
        return self.argv[0]

    def shell(self) -> str:
        """The command as one line of shell, for display only."""
        text = " ".join(shlex.quote(a) for a in self.argv)
        return f"{text} || :" if self.tolerate_failure else text


def cmd(*argv: str, tolerate_failure: bool = False) -> Command:
    return Command(tuple(argv), tolerate_failure)


@dataclass(frozen=True)
class Step:
    title: str
    commands: tuple[Command, ...]
    note: str = ""


DRY_RUN_HEADER = "# tamarack dry run: nothing below has been executed."


def render_steps(warnings: Iterable[str], steps: Iterable[Step]) -> list[str]:
    """Dry-run text: header, ``set -euo pipefail``, then numbered steps."""
    lines = [DRY_RUN_HEADER, *warnings, "set -euo pipefail"]
    for number, step in enumerate(steps, start=1):
        lines += ["", f"# {number}. {step.title}"]
        if step.note:
            lines.append(f"#    {step.note}")
        lines += [c.shell() for c in step.commands]
    return lines
