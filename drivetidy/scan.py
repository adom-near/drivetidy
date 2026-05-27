# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 wt and DriveTidy contributors

"""`drivetidy scan` implementation.

Flow:
  1. Resolve drive type (system_profiler → hdd/ssd/unknown; --type overrides).
  2. Insert a scans row with started_at, get scan_id.
  3. Stream FileEntry from scan_backend.iter_files, batch-insert into files.
  4. Update scans.finished_at and file_count.

On Ctrl+C: whatever made it to the last committed batch stays; scan is
marked incomplete (no finished_at). Re-running creates a fresh scan row;
use `drivetidy status` to see both and clean up later.
"""

from __future__ import annotations

import datetime
import sys
import sqlite3
import time
from pathlib import Path

from . import config, db as dbmod, drive_info
from . import exif as exif_mod
from .scan_backend import iter_files, FileEntry, BackendError


# v1: JPEG only. RAW formats deferred to v1.5 — adding them here is a
# one-line change once `exif.read_exif` learns the format. Lowercase, with
# the dot, for direct suffix comparison.
_EXIF_EXTRACT_EXTS = {".jpg", ".jpeg", ".jpe"}


BATCH_SIZE = 5000   # insert-per-transaction; bigger = fewer fsync, smaller = more granular resume


def _derive_label(root: str, explicit: str | None) -> str:
    if explicit:
        return explicit
    name = Path(root).name
    return name or root


def _sanitize_label(label: str) -> str:
    """Strip control characters (cli.py reviewer nit: label echoed in terminal)."""
    return "".join(ch for ch in label if ch.isprintable())


def _wants_exif(rel_path: str) -> bool:
    """Should we attempt EXIF extraction for this file? Cheap suffix check
    so we don't pay an open() cost on the 99% of files (videos, audio,
    docs) where Pillow would just return None."""
    return Path(rel_path).suffix.lower() in _EXIF_EXTRACT_EXTS


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _resolve_parent_scan_id(conn: sqlite3.Connection, label: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM scans WHERE label = ? AND finished_at IS NOT NULL AND scan_mode = 'full' "
        "ORDER BY started_at DESC LIMIT 1",
        (label,),
    ).fetchone()
    return row["id"] if row else None


