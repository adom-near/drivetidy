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
    tmp_dir = Path(tempfile.gettempdir())
    if "DRIVETIDY_SCAN_EXTRA_ROOTS" not in os.environ:
        os.environ["DRIVETIDY_SCAN_EXTRA_ROOTS"] = "/tmp:/private/tmp:" + str(tmp_dir)


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
