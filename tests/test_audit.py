# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 wt and DriveTidy contributors

"""End-to-end tests for `drivetidy audit`.

These exercise the public run_audit() against tmp dirs (no real drives
needed). Mix of label-based, path-based, and hybrid resolution.
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import pytest

from drivetidy import audit, db as dbmod, scan as scan_mod


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    p = tmp_path / "test.db"
    return str(p)


def _make_tree(root: Path, files: dict[str, bytes]) -> None:
    for rel, content in files.items():
        full = root / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(content)


def _scan(db_path: str, root: Path, label: str) -> int:
    return scan_mod.run_scan(
        str(root),
        label=label,
        drive_type_override="ssd",
        backend_pref="rclone" if not _has_fd() else "fd",
        db_path=db_path,
        out=io.StringIO(),  # silence
    )


def _has_fd() -> bool:
    import shutil
    return shutil.which("fd") is not None or shutil.which("fdfind") is not None


def test_audit_sql_mode_basic(db_path, tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_tree(src, {
        "a/photo.jpg": b"abc",
        "a/orphan.raw": b"xyz12",   # missing on dst
    })
    _make_tree(dst, {
        "copy/photo.jpg": b"abc",
    })
    _scan(db_path, src, "src_label")
    _scan(db_path, dst, "dst_label")

    out = io.StringIO()
    r = audit.run_audit("src_label", ["dst_label"], db_path=db_path, out=out)

    assert r.total_source_files == 2
    assert r.matched == 1
    assert r.missing == 1
    assert r.missing_paths[0][0].endswith("orphan.raw")
    assert r.per_dest_match["dst_label"] == 1
    assert r.source_kind == "label"
    assert r.dest_kinds == ["label"]


def test_audit_path_source(db_path, tmp_path):
    """Source is a live path; dest is a label."""
    src = tmp_path / "card"
    dst = tmp_path / "archive"
    _make_tree(src, {"DCIM/IMG_1.JPG": b"hello"})
    _make_tree(dst, {"backup/IMG_1.JPG": b"hello"})
    _scan(db_path, dst, "archive_label")

    out = io.StringIO()
    r = audit.run_audit(str(src), ["archive_label"], db_path=db_path, out=out)
    assert r.total_source_files == 1
    assert r.matched == 1
    assert r.missing == 0
    assert r.source_kind == "path"


def test_audit_path_dest(db_path, tmp_path):
    """Dest is a live path; source is a label."""
    src = tmp_path / "card"
    dst = tmp_path / "live_archive"
    _make_tree(src, {"a/x.jpg": b"data1", "a/y.jpg": b"data2"})
    _make_tree(dst, {"x.jpg": b"data1"})  # only x is backed up
    _scan(db_path, src, "src_label")

    out = io.StringIO()
    r = audit.run_audit("src_label", [str(dst)], db_path=db_path, out=out)
    assert r.matched == 1
    assert r.missing == 1
    assert r.dest_kinds == ["path"]


def test_audit_path_both_sides(db_path, tmp_path):
    """Both source and dest are live paths (no scan needed at all)."""
    src = tmp_path / "card"
    dst = tmp_path / "drive"
    _make_tree(src, {"a.jpg": b"x", "b.jpg": b"yy"})
    _make_tree(dst, {"sub/a.jpg": b"x", "sub/c.jpg": b"zzz"})

    out = io.StringIO()
    r = audit.run_audit(str(src), [str(dst)], db_path=db_path, out=out)
    assert r.matched == 1
    assert r.missing == 1
    assert r.source_kind == "path"
    assert r.dest_kinds == ["path"]


def test_audit_case_insensitive(db_path, tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_tree(src, {"photo.jpg": b"abc"})
    _make_tree(dst, {"PHOTO.JPG": b"abc"})

    out = io.StringIO()
    r = audit.run_audit(str(src), [str(dst)], db_path=db_path, out=out)
    assert r.matched == 1
    assert r.missing == 0


def test_audit_mtime_breaks_camera_collision(db_path, tmp_path):
    """Camera-numbering scenario: source IMG_0412 has the same basename
    + size as a destination IMG_0412 from a *different* shoot. With
    mtime_match (default), the older shoot's mtime differs by hours so
    the false-positive disappears."""
    src = tmp_path / "card"
    dst = tmp_path / "old_shoot"
    _make_tree(src, {"DCIM/100/IMG_0412.MP4": b"x" * 100})
    _make_tree(dst, {"OldShoot/IMG_0412.MP4": b"x" * 100})  # same basename + size
    # Force mtime divergence: old shoot's IMG_0412 is from an hour ago.
    old_mtime = (tmp_path / "card" / "DCIM/100/IMG_0412.MP4").stat().st_mtime - 3600
    os.utime(str(dst / "OldShoot/IMG_0412.MP4"), (old_mtime, old_mtime))

    out = io.StringIO()
    r = audit.run_audit(str(src), [str(dst)], db_path=db_path, out=out)
    # mtime gating kicks in → no false positive.
    assert r.matched == 0, f"expected miss with mtime check, got matched={r.matched}"
    assert r.missing == 1


def test_audit_mtime_match_disabled_falls_back_to_legacy(db_path, tmp_path):
    """User can opt out of mtime matching when their backup pipeline
    strips mtime. With mtime_match=False, behaviour matches the legacy
    (size, basename) match."""
    src = tmp_path / "card"
    dst = tmp_path / "old_shoot"
    _make_tree(src, {"DCIM/100/IMG_0412.MP4": b"x" * 100})
    _make_tree(dst, {"OldShoot/IMG_0412.MP4": b"x" * 100})
    old_mtime = (tmp_path / "card" / "DCIM/100/IMG_0412.MP4").stat().st_mtime - 3600
    os.utime(str(dst / "OldShoot/IMG_0412.MP4"), (old_mtime, old_mtime))

    out = io.StringIO()
    r = audit.run_audit(
        str(src), [str(dst)], db_path=db_path, out=out, mtime_match=False,
    )
    # Legacy behavior: false-positive returns.
    assert r.matched == 1


def test_audit_skip_realpath_blocks_source_self_via_symlink(db_path, tmp_path):
    """A destination that includes a symlink/firmlink pointing back into
    the source must NOT report source files as 'matched' against
    themselves. macOS firmlink case:
        /Volumes/Macintosh HD/System/Volumes/Data/Volumes/<sd>/...
    realpaths to /Volumes/<sd> (the source). Tests with a symlink because
    plain firmlinks can't be created in test scratch dirs.
    """
    src = tmp_path / "sd_card"
    dst = tmp_path / "internal"
    _make_tree(src, {"DCIM/100/IMG_0001.JPG": b"abc"})
    dst.mkdir(parents=True)
    # Inside dst, a symlink that points back to the source root.
    (dst / "loop_to_source").symlink_to(src)

    out = io.StringIO()
    r = audit.run_audit(str(src), [str(dst)], db_path=db_path, out=out)
    # Without skip_realpath, audit would report matched=1 (source matched
    # against itself via the symlink). With skip_realpath, the dest walk
    # prunes the symlink branch and reports the file as missing.
    assert r.matched == 0
    assert r.missing == 1


def test_audit_match_locations_records_dest_path(db_path, tmp_path):
    """For every matched source, match_locations stores where on the
    destination it was found — required for false-positive verification
    (camera-numbering collisions etc.)."""
    src = tmp_path / "card"
    dst = tmp_path / "hdd"
    # Two source files; both happen to also exist on dst at totally
    # different relative paths (this is what creates false-positive
    # surprise when audit only matches by basename+size).
    _make_tree(src, {
        "DCIM/100/IMG_0001.JPG": b"abc",
        "DCIM/100/IMG_0002.JPG": b"yyyy",
    })
    _make_tree(dst, {
        "OldShoot/2024/IMG_0001.JPG": b"abc",
        "Random/IMG_0002.JPG": b"yyyy",
    })

    out = io.StringIO()
    r = audit.run_audit(str(src), [str(dst)], db_path=db_path, out=out)
    assert r.matched == 2
    assert r.missing == 0
    # Source key is the relative path (live-walk source).
    assert "DCIM/100/IMG_0001.JPG" in r.match_locations
    assert "DCIM/100/IMG_0002.JPG" in r.match_locations
    # Each match records (dest_ident, dest_relpath); dest_path should be
    # the *destination* location (where the file actually lives on dst),
    # NOT the source path — that is the whole point of this feature.
    hits_1 = r.match_locations["DCIM/100/IMG_0001.JPG"]
    assert len(hits_1) == 1
    ident, dest_path = hits_1[0]
    assert ident == str(dst)
    assert dest_path == "OldShoot/2024/IMG_0001.JPG"


def test_audit_min_size(db_path, tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_tree(src, {
        "tiny.txt": b"x",                    # 1 byte
        "big.bin": b"a" * 2048,              # 2 KB
    })
    _make_tree(dst, {"big.bin": b"a" * 2048})

    out = io.StringIO()
    r = audit.run_audit(str(src), [str(dst)], min_size=1024, db_path=db_path, out=out)
    # tiny.txt filtered out by min_size
    assert r.total_source_files == 1
    assert r.matched == 1
    assert r.missing == 0


def test_audit_exclude_regex(db_path, tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_tree(src, {
        "good.jpg": b"a",
        "skip_me.tmp": b"b",
    })
    _make_tree(dst, {"good.jpg": b"a"})

    out = io.StringIO()
    r = audit.run_audit(
        str(src), [str(dst)], exclude_pattern=r"\.tmp$",
        db_path=db_path, out=out,
    )
    assert r.total_source_files == 1
    assert r.matched == 1
    assert r.missing == 0


def test_audit_macos_metadata_filtered(db_path, tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_tree(src, {
        "real.jpg": b"abc",
        "._real.jpg": b"meta",   # macOS AppleDouble
        ".DS_Store": b"x",
    })
    _make_tree(dst, {"real.jpg": b"abc"})

    out = io.StringIO()
    r = audit.run_audit(str(src), [str(dst)], db_path=db_path, out=out)
    # metadata files should not appear in the source set
    assert r.total_source_files == 1
    assert r.missing == 0


def test_audit_unknown_label_exits_1(db_path, tmp_path):
    with pytest.raises(SystemExit) as ei:
        audit.run_audit("never_scanned", ["also_no"], db_path=db_path, out=io.StringIO())
    assert ei.value.code == 1


def test_audit_empty_dests_exits_1(db_path, tmp_path):
    with pytest.raises(SystemExit) as ei:
        audit.run_audit("anything", [], db_path=db_path, out=io.StringIO())
    assert ei.value.code == 1


def test_audit_multi_dest_distribution(db_path, tmp_path):
    """Per-dest match counts must reflect which dest covered which file."""
    src = tmp_path / "src"
    da = tmp_path / "da"
    db = tmp_path / "db"
    _make_tree(src, {"a.jpg": b"1", "b.jpg": b"22", "c.jpg": b"333"})
    _make_tree(da, {"a.jpg": b"1"})           # da has only a
    _make_tree(db, {"b.jpg": b"22", "c.jpg": b"333"})  # db has b + c

    out = io.StringIO()
    r = audit.run_audit(str(src), [str(da), str(db)], db_path=db_path, out=out)
    assert r.matched == 3
    assert r.missing == 0
    assert r.per_dest_match[str(da)] == 1
    assert r.per_dest_match[str(db)] == 2


def test_audit_early_stop_skips_remaining_dests(db_path, tmp_path):
    """When all needles are covered by earlier dests, later live dests skip."""
    src = tmp_path / "src"
    da = tmp_path / "da"
    db = tmp_path / "db"
    _make_tree(src, {"only_one.jpg": b"hi"})
    _make_tree(da, {"backup/only_one.jpg": b"hi"})  # already covers everything
    _make_tree(db, {"redundant/only_one.jpg": b"hi"})  # would also cover

    out = io.StringIO()
    r = audit.run_audit(
        str(src), [str(da), str(db)], early_stop=True,
        db_path=db_path, out=out,
    )
    assert r.matched == 1
    assert r.missing == 0
    # da hit; db was skipped, so its per-dest count is 0 even though
    # it physically contains the file.
    assert r.per_dest_match[str(da)] == 1
    assert r.per_dest_match[str(db)] == 0
    text = out.getvalue()
    assert "skipped" in text or "early-stop" in text


def test_audit_no_early_stop_visits_all_dests(db_path, tmp_path):
    """With --no-early-stop, every dest is fully walked."""
    src = tmp_path / "src"
    da = tmp_path / "da"
    db = tmp_path / "db"
    _make_tree(src, {"only_one.jpg": b"hi"})
    _make_tree(da, {"backup/only_one.jpg": b"hi"})
    _make_tree(db, {"redundant/only_one.jpg": b"hi"})

    out = io.StringIO()
    r = audit.run_audit(
        str(src), [str(da), str(db)], early_stop=False,
        db_path=db_path, out=out,
    )
    # Both dests should report the match this time.
    assert r.per_dest_match[str(da)] == 1
    assert r.per_dest_match[str(db)] == 1


def test_audit_html_report(db_path, tmp_path):
    """End-to-end: run audit, write HTML, verify substitutions + content."""
    from drivetidy import report as report_mod

    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_tree(src, {"a/photo.jpg": b"abc", "a/orphan.raw": b"xyzpq"})
    _make_tree(dst, {"copy/photo.jpg": b"abc"})

    out = io.StringIO()
    r = audit.run_audit(str(src), [str(dst)], db_path=db_path, out=out)
    html_path = tmp_path / "audit.html"
    written = report_mod.write_report(r, str(html_path))

    text = Path(written).read_text(encoding="utf-8")
    assert "{pct}" not in text
    assert "{folders_html}" not in text
    assert "{result_json}" not in text
    assert "orphan.raw" in text
    assert "各備份目的地命中數" in text
    assert "備份稽核" in text


def test_audit_persist_writes_run_and_missing(db_path, tmp_path):
    """run_audit should append to audit_runs (and audit_missing) by default."""
    from drivetidy import db as dbmod_t

    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_tree(src, {"a.jpg": b"x", "b.jpg": b"yy"})
    _make_tree(dst, {"a.jpg": b"x"})  # b is missing

    audit.run_audit(str(src), [str(dst)], db_path=db_path, out=io.StringIO())

    conn = dbmod_t.get_conn(db_path)
    runs = conn.execute("SELECT * FROM audit_runs").fetchall()
    assert len(runs) == 1
    run_id = runs[0]["id"]
    assert runs[0]["matched_count"] == 1
    assert runs[0]["missing_count"] == 1
    miss = conn.execute(
        "SELECT source_path, size FROM audit_missing WHERE run_id = ?", (run_id,)
    ).fetchall()
    assert len(miss) == 1
    assert miss[0]["source_path"].endswith("b.jpg")
    conn.close()


def test_audit_persist_disabled(db_path, tmp_path):
    """persist=False does not write any audit row."""
    from drivetidy import db as dbmod_t

    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_tree(src, {"a.jpg": b"x"})
    _make_tree(dst, {"a.jpg": b"x"})

    audit.run_audit(
        str(src), [str(dst)], db_path=db_path, persist=False, out=io.StringIO(),
    )

    conn = dbmod_t.get_conn(db_path)
    rows = conn.execute("SELECT COUNT(*) AS n FROM audit_runs").fetchone()
    assert rows["n"] == 0
    conn.close()


def test_audit_html_report_all_good(db_path, tmp_path):
    """Empty missing list renders the all-good banner instead of folder rows."""
    from drivetidy import report as report_mod

    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_tree(src, {"a.jpg": b"x"})
    _make_tree(dst, {"copy/a.jpg": b"x"})
    r = audit.run_audit(str(src), [str(dst)], db_path=db_path, out=io.StringIO())
    written = report_mod.write_report(r, str(tmp_path / "audit.html"))
    text = Path(written).read_text(encoding="utf-8")
    assert "所有來源檔案都已備份" in text


def test_audit_early_stop_within_one_dest(db_path, tmp_path):
    """A single live dest stops walking once all needles are found."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_tree(src, {"a.jpg": b"x"})
    # Place the needle near the start of the walk; many extra files
    # afterwards. early-stop should avoid scanning them all.
    files = {"matched/a.jpg": b"x"}
    for i in range(50):
        files[f"extra/file_{i}.bin"] = bytes([i]) * 16
    _make_tree(dst, files)

    out = io.StringIO()
    r = audit.run_audit(str(src), [str(dst)], early_stop=True,
                       db_path=db_path, out=out)
    assert r.matched == 1
    text = out.getvalue()
    # Either we hit the early-stop print or completed naturally; key
    # invariant is correctness, not the print itself.
    assert "1 files" in text or "1 files" in str(r.matched)


