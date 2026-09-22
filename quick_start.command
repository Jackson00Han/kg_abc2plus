#!/bin/sh
# Linux / macOS / WSL: run ./quick_start.command; macOS also supports double-click.
set -eu
workbench_root="$(cd -- "$(dirname -- "$0")" && pwd)"
if [ ! -x "$workbench_root/.venv/bin/python" ]; then
  printf '%s\n' '缺少项目 Python 环境，请先按 quick_start.md 安装依赖。' >&2
  exit 1
fi
exec "$workbench_root/.venv/bin/python" "$workbench_root/scripts/restart_workbench.py"
