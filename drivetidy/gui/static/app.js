// SPDX-License-Identifier: GPL-3.0-or-later

async function fetchJSON(url, opts = {}) {
  const r = await fetch(url, opts);
  if (!r.ok) {
    const txt = await r.text();
    throw new Error(`${r.status} ${r.statusText}: ${txt}`);
  }
  return r.json();
}

async function loadDrives() {
  const list = document.getElementById('drives-list');
  list.innerHTML = '<li class="placeholder">載入中…</li>';
  try {
    const drives = await fetchJSON('/api/drives');
    if (!drives.length) {
      list.innerHTML = '<li class="placeholder">未偵測到外接硬碟</li>';
      return;
    }
    list.innerHTML = '';
    for (const d of drives) {
      const li = document.createElement('li');
      li.classList.add('drive-row');
      const ssd = d.drive_type === 'ssd' ? '⚡' : (d.drive_type === 'hdd' ? '💾' : '?');
      const meta = document.createElement('div');
      meta.className = 'drive-meta-wrap';
      meta.innerHTML = `<span class="drive-name">${ssd} ${escapeHtml(d.name)}</span>` +
                       `<span class="drive-meta">${d.drive_type} · ${escapeHtml(d.path)}</span>`;
      li.appendChild(meta);

      const actions = document.createElement('div');
      actions.className = 'drive-actions';
      const btnSource = document.createElement('button');
      btnSource.type = 'button';
      btnSource.className = 'drive-action-btn';
      btnSource.textContent = '→ 來源';
      btnSource.title = '填入來源（會覆蓋）';
      btnSource.addEventListener('click', () => {
        document.getElementById('audit-source').value = d.path;
      });
      const btnAgainst = document.createElement('button');
      btnAgainst.type = 'button';
      btnAgainst.className = 'drive-action-btn';
      btnAgainst.textContent = '+ 比對';
      btnAgainst.title = '加入比對目的地（保留原有）';
      btnAgainst.addEventListener('click', () => {
        const inp = document.getElementById('audit-against');
        const cur = inp.value.split(',').map(s => s.trim()).filter(Boolean);
        if (!cur.includes(d.path)) cur.push(d.path);
        inp.value = cur.join(',');
      });
      actions.appendChild(btnSource);
      actions.appendChild(btnAgainst);
      li.appendChild(actions);

      list.appendChild(li);
    }
  } catch (e) {
    list.innerHTML = `<li class="placeholder">載入失敗：${escapeHtml(e.message)}</li>`;
  }
}

async function loadLabels() {
  const list = document.getElementById('labels-list');
  list.innerHTML = '<li class="placeholder">載入中…</li>';
  try {
    const labels = await fetchJSON('/api/labels');
    if (!labels.length) {
      list.innerHTML = '<li class="placeholder">尚無已建檔的標籤<br><small>用 <code>drivetidy scan PATH --label NAME</code> 先建檔</small></li>';
      return;
    }
    list.innerHTML = '';
    for (const l of labels) {
      const li = document.createElement('li');
      li.classList.add('label-row');
      li.innerHTML = `<span class="label-name">${escapeHtml(l.label)}</span>` +
                     `<span class="label-meta">${l.file_count} 檔 · ${l.drive_type}</span>`;
      li.style.cursor = 'pointer';
      li.title = '點一下加入比對目的地';
      li.addEventListener('click', () => {
        const inp = document.getElementById('audit-against');
        const cur = inp.value.split(',').map(s => s.trim()).filter(Boolean);
        if (!cur.includes(l.label)) cur.push(l.label);
        inp.value = cur.join(',');
      });
      list.appendChild(li);
    }
  } catch (e) {
    list.innerHTML = `<li class="placeholder">載入失敗：${escapeHtml(e.message)}</li>`;
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);
}

function setStatus(text, kind) {
  const el = document.getElementById('audit-status');
  el.textContent = text;
  el.classList.remove('error', 'success');
  if (kind) el.classList.add(kind);
}

async function runAudit(ev) {
  ev.preventDefault();
  const btn = document.getElementById('audit-run-btn');
  const source = document.getElementById('audit-source').value.trim();
  const againstStr = document.getElementById('audit-against').value.trim();
  const minSize = document.getElementById('audit-min-size').value.trim() || '0';
  const earlyStop = document.getElementById('audit-early-stop').checked;

  if (!source || !againstStr) {
    setStatus('請填來源 + 比對目的地', 'error');
    return;
  }
  const against = againstStr.split(',').map(s => s.trim()).filter(Boolean);

  btn.disabled = true;
  setStatus('稽核中…可能需要幾分鐘');
  try {
    const r = await fetchJSON('/api/audit', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({source, against, min_size: minSize, early_stop: earlyStop}),
    });
    renderResult(r);
    setStatus('完成', 'success');
  } catch (e) {
    setStatus('稽核失敗：' + e.message, 'error');
  } finally {
    btn.disabled = false;
  }
}

