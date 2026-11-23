# SPDX-License-Identifier: GPL-3.0-or-later
"""Run an approved plan step by step, journaling every action.

``start`` runs a reviewed plan document from the beginning; ``resume`` picks
up a job from its journal after a pause or a power cut. Both run commands
through a ``Runner``, which this module never implements: the engine supplies
the real one (step 6 of the design), and tests supply a fake. Nothing here
can reach a disk on its own.

Every record is durable before the action it describes, and the state used
for decisions is the journal replayed, so a job continues exactly where the
journal says it stopped. The resume rules for a command cut off mid-run are
in ``recovery``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from .journal import JobState, JournalError, JournalWriter, apply, load, replay, tail
from .ops import Command
from .recovery import Resume, rule_for
from .review import FORMAT, VERSION, digest


@dataclass(frozen=True)
class Result:
    exit: int
    stderr: str = ""


class Runner(Protocol):
    def run(self, argv: tuple[str, ...]) -> Result: ...


class Resolution(StrEnum):
    """A person's answer for a command the journal cannot vouch for."""

    DONE = "done"  # they checked: it took effect
    RERUN = "rerun"  # they checked: run it again


@dataclass(frozen=True)
class Report:
    state: str  # as Job.State on D-Bus: paused, completed, failed or cancelled
    step: int
    message: str = ""


# Called before each step with its number; returns a reason to pause, or None.
Gate = Callable[[int], str | None]
Clock = Callable[[], str]
# Called with each record once it is durable, and the job's state after it.
Observer = Callable[[dict[str, Any], JobState], None]


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_document(doc: dict[str, Any]) -> None:
    """Refuse a plan document the engine did not produce as reviewed."""
    if doc.get("format") != FORMAT or doc.get("version") != VERSION:
        raise JournalError("not a tamarack plan document")
    for number, step in enumerate(doc["steps"], start=1):
        for c in step["commands"]:
            command = Command(tuple(c["argv"]), bool(c["tolerate_failure"]))
            stored = {k: v for k, v in c.items() if k in ("resume", "probe")}
            if rule_for(command).to_dict() != stored:
                raise JournalError(f"step {number}: resume rule differs for {command.shell()}")


def start(
    doc: dict[str, Any],
    job_id: str,
    path: str,
    runner: Runner,
    gate: Gate | None = None,
    now: Clock = utc_now,
    observer: Observer | None = None,
) -> Report:
    """Run a reviewed plan from its first step."""
    validate_document(doc)
    first = {"t": "job", "id": job_id, "digest": digest(doc), "plan": doc, "started": now()}
    with JournalWriter.create(path, first) as writer:
        state = replay([first])
        if observer is not None:
            observer(first, state)
        job = _Job(state, writer, runner, gate, now, observer)
        return job.run()


def resume(
    path: str,
    runner: Runner,
    *,
    authorized: bool,
    gate: Gate | None = None,
    resolution: Resolution | None = None,
    now: Clock = utc_now,
    observer: Observer | None = None,
) -> Report:
    """Continue a job from its journal.

    Without ``authorized`` (no admin has called Resume) the only thing done is
    re-attaching to an interrupted ``mdadm --wait``, which the kernel is
    finishing anyway; the job then pauses for Resume.
    """
    state = load(path)
    if state.ended:
        raise JournalError(f"job {state.job_id} has already ended ({state.result})")
    if digest(state.plan) != state.digest:
        raise JournalError(f"job {state.job_id}: plan does not match its digest")
    validate_document(state.plan)
    with JournalWriter.reopen(path) as writer:
        job = _Job(state, writer, runner, gate, now, observer)
        if state.failed:
            return job.end("failed", state.message)
        if not authorized:
            if state.in_doubt and job.is_wait(state.step, state.cmd):
                if (report := job.run_command()) is not None:
                    return report
                return job.pause("the resync or reshape finished; waiting for Resume")
            return Report("paused", state.step, state.message or "waiting for Resume")
        job.write({"t": "resume", "authorized": True, "at": now()})
        if state.in_doubt and (report := job.settle(resolution)) is not None:
            return report
        return job.run()


def cancel(path: str, now: Clock = utc_now, observer: Observer | None = None) -> Report:
    """End a paused job between steps. Refused inside a step."""
    state = load(path)
    if state.ended:
        raise JournalError(f"job {state.job_id} has already ended ({state.result})")
    if state.step_open or state.in_doubt:
        raise JournalError("a job can only be cancelled between steps")
    with JournalWriter.reopen(path) as writer:
        job = _Job(state, writer, _NoRunner(), None, now, observer)
        return job.end("cancelled", "cancelled")


