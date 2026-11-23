# SPDX-License-Identifier: GPL-3.0-or-later
"""``tamarack engine …``: drive the storage engine from a terminal.

A thin D-Bus client: everything is decided and checked by the engine, and
polkit authorizes every change. The client only shows the plan, asks the
person to type the drives it erases, and follows the job.

PyGObject is imported only here, when an ``engine`` command runs, so the
planning commands keep needing nothing but Python.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any, TextIO

BUS_NAME = "com.borealislogistics.Tamarack1"
ROOT = "/com/borealislogistics/Tamarack1"
MANAGER = f"{BUS_NAME}.Manager"
PLAN = f"{BUS_NAME}.Plan"
JOB = f"{BUS_NAME}.Job"
ENDED = ("completed", "failed", "cancelled")


class ClientError(Exception):
    """A refusal or failure to show the person, without a traceback."""


def _gi() -> tuple[Any, Any]:
    try:
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
    except (ImportError, ValueError) as exc:
        raise ClientError(
            "talking to the engine needs PyGObject (dnf install python3-gobject-base)"
        ) from exc
    return Gio, GLib


class Engine:
    """The engine's Manager, Plan and Job objects over one connection."""

    def __init__(self, connection: Any = None, timeout_ms: int = 300_000) -> None:
        self.Gio, self.GLib = _gi()
        self.connection = connection or self.Gio.bus_get_sync(self.Gio.BusType.SYSTEM, None)
        # Long enough for an admin to type a password in a polkit prompt.
        self.timeout_ms = timeout_ms

    def call(self, path: str, iface: str, method: str, signature: str = "", *args: Any) -> Any:
        params = self.GLib.Variant(signature, args) if signature else None
        flags = self.Gio.DBusCallFlags.ALLOW_INTERACTIVE_AUTHORIZATION
        try:
            reply = self.connection.call_sync(
                BUS_NAME, path, iface, method, params, None, flags, self.timeout_ms, None
            )
        except self.GLib.Error as exc:
            raise ClientError(_message(self.Gio, exc)) from None
        return reply.unpack() if reply is not None else ()

    def get(self, path: str, iface: str, name: str) -> Any:
        return self.call(path, "org.freedesktop.DBus.Properties", "Get", "(ss)", iface, name)[0]

    def all(self, path: str, iface: str) -> dict[str, Any]:
        (values,) = self.call(path, "org.freedesktop.DBus.Properties", "GetAll", "(s)", iface)
        return dict(values)

    def variant(self, signature: str, value: Any) -> Any:
        return self.GLib.Variant(signature, value)


def _message(gio: Any, exc: Any) -> str:
    name = gio.DBusError.get_remote_error(exc) or ""
    text = str(exc.message)
    prefix = f"GDBus.Error:{name}: "
    if text.startswith(prefix):
        text = text[len(prefix) :]
    short = name.rsplit(".", 1)[-1]
    if name == "org.freedesktop.DBus.Error.ServiceUnknown":
        return "the engine is not installed or not running (dnf install tamarack-engine)"
    if name == "org.freedesktop.DBus.Error.AccessDenied":
        return f"not authorized: {text}"
    return f"{short}: {text}" if short else text


# --- showing things ----------------------------------------------------------


def _size(n: int) -> str:
    from .units import format_size

    return format_size(n)


def show_status(engine: Engine, out: TextIO) -> None:
    m = engine.all(ROOT, MANAGER)
    print(f"Engine {m['Version']}: {m['State']}", file=out)
    if m["Layout"]:
        layout = json.loads(m["Layout"])
        failures = m["DriveFailuresTolerated"]
        print(
            f"Pool: {len(layout['drives'])} drives, {_size(m['UsableBytes'])} usable, "
            f"survives {failures} drive failure{'s' if failures != 1 else ''}",
            file=out,
        )
        for d in layout["drives"]:
            print(f"  {d['device']:<40} {_size(d['size']):>10}", file=out)
    else:
        print("Pool: none yet", file=out)
    if m["Degraded"]:
        print("WARNING: an array is degraded; replace the failed drive", file=out)
    if m["OnBattery"]:
        print("WARNING: the UPS is on battery", file=out)
    when = m["LastBackupConfirmed"]
    backup = time.strftime("%Y-%m-%d %H:%M", time.localtime(when)) if when else "never"
    print(f"Backup last confirmed: {backup}", file=out)
    if m["ActiveJob"] != "/":
        show_job(engine, m["ActiveJob"], out)


