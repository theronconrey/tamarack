# SPDX-License-Identifier: GPL-3.0-or-later
"""The storage engine's core, without D-Bus.

The engine keeps the current layout, resolves drives through UDisks2 (behind
the ``DriveSource`` interface), makes plans with the same planners the CLI
uses and holds them for review until they expire. ``tamarack.service``
exports all of this on the system bus; keeping it separate lets every rule be
tested without a bus.

Executing a plan turns it into a job (``tamarack.job``) run through the
``Runner`` the engine was given. The caller authorizes first: each
``authorize_*`` method validates the request and names the polkit action to
check; the matching action method validates again (time passes during
authentication) and applies every safety check. An engine built without a
runner, as the service is until the VM test matrix passes, refuses to
execute anything.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from . import __version__
from .boot import INTEGRITYTAB, BootError
from .boot import lines as boot_lines
from .boot import members as boot_members
from .boot import merge as merge_integritytab
from .commands import build_steps
from .grow import plan_add_drive
from .identity import DriveChanged as IdentityChanged
from .identity import verify
from .job import Observer, Report, Resolution, Result, Runner, resume, start
from .job import cancel as cancel_journal
from .journal import JobState, JournalError, journal_path, load
from .layout import Disk, Layout, layout_from_plan
from .meta import compact
from .model import Plan, PlanKind
from .planner import make_plan
from .replace import RETIRE_TITLE, plan_replace
from .review import LEAVE, digest, document, expected_drives
from .shares import EXPORTS_FILE, Share, ShareError, make_share
from .shares import dump as dump_shares
from .shares import exports as render_exports
from .shares import load as load_shares
from .steps import ChangePlan

T = TypeVar("T")
log = logging.getLogger("tamarack-engine")

ERROR_PREFIX = "com.borealislogistics.Tamarack1.Error"
PLAN_LIFETIME = 30 * 60  # seconds a plan stays executable after it is made
MAX_PLANS = 16
BACKUP_MAX_AGE = 7 * 24 * 3600  # a backup older than this is not recent
POLKIT = "com.borealislogistics.tamarack"
ACTIONS = {
    PlanKind.CREATE: f"{POLKIT}.create",
    PlanKind.ADD: f"{POLKIT}.add-drive",
    PlanKind.REPLACE: f"{POLKIT}.replace-drive",
    PlanKind.REBUILD: f"{POLKIT}.replace-drive",
}
NEEDS_BACKUP = {PlanKind.ADD, PlanKind.REPLACE, PlanKind.REBUILD}
LAYOUT_FILE = "layout.json"
BACKUP_FILE = "backup-confirmed"
SCHEDULE_FILE = "snapshot-schedule.json"
SHARES_FILE = "shares.json"
NFS_SERVICE = "nfs-server.service"
SNAPSHOT_PERIODS = ("hourly", "daily", "weekly", "monthly")
DEFAULT_SCHEDULE = {"hourly": 24, "daily": 7, "weekly": 4, "monthly": 6}
MAX_KEPT = 1000
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


class StalePlan(EngineError):
    name = "StalePlan"


class NotConfirmed(EngineError):
    name = "NotConfirmed"


class Degraded(EngineError):
    name = "Degraded"


class OnBattery(EngineError):
    name = "OnBattery"


class NoUps(EngineError):
    name = "NoUps"


class NoRecentBackup(EngineError):
    name = "NoRecentBackup"


class NoSuchJob(EngineError):
    name = "NoSuchJob"


class NotBetweenSteps(EngineError):
    name = "NotBetweenSteps"


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

    def present(self, devices: list[str]) -> dict[str, str]:
        """The identity of the drive at each path now; missing paths are left out."""
        ...

    def health(self, devices: list[str]) -> dict[str, tuple[str, str]]:
        """SMART health per device: (ok|failing|unknown|missing, detail)."""
        ...

    def partition_uuids(self, device: str) -> dict[int, str]:
        """GPT partition number -> partition UUID on the drive at ``device``."""
        ...


class MetaSource(Protocol):
    """The layout records on the drives' tk-meta partitions (read only)."""

    def partitions(self) -> list[str]:
        """Every partition labelled tk-meta that is present now."""
        ...

    def read(self, device: str) -> str:
        """The layout JSON on ``device``; raises OSError or ValueError if unreadable."""
        ...


