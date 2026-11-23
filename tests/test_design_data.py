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
API = DATA / "dbus" / f"{BUS_NAME}.xml"
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
