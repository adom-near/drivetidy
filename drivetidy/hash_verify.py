# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 wt and DriveTidy contributors

"""`drivetidy hash` — compute fingerprints for size-collision candidates.

Per internal notes:
  - default algorithm: xxh64 (5-10x faster than md5; collision odds for
    dedup at this scale are <10^-15)
  - --sample-head N: only hash the first N bytes of files larger than N;
    huge time saver for video archives. Pairs that match by sample-head
    are stored with sample_size>0 and reported as 'tentative'; promote
    confirms with full hash.
  - --min-size cutoff drops files below the threshold from candidacy
  - Resume: rows already in `hashes` for the same (scan_id, path, algo,
    sample_size) tuple are skipped; --no-resume rehashes everything.
  - Concurrency: HDD scans run single-threaded (head thrash is anti-
    optimal); SSD scans use a worker pool. Drive type taken from the
    scan's drive_type column.
  - Progress: every 500 files print rate (files/min) + ETA based on
    BYTES (not files), since file-size variance dominates.

The fingerprint is stored as a hex string. xxhash.xxh64.hexdigest() is
16 chars; md5.hexdigest() is 32 chars.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from . import db as dbmod, drive_info


# Streaming chunk size for hash. 8 MB matches internal notes §2.5.
CHUNK = 8 * 1024 * 1024


@dataclass
class HashOptions:
    algo: str = "xxh64"           # "xxh64" | "md5"
    sample_size: int = 0           # 0 = full file; >0 = first N bytes
    min_size: int = 0              # skip files smaller than this
    only_collisions: bool = True   # only hash files whose size collides with another file in scan
    resume: bool = True


@dataclass
class HashSummary:
    scan_id: int
    candidates: int
    skipped_existing: int
    hashed: int
    bytes_read: int
    elapsed: float


# ---------------------------------------------------------------------------
# hash function loading (xxhash if available, fall back to md5)
# ---------------------------------------------------------------------------

def _hasher(algo: str):
    if algo == "xxh64":
        try:
            import xxhash
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "xxhash not installed; pip install -e '.[dev]' or "
                "pass --algo md5"
            ) from e
        return xxhash.xxh64
    if algo == "md5":
        import hashlib
        return hashlib.md5
    raise ValueError(f"unknown algo: {algo}")


def hash_file(path: str, *, algo: str, sample_size: int = 0) -> str:
    """Streaming hash. If sample_size>0, stop after that many bytes."""
    h = _hasher(algo)()
    read = 0
    with open(path, "rb") as f:
        while True:
            want = CHUNK
            if sample_size:
                remaining = sample_size - read
                if remaining <= 0:
                    break
                want = min(CHUNK, remaining)
            chunk = f.read(want)
            if not chunk:
                break
            h.update(chunk)
            read += len(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Candidate selection
# ---------------------------------------------------------------------------

def _candidate_rows(
    conn: sqlite3.Connection, scan_id: int, opts: HashOptions
) -> list[tuple[str, int]]:
    """Return [(path, size), ...] of files we should hash for this scan.

    only_collisions=True (default) keeps only files whose size has at
    least one peer in the same scan — singletons are never duplicates,
    no point hashing.
    """
    if opts.only_collisions:
        rows = conn.execute(
            "SELECT path, size FROM files "
            "WHERE scan_id = ? AND size >= ? "
            "AND size IN ("
            "  SELECT size FROM files WHERE scan_id = ? AND size >= ? "
            "  GROUP BY size HAVING COUNT(*) >= 2"
            ")",
            (scan_id, opts.min_size, scan_id, opts.min_size),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT path, size FROM files WHERE scan_id = ? AND size >= ?",
            (scan_id, opts.min_size),
        ).fetchall()
    return [(r["path"], r["size"]) for r in rows]


def _existing_paths(
    conn: sqlite3.Connection, scan_id: int, algo: str, sample_size: int
) -> set[str]:
    rows = conn.execute(
        "SELECT path FROM hashes "
        "WHERE scan_id = ? AND algo = ? AND sample_size = ?",
        (scan_id, algo, sample_size),
    ).fetchall()
    return {r["path"] for r in rows}


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def run_hash(
    label: str,
    *,
    opts: HashOptions,
    db_path: str | None = None,
    out=sys.stderr,
) -> HashSummary:
    """Hash candidates for the scan associated with `label` (latest finished).

    Raises SystemExit(1) on usage error.
    """
    conn = dbmod.get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT id, root_path, drive_type FROM scans "
            "WHERE label = ? AND finished_at IS NOT NULL "
            "ORDER BY started_at DESC LIMIT 1",
            (label,),
        ).fetchone()
        if row is None:
            print(f"error: no finished scan for label '{label}'", file=sys.stderr)
            raise SystemExit(1)
        scan_id = row["id"]
        root = row["root_path"]
        drive_type_str = row["drive_type"] or "unknown"

        candidates = _candidate_rows(conn, scan_id, opts)
        if opts.resume:
            existing = _existing_paths(conn, scan_id, opts.algo, opts.sample_size)
            todo = [(p, sz) for p, sz in candidates if p not in existing]
            skipped = len(candidates) - len(todo)
        else:
            todo = candidates
            skipped = 0
        total_bytes = sum(
            min(sz, opts.sample_size) if opts.sample_size else sz
            for _, sz in todo
        )

        # Decide concurrency from drive type.
        try:
            dt = drive_info.DriveType(drive_type_str)
        except ValueError:
            dt = drive_info.DriveType.UNKNOWN
        workers = drive_info.recommended_parallelism(dt)

        print(
            f"hash {label} (scan #{scan_id}): "
            f"algo={opts.algo} sample_size={opts.sample_size} "
            f"min_size={opts.min_size}\n"
            f"  candidates={len(candidates)} skipped(existing)={skipped} "
            f"todo={len(todo)} bytes={total_bytes} "
            f"drive_type={drive_type_str} workers={workers}",
            file=out,
        )

        bytes_read = 0
        hashed = 0
        t0 = time.time()
        insert_sql = (
            "INSERT OR REPLACE INTO hashes(scan_id, path, algo, fingerprint, "
            "sample_size, hashed_at) VALUES (?, ?, ?, ?, ?, datetime('now'))"
        )

        # Per-file work unit: read + hash; never touch db inside threads.
        def _work(item):
            rel, sz = item
            full = os.path.join(root, rel)
            try:
                fp = hash_file(full, algo=opts.algo, sample_size=opts.sample_size)
            except (FileNotFoundError, PermissionError, IsADirectoryError):
                return rel, sz, None
            return rel, sz, fp

        try:
            buf = []
            BATCH = 500

            def _flush():
                nonlocal buf
                if not buf:
                    return
                conn.execute("BEGIN")
                try:
                    conn.executemany(insert_sql, buf)
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
                buf = []

            if workers <= 1:
                results = (_work(it) for it in todo)
            else:
                # ThreadPoolExecutor.map preserves order; releases GIL during
                # I/O so SSD parallel reads do scale.
                pool = ThreadPoolExecutor(max_workers=workers)
                results = pool.map(_work, todo, chunksize=8)

            for rel, sz, fp in results:
                if fp is None:
                    continue
                hashed += 1
                bytes_read += min(sz, opts.sample_size) if opts.sample_size else sz
                buf.append((scan_id, rel, opts.algo, fp, opts.sample_size))
                if len(buf) >= BATCH:
                    _flush()
                if hashed % 500 == 0:
                    elapsed = time.time() - t0
                    rate_files = hashed / elapsed if elapsed > 0 else 0
                    rate_bytes = bytes_read / elapsed if elapsed > 0 else 0
                    eta_sec = (
                        (total_bytes - bytes_read) / rate_bytes
                        if rate_bytes > 0
                        else 0.0
                    )
                    print(
                        f"  ...{hashed}/{len(todo)} "
                        f"({rate_files * 60:.0f}/min, "
                        f"{rate_bytes / (1024 * 1024):.1f} MB/s, "
                        f"ETA {eta_sec / 60:.1f} min)",
                        file=out,
                    )
            _flush()
        except KeyboardInterrupt:
            print("\n  interrupted; partial batch committed; resume safe", file=sys.stderr)
            _flush()

        elapsed = time.time() - t0
        return HashSummary(
            scan_id=scan_id,
            candidates=len(candidates),
            skipped_existing=skipped,
            hashed=hashed,
            bytes_read=bytes_read,
            elapsed=elapsed,
        )
    finally:
        conn.close()
