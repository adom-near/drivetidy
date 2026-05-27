# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 wt and DriveTidy contributors

"""One-click backup of an audit run's missing list.

Closes the audit loop: audit reports what's not backed up, this module
copies those exact files to a destination using rsync. **Strictly
additive** — writes to dest, never touches source. Worst-case failure
is a partial copy that the user re-runs; we do not have any path that
deletes or modifies source media.

Out of scope by design (per v1 decision 2026-05-10):
  - full-disk sync (Carbon Copy Cloner's lane)
  - scheduled / incremental backup
  - cross-platform networking destinations (cloud / SSH)

Design notes:
  - rsync is the engine. We do NOT hand-roll file IO; rsync handles
    partial-write recovery, permission errors, broken pipes, and the
    long tail of edge cases that make naïve copies dangerous.
  - `--files-from=<tmpfile>` feeds rsync the exact missing list. Avoids
    command-line length limits when the audit reports thousands of
    files. Each line is interpreted as a path relative to BOTH the
    source root and dest root → preserves source's directory structure
    under dest, which matches every photographer workflow we've seen.
  - dry-run is the default. The CLI / GUI must explicitly pass
    `apply=True` to actually invoke rsync. Even in apply mode, rsync
    runs with `--itemize-changes` so we can report per-file outcomes.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import db as dbmod


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class BackupReport:
    """Outcome of a backup-missing run.

    `applied=False` means dry-run: `planned_files` is populated, the
    other lists are empty. `applied=True` means rsync ran: `succeeded`
    and `failed` describe per-file outcomes.
    """
    audit_run_id: int
    source_root: str
    dest_root: str
    dest_ident: str
    planned_files: list[tuple[str, int]]   # (relpath, size)
    planned_bytes: int
    applied: bool
    succeeded: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (relpath, message)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _resolve_audit_run(conn: sqlite3.Connection, audit_run_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM audit_runs WHERE id = ?", (audit_run_id,)
    ).fetchone()
    if row is None:
        print(f"錯誤：找不到稽核紀錄 #{audit_run_id}", file=sys.stderr)
        raise SystemExit(1)
    return row


def _resolve_source_root(conn: sqlite3.Connection, run: sqlite3.Row) -> str:
    """Map the audit run's source_kind back to a real filesystem root."""
    if run["source_kind"] == "label":
        scan_id = run["source_scan_id"]
        if scan_id is None:
            print(
                f"錯誤：稽核紀錄 #{run['id']} 標記為 label 模式但沒紀錄 source_scan_id",
                file=sys.stderr,
            )
            raise SystemExit(1)
        scan_row = conn.execute(
            "SELECT root_path FROM scans WHERE id = ?", (scan_id,)
        ).fetchone()
        if scan_row is None:
            print(
                f"錯誤：稽核紀錄 #{run['id']} 對應的 scan 已不在 db 內",
                file=sys.stderr,
            )
            raise SystemExit(1)
        return scan_row["root_path"]
    # path-mode: source_ident IS the path the user typed.
    return run["source_ident"]


def _resolve_dest_root(run: sqlite3.Row, dest_ident: Optional[str]) -> tuple[str, str]:
    """Pick which dest from the audit run's dest_specs JSON.

    Returns (dest_root_path, ident). Raises SystemExit on mismatch."""
    specs = json.loads(run["dest_specs"])
    if not specs:
        print(f"錯誤：稽核紀錄 #{run['id']} 沒有 dest", file=sys.stderr)
        raise SystemExit(1)

    if dest_ident is None:
        if len(specs) > 1:
            choices = ", ".join(s["ident"] for s in specs)
            print(
                f"錯誤：稽核 #{run['id']} 有多個 dest（{choices}）；"
                f"請用 --dest-ident 指定要複製到哪一個",
                file=sys.stderr,
            )
            raise SystemExit(1)
        spec = specs[0]
    else:
        spec = next((s for s in specs if s["ident"] == dest_ident), None)
        if spec is None:
            choices = ", ".join(s["ident"] for s in specs)
            print(
                f"錯誤：稽核 #{run['id']} 沒有 dest_ident={dest_ident!r}；"
                f"可選：{choices}",
                file=sys.stderr,
            )
            raise SystemExit(1)

    if spec["kind"] == "label":
        # The dest was a label; we need the path. Look it up by latest
        # finished scan with that label.
        # (Done outside this helper because it needs the conn.)
        return ("__LABEL__:" + spec["ident"], spec["ident"])
    return (spec["ident"], spec["ident"])


