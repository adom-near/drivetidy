# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 wt and DriveTidy contributors

from __future__ import annotations

import pytest

from drivetidy.utils import human_size, parse_size


def test_parse_size_bytes():
    assert parse_size("0") == 0
    assert parse_size("100") == 100
    assert parse_size(0) == 0


def test_parse_size_units():
    assert parse_size("1K") == 1024
    assert parse_size("1KB") == 1024
    assert parse_size("8M") == 8 * 1024**2
    assert parse_size("1.5G") == int(1.5 * 1024**3)
    assert parse_size("2T") == 2 * 1024**4
    # Whitespace + lowercase
    assert parse_size(" 100k ") == 100 * 1024


def test_parse_size_bad():
    with pytest.raises(ValueError):
        parse_size("abc")
    with pytest.raises(ValueError):
        parse_size("100Z")


def test_human_size():
    assert human_size(0) == "0 B"
    assert human_size(1023) == "1023 B"
    assert human_size(1024) == "1.0 KB"
    assert human_size(1024**2) == "1.0 MB"
    # Mid-range
    assert human_size(2_500_000_000).endswith("GB")
