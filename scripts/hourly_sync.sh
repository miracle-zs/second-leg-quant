#!/usr/bin/env bash
# ==============================================================================
# “突破追多与移动保本系统” · 每小时自动同步任务执行脚本
# 执行周期: 每小时整点刚过 (第 02 分)
# 功能: 重新拉取 CMC Pro 官方市值、刷新 1h EMA5/10 均线与通道形态，编译双端仪表盘
# 防重机制: 原子目录锁（防并发竞态）+ 120 秒冷却窗口（防 cron/launchd 双重触发）
# ==============================================================================

set -u

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)" || exit 1
PYTHON_BIN="${PYTHON_BIN:-python3}"
LOG_FILE="$PROJECT_DIR/runs/cron_sync.log"
LOCK_DIR="$PROJECT_DIR/runs/.hourly_sync.lock"
LAST_OK_FILE="$PROJECT_DIR/runs/.last_sync_ok_ts"
COOLDOWN_SEC=120

cd "$PROJECT_DIR" || exit 1
mkdir -p "$PROJECT_DIR/runs"

# 1. 原子目录锁：防止多个调度器（如 cron 与 launchd）同时触发造成并发竞态
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    LOCK_PID=""
    if [ -f "$LOCK_DIR/pid" ]; then
        LOCK_PID=$(cat "$LOCK_DIR/pid" 2>/dev/null || true)
    fi
    if [ -n "$LOCK_PID" ] && kill -0 "$LOCK_PID" 2>/dev/null; then
        echo "⏭️ [$(date '+%Y-%m-%d %H:%M:%S')] 检测到已有同步任务正在运行 (PID: $LOCK_PID)，跳过本次重复触发。" >> "$LOG_FILE"
        exit 0
    fi
    # 清理异常退出残留的陈旧锁目录并重新加锁
    rm -rf "$LOCK_DIR"
    if ! mkdir "$LOCK_DIR" 2>/dev/null; then
        exit 0
    fi
fi

echo "$$" > "$LOCK_DIR/pid"
trap 'rm -rf "$LOCK_DIR"' EXIT INT TERM

# 2. 冷却窗口保护：若 120 秒内刚成功完成过同步，直接跳过以节省 CMC Pro API 额度
NOW_TS=$(date +%s)
if [ -f "$LAST_OK_FILE" ]; then
    LAST_TS=$(cat "$LAST_OK_FILE" 2>/dev/null || echo 0)
    if [ $((NOW_TS - LAST_TS)) -lt "$COOLDOWN_SEC" ]; then
        echo "⏭️ [$(date '+%Y-%m-%d %H:%M:%S')] 距上次成功同步不足 ${COOLDOWN_SEC}s，跳过本次重复触发。" >> "$LOG_FILE"
        exit 0
    fi
fi

echo "=======================================================" >> "$LOG_FILE"
echo "⏰ [$(date '+%Y-%m-%d %H:%M:%S')] 启动每小时自动化同步任务 (PID: $$)" >> "$LOG_FILE"
echo "=======================================================" >> "$LOG_FILE"

"$PYTHON_BIN" scripts/sync_all.py --refresh-only >> "$LOG_FILE" 2>&1
EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    date +%s > "$LAST_OK_FILE"
    echo "✅ [$(date '+%Y-%m-%d %H:%M:%S')] 本轮每小时同步成功完成！" >> "$LOG_FILE"
else
    echo "❌ [$(date '+%Y-%m-%d %H:%M:%S')] 同步异常退出，Exit Code: $EXIT_CODE" >> "$LOG_FILE"
fi

echo "" >> "$LOG_FILE"
exit $EXIT_CODE
