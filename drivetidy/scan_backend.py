# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 wt and DriveTidy contributors

"""File-listing backends for `drivetidy scan`.

Two backends, API-identical:
  - fd   (preferred on local filesystems; 2-3x faster than rclone lsl)
  - rclone lsl (fallback; also the future path for cloud storage)

Both yield `FileEntry(rel_path, rel_path_raw, size, mtime)` tuples.
Paths are relative to the given root, NFC-normalized for JOINs;
`rel_path_raw` preserves whatever bytes the OS returned (for actual
filesystem operations later — only differs on HFS+/NFD volumes).

Per internal notes §2.3 / §3.1:
  - HDD → single worker (fd: -j 1) to avoid head thrash
  - SSD → default parallelism (fd uses CPU count)

Subprocess usage is shell=False with list args; no string concat of
paths into shell commands.
"""

from __future__ import annotations

import os
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from . import config


@dataclass(frozen=True)
class FileEntry:
    rel_path: str        # NFC-normalized, relative to scan root
    rel_path_raw: str    # Raw bytes; equal to rel_path on APFS
    size: int
    mtime: float


class BackendNotAvailable(RuntimeError):
    pass


class BackendError(RuntimeError):
    """Backend ran but failed mid-scan (non-zero exit, broken pipe, etc.).

    Distinct from `BackendNotAvailable` (binary missing): the call started
    but the backend did not finish cleanly. Callers MUST surface this
    rather than persist the partial result as a complete scan."""
    pass


def _normalize(raw: str) -> tuple[str, str | None]:
    """Return (nfc_path, raw_path_or_None).

    If NFC == raw we return None for raw to save db space.
    """
    nfc = unicodedata.normalize("NFC", raw)
    if nfc == raw:
        return nfc, None
    return nfc, raw


def _should_skip_basename(name: str) -> bool:
    return config.is_macos_excluded_basename(name)


def _should_skip_dir(name: str) -> bool:
    return config.is_macos_excluded_dir(name)


# ---------- fd backend ----------

def _fd_cmd(root: str, threads: int) -> list[str]:
    """Build fd argv. fd is told to print null-delimited absolute paths.

    We do NOT use fd's -X stat because that spawns per-file; instead we
    stat from Python, which is a single syscall each and avoids forks.
    """
    fd = config.fd_binary()
    if fd is None:
        raise BackendNotAvailable("fd binary not found (brew install fd)")

    cmd = [
        fd,
        ".",
        root,
        "--type", "f",
        "--hidden",                 # include dotfiles; we filter macOS metadata ourselves
        "--no-ignore",              # don't respect .gitignore — we want everything
        "--print0",
        "-j", str(threads),
    ]
    # Also skip macOS reserved dirs wholesale via --exclude; saves stat calls.
    for d in config.MACOS_EXCLUDE_DIRS:
        cmd.extend(["--exclude", d])
    return cmd


def scan_with_fd(root: str, threads: int) -> Iterator[FileEntry]:
    """Yield FileEntry by running fd + statting each hit from Python."""
    root = os.path.abspath(root)
    cmd = _fd_cmd(root, threads)

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=-1,
    )
    assert proc.stdout is not None
    try:
        buf = b""
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            buf += chunk
            # fd uses NUL as separator with --print0
            while b"\0" in buf:
                raw_bytes, buf = buf.split(b"\0", 1)
                if not raw_bytes:
                    continue
                try:
                    abs_path = raw_bytes.decode("utf-8", errors="surrogateescape")
                except Exception:
                    continue
                name = os.path.basename(abs_path)
                if _should_skip_basename(name):
                    continue
                try:
                    st = os.stat(abs_path)
                except FileNotFoundError:
                    continue
                except OSError:
                    continue
                rel_raw = os.path.relpath(abs_path, root)
                rel_nfc, raw_if_diff = _normalize(rel_raw)
                yield FileEntry(
                    rel_path=rel_nfc,
                    rel_path_raw=raw_if_diff if raw_if_diff is not None else rel_nfc,
                    size=st.st_size,
                    mtime=st.st_mtime,
                )
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        rc = proc.wait(timeout=5)
        if rc not in (0, None):
            # fd returns 1 if nothing matched; 2 on error. Don't hard-fail on 1.
            if rc >= 2:
                raise RuntimeError(f"fd exited with code {rc}")


