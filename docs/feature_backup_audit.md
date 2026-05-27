# 功能提案：Backup Audit（備份完整性稽核）

> 來源：2026-04-25 WT 用 Claude 手動跑記憶卡 → 硬碟、T7 → HDD 的備份檢查流程，發現可以變成 drivetidy 的核心功能之一。

---

## TL;DR

**drivetidy 目前是「去重工具」（找重複），這個提案加上「備份稽核」（找漏掉）— 同一份 metadata 兩種用途。**

新指令：

```
drivetidy audit <source-label> --against <dest-label>[,<dest-label2>...]
```

回答一個問題：**「source 上的檔案，在 dest 們有沒有備份到？沒有的話有哪些？」**

---

## 動機 / 適用場景

WT 今天的真實流程：

1. **格式化記憶卡前**：插卡 → 比對 ~/文件 + Seagate → 確認 100% 備份才敢格式化
2. **盤點備份盤狀態**：T7 內容是不是也在兩顆 HDD 都有？哪些只剩 T7 一份？
3. **拆開備份偵測**：同一個檔案可能拆到 N 個資料夾，傳統 rsync/diff 看不出來，但 (filename, size) 比對能跨資料夾找

實測效益（今天的真實數字）：

| 動作 | 結果 | 耗時 |
|---|---|---|
| 4 張記憶卡逐張驗證 → 確認可格式化 | 全部 99%+ 配對，安心格式化 | 每張 1-3 分鐘 |
| T7 (5,206 檔 / 454 GB) → 兩顆 5TB HDD | 99.9% 已備份，找到 4 個漏網 | 113 秒 |

**沒有這功能前**：人工開 Finder 一個個對，或寫 ad-hoc python script，每次重做。

---

## 核心邏輯（已驗證可行）

### 1. 比對基準：`(filename_lower, size_bytes)`

- **不算 hash**（hash 慢，HDD 上跑 hash 很傷時間）
- 同名同 byte 大小視為「同一檔案」
- 對相機原檔、影片素材、剪輯成品場景準確度 99.9%+
- 跟現有 dedup 用的 size-only compare 同源，差別在這裡只看「有沒有命中」

### 2. 早停搜尋（重點優化）

**關鍵**：HDD 寫滿時整顆 walk 一次要 30-60 分鐘，但「驗證有沒有備份」其實只要找到第一個命中就夠。

演算法：

```
needles = set((basename_lower, size) for f in source)
found = {}
for hdd in destinations:
    if len(found) == len(needles): break  # 早停
    for f in os.walk(hdd):
        if (f.basename, f.size) in needles and not in found:
            found[(f.basename, f.size)] = (hdd, f.path)
            if len(found) == len(needles): break
report(matched=found, missing=needles - found)
```

**今天實測**：T7 5139 個 needle，掃了 167k 個檔案、113 秒就跑完（中途多次 HDD 真在轉動的 IO bound）。比完整 inventory 兩顆 HDD（>2 小時）快一個量級。

### 3. 結果報告

```
T7 → [2025-, 2024-2025]
✓ 找到備份: 5,135 (99.9%)
✗ 沒備份: 4 (30 MB)

按 source 資料夾分組未備份檔：
  📁 新增包含項目的檔案夾/3.作業檔  (3 檔, 32 MB)
  📁 (根目錄)  (1 檔, 2 MB)

備份分佈：
  [2025-] 4,663 個
  [2024-2025] 472 個
```

---

## 實作建議（接到現有 drivetidy 架構）

### 新檔案

```
drivetidy/audit.py            # 新指令邏輯
drivetidy/templates/audit.html # 報告模板（可選）
```

### CLI

```
drivetidy audit <src-label> --against <dest1>,<dest2>...
                           [--min-size 1M]      # 過濾掉太小的（系統檔）
                           [--exclude <regex>]   # 排除 ._/Library/.git 等
                           [--early-stop]        # 預設開啟，可關掉做完整 audit
                           [--out audit.html]    # 輸出報告
```

### 兩種模式