def show_plan(engine: Engine, plan: str, out: TextIO) -> dict[str, Any]:
    p = engine.all(plan, PLAN)
    summary = json.loads(p["Summary"])
    before, after = summary["before"], summary["after"]
    if before:
        print(
            f"Before: {_size(before['usable_bytes_nominal'])} usable, "
            f"survives {before['drive_failures_tolerated']} drive failure(s)",
            file=out,
        )
    print(
        f"After:  {_size(after['usable_bytes_nominal'])} usable, "
        f"survives {after['drive_failures_tolerated']} drive failure(s)",
        file=out,
    )
    print("\nSteps:", file=out)
    for number, (title, _note, commands) in enumerate(p["Steps"], start=1):
        print(f"  {number}. {title} ({len(commands)} commands)", file=out)
    for warning in p["Warnings"]:
        print(f"\nWarning: {warning}", file=out)
    print(f"\nPlan digest: {p['Digest']}", file=out)
    return p


def show_commands(plan_props: dict[str, Any], out: TextIO) -> None:
    for number, (title, note, commands) in enumerate(plan_props["Steps"], start=1):
        print(f"\n# {number}. {title}", file=out)
        if note:
            print(f"#    {note}", file=out)
        for line in commands:
            print(line, file=out)


def show_job(engine: Engine, job: str, out: TextIO) -> dict[str, Any]:
    j = engine.all(job, JOB)
    print(
        f"Job {job.rsplit('/', 1)[1]}: {j['State']}, step {j['Step']} of {j['StepCount']}"
        f" ({j['StepTitle']})",
        file=out,
    )
    if j["Message"]:
        print(f"  {j['Message']}", file=out)
    return j


# --- doing things ------------------------------------------------------------


def confirm(erases: list[str], ask: Callable[[str], str], out: TextIO) -> list[str]:
    """The person types every drive to be erased; anything else stops here."""
    print("\nThese drives will be ERASED:", file=out)
    for device in erases:
        print(f"  {device}", file=out)
    typed = ask("Type them, separated by spaces, to go ahead: ").split()
    if sorted(typed) != sorted(erases):
        raise ClientError("not confirmed: nothing was changed")
    return typed