def _resolve_label_to_path(conn: sqlite3.Connection, label: str) -> str:
    row = conn.execute(
        "SELECT root_path FROM scans "
        "WHERE label = ? AND finished_at IS NOT NULL "
        "ORDER BY started_at DESC LIMIT 1",
        (label,),
    ).fetchone()
    if row is None:
        print(
            f"錯誤：找不到 label {label!r} 對應的硬碟路徑（沒完成過的 scan）",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return row["root_path"]


def _load_missing(conn: sqlite3.Connection, audit_run_id: int) -> list[tuple[str, int]]:
    rows = conn.execute(
        "SELECT source_path, size FROM audit_missing WHERE run_id = ? "
        "ORDER BY source_path",
        (audit_run_id,),
    ).fetchall()
    return [(r["source_path"], r["size"]) for r in rows]


def _validate_roots(source_root: str, dest_root: str) -> None:
    """Refuse to proceed if the input shapes are nonsense — dst doesn't
    exist, src and dst are the same path, etc. We do NOT try to second-
    guess rsync's own permission / capacity checks here; rsync surfaces
    those during the actual copy."""
    src = Path(source_root)
    dst = Path(dest_root)
    if not src.is_dir():
        print(f"錯誤：來源路徑不存在或不是資料夾：{source_root}", file=sys.stderr)
        raise SystemExit(1)
    if not dst.is_dir():
        print(f"錯誤：目的地不存在或不是資料夾：{dest_root}", file=sys.stderr)
        raise SystemExit(1)
    try:
        if src.resolve() == dst.resolve():
            print(
                f"錯誤：來源跟目的地是同一個路徑（{source_root}）— 拒絕",
                file=sys.stderr,
            )
            raise SystemExit(1)
    except OSError:
        # If resolve() blows up (firmlink edge cases), let rsync arbitrate.
        pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_backup_missing(
    audit_run_id: int,
    *,
    apply: bool = False,
    dest_ident: Optional[str] = None,
    db_path: Optional[str] = None,
    out=sys.stderr,
) -> BackupReport:
    """Copy every file in the audit run's missing list from source to dest.

    Strictly additive: writes only to dest, never touches source.

    apply=False  → dry-run: returns BackupReport.planned_* without invoking
                   rsync. Use this to preview before committing.
    apply=True   → real rsync. Per-file results land in `succeeded` /
                   `failed`. rsync is invoked once per call with
                   --files-from feeding the missing list.
    dest_ident   → which destination from the audit run to write to.
                   Required when the audit had multiple dests; optional
                   (single-dest audits use that one).
    """
    conn = dbmod.get_conn(db_path)
    try:
        run = _resolve_audit_run(conn, audit_run_id)
        source_root = _resolve_source_root(conn, run)
        dest_root_raw, dest_ident_resolved = _resolve_dest_root(run, dest_ident)
        if dest_root_raw.startswith("__LABEL__:"):
            dest_root = _resolve_label_to_path(conn, dest_root_raw[len("__LABEL__:"):])
        else:
            dest_root = dest_root_raw

        _validate_roots(source_root, dest_root)

        missing = _load_missing(conn, audit_run_id)
        planned_bytes = sum(s for _, s in missing)

        report = BackupReport(
            audit_run_id=audit_run_id,
            source_root=source_root,
            dest_root=dest_root,
            dest_ident=dest_ident_resolved,
            planned_files=missing,
            planned_bytes=planned_bytes,
            applied=False,
        )

        if not missing:
            print(
                f"稽核 #{audit_run_id} 沒有 missing 檔案 — 不需要複製",
                file=out,
            )
            return report

        print(
            f"稽核 #{audit_run_id}：將從 {source_root} 複製 "
            f"{len(missing)} 個檔（共 {planned_bytes} bytes）到 {dest_root}",
            file=out,
        )

        if not apply:
            print(
                "  (dry-run；要真的複製請加 --apply)",
                file=out,
            )
            return report

        report.applied = True
        _run_rsync(report, out=out)
        return report
    finally:
        conn.close()


def _run_rsync(report: BackupReport, *, out) -> None:
    """Invoke rsync with --files-from. Mutates report in-place to fill
    succeeded / failed. Never raises on per-file errors — those are
    captured in report.failed; only catastrophic launch failures
    (rsync binary missing, signal kill) bubble up."""
    rsync = shutil.which("rsync")
    if rsync is None:
        # Treat as a per-call setup failure — every planned file is
        # marked failed so the caller can show a clear error.
        msg = "找不到 rsync 命令（macOS 內建 rsync 應該在 /usr/bin/rsync）"
        print(f"錯誤：{msg}", file=sys.stderr)
        for path, _ in report.planned_files:
            report.failed.append((path, msg))
        return

    # Write the missing list to a tempfile (one path per line). Use
    # NamedTemporaryFile so it's cleaned up even if rsync crashes.
    with tempfile.NamedTemporaryFile(
        "w", delete=False, suffix=".drivetidy-files-from", encoding="utf-8"
    ) as tf:
        for path, _ in report.planned_files:
            tf.write(path + "\n")
        files_from = tf.name

    try:
        cmd = [
            rsync,
            "-av",
            "--files-from", files_from,
            "--itemize-changes",
            # Trailing slash on src ensures rsync copies CONTENTS, not the
            # source dir itself, into dest. Combined with --files-from,
            # each listed path is interpreted as relative to both ends.
            f"{report.source_root.rstrip('/')}/",
            f"{report.dest_root.rstrip('/')}/",
        ]
        proc = subprocess.run(
            cmd,
            shell=False,
            capture_output=True,
            text=True,
            check=False,
        )
        # rsync's stdout contains one line per file copied/skipped when
        # --itemize-changes is on. Format: <YXcstpoguax> path
        # The exit code tells us whether the run as a whole succeeded;
        # individual file errors land in stderr.
        copied: set[str] = set()
        for line in proc.stdout.splitlines():
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            change, path = parts
            # Itemize prefix codes start with [<>ch.*] for create/update;
            # ignore "skipping" / "sending" prose lines from rsync banner.
            if change[:1] in ("<", ">", "c", "h", ".", "*"):
                copied.add(path)

        # Anything in our planned list and seen as copied → success.
        # Anything not seen → failed (catch-all; rsync's stderr explains).
        for path, _ in report.planned_files:
            if path in copied:
                report.succeeded.append(path)
            else:
                report.failed.append((path, _last_line_of(proc.stderr) or f"rsync exit {proc.returncode}"))

        if report.failed:
            print(
                f"  rsync exit {proc.returncode}；{len(report.succeeded)} OK / "
                f"{len(report.failed)} 失敗。錯誤訊息：\n{proc.stderr.strip()}",
                file=out,
            )
        else:
            print(
                f"  ✓ 全部 {len(report.succeeded)} 個檔複製完成",
                file=out,
            )
    finally:
        try:
            os.unlink(files_from)
        except OSError:
            pass


def _last_line_of(text: str) -> str:
    if not text:
        return ""
    for line in reversed(text.splitlines()):
        s = line.strip()
        if s:
            return s
    return ""
