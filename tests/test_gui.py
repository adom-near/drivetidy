# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 wt and DriveTidy contributors

"""GUI API smoke tests via FastAPI's TestClient."""
from __future__ import annotations

import io
import os
import shutil
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from drivetidy import scan as scan_mod
from drivetidy.gui.app import create_app


def _make_tree(root: Path, files: dict) -> None:
    for rel, content in files.items():
        full = root / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(content)


@pytest.fixture
def app(tmp_path: Path):
    db = str(tmp_path / "test.db")
    return create_app(db_path=db), db


def test_index_returns_html(app):
    a, _ = app
    client = TestClient(a)
    r = client.get("/")
    assert r.status_code == 200
    assert "DriveTidy" in r.text
    assert "備份稽核" in r.text


def test_index_includes_folder_picker_buttons(app):
    """audit form needs picker buttons so users can drill into a
    specific subfolder rather than only the drive root that the
    Drives panel offers."""
    a, _ = app
    client = TestClient(a)
    r = client.get("/")
    assert r.status_code == 200
    assert 'id="pick-source"' in r.text
    assert 'id="pick-against"' in r.text
    assert "選資料夾" in r.text


def test_pick_folder_endpoint_handles_user_cancel(tmp_path, monkeypatch):
    """When osascript reports the user-cancel rc, endpoint returns
    {cancelled: true} (not an error). UI uses this to no-op silently."""
    import subprocess
    class _Result:
        returncode = 1
        stdout = ""
        stderr = "0:0: execution error: User canceled. (-128)"
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _Result())

    db = str(tmp_path / "test.db")
    a = create_app(db_path=db)
    client = TestClient(a)
    r = client.post("/api/pick-folder")
    assert r.status_code == 200
    assert r.json() == {"cancelled": True}


def test_pick_folder_endpoint_returns_path(tmp_path, monkeypatch):
    """Successful pick returns {path: ...} with trailing slash trimmed."""
    import subprocess
    class _Result:
        returncode = 0
        stdout = "/Volumes/SD-Card/DCIM/\n"
        stderr = ""
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _Result())

    db = str(tmp_path / "test.db")
    a = create_app(db_path=db)
    client = TestClient(a)
    r = client.post("/api/pick-folder")
    assert r.status_code == 200
    assert r.json() == {"path": "/Volumes/SD-Card/DCIM"}


def test_drives_endpoint_shape(app):
    a, _ = app
    client = TestClient(a)
    r = client.get("/api/drives")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    # Each entry must have the keys the JS expects.
    for entry in body:
        for k in ("path", "name", "drive_type", "drive_type_source", "accessible"):
            assert k in entry


def test_labels_empty_when_no_scans(app):
    a, _ = app
    client = TestClient(a)
    r = client.get("/api/labels")
    assert r.status_code == 200
    assert r.json() == []


