# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 wt and DriveTidy contributors

"""CLI entry point for drivetidy.

This file only handles argparse dispatch; each subcommand's logic lives
in its own module.

SAFETY: any subcommand that mutates files defaults to dry-run.
"""

from __future__ import annotations

import argparse
import sys

from . import config, db as dbmod


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="drivetidy",
        description="Backup integrity audit: verify your photos and videos are actually backed up",
    )
    p.add_argument("--db", help="Path to SQLite db (default: ~/.drivetidy/drivetidy.db)")
    p.add_argument("--rclone", help="Path to rclone binary (default: auto-detect)")
    p.add_argument("-v", "--verbose", action="count", default=0)

    sub = p.add_subparsers(dest="cmd", required=True)

    # scan
    s_scan = sub.add_parser("scan", help="Index a drive into the db")
    s_scan.add_argument("path", help="Drive root path, e.g. /Volumes/Seagate")
    s_scan.add_argument("--label", help="Human-readable label (default: volume name)")
    s_scan.add_argument(
        "--type", choices=["hdd", "ssd", "auto"], default="auto",
        help="Drive type; auto uses system_profiler, falls back to hdd",
    )
    s_scan.add_argument("--incremental", action="store_true")
    s_scan.add_argument("--backend", choices=["fd", "rclone"], default="fd")
    s_scan.add_argument(
        "--exif", dest="extract_exif", action="store_true",
        help="Cache JPEG EXIF (camera, taken_at) during scan so audit can "
             "disambiguate same-name multi-camera collisions. Off by default "
             "because it adds a per-JPEG file open (HDD seek). Worth turning "
             "on when you mix Sony / Canon / GoPro / phone shots.",
    )
    s_scan.set_defaults(extract_exif=False)

    # compare
    s_cmp = sub.add_parser("compare", help="Size+path comparison between two scans")
    s_cmp.add_argument("label_a")
    s_cmp.add_argument("label_b")
    s_cmp.add_argument("--min-size", default="100K")
    # NB: removed --size-only flag (was always-True; if/when content-compare is
    # added, expose --full-content with explicit semantics).

    # hash
    s_hash = sub.add_parser("hash", help="Compute fingerprints for size-collision candidates")
    s_hash.add_argument("label")
    s_hash.add_argument("--algo", choices=["xxh64", "md5"], default="xxh64")
    s_hash.add_argument("--sample-head", help="Only hash the first N bytes of large files, e.g. 8M")
    s_hash.add_argument("--min-size", default="100K")
    s_hash.add_argument(
        "--resume", action=argparse.BooleanOptionalAction, default=True,
        help="Skip files already in hashes table; use --no-resume to rehash all",
    )

    # audit (Phase 2 — primary feature)
    s_aud = sub.add_parser(
        "audit",
        help="Backup completeness audit: are <source>'s files on the <dest>s?",
    )
    s_aud.add_argument(
        "source",
        help="Source: a previously-scanned label, or a directory path "
             "(detected when it contains '/' or starts with '~'). "
             "Ambiguous names resolve to label first; pass './name' to "
             "force path resolution.",
    )
    s_aud.add_argument(
        "--against", required=True,
        help="Comma-separated destinations. Each entry is either a label "
             "(SQL mode) or a directory path (live-walked). Mixed lists OK.",
    )
    s_aud.add_argument(
        "--early-stop", action=argparse.BooleanOptionalAction, default=True,
        help="Stop walking a live destination once all source needles are "
             "covered. On (default) for typical SD-card audit; --no-early-stop "
             "for full-coverage analysis.",
    )
    s_aud.add_argument(
        "--mtime-match", action=argparse.BooleanOptionalAction, default=True,
        help="Use file mtime as part of the match key (defends against "
             "camera basename + size collisions). Default on. Use "
             "--no-mtime-match when your backup pipeline strips mtime "
             "(some rsync invocations, restored-from-archive workflows).",
    )
    s_aud.add_argument(
        "--mtime-window", type=int, default=2, metavar="SECONDS",
        help="Match tolerance window in seconds when --mtime-match is on. "
             "Default 2 (covers FAT32's 2s resolution + small DST drift). "
             "Increase if your destination filesystem rounds mtime more "
             "aggressively.",
    )
    s_aud.add_argument(
        "--max-walk-ratio", type=float, default=1.5, metavar="N",
        help="Early-stop fallback: cap each destination walk at "
             "N × source-file-count. Without this, missing files force "
             "a full HDD walk (defeats the 113-second headline). "
             "Set 0 or negative to disable (same as --no-early-stop in "
             "spirit).",
    )
    s_aud.add_argument(
        "--min-size", default="0",
        help="Skip files smaller than this (e.g. 1M, 100K). Default 0.",
    )
    s_aud.add_argument(
        "--exclude", default=None,
        help="Python regex; if it matches anywhere in a source file's "
             "relative path (re.search), that file is skipped.",
    )
    s_aud.add_argument(
        "--out", default=None,
        help="Write the HTML report to PATH. Groups missing files by source "
             "folder and sorts by size, biggest first.",
    )

    # backup-missing — close the audit loop. Copies an audit's missing
    # list from source to dest using rsync. Strictly additive, dry-run
    # by default; --apply gates the actual rsync call.
    s_bm = sub.add_parser(
        "backup-missing",
        help="Copy an audit run's missing files from source to dest (additive)",
    )
    s_bm.add_argument("audit_run_id", type=int, help="The audit run id to back up from")
    s_bm.add_argument(
        "--dest-ident",
        help="Which destination from the audit to copy to (required if "
             "the audit had multiple dests)",
    )
    s_bm.add_argument(
        "--apply", action="store_true",
        help="Actually run rsync. Without this, prints the plan only.",
    )

    # gui
    s_gui = sub.add_parser("gui", help="Launch the local web GUI (browser-based)")
    s_gui.add_argument("--host", default="127.0.0.1")
    s_gui.add_argument("--port", type=int, default=8765)
    s_gui.add_argument(
        "--no-open", action="store_true",
        help="Don't open the browser automatically",
    )

    # status
    sub.add_parser("status", help="List scans in the db")

    # db
    s_db = sub.add_parser("db", help="Database inspection commands")
    s_db_sub = s_db.add_subparsers(dest="db_cmd", required=True)
    s_db_sub.add_parser("path", help="Print the db file path")

    return p


