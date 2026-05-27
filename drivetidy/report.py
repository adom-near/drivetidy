# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 wt and DriveTidy contributors

"""HTML report generation.

Self-contained, no external assets. Uses str.format_map for placeholder
substitution (no jinja dep). Per internal notes, the report style sticks to
the orange palette established in WT's earlier reports.
"""

from __future__ import annotations

import html
import json
import os
import time
from collections import defaultdict
from pathlib import Path

from .audit import AuditResult
from .utils import human_size


_TEMPLATE_DIR = Path(__file__).parent / "templates"


def _load_template(name: str) -> str:
    return (_TEMPLATE_DIR / name).read_text(encoding="utf-8")


def _esc(s) -> str:
    return html.escape(str(s), quote=True)


def render_audit(result: AuditResult) -> str:
    """Return a complete HTML document for an AuditResult."""
    pct = (
        100.0 * result.matched / result.total_source_files
        if result.total_source_files
        else 0.0
    )

    # Group missing by source folder for easy triage.
    folders: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for path, size in result.missing_paths:
        folders[os.path.dirname(path) or "(root)"].append((path, size))
    folder_rows = []
    for folder in sorted(folders):
        files = folders[folder]
        total = sum(s for _, s in files)
        # Files inside folder.
        file_lis = "\n".join(
            f'        <li><span class="path">{_esc(p)}</span> '
            f'<span class="size">{_esc(human_size(s))}</span></li>'
            for p, s in files
        )
        folder_rows.append(
            f'  <details>\n'
            f'    <summary>📁 <strong>{_esc(folder)}</strong> '
            f'<span class="count">{len(files)} files</span> '
            f'<span class="size">{_esc(human_size(total))}</span></summary>\n'
            f'    <ul class="file-list">\n{file_lis}\n    </ul>\n'
            f'  </details>'
        )
    folders_html = "\n".join(folder_rows) if folder_rows else (
        '<div class="all-good">✓ 所有來源檔案都已備份。</div>'
    )

    # Per-dest distribution.
    dest_rows = []
    for ident, kind in zip(result.dest_idents, result.dest_kinds):
        n = result.per_dest_match.get(ident, 0)
        kind_label_zh = "現場掃描" if kind == "path" else "已建檔"
        dest_rows.append(
            f'  <tr><td class="dest-name">{_esc(ident)}</td>'
            f'<td><span class="kind-{kind}">{_esc(kind_label_zh)}</span></td>'
            f'<td class="num">{n}</td></tr>'
        )
    per_dest_html = "\n".join(dest_rows)

    # Source annotation (live vs scanned).
    src_kind_badge = "path" if result.source_kind == "path" else "label"
    src_kind_label = "現場掃描" if result.source_kind == "path" else "已建檔"
    dest_summary = "、".join(
        f"{ident}（{'現場' if k == 'path' else '已建檔'}）"
        for ident, k in zip(result.dest_idents, result.dest_kinds)
    )

    # Match-method description for the report banner. Tells the user what
    # "matched" actually proved (and what it did NOT) — same-name + same-size
    # is a probabilistic match, not a byte-identical one.
    if result.mtime_match:
        match_method_zh = (
            f"檔名 + 大小 + 修改時間（容忍 ±{result.mtime_window_seconds} 秒）"
        )
    else:
        match_method_zh = "檔名 + 大小（不比對修改時間）"

    template = _load_template("audit.html")
    return template.format_map({
        "title": _esc(f"備份稽核：{result.source_ident}"),
        "source_ident": _esc(result.source_ident),
        "source_kind_badge": _esc(src_kind_badge),
        "source_kind_label": _esc(src_kind_label),
        "dest_summary": _esc(dest_summary),
        "generated_at": _esc(time.strftime("%Y-%m-%d %H:%M:%S")),
        "total_files": result.total_source_files,
        "total_bytes_human": _esc(human_size(result.total_source_bytes)),
        "matched": result.matched,
        "missing": result.missing,
        "missing_bytes_human": _esc(human_size(result.missing_bytes)),
        "pct": f"{pct:.1f}",
        "pct_int": int(pct),
        "folders_html": folders_html,
        "per_dest_html": per_dest_html,
        "match_method_zh": _esc(match_method_zh),
        # Embed full result as JSON for future tooling.
        "result_json": _esc(json.dumps({
            "source_ident": result.source_ident,
            "source_kind": result.source_kind,
            "dest_idents": result.dest_idents,
            "dest_kinds": result.dest_kinds,
            "total_source_files": result.total_source_files,
            "total_source_bytes": result.total_source_bytes,
            "matched": result.matched,
            "missing_paths": result.missing_paths,
            "per_dest_match": result.per_dest_match,
        }, ensure_ascii=False)),
    })


def write_report(result: AuditResult, out_path: str | os.PathLike) -> str:
    """Render and write the report. Returns the absolute path written to."""
    out = Path(out_path).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_audit(result), encoding="utf-8")
    return str(out)
