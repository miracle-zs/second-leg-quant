#!/usr/bin/env python3
"""
突破追多与移动保本系统全量同步流水线 (Master Sync Pipeline)
-------------------------------------------------------
1. 扫描 CMC Pro 官方核验候选币池 (< $30M, 4h费率与机会区排序)
2. 更新账户数据分析引擎与量化风控指标 (runs/account_analytics.json 或 SQLite)
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

from config import resolve_database_path  # noqa: E402

STDOUT_HIGHLIGHTS = (
    "✅",
    "🎉",
    "双核黄金共振",
    "7天溢出",
    "HTML",
    "总资金净值",
    "账户量化分析",
    "Compiled",
)


def run_step(desc: str, cmd: list[str], env: dict[str, str] | None = None) -> bool:
    print(f"[*] {desc}...")
    t0 = time.time()
    res = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True)
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
    parser = argparse.ArgumentParser(description="刷新候选池、账户报表与看板")
    parser.add_argument("--refresh-only", action="store_true", help="跳过风控评测和预警推送")
    parser.add_argument(
        "--account-database",
        "--database",
        dest="account_database",
        type=Path,
        help="将候选池与账户报表统一写入 SQLite 并跳过静态页编译",
    )
    args = parser.parse_args()

    db_path = resolve_database_path(args.account_database)
    child_env = os.environ.copy()
    if db_path is not None:
        child_env["COIN_DATABASE"] = str(db_path.resolve())

    print("=======================================================")
    print("🚀 启动“突破追多与移动保本系统”同步流水线")
    if db_path is not None:
        print(f"🗄️  统一 SQLite 存储: {db_path}")
    print("=======================================================\n")

    steps: list[tuple[str, list[str], bool]] = [
        (
            "扫描 CMC Pro 官方核验候选币池 (< $30M, 4h费率与机会区)",
            [sys.executable, "scripts/scan_second_leg_pool.py"],
            True,  # critical: abort downstream steps on failure
        ),
    ]

    account_command = [sys.executable, "scripts/account_analytics_engine.py"]
    if db_path is not None:
        account_command.extend(["--database", str(db_path)])
    steps.append(("更新账户数据分析与量化风控指标", account_command, False))

    if not args.refresh_only:
        steps.append(
            (
                "执行交易风控与四重预警评测",
                [sys.executable, "scripts/trade_alert_daemon.py", "--check-once"],
                False,
            )
        )

    all_ok = True
    for index, (desc, cmd, abort_on_fail) in enumerate(steps, start=1):
        ok = run_step(f"{index}/{len(steps)} {desc}", cmd, env=child_env)
        if not ok:
            all_ok = False
            if abort_on_fail:
                print("\n⛔ 关键上游步骤失败，已中止后续流程以避免基于陈旧数据编译或预警。")
                sys.exit(1)

    print("\n=======================================================")
    if all_ok:
        print("🎉 全部同步重构完毕！纯量化小市值底池与机会区已最新！")
    else:
        print("⚠️ 部分步骤存在警告或错误，请检查日志。")
    print("=======================================================")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
