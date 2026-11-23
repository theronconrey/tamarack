# SPDX-License-Identifier: GPL-3.0-or-later
"""The engine on D-Bus: ``com.borealislogistics.Tamarack1`` on the system bus.

Uses GDBus through PyGObject (Fedora's python3-gobject), with the interface
definition shipped in this package as the single source of truth: the
objects are registered from that XML, so what is documented is what is
served.

Read-only for now. Planning works; ``Execute``, ``ConfirmBackup``,
``Resume`` and ``Cancel`` answer ``NotSupported`` until polkit authorization
is in place (step 5 of docs/design/engine.md). No job objects are exported.

Run as ``/usr/libexec/tamarack-engine`` by ``tamarack-engine.service``
(packaged in a later step), or for development:
``python3 -m tamarack.service --session --state-dir DIR``.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from collections.abc import Callable, Sequence
from importlib import resources
from typing import Any

from .engine import ERROR_PREFIX, Engine, EngineError, NotSupported, StoredPlan

BUS_NAME = "com.borealislogistics.Tamarack1"
MANAGER_PATH = "/com/borealislogistics/Tamarack1"
PLANS_PATH = f"{MANAGER_PATH}/Plans"
MANAGER = f"{BUS_NAME}.Manager"
PLAN = f"{BUS_NAME}.Plan"
DEFAULT_STATE_DIR = "/var/lib/tamarack"

log = logging.getLogger("tamarack-engine")


def interface_xml() -> str:
    return resources.files(__package__).joinpath(f"{BUS_NAME}.xml").read_text(encoding="utf-8")


def plan_path(plan_id: int) -> str:
    return f"{PLANS_PATH}/{plan_id}"


class NameTaken(RuntimeError):
    pass


class Service:
    """Exports an Engine on a GDBus connection."""

    def __init__(self, engine: Engine, connection: Any) -> None:
        from gi.repository import Gio

        self.engine = engine
        self.connection = connection
        node = Gio.DBusNodeInfo.new_for_xml(interface_xml())
        self.infos = {info.name: info for info in node.interfaces}
        self._registrations = [
            connection.register_object(
                MANAGER_PATH, self.infos[MANAGER], self._call, self._get, None
            )
        ]
        self._plans: dict[int, int] = {}  # plan id -> registration id

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

    def sync_plans(self) -> bool:
        """Export a Plan object per stored plan; drop expired ones."""
        current = set(self.engine.plan_ids())
        for plan_id in set(self._plans) - current:
            self.connection.unregister_object(self._plans.pop(plan_id))
        for plan_id in sorted(current - set(self._plans)):
            self._plans[plan_id] = self.connection.register_object(
                plan_path(plan_id), self.infos[PLAN], self._call, self._get, None
            )
        return True  # keep the GLib timeout running

    def close(self) -> None:
        for registration in [*self._registrations, *self._plans.values()]:
            self.connection.unregister_object(registration)
        self._registrations, self._plans = [], {}

    def _plan_id(self, path: str) -> int:
        return int(path.rsplit("/", 1)[1])

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
        from gi.repository import GLib

        try:
            signature, value = self._dispatch(path, interface, method, parameters.unpack())
            invocation.return_value(GLib.Variant(signature, value) if signature else None)
        except EngineError as exc:
            log.info("%s.%s from %s refused: %s", interface, method, sender, exc)
            invocation.return_dbus_error(exc.dbus_name, str(exc))
        except Exception:
            log.exception("%s.%s from %s failed", interface, method, sender)
            invocation.return_dbus_error(f"{ERROR_PREFIX}.Failed", "internal error; see the log")
        finally:
            self.sync_plans()

    def _get(self, connection: Any, sender: str, path: str, interface: str, name: str) -> Any:
        from gi.repository import GLib

        getters = self._manager_properties if interface == MANAGER else self._plan_properties
        try:
            properties = getters(path)
            signature = self.infos[interface].lookup_property(name).signature
            return GLib.Variant(signature, properties[name]())
        except Exception:
            log.exception("reading %s.%s on %s failed", interface, name, path)
            return None  # GDBus reports an error to the caller

    # --- the API -------------------------------------------------------------

    def _dispatch(
        self, path: str, interface: str, method: str, args: tuple[Any, ...]
    ) -> tuple[str, tuple[Any, ...]]:
        e = self.engine
        if interface == MANAGER:
            planners: dict[str, Callable[..., StoredPlan]] = {
                "PlanCreate": e.plan_create,
                "PlanAdd": e.plan_add,
                "PlanReplace": e.plan_replace,
                "PlanRebuild": e.plan_rebuild,
            }
            if method in planners:
                plan = planners[method](*args)
                log.info("%s made plan %d (%s)", method, plan.id, plan.digest)
                return "(o)", (plan_path(plan.id),)
            if method == "ConfirmBackup":
                raise NotSupported("confirming a backup needs polkit, which is not wired up yet")
        if interface == PLAN:
            plan_id = self._plan_id(path)
            if method == "Discard":
                e.discard(plan_id)
                return "", ()
            if method == "Execute":
                e.plan(plan_id)
                raise NotSupported("executing plans is not enabled in this version")
        raise NotSupported(f"{interface}.{method} is not available")

    def _manager_properties(self, path: str) -> dict[str, Callable[[], Any]]:
        e = self.engine
        return {
            "Version": lambda: e.version,
            "State": lambda: e.state,
            "Layout": lambda: e.layout_json,
            "DriveFailuresTolerated": lambda: e.drive_failures_tolerated,
            "UsableBytes": lambda: e.usable_bytes,
            "Degraded": lambda: e.degraded,
            "OnBattery": lambda: e.on_battery,
            "LastBackupConfirmed": lambda: e.last_backup_confirmed,
            "ActiveJob": lambda: "/",
        }

    def _plan_properties(self, path: str) -> dict[str, Callable[[], Any]]:
        plan = self.engine.plan(self._plan_id(path))
        return {
            "Kind": lambda: plan.kind.value,
            "Digest": lambda: plan.digest,
            "Summary": lambda: plan.summary_json,
            "Steps": lambda: plan.steps,
            "Warnings": lambda: plan.warnings,
            "Erases": lambda: plan.erases,
            "ExpiresAt": lambda: plan.expires_at,
        }


def upower_on_battery(connection: Any) -> Callable[[], bool]:
    """UPower's OnBattery, or False when UPower is not running."""

    def on_battery() -> bool:
        from gi.repository import Gio, GLib

        try:
            reply = connection.call_sync(
                "org.freedesktop.UPower",
                "/org/freedesktop/UPower",
                "org.freedesktop.DBus.Properties",
                "Get",
                GLib.Variant("(ss)", ("org.freedesktop.UPower", "OnBattery")),
                GLib.VariantType("(v)"),
                Gio.DBusCallFlags.NONE,
                2000,
                None,
            )
        except GLib.Error:
            return False
        return bool(reply.unpack()[0])

    return on_battery


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

    from .udisks import UDisksDrives

    system = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None) if args.session else system
    engine = Engine(args.state_dir, UDisksDrives(system), on_battery=upower_on_battery(system))
    service = Service(engine, bus)
    try:
        service.request_name()
    except NameTaken as exc:
        log.error("%s", exc)
        return 1
    loop = GLib.MainLoop()
    GLib.timeout_add_seconds(30, service.sync_plans)
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
