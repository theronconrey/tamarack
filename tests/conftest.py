# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared fixtures."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:dir={dir}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""


@pytest.fixture
def bus(tmp_path: Path) -> Iterator[str]:
    config = tmp_path / "bus.conf"
    config.write_text(BUS_CONFIG.format(dir=tmp_path))
    daemon = subprocess.Popen(
        ["dbus-daemon", f"--config-file={config}", "--nofork", "--print-address=1"],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert daemon.stdout is not None
    address = daemon.stdout.readline().strip()
    yield address
    daemon.terminate()
    daemon.wait(timeout=10)
