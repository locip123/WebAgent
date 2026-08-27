#!/bin/bash
# ============================================================
# WebRetriever Challenge 固定评测入口
# 用法: bash scripts/run.sh <task_file> <output_dir> <cdp_url1> [cdp_url2] ...
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ $# -lt 3 ]; then
    echo "参数不足：bash scripts/run.sh <task_file> <output_dir> <cdp_url1> [cdp_url2] ..." >&2
    exit 2
fi

TASK_FILE="$1"
if [ ! -f "$TASK_FILE" ]; then
    echo "任务文件不存在: $TASK_FILE" >&2
    exit 2
fi

cd "$PROJECT_DIR"
export PYTHONPATH="$PROJECT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
# 提交运行不需要 Browser Use 的全局日志配置；避免向工作区外写入日志。
export BROWSER_USE_SETUP_LOGGING=false

exec python3 -m browser_use.webretriever.submission "$@"
