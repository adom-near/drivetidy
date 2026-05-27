# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 wt and DriveTidy contributors

"""SQLite schema and connection helpers.

PRAGMA tuning per internal notes §4:
  - journal_mode=WAL           (concurrent reader during writes)
  - synchronous=NORMAL         (2-3x faster writes; acceptable since scans are
                                re-runnable if we lose the last transaction)
  - cache_size=-64000          (64 MB cache)
  - temp_store=MEMORY
  - busy_timeout=5000

All app queries use parametrized placeholders; no string concat of user input.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config


SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    label            TEXT NOT NULL,
    root_path        TEXT NOT NULL,
    drive_type       TEXT,                     -- 'hdd' | 'ssd' | 'unknown'
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    file_count       INTEGER DEFAULT 0,
    scan_mode        TEXT NOT NULL DEFAULT 'full',  -- 'full' | 'incremental'
    parent_scan_id   INTEGER REFERENCES scans(id)
);

CREATE INDEX IF NOT EXISTS idx_scans_label_started
    ON scans(label, started_at DESC);

CREATE TABLE IF NOT EXISTS files (
    scan_id   INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
    path      TEXT    NOT NULL,  -- NFC-normalized (for JOIN / compare / dedup)
    path_raw  TEXT,               -- Raw bytes as-returned by OS; NULL if identical
    size      INTEGER NOT NULL,
    mtime     REAL,
    PRIMARY KEY (scan_id, path)
);

CREATE INDEX IF NOT EXISTS idx_files_size
    ON files(scan_id, size);

CREATE TABLE IF NOT EXISTS hashes (
    scan_id      INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
    path         TEXT    NOT NULL,
    algo         TEXT    NOT NULL DEFAULT 'xxh64',  -- 'xxh64' | 'md5'
    fingerprint  TEXT    NOT NULL,
    sample_size  INTEGER NOT NULL DEFAULT 0,        -- 0 = full file; >0 = sample-head bytes
    hashed_at    TEXT    NOT NULL,
    PRIMARY KEY (scan_id, path, algo, sample_size)
);

CREATE INDEX IF NOT EXISTS idx_hashes_fingerprint
    ON hashes(fingerprint, algo, sample_size);

CREATE TABLE IF NOT EXISTS dedup_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_a       INTEGER NOT NULL REFERENCES scans(id),
    scan_b       INTEGER REFERENCES scans(id),       -- NULL = intra-drive dedup
    policy       TEXT,
    min_size     INTEGER NOT NULL DEFAULT 0,
    algo         TEXT    NOT NULL DEFAULT 'xxh64',
    sample_size  INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS dedup_pairs (
    run_id       INTEGER NOT NULL REFERENCES dedup_runs(id) ON DELETE CASCADE,
    fingerprint  TEXT    NOT NULL,
    path_a       TEXT    NOT NULL,
    path_b       TEXT    NOT NULL,
    size         INTEGER NOT NULL,
    folder_a     TEXT,
    folder_b     TEXT,
    confidence   TEXT NOT NULL DEFAULT 'certain'
        CHECK (confidence IN ('certain', 'tentative', 'rejected'))
);

CREATE INDEX IF NOT EXISTS idx_pairs_folder
    ON dedup_pairs(run_id, folder_a, folder_b);

-- Prevent duplicate pair rows. Includes fingerprint so a sample-head
-- (algo,sample_size) variant of the same path-pair does not trigger
-- a unique conflict during `promote`.
CREATE UNIQUE INDEX IF NOT EXISTS uq_pairs_run_paths_fp
    ON dedup_pairs(run_id, path_a, path_b, fingerprint);

CREATE TABLE IF NOT EXISTS applied_actions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER REFERENCES dedup_runs(id),
    path         TEXT NOT NULL,
    size         INTEGER,
    fingerprint  TEXT,
    action       TEXT NOT NULL CHECK (action IN ('trash', 'rm', 'dry-run')),
    applied_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_ident    TEXT    NOT NULL,
    source_kind     TEXT    NOT NULL CHECK (source_kind IN ('label', 'path')),
    source_scan_id  INTEGER REFERENCES scans(id),         -- NULL when source_kind = 'path'
    dest_specs      TEXT    NOT NULL,                      -- JSON: [{"ident":..., "kind":...}]
    min_size        INTEGER NOT NULL DEFAULT 0,
    early_stop      INTEGER NOT NULL DEFAULT 1,
    total_files     INTEGER NOT NULL,
    total_bytes     INTEGER NOT NULL,
    matched_count   INTEGER NOT NULL,
    missing_count   INTEGER NOT NULL,
    missing_bytes   INTEGER NOT NULL,
    per_dest_match  TEXT,                                  -- JSON: {ident: count}
    ran_at          TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_runs_ran_at
    ON audit_runs(ran_at DESC);

-- v3: history-diff lookup by source identity (S2). Picks the most recent
-- prior audit_run for the same source_ident; the diff query then walks
-- audit_missing for both runs.
CREATE INDEX IF NOT EXISTS idx_audit_runs_source
    ON audit_runs(source_ident, ran_at DESC);

CREATE TABLE IF NOT EXISTS audit_missing (
    run_id       INTEGER NOT NULL REFERENCES audit_runs(id) ON DELETE CASCADE,
    source_path  TEXT    NOT NULL,
    size         INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_missing_run
    ON audit_missing(run_id);

CREATE TABLE IF NOT EXISTS schema_meta (
    key    TEXT PRIMARY KEY,
    value  TEXT
);

-- v3: EXIF cache populated during scan (opt-in via --exif flag).
-- Row exists only when scan ran with EXIF extraction enabled AND the file
-- is a supported still-image format. Audit JOINs this table to get the
-- disambiguation triple; missing row → fall back to (size, basename, mtime)
-- key, never causes a previously-matched file to become missing.
CREATE TABLE IF NOT EXISTS exif_cache (
    scan_id      INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
    path         TEXT    NOT NULL,
    taken_at     INTEGER,                  -- epoch seconds (local TZ); NULL = parse failed / absent
    camera_make  TEXT,
    camera_model TEXT,
    extracted_at TEXT    NOT NULL,
    PRIMARY KEY (scan_id, path)
);

-- Disambiguation lookup: given a (size, basename) collision, fetch all
-- candidates' EXIF rows in one query.
CREATE INDEX IF NOT EXISTS idx_exif_camera_taken
    ON exif_cache(camera_make, camera_model, taken_at);
"""

