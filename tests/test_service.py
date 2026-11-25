# SPDX-License-Identifier: GPL-3.0-or-later
"""The engine on a private D-Bus daemon, with a fake UDisks2 beside it.

Skipped when PyGObject or dbus-daemon is missing; Fedora CI and the RPM
build install both.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

if os.environ.get("TAMARACK_REQUIRE_DBUS_TESTS"):
    import gi
else:
    gi = pytest.importorskip("gi", exc_type=ImportError)
gi.require_version("Gio", "2.0")
from fake_runner import FakeRunner  # noqa: E402
from gi.repository import Gio, GLib  # noqa: E402

from tamarack.engine import Engine  # noqa: E402
from tamarack.layout import layout_from_plan  # noqa: E402
from tamarack.planner import make_plan  # noqa: E402
from tamarack.review import digest, document  # noqa: E402
from tamarack.service import (  # noqa: E402
    BUS_NAME,
    JOB,
    MANAGER,
    MANAGER_PATH,
    PLAN,
    READ_ACTION,
    NameTaken,
    PolkitAuthority,
    ReadOnlyAuthority,
    Service,
    interface_xml,
)
from tamarack.udisks import UDisksDrives  # noqa: E402

TB = 10**12
ERR = "io.github.theronconrey.Tamarack1.Error."

if shutil.which("dbus-daemon") is None and not os.environ.get("TAMARACK_REQUIRE_DBUS_TESTS"):
    pytest.skip("dbus-daemon is not installed", allow_module_level=True)


def connect(address: str) -> Any:
    flags = (
        Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
        | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
    )
    return Gio.DBusConnection.new_for_address_sync(address, flags, None, None)


class Loop:
    """A GLib main loop in its own thread, with its own main context."""

    def __init__(self, setup: Callable[[], Any]) -> None:
        self.context = GLib.MainContext()
        self.loop = GLib.MainLoop(self.context)
        self.ready = threading.Event()
        self.result: Any = None
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, args=(setup,), daemon=True)
        self.thread.start()
        assert self.ready.wait(10), "loop did not start"
        if self.error:
            raise self.error

    def _run(self, setup: Callable[[], Any]) -> None:
        self.context.push_thread_default()
        try:
            self.result = setup()
        except BaseException as exc:
            self.error = exc
            self.ready.set()
            return
        self.ready.set()
        self.loop.run()

    def stop(self) -> None:
        self.context.invoke_full(GLib.PRIORITY_DEFAULT, lambda: self.loop.quit())
        self.thread.join(10)


# --- fake UDisks2 and polkit ------------------------------------------------

UDISKS_XML = """<node><interface name="org.freedesktop.DBus.ObjectManager">
<method name="GetManagedObjects"><arg type="a{oa{sa{sv}}}" direction="out"/></method>
</interface></node>"""

POLKIT_XML = """<node><interface name="org.freedesktop.PolicyKit1.Authority">
<method name="CheckAuthorization">
  <arg type="(sa{sv})" direction="in"/><arg type="s" direction="in"/>
  <arg type="a{ss}" direction="in"/><arg type="u" direction="in"/>
  <arg type="s" direction="in"/><arg type="(bba{ss})" direction="out"/>