class _NoRunner:
    def run(self, argv: tuple[str, ...]) -> Result:
        raise AssertionError("cancel runs nothing")


class _Job:
    def __init__(
        self,
        state: JobState,
        writer: JournalWriter,
        runner: Runner,
        gate: Gate | None,
        now: Clock,
        observer: Observer | None = None,
    ) -> None:
        self.state, self.writer, self.runner, self.gate, self.now = (
            state,
            writer,
            runner,
            gate,
            now,
        )
        self.observer = observer

    def write(self, record: dict[str, Any]) -> None:
        """Make the record durable, then advance the state by it."""
        self.writer.append(record)
        apply(self.state, record)
        if self.observer is not None:
            self.observer(record, self.state)

    def command(self, step: int, cmd: int) -> Command:
        c = self.state.command(step, cmd)
        return Command(tuple(c["argv"]), bool(c["tolerate_failure"]))

    def is_wait(self, step: int, cmd: int) -> bool:
        return self.command(step, cmd).argv[:2] == ("mdadm", "--wait")

    def run(self) -> Report:
        s = self.state
        while not s.finished_all_steps:
            if not s.step_open:
                if self.gate is not None and (reason := self.gate(s.step)):
                    return self.pause(reason)
                self.write({"t": "step-start", "step": s.step})
            while s.cmd < len(s.steps[s.step - 1]["commands"]):
                if (report := self.run_command()) is not None:
                    return report
            self.write({"t": "step-end", "step": s.step})
        return self.end("completed", "")

    def run_command(self) -> Report | None:
        """Run the current command; a Report means the job stopped."""
        s = self.state
        step, cmd = s.step, s.cmd
        command = self.command(step, cmd)
        self.write({"t": "cmd-start", "step": step, "cmd": cmd, "argv": list(command.argv)})
        try:
            result = self.runner.run(command.argv)
        except OSError as exc:
            result = Result(127, str(exc))
        if result.exit < 0:
            # Killed by a signal: the engine is being stopped, not the command
            # failing. Leave it in doubt, as after a power cut, so the next
            # start re-attaches to it (an mdadm --wait) or Resume settles it.
            return self.pause(
                f"step {step} was interrupted: {command.shell()} was stopped by signal "
                f"{-result.exit}; it continues when the engine starts again"
            )
        # Tolerated failures are the command's own (an exit code), never a kill.
        ok = result.exit == 0 or command.tolerate_failure
        self.write(
            {
                "t": "cmd-end",
                "step": step,
                "cmd": cmd,
                "exit": result.exit,
                "ok": ok,
                "stderr_tail": tail(result.stderr),
            }
        )
        if ok:
            return None
        title = s.steps[step - 1]["title"]
        detail = tail(result.stderr).strip()
        message = f"step {step} ({title}) failed: {command.shell()} exited {result.exit}"
        return self.end("failed", f"{message}: {detail}" if detail else message)

    def settle(self, resolution: Resolution | None) -> Report | None:
        """Decide about the command cut off mid-run; a Report means stop."""
        s = self.state
        step, cmd = s.step, s.cmd
        command = self.command(step, cmd)
        if resolution is not None:
            self.write({"t": "resolve", "step": step, "cmd": cmd, "as": resolution.value})
            return None if resolution is Resolution.DONE else self.run_command()
        rule = rule_for(command)
        if rule.resume is Resume.PROBE:
            try:
                probe = self.runner.run(rule.probe)
            except OSError as exc:
                probe = Result(127, str(exc))
            done = probe.exit == 0
            self.write(
                {
                    "t": "probe",
                    "step": step,
                    "cmd": cmd,
                    "argv": list(rule.probe),
                    "exit": probe.exit,
                    "done": done,
                }
            )
            return None if done else self.run_command()
        if rule.resume is Resume.RERUN:
            return self.run_command()
        return self.pause(
            f"step {step}, command {cmd + 1} was cut off and cannot be checked "
            f"automatically: {command.shell()}. Check it, then resume with done or rerun."
        )

    def pause(self, reason: str) -> Report:
        self.write({"t": "pause", "reason": reason, "at": self.now()})
        return Report("paused", self.state.step, reason)

    def end(self, result: str, message: str) -> Report:
        self.write({"t": "job-end", "result": result, "message": message, "at": self.now()})
        return Report(result, self.state.step, message)
