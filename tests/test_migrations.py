# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 wt and DriveTidy contributors

"""Schema migration tests — fresh-DB path, existing-DB path, error path.

The behaviour matrix init_schema documents:
  - Fresh DB → SCHEMA creates everything; INSERT sets version to target;
    run_migrations short-circuits (current == target).
  - Pre-v3 DB → SCHEMA adds new tables idempotently; INSERT OR IGNORE
    leaves version at "2"; run_migrations walks 2 → 3.

We also pin the basic invariant that test_audit / test_gui / test_scan
(117 tests at last count) all see a sensible v3 schema.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from drivetidy import db as dbmod
from drivetidy import migrations as migmod


def _open_raw(path: Path) -> sqlite3.Connection:
    """Open WITHOUT init_schema — for setting up a synthetic v2 fixture."""
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _read_version(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    return row["value"] if row else None


# --- fresh DB ---------------------------------------------------------------

def test_fresh_db_lands_at_target_version(tmp_path):
    """A brand-new DB that's never seen DriveTidy should land at the
    current SCHEMA_VERSION in one shot — no migration steps required."""
    db_path = tmp_path / "fresh.db"
    conn = dbmod.get_conn(db_path)
    try:
        assert _read_version(conn) == dbmod.SCHEMA_VERSION
        # And the v3 table actually exists, not just the version row.
        names = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "exif_cache" in names
    finally:
        conn.close()


def test_fresh_db_run_migrations_is_noop(tmp_path):
    """run_migrations called against a fresh DB at target should not
    walk any steps (current == target → early return). Regression for
    the case where someone calls init_schema twice in a row."""
    db_path = tmp_path / "fresh.db"
    conn = dbmod.get_conn(db_path)
    try:
        # Sanity
        assert _read_version(conn) == dbmod.SCHEMA_VERSION
        # Calling again should be safe.
        migmod.run_migrations(conn, dbmod.SCHEMA_VERSION)
        assert _read_version(conn) == dbmod.SCHEMA_VERSION
    finally:
        conn.close()


# --- existing pre-v3 DB -----------------------------------------------------

def _build_v2_db(path: Path) -> None:
    """Synthesize a DB that looks like it was created by a v2 binary —
    only schema_meta with version='2' (the v2 SCHEMA had no exif_cache
    or idx_audit_runs_source). We don't recreate the full v2 schema
    because the only migration-relevant invariant is the version row +
    absence of v3 objects; SCHEMA executescript will add the rest."""
    conn = _open_raw(path)
    try:
        conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES ('schema_version', '2')"
        )
    finally:
        conn.close()


def test_v2_db_upgrades_to_v3_on_init(tmp_path):
    """An existing v2 DB opened by a v3 binary should:
      1. Get the new v3 tables/indexes via SCHEMA executescript
      2. Have its schema_version walked from '2' to '3' by run_migrations
    """
    db_path = tmp_path / "old.db"
    _build_v2_db(db_path)

    # Sanity: pre-condition.
    pre = _open_raw(db_path)
    try:
        assert _read_version(pre) == "2"
        names_before = {
            r[0] for r in pre.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "exif_cache" not in names_before
    finally:
        pre.close()

    # Open via the public path — this triggers init_schema → migrations.
    conn = dbmod.get_conn(db_path)
    try:
        assert _read_version(conn) == "3"
        names_after = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "exif_cache" in names_after
        # New index from S2 path also lands.
        idx_names = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        assert "idx_audit_runs_source" in idx_names
        assert "idx_exif_camera_taken" in idx_names
    finally:
        conn.close()


def test_v2_db_upgrade_is_idempotent(tmp_path):
    """Open a v2 DB twice in a row — second open should see version 3
    and not re-run any migration. This is the every-launch path; if
    migrations re-execute on every launch we'd hit duplicate-data
    issues the moment a future migration includes data backfill."""
    db_path = tmp_path / "old.db"
    _build_v2_db(db_path)

    conn1 = dbmod.get_conn(db_path)
    conn1.close()
    # Second open: schema_version is now "3"; run_migrations short-circuits.
    conn2 = dbmod.get_conn(db_path)
    try:
        assert _read_version(conn2) == "3"
    finally:
        conn2.close()


# --- error path -------------------------------------------------------------

def test_unknown_version_raises(tmp_path, monkeypatch):
    """A DB with a version we have no migration for must raise loudly,
    not silently keep the old version. Catching this early avoids data
    inconsistency from running v3 code against an unknown schema."""
    db_path = tmp_path / "weird.db"
    conn = _open_raw(db_path)
    try:
        conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES ('schema_version', '99')"
        )
    finally:
        conn.close()

    bad_conn = _open_raw(db_path)
    try:
        with pytest.raises(RuntimeError, match="no registered migration"):
            migmod.run_migrations(bad_conn, dbmod.SCHEMA_VERSION)
        # Version stays at 99 (we refused to bump it).
        assert _read_version(bad_conn) == "99"
    finally:
        bad_conn.close()


def test_failure_inside_migration_rolls_back_version(tmp_path, monkeypatch):
    """If a migration function raises, the version row must NOT advance
    — next launch should retry from the same version, not skip past it."""
    db_path = tmp_path / "old.db"
    _build_v2_db(db_path)

    def explode(_conn):
        raise RuntimeError("simulated migration crash")
    monkeypatch.setitem(migmod.MIGRATIONS, "2", explode)

    conn = _open_raw(db_path)
    try:
        with pytest.raises(RuntimeError, match="simulated migration crash"):
            migmod.run_migrations(conn, "3")
        # Version row must still be '2' — rollback fired.
        assert _read_version(conn) == "2"
    finally:
        conn.close()