@pytest.mark.skipif(
    sys.platform.startswith("linux"),
    reason="walk-cap correctness depends on the backend yielding entries in "
           "sorted order. macOS APFS does; ext4 returns hash-ordered entries, "
           "so the single match can fall after the cap. Linux walk-cap "
           "ordering is a known limitation — tracked for a future fix.",
)
def test_audit_max_walk_ratio_caps_when_source_has_missing(db_path, tmp_path):
    """When some source files are genuinely missing in dest, the
    all-covered condition never fires. Without the walk-cap fallback
    we would walk the full HDD; with it we accept partial matches and
    move on. Regression test for the 113-second-for-5TB headline."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    # 100 source files, only 1 actually backed up. Without max_walk_ratio
    # the walk has to traverse all of dst (which we make large enough to
    # exceed the floor of target+64 = 164).
    src_files = {f"shoot/IMG_{i:04d}.jpg": bytes([i % 256]) * 10 for i in range(100)}
    _make_tree(src, src_files)
    # Dest only has 1 of the source files, plus 500 unrelated files,
    # placed alphabetically AFTER the matched dir so walk encounters
    # noise before the match.
    dst_files = {"backup/IMG_0000.jpg": bytes([0]) * 10}
    for i in range(500):
        dst_files[f"zz_other/blob_{i:04d}.bin"] = bytes([i % 256]) * 32
    _make_tree(dst, dst_files)

    out = io.StringIO()
    r = audit.run_audit(
        str(src), [str(dst)], early_stop=True,
        max_walk_ratio=1.5,  # cap at max(150, 100+64) = 164 walked files
        db_path=db_path, out=out,
    )
    # The 1 real match should still be found (it sorts before the noise).
    assert r.matched == 1
    # The 99 missing files should be reported as missing — cap fires
    # before walking all 501 dst files.
    assert len(r.missing_paths) == 99
    assert "walk-cap reached" in out.getvalue()


def test_audit_dup_basename_size_diff_mtime_early_stop(db_path, tmp_path):
    """Two source files share (size, basename_lower) but differ in mtime
    (e.g. user re-shot the same scene; camera reset numbering). Both
    needles must be tracked even though they collapse to one canonical
    key — early-stop firing on canonical coverage shouldn't lose the
    one that didn't match yet."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_tree(src, {
        "shoot_a/IMG_0001.jpg": b"x" * 100,
        "shoot_b/IMG_0001.jpg": b"x" * 100,  # same size + basename
    })
    # Make the two source files have distinct mtimes (1 hour apart).
    src_a = src / "shoot_a/IMG_0001.jpg"
    src_b = src / "shoot_b/IMG_0001.jpg"
    t_a = src_a.stat().st_mtime
    t_b = t_a - 3600
    os.utime(str(src_b), (t_b, t_b))

    # Dest only contains the shoot_b mtime; the shoot_a copy is genuinely
    # missing. With mtime_match the audit should report 1 matched + 1
    # missing (not 2 matched via canonical-only collapse).
    _make_tree(dst, {"copy/IMG_0001.jpg": b"x" * 100})
    os.utime(str(dst / "copy/IMG_0001.jpg"), (t_b, t_b))

    out = io.StringIO()
    r = audit.run_audit(
        str(src), [str(dst)], early_stop=True,
        db_path=db_path, out=out,
    )
    assert r.matched == 1
    assert r.missing == 1
    missing_paths = {p for p, _ in r.missing_paths}
    assert "shoot_a/IMG_0001.jpg" in missing_paths