@dataclass(frozen=True)
class ExecuteOptions:
    confirm_erase: list[str]
    no_ups: bool = False


def parse_execute_options(options: dict[str, Any]) -> ExecuteOptions:
    unknown = set(options) - {"confirm-erase", "no-ups"}
    if unknown:
        raise InvalidArgument(f"unknown options: {', '.join(sorted(unknown))}")
    confirm = options.get("confirm-erase")
    if not isinstance(confirm, list) or not all(isinstance(d, str) for d in confirm):
        raise NotConfirmed("type each drive to be erased in confirm-erase")
    no_ups = options.get("no-ups", False)
    if not isinstance(no_ups, bool):
        raise InvalidArgument("no-ups must be a boolean")
    return ExecuteOptions(list(confirm), no_ups)


def parse_resume_options(options: dict[str, Any]) -> Resolution | None:
    unknown = set(options) - {"resolution"}
    if unknown:
        raise InvalidArgument(f"unknown options: {', '.join(sorted(unknown))}")
    if "resolution" not in options:
        return None
    try:
        return Resolution(options["resolution"])
    except ValueError:
        raise InvalidArgument('resolution must be "done" or "rerun"') from None


@dataclass(frozen=True)
class JobStatus:
    id: str
    digest: str
    state: str  # running, waiting, paused, completed, failed or cancelled
    step: int
    step_count: int
    step_title: str
    message: str


class JobRun:
    """One run of a job, to be started on a worker thread with ``run``."""

    def __init__(self, engine: Engine, job_id: str, work: Callable[[Observer | None], Report]):
        self.engine = engine
        self.job_id = job_id
        self._work = work

    def run(self, observer: Observer | None = None) -> Report:
        return self._work(observer)


