#!/usr/bin/env python3
"""
突破追多与移动保本系统全量同步流水线 (Master Sync Pipeline)
-------------------------------------------------------
1. 扫描 CMC Pro 官方核验候选币池 (< $30M, 4h费率与机会区排序)
2. 更新账户数据分析引擎与量化风控指标 (SQLite)
3. 执行交易风控与四重预警评测 (新共振/突破/止损/移动锁利)
"""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from config import DEFAULT_DB_PATH, resolve_database_path  # noqa: E402

STDOUT_HIGHLIGHTS = (
    "✅",
    "🎉",
    "双核黄金共振",
    "7天溢出",
    "总资金净值",
    "账户量化分析",
)


def run_step(
    desc: str,
    cmd: list[str],
    env: dict[str, str] | None = None,
    timeout: int = 600,
) -> bool:
    print(f"[*] {desc}...")
    t0 = time.time()
    try:
        res = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        dt = time.time() - t0
        print(f"    [✗ 超时] ({dt:.2f}s, timeout={timeout}s)")
        err_out = exc.stderr or exc.stdout or ""
        if isinstance(err_out, bytes):
            err_out = err_out.decode("utf-8", errors="replace")
        for line in err_out.strip().splitlines()[-10:]:
            print(f"      {line}")
        return False

    dt = time.time() - t0
    if res.returncode == 0:
        print(f"    [✓ 成功] ({dt:.2f}s)")
        if res.stdout:
            for line in res.stdout.strip().splitlines():
                if any(k in line for k in STDOUT_HIGHLIGHTS):
                    print(f"      {line.strip()}")
        return True

    print(f"    [✗ 失败] ({dt:.2f}s)")
    err_msg = res.stderr.strip() or res.stdout.strip()
    for line in err_msg.splitlines()[-10:]:
        print(f"      {line}")
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="刷新候选池、账户报表与风控预警")
    parser.add_argument("--refresh-only", action="store_true", help="跳过风控评测和预警推送")
    parser.add_argument(
        "--database",
        type=Path,
        default=None,
        help="SQLite 数据库路径（默认读取 COIN_DATABASE 或 runs/account.sqlite3）",
    )
    args = parser.parse_args()

    db_path = resolve_database_path(args.database) or DEFAULT_DB_PATH
    resolved_db = str(db_path.resolve())
    child_env = os.environ.copy()
    child_env["COIN_DATABASE"] = resolved_db

    print("=======================================================")
    print("🚀 启动“突破追多与移动保本系统”同步流水线")
    print(f"🗄️  统一 SQLite 存储: {resolved_db}")
    print("=======================================================\n")

    steps: list[tuple[str, list[str]]] = [
        (
            "扫描 CMC Pro 官方核验候选币池 (< $30M, 4h费率与机会区)",
            [sys.executable, "scripts/scan_second_leg_pool.py", "--database", resolved_db],
        ),
        (
            "更新账户数据分析与量化风控指标",
            [sys.executable, "scripts/account_analytics_engine.py", "--database", resolved_db],
        ),
    ]

    if not args.refresh_only:
        steps.append(
            (
                "执行交易风控与四重预警评测",
                # trade_alert_daemon.py reads COIN_DATABASE from child_env (no --database CLI flag).
                [sys.executable, "scripts/trade_alert_daemon.py", "--check-once"],
            )
        )

    all_ok = True
    for index, (desc, cmd) in enumerate(steps, start=1):
        if not run_step(f"{index}/{len(steps)} {desc}", cmd, env=child_env):
            all_ok = False

    print("\n=======================================================")
    if all_ok:
        print("🎉 全部同步完成！纯量化小市值底池、账户指标与机会区已最新！")
    else:
        print("⚠️ 部分步骤存在警告或错误，请检查日志。")
    print("=======================================================")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
