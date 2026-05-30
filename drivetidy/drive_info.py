# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 wt and DriveTidy contributors

"""Drive-type detection: HDD vs SSD.

Workload policy:
  - HDD: single-threaded sequential reads (avoid head-thrash)
  - SSD: parallel workers OK (often faster)

Detection strategy:
  1. Try `system_profiler SPStorageDataType -json` (stable JSON on macOS).
     Walk the tree looking for the mount point; inspect the backing
     device's 'SolidState' / 'Solid State' fields.
  2. If anything is ambiguous, treat as HDD (conservative default).
  3. Callers may override via --type hdd|ssd.

We never fail hard; unknown drives get `DriveType.UNKNOWN` and callers
should treat that like HDD.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class DriveType(str, Enum):
    HDD = "hdd"
    SSD = "ssd"
    UNKNOWN = "unknown"


@dataclass
class DriveInfo:
    mount_path: str
    device: str | None
    drive_type: DriveType
    source: str  # 'system_profiler' | 'manual' | 'default'


def _run_system_profiler() -> dict | None:
    """Call `system_profiler SPStorageDataType -json`, return parsed dict or None."""
    try:
        out = subprocess.run(
            ["system_profiler", "SPStorageDataType", "-json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return None


def _is_ssd_from_storage_entry(entry: dict) -> bool | None:
    """Interpret one SPStorageDataType entry as SSD (True), HDD (False), or unknown (None).

    macOS Sequoia / Tahoe places the signal under
    `physical_drive.medium_type` ('ssd' | 'rotational'). We also check a
    few legacy key names and fall back to `protocol` heuristics for
    Apple Fabric / NVMe (always SSD) and Disk Image (virtual, treat
    as SSD for IO purposes).
    """
    phys = entry.get("physical_drive") or {}
    # Primary: medium_type
    mt = phys.get("medium_type")
    if isinstance(mt, str):
        low = mt.strip().lower()
        if low == "ssd":
            return True
        if low in {"rotational", "hdd"}:
            return False

    # Legacy/alternate boolean or textual flags
    for key in ("is_solid_state_drive", "solid_state", "ssd"):
        if key in phys:
            v = phys[key]
            if isinstance(v, bool):
                return v
            if isinstance(v, str):
                return v.strip().lower() in {"yes", "true"}

    # Protocol heuristic.
    # - Apple Fabric: internal bus on Apple Silicon; in practice always SSD
    #   (technically a connection bus, not a media type, but useful here).
    # - NVMe: media is by definition SSD.
    # - Disk Image: APFS disk image, random-access-cheap → treat as SSD.
    protocol = phys.get("protocol")
    if isinstance(protocol, str):
        low = protocol.strip().lower()
        if low in {"apple fabric", "nvme", "nvm express", "disk image"}:
            return True

    # Last-ditch: look at device_name / media_name strings for explicit
    # HDD/SSD branding. This is the only signal we get for USB externals
    # whose bridge chip does not expose medium_type.
    for k in ("device_name", "media_name"):
        v = phys.get(k)
        if isinstance(v, str):
            low = v.lower()
            if "ssd" in low or "solid state" in low:
                return True
            if "hdd" in low:
                return False

    # Top-level fallbacks (older macOS schemas).
    for key in ("solid_state", "ssd", "SolidState", "Solid State"):
        if key in entry:
            v = entry[key]
            if isinstance(v, bool):
                return v
            if isinstance(v, str):
                return v.strip().lower() in {"yes", "true"}

    return None


def _normalize_mount(path: str) -> str:
    """Resolve symlinks and walk up to a real mount point if needed.

    Callers may pass either a true mount point (`/Volumes/Foo`) or a path
    inside one (`/Volumes/Foo/sub`). system_profiler keys on the mount
    point, so we resolve and then climb until we find a directory whose
    parent has a different st_dev (filesystem boundary).
    """
    try:
        resolved = Path(path).resolve()
    except OSError:
        return (path.rstrip("/") or "/")

    # Climb to a mount point boundary.
    try:
        cur_dev = resolved.stat().st_dev
        cur = resolved
        while cur.parent != cur:
            parent = cur.parent
            try:
                if parent.stat().st_dev != cur_dev:
                    break
            except OSError:
                break
            cur = parent
        return str(cur).rstrip("/") or "/"
    except OSError:
        s = str(resolved)
        return s.rstrip("/") or "/"


def detect(mount_path: str) -> DriveInfo:
    """Detect drive type for a mount point."""
    norm = _normalize_mount(mount_path)
    data = _run_system_profiler()
    if data is None:
        return DriveInfo(mount_path=norm, device=None, drive_type=DriveType.UNKNOWN, source="default")

    # SPStorageDataType -> list of volume entries under 'SPStorageDataType'.
    entries = data.get("SPStorageDataType", [])
    for entry in entries:
        mp = _normalize_mount(entry.get("mount_point", ""))
        if mp == norm:
            verdict = _is_ssd_from_storage_entry(entry)
            device = entry.get("bsd_name") or entry.get("_name")
            if verdict is True:
                return DriveInfo(norm, device, DriveType.SSD, "system_profiler")
            if verdict is False:
                return DriveInfo(norm, device, DriveType.HDD, "system_profiler")
            return DriveInfo(norm, device, DriveType.UNKNOWN, "system_profiler")

    return DriveInfo(mount_path=norm, device=None, drive_type=DriveType.UNKNOWN, source="default")


def resolve(mount_path: str, override: str = "auto") -> DriveInfo:
    """Top-level API used by CLI.

    override:
      - 'auto' → detect()
      - 'hdd' or 'ssd' → return DriveInfo with source='manual'
    """
    override_l = override.lower()
    if override_l in {"hdd", "ssd"}:
        return DriveInfo(
            mount_path=_normalize_mount(mount_path),
            device=None,
            drive_type=DriveType(override_l),
            source="manual",
        )
    return detect(mount_path)


def is_ssd(mount_path: str) -> bool:
    """Convenience helper: treat UNKNOWN as not-SSD (conservative)."""
    return detect(mount_path).drive_type is DriveType.SSD


def recommended_parallelism(drive_type: DriveType, ssd_workers: int = 8) -> int:
    """HDD/UNKNOWN → 1 (avoid head-thrash); SSD → ssd_workers."""
    return ssd_workers if drive_type is DriveType.SSD else 1