function renderResult(r) {
  document.getElementById('audit-result').classList.remove('hidden');
  const pct = r.total_source_files ? (100 * r.matched / r.total_source_files) : 0;
  document.getElementById('audit-pct').textContent = pct.toFixed(1) + '%';
  document.getElementById('audit-matched').textContent = `${r.matched} / ${r.total_source_files}`;
  document.getElementById('audit-missing').textContent = `${r.missing} (${r.missing_bytes_human})`;
  document.getElementById('audit-bar').style.width = pct + '%';

  const perDest = document.getElementById('audit-per-dest');
  perDest.innerHTML = '';
  for (const [ident, count] of Object.entries(r.per_dest_match || {})) {
    const li = document.createElement('li');
    li.innerHTML = `<span>${escapeHtml(ident)}</span><span class="size">${count} 檔</span>`;
    perDest.appendChild(li);
  }

  renderEvidence(r);
  renderMatchLocations(r);
  renderBackupBlock(r);

  const wrap = document.getElementById('audit-missing-list');
  wrap.innerHTML = '';
  if (!r.missing_paths.length) {
    wrap.innerHTML = '<p style="color:#2e7d32;">✓ 所有來源檔案都已備份。</p>';
    return;
  }
  // group by parent dir
  const groups = new Map();
  for (const m of r.missing_paths) {
    const idx = m.path.lastIndexOf('/');
    const folder = idx >= 0 ? m.path.slice(0, idx) : '(根目錄)';
    if (!groups.has(folder)) groups.set(folder, []);
    groups.get(folder).push(m);
  }
  for (const [folder, items] of [...groups].sort()) {
    const totalBytes = items.reduce((a, b) => a + b.size, 0);
    const det = document.createElement('details');
    const sum = document.createElement('summary');
    sum.innerHTML = `📁 ${escapeHtml(folder)} <span class="size">${items.length} 檔 · ${formatBytes(totalBytes)}</span>`;
    det.appendChild(sum);
    const ul = document.createElement('ul');
    for (const it of items) {
      const li = document.createElement('li');
      li.innerHTML = `<span>${escapeHtml(it.path)}</span><span class="size">${escapeHtml(it.size_human)}</span>`;
      ul.appendChild(li);
    }
    det.appendChild(ul);
    wrap.appendChild(det);
  }
}

function renderMatchLocations(r) {
  const wrap = document.getElementById('audit-matched-list');
  wrap.innerHTML = '';
  const entries = Object.entries(r.match_locations || {});
  if (!entries.length) {
    wrap.innerHTML = '<p style="color:#888;">沒有任何 matched 檔可顯示對應位置。</p>';
    return;
  }
  // Group by source folder, same UX pattern as missing list.
  const groups = new Map();
  for (const [src, hits] of entries) {
    const idx = src.lastIndexOf('/');
    const folder = idx >= 0 ? src.slice(0, idx) : '(根目錄)';
    if (!groups.has(folder)) groups.set(folder, []);
    groups.get(folder).push({src, hits});
  }
  for (const [folder, items] of [...groups].sort()) {
    const det = document.createElement('details');
    const sum = document.createElement('summary');
    sum.innerHTML = `📁 ${escapeHtml(folder)} <span class="size">${items.length} 檔</span>`;
    det.appendChild(sum);
    const ul = document.createElement('ul');
    for (const it of items) {
      const li = document.createElement('li');
      const evidenceKind = (r.match_evidence || {})[it.src] || 'weak';
      if (evidenceKind !== 'weak') {
        li.classList.add('ev-' + evidenceKind);
      }
      const srcLine = document.createElement('div');
      srcLine.className = 'match-src';
      srcLine.textContent = it.src;
      if (evidenceKind === 'strong') {
        srcLine.innerHTML += ' <span class="ev-badge ev-strong-badge">EXIF ✓</span>';
      } else if (evidenceKind === 'conflict') {
        srcLine.innerHTML += ' <span class="ev-badge ev-conflict-badge">EXIF ⚠ 衝突</span>';
      }
      li.appendChild(srcLine);
      for (const hit of it.hits) {
        const hitLine = document.createElement('div');
        hitLine.className = 'match-hit';
        hitLine.innerHTML = `↳ <span class="match-ident">[${escapeHtml(hit.ident)}]</span> ${escapeHtml(hit.dest_path)}`;
        li.appendChild(hitLine);
      }
      ul.appendChild(li);
    }
    det.appendChild(ul);
    wrap.appendChild(det);
  }
}

function renderBackupBlock(r) {
  // Show the backup-missing affordance only when there's something to
  // back up AND we have a persisted run_id to feed the API. Persisted
  // pre-v3 runs (loaded via GET /api/audit/{id}) won't have run_id and
  // this stays hidden.
  const block = document.getElementById('backup-missing-block');
  if (!r || !r.run_id || !r.missing_paths || r.missing_paths.length === 0) {
    block.classList.add('hidden');
    return;
  }
  block.classList.remove('hidden');
  document.getElementById('backup-count').textContent = r.missing_paths.length;
  document.getElementById('backup-bytes').textContent = r.missing_bytes_human || '—';

  // Populate dest dropdown from the audit's dests. parallel arrays
  // dest_idents + dest_kinds.
  const select = document.getElementById('backup-dest-ident');
  select.innerHTML = '';
  const idents = r.dest_idents || [];
  for (const ident of idents) {
    const opt = document.createElement('option');
    opt.value = ident;
    opt.textContent = ident;
    select.appendChild(opt);
  }
  // Stash the run id on the element for the click handler.
  document.getElementById('backup-run-btn').dataset.runId = r.run_id;
  document.getElementById('backup-result').classList.add('hidden');
  document.getElementById('backup-status').textContent = '';
}

