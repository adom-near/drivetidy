# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 wt and DriveTidy contributors

"""`drivetidy audit` — backup completeness check.

Question answered: "every file under <source>, is it backed up on at least
one of <dest1...destN>? if not, which ones?"

Comparison key: `(basename_lower, size)`. No hashing — see
docs/internal design doc §核心邏輯 for why this is acceptable for
photo / video workflows (~99.9% accuracy).

This module implements the SQL mode (Phase 2 A1). Both source and all
dests must already have a finished `drivetidy scan`. Live-walk mode for
on-the-fly destinations is A2.
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from . import config, db as dbmod
from .utils import human_size


@dataclass
class AuditResult:
    source_ident: str           # As the user typed it (label name or path)
    source_kind: str            # 'label' | 'path'
    dest_idents: list[str]      # As the user typed them
    dest_kinds: list[str]       # parallel to dest_idents
    total_source_files: int
    total_source_bytes: int
    matched: int
    missing_paths: list[tuple[str, int]]   # (relative_path, size)
    per_dest_match: dict[str, int]          # ident -> match count
    # source_relpath -> list of (dest_ident, dest_relpath_or_path) for each
    # destination where the (basename, size) signature was found. Lets the
    # user spot false positives (e.g. camera serial-number collisions) by
    # eyeballing whether the destination location makes sense.
    match_locations: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    # Match method used (carried for the report so the user knows what the
    # "matched" count actually means — e.g. mtime-window matches are not
    # byte-identical, just same (size, basename) within ±N seconds).
    mtime_match: bool = True
    mtime_window_seconds: int = 2
    # The persisted run id, populated after _persist_audit. None when
    # persist=False or persist failed. The GUI uses this to feed
    # backup-missing when the user clicks "copy these to dest".
    run_id: int | None = None
    # Per matched src_path, the strongest EXIF-derived evidence kind:
    #   "strong"   — both sides have EXIF, camera + taken_at agree
    #   "weak"     — standard mtime match; EXIF missing or insufficient
    #   "conflict" — both sides have EXIF and they DISAGREE; user should
    #                manually verify before deleting source media
    # Iron rule: this annotation NEVER changes `matched` / `missing_paths`.
    # A "conflict" entry is still in matched; it just gets surfaced as a
    # warning so the user can sanity-check before formatting their card.
    match_evidence: dict[str, str] = field(default_factory=dict)

    @property
    def missing(self) -> int:
        return len(self.missing_paths)

    @property
    def missing_bytes(self) -> int:
        return sum(s for _, s in self.missing_paths)


# ---------------------------------------------------------------------------
# EXIF disambiguation — annotation only, never changes matched/missing.
# ---------------------------------------------------------------------------

# Tuple shape stored per (scan_id, path): (taken_at, camera_make, camera_model)
ExifTriple = tuple[int | None, str | None, str | None]


def _load_exif_cache(conn: sqlite3.Connection, scan_id: int) -> dict[str, ExifTriple]:
    """Pull all EXIF rows for a scan into an in-memory dict keyed by relpath.

    A scan that ran without `--exif` has zero rows here — we return an
    empty dict and the caller falls through to "weak" evidence everywhere.
    Path-mode scans (no scan row) call this with no scan_id and never
    reach here."""
    out: dict[str, ExifTriple] = {}
    for row in conn.execute(
        "SELECT path, taken_at, camera_make, camera_model "
        "FROM exif_cache WHERE scan_id = ?",
        (scan_id,),
    ):
        out[row["path"]] = (row["taken_at"], row["camera_make"], row["camera_model"])
    return out


def _classify_evidence(
    src_exif: ExifTriple | None,
    dest_exif: ExifTriple | None,
    *,
    taken_at_tolerance: int = 5,
) -> str:
    """Score one (src, dest) match by comparing EXIF triples.

    Returns 'strong' / 'weak' / 'conflict'. Never raises.

    Iron rule: the caller will NOT use this to demote a match to missing.
    The output is a quality tag surfaced in the report so the user can
    spot multi-camera collisions and verify manually.
    """
    if src_exif is None or dest_exif is None:
        return "weak"

    s_taken, s_make, s_model = src_exif
    d_taken, d_make, d_model = dest_exif

    # Camera mismatch is a hard signal — different cameras can't share a file.
    if s_make and d_make and s_make.strip().lower() != d_make.strip().lower():
        return "conflict"
    if s_model and d_model and s_model.strip().lower() != d_model.strip().lower():
        return "conflict"

    # Capture timestamp must agree closely. Allow `taken_at_tolerance`
    # seconds of drift for clock skew between cameras / DST boundaries.
    if s_taken is not None and d_taken is not None:
        if abs(s_taken - d_taken) > taken_at_tolerance:
            return "conflict"
        # Both sides have full evidence and it agrees → upgrade to strong.
        return "strong"

    # taken_at missing on at least one side: even if camera info matches,
    # we don't claim "strong" without timestamp confirmation. Stay weak.
    return "weak"


def _aggregate_evidence(kinds: list[str]) -> str:
    """Collapse per-dest evidence kinds into a single per-src verdict.

    Priority: strong > weak > conflict. A src with at least one strong
    match is strong overall (the real backup is found, even if a phantom
    same-name file shows up elsewhere). Pure weak stays weak. All-conflict
    flags the src for manual verification."""
    if not kinds:
        return "weak"
    if any(k == "strong" for k in kinds):
        return "strong"
    if all(k == "conflict" for k in kinds):
        return "conflict"
    return "weak"


def _latest_finished_scan(conn: sqlite3.Connection, label: str) -> int | None:
    """Return scan_id of the latest finished scan for the label, or None."""
    row = conn.execute(
        "SELECT id FROM scans "
        "WHERE label = ? AND finished_at IS NOT NULL "
        "ORDER BY started_at DESC LIMIT 1",
        (label,),
    ).fetchone()
    return row["id"] if row else None


def _looks_like_path(s: str) -> bool:
    """Distinguish 'path' from 'label'.

    Heuristic: if it starts with '/' or '~' or contains os.sep, treat as path.
    A label cannot contain '/' (we sanitize at scan time), so this is unambiguous.
    """
    if not s:
        return False
    if s.startswith("/") or s.startswith("~"):
        return True
    if os.sep in s:
        return True
    return False


def _resolve_target(conn: sqlite3.Connection, ident: str) -> tuple[str, object]:
    """Resolve a CLI argument to either ('label', scan_id) or ('path', abs_path).

    Resolution order:
      1. If `ident` looks like a path → expand ~, return ('path', abs)
      2. If a finished scan with that label exists → return ('label', scan_id)
      3. If a directory at `ident` exists → fallback to ('path', abs)
      4. Otherwise raise SystemExit(1) with a clear error.
    """
    if _looks_like_path(ident):
        p = Path(os.path.expanduser(ident)).resolve()
        if not p.is_dir():
            print(f"錯誤：路徑不存在或不是資料夾：{ident}", file=sys.stderr)
            raise SystemExit(1)
        return "path", str(p)

    sid = _latest_finished_scan(conn, ident)
    if sid is not None:
        return "label", sid

    # Last-ditch: maybe user passed a relative path without './'.
    p = Path(os.path.expanduser(ident))
    if p.is_dir():
        return "path", str(p.resolve())

    print(
        f"錯誤：'{ident}' 不是已知 label，也不是存在的資料夾路徑",
        file=sys.stderr,
    )
    raise SystemExit(1)


# ---------------------------------------------------------------------------
# Live-walk helpers
# ---------------------------------------------------------------------------

def _is_inside_source(
    path: str,
    *,
    src_real: str | None,
    src_dev_ino: tuple[int, int] | None,
) -> bool:
    """True if ``path`` reaches back into the source root.

    Two checks combined because either alone misses cases on macOS:

    1. realpath prefix — catches plain symlinks and bind mounts whose
       resolution collapses to the source's canonical path.

    2. (st_dev, st_ino) equality — catches **macOS firmlinks**, which
       expose the same mount point at multiple kernel namespaces (e.g.
       ``/Volumes/<sd>`` vs ``/Volumes/Macintosh HD/System/Volumes/Data
       /Volumes/<sd>``). realpath does NOT collapse these — both paths
       have distinct realpaths — but their (dev, ino) of the mount root
       are identical (typically dev=<sd_disk>, ino=2). Comparing the
       inode of the firmlink entry against the source root's inode
       catches the cycle without depending on path-string heuristics.
    """
    if src_real is None and src_dev_ino is None:
        return False
    if src_real is not None:
        try:
            target_real = os.path.realpath(path)
            if os.path.commonpath([target_real, src_real]) == src_real:
                return True
        except (OSError, ValueError):
            pass
    if src_dev_ino is not None:
        try:
            st = os.stat(path)
        except OSError:
            return False
        if (st.st_dev, st.st_ino) == src_dev_ino:
            return True
    return False


def _iter_live_signatures(
    root: str,
    *,
    min_size: int,
    exclude_re: re.Pattern[str] | None,
    src_real: str | None = None,
    src_dev_ino: tuple[int, int] | None = None,
) -> Iterator[tuple[int, str, str, int]]:
    """Walk a real directory, yielding (size, basename_lower, rel, mtime_sec).

    ``mtime_sec`` is ``int(st.st_mtime)`` — floors to whole-second, which
    is what cross-filesystem copies (FAT32 → APFS via Finder/rsync) tend
    to preserve. Audit can opt this out via mtime_match=False.

    ``src_real`` + ``src_dev_ino`` describe the source root that this
    dest walk must NOT re-enter. realpath catches symlinks; (dev, ino)
    catches macOS firmlinks where the mount root surfaces at multiple
    kernel namespaces. Pass either, both, or neither (for source-side
    walks where there's nothing to skip).

    Filters macOS metadata dirs (skipped wholesale) and metadata basenames
    (._*, .DS_Store, etc.) to match scan_backend behavior.

    Single-threaded by design: HDD walk parallelism is anti-optimal
    (internal notes §3.1). Caller can launch a thread per drive.
    """
    root = os.path.abspath(root)
    skip_active = src_real is not None or src_dev_ino is not None
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune excluded dirs in-place so os.walk does not descend.
        dirnames[:] = [d for d in dirnames if not config.is_macos_excluded_dir(d)]
        # Prune dirs that lead back into source (firmlinks, bind mounts,
        # symlinks). Also prune the current dirpath itself if we already
        # entered such a redirect.
        if skip_active:
            if _is_inside_source(
                dirpath, src_real=src_real, src_dev_ino=src_dev_ino
            ):
                dirnames[:] = []
                continue
            dirnames[:] = [
                d for d in dirnames
                if not _is_inside_source(
                    os.path.join(dirpath, d),
                    src_real=src_real, src_dev_ino=src_dev_ino,
                )
            ]
        for name in filenames:
            if config.is_macos_excluded_basename(name):
                continue
            full = os.path.join(dirpath, name)
            try:
                st = os.stat(full)
            except (FileNotFoundError, OSError):
                continue
            if st.st_size < min_size:
                continue
            rel = os.path.relpath(full, root)
            rel_nfc = unicodedata.normalize("NFC", rel)
            if exclude_re is not None and exclude_re.search(rel_nfc):
                continue
            name_nfc = unicodedata.normalize("NFC", name)
            yield (st.st_size, name_nfc.lower(), rel_nfc, int(st.st_mtime))


def _register_helpers(conn: sqlite3.Connection) -> None:
    """Register Python helpers as deterministic SQL functions.

    SQLite has no basename(); registering one keeps the audit query in
    pure SQL while leveraging Python's path semantics. NFC normalization
    is already enforced at scan time, so basename_lower is a pure
    str.lower() of the last path segment.

    Re-registration is a no-op in the C layer (sub-millisecond), and
    sqlite3.Connection does not allow user attributes for caching, so
    the simplest correct implementation is to call this whenever a
    SQL helper is needed. The fully path-based audit path never calls
    `_label_*` helpers and thus never pays even that cost.
    """
    def basename_lower(p):
        if p is None:
            return None
        return os.path.basename(str(p)).lower()
    # deterministic=True lets SQLite reuse results within a single query.
    conn.create_function("basename_lower", 1, basename_lower, deterministic=True)


def _label_signatures(
    conn: sqlite3.Connection, scan_id: int, min_size: int,
    *, mtime_match: bool,
) -> dict[tuple, str]:
    """Pull match-key -> first-found dest path for one scan.

    Key shape:
      mtime_match=True   → (size, basename_lower, int(mtime))
      mtime_match=False  → (size, basename_lower)        # legacy behavior
    """
    _register_helpers(conn)
    out: dict[tuple, str] = {}
    for path, size, mtime, bname in conn.execute(
        "SELECT path, size, mtime, basename_lower(path) AS bname "
        "FROM files WHERE scan_id = ? AND size >= ? ORDER BY path",
        (scan_id, min_size),
    ):
        if bname is None:
            continue
        if mtime_match:
            mt_int = int(mtime) if mtime is not None else 0
            key = (size, bname, mt_int)
        else:
            key = (size, bname)
        out.setdefault(key, path)
    return out


def _label_source_rows(
    conn: sqlite3.Connection, scan_id: int, min_size: int
) -> list[tuple[str, int, str, int]]:
    """Return (path, size, basename_lower, int_mtime) rows for source scan."""
    _register_helpers(conn)
    rows = conn.execute(
        "SELECT path, size, mtime, basename_lower(path) AS bname "
        "FROM files WHERE scan_id = ? AND size >= ? ORDER BY path",
        (scan_id, min_size),
    ).fetchall()
    return [
        (r["path"], r["size"], r["bname"], int(r["mtime"] or 0))
        for r in rows
    ]


# NOTE: a previous _live_signatures() helper used to live here. Removed
# in the mtime-match refactor; callers now drive _iter_live_signatures
# directly with the appropriate per-side options (skip_realpath only on
# dest walks; source walks always include themselves).


def run_audit(
    source: str,
    dest_idents: list[str],
    *,
    min_size: int = 0,
    exclude_pattern: str | None = None,
    db_path: str | None = None,
    early_stop: bool = True,
    persist: bool = True,
    progress_every: int = 10_000,
    out=sys.stderr,
    mtime_match: bool = True,
    mtime_window_seconds: int = 2,
    max_walk_ratio: float | None = 1.5,
) -> AuditResult:
    """Run audit. `source` and each entry in `dest_idents` is either a label
    (already scanned, looked up via SQLite) or a directory path (live-walked).

    Path detection is automatic: a name that contains '/', starts with '~',
    or refers to an existing directory is a path; otherwise it is looked up
    as a label first. Ambiguous names (a directory and a label share a name)
    resolve to the label; pass './name' to force path resolution.

    early_stop: when True (default), live-walked destinations are walked
    only until every source needle has been found in some destination
    (across the full union). Per docs/internal design doc §核心邏輯.2,
    this is the difference between 113 s and >2 h on a 5 TB HDD audit.

    mtime_match: when True (default), the comparison key includes
    int(st.st_mtime), and each source file's match window covers
    mtime ± ``mtime_window_seconds`` (defaults to 2) to absorb the
    cross-filesystem rounding that happens during FAT32 → APFS copies.
    Default 2 (not 1) because FAT32 has 2-second mtime resolution and
    DST boundaries can introduce ±1 hour drift on some pipelines —
    a 1-second window left too many real backups looking "missing",
    risking users deleting source media on a false report.
    Set to False when the user knows their backup pipeline strips
    mtime (e.g. some rsync invocations) — falls back to the legacy
    (size, basename) key.

    max_walk_ratio: walk fallback for destinations where some source
    files genuinely are missing. Without a cap, the all-needles-covered
    condition never fires and the walk degrades to a full HDD pass
    (the headline "113 s for 5 TB" depends on stopping early). When
    `early_stop=True` and the destination walk has visited
    ``max_walk_ratio * src_count`` files, accept current matches and
    move on. Default 1.5; set to None for no cap (matches --no-early-stop
    semantics: full coverage at any cost).

    Raises SystemExit(1) on usage error.
    """
    if not dest_idents:
        print("錯誤：--against 至少需要一個目的地", file=sys.stderr)
        raise SystemExit(1)

    excl_re: re.Pattern[str] | None = re.compile(exclude_pattern) if exclude_pattern else None

    conn = dbmod.get_conn(db_path)
    try:
        src_kind, src_val = _resolve_target(conn, source)
        dest_specs: list[tuple[str, str, object]] = []
        for ident in dest_idents:
            kind, val = _resolve_target(conn, ident)
            dest_specs.append((ident, kind, val))

        # Compute the source-root identity we must NOT re-enter during
        # destination walks. Two signals:
        #   - realpath: catches plain symlinks
        #   - (st_dev, st_ino): catches macOS firmlinks, whose realpaths
        #     do NOT collapse to the source path despite pointing at the
        #     same mount root (kernel namespace trick).
        if src_kind == "path":
            src_root_path = src_val
        else:
            row = conn.execute(
                "SELECT root_path FROM scans WHERE id = ?", (src_val,)
            ).fetchone()
            src_root_path = row["root_path"] if row else None
        src_skip_real: str | None = None
        src_skip_dev_ino: tuple[int, int] | None = None
        if src_root_path:
            try:
                src_skip_real = os.path.realpath(src_root_path)
            except OSError:
                pass
            try:
                st = os.stat(src_root_path)
                src_skip_dev_ino = (st.st_dev, st.st_ino)
            except OSError:
                pass

        # ---- Build source rows + needle set ----
        # src_rows: list of (rel_path, size, bname_lower, int_mtime)
        if src_kind == "label":
            src_rows = _label_source_rows(conn, src_val, min_size)
            if excl_re is not None:
                src_rows = [r for r in src_rows if not excl_re.search(r[0])]
        else:
            print(f"  掃描來源：{src_val}", file=out)
            src_rows = list(_walk_with_progress(
                src_val, min_size=min_size, exclude_re=excl_re,
                progress_every=progress_every, out=out,
                label="source",
            ))
        total_files = len(src_rows)
        total_bytes = sum(row[1] for row in src_rows)

        # Build needle set. With mtime_match, expand each needle into a
        # window of (mtime-w … mtime+w) so destination keys with mtime
        # within ``mtime_window_seconds`` still hit the set in O(1).
        # Also track the canonical (size, bname) source set; that's what
        # early-stop measures progress against (NOT the expanded needle
        # set, whose cardinality is window-dependent).
        needles: set[tuple] = set()
        src_canonical: set[tuple[int, str]] = set()
        for _, sz, bn, mt in src_rows:
            canon = (sz, bn or "")
            src_canonical.add(canon)
            if mtime_match:
                for delta in range(-mtime_window_seconds, mtime_window_seconds + 1):
                    needles.add((sz, bn or "", mt + delta))
            else:
                needles.add(canon)

        def _to_canonical(sig: tuple) -> tuple[int, str]:
            # Drop the mtime bucket when present; first two elems are
            # always (size, basename_lower).
            return (sig[0], sig[1])

        # ---- Walk/query each dest with early-stop awareness ----
        # Coverage is tracked at the canonical (size, bname) level so
        # mtime-window expansion of needles doesn't prevent early-stop
        # from ever firing. per_dest_hits keeps the full per-dest map of
        # actual matched keys (with mtime) → dest path for the location
        # report.
        per_dest_hits: list[tuple[str, str, dict[tuple, str]]] = []
        covered_canonical: set[tuple[int, str]] = set()
        for ident, kind, val in dest_specs:
            if early_stop and covered_canonical == src_canonical and kind == "path":
                # All sources already covered; do not spin this HDD.
                print(f"  {ident}：略過（早停，所有檔案已在前面命中）", file=out)
                per_dest_hits.append((ident, kind, {}))
                continue

            if kind == "label":
                full = _label_signatures(conn, val, min_size, mtime_match=mtime_match)
                this_hits = {sig: p for sig, p in full.items() if sig in needles}
            else:
                print(f"  掃描目的地：{ident}", file=out)
                this_hits = _walk_dest_for_needles(
                    val, needles=needles,
                    src_canonical=src_canonical,
                    covered_canonical=covered_canonical,
                    min_size=min_size, exclude_re=excl_re,
                    early_stop=early_stop,
                    progress_every=progress_every, out=out,
                    label=ident,
                    mtime_match=mtime_match,
                    src_real=src_skip_real,
                    src_dev_ino=src_skip_dev_ino,
                    max_walk_ratio=max_walk_ratio,
                )
            covered_canonical |= {_to_canonical(k) for k in this_hits}
            per_dest_hits.append((ident, kind, this_hits))

        # ---- Single-pass missing + per-dest counts + match locations ----
        # For each source row, ask: was ANY of (size, bn, mt-w … mt+w)
        # found on a destination? If yes, find which destinations and
        # record one (dest_ident, dest_path) per dest.
        missing: list[tuple[str, int]] = []
        per_dest_match: dict[str, int] = {ident: 0 for ident, _, _ in per_dest_hits}
        match_locations: dict[str, list[tuple[str, str]]] = {}
        matched = 0
        for path, size, bname_lower, mt in src_rows:
            bn = bname_lower or ""
            if mtime_match:
                candidates = [
                    (size, bn, mt + d)
                    for d in range(-mtime_window_seconds, mtime_window_seconds + 1)
                ]
            else:
                candidates = [(size, bn)]
            hits_here: list[tuple[str, str]] = []
            any_hit = False
            for ident, _, ds in per_dest_hits:
                # Take the first candidate found in this dest.
                for cand in candidates:
                    if cand in ds:
                        any_hit = True
                        per_dest_match[ident] += 1
                        hits_here.append((ident, ds[cand]))
                        break
            if any_hit:
                matched += 1
                if hits_here:
                    match_locations[path] = hits_here
            else:
                missing.append((path, size))

        # ---- EXIF disambiguation (annotation only, never demotes match) ----
        # Loads exif_cache for source (label-mode only) and each label-mode
        # destination, then tags every match in match_locations with a
        # quality kind. Path-mode sides have no cache → all matches stay
        # "weak", which is the legacy behaviour. This block adds zero
        # changes to `matched` / `missing_paths` regardless of what EXIF
        # says — the iron rule lives here.
        src_exif: dict[str, ExifTriple] = {}
        if src_kind == "label":
            src_exif = _load_exif_cache(conn, src_val)
        dest_exif_caches: dict[str, dict[str, ExifTriple]] = {}
        for ident, kind, val in dest_specs:
            if kind == "label":
                dest_exif_caches[ident] = _load_exif_cache(conn, val)
            # path-mode dest: no cache row exists; lookups return None
            # via dict.get default below.

        match_evidence: dict[str, str] = {}
        for src_path, locations in match_locations.items():
            src_key = src_exif.get(src_path)
            kinds: list[str] = []
            for dest_ident, dest_path in locations:
                dest_cache = dest_exif_caches.get(dest_ident, {})
                dest_key = dest_cache.get(dest_path)
                kinds.append(_classify_evidence(src_key, dest_key))
            match_evidence[src_path] = _aggregate_evidence(kinds)

        result = AuditResult(
            source_ident=source,
            source_kind=src_kind,
            dest_idents=[i for i, _, _ in dest_specs],
            dest_kinds=[k for _, k, _ in dest_specs],
            total_source_files=total_files,
            total_source_bytes=total_bytes,
            matched=matched,
            missing_paths=missing,
            per_dest_match=per_dest_match,
            match_locations=match_locations,
            mtime_match=mtime_match,
            mtime_window_seconds=mtime_window_seconds,
            match_evidence=match_evidence,
        )

        if persist:
            run_id = _persist_audit(
                conn, result,
                source_scan_id=src_val if src_kind == "label" else None,
                min_size=min_size, early_stop=early_stop,
            )
            result.run_id = run_id
            print(f"  已存為稽核記錄 #{run_id}", file=out)

        _print_report(result, out=out)
        return result
    finally:
        conn.close()


def _persist_audit(
    conn: sqlite3.Connection,
    result: AuditResult,
    *,
    source_scan_id: int | None,
    min_size: int,
    early_stop: bool,
) -> int:
    """Write the AuditResult to audit_runs / audit_missing. Returns run_id."""
    import datetime
    import json

    dest_specs = [
        {"ident": ident, "kind": kind}
        for ident, kind in zip(result.dest_idents, result.dest_kinds)
    ]
    ran_at = datetime.datetime.now().isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO audit_runs("
        "source_ident, source_kind, source_scan_id, dest_specs, min_size, "
        "early_stop, total_files, total_bytes, matched_count, missing_count, "
        "missing_bytes, per_dest_match, ran_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            result.source_ident,
            result.source_kind,
            source_scan_id,
            json.dumps(dest_specs, ensure_ascii=False),
            min_size,
            1 if early_stop else 0,
            result.total_source_files,
            result.total_source_bytes,
            result.matched,
            result.missing,
            result.missing_bytes,
            json.dumps(result.per_dest_match, ensure_ascii=False),
            ran_at,
        ),
    )
    run_id = cur.lastrowid

    if result.missing_paths:
        conn.execute("BEGIN")
        try:
            conn.executemany(
                "INSERT INTO audit_missing(run_id, source_path, size) VALUES (?, ?, ?)",
                ((run_id, p, sz) for p, sz in result.missing_paths),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return run_id


# ---------------------------------------------------------------------------
# Walk helpers with progress + early stop
# ---------------------------------------------------------------------------

def _walk_with_progress(
    root: str, *, min_size: int, exclude_re, progress_every: int, out, label: str
):
    """Iterate (rel, size, bname_lower, int_mtime) and report progress."""
    count = 0
    for size, bname_lower, rel, mt in _iter_live_signatures(
        root, min_size=min_size, exclude_re=exclude_re
    ):
        count += 1
        if count % progress_every == 0:
            print(f"  ...{label}: {count} files walked", file=out)
        yield (rel, size, bname_lower, mt)


def _walk_dest_for_needles(
    root: str, *, needles, src_canonical, covered_canonical,
    min_size: int, exclude_re,
    early_stop: bool, progress_every: int, out, label: str,
    mtime_match: bool,
    src_real: str | None = None,
    src_dev_ino: tuple[int, int] | None = None,
    max_walk_ratio: float | None = None,
) -> dict[tuple, str]:
    """Walk a dest directory; return ``{key: dest_relpath}`` for each needle
    found (first match per key wins).

    Coverage is tracked at the canonical (size, bname) level — mtime is
    a tiebreaker, not a separate countable file. Stops early once
    ``covered_canonical | local_canonical == src_canonical``, or once
    we have walked more than ``max_walk_ratio * len(src_canonical)``
    files without converging (the diminishing-returns fallback).
    """
    found: dict[tuple, str] = {}
    found_canonical: set[tuple[int, str]] = set()
    count = 0
    target = len(src_canonical)
    # Walk-ratio fallback: when source has missing files, the all-covered
    # condition can never fire and the walk degrades to a full HDD pass.
    # The "113 s for 5 TB" headline depends on stopping well before that.
    # Cap the walk at max_walk_ratio * source-size files, after which we
    # accept whatever has been found and move on. None disables the cap
    # (used by --no-early-stop, which is documented as full-coverage).
    # Floor of `target + 64`: the ratio collapses for small targets
    # (target=1 × 1.5 = 1, capping before any unmatched file can be
    # skipped past). The +64 overhead lets the walk look past adjacent
    # non-matches before declaring failure, without affecting realistic
    # photographer-sized sources where ratio dominates.
    walk_cap = (
        max(int(target * max_walk_ratio), target + 64)
        if early_stop and max_walk_ratio is not None and target > 0
        else None
    )
    for size, bname_lower, rel, mt in _iter_live_signatures(
        root, min_size=min_size, exclude_re=exclude_re,
        src_real=src_real, src_dev_ino=src_dev_ino,
    ):
        count += 1
        if count % progress_every == 0:
            covered = len(covered_canonical | found_canonical)
            print(
                f"  ...{label}: {count} walked, {covered}/{target} needles found",
                file=out,
            )
        sig = (size, bname_lower, mt) if mtime_match else (size, bname_lower)
        if sig in needles and sig not in found:
            found[sig] = rel
            found_canonical.add((size, bname_lower))
            if early_stop and (covered_canonical | found_canonical) == src_canonical:
                print(
                    f"  {label}: early-stop after {count} files (all needles covered)",
                    file=out,
                )
                break
        if walk_cap is not None and count >= walk_cap:
            covered = len(covered_canonical | found_canonical)
            print(
                f"  {label}: walk-cap reached after {count} files "
                f"({covered}/{target} needles covered; "
                f"--max-walk-ratio limit hit, see --no-early-stop for full)",
                file=out,
            )
            break
    return found


# Back-compat alias: the SQL-only entry point used to be called run_audit_sql.
def run_audit_sql(
    source_label: str,
    dest_labels: list[str],
    *,
    min_size: int = 0,
    exclude_pattern: str | None = None,
    db_path: str | None = None,
    out=sys.stderr,
) -> AuditResult:
    return run_audit(
        source_label,
        dest_labels,
        min_size=min_size,
        exclude_pattern=exclude_pattern,
        db_path=db_path,
        out=out,
    )


def _print_report(r: AuditResult, *, out) -> None:
    """Human-readable summary, written to `out` (default stderr).

    Stdout is reserved for a future --json mode (matches scan.py convention
    established in commit b499815).
    """
    pct = (100.0 * r.matched / r.total_source_files) if r.total_source_files else 0.0
    # Mark live-walked targets so the user can tell which side hit a real disk.
    def _annot(ident: str, kind: str) -> str:
        return f"{ident}（現場）" if kind == "path" else ident
    dest_join = "、".join(_annot(i, k) for i, k in zip(r.dest_idents, r.dest_kinds))
    src_disp = _annot(r.source_ident, r.source_kind)
    print(
        f"{src_disp} → [{dest_join}]\n"
        f"  來源：{r.total_source_files} 檔 / {human_size(r.total_source_bytes)}\n"
        f"  ✓ 已備份：{r.matched} ({pct:.1f}%)\n"
        f"  ✗ 未備份：{r.missing} ({human_size(r.missing_bytes)})",
        file=out,
    )

    if r.missing_paths:
        folders: dict[str, list[tuple[str, int]]] = defaultdict(list)
        for path, size in r.missing_paths:
            d = os.path.dirname(path) or "(根目錄)"
            folders[d].append((path, size))
        print("\n  未備份檔案（按來源資料夾分組）：", file=out)
        for folder in sorted(folders):
            files = folders[folder]
            total = sum(s for _, s in files)
            print(f"    📁 {folder} ({len(files)} 檔, {human_size(total)})", file=out)
            for path, size in files[:5]:
                print(f"        - {path}  [{human_size(size)}]", file=out)
            if len(files) > 5:
                print(f"        ... 還有 {len(files) - 5} 個", file=out)

    if r.per_dest_match:
        print("\n  各備份目的地命中數：", file=out)
        for ident, count in r.per_dest_match.items():
            print(f"    [{ident}] {count} 檔", file=out)

    if r.match_locations:
        # Print first 5 matches with destination paths so the user can
        # eyeball-verify the audit (camera-numbering collisions etc.).
        sample = list(r.match_locations.items())[:5]
        print(
            "\n  ⚠ 已備份檔案的目的地對應（前 5 筆，請肉眼驗證是否真的是同一個檔）：",
            file=out,
        )
        for src_path, hits in sample:
            print(f"    {src_path}", file=out)
            for ident, dest_path in hits:
                print(f"        ↳ [{ident}] {dest_path}", file=out)
        if len(r.match_locations) > 5:
            print(f"    ... 還有 {len(r.match_locations) - 5} 個（GUI / HTML 報告有完整列表）", file=out)

    # EXIF evidence summary: only print when at least one match has
    # non-weak evidence — otherwise it's pure noise for legacy users
    # who scanned without --exif. The conflict listing is what the
    # user actually needs to act on (manual verify before formatting).
    if r.match_evidence:
        kind_counts = {"strong": 0, "weak": 0, "conflict": 0}
        for kind in r.match_evidence.values():
            kind_counts[kind] = kind_counts.get(kind, 0) + 1
        if kind_counts["strong"] or kind_counts["conflict"]:
            print("\n  EXIF 訊號：", file=out)
            print(
                f"    ✓ strong: {kind_counts['strong']} 檔（兩邊 EXIF 對得上）",
                file=out,
            )
            print(
                f"    · weak:   {kind_counts['weak']} 檔（沒 EXIF 或資訊不夠）",
                file=out,
            )
            print(
                f"    ⚠ conflict: {kind_counts['conflict']} 檔（請手動確認）",
                file=out,
            )
            if kind_counts["conflict"]:
                conflicts = [p for p, k in r.match_evidence.items() if k == "conflict"]
                for path in conflicts[:10]:
                    print(f"        - {path}", file=out)
                if len(conflicts) > 10:
                    print(f"        ... 還有 {len(conflicts) - 10} 個（GUI 報告有完整列表）", file=out)
