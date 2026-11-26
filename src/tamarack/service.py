# SPDX-License-Identifier: GPL-3.0-or-later
"""The engine on D-Bus: ``io.github.theronconrey.Tamarack1`` on the system bus.

Uses GDBus through PyGObject (Fedora's python3-gobject), with the interface
definition shipped in this package as the single source of truth: the
objects are registered from that XML, so what is documented is what is
served.

Every method call is authorized by polkit before it does anything, using
the action named in the method's ``PolkitAction`` annotation (for
``Execute``, the one for the plan's kind). The check runs asynchronously,
so the engine keeps answering while an authentication dialog is open. Jobs
run on a worker thread; their progress comes back to the main loop as
property changes and signals.

The service gets the real command runner only when the administrator turns
execution on in /etc/tamarack/engine.conf (see ``tamarack.runner``); without
it ``Execute`` and ``Resume`` answer ``NotSupported``. Execution is for test
machines until the VM test matrix passes (docs/design/engine.md, step 7).

Run as ``/usr/libexec/tamarack-engine`` by ``tamarack-engine.service``
(packaged in a later step), or for development:
``python3 -m tamarack.service --session --state-dir DIR``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from importlib import resources
from typing import Any, Protocol

from .engine import ERROR_PREFIX, Engine, EngineError, JobRun, NotSupported, StoredPlan
from .job import Report
from .journal import JobState

BUS_NAME = "io.github.theronconrey.Tamarack1"
MANAGER_PATH = "/io/github/theronconrey/Tamarack1"
PLANS_PATH = f"{MANAGER_PATH}/Plans"
JOBS_PATH = f"{MANAGER_PATH}/Jobs"
MANAGER = f"{BUS_NAME}.Manager"
PLAN = f"{BUS_NAME}.Plan"
JOB = f"{BUS_NAME}.Job"
PROPERTIES = "org.freedesktop.DBus.Properties"
ACCESS_DENIED = "org.freedesktop.DBus.Error.AccessDenied"
READ_ACTION = "io.github.theronconrey.tamarack.read"
UDISKS_NAME = "org.freedesktop.UDisks2"
UDISKS_ROOT = "/org/freedesktop/UDisks2"
DRIVES_SETTLE_MS = 1500  # after the last UDisks2 change, look at the drives
# Methods that may take long (a restore copies files): run on a thread.
BACKGROUND_METHODS = frozenset({"RestoreFromSnapshot"})
DEFAULT_STATE_DIR = "/var/lib/tamarack"

log = logging.getLogger("tamarack-engine")

# (allowed, reason when not)
Decision = Callable[[bool, str], None]


def interface_xml() -> str:
    return resources.files(__package__).joinpath(f"{BUS_NAME}.xml").read_text(encoding="utf-8")


def method_actions() -> dict[tuple[str, str], list[str]]:
    """The polkit actions each method's annotation allows."""
    actions = {}
    for iface in ET.fromstring(interface_xml()).iter("interface"):
        for method in iface.iter("method"):
            values = [
                a.attrib["value"]
                for a in method.iter("annotation")
                if a.attrib["name"] == f"{BUS_NAME}.PolkitAction"
            ]
            actions[(iface.attrib["name"], method.attrib["name"])] = " ".join(values).split()
    return actions


def plan_path(plan_id: int) -> str:
    return f"{PLANS_PATH}/{plan_id}"


def job_path(job_id: str) -> str:
    return f"{JOBS_PATH}/{job_id}"


class NameTaken(RuntimeError):
    pass


class Authority(Protocol):
    def check(self, sender: str, action: str, details: dict[str, str], done: Decision) -> None:
        """Decide whether ``sender`` may do ``action``; call ``done`` on the main loop."""
        ...


