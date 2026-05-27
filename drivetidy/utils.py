# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 wt and DriveTidy contributors

"""Small utilities shared across modules."""

from __future__ import annotations

import re


_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGT]?)B?\s*$", re.IGNORECASE)
_SIZE_UNIT = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}


def parse_size(s: str | int) -> int:
    """Parse a human size like '100K', '8M', '1.5G' to bytes.

    Bare integers (or strings of digits) pass through unchanged.
    """
    if isinstance(s, int):
        return int(s)
    m = _SIZE_RE.match(s)
    if not m:
        raise ValueError(f"unrecognized size: {s!r}")
    num, unit = m.groups()
    return int(float(num) * _SIZE_UNIT[unit.upper()])


def human_size(n: int) -> str:
    """Render bytes as a short human-readable string."""
    n = int(n)
    if n < 1024:
        return f"{n} B"
    units = ["KB", "MB", "GB", "TB", "PB"]
    f = float(n)
    for u in units:
        f /= 1024
        if f < 1024:
            return f"{f:.1f} {u}"
    return f"{f:.1f} EB"
