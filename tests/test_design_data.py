# SPDX-License-Identifier: GPL-3.0-or-later
"""Keep the engine's D-Bus, polkit, systemd and module files consistent.

These files are a design draft (docs/design/engine.md) and are not packaged
yet, but they must already agree with each other and with the design.
"""

from __future__ import annotations

import configparser
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
BUS_NAME = "com.borealislogistics.Tamarack1"
API = ROOT / "src" / "tamarack" / f"{BUS_NAME}.xml"
BUS_POLICY = DATA / "dbus" / f"{BUS_NAME}.conf"
ACTIVATION = DATA / "dbus" / f"{BUS_NAME}.service"
POLKIT = DATA / "polkit" / "com.borealislogistics.tamarack.policy"
UNIT = DATA / "systemd" / "tamarack-engine.service"
MODULES = DATA / "modules-load" / "tamarack.conf"
DESIGN = ROOT / "docs" / "design" / "engine.md"
ANNOTATION = f"{BUS_NAME}.PolkitAction"

DESTRUCTIVE = {"create", "add-drive", "replace-drive", "resume"}


def polkit_actions() -> dict[str, dict[str, str]]:
    actions = {}
    for action in ET.parse(POLKIT).getroot().iter("action"):
        defaults = action.find("defaults")
        assert defaults is not None
        actions[action.attrib["id"]] = {child.tag: (child.text or "") for child in defaults}
    return actions


def api_methods() -> dict[str, list[str]]:
    methods = {}
    for iface in ET.parse(API).getroot().iter("interface"):
        for method in iface.iter("method"):
            name = f"{iface.attrib['name']}.{method.attrib['name']}"
            notes = [a for a in method.iter("annotation") if a.attrib["name"] == ANNOTATION]
            methods[name] = notes[0].attrib["value"].split() if notes else []
    return methods


def unit_settings() -> dict[str, list[str]]:
    """systemd units may repeat keys, so parse them by hand."""
    settings: dict[str, list[str]] = {}
    for raw in UNIT.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "[")):
            continue
        key, _, value = line.partition("=")
        settings.setdefault(key, []).append(value)
    return settings


@pytest.mark.parametrize("path", [API, BUS_POLICY, POLKIT])
def test_xml_files_are_well_formed(path: Path) -> None:
    ET.parse(path)


def test_every_method_names_a_polkit_action() -> None:
    for method, actions in api_methods().items():
        assert actions, f"{method} has no polkit action"


def test_api_and_polkit_policy_agree() -> None:
    used = {a for actions in api_methods().values() for a in actions}
    defined = set(polkit_actions())
    assert used <= defined, f"undefined actions: {used - defined}"
    assert defined <= used, f"unused actions: {defined - used}"


def test_destructive_actions_always_ask_for_the_admin_password() -> None:
    for action_id, defaults in polkit_actions().items():
        suffix = action_id.rsplit(".", 1)[1]
        if suffix in DESTRUCTIVE:
            assert defaults == {
                "allow_any": "no",
                "allow_inactive": "no",
                "allow_active": "auth_admin",
            }, action_id


def test_only_read_is_open_to_everyone() -> None:
    open_actions = [a for a, d in polkit_actions().items() if d["allow_active"] == "yes"]
    assert open_actions == ["com.borealislogistics.tamarack.read"]


def test_bus_policy_covers_every_interface_and_only_root_owns() -> None:
    root = ET.parse(BUS_POLICY).getroot()
    allowed = {
        allow.attrib["send_interface"]
        for allow in root.iter("allow")
        if allow.attrib.get("send_destination") == BUS_NAME
    }
    interfaces = {i.attrib["name"] for i in ET.parse(API).getroot().iter("interface")}
    assert interfaces <= allowed
    owners = [p for p in root.iter("policy") if any(a.get("own") for a in p.iter("allow"))]
    assert [p.attrib for p in owners] == [{"user": "root"}]


def test_activation_hands_off_to_the_systemd_unit() -> None:
    parser = configparser.ConfigParser()
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    parser.read(ACTIVATION)
    service = parser["D-BUS Service"]
    assert service["Name"] == BUS_NAME
    assert service["SystemdService"] == UNIT.name
    assert unit_settings()["BusName"] == [BUS_NAME]


@pytest.mark.parametrize(
    "key, value",
    [
        ("Type", "dbus"),
        ("ProtectSystem", "strict"),
        ("ProtectHome", "yes"),
        ("PrivateTmp", "yes"),
        ("PrivateNetwork", "yes"),
        ("NoNewPrivileges", "yes"),
        ("ProtectKernelModules", "yes"),
        ("DevicePolicy", "closed"),
        ("StateDirectory", "tamarack"),
        ("MemoryDenyWriteExecute", "yes"),
        ("RestrictAddressFamilies", "AF_UNIX AF_NETLINK"),
    ],
)
def test_unit_hardening(key: str, value: str) -> None:
    assert unit_settings().get(key) == [value]


