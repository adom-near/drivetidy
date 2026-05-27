# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 wt and DriveTidy contributors

"""Unit tests for `drivetidy.exif`.

These use synthetic JPEGs (Pillow writes EXIF tags we then read back) — no
real-world camera fixtures required. A future integration test under
`tests/fixtures/exif/` will cover real Sony/Canon/GoPro outputs once we have
them, but the read-side parsing rules are fully exercised here."""
from __future__ import annotations

import io
import time
from pathlib import Path

import pytest
from PIL import Image
from PIL.ExifTags import IFD as _IFD

from drivetidy import exif as exif_mod


# --- helpers ----------------------------------------------------------------

def _write_jpeg_with_exif(
    path: Path,
    *,
    make: str | None = None,
    model: str | None = None,
    datetime_original: str | None = None,
    image_mode: str = "RGB",
    image_format: str = "JPEG",
) -> Path:
    """Create a 4×4 JPEG with the given EXIF tags. Returns the path."""
    im = Image.new(image_mode, (4, 4), color=(127, 127, 127))
    exif = im.getexif()
    if make is not None:
        exif[0x010F] = make           # Make
    if model is not None:
        exif[0x0110] = model          # Model
    if datetime_original is not None:
        exif_ifd = exif.get_ifd(_IFD.Exif)
        exif_ifd[0x9003] = datetime_original  # DateTimeOriginal
    im.save(path, format=image_format, exif=exif)
    return path


# --- happy paths ------------------------------------------------------------

def test_full_exif_extracts_all_three_fields(tmp_path):
    p = _write_jpeg_with_exif(
        tmp_path / "shot.jpg",
        make="SONY",
        model="ILCE-7M4",
        datetime_original="2026:04:28 17:21:41",
    )
    key = exif_mod.read_exif(str(p))
    assert key is not None
    assert key.camera_make == "SONY"
    assert key.camera_model == "ILCE-7M4"
    assert key.taken_at is not None
    # Sanity: the parsed epoch should round-trip to the same string in local TZ.
    assert time.strftime("%Y:%m:%d %H:%M:%S", time.localtime(key.taken_at)) \
           == "2026:04:28 17:21:41"


def test_only_make_no_datetime(tmp_path):
    """Some action cams write Make/Model but no DateTimeOriginal — the
    reader must still surface what's there."""
    p = _write_jpeg_with_exif(tmp_path / "gp.jpg", make="GoPro", model="HERO12")
    key = exif_mod.read_exif(str(p))
    assert key is not None
    assert key.camera_make == "GoPro"
    assert key.camera_model == "HERO12"
    assert key.taken_at is None


def test_only_datetime_no_camera(tmp_path):
    """Phone screenshots etc. sometimes carry only DateTimeOriginal."""
    p = _write_jpeg_with_exif(tmp_path / "x.jpg", datetime_original="2026:01:02 03:04:05")
    key = exif_mod.read_exif(str(p))
    assert key is not None
    assert key.taken_at is not None
    assert key.camera_make is None
    assert key.camera_model is None


# --- empty / unsupported ----------------------------------------------------

def test_jpeg_without_exif_returns_none(tmp_path):
    """Pillow doesn't embed EXIF unless we ask. A vanilla JPEG → None,
    not an empty ExifKey, so callers can early-exit on `if key is None`."""
    p = tmp_path / "plain.jpg"
    Image.new("RGB", (4, 4), (0, 0, 0)).save(p, format="JPEG")
    assert exif_mod.read_exif(str(p)) is None


def test_png_returns_none(tmp_path):
    """v1 only handles JPEG. PNG/TIFF/RAW → None so audit's fallback path runs."""
    p = tmp_path / "x.png"
    Image.new("RGB", (4, 4), (0, 0, 0)).save(p, format="PNG")
    assert exif_mod.read_exif(str(p)) is None


def test_empty_strings_become_none(tmp_path):
    """Some encoders write empty/whitespace-only Make. Treat as missing,
    don't propagate '' as a real signal."""
    p = _write_jpeg_with_exif(tmp_path / "e.jpg", make="   ", model="\x00\x00")
    # Both Make and Model are effectively empty; if DateTimeOriginal is also
    # absent, ExifKey.is_empty() short-circuits to None.
    assert exif_mod.read_exif(str(p)) is None


# --- failure modes (must never raise) ---------------------------------------

def test_missing_file_returns_none(tmp_path):
    assert exif_mod.read_exif(str(tmp_path / "does_not_exist.jpg")) is None


def test_truncated_file_returns_none(tmp_path):
    """A JPEG header byte then garbage — Pillow either rejects or partial-reads."""
    p = tmp_path / "broken.jpg"
    p.write_bytes(b"\xff\xd8\xff\xe0this is not a valid jpeg")
    assert exif_mod.read_exif(str(p)) is None


def test_random_binary_returns_none(tmp_path):
    p = tmp_path / "noise.bin"
    p.write_bytes(b"\x00\x01\x02\x03" * 100)
    assert exif_mod.read_exif(str(p)) is None


def test_text_file_returns_none(tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("hello world")
    assert exif_mod.read_exif(str(p)) is None


# --- parse robustness -------------------------------------------------------

def test_malformed_datetime_treated_as_missing(tmp_path):
    """Exif spec format is 'YYYY:MM:DD HH:MM:SS'. Anything else → taken_at=None,
    other fields still pass through."""
    p = _write_jpeg_with_exif(
        tmp_path / "bad_ts.jpg",
        make="Canon",
        datetime_original="not-a-real-timestamp",
    )
    key = exif_mod.read_exif(str(p))
    assert key is not None
    assert key.camera_make == "Canon"
    assert key.taken_at is None


def test_iron_law_never_raises(tmp_path):
    """Smoke test: every weird input must return None or ExifKey, never raise.
    Regression guard for the "never matched→missing" rule downstream — a
    raised exception in audit's match loop would break the whole audit."""
    inputs = [
        b"",
        b"\x00",
        b"\xff" * 1024,
        b"\xff\xd8" + b"\x00" * 2048,           # JPEG SOI then nulls
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 100,    # PNG header truncated
    ]
    for i, blob in enumerate(inputs):
        p = tmp_path / f"f{i}.dat"
        p.write_bytes(blob)
        # No assertion on value; the assertion is "no exception".
        result = exif_mod.read_exif(str(p))
        assert result is None or isinstance(result, exif_mod.ExifKey)
