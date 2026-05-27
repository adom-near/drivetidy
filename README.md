# DriveTidy

> **Verify your photos and videos are actually backed up — in 113 seconds, not 2 hours.**

DriveTidy is a backup integrity auditor for photographers, videographers, and data hoarders. It answers one question fast: *"For every file on this source drive, is there a copy on my backups?"*

[繁體中文](#繁體中文-readme) ↓

---

## Why DriveTidy

You bought 4 external drives so you'd never lose your photos. But you've never actually verified the backups are complete. The dread before formatting an SD card is real.

DriveTidy walks the source, compares against your backup drives, and tells you exactly what's missing. **No cloud, no subscription, no telemetry.** Your file paths never leave your machine.

- **Fast**: 113 seconds to verify a 5TB drive (early-stop search; full walk would take 2 hours)
- **Probabilistic match with evidence**: filename + size + mtime, with optional EXIF disambiguation for JPEGs
- **HTML report**: missing files grouped by folder, sorted by size, so you fix the biggest gaps first
- **One-click backup of what's missing** (`drivetidy backup-missing`): rsync the gap, additive only — never deletes
- **Both CLI and GUI**: `pip install drivetidy` for the terminal, or download the `.app`/`.exe` for the visual version

## Installation

### CLI (Python users)

```bash
pip install drivetidy
```

Requires Python 3.11+. You also need `fd` or `rclone` installed for fast directory walking:

```bash
brew install fd        # macOS
apt install fd-find    # Debian/Ubuntu
```

### GUI (everyone else)

Download the latest release for your platform from the [Releases page](https://github.com/adom-near/drivetidy/releases):

- **macOS**: `drivetidy-macos-arm64.zip` — unzip, then double-click `start.command`. *First launch: right-click → Open* to bypass Gatekeeper (the app is unsigned).
- **Windows**: `drivetidy-windows-x64.zip` — unzip, then double-click `drivetidy.exe`. SmartScreen may warn; click "More info" → "Run anyway".

## Quick start

```bash
# 1. Scan each drive once
drivetidy scan /Volumes/SD-Card --label sd-source
drivetidy scan /Volumes/Backup-1 --label backup-1
drivetidy scan /Volumes/Backup-2 --label backup-2

# 2. Audit — is everything on sd-source covered by the backups?
#    Pass --out to also write an HTML report grouping missing files by folder.
drivetidy audit sd-source --against backup-1,backup-2 --out missing.html

# 3. (Optional) Copy missing files to a backup — additive, never deletes
drivetidy backup-missing <audit_run_id> --dest-ident backup-1 --apply
```

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).

This means:
- You can use, study, modify, and redistribute DriveTidy freely.
- If you distribute a modified version, you must also release your changes under GPL-3.
- Commercial use is allowed but the source must remain open.

## Contributing

Bug reports, feature requests, and pull requests welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

---

## 繁體中文 README

> **113 秒驗完 5TB 硬碟，幫你確認照片影片真的有備份到。**

DriveTidy 是給攝影師、剪輯師、資料儲存玩家的備份完整性檢查工具。它只回答一個問題：*「來源硬碟上的每個檔案，在我的備份硬碟裡有沒有？」*

### 為什麼用 DriveTidy

你買了 4 顆外接硬碟備份照片，但你從來沒真的驗證過備份有沒有漏。每次要格式化 SD 卡前那種「萬一漏了」的焦慮是真的。

DriveTidy 掃描來源、跟備份硬碟比對，明確告訴你哪些檔案沒備份到。**不上雲、不訂閱、不傳送任何資料**，你的檔案路徑永遠留在自己電腦上。

- **快**：113 秒驗完 5TB（早停搜尋；完整 walk 要 2 小時）
- **多重比對 + 證據**：檔名 + 大小 + 修改時間，JPEG 可選用 EXIF 加強判定
- **HTML 報告**：缺檔案依資料夾分組、依大小排序，先補最大的洞
- **一鍵補檔**（`drivetidy backup-missing`）：rsync 缺的檔，只新增不刪除
- **CLI 跟 GUI 兩種版本**：開發者 `pip install drivetidy`；一般人下載 `.app` / `.exe`

### 安裝

#### CLI（指令版）

```bash
pip install drivetidy
```

需要 Python 3.11+，並安裝 `fd` 或 `rclone`：

```bash
brew install fd        # macOS
```

#### GUI（視窗版）

到 [Releases 頁面](https://github.com/adom-near/drivetidy/releases) 下載：

- **macOS**：`drivetidy-macos-arm64.zip` — 解壓後雙擊 `start.command`。*第一次打開要「右鍵 → 打開」* 才能繞過 Gatekeeper（App 沒簽章）
- **Windows**：`drivetidy-windows-x64.zip` — 解壓後雙擊 `drivetidy.exe`。SmartScreen 可能會跳警告，點「其他資訊」→「仍要執行」

### 快速上手

```bash
# 1. 各硬碟先各 scan 一次
drivetidy scan /Volumes/SD卡 --label sd-source
drivetidy scan /Volumes/備份-1 --label backup-1
drivetidy scan /Volumes/備份-2 --label backup-2

# 2. 比對 — sd-source 的東西兩顆備份是否全包到？
#    加 --out 可同時產出缺檔 HTML 報告（按來源資料夾分組）
drivetidy audit sd-source --against backup-1,backup-2 --out 缺檔報告.html

# 3. （選用）把缺的檔複製到某顆備份（只新增不刪除）
drivetidy backup-missing <audit_run_id> --dest-ident backup-1 --apply
```

### 授權

GPL-3.0-or-later，詳見 [LICENSE](LICENSE)。

意思是：
- 你可以自由使用、研究、修改、再散佈 DriveTidy
- 但散佈修改版時，也要用 GPL-3 開源
- 可以商用，但程式碼必須保持開源

### 貢獻

歡迎 issue、PR、功能建議。詳見 [CONTRIBUTING.md](CONTRIBUTING.md)。