</method></interface></node>"""

B, D = "org.freedesktop.UDisks2.Block", "org.freedesktop.UDisks2.Drive"


ATA = "org.freedesktop.UDisks2.Drive.Ata"


def fake_objects(
    drives: dict[str, tuple[int, str]], failing: set[str] | None = None
) -> dict[str, Any]:
    objects: dict[str, Any] = {}
    for n, (device, (size, wwn)) in enumerate(sorted(drives.items())):
        drive = f"/org/freedesktop/UDisks2/drives/d{n}"
        smart = {
            "SmartSupported": GLib.Variant("b", True),
            "SmartEnabled": GLib.Variant("b", True),
            "SmartFailing": GLib.Variant("b", device in (failing or set())),
        }
        objects[drive] = {
            D: {"WWN": GLib.Variant("s", wwn), "Serial": GLib.Variant("s", "")},
            ATA: smart,
        }
        objects[f"/org/freedesktop/UDisks2/block_devices/b{n}"] = {
            B: {
                "Device": GLib.Variant("ay", device.encode() + b"\0"),
                "Symlinks": GLib.Variant("aay", []),
                "Size": GLib.Variant("t", size),
                "Drive": GLib.Variant("o", drive),
                "ReadOnly": GLib.Variant("b", False),
                "MDRaidMember": GLib.Variant("o", "/"),
            }
        }
    return objects


class Fakes:
    """UDisks2 and polkit, served from one connection on their own thread."""

    def __init__(self, drives: dict[str, tuple[int, str]]) -> None:
        self.drives = drives
        self.denied: set[str] = set()
        self.failing: set[str] = set()
        self.checks: list[tuple[str, str, dict[str, str]]] = []  # sender, action, details

    def serve(self, address: str) -> Any:
        connection = connect(address)

        def udisks(
            conn: Any, sender: str, path: str, iface: str, method: str, params: Any, inv: Any
        ) -> None:
            objects = fake_objects(self.drives, self.failing)
            inv.return_value(GLib.Variant("(a{oa{sa{sv}}})", (objects,)))

        def polkit(
            conn: Any, sender: str, path: str, iface: str, method: str, params: Any, inv: Any
        ) -> None:
            subject, action, details, flags, _cancel = params.unpack()
            self.checks.append((subject[1]["name"], action, details))
            allowed = action not in self.denied
            inv.return_value(GLib.Variant("((bba{ss}))", ((allowed, False, {}),)))

        for xml, path, handler in (
            (UDISKS_XML, "/org/freedesktop/UDisks2", udisks),
            (POLKIT_XML, "/org/freedesktop/PolicyKit1/Authority", polkit),
        ):
            info = Gio.DBusNodeInfo.new_for_xml(xml).interfaces[0]
            connection.register_object(path, info, handler, None, None)
        for name in ("org.freedesktop.UDisks2", "org.freedesktop.PolicyKit1"):
            reply = connection.call_sync(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "RequestName",
                GLib.Variant("(su)", (name, 4)),
                None,
                0,
                -1,
                None,
            )
            assert reply.unpack()[0] == 1
        return connection


DRIVES = {
    "/dev/sda": (4 * TB, "0x5000000000000001"),
    "/dev/sdb": (4 * TB, "0x5000000000000002"),
    "/dev/sdc": (8 * TB, "0x5000000000000003"),
}


class Harness:
    def __init__(self, bus: str, state: Path, runner: Any = None, **engine_kw: Any) -> None:
        self.fakes = Fakes(dict(DRIVES))
        self.fake_loop = Loop(lambda: self.fakes.serve(bus))
        self.state = state

        def setup() -> Service:
            connection = connect(bus)
            engine = Engine(
                str(state), UDisksDrives(connection, timeout_ms=5000), runner=runner, **engine_kw
            )
            service = Service(engine, connection, PolkitAuthority(connection))
            service.request_name()
            return service

        self.engine_loop = Loop(setup)
        self.service: Service = self.engine_loop.result
        self.client = connect(bus)
        self.signals: list[tuple[str, str, Any]] = []
        self.client.signal_subscribe(
            BUS_NAME,
            None,
            None,
            None,
            None,
            Gio.DBusSignalFlags.NONE,
            lambda c, sender, path, iface, name, params: self.signals.append(
                (path, name, params.unpack())
            ),
        )

    def stop(self) -> None:
        self.engine_loop.stop()
        self.service.wait_for_jobs()
        self.fake_loop.stop()

    def wait(self, predicate: Callable[[], bool], timeout: float = 10.0) -> None:
        context = GLib.MainContext.default()
        deadline = time.monotonic() + timeout
        while not predicate():
            while context.iteration(False):
                pass
            assert time.monotonic() < deadline, "timed out"
            time.sleep(0.01)


@pytest.fixture
def harness(bus: str, tmp_path: Path) -> Iterator[Harness]:
    state = tmp_path / "state"
    state.mkdir()
    h = Harness(bus, state)
    yield h
    h.stop()


@pytest.fixture
def engine_bus(harness: Harness) -> tuple[Any, Path]:
    return harness.client, harness.state


def call(
    client: Any, method: str, args: Any = None, path: str = MANAGER_PATH, iface: str = MANAGER
) -> Any:
    reply = client.call_sync(BUS_NAME, path, iface, method, args, None, 0, 10_000, None)
    return reply.unpack() if reply is not None else None


def get(client: Any, name: str, path: str = MANAGER_PATH, iface: str = MANAGER) -> Any:
    reply = client.call_sync(
        BUS_NAME,
        path,
        "org.freedesktop.DBus.Properties",
        "Get",
        GLib.Variant("(ss)", (iface, name)),
        None,
        0,
        10_000,
        None,
    )
    return reply.unpack()[0]


def error_of(fn: Callable[[], Any]) -> tuple[str, str]:
    with pytest.raises(GLib.Error) as info:
        fn()
    name = Gio.DBusError.get_remote_error(info.value) or ""
    Gio.DBusError.strip_remote_error(info.value)
    return name, info.value.message


# --- reading and planning ----------------------------------------------------


def test_manager_properties(engine_bus: tuple[Any, Path]) -> None:
    client, _ = engine_bus
    assert get(client, "Version") == "0.1.0"
    assert get(client, "State") == "idle"
    assert get(client, "Layout") == ""
    assert get(client, "ActiveJob") == "/"
    assert get(client, "Degraded") is False
    assert get(client, "LastBackupConfirmed") == 0
    assert json.loads(get(client, "Drives")) == []
    assert json.loads(get(client, "Upgrades")) == {}
    assert json.loads(get(client, "JobHistory")) == []
    assert get(client, "HostName")
    candidates = {c["device"]: c["verdict"] for c in json.loads(get(client, "Candidates"))}
    assert candidates and set(candidates.values()) <= {"suggest", "warn", "refuse"}


def test_introspection_matches_the_shipped_xml(engine_bus: tuple[Any, Path]) -> None:
    client, _ = engine_bus
    xml = call(client, "Introspect", None, iface="org.freedesktop.DBus.Introspectable")[0]
    served = Gio.DBusNodeInfo.new_for_xml(xml).lookup_interface(MANAGER)
    shipped = Gio.DBusNodeInfo.new_for_xml(interface_xml()).lookup_interface(MANAGER)
    assert [m.name for m in served.methods] == [m.name for m in shipped.methods]
    assert [p.name for p in served.properties] == [p.name for p in shipped.properties]


def test_create_plan_over_the_bus(harness: Harness) -> None:
    client = harness.client
    devices = ["/dev/sda", "/dev/sdb", "/dev/sdc"]
    (path,) = call(client, "PlanCreate", GLib.Variant("(as)", (devices,)))
    assert path == f"{MANAGER_PATH}/Plans/1"
    expected = document(
        make_plan(
            [4 * TB, 4 * TB, 8 * TB], devices, [f"wwn:0x500000000000000{i}" for i in (1, 2, 3)]
        )
    )
    assert get(client, "Kind", path, PLAN) == "create"
    assert get(client, "Digest", path, PLAN) == digest(expected)
    assert get(client, "Erases", path, PLAN) == devices
    steps = get(client, "Steps", path, PLAN)
    assert (
        steps[0][0] == "Partition each drive"
        and steps[0][2][0] == "parted --script /dev/sda mklabel gpt"
    )
    assert json.loads(get(client, "Summary", path, PLAN))["kind"] == "create"
    assert get(client, "ExpiresAt", path, PLAN) > 0
    # Planning is authorized as "read", for the caller who asked.
    assert harness.fakes.checks == [(client.get_unique_name(), READ_ACTION, {})]


def test_planning_errors_have_names(engine_bus: tuple[Any, Path]) -> None:
    client, _ = engine_bus
    name, message = error_of(
        lambda: call(client, "PlanCreate", GLib.Variant("(as)", (["/dev/sdq"],)))
    )
    assert name == ERR + "NoSuchDrive" and "/dev/sdq" in message
    name, _ = error_of(lambda: call(client, "PlanCreate", GLib.Variant("(as)", (["../x"],))))
    assert name == ERR + "InvalidArgument"
    name, _ = error_of(lambda: call(client, "PlanAdd", GLib.Variant("(s)", ("/dev/sda",))))
    assert name == ERR + "NoLayout"


def test_polkit_can_deny_reading(harness: Harness) -> None:
    harness.fakes.denied.add(READ_ACTION)
    name, _ = error_of(
        lambda: call(harness.client, "PlanCreate", GLib.Variant("(as)", (["/dev/sda"],)))
    )
    assert name == "org.freedesktop.DBus.Error.AccessDenied"
    assert harness.service.engine.plan_ids() == []


def test_without_a_runner_nothing_executes(harness: Harness) -> None:
    client = harness.client
    (path,) = call(client, "PlanCreate", GLib.Variant("(as)", (["/dev/sda"],)))
    plan_digest = get(client, "Digest", path, PLAN)
    options = {"confirm-erase": GLib.Variant("as", ["/dev/sda"])}
    name, _ = error_of(
        lambda: call(
            client, "Execute", GLib.Variant("(sa{sv})", (plan_digest, options)), path, PLAN
        )
    )
    assert name == ERR + "NotSupported"
    # Refused before polkit was even asked.
    assert [action for _, action, _ in harness.fakes.checks] == [READ_ACTION]


def test_discard_removes_the_plan_object(engine_bus: tuple[Any, Path]) -> None:
    client, _ = engine_bus
    (path,) = call(client, "PlanCreate", GLib.Variant("(as)", (["/dev/sda"],)))
    call(client, "Discard", None, path, PLAN)
    name, _ = error_of(lambda: get(client, "Kind", path, PLAN))
    assert name.startswith("org.freedesktop.DBus.Error.")


def test_add_plan_with_a_layout(bus: str, tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    plan = make_plan(
        [4 * TB, 4 * TB],
        ["/dev/sda", "/dev/sdb"],
        ["wwn:0x5000000000000001", "wwn:0x5000000000000002"],
    )
    (state / "layout.json").write_text(layout_from_plan(plan).to_json())
    h = Harness(bus, state)
    try:
        client = h.client
        assert get(client, "DriveFailuresTolerated") == 1
        (path,) = call(client, "PlanAdd", GLib.Variant("(s)", ("/dev/sdc",)))
        assert get(client, "Kind", path, PLAN) == "add"
        assert get(client, "Erases", path, PLAN) == ["/dev/sdc"]
        name, _ = error_of(
            lambda: call(client, "PlanCreate", GLib.Variant("(as)", (["/dev/sdc"],)))
        )
        assert name == ERR + "LayoutExists"
    finally:
        h.stop()


def test_a_second_engine_refuses_to_start(harness: Harness, bus: str) -> None:
    connection = connect(bus)
    service = Service(
        Engine("/nonexistent", UDisksDrives(connection)), connection, ReadOnlyAuthority()
    )
    with pytest.raises(NameTaken):
        service.request_name()
    service.close()


# --- executing, with a fake runner -------------------------------------------


@pytest.fixture
def runner_harness(bus: str, tmp_path: Path) -> Iterator[tuple[Harness, FakeRunner]]:
    state = tmp_path / "state"
    state.mkdir()
    runner = FakeRunner()
    h = Harness(bus, state, runner=runner)
    yield h, runner
    h.stop()


def execute(h: Harness, path: str, erase: list[str], **extra: Any) -> Any:
    plan_digest = get(h.client, "Digest", path, PLAN)
    options = {"confirm-erase": GLib.Variant("as", erase), "no-ups": GLib.Variant("b", True)}
    options.update(extra)
    return call(h.client, "Execute", GLib.Variant("(sa{sv})", (plan_digest, options)), path, PLAN)


def test_execute_runs_the_job_and_records_the_layout(
    runner_harness: tuple[Harness, FakeRunner],
) -> None:
    h, runner = runner_harness
    devices = ["/dev/sda", "/dev/sdb"]
    (plan,) = call(h.client, "PlanCreate", GLib.Variant("(as)", (devices,)))
    expected_commands = [
        tuple(c["argv"])
        for step in h.service.engine.plan(1).document["steps"]
        for c in step["commands"]
    ]
    (job,) = execute(h, plan, devices)
    assert job == f"{MANAGER_PATH}/Jobs/1"
    h.wait(lambda: any(name == "Finished" for _, name, _ in h.signals))
    assert get(h.client, "State", job, JOB) == "completed"
    assert get(h.client, "StepCount", job, JOB) == 7
    assert runner.calls == expected_commands
    started = [args for path, name, args in h.signals if name == "StepStarted"]
    assert started[0] == (1, "Partition each drive") and len(started) == 7
    assert (job, "Finished", (True, "")) in h.signals
    # The plan was consumed, and the new layout is now the box's layout.
    name, _ = error_of(lambda: get(h.client, "Kind", plan, PLAN))
    assert name.startswith("org.freedesktop.DBus.Error.")
    assert json.loads(get(h.client, "Layout"))["format"] == "tamarack-layout"
    assert get(h.client, "DriveFailuresTolerated") == 1
    assert get(h.client, "State") == "idle"
    (done,) = json.loads(get(h.client, "JobHistory"))
    assert done["kind"] == "create" and done["result"] == "completed"
    assert [d["device"] for d in json.loads(get(h.client, "Drives"))] == devices
    assert json.loads(get(h.client, "Upgrades"))["usable"] > 0
    # Execute was authorized with the create action, naming the drives.
    assert (
        h.client.get_unique_name(),
        "io.github.theronconrey.tamarack.create",
        {"kind": "create", "erases": "/dev/sda /dev/sdb"},
    ) in h.fakes.checks


def test_execute_refusals(runner_harness: tuple[Harness, FakeRunner]) -> None:
    h, runner = runner_harness
    (plan,) = call(h.client, "PlanCreate", GLib.Variant("(as)", (["/dev/sda"],)))
    options = GLib.Variant(
        "(sa{sv})", ("sha256:nope", {"confirm-erase": GLib.Variant("as", ["/dev/sda"])})
    )
    name, _ = error_of(lambda: call(h.client, "Execute", options, plan, PLAN))
    assert name == ERR + "StalePlan"
    name, _ = error_of(lambda: execute(h, plan, ["/dev/sdb"]))
    assert name == ERR + "NotConfirmed"
    name, _ = error_of(
        lambda: execute(h, plan, ["/dev/sda"], **{"no-ups": GLib.Variant("b", False)})
    )
    assert name == ERR + "NoUps"
    h.fakes.denied.add("io.github.theronconrey.tamarack.create")
    name, _ = error_of(lambda: execute(h, plan, ["/dev/sda"]))
    assert name == "org.freedesktop.DBus.Error.AccessDenied"
    assert runner.calls == []


def test_a_swapped_drive_is_caught_at_execute(runner_harness: tuple[Harness, FakeRunner]) -> None:
    h, runner = runner_harness
    (plan,) = call(h.client, "PlanCreate", GLib.Variant("(as)", (["/dev/sda"],)))
    h.fakes.drives["/dev/sda"] = (4 * TB, "0x50000000000000ff")
    name, message = error_of(lambda: execute(h, plan, ["/dev/sda"]))
    assert name == ERR + "DriveChanged" and "/dev/sda" in message
    assert runner.calls == []


def test_pause_resume_and_confirm_backup(bus: str, tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    battery = {"on": False}
    runner = FakeRunner()
    h = Harness(bus, state, runner=runner, on_battery=lambda: battery["on"])
    try:
        (plan,) = call(h.client, "PlanCreate", GLib.Variant("(as)", (["/dev/sda", "/dev/sdb"],)))
        first_step = len(h.service.engine.plan(1).document["steps"][0]["commands"])
        total = sum(len(st["commands"]) for st in h.service.engine.plan(1).document["steps"])

        def cut_power(index: int, argv: tuple[str, ...]) -> bool:
            if index == first_step - 1:
                battery["on"] = True  # the UPS takes over during step 1
            return False

        runner.cut = cut_power
        (job,) = execute(h, plan, ["/dev/sda", "/dev/sdb"])
        h.wait(lambda: get(h.client, "State", job, JOB) == "paused")
        assert "battery" in get(h.client, "Message", job, JOB)
        assert get(h.client, "Step", job, JOB) == 2
        assert get(h.client, "State") == "paused" and get(h.client, "ActiveJob") == job
        assert get(h.client, "CanCancel", job, JOB) is True  # paused between steps
        assert get(h.client, "NeedsCheck", job, JOB) is False
        name, _ = error_of(
            lambda: call(h.client, "Resume", GLib.Variant("(a{sv})", ({},)), job, JOB)
        )
        assert name == ERR + "OnBattery"

        battery["on"] = False
        call(h.client, "Resume", GLib.Variant("(a{sv})", ({},)), job, JOB)
        h.wait(lambda: get(h.client, "State", job, JOB) == "completed")
        assert len(runner.calls) == total  # nothing ran twice

        call(h.client, "ConfirmBackup")
        assert get(h.client, "LastBackupConfirmed") > 0
        assert any(
            a == "io.github.theronconrey.tamarack.confirm-backup" for _, a, _ in h.fakes.checks
        )
    finally:
        h.stop()


def test_cancel_a_paused_job(bus: str, tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    battery = {"on": False}
    runner = FakeRunner(cut=lambda i, a: battery.update(on=True) or False)
    h = Harness(bus, state, runner=runner, on_battery=lambda: battery["on"])
    try:
        (plan,) = call(h.client, "PlanCreate", GLib.Variant("(as)", (["/dev/sda"],)))
        (job,) = execute(h, plan, ["/dev/sda"])
        h.wait(lambda: get(h.client, "State", job, JOB) == "paused")
        call(h.client, "Cancel", None, job, JOB)
        h.wait(lambda: (job, "Finished", (False, "cancelled")) in h.signals)
        assert get(h.client, "State", job, JOB) == "cancelled"
        assert get(h.client, "State") == "idle"
        assert get(h.client, "Layout") == ""
    finally:
        h.stop()


def test_health_alerts_when_a_drive_starts_failing(bus: str, tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    plan = make_plan(
        [4 * TB, 4 * TB],
        ["/dev/sda", "/dev/sdb"],
        ["wwn:0x5000000000000001", "wwn:0x5000000000000002"],
    )
    (state / "layout.json").write_text(layout_from_plan(plan).to_json())
    h = Harness(bus, state)
    try:
        h.service.check_health()  # the baseline: no alerts
        health = json.loads(get(h.client, "Health"))
        assert [d["state"] for d in health["drives"]] == ["ok", "ok"]
        h.fakes.failing.add("/dev/sdb")
        h.service.check_health()
        h.wait(lambda: any(name == "DriveFailing" for _, name, _ in h.signals))
        assert (
            MANAGER_PATH,
            "DriveFailing",
            ("/dev/sdb", "failing: SMART says the drive is failing"),
        ) in h.signals
        # (No md arrays exist on the test machine, so the status itself is
        # "degraded"; what matters here is the drive's own state.)
        assert json.loads(get(h.client, "Health"))["drives"][1]["state"] == "failing"
        h.service.check_health()  # no change: no second alert
        h.wait(lambda: True)
        assert sum(name == "DriveFailing" for _, name, _ in h.signals) == 1
    finally:
        h.stop()


def test_snapshot_schedule_over_the_bus(runner_harness: tuple[Harness, FakeRunner]) -> None:
    h, runner = runner_harness
    assert get(h.client, "SnapshotSchedule") == {
        "hourly": 24,
        "daily": 7,
        "weekly": 4,
        "monthly": 6,
    }
    devices = ["/dev/sda", "/dev/sdb"]
    (plan,) = call(h.client, "PlanCreate", GLib.Variant("(as)", (devices,)))
    (job,) = execute(h, plan, devices)
    h.wait(lambda: get(h.client, "State", job, JOB) == "completed")
    schedule = {"hourly": 6, "daily": 7, "weekly": 0, "monthly": 12}
    call(h.client, "SetSnapshotSchedule", GLib.Variant("(a{su})", (schedule,)))
    assert get(h.client, "SnapshotSchedule") == schedule
    assert runner.calls[-1][:4] == ("snapper", "--config", "tamarack", "set-config")
    assert any(a == "io.github.theronconrey.tamarack.configure" for _, a, _ in h.fakes.checks)
    bad = {"hourly": 6}
    name, _ = error_of(
        lambda: call(h.client, "SetSnapshotSchedule", GLib.Variant("(a{su})", (bad,)))
    )
    assert name == ERR + "InvalidArgument"


def test_shares_over_the_bus(bus: str, tmp_path: Path, chowns: list[tuple[str, int, int]]) -> None:
    state, root = tmp_path / "state", tmp_path / "root"
    state.mkdir()
    (root / "srv" / "tamarack" / "shares").mkdir(parents=True)
    (root / "etc" / "exports.d").mkdir(parents=True)
    (root / "etc" / "nfs.conf.d").mkdir(parents=True)
    (root / "etc" / "nfs.conf.d" / "tamarack.conf").write_text("")  # tmpfiles.d
    plan = make_plan(
        [4 * TB, 4 * TB],
        ["/dev/sda", "/dev/sdb"],
        ["wwn:0x5000000000000001", "wwn:0x5000000000000002"],
    )
    (state / "layout.json").write_text(layout_from_plan(plan).to_json())
    runner = FakeRunner()
    h = Harness(bus, state, runner=runner, root=str(root))
    try:
        assert json.loads(get(h.client, "Shares")) == []
        options = {
            "clients": GLib.Variant("as", ["192.168.1.0/24"]),
            "read-only": GLib.Variant("b", True),
        }
        call(h.client, "SetShare", GLib.Variant("(sa{sv})", ("media", options)))
        (share,) = json.loads(get(h.client, "Shares"))
        assert share["path"] == "/srv/tamarack/shares/media" and share["read_only"]
        assert share["security"][0].startswith("sec=sys trusts the user IDs")
        assert (root / "etc/exports.d/tamarack.exports").read_text().count("media") == 1
        assert any(a == "io.github.theronconrey.tamarack.share" for _, a, _ in h.fakes.checks)
        bad = {"clients": GLib.Variant("as", ["host(rw)"])}
        name, _ = error_of(lambda: call(h.client, "SetShare", GLib.Variant("(sa{sv})", ("x", bad))))
        assert name == ERR + "InvalidArgument"
        call(h.client, "RemoveShare", GLib.Variant("(s)", ("media",)))
        assert json.loads(get(h.client, "Shares")) == []
    finally:
        h.stop()


def test_browse_and_restore_over_the_bus(bus: str, tmp_path: Path) -> None:
    state, root = tmp_path / "state", tmp_path / "root"
    state.mkdir()
    plan = make_plan(
        [4 * TB, 4 * TB],
        ["/dev/sda", "/dev/sdb"],
        ["wwn:0x5000000000000001", "wwn:0x5000000000000002"],
    )
    (state / "layout.json").write_text(layout_from_plan(plan).to_json())
    snap = root / "srv/tamarack/shares/.snapshots/3"
    (snap / "snapshot").mkdir(parents=True)
    (snap / "snapshot" / "notes.txt").write_text("v1")
    (snap / "info.xml").write_text(
        "<snapshot><type>single</type><num>3</num><date>2026-11-20 14:00:00</date>"
        "<description>timeline</description><cleanup>timeline</cleanup></snapshot>"
    )
    h = Harness(bus, state, root=str(root))
    try:
        (listing,) = call(h.client, "BrowseSnapshot", GLib.Variant("(us)", (3, "")))
        assert [e["name"] for e in json.loads(listing)["entries"]] == ["notes.txt"]
        (restored,) = call(h.client, "RestoreFromSnapshot", GLib.Variant("(us)", (3, "notes.txt")))
        assert (root / "srv/tamarack/shares" / restored).read_text() == "v1"
        assert ("io.github.theronconrey.tamarack.restore") in [a for _, a, _ in h.fakes.checks]
        name, _ = error_of(
            lambda: call(h.client, "RestoreFromSnapshot", GLib.Variant("(us)", (3, "gone.txt")))
        )
        assert name == ERR + "InvalidArgument"
        h.fakes.denied.add("io.github.theronconrey.tamarack.restore")
        name, _ = error_of(lambda: call(h.client, "BrowseSnapshot", GLib.Variant("(us)", (3, ""))))
        assert name == "org.freedesktop.DBus.Error.AccessDenied"
    finally:
        h.stop()
