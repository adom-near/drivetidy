# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 wt and DriveTidy contributors

"""PyInstaller entry shim.

PyInstaller wants a single Python file as the build entry. This wrapper
just forwards to ``drivetidy.cli:main`` so the bundled binary behaves
exactly like the dev-installed ``drivetidy`` console script.
"""
from drivetidy.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
