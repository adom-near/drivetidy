# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 wt and DriveTidy contributors

"""Pytest setup: skip backend-dependent tests when fd/rclone aren't installed."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest


def pytest_configure(config):
    # The scan allowlist matches by string prefix without resolving symlinks.
    # macOS pytest puts tmp_path under /var/folders/... but the path may be
    # handed to the app in its /private/var/folders/... resolved form (or vice
    # versa), so include both. Same for the basetemp pytest actually uses.
    candidates = ["/tmp", "/private/tmp"]
    tmp_dir = tempfile.gettempdir()
    candidates += [tmp_dir, os.path.realpath(tmp_dir)]
    basetemp = getattr(config.option, "basetemp", None)
    if basetemp:
        candidates += [str(basetemp), os.path.realpath(str(basetemp))]
    roots = ":".join(dict.fromkeys(c for c in candidates if c))
    if "DRIVETIDY_SCAN_EXTRA_ROOTS" not in os.environ:
        os.environ["DRIVETIDY_SCAN_EXTRA_ROOTS"] = roots


def _has_backend() -> bool:
    if shutil.which("fd") or shutil.which("fdfind"):
        return True
    env = os.environ.get("DRIVETIDY_RCLONE")
    if env and Path(env).is_file():
        return True
    if shutil.which("rclone"):
        return True
    return False


@pytest.fixture(scope="session", autouse=True)
def _skip_if_no_backend():
    if not _has_backend():
        pytest.skip(
            "no scan backend (fd / rclone) available; "
            "install fd via brew or set DRIVETIDY_RCLONE"
        )
