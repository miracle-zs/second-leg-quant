#!/usr/bin/env bash
# ==============================================================================
# “突破追多与移动保本系统” · 每小时自动同步任务执行脚本
# 执行周期: 每小时整点刚过 (第 02 分)
# 防重机制: 基于 Python fcntl.flock 的共享文件锁 + 120 秒冷却窗口
# ==============================================================================

set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
LOG_FILE="$PROJECT_DIR/runs/cron_sync.log"
LOCK_FILE="$PROJECT_DIR/runs/.sync_pipeline.lock"
LAST_OK_FILE="$PROJECT_DIR/runs/.last_sync_ok_ts"
COOLDOWN_SEC=120

cd "$PROJECT_DIR"
mkdir -p "$PROJECT_DIR/runs"

# 使用固定 FD 9 与 Python fcntl.flock（兼容 macOS Bash 3.2 与 Linux），防止并发竞态
exec 9>"$LOCK_FILE"
if ! "$PYTHON_BIN" -c '
import fcntl, sys
try:
    fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(1)
'; then
    echo "⏭️ [$(date '+%Y-%m-%d %H:%M:%S')] 检测到已有同步任务正在运行，跳过本次重复触发。" >> "$LOG_FILE"
    exit 0
fi

NOW_TS=$(date +%s)
if [[ -f "$LAST_OK_FILE" ]]; then
    LAST_TS=$(cat "$LAST_OK_FILE")
    if [[ "$LAST_TS" =~ ^[0-9]+$ ]] && (( NOW_TS - LAST_TS < COOLDOWN_SEC )); then
        echo "⏭️ [$(date '+%Y-%m-%d %H:%M:%S')] 距上次成功同步不足 ${COOLDOWN_SEC}s，跳过本次重复触发。" >> "$LOG_FILE"
        exit 0
    fi
fi

{
    echo "======================================================="
    echo "⏰ [$(date '+%Y-%m-%d %H:%M:%S')] 启动每小时自动化同步任务 (PID: $$)"
    echo "======================================================="
} >> "$LOG_FILE"

set +e
"$PYTHON_BIN" scripts/sync_all.py --refresh-only >> "$LOG_FILE" 2>&1
EXIT_CODE=$?
set -e

if [[ $EXIT_CODE -eq 0 ]]; then
    date +%s > "$LAST_OK_FILE"
    echo "✅ [$(date '+%Y-%m-%d %H:%M:%S')] 本轮每小时同步成功完成！" >> "$LOG_FILE"
else
    echo "❌ [$(date '+%Y-%m-%d %H:%M:%S')] 同步异常退出，Exit Code: $EXIT_CODE" >> "$LOG_FILE"
fi

echo "" >> "$LOG_FILE"
exit $EXIT_CODE