def test_every_sandbox_relaxation_is_explained() -> None:
    lines = UNIT.read_text().splitlines()
    for i, line in enumerate(lines):
        if line in ("ProtectKernelTunables=no", "PrivateDevices=no"):
            assert lines[i - 1].startswith("#"), f"{line} needs a comment above it"


def test_capabilities_are_bounded() -> None:
    caps = unit_settings()["CapabilityBoundingSet"][0].split()
    assert "CAP_SYS_MODULE" not in caps
    assert "CAP_NET_ADMIN" not in caps
    assert len(caps) <= 5


def test_modules_needed_by_the_engine_load_at_boot() -> None:
    modules = {
        line.strip()
        for line in MODULES.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert modules == {"dm_integrity", "raid1", "raid456"}


def test_design_doc_lists_every_polkit_action() -> None:
    text = DESIGN.read_text()
    for action_id in polkit_actions():
        suffix = action_id.removeprefix("com.borealislogistics.")
        assert f"`…{suffix}`" in text, f"{action_id} missing from the design doc"


SELINUX = ROOT / "selinux"


def test_selinux_module_labels_what_the_package_installs() -> None:
    contexts = {}
    for line in (SELINUX / "tamarack.fc").read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            fields = line.split()
            contexts[fields[0]] = fields[-1]
    exec_start = unit_settings()["ExecStart"][0]
    assert contexts[exec_start].endswith(":tamarack_engine_exec_t,s0)")
    assert contexts["/var/lib/tamarack(/.*)?"].endswith(":tamarack_var_lib_t,s0)")
    assert contexts["/etc/tamarack(/.*)?"].endswith(":tamarack_etc_t,s0)")
    assert unit_settings()["StateDirectory"] == ["tamarack"]


def test_selinux_policy_is_enforcing_and_named() -> None:
    te = (SELINUX / "tamarack.te").read_text()
    code = [line for line in te.splitlines() if not line.lstrip().startswith("#")]
    assert "policy_module(tamarack," in "\n".join(code)
    assert not any("permissive" in line for line in code)
    # Raw-disk access goes to tamarack-meta alone, never to the engine.
    assert "sys_rawio" not in te
    raw = [line for line in code if "fixed_disk" in line]
    assert raw and all("(tamarack_meta_t)" in line for line in raw)


def test_mount_unit_matches_what_the_planner_builds() -> None:
    from tamarack.commands import LV_NAME, MOUNT_OPTIONS, MOUNT_POINT, MOUNT_UNIT, VG_NAME

    unit = DATA / "systemd" / MOUNT_UNIT
    parser = configparser.ConfigParser()
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    parser.read(unit)
    mount = parser["Mount"]
    assert mount["What"] == f"/dev/{VG_NAME}/{LV_NAME}"
    assert mount["Where"] == MOUNT_POINT
    assert mount["Type"] == "btrfs"
    assert mount["Options"] == MOUNT_OPTIONS
    # systemd requires a mount unit to be named after its mount point.
    assert MOUNT_POINT.strip("/").replace("/", "-") + ".mount" == MOUNT_UNIT


def test_scrub_runs_monthly_on_the_volume_and_create_enables_it() -> None:
    from tamarack.commands import MOUNT_POINT, MOUNT_UNIT, SCRUB_TIMER, build_steps
    from tamarack.planner import make_plan

    def read(name: str) -> configparser.ConfigParser:
        parser = configparser.ConfigParser(strict=False)
        parser.optionxform = str  # type: ignore[assignment,method-assign]
        parser.read(DATA / "systemd" / name)
        return parser

    service, timer = read("tamarack-scrub.service"), read("tamarack-scrub.timer")
    assert service["Service"]["ExecStart"].endswith(f"scrub start -B -d {MOUNT_POINT}")
    assert service["Unit"]["Requires"] == MOUNT_UNIT
    assert service["Unit"]["ConditionPathIsMountPoint"] == MOUNT_POINT
    assert timer["Timer"]["OnCalendar"] == "monthly"
    assert timer["Timer"]["Persistent"] == "true"
    argvs = [c.argv for step in build_steps(make_plan([4 * 10**12])) for c in step.commands]
    assert ("systemctl", "enable", "--now", MOUNT_UNIT, SCRUB_TIMER) in argvs
