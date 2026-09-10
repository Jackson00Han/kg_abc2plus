#!/bin/zsh
# macOS: double-click this file, or run ./quick_start.command from a terminal.
set -eu
workbench_root="$(cd -- "$(dirname -- "$0")" && pwd)"
if [[ ! -x "$workbench_root/.venv/bin/python" ]]; then
  print -u2 '缺少项目 Python 环境，请先按 quick_start.md 安装依赖。'
  exit 1
fi
exec "$workbench_root/.venv/bin/python" "$workbench_root/scripts/restart_workbench.py"
