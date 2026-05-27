# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 wt and DriveTidy contributors

"""`drivetidy compare` — size-only diff between two scans.

Pure SQL on the existing `files` table. No hashing — this is the cheap
"have these two drives diverged?" check that runs in seconds even on
hundreds of thousands of files.

Output (4-row summary):
  match     same path + same size in both
  differ    same path, different size
  only_a    path exists only in scan A
  only_b    path exists only in scan B

Note: comparison is path-keyed (NFC), unlike audit which is
basename+size keyed. compare is for "is this drive in sync with that
one?", audit is for "is every source file present somewhere?".
"""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass

from . import db as dbmod
from .utils import human_size


@dataclass
class CompareResult:
    label_a: str
    label_b: str
    scan_a: int
    scan_b: int
    match: int
    differ: int
    only_a: int
    only_b: int
    differ_examples: list[tuple[str, int, int]]   # (path, size_a, size_b)
    only_a_examples: list[tuple[str, int]]
    only_b_examples: list[tuple[str, int]]


def _latest_finished_scan(conn: sqlite3.Connection, label: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM scans WHERE label = ? AND finished_at IS NOT NULL "
        "ORDER BY started_at DESC LIMIT 1",
        (label,),
    ).fetchone()
    return row["id"] if row else None


def run_compare(
    label_a: str,
    label_b: str,
    *,
    min_size: int = 0,
    db_path: str | None = None,
    examples: int = 10,
    out=sys.stderr,
) -> CompareResult:
    """Run size-only compare. Raises SystemExit(1) on usage error."""
    conn = dbmod.get_conn(db_path)
    try:
        sa = _latest_finished_scan(conn, label_a)
        sb = _latest_finished_scan(conn, label_b)
        if sa is None:
            print(f"error: no finished scan for label '{label_a}'", file=sys.stderr)
            raise SystemExit(1)
        if sb is None:
            print(f"error: no finished scan for label '{label_b}'", file=sys.stderr)
            raise SystemExit(1)
        if sa == sb:
            print(f"error: '{label_a}' and '{label_b}' resolve to the same scan", file=sys.stderr)
            raise SystemExit(1)

        # match: same path + same size
        match = conn.execute(
            "SELECT COUNT(*) FROM files a JOIN files b "
            "ON a.path = b.path AND a.size = b.size "
            "WHERE a.scan_id = ? AND b.scan_id = ? AND a.size >= ?",
            (sa, sb, min_size),
        ).fetchone()[0]

        # differ: same path, different size
        differ_rows = conn.execute(
            "SELECT a.path, a.size, b.size FROM files a JOIN files b "
            "ON a.path = b.path "
            "WHERE a.scan_id = ? AND b.scan_id = ? "
            "AND a.size != b.size AND a.size >= ?",
            (sa, sb, min_size),
        ).fetchall()
        differ = len(differ_rows)

        # only_a: in A but not in B (any size)
        only_a_rows = conn.execute(
            "SELECT a.path, a.size FROM files a "
            "LEFT JOIN files b ON a.path = b.path AND b.scan_id = ? "
            "WHERE a.scan_id = ? AND b.path IS NULL AND a.size >= ? "
            "ORDER BY a.size DESC LIMIT ?",
            (sb, sa, min_size, examples),
        ).fetchall()
        only_a_count = conn.execute(
            "SELECT COUNT(*) FROM files a "
            "LEFT JOIN files b ON a.path = b.path AND b.scan_id = ? "
            "WHERE a.scan_id = ? AND b.path IS NULL AND a.size >= ?",
            (sb, sa, min_size),
        ).fetchone()[0]

        only_b_rows = conn.execute(
            "SELECT b.path, b.size FROM files b "
            "LEFT JOIN files a ON a.path = b.path AND a.scan_id = ? "
            "WHERE b.scan_id = ? AND a.path IS NULL AND b.size >= ? "
            "ORDER BY b.size DESC LIMIT ?",
            (sa, sb, min_size, examples),
        ).fetchall()
        only_b_count = conn.execute(
            "SELECT COUNT(*) FROM files b "
            "LEFT JOIN files a ON a.path = b.path AND a.scan_id = ? "
            "WHERE b.scan_id = ? AND a.path IS NULL AND b.size >= ?",
            (sa, sb, min_size),
        ).fetchone()[0]

        result = CompareResult(
            label_a=label_a,
            label_b=label_b,
            scan_a=sa,
            scan_b=sb,
            match=match,
            differ=differ,
            only_a=only_a_count,
            only_b=only_b_count,
            differ_examples=[(r["path"], r["size"], r[2]) for r in differ_rows[:examples]],
            only_a_examples=[(r["path"], r["size"]) for r in only_a_rows],
            only_b_examples=[(r["path"], r["size"]) for r in only_b_rows],
        )

        _print_report(result, out=out)
        return result
    finally:
        conn.close()


def _print_report(r: CompareResult, *, out) -> None:
    print(
        f"compare {r.label_a} (#{r.scan_a}) vs {r.label_b} (#{r.scan_b})\n"
        f"  match:   {r.match}\n"
        f"  differ:  {r.differ}\n"
        f"  only {r.label_a}: {r.only_a}\n"
        f"  only {r.label_b}: {r.only_b}",
        file=out,
    )

    def _show(title: str, rows: list, fmt):
        if not rows:
            return
        print(f"\n  {title} (top {len(rows)} by size):", file=out)
        for row in rows:
            print(f"    {fmt(row)}", file=out)

    _show(
        "differ",
        r.differ_examples,
        lambda x: f"{x[0]}  [{r.label_a}={human_size(x[1])} | {r.label_b}={human_size(x[2])}]",
    )
    _show(
        f"only {r.label_a}",
        r.only_a_examples,
        lambda x: f"{x[0]}  [{human_size(x[1])}]",
    )
    _show(
        f"only {r.label_b}",
        r.only_b_examples,
        lambda x: f"{x[0]}  [{human_size(x[1])}]",
    )