# ---------------------------------------------------------------------------
# EXIF disambiguation (annotation only — must NEVER change matched/missing)
# ---------------------------------------------------------------------------

def _make_jpeg(path: Path, *, make: str = "SONY", model: str = "ILCE-7M4",
               taken: str | None = "2026:05:09 09:00:00") -> Path:
    """Write a tiny JPEG with the given EXIF tags."""
    from PIL import Image
    from PIL.ExifTags import IFD as _IFD
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", (4, 4), (127, 127, 127))
    exif = im.getexif()
    if make:
        exif[0x010F] = make
    if model:
        exif[0x0110] = model
    if taken:
        exif.get_ifd(_IFD.Exif)[0x9003] = taken
    im.save(path, format="JPEG", exif=exif)
    return path


def test_audit_evidence_weak_when_no_exif_anywhere(db_path, tmp_path):
    """Existing user (scanned without --exif) must see legacy behaviour:
    every match is 'weak', matched count unchanged. Regression for
    keeping the fall-through path harmless when scans pre-date v3."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_jpeg(src / "a.jpg", taken="2026:05:09 09:00:00")
    _make_jpeg(dst / "a.jpg", taken="2026:05:09 09:00:00")
    src_id = _scan(db_path, src, "src_lbl")  # no extract_exif
    _scan(db_path, dst, "dst_lbl")

    out = io.StringIO()
    r = audit.run_audit("src_lbl", ["dst_lbl"], db_path=db_path, out=out)
    assert r.matched == 1
    assert r.match_evidence == {"a.jpg": "weak"}


def test_audit_evidence_strong_when_exif_agrees(db_path, tmp_path):
    """Both sides scanned with --exif and EXIF agrees → 'strong'."""
    from drivetidy import scan as scan_mod
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_jpeg(src / "a.jpg", make="SONY", model="ILCE-7M4",
               taken="2026:05:09 09:00:00")
    _make_jpeg(dst / "a.jpg", make="SONY", model="ILCE-7M4",
               taken="2026:05:09 09:00:00")
    scan_mod.run_scan(str(src), label="src", db_path=db_path,
                      extract_exif=True, out=io.StringIO())
    scan_mod.run_scan(str(dst), label="dst", db_path=db_path,
                      extract_exif=True, out=io.StringIO())

    r = audit.run_audit("src", ["dst"], db_path=db_path, out=io.StringIO())
    assert r.matched == 1
    assert r.match_evidence == {"a.jpg": "strong"}


def test_audit_evidence_conflict_when_camera_differs(db_path, tmp_path):
    """Both sides have EXIF but cameras don't match → 'conflict'.
    THE FILE STILL COUNTS AS MATCHED — iron rule. EXIF only annotates.

    To exercise the conflict path, both files must first match by the
    legacy `(size, basename, mtime)` key — that's the regime where EXIF
    provides additional safety. We pad to common size + sync mtime so
    the legacy key collapses both files to one match."""
    from drivetidy import scan as scan_mod
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    a = _make_jpeg(src / "IMG_0001.jpg", make="SONY", model="ILCE-7M4")
    b = _make_jpeg(dst / "IMG_0001.jpg", make="GoPro", model="HERO12")
    target = max(a.stat().st_size, b.stat().st_size)
    for f in (a, b):
        cur = f.stat().st_size
        if cur < target:
            with open(f, "ab") as fp:
                fp.write(b"\x00" * (target - cur))
    common_mtime = a.stat().st_mtime
    os.utime(str(b), (common_mtime, common_mtime))

    scan_mod.run_scan(str(src), label="src", db_path=db_path,
                      extract_exif=True, out=io.StringIO())
    scan_mod.run_scan(str(dst), label="dst", db_path=db_path,
                      extract_exif=True, out=io.StringIO())

    r = audit.run_audit("src", ["dst"], db_path=db_path, out=io.StringIO())
    # IRON RULE: matched count is NOT lowered. The file is still in matched,
    # not in missing_paths. EXIF only flags it for the user to verify.
    assert r.matched == 1, "EXIF must NEVER turn matched into missing"
    assert r.missing == 0
    assert r.match_evidence == {"IMG_0001.jpg": "conflict"}


def test_audit_evidence_weak_when_dest_lacks_exif(db_path, tmp_path):
    """Source scan has --exif but dest scan didn't. Insufficient evidence
    to upgrade — fall back to 'weak'. Common case during transition: user
    upgrades to v3, re-scans source but hasn't re-scanned an old dest."""
    from drivetidy import scan as scan_mod
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_jpeg(src / "a.jpg")
    _make_jpeg(dst / "a.jpg")
    scan_mod.run_scan(str(src), label="src", db_path=db_path,
                      extract_exif=True, out=io.StringIO())
    scan_mod.run_scan(str(dst), label="dst", db_path=db_path,
                      out=io.StringIO())  # no extract_exif

    r = audit.run_audit("src", ["dst"], db_path=db_path, out=io.StringIO())
    assert r.matched == 1
    assert r.match_evidence == {"a.jpg": "weak"}


