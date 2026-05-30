# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 wt and DriveTidy contributors

"""Defaults and discoverable paths.

No side effects at import time: path lookups are lazy via functions so tests
can monkeypatch.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


DEFAULT_DB_DIR = Path.home() / ".drivetidy"
DEFAULT_DB_FILE = DEFAULT_DB_DIR / "drivetidy.db"

# OS-generated metadata files that we never want to track as user content.
MACOS_EXCLUDE_NAMES = frozenset({
    ".DS_Store",
    ".localized",
    ".VolumeIcon.icns",
    ".VolumeIcon.ico",
    "Autorun.inf",
    "desktop.ini",
    "Thumbs.db",
})

# Prefix check (applied to basename).
MACOS_EXCLUDE_PREFIXES = ("._",)

# Directory names to skip entirely when walking.
MACOS_EXCLUDE_DIRS = frozenset({
    ".Spotlight-V100",
    ".fseventsd",
    ".Trashes",
    ".TemporaryItems",
    "System Volume Information",
    "$RECYCLE.BIN",
})


def db_path() -> Path:
    """Resolve the SQLite db path. Env var DRIVETIDY_DB overrides default."""
    override = os.environ.get("DRIVETIDY_DB")
    if override:
        return Path(override).expanduser()
    return DEFAULT_DB_FILE


def rclone_binary() -> str | None:
    """Return absolute path to rclone if found, else None.

    Resolution order: DRIVETIDY_RCLONE env > shutil.which("rclone").
    Users who keep rclone in a non-standard location should export
    DRIVETIDY_RCLONE in their shell rc.
    """
    env = os.environ.get("DRIVETIDY_RCLONE")
    if env and Path(env).is_file():
        return env
    return shutil.which("rclone")


def fd_binary() -> str | None:
    """Return absolute path to fd if found, else None."""
    return shutil.which("fd") or shutil.which("fdfind")


def is_macos_excluded_basename(name: str) -> bool:
    """Check whether a path basename should be skipped as macOS metadata."""
    if name in MACOS_EXCLUDE_NAMES:
        return True
    for prefix in MACOS_EXCLUDE_PREFIXES:
        if name.startswith(prefix):
            return True
    return False


def is_macos_excluded_dir(name: str) -> bool:
    return name in MACOS_EXCLUDE_DIRS
