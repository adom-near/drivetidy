# Contributing to DriveTidy

Thanks for your interest! DriveTidy is GPL-3.0-or-later.

## Reporting bugs

Open an issue with:
- What you ran (`drivetidy ...` command or GUI action)
- What you expected to happen
- What actually happened (paste output / screenshot)
- OS + Python version (CLI) or app version (GUI)

## Pull requests

1. Fork the repo, create a branch from `main`
2. Make your change. Add a test in `tests/` if you're touching logic
3. Run `pytest` — all tests must pass
4. Open a PR with a clear description of what changes and why

By submitting a PR, you agree to license your contribution under GPL-3.0-or-later.

## Development setup

```bash
git clone https://github.com/wt/drivetidy.git
cd drivetidy
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,gui]"
pytest
```

You'll need `fd` or `rclone` on PATH for backend-dependent tests:

```bash
brew install fd        # macOS
apt install fd-find    # Debian/Ubuntu
```

## Code style

- Python: standard library + minimal deps. No `black` / `ruff` enforcement yet, but follow existing style.
- Commits: conventional commit prefixes (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`).
- Files: every `.py` file starts with the SPDX header (`# SPDX-License-Identifier: GPL-3.0-or-later`).

## Code of Conduct

See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
