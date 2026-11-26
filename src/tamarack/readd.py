# SPDX-License-Identifier: GPL-3.0-or-later
"""Put a pool drive that came back into its layers again, erasing nothing.

A drive that drops out (a loose cable, a re-seated drive, one that shows up
late at boot) is still the same drive: the same identity, its partitions,
dm-integrity superblocks and tags intact. Decided Nov 24, 2026: such a drive
is re-added without asking.

The plan is a rebuild onto the drive itself, without partitioning or
formatting anything: drop the stale md members and integrity mappings left
behind when it vanished, open its integrity mappings again (``open``, never
``format``), and ``mdadm --add`` each member back. mdadm re-adds a member
it recognises; with PPL there is no write-intent bitmap, so md then copies
that member's part of every stripe from the other drives. Nothing the
drive holds is trusted until md has rewritten it.

The drive may come back under another path (``/dev/sdc`` where it was
``/dev/sdb``); the layout and the tk-meta records then follow it.

Like the other planners, this only produces data and text.
"""

from __future__ import annotations

from .commands import integrity_device_name, md_device, partition_path
from .layout import Disk, Layout
from .model import PlanKind
from .ops import Command, Step, cmd
from .steps import ChangePlan, health_check, mapper, metadata_step, wait_idle


def plan_readd(
    layout: Layout, drive: int, device: str, missing: list[tuple[int, int]]
) -> ChangePlan:
    """Re-add ``layout.drives[drive]``, now at ``device``.

    ``missing`` lists (partition, layer) for the members md doesn't have.
    """
    if not missing:
        raise ValueError("nothing to re-add: the drive is in every layer it belongs to")
    old = layout.drives[drive]
    drives = list(layout.drives)
    drives[drive] = Disk(device, old.size, old.identity)
    after = Layout(drives=drives, layers=list(layout.layers))
    name = f"the returning drive ({device})"

    stale: list[Command] = []
    for n, layer in missing:
        md, member = md_device(layer), mapper(drive, n)
        stale.append(cmd("mdadm", md, "--fail", member, tolerate_failure=True))
        stale.append(cmd("mdadm", md, "--remove", member, tolerate_failure=True))
    for layer in sorted({layer for _, layer in missing}):
        stale.append(cmd("mdadm", md_device(layer), "--remove", "detached", tolerate_failure=True))
    for n, _ in missing:
        stale.append(
            cmd("integritysetup", "close", integrity_device_name(drive, n), tolerate_failure=True)
        )

    reopen = [
        cmd(
            "integritysetup",
            "open",
            "--integrity",
            "crc32c",
            partition_path(device, n),
            integrity_device_name(drive, n),
        )
        for n, _ in missing
    ]
    readd: list[Command] = []
    for n, layer in missing:
        readd.append(cmd("mdadm", md_device(layer), "--add", mapper(drive, n)))
        readd.append(wait_idle(md_device(layer)))

    steps = [
        Step(
            f"Clear what {name} left behind when it dropped out",
            tuple(stale),
            "md and device-mapper still hold the drive as it was; both go before it comes back.",
        ),
        Step(
            f"Open the integrity mappings on {name} again",
            tuple(reopen),
            "Opened, never formatted: its partitions and checksums are as it left them.",
        ),
        Step(
            f"Put {name} back into its layers",
            tuple(readd),
            "md takes the drive back and copies its part of every stripe from the other "
            "drives, so nothing it held is trusted until it has been rewritten. Your files "
            "stay available meanwhile.",
        ),
    ]
    if device != old.device:
        steps.append(metadata_step(after))
    steps.append(health_check(after))
    return ChangePlan(kind=PlanKind.READD, before=layout, after=after, steps=steps)
