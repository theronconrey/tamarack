# SPDX-License-Identifier: GPL-3.0-or-later
"""A command runner for tests: records calls and never runs anything."""

from __future__ import annotations

from collections.abc import Callable

from tamarack.job import Result


class PowerCut(BaseException):  # noqa: N818 - an event, not an error
    """Raised by FakeRunner to simulate losing power mid-command."""


class FakeRunner:
    def __init__(
        self,
        results: dict[tuple[str, ...], Result] | None = None,
        cut: Callable[[int, tuple[str, ...]], bool] | None = None,
    ) -> None:
        self.results = results or {}
        self.cut = cut
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: tuple[str, ...]) -> Result:
        index = len(self.calls)
        self.calls.append(argv)
        if self.cut is not None and self.cut(index, argv):
            raise PowerCut
        return self.results.get(argv, Result(0))
