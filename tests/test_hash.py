# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 wt and DriveTidy contributors

"""Tests for `drivetidy hash`."""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest

from drivetidy import db as dbmod, hash_verify, scan as scan_mod


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


def test_hash_xxh64_collision_only(db_path, tmp_path):
    """Files with unique sizes are skipped; collision pairs are hashed."""
    a = tmp_path / "a"
    # two pairs of equal sizes + one unique
    _make_tree(a, {
        "p1": b"abc",          # size 3 — pair with p2
        "p2": b"xyz",          # size 3
        "u1": b"unique_size",  # size 11 — singleton
    })
    _scan(db_path, a, "a")

    summary = hash_verify.run_hash(
        "a",
        opts=hash_verify.HashOptions(algo="xxh64", min_size=0),
        db_path=db_path, out=io.StringIO(),
    )
    # only the size-3 pair should have been hashed
    assert summary.candidates == 2
    assert summary.hashed == 2

    conn = dbmod.get_conn(db_path)
    rows = conn.execute(
        "SELECT path, fingerprint, sample_size, algo FROM hashes ORDER BY path"
    ).fetchall()
    paths = [r["path"] for r in rows]
    assert paths == ["p1", "p2"]
    # different content → different fingerprint
    assert rows[0]["fingerprint"] != rows[1]["fingerprint"]
    assert rows[0]["algo"] == "xxh64"
    assert rows[0]["sample_size"] == 0
    conn.close()


def test_hash_md5_alias(db_path, tmp_path):
    a = tmp_path / "a"
    _make_tree(a, {"p1": b"hello", "p2": b"hello"})
    _scan(db_path, a, "a")

    hash_verify.run_hash(
        "a",
        opts=hash_verify.HashOptions(algo="md5"),
        db_path=db_path, out=io.StringIO(),
    )

    conn = dbmod.get_conn(db_path)
    rows = conn.execute("SELECT fingerprint, algo FROM hashes ORDER BY path").fetchall()
    # md5 hex is 32 chars
    assert all(len(r["fingerprint"]) == 32 for r in rows)
    # identical content → identical fingerprints
    assert rows[0]["fingerprint"] == rows[1]["fingerprint"]
    assert all(r["algo"] == "md5" for r in rows)
    conn.close()


def test_hash_sample_head(db_path, tmp_path):
    """sample_size>0: huge file's hash is the hash of its first N bytes."""
    a = tmp_path / "a"
    big_content = b"head" + b"\x00" * 4096 + b"tail"   # >4 KB total
    other = b"head" + b"\x00" * 4096 + b"DIFF"          # same first 8B
    # Make them same size so they become collision candidates.
    assert len(big_content) == len(other)
    _make_tree(a, {"big1.bin": big_content, "big2.bin": other})
    _scan(db_path, a, "a")

    sample = 8  # only hash first 8 bytes
    hash_verify.run_hash(
        "a",
        opts=hash_verify.HashOptions(algo="xxh64", sample_size=sample),
        db_path=db_path, out=io.StringIO(),
    )

    conn = dbmod.get_conn(db_path)
    rows = conn.execute("SELECT path, fingerprint, sample_size FROM hashes "
                        "ORDER BY path").fetchall()
    # both files have the same first 8 bytes → tentative match
    assert rows[0]["fingerprint"] == rows[1]["fingerprint"]
    assert rows[0]["sample_size"] == sample
    conn.close()


def test_hash_resume_skips_existing(db_path, tmp_path):
    a = tmp_path / "a"
    _make_tree(a, {"p1": b"abc", "p2": b"xyz"})  # collision pair
    _scan(db_path, a, "a")

    s1 = hash_verify.run_hash(
        "a", opts=hash_verify.HashOptions(algo="xxh64"),
        db_path=db_path, out=io.StringIO(),
    )
    assert s1.hashed == 2

    # Second run should skip everything (resume=True default)
    s2 = hash_verify.run_hash(
        "a", opts=hash_verify.HashOptions(algo="xxh64", resume=True),
        db_path=db_path, out=io.StringIO(),
    )
    assert s2.hashed == 0
    assert s2.skipped_existing == 2


def test_hash_no_resume_recomputes(db_path, tmp_path):
    a = tmp_path / "a"
    _make_tree(a, {"p1": b"abc", "p2": b"xyz"})
    _scan(db_path, a, "a")

    hash_verify.run_hash(
        "a", opts=hash_verify.HashOptions(algo="xxh64"),
        db_path=db_path, out=io.StringIO(),
    )
    s = hash_verify.run_hash(
        "a", opts=hash_verify.HashOptions(algo="xxh64", resume=False),
        db_path=db_path, out=io.StringIO(),
    )
    assert s.hashed == 2  # rehashed despite existing rows


def test_hash_unknown_label_exits(db_path, tmp_path):
    with pytest.raises(SystemExit) as ei:
        hash_verify.run_hash(
            "ghost", opts=hash_verify.HashOptions(),
            db_path=db_path, out=io.StringIO(),
        )
    assert ei.value.code == 1


def test_hash_full_xxh64_distinguishes(db_path, tmp_path):
    """Without sample-head, fingerprints differ when content differs."""
    a = tmp_path / "a"
    _make_tree(a, {"p1": b"a" * 10000, "p2": b"b" * 10000})  # same size
    _scan(db_path, a, "a")

    hash_verify.run_hash(
        "a", opts=hash_verify.HashOptions(algo="xxh64"),
        db_path=db_path, out=io.StringIO(),
    )
    conn = dbmod.get_conn(db_path)
    rows = conn.execute("SELECT fingerprint FROM hashes ORDER BY path").fetchall()
    assert rows[0]["fingerprint"] != rows[1]["fingerprint"]
    conn.close()