def follow(
    engine: Engine,
    job: str,
    out: TextIO,
    poll: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    last: tuple[Any, ...] = ()
    while True:
        j = engine.all(job, JOB)
        now = (j["State"], j["Step"])
        if now != last:
            print(f"  [{j['State']}] step {j['Step']}/{j['StepCount']}: {j['StepTitle']}", file=out)
            last = now
        if j["State"] in ENDED or j["State"] == "paused":
            if j["Message"]:
                print(f"  {j['Message']}", file=out)
            state: str = j["State"]
            return state
        sleep(poll)


def change(
    engine: Engine,
    method: str,
    signature: str,
    args: tuple[Any, ...],
    options: argparse.Namespace,
    ask: Callable[[str], str],
    out: TextIO,
) -> int:
    (plan,) = engine.call(ROOT, MANAGER, method, signature, *args)
    props = show_plan(engine, plan, out)
    if options.commands:
        show_commands(props, out)
    if options.dry_run:
        engine.call(plan, PLAN, "Discard")
        print("\nDry run: the plan was discarded.", file=out)
        return 0
    try:
        typed = confirm(list(props["Erases"]), ask, out)
    except ClientError:
        engine.call(plan, PLAN, "Discard")
        raise
    execute_options = {"confirm-erase": engine.variant("as", typed)}
    if options.no_ups:
        execute_options["no-ups"] = engine.variant("b", True)
    with polkit_agent():
        (job,) = engine.call(plan, PLAN, "Execute", "(sa{sv})", props["Digest"], execute_options)
    print(f"\nStarted job {job.rsplit('/', 1)[1]}.", file=out)
    if options.no_wait:
        return 0
    return 0 if follow(engine, job, out) == "completed" else 1


class polkit_agent:
    """Like systemctl: run a text polkit agent while we may need a password."""

    def __enter__(self) -> polkit_agent:
        self.process: subprocess.Popen[bytes] | None = None
        agent = shutil.which("pkttyagent")
        if agent and sys.stdin.isatty() and os.geteuid() != 0:
            self.process = subprocess.Popen(
                [agent, "--process", str(os.getpid()), "--fallback"], close_fds=True
            )
            time.sleep(0.2)  # let it register before polkit looks for an agent
        return self

    def __exit__(self, *exc: object) -> None:
        if self.process is not None:
            self.process.terminate()
            self.process.wait(timeout=5)


def job_path(job: str) -> str:
    return job if job.startswith("/") else f"{ROOT}/Jobs/{job}"


def add_parser(sub: Any) -> None:
    engine = sub.add_parser("engine", help="drive the storage engine (changes disks)")
    commands = engine.add_subparsers(dest="engine_command", required=True)
    commands.add_parser("status", help="the pool, its health and any job")

    def changing(name: str, help: str) -> argparse.ArgumentParser:
        p: argparse.ArgumentParser = commands.add_parser(name, help=help)
        p.add_argument("--dry-run", action="store_true", help="show the plan, then discard it")
        p.add_argument("--commands", action="store_true", help="also print every command")
        p.add_argument("--no-ups", action="store_true", help="go ahead without a UPS")
        p.add_argument("--no-wait", action="store_true", help="start the job and return")
        return p

    changing("create", "build a new pool (erases every drive given)").add_argument(
        "devices", nargs="+", metavar="DEVICE"
    )
    changing("add", "add a drive to the pool").add_argument("device", metavar="DEVICE")
    p = changing("replace", "swap a working drive for another")
    p.add_argument("old", metavar="OLD")
    p.add_argument("new", metavar="NEW")
    p = changing("rebuild", "replace a failed drive")
    p.add_argument("failed", metavar="FAILED")
    p.add_argument("new", metavar="NEW")
    commands.add_parser("confirm-backup", help="record that a current backup exists")
    p = commands.add_parser("resume", help="resume a paused job")
    p.add_argument("job", metavar="JOB")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--done", action="store_true", help="the command in doubt took effect")
    group.add_argument("--rerun", action="store_true", help="run the command in doubt again")
    p = commands.add_parser("cancel", help="cancel a paused job between steps")
    p.add_argument("job", metavar="JOB")


def run(
    args: argparse.Namespace,
    engine: Engine | None = None,
    ask: Callable[[str], str] = input,
    out: TextIO = sys.stdout,
) -> int:
    engine = engine or Engine()
    command = args.engine_command
    if command == "status":
        show_status(engine, out)
        return 0
    if command == "create":
        return change(engine, "PlanCreate", "(as)", (args.devices,), args, ask, out)
    if command == "add":
        return change(engine, "PlanAdd", "(s)", (args.device,), args, ask, out)
    if command == "replace":
        return change(engine, "PlanReplace", "(ss)", (args.old, args.new), args, ask, out)
    if command == "rebuild":
        return change(engine, "PlanRebuild", "(ss)", (args.failed, args.new), args, ask, out)
    if command == "confirm-backup":
        with polkit_agent():
            engine.call(ROOT, MANAGER, "ConfirmBackup")
        print("Backup confirmed.", file=out)
        return 0
    job = job_path(args.job)
    if command == "resume":
        options = {}
        if args.done or args.rerun:
            options["resolution"] = engine.variant("s", "done" if args.done else "rerun")
        with polkit_agent():
            engine.call(job, JOB, "Resume", "(a{sv})", options)
        return 0 if follow(engine, job, out) == "completed" else 1
    with polkit_agent():
        engine.call(job, JOB, "Cancel")
    show_job(engine, job, out)
    return 0