def run_scan(
    root: str,
    *,
    label: str | None = None,
    drive_type_override: str = "auto",
    incremental: bool = False,
    backend_pref: str = "fd",
    db_path: str | None = None,
    progress_every: int = 10_000,
    out=sys.stderr,
    extract_exif: bool = False,
) -> int:
    """Run a scan. Returns scan_id (>0). Raises SystemExit(1) on usage error.

    extract_exif: opt-in EXIF extraction during scan. When True, every
    JPEG (case-insensitive `.jpg/.jpeg/.jpe`) gets its EXIF head read
    via Pillow and cached into `exif_cache`; audit later JOINs that
    table to disambiguate (size, basename) collisions. Default OFF
    because each JPEG open adds a seek on HDD — opting in trades the
    "113 s for 5 TB" headline for stronger camera-collision matching.
    Failures (corrupt JPEG, permission denied, format Pillow rejects)
    are silently dropped: the row simply doesn't get cached, audit
    falls through to the legacy mtime key.
    """
    root_abs = str(Path(root).resolve())
    if not Path(root_abs).is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        raise SystemExit(1)

    label_raw = _derive_label(root_abs, label)
    label_clean = _sanitize_label(label_raw)

    info = drive_info.resolve(root_abs, drive_type_override)
    threads = drive_info.recommended_parallelism(info.drive_type)

    conn = dbmod.get_conn(db_path)
    try:
        scan_mode = "full"
        parent_scan_id: int | None = None
        if incremental:
            parent_scan_id = _resolve_parent_scan_id(conn, label_clean)
            if parent_scan_id is None:
                print(
                    f"warning: no prior full scan found for label '{label_clean}'; "
                    f"falling back to full scan",
                    file=sys.stderr,
                )
            else:
                scan_mode = "incremental"

        started = _now()
        cur = conn.execute(
            "INSERT INTO scans(label, root_path, drive_type, started_at, scan_mode, parent_scan_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (label_clean, root_abs, info.drive_type.value, started, scan_mode, parent_scan_id),
        )
        scan_id = cur.lastrowid
        print(
            f"scan #{scan_id}: {label_clean!r} at {root_abs}  "
            f"drive_type={info.drive_type.value} (source={info.source}) "
            f"threads={threads} backend={backend_pref} mode={scan_mode}",
            file=out,
        )

        # INSERT OR IGNORE: tolerate fd/rclone yielding the same path twice
        # (circular symlink, mount loop), but never silently overwrite an
        # already-recorded entry — drop the duplicate (first wins).
        insert_sql = (
            "INSERT OR IGNORE INTO files(scan_id, path, path_raw, size, mtime) "
            "VALUES (?, ?, ?, ?, ?)"
        )
        # Same INSERT OR IGNORE discipline for EXIF: a JPEG yielded twice
        # (mount loop) gets exactly one cache row, first read wins.
        exif_insert_sql = (
            "INSERT OR IGNORE INTO exif_cache"
            "(scan_id, path, taken_at, camera_make, camera_model, extracted_at) "
            "VALUES (?, ?, ?, ?, ?, ?)"
        )
        batch: list[tuple] = []
        exif_batch: list[tuple] = []
        count = 0
        exif_count = 0
        t_start = time.time()

        # Outcome bookkeeping. We must NEVER mark finished_at unless the
        # backend produced a complete listing; downstream audit treats a
        # scan with finished_at as authoritative ("all files in this drive
        # are these"), so persisting an interrupted/failed scan as
        # complete would mask missing backups as already-backed-up.
        interrupted = False
        backend_failure: BackendError | None = None
        try:
            for entry in iter_files(root_abs, backend_pref=backend_pref, hdd_parallelism=threads):
                path_raw = entry.rel_path_raw if entry.rel_path_raw != entry.rel_path else None
                batch.append((scan_id, entry.rel_path, path_raw, entry.size, entry.mtime))
                count += 1

                if extract_exif and _wants_exif(entry.rel_path):
                    full_path = str(Path(root_abs) / entry.rel_path)
                    key = exif_mod.read_exif(full_path)
                    if key is not None:
                        exif_batch.append((
                            scan_id, entry.rel_path,
                            key.taken_at, key.camera_make, key.camera_model,
                            _now(),
                        ))
                        exif_count += 1

                if len(batch) >= BATCH_SIZE:
                    _flush(conn, insert_sql, batch)
                    batch.clear()
                    if exif_batch:
                        _flush(conn, exif_insert_sql, exif_batch)
                        exif_batch.clear()
                if count % progress_every == 0:
                    elapsed = time.time() - t_start
                    rate = count / elapsed if elapsed > 0 else 0
                    exif_note = f", {exif_count} exif" if extract_exif else ""
                    print(f"  ...{count:>10} files indexed ({rate:.0f}/s{exif_note})", file=out)
        except KeyboardInterrupt:
            interrupted = True
            print(
                "\n interrupted; committing partial batch and marking scan INCOMPLETE",
                file=sys.stderr,
            )
        except BackendError as e:
            backend_failure = e
            print(
                f"\n backend failed: {e}; committing partial batch and "
                f"marking scan INCOMPLETE",
                file=sys.stderr,
            )

        if batch:
            _flush(conn, insert_sql, batch)
        if exif_batch:
            _flush(conn, exif_insert_sql, exif_batch)
            exif_batch.clear()

        if not interrupted and backend_failure is None:
            finished = _now()
            conn.execute(
                "UPDATE scans SET finished_at = ?, file_count = ? WHERE id = ?",
                (finished, count, scan_id),
            )
            elapsed = time.time() - t_start
            exif_note = f", {exif_count} exif rows cached" if extract_exif else ""
            print(
                f"scan #{scan_id} done: {count} files in {elapsed:.1f}s "
                f"({count/elapsed:.0f}/s avg){exif_note}",
                file=out,
            )
        else:
            # Persist file_count for diagnostic visibility but leave
            # finished_at NULL so audit / status know this scan is partial.
            conn.execute(
                "UPDATE scans SET file_count = ? WHERE id = ?",
                (count, scan_id),
            )
            elapsed = time.time() - t_start
            reason = "interrupted" if interrupted else "backend-failed"
            print(
                f"scan #{scan_id} INCOMPLETE ({reason}): "
                f"{count} files in {elapsed:.1f}s before stop",
                file=out,
            )

        if backend_failure is not None:
            # Surface to caller (CLI translates SystemExit; GUI 400). We
            # raise after committing the partial batch so the diagnostic
            # row is still queryable.
            raise backend_failure
        return scan_id
    finally:
        conn.close()


def _flush(conn: sqlite3.Connection, sql: str, batch: list[tuple]) -> None:
    conn.execute("BEGIN")
    try:
        conn.executemany(sql, batch)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
