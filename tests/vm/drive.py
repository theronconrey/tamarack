# SPDX-License-Identifier: GPL-3.0-or-later
"""Drive the Tamarack engine over the system bus, as a client would.

For the VM tests only. Refuses any drive that is not a scsi_debug RAM disk,
so it cannot erase a real drive even if run on the wrong machine.

    drive.py create DEV...      drive.py add DEV
    drive.py replace OLD NEW    drive.py rebuild FAILED NEW
    drive.py confirm-backup     drive.py tolerated     drive.py degraded
    drive.py start-add DEV      drive.py wait-for JOB STATE
    drive.py resume JOB         drive.py state JOB
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

BUS_NAME = "com.borealislogistics.Tamarack1"
ROOT = "/com/borealislogistics/Tamarack1"
MANAGER = f"{BUS_NAME}.Manager"
PLAN = f"{BUS_NAME}.Plan"
JOB = f"{BUS_NAME}.Job"
JOB_TIMEOUT = 20 * 60

bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)


def call(path: str, iface: str, method: str, args: Any = None) -> Any:
    reply = bus.call_sync(BUS_NAME, path, iface, method, args, None, 0, 120_000, None)
    return reply.unpack() if reply is not None else ()


def get(path: str, iface: str, name: str) -> Any:
    args = GLib.Variant("(ss)", (iface, name))
    return call(path, "org.freedesktop.DBus.Properties", "Get", args)[0]


def only_fake(device: str) -> None:
    name = os.path.basename(os.path.realpath(device))
    with open(f"/sys/block/{name}/device/model") as f:
        if f.read().strip() != "scsi_debug":
            sys.exit(f"drive.py: {device} is not a scsi_debug test drive; refusing")


def run(method: str, signature: str, *args: Any, wait: bool = True) -> str:
    (plan,) = call(ROOT, MANAGER, method, GLib.Variant(signature, args))
    erases = get(plan, PLAN, "Erases")
    for device in erases:
        only_fake(device)
    digest = get(plan, PLAN, "Digest")
    print(f"{method}: plan {plan}, erasing {' '.join(erases)}")
    options = {"confirm-erase": GLib.Variant("as", erases), "no-ups": GLib.Variant("b", True)}
    (job,) = call(plan, PLAN, "Execute", GLib.Variant("(sa{sv})", (digest, options)))
    if wait:
        follow(job)
    return str(job)


def follow(job: str, until: tuple[str, ...] = ("completed",)) -> None:
    """Print progress until the job stops; exit unless it ends in ``until``."""
    deadline = time.monotonic() + JOB_TIMEOUT
    last = None
    while True:
        state, step, title = (get(job, JOB, n) for n in ("State", "Step", "StepTitle"))
        if (state, step) != last:
            print(f"  {state}: step {step}, {title}")
            last = (state, step)
        if state in ("completed", "failed", "cancelled", "paused", *until):
            break
        if time.monotonic() > deadline:
            sys.exit(f"drive.py: job {job} timed out")
        time.sleep(1)
    if state not in until:
        sys.exit(f"drive.py: job {job} {state}: {get(job, JOB, 'Message')}")


def main(argv: list[str]) -> None:
    command, args = argv[0], argv[1:]
    if command == "create":
        run("PlanCreate", "(as)", args)
    elif command == "add":
        run("PlanAdd", "(s)", *args)
    elif command == "start-add":  # print the job and return at once
        print(run("PlanAdd", "(s)", *args, wait=False))
    elif command == "wait-for":  # JOB STATE: until the job reaches STATE
        follow(args[0], until=(args[1],))
    elif command == "resume":
        call(args[0], JOB, "Resume", GLib.Variant("(a{sv})", ({},)))
        follow(args[0])
    elif command == "state":
        print(get(args[0], JOB, "State"))
    elif command == "replace":
        run("PlanReplace", "(ss)", *args)
    elif command == "rebuild":
        run("PlanRebuild", "(ss)", *args)
    elif command == "confirm-backup":
        call(ROOT, MANAGER, "ConfirmBackup")
    elif command == "tolerated":
        print(get(ROOT, MANAGER, "DriveFailuresTolerated"))
    elif command == "degraded":
        print(get(ROOT, MANAGER, "Degraded"))
    else:
        sys.exit(f"drive.py: unknown command {command}")


if __name__ == "__main__":
    main(sys.argv[1:])
