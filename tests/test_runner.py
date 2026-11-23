# SPDX-License-Identifier: GPL-3.0-or-later
"""The real runner, run only against harmless programs in a temporary directory."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tamarack.runner import (
    ENVIRONMENT,
    SubprocessRunner,
    UnsafeProgram,
    execution_enabled,
    resolve,
)

ROOT = os.geteuid() == 0


def fake_program(directory: Path, name: str, script: str, mode: int = 0o755) -> Path:
    path = directory / name
    path.write_text(f"#!/bin/sh\n{script}\n")
    path.chmod(mode)
    return path


def test_only_allowed_programs_resolve(tmp_path: Path) -> None:
    fake_program(tmp_path, "rm", "exit 0")
    with pytest.raises(UnsafeProgram, match="not a program"):
        resolve("rm", [str(tmp_path)])


def test_missing_program(tmp_path: Path) -> None:
    with pytest.raises(UnsafeProgram, match="not installed"):
        resolve("mdadm", [str(tmp_path)])


def test_group_writable_program_is_refused(tmp_path: Path) -> None:
    fake_program(tmp_path, "mdadm", "exit 0", 0o775)
    with pytest.raises(UnsafeProgram, match="only root"):
        resolve("mdadm", [str(tmp_path)])


@pytest.mark.skipif(not ROOT, reason="programs must be owned by root")
def test_runs_with_an_argument_list_and_a_clean_environment(tmp_path: Path) -> None:
    out = tmp_path / "out"
    fake_program(
        tmp_path,
        "mdadm",
        f'printf "%s|" "$@" > {out}; env > {out}.env; echo oops >&2; read x || exit 3',
    )
    result = SubprocessRunner([str(tmp_path)]).run(("mdadm", "a b", "$(x)", ";"))
    assert out.read_text() == "a b|$(x)|;|"  # no shell: every argument arrives as is
    env = dict(line.split("=", 1) for line in (tmp_path / "out.env").read_text().splitlines())
    assert {k: env[k] for k in ENVIRONMENT} == ENVIRONMENT
    assert "HOME" not in env
    assert result.exit == 3 and result.stderr == "oops\n"  # stdin is /dev/null


def test_execution_is_off_unless_turned_on(tmp_path: Path) -> None:
    config = tmp_path / "engine.conf"
    assert not execution_enabled(str(config))
    for text, enabled in (
        ("[execution]\nenabled = yes\n", True),
        ("[execution]\nenabled = YES\n", True),
        ("[execution]\nenabled = no\n", False),
        ("[execution]\nenabled = true\n", False),
        ("[other]\nenabled = yes\n", False),
        ("not ini", False),
    ):
        config.write_text(text)
        assert execution_enabled(str(config)) is enabled, text


def test_tamarack_helpers_resolve_only_in_libexec(tmp_path: Path) -> None:
    from tamarack.runner import LIBEXEC

    assert LIBEXEC == {"tamarack-meta": "/usr/libexec"}
    with pytest.raises(UnsafeProgram, match="not installed in /usr/libexec"):
        resolve("tamarack-meta")
