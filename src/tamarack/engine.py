# SPDX-License-Identifier: GPL-3.0-or-later
"""The storage engine's core, without D-Bus.

The engine keeps the current layout, resolves drives through UDisks2 (behind
the ``DriveSource`` interface), makes plans with the same planners the CLI
uses and holds them for review until they expire. ``tamarack.service``
exports all of this on the system bus; keeping it separate lets every rule be
tested without a bus.

This step is read-only: nothing here starts a job or runs a command.
Executing a plan, confirming a backup, resuming and cancelling answer
``NotSupported`` until polkit authorization exists (step 5 of the design).
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from . import __version__
from .commands import build_steps
from .grow import plan_add_drive
from .journal import JournalError, load
from .layout import Disk, Layout, layout_from_plan
from .model import Plan, PlanKind
from .planner import make_plan
from .replace import plan_replace
from .review import digest, document
from .steps import ChangePlan

T = TypeVar("T")

ERROR_PREFIX = "com.borealislogistics.Tamarack1.Error"
PLAN_LIFETIME = 30 * 60  # seconds a plan stays executable after it is made
MAX_PLANS = 16
LAYOUT_FILE = "layout.json"
BACKUP_FILE = "backup-confirmed"
JOBS_SUBDIR = "jobs"

# Paths UDisks2 and udev create: /dev/sdb, /dev/nvme0n1, /dev/disk/by-id/...
_DEVICE_PATH = re.compile(r"/dev/[A-Za-z0-9._:+@-]+(/[A-Za-z0-9._:+@-]+)*")


class EngineError(Exception):
    """An error with a D-Bus name, reported to the caller as is."""

    name = "Failed"

    @property
    def dbus_name(self) -> str:
        return f"{ERROR_PREFIX}.{self.name}"


class InvalidArgument(EngineError):
    name = "InvalidArgument"


class NoSuchDrive(EngineError):
    name = "NoSuchDrive"


class DriveChanged(EngineError):
    name = "DriveChanged"


class NoLayout(EngineError):
    name = "NoLayout"


class LayoutExists(EngineError):
    name = "LayoutExists"


class LayoutUnreadable(EngineError):
    name = "LayoutUnreadable"


class Busy(EngineError):
    name = "Busy"


class NoSuchPlan(EngineError):
    name = "NoSuchPlan"


class TooManyPlans(EngineError):
    name = "TooManyPlans"


class NotSupported(EngineError):
    name = "NotSupported"


def validate_device_path(path: str) -> str:
    """Accept only plain, normalised paths under /dev."""
    if len(path) > 255 or not _DEVICE_PATH.fullmatch(path) or os.path.normpath(path) != path:
        raise InvalidArgument(f"not a device path: {path!r}")
    if any(part in (".", "..") for part in path.split("/")):
        raise InvalidArgument(f"not a device path: {path!r}")
    return path


@dataclass(frozen=True)
class DriveInfo:
    device: str  # the path the caller gave
    size: int  # bytes
    identity: str  # see tamarack.identity; never empty


class DriveSource(Protocol):
    def lookup(self, device: str, unused: bool) -> DriveInfo:
        """The whole drive at ``device``; raises NoSuchDrive or InvalidArgument.

        With ``unused``, also refuse a drive that is mounted, part of an array
        or otherwise in use: the drive is about to be erased.
        """
        ...


@dataclass(frozen=True)
class StoredPlan:
    id: int
    kind: PlanKind
    document: dict[str, Any]
    digest: str
    summary: dict[str, Any]
    steps: list[tuple[str, str, list[str]]]  # title, note, commands as shell lines
    warnings: list[str]
    erases: list[str]
    expires_at: int  # Unix time

    @property
    def summary_json(self) -> str:
        return json.dumps(self.summary, sort_keys=True)


def _side(layout: Layout) -> dict[str, Any]:
    return {
        "drives": [d.device for d in layout.drives],
        "drive_failures_tolerated": layout.drive_failures_tolerated,
        "usable_bytes_nominal": layout.usable_nominal,
    }


class Engine:
    def __init__(
        self,
        state_dir: str,
        drives: DriveSource,
        clock: Callable[[], float] = time.time,
        on_battery: Callable[[], bool] = lambda: False,
        root: str = "/",
    ) -> None:
        self.state_dir = state_dir
        self.drives = drives
        self.clock = clock
        self._on_battery = on_battery
        self.root = root
        self._plans: dict[int, StoredPlan] = {}
        self._next_id = 1
        self._layout: Layout | None = None
        self._layout_error = ""
        self.reload()

    # --- state ---------------------------------------------------------------

    def reload(self) -> None:
        """Read the layout file again. An unreadable file blocks all planning."""
        path = os.path.join(self.state_dir, LAYOUT_FILE)
        self._layout, self._layout_error = None, ""
        try:
            with open(path, encoding="utf-8") as f:
                self._layout = Layout.from_json(f.read())
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self._layout_error = f"{path} cannot be used: {exc}"

    @property
    def version(self) -> str:
        return __version__

    @property
    def layout(self) -> Layout | None:
        return self._layout

    @property
    def layout_json(self) -> str:
        return self._layout.to_json() if self._layout else ""

    @property
    def drive_failures_tolerated(self) -> int:
        return self._layout.drive_failures_tolerated if self._layout else 0

    @property
    def usable_bytes(self) -> int:
        return self._layout.usable_nominal if self._layout else 0

    def unfinished_jobs(self) -> list[str]:
        """Job ids whose journal has no job-end (or cannot be read)."""
        directory = os.path.join(self.state_dir, JOBS_SUBDIR)
        try:
            names = sorted(os.listdir(directory))
        except FileNotFoundError:
            return []
        unfinished = []
        for name in names:
            job_id, ext = os.path.splitext(name)
            if ext != ".jsonl":
                continue
            try:
                ended = load(os.path.join(directory, name)).ended
            except (OSError, JournalError):
                ended = False  # a journal that cannot be read still needs a person
            if not ended:
                unfinished.append(job_id)
        return unfinished

    @property
    def state(self) -> str:
        return "paused" if self.unfinished_jobs() else "idle"

    def degraded_layers(self) -> list[int]:
        """Layers whose md array is missing or has missing members (from sysfs)."""
        if not self._layout:
            return []
        degraded = []
        for layer in self._layout.layers:
            link = os.path.join(self.root, "dev", "md", f"tk-l{layer.index}")
            name = os.path.basename(os.path.realpath(link))
            try:
                with open(os.path.join(self.root, "sys", "block", name, "md", "degraded")) as f:
                    missing = int(f.read().strip() or "0")
            except (OSError, ValueError):
                missing = 1  # the array is not assembled
            if missing:
                degraded.append(layer.index)
        return degraded

    @property
    def degraded(self) -> bool:
        return bool(self.degraded_layers())

    @property
    def on_battery(self) -> bool:
        return self._on_battery()

    @property
    def last_backup_confirmed(self) -> int:
        try:
            with open(os.path.join(self.state_dir, BACKUP_FILE), encoding="ascii") as f:
                return max(0, int(f.read().strip()))
        except (OSError, ValueError):
            return 0  # unknown counts as never

    # --- plans ---------------------------------------------------------------

    def plan_ids(self) -> list[int]:
        self.prune()
        return sorted(self._plans)

    def plan(self, plan_id: int) -> StoredPlan:
        self.prune()
        try:
            return self._plans[plan_id]
        except KeyError:
            raise NoSuchPlan(f"no plan {plan_id} (it may have expired)") from None

    def discard(self, plan_id: int) -> None:
        self.plan(plan_id)
        del self._plans[plan_id]

    def prune(self) -> None:
        now = self.clock()
        for plan_id in [i for i, p in self._plans.items() if p.expires_at <= now]:
            del self._plans[plan_id]

    def plan_create(self, devices: list[str]) -> StoredPlan:
        self._ready_to_plan()
        if self._layout is not None:
            raise LayoutExists("a layout already exists; add or replace drives instead")
        if not devices:
            raise InvalidArgument("at least one drive is required")
        infos = [self._lookup(d) for d in devices]
        plan = self._planner(
            lambda: make_plan(
                [i.size for i in infos], [i.device for i in infos], [i.identity for i in infos]
            )
        )
        after = layout_from_plan(plan)
        summary = {"kind": "create", "before": None, "after": _side(after)}
        lines = render_create_steps(plan)
        return self._store(PlanKind.CREATE, document(plan), summary, lines)

    def plan_add(self, device: str) -> StoredPlan:
        layout = self._current()
        info = self._lookup(device)
        change = self._planner(
            lambda: plan_add_drive(layout, info.size, info.device, info.identity)
        )
        return self._store_change(change)

    def plan_replace(self, old_device: str, new_device: str) -> StoredPlan:
        layout = self._current()
        old = self._layout_drive(layout, old_device)
        found = self._lookup(old_device, unused=False)
        if found.identity != old.identity:
            raise DriveChanged(
                f"{old_device} is now {found.identity}, not the {old.identity} in the layout"
            )
        info = self._lookup(new_device)
        change = self._planner(
            lambda: plan_replace(layout, old_device, info.size, info.device, False, info.identity)
        )
        return self._store_change(change)

    def plan_rebuild(self, failed_device: str, new_device: str) -> StoredPlan:
        layout = self._current()
        self._layout_drive(layout, failed_device)  # the failed drive need not be present
        info = self._lookup(new_device)
        change = self._planner(
            lambda: plan_replace(layout, failed_device, info.size, info.device, True, info.identity)
        )
        return self._store_change(change)

    # --- helpers -------------------------------------------------------------

    def _ready_to_plan(self) -> None:
        if self._layout_error:
            raise LayoutUnreadable(self._layout_error)
        jobs = self.unfinished_jobs()
        if jobs:
            raise Busy(f"job {jobs[0]} has not finished; resume or cancel it first")

    def _current(self) -> Layout:
        self._ready_to_plan()
        if self._layout is None:
            raise NoLayout("nothing has been built yet; make a create plan first")
        return self._layout

    def _layout_drive(self, layout: Layout, device: str) -> Disk:
        validate_device_path(device)
        for disk in layout.drives:
            if disk.device == device:
                return disk
        raise InvalidArgument(f"{device} is not part of the layout")

    def _lookup(self, device: str, unused: bool = True) -> DriveInfo:
        info = self.drives.lookup(validate_device_path(device), unused)
        if not info.identity:
            raise NoSuchDrive(f"{device} reports neither a WWN nor a serial number")
        return info

    @staticmethod
    def _planner(make: Callable[[], T]) -> T:
        try:
            return make()
        except ValueError as exc:  # includes UnsupportedChange
            raise InvalidArgument(str(exc)) from exc

    def _store_change(self, change: ChangePlan) -> StoredPlan:
        summary = {
            "kind": change.kind.value,
            "before": _side(change.before),
            "after": _side(change.after),
        }
        lines = [
            (step.title, step.note, [c.shell() for c in step.commands]) for step in change.steps
        ]
        return self._store(change.kind, document(change), summary, lines)

    def _store(
        self,
        kind: PlanKind,
        doc: dict[str, Any],
        summary: dict[str, Any],
        steps: list[tuple[str, str, list[str]]],
    ) -> StoredPlan:
        self.prune()
        if len(self._plans) >= MAX_PLANS:
            raise TooManyPlans(f"{MAX_PLANS} plans are waiting; discard some first")
        plan = StoredPlan(
            id=self._next_id,
            kind=kind,
            document=doc,
            digest=digest(doc),
            summary=summary,
            steps=steps,
            warnings=list(doc["warnings"]),
            erases=list(doc["erases"]),
            expires_at=int(self.clock()) + PLAN_LIFETIME,
        )
        self._plans[plan.id] = plan
        self._next_id += 1
        return plan


def render_create_steps(plan: Plan) -> list[tuple[str, str, list[str]]]:
    return [
        (step.title, step.note, [c.shell() for c in step.commands]) for step in build_steps(plan)
    ]
