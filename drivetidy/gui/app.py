# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 wt and DriveTidy contributors

"""FastAPI app: DriveTidy GUI shell.

Endpoints (v1, audit-only):
  GET  /                       → main page
  GET  /api/drives             → mounted volumes + drive_info
  GET  /api/labels             → already-scanned labels (with stats)
  POST /api/scan               → kick off a scan synchronously (v1)
  POST /api/audit              → run audit synchronously, return run_id + result
  GET  /api/audit/{run_id}     → fetch a stored audit run
  GET  /static/<asset>         → CSS / JS bundles
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from .. import audit as audit_mod
from .. import db as dbmod
from .. import drive_info
from .. import scan as scan_mod
from ..scan_backend import BackendError, BackendNotAvailable
from ..utils import human_size, parse_size


_HERE = Path(__file__).parent
_INDEX_PATH = _HERE / "templates" / "index.html"

DEFAULT_PORT = 8765
PORT_FALLBACKS = (8765, 8766, 8767, 8768)


def _default_allowed_hosts(port: int) -> set[str]:
    return {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}


def _scan_extra_roots() -> list[str]:
    """Extra root prefixes from `DRIVETIDY_SCAN_EXTRA_ROOTS` (colon-sep).
    Used by tests to allow /tmp scratch dirs without weakening the
    default allowlist seen by real users."""
    raw = os.environ.get("DRIVETIDY_SCAN_EXTRA_ROOTS", "")
    return [p for p in raw.split(":") if p]


def _is_scan_path_allowed(path: str, extra_roots: list[str] | None = None) -> bool:
    """Allowlist for /api/scan: must point at an external volume or a
    non-hidden subtree of the user's home. Refuses /etc, /System,
    `~/.drivetidy`, etc. Symlinks are NOT resolved (a user pointing at
    `/Volumes/MyDrive` should pass even if it firmlinks elsewhere); we
    only normalise `..` to block traversal."""
    abs_path = os.path.abspath(os.path.expanduser(path))
    home = os.path.expanduser("~").rstrip("/")

    if abs_path == "/Volumes" or abs_path.startswith("/Volumes/"):
        return True

    if home and (abs_path == home or abs_path.startswith(home + "/")):
        rel = os.path.relpath(abs_path, home)
        first = rel.split(os.sep)[0]
        if first and first != "." and not first.startswith("."):
            return True

    for root in (extra_roots or []):
        root_abs = os.path.abspath(os.path.expanduser(root))
        if abs_path == root_abs or abs_path.startswith(root_abs + "/"):
            return True

    return False


class _HostGuard(BaseHTTPMiddleware):
    """Reject requests whose Host header is not in the allow-list.

    Defends against DNS-rebinding attacks: a malicious site that resolves
    its own hostname to 127.0.0.1 cannot reach this server even though
    the browser will think the request is same-origin (the kernel sends
    the packet to localhost; we look at the HTTP Host header and refuse).
    """

    def __init__(self, app, allowed: Optional[set[str]]):
        super().__init__(app)
        self.allowed = allowed  # None → permissive (tests / dev)

    async def dispatch(self, request, call_next):
        if self.allowed is not None:
            host = request.headers.get("host", "").lower()
            if host not in self.allowed:
                return JSONResponse(
                    {"detail": f"host '{host}' not allowed"}, status_code=421
                )
        return await call_next(request)


# Pydantic models declared at module scope so FastAPI's body-introspection
# treats them as JSON bodies (not query). Closure-defined models trip up
# starlette's signature inspection in some versions.

class ScanReq(BaseModel):
    path: str
    label: Optional[str] = None
    type: str = "auto"
    backend: str = "fd"
    exif: bool = False  # opt-in EXIF cache; off by default to preserve scan speed


class BackupMissingReq(BaseModel):
    audit_run_id: int
    apply: bool = False                       # default dry-run; UI flips after confirm
    dest_ident: Optional[str] = None          # required when audit had multi-dests


class AuditReq(BaseModel):
    source: str = Field(..., description="label name or absolute path")
    against: list[str] = Field(..., min_length=1)
    min_size: str = "0"
    early_stop: bool = True
    exclude: Optional[str] = None
    mtime_match: bool = True
    mtime_window: int = Field(2, ge=0, le=60)
    max_walk_ratio: Optional[float] = Field(1.5, ge=0)


def create_app(
    db_path: Optional[str] = None,
    *,
    allowed_hosts: Optional[set[str]] = None,
) -> FastAPI:
    """Build the FastAPI instance. db_path None → default config.db_path().

    ``allowed_hosts`` defends against DNS rebinding: any request whose Host
    header is not in this set is rejected with 421. The default is the
    localhost bindings the launcher uses; tests can pass None or an
    explicit set.
    """
    app = FastAPI(title="DriveTidy", version="0.1.0")
    app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")
    app.add_middleware(_HostGuard, allowed=allowed_hosts)

    # Stash the db_path on the app so handlers can reach it.
    app.state.db_path = db_path

    @app.get("/", response_class=HTMLResponse)
    def index():
        # Read on every request — small file, supports hot-reload in dev.
        return HTMLResponse(_INDEX_PATH.read_text(encoding="utf-8"))

    @app.get("/api/drives")
    def list_drives():
        """Enumerate /Volumes and report drive_info for each mounted disk."""
        out = []
        volumes = Path("/Volumes")
        if not volumes.is_dir():
            return out
        for entry in sorted(volumes.iterdir()):
            if not entry.is_dir():
                continue
            try:
                info = drive_info.resolve(str(entry))
            except Exception:
                continue
            try:
                next(entry.iterdir(), None)  # one syscall — raises if blocked
                accessible = True
            except (PermissionError, OSError):
                accessible = False
            out.append({
                "path": str(entry),
                "name": entry.name,
                "drive_type": info.drive_type.value,
                "drive_type_source": info.source,
                "device": info.device,
                "accessible": accessible,
            })
        return out

    @app.get("/api/labels")
    def list_labels():
        conn = dbmod.get_conn(app.state.db_path)
        try:
            rows = conn.execute(
                "SELECT id, label, root_path, drive_type, started_at, "
                "finished_at, file_count, scan_mode "
                "FROM scans WHERE finished_at IS NOT NULL "
                "ORDER BY started_at DESC"
            ).fetchall()
            seen: set[str] = set()
            out = []
            for r in rows:
                if r["label"] in seen:
                    continue
                seen.add(r["label"])
                out.append({
                    "label": r["label"],
                    "scan_id": r["id"],
                    "root_path": r["root_path"],
                    "drive_type": r["drive_type"],
                    "file_count": r["file_count"],
                    "scanned_at": r["finished_at"],
                })
            return out
        finally:
            conn.close()

    @app.post("/api/scan")
    def start_scan(req: ScanReq):
        if not _is_scan_path_allowed(req.path, _scan_extra_roots()):
            raise HTTPException(
                status_code=400,
                detail=(
                    "掃描路徑不被允許。請選擇外接磁碟（/Volumes/...）"
                    "或家目錄下的非隱藏資料夾。"
                ),
            )
        try:
            scan_id = scan_mod.run_scan(
                req.path,
                label=req.label,
                drive_type_override=req.type,
                backend_pref=req.backend,
                extract_exif=req.exif,
                db_path=app.state.db_path,
                out=io.StringIO(),
            )
        except SystemExit as e:
            raise HTTPException(status_code=400, detail=f"scan failed (exit {e.code})")
        except BackendNotAvailable as e:
            raise HTTPException(status_code=400, detail=f"scan backend missing: {e}")
        except BackendError as e:
            # Backend started but failed mid-scan; the scan row is in the
            # DB marked incomplete (no finished_at) so the user can see it
            # via /status, but audit will refuse to use it.
            raise HTTPException(status_code=502, detail=f"scan failed: {e}")
        return {"scan_id": scan_id}

    @app.get("/api/state")
    def get_state():
        return {"state": "ACTIVE"}

    @app.post("/api/pick-folder")
    def pick_folder():
        """Open the native macOS folder picker; return the chosen POSIX path.

        Uses osascript so we don't need pyobjc / Tk bundled. Blocks until
        the user picks or cancels (osascript prints "User canceled" on
        stderr with rc != 0). The host-guard middleware already restricts
        this endpoint to localhost; cancelled returns 200 with cancelled=true
        so the front-end can ignore quietly.
        """
        try:
            result = subprocess.run(
                ["osascript", "-e", "POSIX path of (choose folder)"],
                capture_output=True, text=True, timeout=300,
            )
        except (FileNotFoundError, subprocess.SubprocessError, OSError) as e:
            raise HTTPException(500, f"無法叫出資料夾選擇器：{e}")

        if result.returncode != 0:
            err = (result.stderr or "").strip()
            if "User canceled" in err or "(-128)" in err:
                return {"cancelled": True}
            raise HTTPException(500, f"選擇器錯誤：{err or 'osascript 失敗'}")

        path = result.stdout.strip()
        # osascript's "POSIX path of" adds a trailing slash on directories.
        if path.endswith("/") and len(path) > 1:
            path = path[:-1]
        return {"path": path}

    @app.post("/api/audit")
    def run_audit(req: AuditReq):
        err_buf = io.StringIO()
        try:
            with contextlib.redirect_stderr(err_buf):
                result = audit_mod.run_audit(
                    req.source,
                    req.against,
                    min_size=parse_size(req.min_size),
                    exclude_pattern=req.exclude,
                    early_stop=req.early_stop,
                    mtime_match=req.mtime_match,
                    mtime_window_seconds=req.mtime_window,
                    max_walk_ratio=(req.max_walk_ratio if req.max_walk_ratio and req.max_walk_ratio > 0 else None),
                    db_path=app.state.db_path,
                    out=io.StringIO(),
                )
        except SystemExit as e:
            # audit_mod prints the human message to stderr before raising;
            # surface that to the UI instead of a generic exit code.
            msg = err_buf.getvalue().strip() or f"audit failed (exit {e.code})"
            raise HTTPException(status_code=400, detail=msg)

        return _audit_to_dict(result)

    @app.post("/api/backup-missing")
    def backup_missing(req: BackupMissingReq):
        """Copy an audit run's missing files to dest. Strictly additive."""
        from .. import backup as backup_mod
        err_buf = io.StringIO()
        try:
            with contextlib.redirect_stderr(err_buf):
                report = backup_mod.run_backup_missing(
                    req.audit_run_id,
                    apply=req.apply,
                    dest_ident=req.dest_ident,
                    db_path=app.state.db_path,
                    out=io.StringIO(),
                )
        except SystemExit as e:
            msg = err_buf.getvalue().strip() or f"backup failed (exit {e.code})"
            raise HTTPException(status_code=400, detail=msg)
        return _backup_report_to_dict(report)

    @app.get("/api/audit/{run_id}")
    def get_audit(run_id: int):
        conn = dbmod.get_conn(app.state.db_path)
        try:
            row = conn.execute(
                "SELECT * FROM audit_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise HTTPException(404, "audit run not found")
            missing = conn.execute(
                "SELECT source_path, size FROM audit_missing WHERE run_id = ? "
                "ORDER BY size DESC",
                (run_id,),
            ).fetchall()
            return {
                "id": row["id"],
                "source_ident": row["source_ident"],
                "source_kind": row["source_kind"],
                "dest_specs": json.loads(row["dest_specs"]),
                "min_size": row["min_size"],
                "early_stop": bool(row["early_stop"]),
                "total_files": row["total_files"],
                "total_bytes": row["total_bytes"],
                "matched": row["matched_count"],
                "missing": row["missing_count"],
                "missing_bytes": row["missing_bytes"],
                "per_dest_match": json.loads(row["per_dest_match"] or "{}"),
                "ran_at": row["ran_at"],
                "missing_paths": [
                    {"path": m["source_path"], "size": m["size"], "size_human": human_size(m["size"])}
                    for m in missing
                ],
                # Persisted runs predate the EXIF annotation column;
                # reconstruction returns empty so the frontend doesn't
                # have to special-case missing keys.
                "match_evidence": {},
                "match_locations": {},
            }
        finally:
            conn.close()

    return app


def _backup_report_to_dict(report) -> dict:
    return {
        "audit_run_id": report.audit_run_id,
        "source_root": report.source_root,
        "dest_root": report.dest_root,
        "dest_ident": report.dest_ident,
        "applied": report.applied,
        "planned_files": [
            {"path": p, "size": s, "size_human": human_size(s)}
            for p, s in report.planned_files
        ],
        "planned_count": len(report.planned_files),
        "planned_bytes": report.planned_bytes,
        "planned_bytes_human": human_size(report.planned_bytes),
        "succeeded": list(report.succeeded),
        "failed": [
            {"path": p, "error": e} for p, e in report.failed
        ],
    }


def _audit_to_dict(r) -> dict:
    return {
        "run_id": r.run_id,  # for backup-missing button to feed POST /api/backup-missing
        "source_ident": r.source_ident,
        "source_kind": r.source_kind,
        "dest_idents": r.dest_idents,
        "dest_kinds": r.dest_kinds,
        "total_source_files": r.total_source_files,
        "total_source_bytes": r.total_source_bytes,
        "total_source_bytes_human": human_size(r.total_source_bytes),
        "matched": r.matched,
        "missing": r.missing,
        "missing_bytes": r.missing_bytes,
        "missing_bytes_human": human_size(r.missing_bytes),
        "missing_paths": [
            {"path": p, "size": s, "size_human": human_size(s)} for p, s in r.missing_paths
        ],
        "per_dest_match": r.per_dest_match,
        # source_path -> [{ident, dest_path}, ...] so the UI can show the
        # user where each "matched" file landed (catches camera-numbering
        # collisions where two different shots share basename + size).
        "match_locations": {
            src: [{"ident": ident, "dest_path": dp} for ident, dp in hits]
            for src, hits in r.match_locations.items()
        },
        # Per-src EXIF evidence kind: "strong" / "weak" / "conflict".
        # Iron rule: this annotation never moves a file between matched
        # and missing — frontend uses it to highlight rows the user
        # should manually verify, not to filter them out.
        "match_evidence": dict(r.match_evidence),
    }


def _pick_free_port(host: str, preferred: int) -> int:
    """Return ``preferred`` if free, else fall through PORT_FALLBACKS.

    If everything in the fallback list is taken, return preferred anyway
    and let uvicorn raise a clear bind error.
    """
    candidates = [preferred] + [p for p in PORT_FALLBACKS if p != preferred]
    for p in candidates:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, p))
            except OSError:
                continue
            return p
    return preferred


def serve(
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    db_path: Optional[str] = None,
    *,
    port_already_picked: bool = False,
) -> None:
    """Run the GUI server with uvicorn. Blocks until interrupted.

    ``port_already_picked=True`` skips the internal _pick_free_port call.
    Use this when the caller (cli.cmd_gui) has already bound to ``port``
    and opened a browser there: re-picking would create a race window
    where the port is taken between cli's pick and serve's pick, leaving
    the browser pointed at a port the server may not bind to.
    """
    import uvicorn

    if port_already_picked:
        actual_port = port
    else:
        actual_port = _pick_free_port(host, port)
        if actual_port != port:
            print(
                f"port {port} in use; falling back to {actual_port}",
                file=sys.stderr,
            )
    app = create_app(
        db_path=db_path,
        allowed_hosts=_default_allowed_hosts(actual_port),
    )
    uvicorn.run(app, host=host, port=actual_port, log_level="info")


if __name__ == "__main__":  # pragma: no cover
    serve()
