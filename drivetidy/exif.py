# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 wt and DriveTidy contributors

"""EXIF reader for audit match-key disambiguation.

Returns at most three signals — `taken_at` (DateTimeOriginal), `camera_make`,
`camera_model` — which `audit.py` uses to break ties when multiple destination
candidates have the same `(size, basename, mtime±2s)` key.

**Hard rule:** this module is for *disambiguation*, not for replacing the
fallback match key. `audit.py` must keep the existing match logic intact and
only consult EXIF when there is more than one candidate. A failure / missing
tag here returns None and the caller falls back to the legacy behaviour. EXIF
output is **never** allowed to turn an existing match into a miss.

JPEG only for v1. RAW (CR2/ARW/NEF/DNG) is deferred to v1.5 — those formats
have vendor-specific layouts and benefit from a battle-tested library; we'll
revisit when we have real fixture coverage.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

# Pillow is a hard dep (also used by share.py for the result card). Imported
# at module load so any ImportError fails loudly during install rather than
# silently returning None for every file.
from PIL import Image, UnidentifiedImageError
from PIL.ExifTags import IFD as _IFD


# --- Tag IDs (per EXIF 2.32) ---
# Listed inline (rather than imported from PIL.ExifTags.Base) so the contract
# is stable even if Pillow renames symbols across versions.
_TAG_MAKE = 0x010F           # 271 — TIFF IFD0
_TAG_MODEL = 0x0110          # 272 — TIFF IFD0
_TAG_DATETIME_ORIGINAL = 0x9003   # 36867 — Exif sub-IFD


@dataclass(frozen=True)
class ExifKey:
    """The three fields audit.py uses for disambiguation. Each may be None
    independently; callers must not assume a non-None tuple."""
    taken_at: Optional[int]      # epoch seconds (local TZ at parse time), None if absent / unparseable
    camera_make: Optional[str]   # e.g. "SONY"
    camera_model: Optional[str]  # e.g. "ILCE-7M4"

    def is_empty(self) -> bool:
        return self.taken_at is None and self.camera_make is None and self.camera_model is None


def _parse_datetime_original(raw: object) -> Optional[int]:
    """EXIF DateTimeOriginal format is 'YYYY:MM:DD HH:MM:SS' (no timezone).
    Treat as local time — same convention filesystem mtime uses, which keeps
    EXIF-based disambiguation comparable to the existing mtime±2s key."""
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    try:
        struct = time.strptime(s, "%Y:%m:%d %H:%M:%S")
        return int(time.mktime(struct))
    except (ValueError, OverflowError):
        return None


def _coerce_str(raw: object) -> Optional[str]:
    """EXIF string fields can come back as bytes (older Pillow) or str
    (newer). Trim trailing NULs/whitespace; return None on empty."""
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("ascii", errors="replace")
        except Exception:
            return None
    if not isinstance(raw, str):
        return None
    s = raw.strip().strip("\x00").strip()
    return s or None


def read_exif(path: str) -> Optional[ExifKey]:
    """Return the disambiguation triple for a JPEG, or None.

    None covers every failure mode: non-JPEG, no EXIF block, malformed EXIF,
    permission denied, file not found. Never raises.

    The caller (`audit.py`) interprets None as "use the fallback match key
    unmodified" — so the worst this function can do is return False-y data,
    which preserves the legacy match behaviour.
    """
    try:
        with Image.open(path) as im:
            # JPEG only for v1. PNG / TIFF / RAW return None (RAW v1.5).
            fmt = (im.format or "").upper()
            if fmt not in ("JPEG", "JPG", "MPO"):
                return None
            exif = im.getexif()
            if not exif:
                return None

            make = _coerce_str(exif.get(_TAG_MAKE))
            model = _coerce_str(exif.get(_TAG_MODEL))

            # DateTimeOriginal lives in the Exif sub-IFD, not the top-level.
            # If get_ifd(Exif) raises, treat as missing — never propagate.
            taken_at: Optional[int] = None
            try:
                exif_ifd = exif.get_ifd(_IFD.Exif)
                if exif_ifd:
                    taken_at = _parse_datetime_original(exif_ifd.get(_TAG_DATETIME_ORIGINAL))
            except (KeyError, ValueError, AttributeError):
                taken_at = None

            key = ExifKey(taken_at=taken_at, camera_make=make, camera_model=model)
            return None if key.is_empty() else key
    except (OSError, UnidentifiedImageError, ValueError, SyntaxError):
        # OSError covers FileNotFoundError, PermissionError, broken pipe.
        # UnidentifiedImageError = Pillow couldn't identify the format.
        # ValueError / SyntaxError = malformed JPEG header.
        return None