1. **走 SQLite（已掃過的 label）**：直接 SQL JOIN，秒級完成
   - `SELECT s.path FROM files s LEFT JOIN files d ON s.size=d.size AND lower(basename(s.path))=lower(basename(d.path)) WHERE s.scan_id=? AND d.scan_id IN (?,?) AND d.scan_id IS NULL`
2. **走 live walk（沒掃過的目的地）**：Python `os.walk` + needle set，早停
   - 適用「插上一張新卡，當下要驗證」的情境，不必先做 full scan

預設：source 一定要先掃過（在 SQLite），dest 可以是 label（用 SQL）或 path（用 live walk）。

### 與現有 schema 整合

不需要新表。完全沿用：
- `scans` 紀錄掃過的 label
- `files` 提供 (size, path) 索引

可以額外加：

```sql
audit_runs(
  id INTEGER PRIMARY KEY,
  source_scan_id INTEGER,
  dest_labels TEXT,        -- JSON array
  matched_count INTEGER,
  missing_count INTEGER,
  missing_size INTEGER,
  ran_at TEXT
)

audit_missing(
  run_id INTEGER,
  source_path TEXT,
  size INTEGER
)
```

讓 audit 結果可以保存、之後重看、或差異追蹤（這次跟上次相比，哪些檔案新加但沒備份）。

---

## UI/UX 想法（如果要做 GUI）

drivetidy 的 GUI_PLAN 已有規劃。Audit 介面建議：

1. **左邊**：選 source label（例如「記憶卡 EOS_DIGITAL」）
2. **右邊**：勾選 dest labels（例如「2025-」「2024-2025」「~/文件」）
3. **點「Run Audit」**：跑早停搜尋
4. **結果頁**：
   - 大圓餅圖：✓ 已備份 / ✗ 未備份 比例
   - 紅色 box：未備份檔案清單，按資料夾分組
   - 「複製 missing 檔案到 X」按鈕（可選擇目的地）
5. **快速動作**：「我已確認 → 標記 source 為可格式化」（記到 audit_runs）

---

## 優先序建議

接在現有 internal notes 的 Phase 2 之後做：

| Phase | 內容 | 估時 |
|---|---|---|
| **Phase 3.1** | `audit` CLI（純 SQL 模式，dest 必須先 scan 過） | 2-3h |
| Phase 3.2 | live walk 模式（dest 是 path） | 2-3h |
| Phase 3.3 | early-stop 優化 + 進度回報 | 1h |
| Phase 3.4 | HTML 報告 | 2h |
| Phase 3.5 | GUI 整合 | 接 GUI_PLAN |

合計 ~7-9h 可上線 CLI 版。

---

## 跟現有功能的關係

| 功能 | 問題 | 結論 |
|---|---|---|
| `compare` | 兩個碟有什麼差別？ | A 有 B 沒有 / B 有 A 沒有 / 都有 |
| `dedup` | 找重複以便刪除 | 同一個檔案存在 N 份，刪掉 N-1 |
| **`audit`（新）** | source 是不是都備份到 dest 了？ | source 的每個檔案 → dest 是否有命中 |

三者用同一份 `files` 表，**只是 SQL JOIN 的方向跟過濾條件不同**。

---

## 開放議題

1. **「相同」的判斷要不要可配置？**
   - 目前 (filename, size) 夠用
   - 若使用者要更嚴格 → 可加 `--require-hash` flag，回退到 hash 模式

2. **跨平台檔名差異**
   - macOS 大小寫不敏感 vs HDD 是 exFAT/NTFS 大小寫敏感
   - 預設 `lower()` 比對（已在 prototype 驗證有效）

3. **外接碟拔除/重插的 label 永久性**
   - 沿用 drivetidy 現有 label 機制即可

---

## 參考：今天跑出來的 prototype 檔案

- `/tmp/claude/cardcheck/find_t7_in_hdds.py` — 早停搜尋核心邏輯（70 行）
- `/tmp/claude/cardcheck/compare4.py` — 多目的地比對 + 冗餘分析
- `/tmp/claude/cardcheck/source*.txt` — 4 張卡的 inventory
- `/tmp/claude/cardcheck/find_result.log` — T7→HDD 跑完的完整 log

可以直接 copy 到 `drivetidy/scripts/` 當實作參考。
