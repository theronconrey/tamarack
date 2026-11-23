# SPDX-License-Identifier: GPL-3.0-or-later
"""The automatic redundancy policy. Users are never asked for a RAID level.

Decided policy (project plan, Nov 22, 2026):

    1 drive   -> two copies on that drive, in two partitions (+ warning)
    2 drives  -> one-drive protection (two copies)
    3-4       -> one-drive protection
    5+        -> two-drive protection

Layers with one-drive protection are RAID5 from the start, even with two
members (where parity is a copy, so it holds what a mirror holds). RAID5
can keep md's partial parity log (PPL), which closes the write hole and
lets a dirty degraded array start on its own; a mirror converted to RAID5
later has no room for it (mdadm puts PPL before the bad block log, which a
mirror with a bitmap has right after the superblock). Decided Nov 23, 2026.

Protection only ever moves up as drives are added; it never moves down
except after a failure.
"""

from __future__ import annotations

from .model import Level, Protection

# Drives at or above this size get a rebuild-risk warning (decimal bytes).
REBUILD_RISK_DRIVE_SIZE = 16 * 10**12


def protection_for(drive_count: int) -> Protection:
    if drive_count < 1:
        raise ValueError("at least one drive is required")
    if drive_count == 1:
        return Protection.SAME_DRIVE_MIRROR
    if drive_count <= 4:
        return Protection.ONE_DRIVE
    return Protection.TWO_DRIVE


def level_for(member_count: int, protection: Protection) -> Level:
    """Pick the level for one layer from how many drives share that slice."""
    if member_count < 2:
        return Level.UNUSED
    if protection is Protection.TWO_DRIVE:
        if member_count <= 3:
            return Level.RAID1  # 2-way (reduced) or 3-way mirror
        return Level.RAID6
    return Level.RAID5  # two members too: see the module docstring
