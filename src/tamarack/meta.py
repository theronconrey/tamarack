# SPDX-License-Identifier: GPL-3.0-or-later
"""The layout record on every drive's ``tk-meta`` partition.

Partition 1 of every Tamarack drive is a 16 MiB ``tk-meta`` partition. It
holds the layout file, so a box can be understood (and Tamarack can carry
on) from its drives alone. The record is framed so a partial or stale write
is never mistaken for a layout::

    TAMARACK-META 1 <length> sha256:<hex>\\n
    <length bytes of layout JSON>
    <zero padding to a 4 KiB boundary>

``tamarack-meta`` (installed in /usr/libexec) is the only program that
writes it. The engine runs it as a plan step, with the layout as an
argument, so the plan digest covers exactly what is written. It refuses any
device that is not a partition labelled ``tk-meta``, and has its own SELinux
domain: raw-disk writes are granted to it, not to the engine.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Sequence

from .commands import META_PARTLABEL
from .layout import Layout

MAGIC = b"TAMARACK-META"
VERSION = 1
BLOCK = 4096
MAX_RECORD = 1 << 20  # far more than any layout needs; the partition is 16 MiB


class MetaError(ValueError):
    pass


def compact(layout: Layout) -> str:
    """The layout as one line of canonical JSON (a plan argument)."""
    return json.dumps(layout.to_dict(), sort_keys=True, separators=(",", ":"))


def encode(layout_json: str) -> bytes:
    body = layout_json.encode("utf-8")
    header = b"%s %d %d sha256:%s\n" % (
        MAGIC,
        VERSION,
        len(body),
        hashlib.sha256(body).hexdigest().encode(),
    )
    record = header + body
    if len(record) > MAX_RECORD:
        raise MetaError("layout too large for the tk-meta partition")
    return record + b"\0" * (-len(record) % BLOCK)


def decode(data: bytes) -> str:
    header, sep, rest = data.partition(b"\n")
    fields = header.split(b" ")
    if not sep or len(fields) != 4 or fields[0] != MAGIC:
        raise MetaError("no Tamarack layout record here")
    if fields[1] != str(VERSION).encode():
        raise MetaError(f"unsupported record version {fields[1]!r}")
    try:
        length = int(fields[2])
    except ValueError:
        raise MetaError("damaged record header") from None
    body = rest[:length]
    if len(body) != length or b"sha256:" + hashlib.sha256(body).hexdigest().encode() != fields[3]:
        raise MetaError("the layout record is damaged (checksum mismatch)")
    return body.decode("utf-8")


def partition_label(device: str, sys_root: str = "/") -> str:
    """GPT partition name of ``device``: the kernel's PARTNAME, else udev's.

    The kernel only knows a partition's name when it read the table itself;
    partitions that parted adds one by one (BLKPG) have no PARTNAME, so the
    name then comes from udev's database (ID_PART_ENTRY_NAME, from blkid).
    """
    name = os.path.basename(os.path.realpath(device))
    uevent = os.path.join(sys_root, "sys", "class", "block", name, "uevent")
    fields: dict[str, str] = {}
    try:
        with open(uevent, encoding="utf-8") as f:
            for line in f:
                key, _, value = line.rstrip("\n").partition("=")
                fields[key] = value
    except OSError:
        return ""
    if fields.get("PARTNAME"):
        return fields["PARTNAME"]
    if not (fields.get("MAJOR", "").isdigit() and fields.get("MINOR", "").isdigit()):
        return ""
    db = os.path.join(sys_root, "run", "udev", "data", f"b{fields['MAJOR']}:{fields['MINOR']}")
    try:
        with open(db, encoding="utf-8") as f:
            for line in f:
                if line.startswith("E:ID_PART_ENTRY_NAME="):
                    return line.rstrip("\n").partition("=")[2]
    except OSError:
        pass
    return ""


def write(device: str, layout_json: str, sys_root: str = "/") -> None:
    if partition_label(device, sys_root) != META_PARTLABEL:
        raise MetaError(f"{device} is not a {META_PARTLABEL} partition")
    Layout.from_json(layout_json)  # refuse anything that is not a valid layout
    record = encode(layout_json)
    fd = os.open(device, os.O_WRONLY | os.O_CLOEXEC)
    try:
        view = memoryview(record)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    finally:
        os.close(fd)


def read(device: str) -> str:
    with open(device, "rb") as f:
        return decode(f.read(MAX_RECORD + BLOCK))


def main(argv: Sequence[str] | None = None, sys_root: str = "/") -> int:
    parser = argparse.ArgumentParser(
        prog="tamarack-meta", description="Read or write the layout on a tk-meta partition."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    w = sub.add_parser("write")
    w.add_argument("device")
    w.add_argument("layout", help="the layout as JSON")
    r = sub.add_parser("read")
    r.add_argument("device")
    args = parser.parse_args(argv)
    from .engine import EngineError, validate_device_path

    try:
        validate_device_path(args.device)
        if args.command == "write":
            write(args.device, args.layout, sys_root)
        else:
            print(read(args.device))
    except (EngineError, MetaError, ValueError, OSError) as exc:
        print(f"tamarack-meta: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
