# SPDX-License-Identifier: GPL-3.0-or-later
"""Drive identities: telling physical drives apart when paths move.

``/dev/sdb`` can name a different drive after a reboot or a re-plug. Plans
therefore record each drive's identity, taken by the engine from UDisks2:
``wwn:`` plus the drive's World Wide Name when it has one, otherwise
``serial:`` plus its serial number. Before every step the engine looks the
paths up again and calls ``verify`` with what it sees; any drive that is
missing or now has a different identity stops the job.

Nothing here talks to UDisks2. This module checks values handed to it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

# Printable ASCII without spaces, after a kind prefix. UDisks2 trims serials;
# anything else is refused rather than guessed at.
_IDENTITY = re.compile(r"(wwn|serial):[!-~]{1,128}")


class DriveChanged(ValueError):
    """A drive a plan relies on is missing or is not the drive it was."""


def validate_identity(identity: str) -> str:
    """Return ``identity`` if well formed, else raise ValueError."""
    if not _IDENTITY.fullmatch(identity):
        raise ValueError(f"invalid drive identity {identity!r}: expected wwn:VALUE or serial:VALUE")
    return identity


def from_udisks(wwn: str, serial: str) -> str:
    """The identity for a drive with UDisks2 ``Drive.WWN`` and ``Drive.Serial``.

    Returns "" when the drive reports neither; such a drive cannot be used.
    """
    wwn, serial = wwn.strip(), serial.strip()
    if wwn:
        return validate_identity(f"wwn:{wwn.lower()}")
    if serial:
        return validate_identity(f"serial:{serial}")
    return ""


@dataclass(frozen=True)
class ExpectedDrive:
    device: str
    identity: str


def verify(expected: Iterable[ExpectedDrive], current: Mapping[str, str]) -> None:
    """Check every expected drive is at its path now.

    ``current`` maps device paths to the identity found there now; a path
    with no drive is simply absent. Raises DriveChanged on the first problem.
    """
    for drive in expected:
        if not drive.identity:
            raise DriveChanged(f"{drive.device}: the plan has no identity for this drive")
        found = current.get(drive.device)
        if found is None:
            raise DriveChanged(f"{drive.device} is missing (expected {drive.identity})")
        if found != drive.identity:
            raise DriveChanged(
                f"{drive.device} is now {found}, not {drive.identity}: make a new plan"
            )
