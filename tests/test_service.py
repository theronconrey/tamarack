# SPDX-License-Identifier: GPL-3.0-or-later
"""The engine on a private D-Bus daemon, with a fake UDisks2 beside it.

Skipped when PyGObject or dbus-daemon is missing; Fedora CI and the RPM
build install both.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

if os.environ.get("TAMARACK_REQUIRE_DBUS_TESTS"):
    import gi
else:
    gi = pytest.importorskip("gi", exc_type=ImportError)
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from tamarack.engine import Engine  # noqa: E402
from tamarack.layout import layout_from_plan  # noqa: E402
from tamarack.planner import make_plan  # noqa: E402
from tamarack.review import digest, document  # noqa: E402
from tamarack.service import (  # noqa: E402
    BUS_NAME,
    MANAGER,
    MANAGER_PATH,
    PLAN,
    NameTaken,
    Service,
    interface_xml,
)
from tamarack.udisks import UDisksDrives  # noqa: E402

TB = 10**12
ERR = "com.borealislogistics.Tamarack1.Error."

if shutil.which("dbus-daemon") is None and not os.environ.get("TAMARACK_REQUIRE_DBUS_TESTS"):
    pytest.skip("dbus-daemon is not installed", allow_module_level=True)

BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:dir={dir}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""


@pytest.fixture
def bus(tmp_path: Path) -> Iterator[str]:
    config = tmp_path / "bus.conf"
    config.write_text(BUS_CONFIG.format(dir=tmp_path))
    daemon = subprocess.Popen(
        ["dbus-daemon", f"--config-file={config}", "--nofork", "--print-address=1"],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert daemon.stdout is not None
    address = daemon.stdout.readline().strip()
    yield address
    daemon.terminate()
    daemon.wait(timeout=10)


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


# --- a fake UDisks2 ----------------------------------------------------------

UDISKS_XML = """<node><interface name="org.freedesktop.DBus.ObjectManager">
<method name="GetManagedObjects"><arg type="a{oa{sa{sv}}}" direction="out"/></method>
</interface></node>"""

B, D = "org.freedesktop.UDisks2.Block", "org.freedesktop.UDisks2.Drive"


def fake_objects(drives: dict[str, tuple[int, str]]) -> dict[str, Any]:
    objects: dict[str, Any] = {}
    for n, (device, (size, wwn)) in enumerate(sorted(drives.items())):
        drive = f"/org/freedesktop/UDisks2/drives/d{n}"
        objects[drive] = {D: {"WWN": GLib.Variant("s", wwn), "Serial": GLib.Variant("s", "")}}
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


def serve_udisks(address: str, drives: dict[str, tuple[int, str]]) -> Any:
    connection = connect(address)
    info = Gio.DBusNodeInfo.new_for_xml(UDISKS_XML).interfaces[0]

    def call(
        conn: Any, sender: str, path: str, iface: str, method: str, params: Any, inv: Any
    ) -> None:
        inv.return_value(GLib.Variant("(a{oa{sa{sv}}})", (fake_objects(drives),)))

    connection.register_object("/org/freedesktop/UDisks2", info, call, None, None)
    reply = connection.call_sync(
        "org.freedesktop.DBus",
        "/org/freedesktop/DBus",
        "org.freedesktop.DBus",
        "RequestName",
        GLib.Variant("(su)", ("org.freedesktop.UDisks2", 4)),
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


@pytest.fixture
def engine_bus(bus: str, tmp_path: Path) -> Iterator[tuple[Any, Path]]:
    udisks = Loop(lambda: serve_udisks(bus, DRIVES))
    state = tmp_path / "state"
    state.mkdir()

    def setup() -> Service:
        connection = connect(bus)
        engine = Engine(str(state), UDisksDrives(connection, timeout_ms=5000))
        service = Service(engine, connection)
        service.request_name()
        return service

    engine = Loop(setup)
    client = connect(bus)
    yield client, state
    engine.stop()
    udisks.stop()


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


# --- tests -------------------------------------------------------------------


def test_manager_properties(engine_bus: tuple[Any, Path]) -> None:
    client, _ = engine_bus
    assert get(client, "Version") == "0.1.0"
    assert get(client, "State") == "idle"
    assert get(client, "Layout") == ""
    assert get(client, "ActiveJob") == "/"
    assert get(client, "Degraded") is False
    assert get(client, "LastBackupConfirmed") == 0


def test_introspection_matches_the_shipped_xml(engine_bus: tuple[Any, Path]) -> None:
    client, _ = engine_bus
    xml = call(client, "Introspect", None, iface="org.freedesktop.DBus.Introspectable")[0]
    served = Gio.DBusNodeInfo.new_for_xml(xml).lookup_interface(MANAGER)
    shipped = Gio.DBusNodeInfo.new_for_xml(interface_xml()).lookup_interface(MANAGER)
    assert [m.name for m in served.methods] == [m.name for m in shipped.methods]
    assert [p.name for p in served.properties] == [p.name for p in shipped.properties]


def test_create_plan_over_the_bus(engine_bus: tuple[Any, Path]) -> None:
    client, _ = engine_bus
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
    assert steps[0][0] == "Partition each drive" and steps[0][2][0] == "sgdisk --zap-all /dev/sda"
    assert json.loads(get(client, "Summary", path, PLAN))["kind"] == "create"
    assert get(client, "ExpiresAt", path, PLAN) > 0


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


def test_execute_and_confirm_backup_are_not_enabled(engine_bus: tuple[Any, Path]) -> None:
    client, _ = engine_bus
    (path,) = call(client, "PlanCreate", GLib.Variant("(as)", (["/dev/sda"],)))
    digest_ = get(client, "Digest", path, PLAN)
    name, _ = error_of(
        lambda: call(client, "Execute", GLib.Variant("(sa{sv})", (digest_, {})), path, PLAN)
    )
    assert name == ERR + "NotSupported"
    name, _ = error_of(lambda: call(client, "ConfirmBackup"))
    assert name == ERR + "NotSupported"


def test_discard_removes_the_plan_object(engine_bus: tuple[Any, Path]) -> None:
    client, _ = engine_bus
    (path,) = call(client, "PlanCreate", GLib.Variant("(as)", (["/dev/sda"],)))
    call(client, "Discard", None, path, PLAN)
    name, _ = error_of(lambda: get(client, "Kind", path, PLAN))
    assert name.startswith("org.freedesktop.DBus.Error.")


def test_add_plan_with_a_layout(bus: str, tmp_path: Path) -> None:
    udisks = Loop(lambda: serve_udisks(bus, DRIVES))
    state = tmp_path / "state"
    state.mkdir()
    plan = make_plan(
        [4 * TB, 4 * TB],
        ["/dev/sda", "/dev/sdb"],
        ["wwn:0x5000000000000001", "wwn:0x5000000000000002"],
    )
    (state / "layout.json").write_text(layout_from_plan(plan).to_json())

    def setup() -> Service:
        connection = connect(bus)
        service = Service(Engine(str(state), UDisksDrives(connection)), connection)
        service.request_name()
        return service

    engine = Loop(setup)
    try:
        client = connect(bus)
        assert get(client, "DriveFailuresTolerated") == 1
        (path,) = call(client, "PlanAdd", GLib.Variant("(s)", ("/dev/sdc",)))
        assert get(client, "Kind", path, PLAN) == "add"
        assert get(client, "Erases", path, PLAN) == ["/dev/sdc"]
        name, _ = error_of(
            lambda: call(client, "PlanCreate", GLib.Variant("(as)", (["/dev/sdc"],)))
        )
        assert name == ERR + "LayoutExists"
    finally:
        engine.stop()
        udisks.stop()


def test_a_second_engine_refuses_to_start(engine_bus: tuple[Any, Path], bus: str) -> None:
    connection = connect(bus)
    service = Service(Engine("/nonexistent", UDisksDrives(connection)), connection)
    with pytest.raises(NameTaken):
        service.request_name()
    service.close()