# ---------- rclone backend ----------

def _rclone_cmd(root: str) -> list[str]:
    rc = config.rclone_binary()
    if rc is None:
        raise BackendNotAvailable("rclone binary not found; set DRIVETIDY_RCLONE or install rclone")
    cmd = [rc, "lsl", root]
    # Tell rclone to skip macOS reserved dirs at the source. Local-disk impact
    # is small (few entries), but a future S3/B2 backend would otherwise pay
    # network cost listing these.
    for d in config.MACOS_EXCLUDE_DIRS:
        cmd.extend(["--exclude", f"{d}/**"])
    return cmd


def scan_with_rclone(root: str) -> Iterator[FileEntry]:
    """Yield FileEntry from `rclone lsl <root>` output.

    Output format: `     SIZE YYYY-MM-DD HH:MM:SS.ffffff path/relative/to/root`
    (size is right-padded to a fixed width).
    """
    import datetime

    root = os.path.abspath(root)
    cmd = _rclone_cmd(root)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=-1,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
    )
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            line = line.rstrip("\n")
            if not line:
                continue
            # Split on whitespace limited to 3 (size, date, time, path)
            parts = line.split(None, 3)
            if len(parts) != 4:
                continue
            size_s, date_s, time_s, rel = parts
            try:
                size = int(size_s)
            except ValueError:
                continue
            try:
                dt = datetime.datetime.fromisoformat(f"{date_s}T{time_s}")
                mtime = dt.timestamp()
            except ValueError:
                mtime = 0.0
            name = os.path.basename(rel)
            if _should_skip_basename(name):
                continue
            # rclone lsl already skips our macOS dirs only if we passed --exclude;
            # filter basenames here to keep parity with fd backend.
            # Directory-level skips (.Spotlight-V100 etc.) won't appear because
            # rclone cannot read them without permission; any residue we drop by
            # excluded dir name anywhere in the path.
            skip = False
            for part in rel.split(os.sep):
                if _should_skip_dir(part):
                    skip = True
                    break
            if skip:
                continue
            rel_nfc, raw_if_diff = _normalize(rel)
            yield FileEntry(
                rel_path=rel_nfc,
                rel_path_raw=raw_if_diff if raw_if_diff is not None else rel_nfc,
                size=size,
                mtime=mtime,
            )
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        try:
            rc = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            rc = proc.wait()
        # Drain stderr after wait — rclone's error volume is modest and
        # pipe buffers are large enough that this rarely deadlocks; if
        # it does we accept truncation rather than hold the scan.
        err_text = ""
        if proc.stderr is not None:
            try:
                err_text = proc.stderr.read() or ""
            except Exception:
                pass
            try:
                proc.stderr.close()
            except Exception:
                pass
        if rc != 0:
            msg = err_text.strip() or f"rclone lsl exited {rc}"
            raise BackendError(f"rclone scan failed: {msg}")


# ---------- top-level dispatcher ----------

def iter_files(root: str, *, backend_pref: str, hdd_parallelism: int) -> Iterator[FileEntry]:
    """Dispatch to fd (preferred) or rclone lsl.

    hdd_parallelism: passed to fd's -j. For HDDs use 1 (caller decides).
    """
    if backend_pref == "rclone":
        yield from scan_with_rclone(root)
        return
    # default: try fd, fall back to rclone
    try:
        yield from scan_with_fd(root, threads=hdd_parallelism)
    except BackendNotAvailable:
        yield from scan_with_rclone(root)
