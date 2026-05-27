# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 wt and DriveTidy contributors

"""Schema migration framework.

Each migration is a Python function that takes a sqlite3.Connection and
brings an existing DB from version N to N+1. Migrations run inside an
explicit BEGIN/COMMIT (autocommit mode is on at the connection level, so
we manage the transaction ourselves). A failure rolls back, leaving the
schema_version row at the last successfully-applied step.

**Why our own (rather than alembic / yoyo)**: DriveTidy is a single-file
SQLite desktop app. We don't need branching, downgrades, or a migration
daemon — 40 lines of dict + while-loop covers the use case without
adding a dep that PyInstaller has to bundle and notarize.

**Adding a new migration**:
  1. Bump `db.SCHEMA_VERSION` to the new string (`"3"` → `"4"`)
  2. Add the new tables / indexes / columns to `db.SCHEMA` so a *fresh*
     DB gets them in one shot via `executescript`
  3. Write `_migrate_<from>_to_<to>(conn)` here. For purely additive
     changes (CREATE TABLE IF NOT EXISTS, CREATE INDEX IF NOT EXISTS),
     the body can be empty — `db.SCHEMA` already covers existing DBs
     too because the IF NOT EXISTS clauses are idempotent. The empty
     function still serves as documentation of the bump.
  4. For destructive changes (DROP TABLE, ALTER, data backfill), put
     the actual SQL/Python in the function body.
  5. Register: `MIGRATIONS["<from>"] = _migrate_<from>_to_<to>`
"""

from __future__ import annotations

import sqlite3
from typing import Callable


# --- migrations -------------------------------------------------------------

def _migrate_2_to_3(conn: sqlite3.Connection) -> None:
    """v2 → v3: add `exif_cache` table + `idx_audit_runs_source` index.

    Both are purely additive: `db.SCHEMA` runs first via
    `executescript`, and its `CREATE TABLE IF NOT EXISTS` /
    `CREATE INDEX IF NOT EXISTS` statements have already created the
    new objects on an existing v2 DB by the time this function runs.

    The migration body is therefore empty — we still register the
    version mapping so the version-bump path is explicit, and so a
    future v3 → v4 with destructive changes has somewhere to slot in."""
    pass


MIGRATIONS: dict[str, Callable[[sqlite3.Connection], None]] = {
    "2": _migrate_2_to_3,
}


# --- runner -----------------------------------------------------------------

def run_migrations(conn: sqlite3.Connection, target_version: str) -> None:
    """Walk an existing DB from its current `schema_version` to `target_version`.

    No-op on fresh DBs: `db.init_schema` sets `schema_version` directly
    to the target before calling us, so `current == target` short-circuits.

    Each step runs in its own transaction. On error the version row stays
    at the last successful step; the next launch picks up from there.
    """
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()
    if row is None:
        # No version row — caller forgot to insert one before calling us.
        # Don't speculate; let the next init_schema attempt set it.
        return
    current = row["value"]
    if current == target_version:
        return

    while current != target_version:
        if current not in MIGRATIONS:
            raise RuntimeError(
                f"schema_version {current!r} has no registered migration; "
                f"target is {target_version!r}. Was a step skipped in "
                f"migrations.MIGRATIONS?"
            )
        try:
            conn.execute("BEGIN")
            MIGRATIONS[current](conn)
            new_version = str(int(current) + 1)
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
                ("schema_version", new_version),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        current = new_version