async function runBackupMissing() {
  const btn = document.getElementById('backup-run-btn');
  const status = document.getElementById('backup-status');
  const runId = parseInt(btn.dataset.runId, 10);
  if (!runId) {
    status.textContent = '錯誤：沒有 audit run id';
    status.classList.add('error');
    return;
  }
  const destIdent = document.getElementById('backup-dest-ident').value;
  const count = document.getElementById('backup-count').textContent;
  const dest = destIdent || '(預設)';
  // Native confirm — keeps GUI scope tight; a custom modal would be
  // nicer but is post-launch polish.
  if (!confirm(
    `將從來源複製 ${count} 個檔案到 ${dest}。\n` +
    `這個動作只寫不刪，不會動到來源。確定？`
  )) {
    return;
  }

  btn.disabled = true;
  status.textContent = '複製中…';
  status.classList.remove('error', 'success');
  try {
    const r = await fetchJSON('/api/backup-missing', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({audit_run_id: runId, apply: true, dest_ident: destIdent}),
    });
    renderBackupResult(r);
    status.textContent = r.failed.length
      ? `部分失敗（${r.succeeded.length} OK / ${r.failed.length} 失敗）`
      : '完成';
    status.classList.add(r.failed.length ? 'error' : 'success');
  } catch (e) {
    status.textContent = '備份失敗：' + e.message;
    status.classList.add('error');
  } finally {
    btn.disabled = false;
  }
}

function renderBackupResult(r) {
  const result = document.getElementById('backup-result');
  result.classList.remove('hidden');
  const summary = document.getElementById('backup-result-summary');
  summary.textContent = `已複製 ${r.succeeded.length} / ${r.planned_count} 個檔到 ${r.dest_root}`;
  const details = document.getElementById('backup-failed-details');
  const list = document.getElementById('backup-failed-list');
  list.innerHTML = '';
  if (r.failed && r.failed.length) {
    details.classList.remove('hidden');
    for (const f of r.failed) {
      const li = document.createElement('li');
      li.textContent = `${f.path} — ${f.error}`;
      list.appendChild(li);
    }
  } else {
    details.classList.add('hidden');
  }
}

function renderEvidence(r) {
  // Evidence block stays hidden if every match is "weak" (legacy path
  // — no scan ran with --exif). Showing zeroes everywhere would be
  // noise for the 90% of users who haven't opted into EXIF yet.
  const block = document.getElementById('audit-evidence');
  const evidence = r.match_evidence || {};
  const counts = { strong: 0, weak: 0, conflict: 0 };
  for (const kind of Object.values(evidence)) {
    if (counts[kind] !== undefined) counts[kind] += 1;
  }
  if (counts.strong === 0 && counts.conflict === 0) {
    block.classList.add('hidden');
    return;
  }
  block.classList.remove('hidden');
  document.getElementById('evidence-strong').textContent = counts.strong;
  document.getElementById('evidence-weak').textContent = counts.weak;
  document.getElementById('evidence-conflict').textContent = counts.conflict;
}

function formatBytes(n) {
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(i === 0 ? 0 : 1) + ' ' + u[i];
}

async function pickFolderInto(inputId, mode, btn) {
  // mode: 'replace' (source) or 'append' (against, comma-merged like labels)
  btn.disabled = true;
  try {
    const r = await fetchJSON('/api/pick-folder', {method: 'POST'});
    if (r.cancelled) return;
    const inp = document.getElementById(inputId);
    if (mode === 'append') {
      const cur = inp.value.split(',').map(s => s.trim()).filter(Boolean);
      if (!cur.includes(r.path)) cur.push(r.path);
      inp.value = cur.join(',');
    } else {
      inp.value = r.path;
    }
  } catch (e) {
    alert('資料夾選擇器無法打開：' + e.message);
  } finally {
    btn.disabled = false;
  }
}

document.addEventListener('DOMContentLoaded', () => {
  loadDrives();
  loadLabels();
  document.getElementById('refresh-drives').addEventListener('click', loadDrives);
  document.getElementById('audit-form').addEventListener('submit', runAudit);
  document.getElementById('backup-run-btn').addEventListener('click', runBackupMissing);
  const pickSrc = document.getElementById('pick-source');
  pickSrc.addEventListener('click', () => pickFolderInto('audit-source', 'replace', pickSrc));
  const pickAg = document.getElementById('pick-against');
  pickAg.addEventListener('click', () => pickFolderInto('audit-against', 'append', pickAg));
});