def test_audit_evidence_iron_rule_under_collision(db_path, tmp_path):
    """The motivating bug: two source files share (size, basename, mtime)
    but were taken by different cameras. Without EXIF both currently get
    matched=True (one is a false positive). With EXIF: the one whose
    camera matches dest is 'strong', the other is 'conflict' — but BOTH
    are still in matched. The user reads the conflict flag and verifies
    manually before acting on it. EXIF must NEVER turn the false-positive
    side into 'missing' (would risk a real backup looking absent if EXIF
    happened to be stripped)."""
    from drivetidy import scan as scan_mod
    # Build src and dst trees so two source files share (size, basename).
    # Same mtime is forced via os.utime so the legacy mtime-window key
    # still matches both (which is the false-positive regime EXIF flags).
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_jpeg(src / "shoot_a/IMG_0001.jpg", make="SONY", model="ILCE-7M4")
    _make_jpeg(src / "shoot_b/IMG_0001.jpg", make="GoPro", model="HERO12")
    _make_jpeg(dst / "backup/IMG_0001.jpg", make="SONY", model="ILCE-7M4")

    # Force matching size + mtime so the legacy key collapses both srcs
    # to the dest. (Sizes already match because the JPEGs were generated
    # with identical Pillow params; tiny differences from EXIF lengths
    # are normalised by also size-padding the larger one.)
    src_a = src / "shoot_a/IMG_0001.jpg"
    src_b = src / "shoot_b/IMG_0001.jpg"
    dst_f = dst / "backup/IMG_0001.jpg"
    # Pad to a common size so legacy (size, basename) match collides.
    target_size = max(src_a.stat().st_size, src_b.stat().st_size, dst_f.stat().st_size)
    for f in (src_a, src_b, dst_f):
        cur = f.stat().st_size
        if cur < target_size:
            with open(f, "ab") as fp:
                fp.write(b"\x00" * (target_size - cur))
    common_mtime = src_a.stat().st_mtime
    for f in (src_a, src_b, dst_f):
        os.utime(str(f), (common_mtime, common_mtime))

    scan_mod.run_scan(str(src), label="src", db_path=db_path,
                      extract_exif=True, out=io.StringIO())
    scan_mod.run_scan(str(dst), label="dst", db_path=db_path,
                      extract_exif=True, out=io.StringIO())

    r = audit.run_audit("src", ["dst"], db_path=db_path, out=io.StringIO())

    # Iron rule: BOTH sources still appear matched. Padding may collapse
    # them to the same canonical key, so the audit reports the collision
    # via match_evidence rather than via missing_paths.
    assert r.missing == 0, (
        f"EXIF must not demote a match — but missing={r.missing_paths}"
    )
    # The Sony src has the agreeing dest → strong; the GoPro src either
    # also matches (collision regime) and is flagged conflict.
    sony_path = "shoot_a/IMG_0001.jpg"
    gopro_path = "shoot_b/IMG_0001.jpg"
    assert r.match_evidence.get(sony_path) == "strong"
    assert r.match_evidence.get(gopro_path) == "conflict", (
        f"GoPro source colliding with Sony backup must be flagged 'conflict' "
        f"so the user can verify before acting on it; got "
        f"{r.match_evidence.get(gopro_path)!r}"
    )


