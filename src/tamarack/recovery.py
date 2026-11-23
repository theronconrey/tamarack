# SPDX-License-Identifier: GPL-3.0-or-later
"""What to do with a command that was cut off mid-run.

After a power cut the journal shows every command that finished, and at most
one that started without finishing. Commands after it never started, so only
that one is in doubt. Each command has a resume rule:

* ``rerun``: running it again is harmless (``partprobe``, ``mdadm --wait``,
  formatting a partition nothing uses yet).
* ``probe``: a read-only command tells whether it took effect; if the probe
  succeeds the command is treated as done, otherwise it runs again.
* ``ask``: neither is safe to assume. The job stops and names the command so
  a person can look.

The rules live in this one table so they can be reviewed together. A command
the table does not recognise gets ``ask``. Rules are part of the reviewed plan
document, so the digest covers them too.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .commands import MOUNT_POINT, MOUNT_UNIT
from .ops import Command

# Read-only programs a probe may call, in the forms used below.
PROBE_PROGRAMS = frozenset(
    {"blkid", "btrfs", "dmsetup", "findmnt", "lvs", "mdadm", "pvs", "snapper", "vgs"}
)


class Resume(StrEnum):
    RERUN = "rerun"
    PROBE = "probe"
    ASK = "ask"


@dataclass(frozen=True)
class Rule:
    resume: Resume
    probe: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (self.resume is Resume.PROBE) != bool(self.probe):
            raise ValueError("a probe rule needs a probe, and only a probe rule has one")
        if self.probe and self.probe[0] not in PROBE_PROGRAMS:
            raise ValueError(f"{self.probe[0]!r} is not a probe program")

    def to_dict(self) -> dict[str, object]:
        if self.probe:
            return {"resume": self.resume.value, "probe": list(self.probe)}
        return {"resume": self.resume.value}


RERUN = Rule(Resume.RERUN)
ASK = Rule(Resume.ASK)


def _probe(*argv: str) -> Rule:
    return Rule(Resume.PROBE, argv)


def rule_for(command: Command) -> Rule:
    """The resume rule for ``command``."""
    argv = command.argv
    program, args = argv[0], argv[1:]
    if command.tolerate_failure:
        return RERUN  # its failure is already accepted
    if program == "parted":
        if "mklabel" in args:
            return RERUN  # the drive is being erased anyway
        if "mkpart" not in args:
            return RERUN  # only sets partition names
        return ASK  # a half-made partition needs a look
    if program in ("partprobe", "tamarack-meta"):
        return RERUN  # tamarack-meta rewrites the same record
    if program == "integritysetup":
        if args[0] == "format":
            return RERUN  # a brand-new partition; it is opened only afterwards
        if args[0] == "open":
            return _probe("dmsetup", "info", args[-1])
        return ASK
    if program == "mdadm":
        if args[:2] == ("--detail", "--test") or args[0] in ("--wait", "--zero-superblock"):
            return RERUN
        if args[0] == "--create":
            return _probe("mdadm", "--detail", args[1])
        return ASK  # --add, --replace, --remove, --grow change array membership or shape
    if program == "pvcreate":
        return _probe("pvs", *args)
    if program == "pvresize":
        return RERUN
    if program == "vgcreate":
        return _probe("vgs", args[0])
    if program == "lvcreate" and "--name" in args:
        name = args[args.index("--name") + 1]
        return _probe("lvs", f"{args[-1]}/{name}")
    if program == "mkfs.btrfs":
        return _probe("blkid", "--match-token", "TYPE=btrfs", args[-1])
    if program == "systemctl" and args[:2] == ("enable", "--now"):
        if MOUNT_UNIT in args:
            return _probe("findmnt", "--mountpoint", MOUNT_POINT)
        return RERUN  # enabling timers again changes nothing
    if program == "snapper" and args[:2] == ("--config", args[1]):
        if args[2] == "create-config":
            return _probe("snapper", "--config", args[1], "get-config")
        if args[2] == "set-config":
            return RERUN
    if program == "wipefs":
        return RERUN  # only ever run on a new array, before pvcreate
    if program == "restorecon":
        return RERUN  # labelling again changes nothing
    if program == "btrfs":
        if args[:2] == ("subvolume", "create"):
            return _probe("btrfs", "subvolume", "show", args[2])
        if args[:3] == ("filesystem", "resize", "max"):
            return RERUN
    return ASK