def _resolve_db_path(args: argparse.Namespace):
    return args.db if getattr(args, "db", None) else config.db_path()


def cmd_status(args: argparse.Namespace) -> int:
    """List scans and audit runs in the configured db."""
    path = _resolve_db_path(args)
    conn = dbmod.get_conn(path)
    try:
        print(f"DB: {path}")
        scans = conn.execute(
            "SELECT id, label, root_path, drive_type, started_at, finished_at, "
            "file_count, scan_mode, parent_scan_id "
            "FROM scans ORDER BY started_at DESC"
        ).fetchall()
        print(f"\nScans ({len(scans)}):")
        if not scans:
            print("  (none)")
        else:
            for s in scans:
                status = "done" if s["finished_at"] else "in-progress"
                parent = f" (incremental of #{s['parent_scan_id']})" if s["parent_scan_id"] else ""
                print(
                    f"  #{s['id']:<3} {s['label']:<20} "
                    f"{(s['drive_type'] or 'unknown'):<8} "
                    f"mode={s['scan_mode']:<11}{parent} "
                    f"files={s['file_count']:>8}  {status}  {s['started_at']}"
                )

        audits = conn.execute(
            "SELECT id, source_ident, source_kind, dest_specs, total_files, "
            "matched_count, missing_count, missing_bytes, ran_at "
            "FROM audit_runs ORDER BY ran_at DESC"
        ).fetchall()
        print(f"\nAudit runs ({len(audits)}):")
        if not audits:
            print("  (none)")
        else:
            import json as _json
            for a in audits:
                try:
                    specs = _json.loads(a["dest_specs"])
                    dest_summary = ", ".join(d.get("ident", "?") for d in specs)
                except Exception:
                    dest_summary = a["dest_specs"]
                pct = (
                    100.0 * a["matched_count"] / a["total_files"]
                    if a["total_files"] else 0.0
                )
                print(
                    f"  #{a['id']:<3} {a['source_ident']:<20} → {dest_summary:<30} "
                    f"matched={a['matched_count']:>6}/{a['total_files']:<6} "
                    f"({pct:5.1f}%) missing={a['missing_count']:<5} {a['ran_at']}"
                )
    finally:
        conn.close()
    return 0


