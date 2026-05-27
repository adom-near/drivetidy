# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 wt and DriveTidy contributors

"""Backup-missing tests.

Closes the audit loop: audit reports missing files, this module copies
them to dest. Hard invariants this file pins:
  - dry-run NEVER invokes rsync (no temp files written, no subprocess)
  - apply mode actually copies the missing files (verified by checking
    dest has the files post-call)
  - source side is byte-for-byte unchanged after either mode
  - audit_run_id mismatches surface as SystemExit(1), not silent no-op
  - dest_ident multiselect: 0/1/many dests in the audit, picking and
    error paths covered
"""
from __future__ import annotations

import io
import shutil
import sqlite3
from pathlib import Path

import pytest

from drivetidy import audit, backup


def _make_tree(root: Path, files: dict[str, bytes]) -> None:
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def _snapshot(root: Path) -> dict[str, bytes]:
    """Capture (relpath, content) for every file under root, for after-
    state comparison. Used to assert source isn't touched."""
    snap = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            snap[str(p.relative_to(root))] = p.read_bytes()
    return snap


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


def _run_audit(db_path, src: Path, dst: Path, *, dst_label: str = "dst_lbl"):
    """Run audit so audit_runs / audit_missing have rows for backup tests
    to chew on. Returns (run_id, audit result)."""
    out = io.StringIO()
    r = audit.run_audit(str(src), [str(dst)], db_path=db_path, out=out, persist=True)
    # Latest audit_runs row is ours.
    conn = sqlite3.connect(db_path)
    try:
        run_id = conn.execute(
            "SELECT id FROM audit_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    finally:
        conn.close()
    return run_id, r


# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------

def test_dry_run_lists_missing_without_invoking_rsync(db_path, tmp_path, monkeypatch):
    """apply=False must populate planned_files but NOT touch dest and
    NOT spawn rsync. Hard line: any subprocess call is a regression."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_tree(src, {"a.jpg": b"AAA", "b.jpg": b"BBB"})
    _make_tree(dst, {"a.jpg": b"AAA"})  # b.jpg is missing
    run_id, _ = _run_audit(db_path, src, dst)

    # Sentinel: any subprocess.run call would mean dry-run leaked.
    def fail_subprocess(*args, **kwargs):
        raise AssertionError(
            f"dry-run must NOT call subprocess (got args={args[:1]})"
        )
    monkeypatch.setattr(backup.subprocess, "run", fail_subprocess)

    out = io.StringIO()
    report = backup.run_backup_missing(
        run_id, apply=False, db_path=db_path, out=out,
    )

    assert report.applied is False
    assert len(report.planned_files) == 1
    assert report.planned_files[0][0].endswith("b.jpg")
    assert report.succeeded == []
    assert report.failed == []
    # dest still has only a.jpg, b.jpg NOT copied.
    assert (dst / "a.jpg").exists()
    assert not (dst / "b.jpg").exists()


# ---------------------------------------------------------------------------
# Apply (real rsync)
# ---------------------------------------------------------------------------

def test_apply_actually_copies_missing_files(db_path, tmp_path):
    """apply=True invokes rsync; dest must have the missing files
    afterwards; source must be byte-for-byte unchanged."""
    if shutil.which("rsync") is None:
        pytest.skip("rsync not installed")

    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_tree(src, {
        "DCIM/IMG_0001.jpg": b"shot one bytes",
        "DCIM/IMG_0002.jpg": b"shot two bytes",
        "DCIM/IMG_0003.jpg": b"shot three bytes",
    })
    _make_tree(dst, {"DCIM/IMG_0001.jpg": b"shot one bytes"})  # only #1 backed
    src_before = _snapshot(src)
    run_id, _ = _run_audit(db_path, src, dst)

    out = io.StringIO()
    report = backup.run_backup_missing(
        run_id, apply=True, db_path=db_path, out=out,
    )

    assert report.applied is True
    assert len(report.failed) == 0, f"unexpected failures: {report.failed}"
    assert len(report.succeeded) == 2

    # Source untouched.
    assert _snapshot(src) == src_before

    # Dest now has both missing files at the right relative paths.
    assert (dst / "DCIM/IMG_0002.jpg").read_bytes() == b"shot two bytes"
    assert (dst / "DCIM/IMG_0003.jpg").read_bytes() == b"shot three bytes"


def test_apply_with_no_missing_is_noop(db_path, tmp_path):
    """audit run with 0 missing → no rsync invocation, empty report."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_tree(src, {"a.jpg": b"X"})
    _make_tree(dst, {"a.jpg": b"X"})
    run_id, _ = _run_audit(db_path, src, dst)

    out = io.StringIO()
    report = backup.run_backup_missing(
        run_id, apply=True, db_path=db_path, out=out,
    )
    # Even with apply=True, an empty plan returns applied=False because
    # rsync was never called. Surfaces "nothing to do" cleanly.
    assert report.planned_files == []
    assert "沒有 missing" in out.getvalue()


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------

def test_unknown_audit_run_id_raises(db_path, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    # Init the db without ever running an audit.
    from drivetidy import db as dbmod
    dbmod.get_conn(db_path).close()

    with pytest.raises(SystemExit):
        backup.run_backup_missing(99999, db_path=db_path, out=io.StringIO())


def test_dest_ident_required_when_audit_has_multiple_dests(db_path, tmp_path):
    src = tmp_path / "src"
    dst1 = tmp_path / "dst1"
    dst2 = tmp_path / "dst2"
    _make_tree(src, {"a.jpg": b"X", "b.jpg": b"Y"})
    _make_tree(dst1, {"a.jpg": b"X"})
    _make_tree(dst2, {"a.jpg": b"X"})
    out = io.StringIO()
    audit.run_audit(
        str(src), [str(dst1), str(dst2)], db_path=db_path, out=out, persist=True,
    )
    conn = sqlite3.connect(db_path)
    try:
        run_id = conn.execute(
            "SELECT id FROM audit_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    finally:
        conn.close()

    # No dest_ident → ambiguous → must SystemExit.
    with pytest.raises(SystemExit):
        backup.run_backup_missing(run_id, db_path=db_path, out=io.StringIO())

    # Correct ident → works (dry-run for cheap test).
    report = backup.run_backup_missing(
        run_id, dest_ident=str(dst1), db_path=db_path, out=io.StringIO(),
    )
    assert report.dest_ident == str(dst1)


def test_unknown_dest_ident_raises(db_path, tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_tree(src, {"a.jpg": b"X", "b.jpg": b"Y"})
    _make_tree(dst, {"a.jpg": b"X"})
    out = io.StringIO()
    audit.run_audit(str(src), [str(dst)], db_path=db_path, out=out, persist=True)
    conn = sqlite3.connect(db_path)
    try:
        run_id = conn.execute(
            "SELECT id FROM audit_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    finally:
        conn.close()

    with pytest.raises(SystemExit):
        backup.run_backup_missing(
            run_id, dest_ident="not-a-real-dest", db_path=db_path,
            out=io.StringIO(),
        )


def test_source_root_missing_after_audit_raises(db_path, tmp_path):
    """If the SD card is unmounted between audit and backup, we must
    refuse cleanly rather than letting rsync fail with cryptic errors."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_tree(src, {"a.jpg": b"X", "b.jpg": b"Y"})
    _make_tree(dst, {"a.jpg": b"X"})
    run_id, _ = _run_audit(db_path, src, dst)

    # Nuke source after audit ran but before backup — same as unmounting
    # the SD card. backup must refuse, not invoke rsync against /.
    shutil.rmtree(src)

    with pytest.raises(SystemExit):
        backup.run_backup_missing(
            run_id, apply=True, db_path=db_path, out=io.StringIO(),
        )


def test_same_path_for_src_and_dst_refused(db_path, tmp_path):
    """A typo / drag-and-drop accident shouldn't end up running
    rsync on the same directory in both arguments."""
    src = tmp_path / "drive"
    _make_tree(src, {"a.jpg": b"X"})
    out = io.StringIO()
    # Build an audit row whose dest IS the same path as source (degenerate
    # but possible if the user typo'd). We stuff it directly so the test
    # doesn't depend on audit refusing this upstream.
    from drivetidy import db as dbmod
    conn = dbmod.get_conn(db_path)
    import json
    try:
        cur = conn.execute(
            "INSERT INTO audit_runs("
            "source_ident, source_kind, source_scan_id, dest_specs, min_size, "
            "early_stop, total_files, total_bytes, matched_count, missing_count, "
            "missing_bytes, per_dest_match, ran_at) "
            "VALUES (?, ?, NULL, ?, 0, 1, 1, 1, 0, 1, 1, '{}', '2026-05-10')",
            (str(src), "path", json.dumps([{"ident": str(src), "kind": "path"}])),
        )
        run_id = cur.lastrowid
        conn.execute(
            "INSERT INTO audit_missing(run_id, source_path, size) VALUES (?, ?, ?)",
            (run_id, "a.jpg", 1),
        )
    finally:
        conn.close()

    with pytest.raises(SystemExit):
        backup.run_backup_missing(
            run_id, apply=True, db_path=db_path, out=io.StringIO(),
        )