class PolkitAuthority:
    """Asks polkit, allowing it to show an authentication dialog."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def check(self, sender: str, action: str, details: dict[str, str], done: Decision) -> None:
        from gi.repository import Gio, GLib

        subject = ("system-bus-name", {"name": GLib.Variant("s", sender)})
        allow_interaction = 1  # CheckAuthorizationFlags.AllowUserInteraction

        def finished(connection: Any, result: Any) -> None:
            try:
                reply = connection.call_finish(result)
                authorized, _challenge, _details = reply.unpack()[0]
            except GLib.Error as exc:
                log.warning("polkit check for %s failed: %s", action, exc.message)
                done(False, "polkit is not available")
                return
            done(bool(authorized), "" if authorized else f"not authorized for {action}")

        self.connection.call(
            "org.freedesktop.PolicyKit1",
            "/org/freedesktop/PolicyKit1/Authority",
            "org.freedesktop.PolicyKit1.Authority",
            "CheckAuthorization",
            GLib.Variant("((sa{sv})sa{ss}us)", (subject, action, details, allow_interaction, "")),
            GLib.VariantType("((bba{ss}))"),
            Gio.DBusCallFlags.NONE,
            GLib.MAXINT,  # an admin may take a while to type a password
            None,
            finished,
        )


class ReadOnlyAuthority:
    """For development on the session bus, where polkit cannot vouch for callers."""

    def check(self, sender: str, action: str, details: dict[str, str], done: Decision) -> None:
        done(action == READ_ACTION, "only reading is allowed on the session bus")


# (D-Bus signature, value) of a method's return, or ("", ()) for none.
Reply = tuple[str, tuple[Any, ...]]
Work = Callable[[], Reply]


class Service:
    """Exports an Engine on a GDBus connection."""

    def __init__(self, engine: Engine, connection: Any, authority: Authority) -> None:
        from gi.repository import Gio, GLib

        self.engine = engine
        self.connection = connection
        self.authority = authority
        self.context = GLib.MainContext.ref_thread_default()
        node = Gio.DBusNodeInfo.new_for_xml(interface_xml())
        self.infos = {info.name: info for info in node.interfaces}
        self.actions = method_actions()
        self._manager = connection.register_object(
            MANAGER_PATH, self.infos[MANAGER], self._call, self._get, None
        )
        self._plans: dict[int, int] = {}  # plan id -> registration id
        self._jobs: dict[str, int] = {}  # job id -> registration id
        self._threads: list[threading.Thread] = []
        self._last_health: dict[str, Any] | None = None
        self._last_space: dict[str, int] | None = None
        self._last_drives: list[dict[str, Any]] | None = None
        self._last_snapshots: list[dict[str, Any]] | None = None
        self._last_candidates: dict[str, dict[str, Any]] | None = None
        self._drives_timer = 0
        self._udisks_watch = 0
        self.sync_objects()

    # --- bus plumbing --------------------------------------------------------

    def request_name(self) -> None:
        """Own the bus name, or raise NameTaken if another engine has it."""
        from gi.repository import Gio, GLib

        reply = self.connection.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "RequestName",
            GLib.Variant("(su)", (BUS_NAME, 4)),  # DBUS_NAME_FLAG_DO_NOT_QUEUE
            GLib.VariantType("(u)"),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
        )
        if reply.unpack()[0] != 1:  # DBUS_REQUEST_NAME_REPLY_PRIMARY_OWNER
            raise NameTaken(f"{BUS_NAME} is already owned: another engine is running")

    def sync_objects(self) -> bool:
        """Export a Plan object per stored plan and a Job object per job."""
        plans = set(self.engine.plan_ids())
        for plan_id in set(self._plans) - plans:
            self.connection.unregister_object(self._plans.pop(plan_id))
        for plan_id in sorted(plans - set(self._plans)):
            self._plans[plan_id] = self.connection.register_object(
                plan_path(plan_id), self.infos[PLAN], self._call, self._get, None
            )
        for job_id in self.engine.job_ids():
            if job_id not in self._jobs:
                self._jobs[job_id] = self.connection.register_object(
                    job_path(job_id), self.infos[JOB], self._call, self._get, None
                )
        return True  # keep the GLib timeout running

    # --- drives coming and going -----------------------------------------------

    def watch_drives(self) -> None:
        """Follow UDisks2: a drive plugged in or pulled updates Candidates and
        Health at once, and a new drive is announced with DriveAppeared."""
        from gi.repository import Gio

        self._last_candidates = {c["device"]: c for c in self.engine.candidates()}
        self._udisks_watch = self.connection.signal_subscribe(
            UDISKS_NAME,
            "org.freedesktop.DBus.ObjectManager",
            None,  # InterfacesAdded and InterfacesRemoved
            UDISKS_ROOT,
            None,
            Gio.DBusSignalFlags.NONE,
            self._udisks_changed,
        )

    def _udisks_changed(self, *_: Any) -> None:
        from gi.repository import GLib

        # A drive arrives as a burst of objects (drive, block, partitions):
        # wait for it to settle, then look once.
        if not self._drives_timer:
            source = GLib.timeout_source_new(DRIVES_SETTLE_MS)
            source.set_callback(lambda *_: self._drives_settled())
            self._drives_timer = source.attach(self.context)

    def _drives_settled(self) -> bool:
        self._drives_timer = 0
        try:
            candidates = {c["device"]: c for c in self.engine.candidates()}
        except Exception:
            log.exception("looking at the drives failed")
            return False
        before, self._last_candidates = self._last_candidates or {}, candidates
        for device, candidate in sorted(candidates.items()):
            old = before.get(device)
            if candidate["verdict"] == "pool":
                continue
            if old is None or old.get("identity") != candidate.get("identity"):
                log.info("drive %s appeared (%s)", device, candidate["verdict"])
                self._emit(
                    MANAGER_PATH,
                    MANAGER,
                    "DriveAppeared",
                    "(ss)",
                    (device, json.dumps(candidate, sort_keys=True)),
                )
        self._manager_changed("Candidates")
        self.check_health()  # a pulled pool drive shows as missing now, not in a minute
        return False  # GLib.SOURCE_REMOVE

    def close(self) -> None:

        if self._udisks_watch:
            self.connection.signal_unsubscribe(self._udisks_watch)
            self._udisks_watch = 0
        if self._drives_timer:
            source = self.context.find_source_by_id(self._drives_timer)
            if source is not None:
                source.destroy()
            self._drives_timer = 0
        for thread in self._threads:
            thread.join()
        for registration in [self._manager, *self._plans.values(), *self._jobs.values()]:
            self.connection.unregister_object(registration)
        self._plans, self._jobs = {}, {}

    def wait_for_jobs(self, timeout: float = 10.0) -> None:
        """Join finished worker threads (tests)."""
        for thread in list(self._threads):
            thread.join(timeout)

    @staticmethod
    def _tail(path: str) -> str:
        return path.rsplit("/", 1)[1]

    def _call(
        self,
        connection: Any,
        sender: str,
        path: str,
        interface: str,
        method: str,
        parameters: Any,
        invocation: Any,
    ) -> None:
        name = f"{interface}.{method}"
        try:
            action, details, work = self._prepare(path, interface, method, parameters.unpack())
            if action not in self.actions[(interface, method)]:
                raise RuntimeError(f"{action} is not an action of {name}")
        except EngineError as exc:
            self._refuse(invocation, name, sender, exc)
            return
        except Exception:
            self._crash(invocation, name, sender)
            return

        def decided(allowed: bool, reason: str) -> None:
            if not allowed:
                log.info("%s from %s denied: %s", name, sender, reason)
                invocation.return_dbus_error(ACCESS_DENIED, reason)
                return
            if method in BACKGROUND_METHODS:
                self._finish_in_background(invocation, name, sender, work)
                return
            self._finish(invocation, name, sender, work)

        self.authority.check(sender, action, details, decided)

    def _finish(self, invocation: Any, name: str, sender: str, work: Work) -> None:
        from gi.repository import GLib

        try:
            signature, value = work()
            # Export new objects before replying, so the caller can use them at once.
            self.sync_objects()
            invocation.return_value(GLib.Variant(signature, value) if signature else None)
        except EngineError as exc:
            self.sync_objects()
            self._refuse(invocation, name, sender, exc)
        except Exception:
            self._crash(invocation, name, sender)

    def _finish_in_background(self, invocation: Any, name: str, sender: str, work: Work) -> None:
        """Run ``work`` on a thread (a restore can copy a lot) and reply when it is done."""
        from gi.repository import GLib

        def run() -> None:
            outcome: tuple[str, Any] | EngineError | None
            try:
                outcome = work()
            except EngineError as exc:
                outcome = exc
            except Exception:
                log.exception("%s from %s failed", name, sender)
                outcome = None

            def reply() -> bool:
                if isinstance(outcome, EngineError):
                    self._refuse(invocation, name, sender, outcome)
                elif outcome is None:
                    invocation.return_dbus_error(
                        f"{ERROR_PREFIX}.Failed", "internal error; see the log"
                    )
                else:
                    signature, value = outcome
                    invocation.return_value(GLib.Variant(signature, value) if signature else None)
                return False  # GLib.SOURCE_REMOVE

            self.context.invoke_full(0, reply)

        thread = threading.Thread(target=run, name=f"call-{name}")
        self._threads = [t for t in self._threads if t.is_alive()] + [thread]
        thread.start()

    @staticmethod
    def _refuse(invocation: Any, name: str, sender: str, exc: EngineError) -> None:
        log.info("%s from %s refused: %s", name, sender, exc)
        invocation.return_dbus_error(exc.dbus_name, str(exc))

    @staticmethod
    def _crash(invocation: Any, name: str, sender: str) -> None:
        log.exception("%s from %s failed", name, sender)
        invocation.return_dbus_error(f"{ERROR_PREFIX}.Failed", "internal error; see the log")

    def _get(self, connection: Any, sender: str, path: str, interface: str, name: str) -> Any:
        from gi.repository import GLib

        getters = {
            MANAGER: self._manager_properties,
            PLAN: self._plan_properties,
            JOB: self._job_properties,
        }[interface]
        try:
            signature = self.infos[interface].lookup_property(name).signature
            return GLib.Variant(signature, getters(path)[name]())
        except Exception:
            log.exception("reading %s.%s on %s failed", interface, name, path)
            return None  # GDBus reports an error to the caller

    # --- the API -------------------------------------------------------------

    def _prepare(
        self, path: str, interface: str, method: str, args: tuple[Any, ...]
    ) -> tuple[str, dict[str, str], Work]:
        """Validate a call: the polkit action, its details, and the work to do."""
        e = self.engine
        if interface == MANAGER:
            planners: dict[str, Callable[..., StoredPlan]] = {
                "PlanCreate": e.plan_create,
                "PlanAdd": e.plan_add,
                "PlanReplace": e.plan_replace,
                "PlanRebuild": e.plan_rebuild,
            }
            if method in planners:
                make = planners[method]

                def plan() -> Reply:
                    stored = make(*args)
                    log.info("%s made plan %d (%s)", method, stored.id, stored.digest)
                    return "(o)", (plan_path(stored.id),)

                return READ_ACTION, {}, plan
            if method == "SetShare":
                name, options = args
                action = e.authorize_share(name, options)

                def set_share() -> Reply:
                    share = e.set_share(name, options)
                    log.info("share %s now exported to %s", share.name, ", ".join(share.clients))
                    self._manager_changed("Shares")
                    return "", ()

                return action, {"share": name}, set_share
            if method == "RemoveShare":
                (name,) = args
                action = e.authorize_share(name)

                def remove_share() -> Reply:
                    e.remove_share(name)
                    log.info("share %s no longer exported", name)
                    self._manager_changed("Shares")
                    return "", ()

                return action, {"share": name}, remove_share
            if method == "SetSnapshotSchedule":
                (schedule,) = args
                action = e.authorize_snapshot_schedule(schedule)

                def snapshots() -> Reply:
                    applied = e.set_snapshot_schedule(schedule)
                    log.info("snapshot schedule set to %s", applied)
                    self._manager_changed("SnapshotSchedule")
                    return "", ()

                return action, {}, snapshots
            if method == "BrowseSnapshot":
                number, folder = args
                action = e.authorize_restore(number, folder)

                def browse() -> Reply:
                    return "(s)", (json.dumps(e.browse_snapshot(number, folder), sort_keys=True),)

                return action, {"snapshot": str(number), "path": folder}, browse
            if method == "RestoreFromSnapshot":
                number, item = args
                action = e.authorize_restore(number, item)

                def restore() -> Reply:
                    restored = e.restore_from_snapshot(number, item)
                    log.info("restored %s from snapshot %d as %s", item, number, restored)
                    return "(s)", (restored,)

                return action, {"snapshot": str(number), "path": item}, restore
            if method == "ConfirmBackup":

                def confirm() -> Reply:
                    when = e.confirm_backup()
                    log.info("backup confirmed at %d", when)
                    self._manager_changed("LastBackupConfirmed")
                    return "", ()

                return e.authorize_confirm_backup(), {}, confirm
        if interface == PLAN:
            plan_id = int(self._tail(path))
            if method == "Discard":
                e.plan(plan_id)

                def discard() -> Reply:
                    e.discard(plan_id)
                    return "", ()

                return READ_ACTION, {}, discard
            if method == "Execute":
                plan_digest, options = args
                action, details = e.authorize_execute(plan_id, plan_digest, options)

                def execute() -> Reply:
                    run = e.execute(plan_id, plan_digest, options)
                    log.info("plan %d executing as job %s", plan_id, run.job_id)
                    self._launch(run)
                    return "(o)", (job_path(run.job_id),)

                return action, details, execute
        if interface == JOB:
            job_id = self._tail(path)
            if method == "Resume":
                (options,) = args
                action = e.authorize_resume(job_id, options)

                def resume() -> Reply:
                    self._launch(e.resume_job(job_id, options))
                    return "", ()

                return action, {"job": job_id}, resume
            if method == "Cancel":
                action = e.authorize_cancel(job_id)

                def cancel() -> Reply:
                    report = e.cancel_job(job_id)
                    if report is not None:
                        self._job_ended(job_id, report)
                    return "", ()

                return action, {"job": job_id}, cancel
        raise NotSupported(f"{interface}.{method} is not available")

    # --- jobs ----------------------------------------------------------------

    def start_reattached(self) -> None:
        """Pick up interrupted resyncs and reshapes after a restart."""
        for run in self.engine.reattach_runs():
            log.info("re-attaching to job %s", run.job_id)
            self._launch(run)

    def _launch(self, run: JobRun) -> None:
        self.sync_objects()
        self._manager_changed("State", "ActiveJob", "JobHistory")

        def observer(record: dict[str, Any], state: JobState) -> None:
            # Runs on the worker thread: take what is needed, then hand over.
            step = record.get("step", 0)
            title = state.steps[step - 1]["title"] if record["t"] == "step-start" else ""
            self.context.invoke_full(0, lambda: self._job_record(run.job_id, record, title))

        def work() -> None:
            report: Report | None = None
            try:
                report = run.run(observer)
            except Exception:
                log.exception("job %s stopped unexpectedly", run.job_id)
            self.context.invoke_full(0, lambda: self._job_returned(run.job_id, report))

        thread = threading.Thread(target=work, name=f"job-{run.job_id}")
        self._threads = [t for t in self._threads if t.is_alive()] + [thread]
        thread.start()

    def _job_record(self, job_id: str, record: dict[str, Any], title: str) -> bool:
        kind = record["t"]
        if kind == "step-start":
            self._emit(job_path(job_id), JOB, "StepStarted", "(us)", (record["step"], title))
        elif kind == "step-end":
            self._emit(job_path(job_id), JOB, "StepFinished", "(u)", (record["step"],))
        if kind in ("job", "step-start", "cmd-start", "cmd-end", "pause", "resume"):
            self._job_changed(job_id)
        return False  # GLib.SOURCE_REMOVE

    def _job_returned(self, job_id: str, report: Report | None) -> bool:
        try:
            final = self.engine.job_finished(job_id, report)
        except Exception:
            log.exception("finishing job %s failed", job_id)
            final = report
        if final is not None:
            log.info("job %s %s: %s", job_id, final.state, final.message)
            self._job_ended(job_id, final)
        self._manager_changed(
            "State",
            "ActiveJob",
            "Layout",
            "DriveFailuresTolerated",
            "UsableBytes",
            "Upgrades",
            "JobHistory",
        )
        return False  # GLib.SOURCE_REMOVE

    def _job_ended(self, job_id: str, report: Report) -> None:
        self._job_changed(job_id)
        if report.state in ("completed", "failed", "cancelled"):
            success = report.state == "completed"
            self._emit(job_path(job_id), JOB, "Finished", "(bs)", (success, report.message))

    # --- health ----------------------------------------------------------------

    def check_health(self) -> bool:
        """Compare the pool's health with the last check; alert on what got worse.

        Runs every minute from the main loop. Alerts go to the journal (with an
        ALERT prefix) and out as D-Bus signals for Cockpit or any other client.
        """
        try:
            health = self.engine.health()
        except Exception:
            log.exception("health check failed")
            return True
        space = self.engine.space()
        if space != self._last_space:
            self._last_space = space
            self._manager_changed("Space")
        drives = self.engine.drive_details()
        if drives != self._last_drives:
            self._last_drives = drives
            self._manager_changed("Drives")
        snapshots = self.engine.snapshots()
        if snapshots != self._last_snapshots:
            self._last_snapshots = snapshots
            self._manager_changed("Snapshots")
        # The first check compares against a clean slate, so a pool that is
        # already degraded when the engine starts is reported too.
        before = self._last_health or {"status": "none", "degraded_layers": [], "drives": []}
        self._last_health = health
        if before == health:
            return True
        new_layers = set(health["degraded_layers"]) - set(before["degraded_layers"])
        for layer in sorted(new_layers):
            log.warning("ALERT: layer %d is degraded; replace the failed drive", layer)
            self._emit(MANAGER_PATH, MANAGER, "ArrayDegraded", "(u)", (layer,))
        was_bad = {d["device"] for d in before["drives"] if d["state"] in ("failing", "missing")}
        for drive in health["drives"]:
            if drive["state"] in ("failing", "missing") and drive["device"] not in was_bad:
                log.warning("ALERT: %s is %s: %s", drive["device"], drive["state"], drive["detail"])
                self._emit(
                    MANAGER_PATH,
                    MANAGER,
                    "DriveFailing",
                    "(ss)",
                    (drive["device"], f"{drive['state']}: {drive['detail']}"),
                )
        if health["status"] == "ok" and before["status"] != "ok":
            log.info("the pool is healthy again")
        self._manager_changed("Health", "Degraded")
        return True

    # --- change notification -------------------------------------------------

    def _emit(self, path: str, interface: str, name: str, signature: str, value: Any) -> None:
        from gi.repository import GLib

        self.connection.emit_signal(None, path, interface, name, GLib.Variant(signature, value))

    def _changed(self, path: str, interface: str, values: dict[str, Any]) -> None:
        from gi.repository import GLib

        signatures = {
            name: self.infos[interface].lookup_property(name).signature for name in values
        }
        changed = {n: GLib.Variant(signatures[n], v) for n, v in values.items()}
        self._emit(path, PROPERTIES, "PropertiesChanged", "(sa{sv}as)", (interface, changed, []))

    def _manager_changed(self, *names: str) -> None:
        getters = self._manager_properties(MANAGER_PATH)
        self._changed(MANAGER_PATH, MANAGER, {n: getters[n]() for n in names})

    def _job_changed(self, job_id: str) -> None:
        getters = self._job_properties(job_path(job_id))
        names = ("State", "Step", "StepTitle", "Message", "NeedsCheck", "CanCancel")
        self._changed(job_path(job_id), JOB, {n: getters[n]() for n in names})

    # --- properties ----------------------------------------------------------

    def _manager_properties(self, path: str) -> dict[str, Callable[[], Any]]:
        e = self.engine
        return {
            "Version": lambda: e.version,
            "State": lambda: e.state,
            "Layout": lambda: e.layout_json,
            "DriveFailuresTolerated": lambda: e.drive_failures_tolerated,
            "UsableBytes": lambda: e.usable_bytes,
            "Space": lambda: json.dumps(e.space(), sort_keys=True),
            "Degraded": lambda: e.degraded,
            "Health": lambda: json.dumps(e.health(), sort_keys=True),
            "OnBattery": lambda: e.on_battery,
            "LastBackupConfirmed": lambda: e.last_backup_confirmed,
            "SnapshotSchedule": lambda: e.snapshot_schedule,
            "Shares": lambda: json.dumps([s.to_dict() for s in e.shares], sort_keys=True),
            "ActiveJob": lambda: job_path(e.active_job) if e.active_job else "/",
            "Drives": lambda: json.dumps(e.drive_details(), sort_keys=True),
            "Upgrades": lambda: json.dumps(e.upgrades(), sort_keys=True),
            "JobHistory": lambda: json.dumps(e.job_history(), sort_keys=True),
            "HostName": lambda: e.host_name,
            "Candidates": lambda: json.dumps(e.candidates(), sort_keys=True),
            "Snapshots": lambda: json.dumps(e.snapshots(), sort_keys=True),
        }

    def _plan_properties(self, path: str) -> dict[str, Callable[[], Any]]:
        plan = self.engine.plan(int(self._tail(path)))
        return {
            "Kind": lambda: plan.kind.value,
            "Digest": lambda: plan.digest,
            "Summary": lambda: plan.summary_json,
            "Steps": lambda: plan.steps,
            "Warnings": lambda: plan.warnings,
            "Erases": lambda: plan.erases,
            "ExpiresAt": lambda: plan.expires_at,
        }

    def _job_properties(self, path: str) -> dict[str, Callable[[], Any]]:
        status = self.engine.job_status(self._tail(path))
        return {
            "Digest": lambda: status.digest,
            "State": lambda: status.state,
            "Step": lambda: status.step,
            "StepCount": lambda: status.step_count,
            "StepTitle": lambda: status.step_title,
            "Progress": lambda: 0.0,  # resync and reshape progress: with the real runner
            "Message": lambda: status.message,
            "NeedsCheck": lambda: status.needs_check,
            "CanCancel": lambda: status.can_cancel,
        }


def upower(connection: Any) -> tuple[Callable[[], bool], Callable[[], bool]]:
    """UPower's OnBattery, and whether a UPS is connected; both false without UPower."""
    from gi.repository import Gio, GLib

    def call(path: str, interface: str, method: str, args: Any, reply: str) -> Any:
        return connection.call_sync(
            "org.freedesktop.UPower",
            path,
            interface,
            method,
            args,
            GLib.VariantType(reply),
            Gio.DBusCallFlags.NONE,
            2000,
            None,
        ).unpack()

    def on_battery() -> bool:
        try:
            args = GLib.Variant("(ss)", ("org.freedesktop.UPower", "OnBattery"))
            return bool(call("/org/freedesktop/UPower", PROPERTIES, "Get", args, "(v)")[0])
        except GLib.Error:
            return False

    def ups_present() -> bool:
        try:
            (devices,) = call(
                "/org/freedesktop/UPower",
                "org.freedesktop.UPower",
                "EnumerateDevices",
                None,
                "(ao)",
            )
            for device in devices:
                args = GLib.Variant("(ss)", ("org.freedesktop.UPower.Device", "Type"))
                if call(device, PROPERTIES, "Get", args, "(v)")[0] == 3:  # UP_DEVICE_KIND_UPS
                    return True
        except GLib.Error:
            pass
        return False

    return on_battery, ups_present


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tamarack-engine")
    parser.add_argument("--session", action="store_true", help="use the session bus (testing)")
    parser.add_argument(
        "--state-dir",
        default=os.environ.get("STATE_DIRECTORY", DEFAULT_STATE_DIR),
        help="state directory (default: $STATE_DIRECTORY from systemd, else %(default)s)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib

    from .runner import CONFIG_FILE, SubprocessRunner, execution_enabled
    from .udisks import DriveRecords, UDisksDrives

    system = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None) if args.session else system
    on_battery, ups_present = upower(system)
    runner = None
    if execution_enabled() and not args.session:
        runner = SubprocessRunner()
        log.warning("execution is enabled in %s: approved plans will change disks", CONFIG_FILE)
    drives = UDisksDrives(system)
    engine = Engine(
        args.state_dir,
        drives,
        on_battery=on_battery,
        ups_present=ups_present,
        runner=runner,
        meta=DriveRecords(drives),
    )
    authority: Authority = ReadOnlyAuthority() if args.session else PolkitAuthority(system)
    service = Service(engine, bus, authority)
    try:
        service.request_name()
    except NameTaken as exc:
        log.error("%s", exc)
        return 1
    engine.sync_boot()
    engine.keep_nfs_v4_only()
    service.watch_drives()
    service.start_reattached()
    loop = GLib.MainLoop()
    GLib.timeout_add_seconds(30, service.sync_objects)
    service.check_health()
    GLib.timeout_add_seconds(60, service.check_health)
    for signum in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, loop.quit)
    log.info("tamarack-engine %s ready on the %s bus", engine.version, bus_kind(args.session))
    loop.run()
    service.close()
    return 0


def bus_kind(session: bool) -> str:
    return "session" if session else "system"


if __name__ == "__main__":
    sys.exit(main())
