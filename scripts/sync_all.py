#!/usr/bin/env python3
"""
突破追多与移动保本系统全量同步流水线 (Master Sync Pipeline)
-------------------------------------------------------
1. 扫描 CMC Pro 官方核验候选币池 (< $30M, 4h费率与机会区排序)
2. 重构并编译总控制台首页 (index.html)
3. 编译突破追多与移动保本战法手册 (strategy_second_leg_playbook.html)
"""

import sys
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

def run_step(desc: str, cmd: list) -> bool:
    print(f"[*] {desc}...")
    t0 = time.time()
    res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    dt = time.time() - t0
    if res.returncode == 0:
        print(f"    [✓ 成功] ({dt:.2f}s)")
        if res.stdout:
            for line in res.stdout.strip().splitlines():
                if any(k in line for k in ["✅", "🎉", "🔥 三重共振", "★ 通道④", "★ 7天溢出", "HTML"]):
                    print(f"      {line.strip()}")
        return True
    else:
        print(f"    [✗ 失败] ({dt:.2f}s)")
        err_msg = res.stderr.strip() or res.stdout.strip()
        for line in err_msg.splitlines()[-10:]:
            print(f"      {line}")
        return False

def main():
    print("=======================================================")
    print("🚀 启动“突破追多与移动保本系统”全量同步流水线")
    print("=======================================================\n")

    steps = [
        ("1/4 扫描 CMC Pro 官方核验候选币池 (< $30M, 4h费率与三大机会区)", [sys.executable, "scripts/scan_second_leg_pool.py"]),
        ("2/4 重构并编译总控制台首页 (index.html)", [sys.executable, "scripts/build_index.py"]),
        ("3/4 编译突破追多与移动保本战法手册 (strategy_second_leg_playbook.html)", [sys.executable, "scripts/build_second_leg_playbook.py"]),
        ("4/4 执行交易风控与四重预警评测 (新共振/突破/止损)", [sys.executable, "scripts/trade_alert_daemon.py", "--check-once"]),
    ]

    all_ok = True
    for desc, cmd in steps:
        ok = run_step(desc, cmd)
        if not ok:
            all_ok = False

    print("\n=======================================================")
    if all_ok:
        print("🎉 全部同步重构完毕！纯量化小市值底池与机会区已最新！")
    else:
        print("⚠️ 部分步骤存在警告或错误，请检查日志。")
    print("=======================================================")
    sys.exit(0 if all_ok else 1)

if __name__ == '__main__':
    main()