def test_audit_endpoint_end_to_end(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_tree(src, {"a.jpg": b"abc", "b.jpg": b"orphan_only_in_src"})
    _make_tree(dst, {"copy/a.jpg": b"abc"})
    db = str(tmp_path / "test.db")
    app = create_app(db_path=db)
    client = TestClient(app)

    # Scan dst as a label, then audit src (live path) against the label.
    r = client.post("/api/scan", json={
        "path": str(dst), "label": "dst_label", "type": "ssd", "backend": "rclone",
    })
    assert r.status_code == 200, r.text
    assert r.json()["scan_id"] >= 1

    r = client.post("/api/audit", json={
        "source": str(src),
        "against": ["dst_label"],
        "min_size": "0",
        "early_stop": True,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["matched"] == 1
    assert body["missing"] == 1
    assert body["missing_paths"][0]["path"].endswith("b.jpg")
    assert body["per_dest_match"]["dst_label"] == 1
    # match_evidence is always present in fresh-audit responses (frontend
    # uses it to highlight conflict rows; absence would force a defensive
    # `?? {}` everywhere). Empty dict is fine when no EXIF was scanned.
    assert "match_evidence" in body
    assert isinstance(body["match_evidence"], dict)


def test_audit_response_includes_run_id(tmp_path):
    """Backup-missing button needs the run_id; /api/audit must surface
    it. Persisted-only scenario covered separately by GET /api/audit/{id}."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_tree(src, {"a.jpg": b"X", "b.jpg": b"Y"})
    _make_tree(dst, {"a.jpg": b"X"})
    db = str(tmp_path / "test.db")
    app = create_app(db_path=db)
    client = TestClient(app)
    r = client.post("/api/audit", json={
        "source": str(src), "against": [str(dst)],
        "min_size": "0", "early_stop": True,
    })
    assert r.status_code == 200
    assert r.json()["run_id"] >= 1


def test_backup_missing_dry_run_via_api(tmp_path):
    """POST /api/backup-missing apply=false returns the plan; rsync
    is never invoked. Mirrors test_backup.py's dry-run regression but
    via the HTTP layer so the JSON shape is locked too."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_tree(src, {"a.jpg": b"X", "b.jpg": b"Y"})
    _make_tree(dst, {"a.jpg": b"X"})
    db = str(tmp_path / "test.db")
    app = create_app(db_path=db)
    client = TestClient(app)
    r = client.post("/api/audit", json={
        "source": str(src), "against": [str(dst)],
        "min_size": "0", "early_stop": True,
    })
    run_id = r.json()["run_id"]

    r = client.post("/api/backup-missing", json={
        "audit_run_id": run_id, "apply": False,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] is False
    assert body["planned_count"] == 1
    assert body["planned_files"][0]["path"].endswith("b.jpg")
    assert body["succeeded"] == []
    assert body["failed"] == []
    # dest must not have been touched.
    assert not (dst / "b.jpg").exists()


def test_backup_missing_apply_via_api_copies_files(tmp_path):
    """End-to-end: audit finds missing, /api/backup-missing apply=true
    actually copies, dest now has the missing files."""
    if shutil.which("rsync") is None:
        pytest.skip("rsync not installed")
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_tree(src, {"a.jpg": b"X", "shoot/b.jpg": b"YY"})
    _make_tree(dst, {"a.jpg": b"X"})
    db = str(tmp_path / "test.db")
    app = create_app(db_path=db)
    client = TestClient(app)
    r = client.post("/api/audit", json={
        "source": str(src), "against": [str(dst)],
        "min_size": "0", "early_stop": True,
    })
    run_id = r.json()["run_id"]

    r = client.post("/api/backup-missing", json={
        "audit_run_id": run_id, "apply": True,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] is True
    assert len(body["succeeded"]) == 1
    assert len(body["failed"]) == 0
    assert (dst / "shoot/b.jpg").read_bytes() == b"YY"


def test_backup_missing_unknown_run_id_returns_400(tmp_path):
    db = str(tmp_path / "test.db")
    app = create_app(db_path=db)
    client = TestClient(app)
    r = client.post("/api/backup-missing", json={
        "audit_run_id": 99999, "apply": False,
    })
    assert r.status_code == 400
    assert "找不到稽核紀錄" in r.json()["detail"]


def test_audit_get_persisted_returns_empty_evidence(tmp_path):
    """Persisted runs predate the EXIF column. GET /api/audit/{id} must
    return match_evidence: {} (not 500, not undefined) so the frontend's
    renderEvidence() can run safely against historical runs."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_tree(src, {"a.jpg": b"x"})
    _make_tree(dst, {"a.jpg": b"x"})
    db = str(tmp_path / "test.db")
    app = create_app(db_path=db)
    client = TestClient(app)
    client.post("/api/audit", json={
        "source": str(src), "against": [str(dst)],
        "min_size": "0", "early_stop": True,
    })
    # Fetch the latest run via the labels endpoint (audit_runs is the only
    # reachable id). We assume run_id = 1 since this test starts from a
    # fresh DB.
    r = client.get("/api/audit/1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("match_evidence") == {}


def test_host_guard_rejects_unknown_host(tmp_path):
    db = str(tmp_path / "test.db")
    a = create_app(db_path=db, allowed_hosts={"127.0.0.1:8765"})
    client = TestClient(a)
    # TestClient default Host = "testserver"
    r = client.get("/api/state")
    assert r.status_code == 421
    # Allowed Host passes
    r2 = client.get("/api/state", headers={"Host": "127.0.0.1:8765"})
    assert r2.status_code == 200


def test_serve_skips_port_pick_when_flag_set(monkeypatch):
    """When cli.cmd_gui has already picked a port and opened a browser,
    serve() must reuse that port rather than re-pick (avoids race)."""
    from drivetidy.gui import app as gui_app

    pick_calls = []

    def fake_pick(host, port):
        pick_calls.append((host, port))
        return port + 1  # would shift port if invoked

    uvicorn_calls = []

    class FakeUvicorn:
        @staticmethod
        def run(app, host, port, log_level):
            uvicorn_calls.append((host, port))

    monkeypatch.setattr(gui_app, "_pick_free_port", fake_pick)
    monkeypatch.setitem(__import__("sys").modules, "uvicorn", FakeUvicorn)

    gui_app.serve(host="127.0.0.1", port=8765, db_path=None, port_already_picked=True)

    assert pick_calls == []
    assert uvicorn_calls == [("127.0.0.1", 8765)]


def test_serve_picks_port_when_flag_unset(monkeypatch):
    """Default path: serve() still picks a free port if caller did not."""
    from drivetidy.gui import app as gui_app

    pick_calls = []

    def fake_pick(host, port):
        pick_calls.append((host, port))
        return 8800

    class FakeUvicorn:
        @staticmethod
        def run(app, host, port, log_level):
            pass

    monkeypatch.setattr(gui_app, "_pick_free_port", fake_pick)
    monkeypatch.setitem(__import__("sys").modules, "uvicorn", FakeUvicorn)

    gui_app.serve(host="127.0.0.1", port=8765, db_path=None)

    assert pick_calls == [("127.0.0.1", 8765)]


def test_pick_free_port_avoids_in_use():
    from drivetidy.gui import app as gui_app
    import socket as _s
    s = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
    except (PermissionError, OSError) as e:
        s.close()
        pytest.skip(f"socket bind not permitted in this environment: {e}")
    busy_port = s.getsockname()[1]
    try:
        picked = gui_app._pick_free_port("127.0.0.1", busy_port)
        assert picked != busy_port
    finally:
        s.close()


def test_scan_rejects_path_outside_allowlist(tmp_path, monkeypatch):
    """`/api/scan` must reject paths not under /Volumes/* or a non-hidden
    home subdir, even when the trial is healthy. Defence-in-depth: a
    DNS-rebound or XSRF'd request shouldn't be able to enumerate /etc."""
    # Drop the test-mode override so we see real prod behaviour.
    monkeypatch.delenv("DRIVETIDY_SCAN_EXTRA_ROOTS", raising=False)

    db = str(tmp_path / "test.db")
    app = create_app(db_path=db)
    client = TestClient(app)

    r = client.post("/api/scan", json={
        "path": "/etc", "label": "x", "type": "ssd", "backend": "rclone",
    })
    assert r.status_code == 400
    assert "不被允許" in r.json()["detail"]


def test_scan_path_allowlist_helper():
    """Pin the rules in `_is_scan_path_allowed` directly so we can iterate
    edge cases without spinning up a full FastAPI app."""
    from drivetidy.gui.app import _is_scan_path_allowed
    home = os.path.expanduser("~")

    assert _is_scan_path_allowed("/Volumes/Untitled")
    assert _is_scan_path_allowed("/Volumes/MyDrive/shoot/2026")
    assert _is_scan_path_allowed(f"{home}/Pictures")
    assert _is_scan_path_allowed(f"{home}/PyCharmMiscProject/sub")

    assert not _is_scan_path_allowed("/etc")
    assert not _is_scan_path_allowed("/System/Library")
    assert not _is_scan_path_allowed(f"{home}/.drivetidy")
    assert not _is_scan_path_allowed(f"{home}/.ssh/id_rsa")
    # Path traversal must not leak into a denied area.
    assert not _is_scan_path_allowed(f"{home}/foo/../../etc")

    # Extra roots opt-in (used by tests / dev).
    assert _is_scan_path_allowed("/private/tmp/x", extra_roots=["/private/tmp"])


def test_audit_get_persists(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_tree(src, {"a.jpg": b"abc", "b.jpg": b"yyy"})
    _make_tree(dst, {"a.jpg": b"abc"})
    db = str(tmp_path / "test.db")
    app = create_app(db_path=db)
    client = TestClient(app)

    r = client.post("/api/audit", json={
        "source": str(src), "against": [str(dst)], "min_size": "0", "early_stop": True,
    })
    assert r.status_code == 200

    # Stored audit run id should be reachable via /api/audit/{id}.
    # We don't return the run id from /api/audit currently, so look up
    # via /api/labels-style listing — but for the test, just hit id=1.
    r2 = client.get("/api/audit/1")
    assert r2.status_code == 200
    body = r2.json()
    assert body["matched"] == 1
    assert body["missing"] == 1
