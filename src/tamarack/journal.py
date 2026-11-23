# SPDX-License-Identifier: GPL-3.0-or-later
"""The step journal: an append-only record of one job, safe across power cuts.

One JSON object per line. Each line is written whole and ``fsync``ed before
the engine takes the next action, so after a crash the journal says exactly
how far the job got. A power cut can tear only the last line; a torn last
line is ignored on reading and cut off before anything more is appended.

Records (steps are numbered from 1, as in the dry run; commands from 0)::

    {"t":"job","id":…,"digest":…,"plan":{…},"started":…}
    {"t":"step-start","step":3}
    {"t":"cmd-start","step":3,"cmd":0,"argv":[…]}
    {"t":"cmd-end","step":3,"cmd":0,"exit":0,"ok":true,"stderr_tail":""}
    {"t":"probe","step":3,"cmd":0,"argv":[…],"exit":0,"done":true}
    {"t":"resolve","step":3,"cmd":0,"as":"done"}
    {"t":"step-end","step":3}
    {"t":"pause","reason":…,"at":…}
    {"t":"resume","authorized":true,"at":…}
    {"t":"job-end","result":"completed","message":"","at":…}

``replay`` rebuilds where the job stands and refuses a journal whose records
are out of order or do not match the plan they carry.

Journals live in ``/var/lib/tamarack/jobs`` (``StateDirectory=tamarack``) and
are kept after the job ends as its audit trail.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

JOBS_DIR = "/var/lib/tamarack/jobs"
STDERR_TAIL = 2048  # bytes of stderr kept per command

RESULTS = ("completed", "failed", "cancelled")


class JournalError(ValueError):
    """The journal is unreadable, out of order or does not match its plan."""


def encode(record: dict[str, Any]) -> bytes:
    return (
        json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode()


class JournalWriter:
    """Appends records, each made durable before ``append`` returns."""

    def __init__(self, fd: int) -> None:
        self._fd = fd

    @classmethod
    def create(cls, path: str, first: dict[str, Any]) -> JournalWriter:
        """Start a new journal; refuses to touch an existing file."""
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND | os.O_CLOEXEC, 0o600)
        writer = cls(fd)
        writer.append(first)
        _fsync_dir(os.path.dirname(path) or ".")
        return writer

    @classmethod
    def reopen(cls, path: str) -> JournalWriter:
        """Continue an existing journal, cutting off a torn last line first."""
        fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CLOEXEC)
        try:
            data = _read_all(fd)
            keep = data.rfind(b"\n") + 1
            if keep != len(data):
                os.ftruncate(fd, keep)
                os.fsync(fd)
        except BaseException:
            os.close(fd)
            raise
        return cls(fd)

    def append(self, record: dict[str, Any]) -> None:
        data = memoryview(encode(record))
        while data:
            written = os.write(self._fd, data)
            data = data[written:]
        os.fsync(self._fd)

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __enter__(self) -> JournalWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _read_all(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = []
    while chunk := os.read(fd, 1 << 16):
        chunks.append(chunk)
    return b"".join(chunks)


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_records(path: str) -> list[dict[str, Any]]:
    """Every complete record in the journal. A torn last line is skipped."""
    with open(path, "rb") as f:
        data = f.read()
    lines = data.split(b"\n")
    lines.pop()  # after the last newline: empty, or a torn write
    records = []
    for number, line in enumerate(lines, start=1):
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise JournalError(f"{path}: line {number} is not JSON") from exc
        if not isinstance(record, dict) or not isinstance(record.get("t"), str):
            raise JournalError(f"{path}: line {number} is not a journal record")
        records.append(record)
    return records


# --- replay ------------------------------------------------------------------


@dataclass
class JobState:
    """Where a job stands, rebuilt from its journal."""

    job_id: str
    digest: str
    plan: dict[str, Any]
    started: str
    step: int = 1  # the next step to run (or the one in progress)
    cmd: int = 0  # the next command in that step
    step_open: bool = False  # step-start written, step-end not yet
    in_doubt: bool = False  # command (step, cmd) started and did not finish
    result: str = ""  # set once the job has ended
    failed: bool = False  # a command failed; job-end may not be written yet
    message: str = ""  # last pause reason, failure or end message

    @property
    def steps(self) -> list[dict[str, Any]]:
        steps: list[dict[str, Any]] = self.plan["steps"]
        return steps

    @property
    def ended(self) -> bool:
        return bool(self.result)

    @property
    def finished_all_steps(self) -> bool:
        return self.step > len(self.steps)

    def command(self, step: int, cmd: int) -> dict[str, Any]:
        command: dict[str, Any] = self.steps[step - 1]["commands"][cmd]
        return command


def replay(records: list[dict[str, Any]]) -> JobState:
    if not records or records[0].get("t") != "job":
        raise JournalError("journal does not start with a job record")
    first = records[0]
    try:
        state = JobState(
            job_id=str(first["id"]),
            digest=str(first["digest"]),
            plan=dict(first["plan"]),
            started=str(first["started"]),
        )
        for number, record in enumerate(records[1:], start=2):
            apply(state, record, number)
    except (KeyError, TypeError, IndexError) as exc:
        raise JournalError(f"malformed journal record: {exc}") from exc
    return state


def apply(state: JobState, r: dict[str, Any], number: int = 0) -> None:
    """Advance ``state`` by one record, refusing records out of order."""

    def bad(why: str) -> JournalError:
        where = f"record {number}" if number else "record"
        return JournalError(f"{where} ({r['t']}): {why}")

    if state.ended:
        raise bad("record after job-end")
    if state.failed and r["t"] != "job-end":
        raise bad("only job-end may follow a failed command")
    t = r["t"]
    here = (state.step, state.cmd)
    if t in ("cmd-start", "cmd-end", "probe", "resolve") and (r["step"], r["cmd"]) != here:
        raise bad(f"expected step {state.step} command {state.cmd}")
    if t == "step-start":
        if state.step_open or r["step"] != state.step or state.finished_all_steps:
            raise bad("step out of order")
        state.step_open = True
    elif t == "cmd-start":
        if not state.step_open or state.cmd >= len(state.steps[state.step - 1]["commands"]):
            raise bad("no such command")
        if r["argv"] != state.command(state.step, state.cmd)["argv"]:
            raise bad("command differs from the plan")
        state.in_doubt = True  # also for a rerun of a command already in doubt
    elif t == "cmd-end":
        if not state.in_doubt:
            raise bad("command was not started")
        if r["ok"]:
            _command_done(state)
        else:
            state.in_doubt, state.failed = False, True
            state.message = f"command failed with exit status {r['exit']}"
    elif t == "probe":
        if not state.in_doubt:
            raise bad("nothing to probe")
        if r["done"]:
            _command_done(state)
    elif t == "resolve":
        if not state.in_doubt or r["as"] not in ("done", "rerun"):
            raise bad("nothing to resolve")
        if r["as"] == "done":
            _command_done(state)
    elif t == "step-end":
        commands = state.steps[state.step - 1]["commands"]
        if not state.step_open or r["step"] != state.step or state.cmd != len(commands):
            raise bad("step ended early")
        state.step, state.cmd, state.step_open = state.step + 1, 0, False
    elif t == "pause":
        state.message = str(r["reason"])
    elif t == "resume":
        state.message = ""
    elif t == "job-end":
        if r["result"] not in RESULTS:
            raise bad("unknown result")
        if r["result"] == "completed" and not state.finished_all_steps:
            raise bad("completed with steps left")
        if r["result"] == "cancelled" and (state.step_open or state.in_doubt):
            raise bad("cancelled inside a step")
        state.result = r["result"]
        state.message = str(r.get("message", ""))
    else:
        raise bad("unknown record type")


def _command_done(state: JobState) -> None:
    state.in_doubt = False
    state.cmd += 1


def journal_path(job_id: str, directory: str = JOBS_DIR) -> str:
    if not job_id.isdigit() or not job_id.isascii():
        raise ValueError(f"invalid job id {job_id!r}")
    return os.path.join(directory, f"{job_id}.jsonl")


def load(path: str) -> JobState:
    return replay(read_records(path))


def tail(text: str) -> str:
    data = text.encode(errors="replace")[-STDERR_TAIL:]
    return data.decode(errors="replace")
