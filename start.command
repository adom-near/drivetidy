#!/bin/bash
# DriveTidy launcher — 雙擊我啟動 GUI
# SPDX-License-Identifier: GPL-3.0-or-later
set -e

cd "$(dirname "$0")"

# macOS 預設的 python3 (Sequoia 之前) 可能是 3.9，DriveTidy 需要 3.11+。
# 沒先擋會 silent fail：venv 建得起來、pip install 跑得過甚至，但 runtime
# 才炸 import error，使用者會困惑。
# Version threshold MUST match pyproject.toml `requires-python`.
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
  PYVER=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:3])))' 2>/dev/null || echo "未知")
  {
    echo ""
    echo "❌ DriveTidy 需要 Python 3.11 或以上。"
    echo "   你目前的 python3 版本：$PYVER"
    echo ""
    echo "   解法（擇一）："
    echo "     1. 從 https://www.python.org/downloads/macos/ 下載安裝最新版 Python"
    echo "     2. 或用 Homebrew：brew install python@3.12"
    echo "     3. 安裝後重新雙擊本檔"
    echo ""
  } >&2
  read -n 1 -s -r -p "按任意鍵關閉…"
  exit 1
fi

if [ ! -d ".venv" ]; then
  echo "→ 首次啟動，建立 Python 環境（約需 30 秒）..."
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -e '.[gui]'
fi

# shellcheck disable=SC1091
source .venv/bin/activate

cat <<MSG

╭──────────────────────────────────────────────╮
│  DriveTidy GUI 已啟動                        │
│                                              │
│  http://127.0.0.1:8765                       │
│  瀏覽器會自動開啟。                          │
│                                              │
│  關閉本視窗（或按 Ctrl+C）即可停止 server。  │
╰──────────────────────────────────────────────╯

MSG

exec drivetidy gui