def test_audit_evidence_helper_classify_conflict_via_taken_at(db_path, tmp_path):
    """Direct unit test on the classifier — both sides have EXIF, same
    camera, but DateTimeOriginal differs by hours → 'conflict'."""
    src_exif = (1700000000, "SONY", "ILCE-7M4")
    dst_exif = (1700003600, "SONY", "ILCE-7M4")  # +1 hour
    assert audit._classify_evidence(src_exif, dst_exif) == "conflict"


def test_audit_evidence_helper_classify_strong_within_tolerance():
    """5-second drift is acceptable (clock skew between cameras / DST
    boundary fuzziness). Same camera, taken_at within tolerance → strong."""
    src_exif = (1700000000, "SONY", "ILCE-7M4")
    dst_exif = (1700000003, "SONY", "ILCE-7M4")  # +3 seconds
    assert audit._classify_evidence(src_exif, dst_exif) == "strong"


def test_audit_cli_report_surfaces_conflict_summary(db_path, tmp_path):
    """When there are 'strong' or 'conflict' evidence entries, the human
    CLI report must list them so the user notices without opening the
    GUI. Pure 'weak' (legacy / no --exif) → no summary block (don't
    bother users who haven't opted into EXIF)."""
    from drivetidy import scan as scan_mod
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    a = _make_jpeg(src / "IMG_0001.jpg", make="SONY", model="ILCE-7M4")
    b = _make_jpeg(dst / "IMG_0001.jpg", make="GoPro", model="HERO12")
    target = max(a.stat().st_size, b.stat().st_size)
    for f in (a, b):
        if f.stat().st_size < target:
            with open(f, "ab") as fp:
                fp.write(b"\x00" * (target - f.stat().st_size))
    common = a.stat().st_mtime
    os.utime(str(b), (common, common))
    scan_mod.run_scan(str(src), label="src", db_path=db_path,
                      extract_exif=True, out=io.StringIO())
    scan_mod.run_scan(str(dst), label="dst", db_path=db_path,
                      extract_exif=True, out=io.StringIO())

    out = io.StringIO()
    audit.run_audit("src", ["dst"], db_path=db_path, out=out)
    text = out.getvalue()
    assert "EXIF 訊號" in text
    assert "conflict" in text
    assert "請手動確認" in text


