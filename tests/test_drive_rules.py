# SPDX-License-Identifier: GPL-3.0-or-later
"""Which drives may join the pool, and finding the drives the system is on."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from tamarack.system import system_disks, system_sources
from tamarack.udisks import classify_drives

TB = 10**12
B = "org.freedesktop.UDisks2.Block"
D = "org.freedesktop.UDisks2.Drive"
P = "org.freedesktop.UDisks2.Partition"
F = "org.freedesktop.UDisks2.Filesystem"
ATA = "org.freedesktop.UDisks2.Drive.Ata"


def drive(
    objects: dict[str, Any],
    name: str,
    *,
    bus: str = "sata",
    model: str = "WDC WD80EFZZ",
    serial: str | None = None,
    smart: bool = True,
    failing: bool = False,
    hours: int = 1000,
    number: int | None = None,
    **block: Any,
) -> str:
    """Add a whole drive /dev/NAME to ``objects``; return its block object path."""
    drive_path, block_path = f"/drives/{name}", f"/block/{name}"
    objects[drive_path] = {
        D: {
            "Model": model,
            "Serial": serial if serial is not None else f"SN-{name}",
            "WWN": "",
            "Size": 8 * TB,
            "ConnectionBus": bus,
            "RotationRate": 5400,
        }
    }
    if smart:
        objects[drive_path][ATA] = {
            "SmartSupported": True,
            "SmartEnabled": True,
            "SmartFailing": failing,
            "SmartPowerOnSeconds": hours * 3600,
        }
    objects[block_path] = {
        B: {"Device": f"/dev/{name}\0".encode(), "Drive": drive_path, "Size": 8 * TB, **block}
    }
    if number is not None:
        objects[block_path][B]["DeviceNumber"] = number
    return block_path


def partition(objects: dict[str, Any], table: str, name: str, **extra: Any) -> None:
    block = extra.pop("block", {})
    objects[f"/block/{name}"] = {
        B: {"Device": f"/dev/{name}\0".encode(), "Drive": "/", "Size": 2 * TB, **block},
        P: {"Table": table, "Number": 1, **extra},
    }


def verdicts(objects: dict[str, Any], **kw: Any) -> dict[str, dict[str, Any]]:
    return classify_drives(objects, **kw)


def test_a_blank_healthy_internal_drive_is_suggested() -> None:
    objects: dict[str, Any] = {}
    drive(objects, "sda")
    v = verdicts(objects)["/dev/sda"]
    assert v["verdict"] == "suggest" and v["reasons"] == [] and v["warnings"] == []
    assert v["model"] == "WDC WD80EFZZ" and v["identity"] == "serial:SN-sda"


def test_refused_drives_say_why() -> None:
    objects: dict[str, Any] = {}
    drive(objects, "sdb", bus="usb")
    drive(objects, "mmcblk0", bus="sdio")
    drive(objects, "sdc", model="LOGICAL VOLUME")
    drive(objects, "sdd", failing=True)
    drive(objects, "sde", IdType="isw_raid_member", IdUsage="raid")
    drive(objects, "sdf", serial="SAME")
    drive(objects, "sdg", serial="SAME")
    drive(objects, "sdh", serial="")
    pool = drive(objects, "sdi")
    partition(objects, pool, "sdi1", Name="tk-meta")
    busy = drive(objects, "sdj")
    partition(objects, busy, "sdj1", block={}, **{})
    objects["/block/sdj1"][F] = {"MountPoints": [b"/mnt/x\0"]}
    v = verdicts(objects)
    why = {d: " ".join(x["reasons"]) for d, x in v.items()}
    assert all(v[d]["verdict"] == "refuse" for d in why if d != "/dev/sda"), v
    assert "USB" in why["/dev/sdb"]
    assert "SD card" in why["/dev/mmcblk0"]
    assert "RAID controller" in why["/dev/sdc"]
    assert "failing" in why["/dev/sdd"]
    assert "Intel firmware RAID" in why["/dev/sde"]
    assert "same identity" in why["/dev/sdf"] and "same identity" in why["/dev/sdg"]
    assert "neither a WWN nor a serial" in why["/dev/sdh"]
    assert "Tamarack pool" in why["/dev/sdi"]
    assert "mounted" in why["/dev/sdj"]


def test_the_system_drive_is_refused_by_device_number() -> None:
    objects: dict[str, Any] = {}
    drive(objects, "nvme0n1", number=os.makedev(259, 0))
    drive(objects, "sda", number=os.makedev(8, 0))
    v = verdicts(objects, system={os.makedev(259, 0)})
    assert v["/dev/nvme0n1"]["reasons"] == ["the operating system is on it"]
    assert v["/dev/sda"]["verdict"] == "suggest"


def test_warnings_describe_what_would_be_lost() -> None:
    objects: dict[str, Any] = {}
    old = drive(objects, "sda", hours=7 * 8766)
    partition(
        objects, old, "sda1", block={"IdType": "ntfs", "IdUsage": "filesystem", "IdLabel": "Photos"}
    )
    drive(objects, "sdb", smart=False)
    v = verdicts(objects)
    assert v["/dev/sda"]["verdict"] == "warn"
    assert v["/dev/sda"]["warnings"] == [
        "/dev/sda has run for about 7 years",
        "/dev/sda holds a 2.00 TB NTFS partition 'Photos'; joining the pool erases it",
    ]
    assert v["/dev/sdb"]["warnings"] == ["/dev/sdb reports no health (SMART) data"]


def test_sysfs_refusals(tmp_path: Path) -> None:
    objects: dict[str, Any] = {}
    drive(objects, "sda")
    drive(objects, "sdb")
    for name, zoned, sector in (("sda", "host-managed", "4096"), ("sdb", "none", "520")):
        queue = tmp_path / "sys/class/block" / name / "queue"
        queue.mkdir(parents=True)
        (queue / "zoned").write_text(zoned + "\n")
        (queue / "logical_block_size").write_text(sector + "\n")
    v = verdicts(objects, sysfs_root=str(tmp_path))
    assert "host-managed SMR" in v["/dev/sda"]["reasons"][0]
    assert "520 bytes" in v["/dev/sdb"]["reasons"][0]


# --- the drives the system is on ---------------------------------------------


def block(root: Path, name: str, dev: str, parent: str | None = None) -> Path:
    """A /sys/class/block entry; partitions live under their disk, as in sysfs."""
    devices = root / "sys/devices/block"
    node = (devices / parent / name) if parent else (devices / name)
    node.mkdir(parents=True)
    (node / "dev").write_text(dev + "\n")
    if parent:
        (node / "partition").write_text("1\n")
    link = root / "sys/class/block" / name
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(node)
    (root / "dev").mkdir(exist_ok=True)
    (root / "dev" / name).touch()
    return node


def test_system_disks_follow_lvm_luks_and_btrfs_to_whole_drives(tmp_path: Path) -> None:
    root = tmp_path
    block(root, "nvme0n1", "259:0")
    block(root, "nvme0n1p2", "259:2", parent="nvme0n1")
    block(root, "sda", "8:0")
    block(root, "sda1", "8:1", parent="sda")
    block(root, "sdb", "8:16")  # the pool's drive: never named
    # / is btrfs on dm-0 (LUKS on nvme0n1p2); the same btrfs also spans sda1.
    dm = block(root, "dm-0", "253:0")
    (dm / "slaves").mkdir()
    (dm / "slaves" / "nvme0n1p2").touch()
    (root / "dev/mapper").mkdir()
    (root / "dev/mapper/luks-root").symlink_to("../dm-0")
    btrfs = root / "sys/fs/btrfs/fsid-1/devices"
    btrfs.mkdir(parents=True)
    (btrfs / "dm-0").touch()
    (btrfs / "sda1").touch()
    (root / "proc/self").mkdir(parents=True)
    (root / "proc/self/mountinfo").write_text(
        "21 1 0:31 / / rw,relatime - btrfs /dev/mapper/luks-root rw\n"
        "22 21 0:32 / /srv/tamarack rw - btrfs /dev/mapper/tamarack-data rw\n"
        "23 21 0:33 / /mnt/my\\040disk rw - ext4 /dev/sdb rw\n"
    )
    (root / "proc/swaps").write_text("Filename Type Size Used Priority\n")
    assert ("btrfs", "/dev/mapper/luks-root") in system_sources(str(root))
    assert system_disks(str(root)) == {os.makedev(259, 0), os.makedev(8, 0)}