def write_atomically(path: str, text: str) -> None:
    """Replace ``path`` so a crash leaves either the old file or the new one."""
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    dfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


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
        runner: Runner | None = None,
        ups_present: Callable[[], bool] = lambda: False,
        meta: MetaSource | None = None,
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
        self.runner = runner
        self.meta = meta
        self._ups_present = ups_present
        self._running: str | None = None  # job id of the job on a worker thread
        self._session_jobs: set[str] = set()  # jobs run since start, kept visible
        self._cancel_requested: set[str] = set()
        self._lock = threading.Lock()
        self.reload()

    # --- state ---------------------------------------------------------------

    def reload(self) -> None:
        """Read the layout again: the state file, checked against the drives.

        An unreadable file, drives that disagree with each other or with the
        file, or tk-meta partitions none of which can be read all block
        planning: the engine must never mistake a pool for empty drives.
        """
        path = os.path.join(self.state_dir, LAYOUT_FILE)
        self._layout, self._layout_error = None, ""
        try:
            with open(path, encoding="utf-8") as f:
                self._layout = Layout.from_json(f.read())
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self._layout_error = f"{path} cannot be used: {exc}"
            return
        if self.meta is not None and not self.unfinished_jobs():
            self._check_drives(path)

    def _check_drives(self, path: str) -> None:
        """Compare the layout records on the drives with the state file."""
        assert self.meta is not None
        try:
            partitions = self.meta.partitions()
        except EngineError as exc:
            self._layout_error = f"the drives' layout records cannot be listed: {exc}"
            return
        records: dict[str, list[str]] = {}  # canonical layout -> partitions holding it
        damaged = []
        for partition in partitions:
            try:
                text = compact(Layout.from_json(self.meta.read(partition)))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                log.warning("layout record on %s cannot be read: %s", partition, exc)
                damaged.append(partition)
                continue
            records.setdefault(text, []).append(partition)
        if not partitions:
            return  # no Tamarack drives: a fresh box, or one built before tk-meta records
        if len(records) > 1:
            groups = "; ".join(", ".join(p) for p in records.values())
            self._layout_error = f"the drives disagree about the layout ({groups})"
            return
        if not records:
            self._layout_error = (
                f"tk-meta partitions found ({', '.join(damaged)}) but none can be read"
            )
            return
        ((text, holders),) = records.items()
        on_drives = Layout.from_json(text)
        if self._layout is None:
            log.info("layout taken from the drives (%s)", ", ".join(holders))
            write_atomically(path, on_drives.to_json() + "\n")
            self._layout = on_drives
        elif compact(self._layout) != text:
            self._layout_error = f"{path} differs from the layout recorded on the drives"
            self._layout = None

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
        if self._running:
            return "running"
        return "paused" if self.unfinished_jobs() else "idle"

    @property
    def active_job(self) -> str:
        if self._running:
            return self._running
        unfinished = self.unfinished_jobs()
        return unfinished[0] if unfinished else ""

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

    def health(self) -> dict[str, Any]:
        """The pool's health in one place: arrays, then each drive's SMART state."""
        layout = self._layout
        devices = [d.device for d in layout.drives] if layout else []
        try:
            drives = self.drives.health(devices) if devices else {}
        except EngineError as exc:
            drives = {d: ("unknown", f"UDisks2: {exc}") for d in devices}
        degraded = self.degraded_layers()
        failing = sorted(d for d, (state, _) in drives.items() if state in ("failing", "missing"))
        if degraded or failing:
            status = "degraded" if degraded else "warning"
        else:
            status = "ok" if layout else "none"
        return {
            "status": status,
            "degraded_layers": degraded,
            "drives": [
                {"device": d, "state": drives[d][0], "detail": drives[d][1]} for d in devices
            ],
        }

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

    # --- executing -----------------------------------------------------------

    def authorize_execute(
        self, plan_id: int, plan_digest: str, options: dict[str, Any]
    ) -> tuple[str, dict[str, str]]:
        """Validate an Execute call; return the polkit action and its details."""
        plan = self._executable(plan_id, plan_digest, parse_execute_options(options))
        return ACTIONS[plan.kind], {"kind": plan.kind.value, "erases": " ".join(plan.erases)}

    def execute(self, plan_id: int, plan_digest: str, options: dict[str, Any]) -> JobRun:
        """After authorization: check everything again, then make the job."""
        opts = parse_execute_options(options)
        plan = self._executable(plan_id, plan_digest, opts)
        self._safety(plan, opts)
        with self._lock:
            if self._running:
                raise Busy(f"job {self._running} is running")
            job_id = self._new_job_id()
            path = self._journal(job_id)
            del self._plans[plan_id]
            self._running = job_id
            self._session_jobs.add(job_id)
        doc, runner = plan.document, self._require_runner()

        def work(observer: Observer | None) -> Report:
            return start(doc, job_id, path, runner, self._gate(job_id, doc), observer=observer)

        return JobRun(self, job_id, work)

    def authorize_resume(self, job_id: str, options: dict[str, Any]) -> str:
        self._resumable(job_id)
        parse_resume_options(options)
        return f"{POLKIT}.resume"

    def resume_job(self, job_id: str, options: dict[str, Any]) -> JobRun:
        state = self._resumable(job_id)
        resolution = parse_resume_options(options)
        if self.on_battery:
            raise OnBattery("the UPS is on battery; resume when power is back")
        return self._claim(job_id, state.plan, authorized=True, resolution=resolution)

    def reattach_runs(self) -> list[JobRun]:
        """At start: re-attach to interrupted ``mdadm --wait`` commands, unauthorized."""
        if self.runner is None:
            return []
        runs = []
        for job_id in self.unfinished_jobs():
            try:
                state = load(self._journal(job_id))
            except (OSError, JournalError):
                continue
            if state.in_doubt and _is_wait(state) and self._running is None:
                runs.append(self._claim(job_id, state.plan, authorized=False, resolution=None))
        return runs[:1]  # one job at a time; others wait for Resume

    def authorize_cancel(self, job_id: str) -> str:
        self._unfinished(job_id)
        return f"{POLKIT}.cancel"

    def cancel_job(self, job_id: str) -> Report | None:
        """Cancel between steps. A running job stops at its next step boundary."""
        self._unfinished(job_id)
        if self._running == job_id:
            self._cancel_requested.add(job_id)
            return None
        try:
            return cancel_journal(self._journal(job_id))
        except JournalError as exc:
            raise NotBetweenSteps(str(exc)) from exc

    def job_finished(self, job_id: str, report: Report | None) -> Report | None:
        """Called on the main thread when a JobRun returns (``None`` if it crashed)."""
        with self._lock:
            if self._running == job_id:
                self._running = None
        if report is not None and report.state == "completed":
            doc = load(self._journal(job_id)).plan
            write_atomically(
                os.path.join(self.state_dir, LAYOUT_FILE),
                Layout.from_dict(doc["after"]).to_json() + "\n",
            )
            self.reload()
            self.sync_boot()
        if job_id in self._cancel_requested and report is not None and report.state == "paused":
            self._cancel_requested.discard(job_id)
            return cancel_journal(self._journal(job_id))
        self._cancel_requested.discard(job_id)
        return report

    @property
    def snapshot_schedule(self) -> dict[str, int]:
        try:
            with open(os.path.join(self.state_dir, SCHEDULE_FILE), encoding="utf-8") as f:
                stored = json.load(f)
            return {p: int(stored[p]) for p in SNAPSHOT_PERIODS}
        except (OSError, ValueError, KeyError, TypeError):
            return dict(DEFAULT_SCHEDULE)

    def authorize_snapshot_schedule(self, schedule: dict[str, Any]) -> str:
        self._valid_schedule(schedule)
        return f"{POLKIT}.configure"

    def set_snapshot_schedule(self, schedule: dict[str, Any]) -> dict[str, int]:
        """Apply a new timeline schedule with snapper, then remember it."""
        wanted = self._valid_schedule(schedule)
        runner = self._require_runner()
        if self._layout is None:
            raise NoLayout("there is no pool to snapshot yet")
        if self._running:
            raise Busy(f"job {self._running} is running")
        from .commands import SNAPPER_CONFIG
        from .ops import cmd

        on = any(wanted.values())
        settings = [f"TIMELINE_CREATE={'yes' if on else 'no'}"] + [
            f"TIMELINE_LIMIT_{period.upper()}={wanted[period]}" for period in SNAPSHOT_PERIODS
        ]
        command = cmd("snapper", "--config", SNAPPER_CONFIG, "set-config", *settings)
        result = runner.run(command.argv)
        if result.exit:
            raise EngineError(f"snapper could not change the schedule: {result.stderr.strip()}")
        write_atomically(
            os.path.join(self.state_dir, SCHEDULE_FILE), json.dumps(wanted, sort_keys=True) + "\n"
        )
        return wanted

    @staticmethod
    def _valid_schedule(schedule: dict[str, Any]) -> dict[str, int]:
        if set(schedule) != set(SNAPSHOT_PERIODS):
            raise InvalidArgument(f"give all of {', '.join(SNAPSHOT_PERIODS)}")
        wanted = {}
        for period in SNAPSHOT_PERIODS:
            value = schedule[period]
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= MAX_KEPT:
                raise InvalidArgument(f"{period} must be a whole number from 0 to {MAX_KEPT}")
            wanted[period] = value
        return wanted

    # --- NFS shares ----------------------------------------------------------

    @property
    def shares(self) -> list[Share]:
        try:
            with open(os.path.join(self.state_dir, SHARES_FILE), encoding="utf-8") as f:
                return load_shares(f.read())
        except FileNotFoundError:
            return []
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise EngineError(f"{SHARES_FILE} cannot be read: {exc}") from exc

    def authorize_share(self, name: str, options: dict[str, Any] | None = None) -> str:
        try:
            if options is not None:
                make_share(name, options)
            elif name not in {s.name for s in self.shares}:
                raise ShareError(f"there is no share called {name}")
        except ShareError as exc:
            raise InvalidArgument(str(exc)) from exc
        return f"{POLKIT}.share"

    def set_share(self, name: str, options: dict[str, Any]) -> Share:
        """Create or change a share. Its directory is created, never removed."""
        try:
            share = make_share(name, options)
        except ShareError as exc:
            raise InvalidArgument(str(exc)) from exc
        self._ready_for_shares()
        directory = self._host_path(share.path)
        if not os.path.isdir(directory):
            os.mkdir(directory, 0o2775)
            # chmod while root still owns it: afterwards it would need
            # CAP_FOWNER, which the unit does not grant. Directories keep
            # setgid through chown, so new files keep the share's group.
            os.chmod(directory, 0o2775)
            os.chown(directory, share.uid, share.gid)
        others = [s for s in self.shares if s.name != name]
        self._apply_shares([*others, share])
        return share

    def remove_share(self, name: str) -> None:
        """Stop exporting a share. Its files stay where they are."""
        self.authorize_share(name)
        self._ready_for_shares()
        self._apply_shares([s for s in self.shares if s.name != name])

    def _ready_for_shares(self) -> None:
        self._require_runner()
        if self._layout is None:
            raise NoLayout("there is no pool to share yet")
        if self._running:
            raise Busy(f"job {self._running} is running")

    def _host_path(self, path: str) -> str:
        return os.path.join(self.root, path.lstrip("/"))

    def _apply_shares(self, shares: list[Share]) -> None:
        """Write the exports file, have nfs-server and firewalld follow, then save.

        If nfs-server refuses the new exports, the old file is put back.
        """
        from .ops import cmd

        runner = self._require_runner()
        exports_path = self._host_path(EXPORTS_FILE)
        try:
            with open(exports_path, encoding="utf-8") as f:
                previous: str | None = f.read()
        except FileNotFoundError:
            previous = None
        write_atomically(exports_path, render_exports(shares))

        def run(*argv: str) -> Result:
            result = runner.run(cmd(*argv).argv)
            if result.exit:
                log.warning("%s exited %d: %s", " ".join(argv), result.exit, result.stderr.strip())
            return result

        if shares:
            steps = [
                ("systemctl", "enable", "--now", NFS_SERVICE),
                ("systemctl", "reload", NFS_SERVICE),
            ]
        else:
            steps = [("systemctl", "reload", NFS_SERVICE)]
        for argv in steps:
            result = run(*argv)
            if result.exit:
                if previous is None:
                    os.unlink(exports_path)
                else:
                    write_atomically(exports_path, previous)
                run("systemctl", "reload", NFS_SERVICE)
                raise EngineError(f"nfs-server refused the new exports: {result.stderr.strip()}")
        # Open or close NFS (port 2049 only: the package makes the server
        # NFSv4-only) in firewalld, now and after reboots.
        change = "--add-service=nfs" if shares else "--remove-service=nfs"
        for argv in (("firewall-cmd", "--permanent", change), ("firewall-cmd", change)):
            try:
                run(*argv)
            except OSError as exc:
                log.warning("firewalld is not available (%s); open NFS yourself", exc)
                break
        write_atomically(os.path.join(self.state_dir, SHARES_FILE), dump_shares(shares))

    def authorize_confirm_backup(self) -> str:
        return f"{POLKIT}.confirm-backup"

    def confirm_backup(self) -> int:
        now = int(self.clock())
        write_atomically(os.path.join(self.state_dir, BACKUP_FILE), f"{now}\n")
        return now

    # --- boot ----------------------------------------------------------------

    def sync_boot(self) -> bool:
        """Bring the Tamarack lines in /etc/integritytab in line with the layout.

        Returns True if the file changed. Never raises: a failure is logged,
        and the existing file is left as it was.
        """
        path = os.path.join(self.root, INTEGRITYTAB.lstrip("/"))
        try:
            with open(path, encoding="utf-8") as f:
                existing = f.read()
        except FileNotFoundError:
            existing = ""
        except OSError as exc:
            log.error("cannot read %s: %s", path, exc)
            return False
        ours: list[str] = []
        if self._layout is not None:
            layout = self._layout
            uuids: dict[tuple[int, int], str] = {}
            try:
                for index, disk in enumerate(layout.drives):
                    if any(d == index for d, _ in boot_members(layout)):
                        for number, uuid in self.drives.partition_uuids(disk.device).items():
                            uuids[(index, number)] = uuid
                ours = boot_lines(layout, uuids)
            except (EngineError, BootError) as exc:
                # A missing drive: keep the old lines; the arrays start degraded.
                log.warning("%s not updated: %s", INTEGRITYTAB, exc)
                return False
        wanted = merge_integritytab(existing, ours)
        if wanted == existing:
            return False
        try:
            # In place: the sandbox makes only this file writable, not /etc.
            with open(path, "w", encoding="utf-8") as f:
                f.write(wanted)
                f.flush()
                os.fsync(f.fileno())
        except OSError as exc:
            log.error("cannot write %s: %s", path, exc)
            return False
        log.info("%s now opens %d Tamarack partitions at boot", INTEGRITYTAB, len(ours))
        return True

    # --- jobs ----------------------------------------------------------------

    def job_ids(self) -> list[str]:
        return sorted(set(self.unfinished_jobs()) | self._session_jobs, key=int)

    def job_status(self, job_id: str) -> JobStatus:
        try:
            state = load(self._journal(job_id))
        except (OSError, JournalError) as exc:
            return JobStatus(job_id, "", "paused", 0, 0, "", f"the journal cannot be read: {exc}")
        count = len(state.steps)
        step = min(state.step, count)
        title = state.steps[step - 1]["title"] if step else ""
        if state.ended:
            status = state.result
        elif self._running == job_id:
            status = "waiting" if state.in_doubt and _is_wait(state) else "running"
        else:
            status = "paused"
        return JobStatus(job_id, state.digest, status, step, count, title, state.message)

    # --- helpers -------------------------------------------------------------

    def _require_runner(self) -> Runner:
        if self.runner is None:
            raise NotSupported(
                "this engine has no command runner: executing changes is not enabled yet"
            )
        return self.runner

    def _executable(self, plan_id: int, plan_digest: str, opts: ExecuteOptions) -> StoredPlan:
        self._require_runner()
        plan = self.plan(plan_id)
        if plan_digest != plan.digest:
            raise StalePlan("the plan differs from the one reviewed; review it again")
        self._ready_to_plan()
        current = self._layout.to_dict() if self._layout else None
        if plan.document["before"] != current:
            raise StalePlan("the layout changed since this plan was made; make a new plan")
        if sorted(opts.confirm_erase) != sorted(plan.erases):
            raise NotConfirmed(f"type exactly the drives to be erased: {', '.join(plan.erases)}")
        return plan

    def _safety(self, plan: StoredPlan, opts: ExecuteOptions) -> None:
        expected = expected_drives(plan.document)
        try:
            verify(expected, self.drives.present([e.device for e in expected]))
        except IdentityChanged as exc:
            raise DriveChanged(str(exc)) from exc
        degraded = set(self.degraded_layers())
        if degraded - _rebuilt_layers(plan.document):
            names = ", ".join(str(i) for i in sorted(degraded))
            raise Degraded(f"layer {names} is degraded: replace the failed drive first")
        if self.on_battery:
            raise OnBattery("the UPS is on battery; try again when power is back")
        if not self._ups_present() and not opts.no_ups:
            raise NoUps("no UPS is connected; type no-ups to go ahead without one")
        stale_backup = self.clock() - self.last_backup_confirmed > BACKUP_MAX_AGE
        if plan.kind in NEEDS_BACKUP and stale_backup:
            raise NoRecentBackup("confirm a backup from the last 7 days first")

    def _gate(self, job_id: str, doc: dict[str, Any]) -> Callable[[int], str | None]:
        """Checks before every step; a reason pauses the job."""
        expected = expected_drives(doc)
        leaving = {d["device"] for d in doc["drives"] if d["role"] == LEAVE}
        titles = [step["title"] for step in doc["steps"]]
        retire = titles.index(RETIRE_TITLE) + 1 if RETIRE_TITLE in titles else 0

        def gate(step: int) -> str | None:
            if job_id in self._cancel_requested:
                return "cancel requested"
            if self.on_battery:
                return "the UPS is on battery; resume when power is back"
            # A replaced drive may be pulled once it is retired.
            need = [e for e in expected if not (retire and step > retire and e.device in leaving)]
            try:
                verify(need, self.drives.present([e.device for e in need]))
            except IdentityChanged as exc:
                return str(exc)
            except EngineError as exc:
                return f"the drives could not be checked: {exc}"
            return None

        return gate

    def _claim(
        self,
        job_id: str,
        doc: dict[str, Any],
        authorized: bool,
        resolution: Resolution | None,
    ) -> JobRun:
        runner = self._require_runner()
        with self._lock:
            if self._running:
                raise Busy(f"job {self._running} is running")
            self._running = job_id
            self._session_jobs.add(job_id)
        path, gate = self._journal(job_id), self._gate(job_id, doc)

        def work(observer: Observer | None) -> Report:
            return resume(
                path,
                runner,
                authorized=authorized,
                gate=gate,
                resolution=resolution,
                observer=observer,
            )

        return JobRun(self, job_id, work)

    def _unfinished(self, job_id: str) -> None:
        if not job_id.isdigit() or job_id not in self.unfinished_jobs():
            raise NoSuchJob(f"no unfinished job {job_id}")

    def _resumable(self, job_id: str) -> JobState:
        self._require_runner()
        self._unfinished(job_id)
        if self._running == job_id:
            raise Busy(f"job {job_id} is already running")
        try:
            return load(self._journal(job_id))
        except (OSError, JournalError) as exc:
            raise NoSuchJob(f"job {job_id} cannot be resumed: {exc}") from exc

    def _jobs_dir(self) -> str:
        directory = os.path.join(self.state_dir, JOBS_SUBDIR)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        return directory

    def _journal(self, job_id: str) -> str:
        return journal_path(job_id, self._jobs_dir())

    def _new_job_id(self) -> str:
        ids = [
            int(name.removesuffix(".jsonl"))
            for name in os.listdir(self._jobs_dir())
            if name.endswith(".jsonl") and name.removesuffix(".jsonl").isdigit()
        ]
        return str(max(ids, default=0) + 1)

    def _ready_to_plan(self) -> None:
        if self._layout_error:
            raise LayoutUnreadable(self._layout_error)
        if self._running:
            raise Busy(f"job {self._running} is running")
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


def _is_wait(state: JobState) -> bool:
    argv = state.command(state.step, state.cmd)["argv"]
    return bool(argv[:2] == ["mdadm", "--wait"])


def _rebuilt_layers(doc: dict[str, Any]) -> set[int]:
    """Layers a rebuild plan repairs: those with a member on the failed drive."""
    if doc["kind"] != PlanKind.REBUILD.value:
        return set()
    failed = {d["device"] for d in doc["drives"] if d["role"] == "failed"}
    before = doc["before"]
    indexes = {i for i, d in enumerate(before["drives"]) if d["device"] in failed}
    return {
        layer["index"]
        for layer in before["layers"]
        if any(m["drive"] in indexes for m in layer["members"])
    }


def render_create_steps(plan: Plan) -> list[tuple[str, str, list[str]]]:
    return [
        (step.title, step.note, [c.shell() for c in step.commands]) for step in build_steps(plan)
    ]