SCHEMA_VERSION = "3"


def _apply_session_pragmas(conn: sqlite3.Connection) -> None:
    """PRAGMAs that apply per-connection and need re-setting every open."""
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA cache_size = -64000")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")


def _apply_persistent_pragmas(conn: sqlite3.Connection) -> None:
    """PRAGMAs persisted in the db file; only need to be set once on init."""
    conn.execute("PRAGMA journal_mode = WAL")


def init_schema(conn: sqlite3.Connection) -> None:
    """Create tables/indexes if missing. Persistent PRAGMAs set once here.

    Behaviour matrix:
      - Fresh DB: SCHEMA executescript creates everything at the current
        version; INSERT writes schema_version = SCHEMA_VERSION; migrations
        short-circuit (current == target).
      - Existing pre-v3 DB: SCHEMA's CREATE IF NOT EXISTS adds the new
        v3 tables/indexes; the existing schema_version row stays at "2"
        (INSERT OR IGNORE no-op); run_migrations walks 2 → 3, bumping
        the version row inside a transaction.
    """
    from .migrations import run_migrations

    _apply_persistent_pragmas(conn)
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO schema_meta(key, value) VALUES (?, ?)",
        ("schema_version", SCHEMA_VERSION),
    )
    run_migrations(conn, SCHEMA_VERSION)
    # No explicit commit: with isolation_level=None each statement (and
    # executescript's BEGIN/COMMIT) auto-commits; run_migrations manages
    # its own BEGIN/COMMIT per step.


def get_conn(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open (and initialize if needed) the drivetidy SQLite db.

    Passing None uses config.db_path(). The parent dir is created on demand.
    """
    path = Path(db_path) if db_path is not None else config.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None → autocommit ON; callers wrap bulk writes in
    # explicit BEGIN/COMMIT for speed (see scan._flush).
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    _apply_session_pragmas(conn)
    init_schema(conn)
    return conn
