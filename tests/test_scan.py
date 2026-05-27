# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 wt and DriveTidy contributors

"""Scan-level regression tests.

We exercise scan through GUI/CLI integration in test_gui.py + test_audit.py;
this file pins behaviours that are easier to assert at the scan layer:
finished_at must be NULL on interrupt / backend failure (audit relies on
that to refuse stale-incomplete data); EXIF cache populated only when
explicitly opted in."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from PIL import Image
from PIL.ExifTags import IFD as _IFD

from drivetidy import scan as scan_mod
from drivetidy.scan_backend import BackendError, FileEntry


def _write_jpeg(path: Path, *, make: str = "SONY", model: str = "ILCE-7M4",
                taken: str = "2026:05:08 19:00:00") -> Path:
    im = Image.new("RGB", (4, 4), (127, 127, 127))
    exif = im.getexif()
    exif[0x010F] = make
    exif[0x0110] = model
    exif.get_ifd(_IFD.Exif)[0x9003] = taken
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, format="JPEG", exif=exif)
    return path


def _scan_row(db_path: str, scan_id: int) -> sqlite3.Row:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM scans WHERE id = ?", (scan_id,)).fetchone()
    finally:
        conn.close()


def test_run_scan_keyboard_interrupt_leaves_finished_at_null(tmp_path, monkeypatch):
    """KeyboardInterrupt mid-scan must NOT mark the scan finished —
    audit treats finished_at as proof-of-completeness; persisting a
    partial scan as complete causes false 'all backed up' verdicts."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.jpg").write_bytes(b"x")
    db = str(tmp_path / "test.db")

    def fake_iter(*_args, **_kwargs):
        # Yield a few entries then bail.
        yield FileEntry(rel_path="a.jpg", rel_path_raw="a.jpg", size=1, mtime=0.0)
        raise KeyboardInterrupt
    monkeypatch.setattr(scan_mod, "iter_files", fake_iter)

    scan_id = scan_mod.run_scan(str(src), db_path=db)
    row = _scan_row(db, scan_id)
    assert row is not None
    assert row["finished_at"] is None, (
        "interrupted scan must keep finished_at NULL; audit relies on "
        "this to refuse stale data"
    )
    # file_count is still recorded for diagnostic visibility.
    assert row["file_count"] == 1


def test_run_scan_backend_error_leaves_finished_at_null_and_raises(tmp_path, monkeypatch):
    """Backend non-zero exit must propagate to caller AND not finalise
    the scan row. A silent finished scan with file_count=0 would make
    audit report every source file as missing."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.jpg").write_bytes(b"x")
    db = str(tmp_path / "test.db")

    def fake_iter(*_args, **_kwargs):
        if False:
            yield  # pragma: no cover — make this a generator
        raise BackendError("rclone scan failed: simulated")
    monkeypatch.setattr(scan_mod, "iter_files", fake_iter)

    with pytest.raises(BackendError):
        scan_mod.run_scan(str(src), db_path=db)
    # Locate the (only) scan row that was created before the failure.
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["finished_at"] is None


def test_scan_default_does_not_populate_exif_cache(tmp_path):
    """Default scan must NOT touch exif_cache. Promise: turning the flag
    off keeps the existing scan-time profile (no per-JPEG file open)."""
    src = tmp_path / "src"
    _write_jpeg(src / "a.jpg")
    db = str(tmp_path / "test.db")
    scan_mod.run_scan(str(src), db_path=db)  # extract_exif defaults to False

    conn = sqlite3.connect(db)
    try:
        rows = conn.execute("SELECT COUNT(*) FROM exif_cache").fetchone()
    finally:
        conn.close()
    assert rows[0] == 0


def test_scan_extract_exif_caches_jpeg_and_skips_non_image(tmp_path):
    """With extract_exif=True a JPEG gets its EXIF triple cached; an MP4
    in the same tree does not (we don't even open it). Pin the suffix
    whitelist so future format additions get a deliberate test."""
    src = tmp_path / "src"
    _write_jpeg(src / "DCIM/IMG_0001.jpg",
                make="SONY", model="ILCE-7M4",
                taken="2026:05:08 19:01:00")
    (src / "DCIM/clip.mp4").parent.mkdir(parents=True, exist_ok=True)
    (src / "DCIM/clip.mp4").write_bytes(b"FAKE_VIDEO_DATA" * 10)
    db = str(tmp_path / "test.db")

    scan_mod.run_scan(str(src), db_path=db, extract_exif=True)

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM exif_cache ORDER BY path").fetchall()
    finally:
        conn.close()
    assert len(rows) == 1, [dict(r) for r in rows]
    assert rows[0]["path"] == "DCIM/IMG_0001.jpg"
    assert rows[0]["camera_make"] == "SONY"
    assert rows[0]["camera_model"] == "ILCE-7M4"
    assert rows[0]["taken_at"] is not None


def test_scan_extract_exif_skips_jpeg_without_exif(tmp_path):
    """A JPEG with no EXIF (e.g. exported without metadata) must NOT
    leave an empty cache row — exif.read_exif returns None there and
    the scan loop drops it. Otherwise audit would JOIN to a row with
    every field NULL and falsely think 'this file had EXIF parsed'."""
    src = tmp_path / "src"
    src.mkdir()
    Image.new("RGB", (4, 4), (0, 0, 0)).save(src / "plain.jpg", format="JPEG")
    db = str(tmp_path / "test.db")

    scan_mod.run_scan(str(src), db_path=db, extract_exif=True)

    conn = sqlite3.connect(db)
    try:
        rows = conn.execute("SELECT COUNT(*) FROM exif_cache").fetchone()
    finally:
        conn.close()
    assert rows[0] == 0


def test_scan_with_rclone_raises_backend_error_on_nonzero_exit(monkeypatch):
    """Pin the scan_backend layer: a non-zero rclone exit surfaces as
    BackendError with the stderr message attached, not a silent empty
    iterator."""
    from drivetidy import scan_backend as sb

    def fake_cmd(_root: str) -> list[str]:
        # Print a fake message to stderr then exit 1.
        return ["sh", "-c", "echo 'simulated rclone permission denied' >&2; exit 1"]
    monkeypatch.setattr(sb, "_rclone_cmd", fake_cmd)

    with pytest.raises(BackendError) as exc:
        list(sb.scan_with_rclone("/tmp"))
    assert "simulated rclone permission denied" in str(exc.value)