def cmd_db_path(args: argparse.Namespace) -> int:
    print(_resolve_db_path(args))
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    from . import scan as scan_mod
    # run_scan returns a scan_id (>0) on success and raises SystemExit(1) on
    # usage error (e.g. path not a directory). Either branch maps cleanly to
    # the CLI exit code without further wrapping logic.
    scan_mod.run_scan(
        args.path,
        label=args.label,
        drive_type_override=args.type,
        incremental=args.incremental,
        backend_pref=args.backend,
        extract_exif=args.extract_exif,
        db_path=_resolve_db_path(args) if args.db else None,
    )
    return 0


def cmd_hash(args: argparse.Namespace) -> int:
    from . import hash_verify
    from .utils import parse_size

    sample = parse_size(args.sample_head) if args.sample_head else 0
    opts = hash_verify.HashOptions(
        algo=args.algo,
        sample_size=sample,
        min_size=parse_size(args.min_size),
        resume=args.resume,
    )
    summary = hash_verify.run_hash(
        args.label,
        opts=opts,
        db_path=_resolve_db_path(args) if args.db else None,
    )
    print(
        f"hashed {summary.hashed} files / "
        f"{summary.bytes_read / (1024 ** 2):.1f} MB in {summary.elapsed:.1f}s",
        file=sys.stderr,
    )
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    try:
        from .gui.app import serve, _pick_free_port
    except ImportError as e:
        print(
            f"GUI dependencies missing: {e}. Run: pip install -e '.[gui]'",
            file=sys.stderr,
        )
        return 1
    actual_port = _pick_free_port(args.host, args.port)
    if actual_port != args.port:
        print(f"port {args.port} in use; using {actual_port}", file=sys.stderr)
    if not args.no_open:
        import threading
        import time
        import webbrowser
        url = f"http://{args.host}:{actual_port}/"
        threading.Thread(
            target=lambda: (time.sleep(0.6), webbrowser.open(url)),
            daemon=True,
        ).start()
    serve(
        host=args.host,
        port=actual_port,
        db_path=_resolve_db_path(args) if args.db else None,
        port_already_picked=True,
    )
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    from . import compare as compare_mod
    from .utils import parse_size

    compare_mod.run_compare(
        args.label_a,
        args.label_b,
        min_size=parse_size(args.min_size),
        db_path=_resolve_db_path(args) if args.db else None,
    )
    return 0


def cmd_backup_missing(args: argparse.Namespace) -> int:
    from . import backup as backup_mod
    report = backup_mod.run_backup_missing(
        args.audit_run_id,
        apply=args.apply,
        dest_ident=args.dest_ident,
        db_path=_resolve_db_path(args) if args.db else None,
    )
    if report.applied and report.failed:
        # Treat any per-file failure as overall non-zero exit so shell
        # users notice rsync didn't fully succeed; succeeded files
        # still landed at dest, the report explains which didn't.
        return 2
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    from . import audit as audit_mod, report as report_mod
    from .utils import parse_size

    dest_idents = [s.strip() for s in args.against.split(",") if s.strip()]
    result = audit_mod.run_audit(
        args.source,
        dest_idents,
        min_size=parse_size(args.min_size),
        exclude_pattern=args.exclude,
        early_stop=args.early_stop,
        mtime_match=args.mtime_match,
        mtime_window_seconds=args.mtime_window,
        max_walk_ratio=(args.max_walk_ratio if args.max_walk_ratio > 0 else None),
        db_path=_resolve_db_path(args) if args.db else None,
    )

    if args.out:
        path = report_mod.write_report(result, args.out)
        print(f"\n  HTML report: {path}", file=sys.stderr)

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "db" and args.db_cmd == "path":
        return cmd_db_path(args)
    if args.cmd == "scan":
        return cmd_scan(args)
    if args.cmd == "gui":
        return cmd_gui(args)
    if args.cmd == "compare":
        return cmd_compare(args)
    if args.cmd == "hash":
        return cmd_hash(args)
    if args.cmd == "audit":
        return cmd_audit(args)
    if args.cmd == "backup-missing":
        return cmd_backup_missing(args)

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
