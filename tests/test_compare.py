# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 wt and DriveTidy contributors

"""Tests for `drivetidy compare`."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from drivetidy import compare, scan as scan_mod


def _make_tree(root: Path, files: dict) -> None:
    for rel, content in files.items():
        full = root / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(content)


def _scan(db_path: str, root: Path, label: str) -> int:
    return scan_mod.run_scan(
        str(root), label=label, drive_type_override="ssd",
        backend_pref="rclone", db_path=db_path, out=io.StringIO(),
    )


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "test.db")


def test_compare_identical(db_path, tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    files = {"x.jpg": b"hi", "sub/y.jpg": b"yo"}
    _make_tree(a, files)
    _make_tree(b, files)
    _scan(db_path, a, "a")
    _scan(db_path, b, "b")

    out = io.StringIO()
    r = compare.run_compare("a", "b", db_path=db_path, out=out)
    assert r.match == 2
    assert r.differ == 0
    assert r.only_a == 0
    assert r.only_b == 0


def test_compare_only_in_a(db_path, tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    _make_tree(a, {"shared.jpg": b"x", "extra.jpg": b"yy"})
    _make_tree(b, {"shared.jpg": b"x"})
    _scan(db_path, a, "a")
    _scan(db_path, b, "b")

    r = compare.run_compare("a", "b", db_path=db_path, out=io.StringIO())
    assert r.match == 1
    assert r.only_a == 1
    assert r.only_b == 0
    assert r.only_a_examples[0][0].endswith("extra.jpg")


def test_compare_differ(db_path, tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    _make_tree(a, {"file.bin": b"short"})
    _make_tree(b, {"file.bin": b"longer content"})
    _scan(db_path, a, "a")
    _scan(db_path, b, "b")

    r = compare.run_compare("a", "b", db_path=db_path, out=io.StringIO())
    assert r.differ == 1
    assert r.match == 0
    # examples carry both sizes
    p, sa, sb = r.differ_examples[0]
    assert p.endswith("file.bin")
    assert sa != sb


def test_compare_unknown_label_exits(db_path, tmp_path):
    a = tmp_path / "a"
    _make_tree(a, {"x": b"x"})
    _scan(db_path, a, "a")
    with pytest.raises(SystemExit) as ei:
        compare.run_compare("a", "ghost", db_path=db_path, out=io.StringIO())
    assert ei.value.code == 1


def test_compare_same_label_twice_exits(db_path, tmp_path):
    a = tmp_path / "a"
    _make_tree(a, {"x": b"x"})
    _scan(db_path, a, "a")
    with pytest.raises(SystemExit) as ei:
        compare.run_compare("a", "a", db_path=db_path, out=io.StringIO())
    assert ei.value.code == 1


def test_compare_min_size_filters(db_path, tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    _make_tree(a, {
        "tiny.txt": b"x",            # 1 byte
        "big.bin": b"a" * 2048,      # 2 KB
    })
    _make_tree(b, {"big.bin": b"a" * 2048})
    _scan(db_path, a, "a")
    _scan(db_path, b, "b")

    r = compare.run_compare("a", "b", min_size=1024, db_path=db_path, out=io.StringIO())
    assert r.match == 1
    assert r.only_a == 0          # tiny.txt filtered out
    assert r.differ == 0
