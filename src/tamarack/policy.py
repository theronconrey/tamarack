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

With two-drive protection, a layer is RAID6, so it needs at least four
members. Space that only two or three drives could share is left unused,
waiting until enough drives that large match it: one weaker layer would
make the whole pool (one volume over every layer) survive only one drive
failure. Decided Nov 24, 2026; RAID6 layers keep an internal bitmap,
because md's PPL covers RAID5 only.

Protection only ever moves up as drives are added; it never moves down
except after a failure.
"""

from __future__ import annotations

from .model import Level, Protection

# Drives at or above this size get a rebuild-risk warning (decimal bytes).
REBUILD_RISK_DRIVE_SIZE = 16 * 10**12
MIN_RAID6_MEMBERS = 4


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
        # Never weaker than the pool: see the module docstring.
        return Level.RAID6 if member_count >= MIN_RAID6_MEMBERS else Level.UNUSED
    return Level.RAID5  # two members too: see the module docstring
