#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Server酱 (ServerChan) 通知客户端与预警消息格式化工具。
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from config import clean_symbol  # noqa: E402
from http_client import HttpError, request_json  # noqa: E402

logger = logging.getLogger("trade_alert_daemon")


def format_alert_price(val: float) -> str:
    """Format prices dynamically (2-8 decimals) so sub-$0.001 micro-caps never truncate to $0.0000."""
    num = float(val)
    abs_v = abs(num)
    if abs_v >= 100:
        dec = 2
    elif abs_v >= 0.01:
        dec = 4
    elif abs_v >= 0.0001:
        dec = 6
    else:
        dec = 8
    return f"{num:.{dec}f}"


def build_tv_chart_link(layout_id: str, symbol: str, interval: str = "15") -> str:
    """Build a TradingView chart link for a Binance USD-M perpetual symbol."""
    sym_clean = clean_symbol(symbol)
    return f"https://cn.tradingview.com/chart/{layout_id}/?symbol=BINANCE:{sym_clean}.P&interval={interval}"


class ServerChanNotifier:
    """Server酱推送服务客户端，支持真实推送与 Dry-Run / 日志模式。"""

    def __init__(self, sendkey: str = "", dry_run: bool = False):
        self.sendkey = (sendkey or "").strip()
        self.dry_run = dry_run or not bool(self.sendkey)
        if self.dry_run:
            if not self.sendkey:
                logger.info("ℹ️ 未配置 SERVERCHAN_SENDKEY，预警守护进程将运行在 [Dry-Run / 日志模式]。")
            else:
                logger.info("ℹ️ 已显式指定 --dry-run，预警守护进程将运行在 [Dry-Run / 日志模式]。")
        else:
            masked = self.sendkey[:4] + "***" + self.sendkey[-3:] if len(self.sendkey) > 7 else "***"
            logger.info(f"✅ Server酱通知通道已就绪 (SendKey: {masked})")

    def is_configured(self) -> bool:
        """Return True when a non-empty Server酱 SendKey is configured."""
        return bool(self.sendkey)

    def send_alert(self, title: str, desp: str) -> bool:
        """发送通知消息到微信 Server酱。"""
        prefix = "[DRY-RUN]" if self.dry_run else "[LIVE]"
        logger.info(f"🔔 {prefix} 发送通知: {title}")

        if self.dry_run:
            print("\n" + "=" * 60)
            print(f"📣 [Server酱模拟推送] {title}")
            print("-" * 60)
            print(desp.strip())
            print("=" * 60 + "\n")
            return True

        url = f"https://sctapi.ftqq.com/{self.sendkey}.send"
        payload = json.dumps({"title": title, "desp": desp})
        try:
            resp_data = request_json(
                url,
                method="POST",
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "User-Agent": "TradeAlertDaemon/1.0",
                },
                body=payload,
                timeout=25.0,
                max_retries=0,
            )
        except HttpError as e:
            logger.warning(f"    ⚠️ Server酱网络请求异常: {e}")
            return False

        if isinstance(resp_data, dict) and resp_data.get("code") == 0:
            logger.info(f"    ✓ Server酱推送成功: {title}")
            return True
        logger.error(f"    ✗ Server酱接口返回错误: {resp_data}")
        return False
