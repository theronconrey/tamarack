# SPDX-License-Identifier: GPL-3.0-or-later
"""The real command runner, and the switch that allows the engine to use it.

``SubprocessRunner`` runs one allowed program with an argument list: never a
shell, standard input from /dev/null, a fixed environment and a fixed search
path. Each program is resolved to an absolute path under /usr and refused if
it is not a root-owned regular file that only root can write.

The engine is given this runner only when the administrator has turned
execution on in ``/etc/tamarack/engine.conf``::

    [execution]
    enabled = yes

Without that file, or with anything but ``yes``, the engine plans and
reviews but refuses to execute. Until the VM test matrix (step 7 of
docs/design/engine.md) passes, execution is meant for test machines only.
"""

from __future__ import annotations

import configparser
import logging
import os
import stat
import subprocess
from collections.abc import Iterable

from .job import Result
from .ops import ALLOWED_PROGRAMS
from .recovery import PROBE_PROGRAMS

CONFIG_FILE = "/etc/tamarack/engine.conf"
PROGRAM_DIRS = ("/usr/sbin", "/usr/bin")
# Tamarack's own helpers live in /usr/libexec, not on anyone's PATH.
LIBEXEC = {"tamarack-meta": "/usr/libexec"}
ENVIRONMENT = {"PATH": ":".join(PROGRAM_DIRS), "LC_ALL": "C.UTF-8"}
STDERR_LIMIT = 64 * 1024  # characters of stderr kept per command

log = logging.getLogger("tamarack-engine")


class UnsafeProgram(OSError):
    """A program is missing or could be replaced by someone other than root."""


def resolve(program: str, directories: Iterable[str] = PROGRAM_DIRS) -> str:
    """The absolute path of an allowed program, checked for safe ownership."""
    if program not in ALLOWED_PROGRAMS | PROBE_PROGRAMS:
        raise UnsafeProgram(f"{program!r} is not a program Tamarack may run")
    if program in LIBEXEC and tuple(directories) == PROGRAM_DIRS:
        directories = (LIBEXEC[program],)
    for directory in directories:
        path = os.path.join(directory, program)
        try:
            info = os.stat(path)
        except FileNotFoundError:
            continue
        unsafe = info.st_uid != 0 or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        if not stat.S_ISREG(info.st_mode) or unsafe or not info.st_mode & stat.S_IXUSR:
            raise UnsafeProgram(f"{path} is not a root-owned executable only root can change")
        return path
    raise UnsafeProgram(f"{program} is not installed in {' or '.join(directories)}")


class SubprocessRunner:
    """Runs commands for real. Only the engine, inside its sandbox, uses this."""

    def __init__(self, directories: Iterable[str] = PROGRAM_DIRS) -> None:
        self.directories = tuple(directories)

    def run(self, argv: tuple[str, ...]) -> Result:
        path = resolve(argv[0], self.directories)
        log.info("running %s", " ".join(argv))
        completed = subprocess.run(
            [path, *argv[1:]],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=ENVIRONMENT,
            close_fds=True,
            check=False,
            text=True,
            errors="replace",
        )
        stderr = completed.stderr[-STDERR_LIMIT:]
        if completed.returncode:
            log.warning("%s exited %d", argv[0], completed.returncode)
        return Result(completed.returncode, stderr)


def execution_enabled(path: str = CONFIG_FILE) -> bool:
    """True only if the admin config says ``[execution] enabled = yes``."""
    parser = configparser.ConfigParser()
    try:
        with open(path, encoding="utf-8") as f:
            parser.read_file(f)
    except FileNotFoundError:
        return False
    except (OSError, configparser.Error, UnicodeDecodeError) as exc:
        log.error("%s cannot be read, so execution stays off: %s", path, exc)
        return False
    return parser.get("execution", "enabled", fallback="no").strip().lower() == "yes"
