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


def _set(value: str) -> str:
    """An NVMe identifier, or "" when it is all zeros (the drive doesn't set it)."""
    value = value.strip().lower()
    return "" if not value.strip("0-:.") else value


def from_nvme_namespace(wwn: str, nguid: str, eui64: str, serial: str, nsid: int) -> str:
    """The identity of one NVMe namespace (UDisks2 ``NVMe.Namespace``).

    ``Drive.WWN`` is empty for NVMe and the serial is the controller's, which
    every namespace on it shares. So: the namespace's WWN, else its NGUID,
    else its EUI-64, else the controller serial with the namespace number.
    """
    if _set(wwn):
        return validate_identity(f"wwn:{_set(wwn)}")
    if _set(nguid):
        return validate_identity(f"wwn:nguid.{_set(nguid)}")
    if _set(eui64):
        return validate_identity(f"wwn:eui.{_set(eui64)}")
    if serial.strip():
        return validate_identity(f"serial:{serial.strip()}:n{nsid}")
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