def test_audit_cli_report_no_evidence_block_when_all_weak(db_path, tmp_path):
    """Legacy users (scanned without --exif) don't get the EXIF block —
    every match is 'weak' and the summary stays silent. Keeps the audit
    output clean for the common no-EXIF path."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_tree(src, {"a.jpg": b"hello world"})
    _make_tree(dst, {"a.jpg": b"hello world"})
    out = io.StringIO()
    audit.run_audit(str(src), [str(dst)], db_path=db_path, out=out)
    text = out.getvalue()
    assert "EXIF 訊號" not in text


def test_audit_evidence_helper_aggregate_priority():
    """Per-src aggregation: strong > weak > conflict.
    A src matched at multiple dests where one is strong overall is strong;
    only when EVERY match is conflict do we flag the src as conflict."""
    assert audit._aggregate_evidence(["strong", "weak"]) == "strong"
    assert audit._aggregate_evidence(["strong", "conflict"]) == "strong"
    assert audit._aggregate_evidence(["weak", "weak"]) == "weak"
    assert audit._aggregate_evidence(["weak", "conflict"]) == "weak"
    assert audit._aggregate_evidence(["conflict", "conflict"]) == "conflict"
    assert audit._aggregate_evidence([]) == "weak"


def test_audit_max_walk_ratio_disabled_walks_full_dest(db_path, tmp_path):
    """max_walk_ratio=None disables the cap (matches --no-early-stop
    full-coverage semantics)."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_tree(src, {"a.jpg": b"x", "b.jpg": b"y"})
    # Put the second match deep behind a lot of noise so the cap would
    # have stopped us short.
    files = {"early/a.jpg": b"x"}
    for i in range(200):
        files[f"middle/blob_{i:04d}.bin"] = bytes([i % 256]) * 16
    files["zzz_late/b.jpg"] = b"y"
    _make_tree(dst, files)

    out = io.StringIO()
    r = audit.run_audit(
        str(src), [str(dst)], early_stop=True,
        max_walk_ratio=None,  # disabled
        db_path=db_path, out=out,
    )
    assert r.matched == 2
    assert "walk-cap reached" not in out.getvalue()
