# SPDX-License-Identifier: GPL-3.0-or-later
"""Size parsing and formatting.

Drive sizes are marketed in decimal units (1 TB = 10**12 bytes), so plain
units like ``TB`` and ``T`` are decimal. Binary units (``TiB``) are accepted
too. Internally everything is bytes.
"""

from __future__ import annotations

import re

KiB = 1024
MiB = 1024 * KiB
GiB = 1024 * MiB
TiB = 1024 * GiB

_DECIMAL = {
    "": 1,
    "B": 1,
    "K": 10**3,
    "KB": 10**3,
    "M": 10**6,
    "MB": 10**6,
    "G": 10**9,
    "GB": 10**9,
    "T": 10**12,
    "TB": 10**12,
    "P": 10**15,
    "PB": 10**15,
}
_BINARY = {"KIB": KiB, "MIB": MiB, "GIB": GiB, "TIB": TiB, "PIB": 1024 * TiB}

_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([A-Za-z]*)\s*$")


def parse_size(text: str) -> int:
    """Parse ``"4TB"``, ``"500G"``, ``"3.84T"``, ``"4TiB"`` or raw bytes."""
    match = _SIZE_RE.match(text)
    if not match:
        raise ValueError(f"not a size: {text!r}")
    number, unit = match.groups()
    key = unit.upper()
    if key in _BINARY:
        factor = _BINARY[key]
    elif key in _DECIMAL:
        factor = _DECIMAL[key]
    else:
        raise ValueError(f"unknown size unit in {text!r}")
    value = int(float(number) * factor)
    if value <= 0:
        raise ValueError(f"size must be positive: {text!r}")
    return value


def format_size(n_bytes: int) -> str:
    """Format bytes in decimal units, the way drives are sold."""
    for unit, factor in (("TB", 10**12), ("GB", 10**9), ("MB", 10**6)):
        if n_bytes >= factor:
            return f"{n_bytes / factor:.2f} {unit}"
    return f"{n_bytes} B"


def align_down(n_bytes: int, alignment: int = MiB) -> int:
    return n_bytes - (n_bytes % alignment)
