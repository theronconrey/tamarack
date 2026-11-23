# SPDX-License-Identifier: GPL-3.0-or-later
"""The reviewed form of a plan, and its digest.

What a person approves must be exactly what runs. Every plan, create or
change, is turned into one review document: its kind, the layout before and
after, each drive and its role, the drives to be erased, every step with every
command's argv and resume rule, and the warnings. ``digest`` is SHA-256 over that document's
canonical JSON. The engine exports the digest with the plan; ``Execute`` must
send it back and is refused if it differs from the plan the engine holds.

The dry-run text is rendered from the same steps, so it is covered too. Time
is not part of the document: the 30-minute plan expiry is kept by the engine
beside the plan, so the same inputs always give the same digest.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .commands import build_steps
from .identity import ExpectedDrive
from .layout import Disk, layout_from_plan
from .model import Plan, PlanKind
from .ops import Step
from .recovery import rule_for
from .steps import ChangePlan

FORMAT = "tamarack-plan"
VERSION = 1

# Drive roles. Drives in every role but FAILED must be present, with the
# identity recorded, before each step runs.
ERASE = "erase"  # wiped and partitioned by this plan
KEEP = "keep"  # part of the layout before and after
LEAVE = "leave"  # replaced while it still works: needed until it is retired
FAILED = "failed"  # replaced after failing: not expected to be present


def _steps(steps: list[Step]) -> list[dict[str, Any]]:
    return [
        {
            "title": step.title,
            "note": step.note,
            "commands": [
                {
                    "argv": list(c.argv),
                    "tolerate_failure": c.tolerate_failure,
                    **rule_for(c).to_dict(),
                }
                for c in step.commands
            ],
        }
        for step in steps
    ]


def _drive(disk: Disk, role: str) -> dict[str, Any]:
    return {"device": disk.device, "size": disk.size, "id": disk.identity, "role": role}


def create_document(plan: Plan) -> dict[str, Any]:
    after = layout_from_plan(plan)
    return {
        "format": FORMAT,
        "version": VERSION,
        "kind": PlanKind.CREATE.value,
        "before": None,
        "after": after.to_dict(),
        "drives": [_drive(d, ERASE) for d in after.drives],
        "erases": [d.device for d in after.drives],
        "steps": _steps(build_steps(plan)),
        "warnings": list(plan.warnings),
    }


def _swapped(plan: ChangePlan) -> int | None:
    """Index of the replaced drive, for replace and rebuild plans."""
    if plan.kind not in (PlanKind.REPLACE, PlanKind.REBUILD):
        return None
    before, after = plan.before.drives, plan.after.drives
    changed = [i for i in range(len(before)) if before[i] != after[i]]
    if changed:
        (index,) = changed
        return index
    # Same path, size and no identities (a CLI dry run): go by the erased path.
    return [d.device for d in after].index(plan.erases)


def change_document(plan: ChangePlan) -> dict[str, Any]:
    before, after = plan.before.drives, plan.after.drives
    swapped = _swapped(plan)
    drives = []
    for i, disk in enumerate(after):
        if i == swapped:
            old_role = FAILED if plan.kind is PlanKind.REBUILD else LEAVE
            drives.append(_drive(before[i], old_role))
        drives.append(_drive(disk, ERASE if i >= len(before) or i == swapped else KEEP))
    return {
        "format": FORMAT,
        "version": VERSION,
        "kind": plan.kind.value,
        "before": plan.before.to_dict(),
        "after": plan.after.to_dict(),
        "drives": drives,
        "erases": [plan.erases] if plan.erases else [],
        "steps": _steps(plan.steps),
        "warnings": list(plan.warnings),
    }


def document(plan: Plan | ChangePlan) -> dict[str, Any]:
    if isinstance(plan, Plan):
        return create_document(plan)
    return change_document(plan)


def canonical(doc: dict[str, Any]) -> bytes:
    """One byte string per document: sorted keys, no spaces, ASCII only."""
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def digest(doc: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical(doc)).hexdigest()


def expected_drives(doc: dict[str, Any]) -> list[ExpectedDrive]:
    """The drives that must be present, for ``identity.verify``."""
    return [
        ExpectedDrive(str(d["device"]), str(d["id"])) for d in doc["drives"] if d["role"] != FAILED
    ]
