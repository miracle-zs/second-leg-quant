#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
币安合约实时持仓同步客户端 (Binance USDⓈ-M Futures Position Client)
==============================================================================
功能:
1. 复用 scripts/binance_client.py 的统一 BinanceFuturesClient（10 分钟时间偏移校准、
   HMAC-SHA256 签名鉴权、-1021 自动重校准重试、多空双向持仓查询）。
2. 与本地风控持仓文件 (runs/trade_positions.json) 执行双向自动同步:
   - 币安出现新持仓 -> 自动录入实际开仓均价 entryPrice、持仓数量 amount 与 mark_price，
     并按严格币种匹配 + 15m 趋势线有效性过滤关联 TradingView 专属布局 (ig0LuDL8) 中的趋势线。
   - 币安已平仓 (positionAmt == 0) -> 自动归档至 history 历史记录。
3. API Key 缺省时优雅降级为模拟/离线模式，保障系统高可用。
==============================================================================
"""

import logging
import os
import sys
import time
import urllib.error
from pathlib import Path
from typing import Any, Callable, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
RUNS_DIR = PROJECT_ROOT / "runs"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from binance_client import (  # noqa: E402
    BINANCE_FUTURES_BASE_URL,
    POSITION_RISK_ENDPOINT,
    SERVER_TIME_ENDPOINT,
    BinanceFuturesClient,
)
from config import clean_symbol  # noqa: E402

try:
    import tv_trendline_engine as tve
except ImportError:
    try:
        from scripts import tv_trendline_engine as tve
    except ImportError:
        tve = None

logger = logging.getLogger("binance_client")


def _extract_line_interval(line: dict) -> str:
    iv = line.get("interval")
    if not iv and isinstance(line.get("state"), dict):
        iv = line["state"].get("interval")
    if not iv and isinstance(line.get("points"), list):
        for pt in line["points"]:
            if isinstance(pt, dict) and pt.get("interval"):
                iv = pt["interval"]
                break
    return str(iv).strip().upper() if iv is not None else ""


def _extract_line_recency(line: dict) -> float:
    s_time = line.get("server_update_time")
    if s_time is not None:
        try:
            val = float(s_time)
            return val / 1000.0 if val > 1e11 else val
        except (TypeError, ValueError):
            pass
    t2 = line.get("t2")
    if t2 is None and isinstance(line.get("points"), list) and len(line["points"]) >= 2:
        t2 = line["points"][1].get("time_t") or line["points"][1].get("time")
    try:
        return float(t2) if t2 is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _is_descending_line(line: dict) -> bool:
    p1, p2 = line.get("p1"), line.get("p2")
    if (p1 is None or p2 is None) and isinstance(line.get("points"), list) and len(line["points"]) >= 2:
        p1 = line["points"][0].get("price")
        p2 = line["points"][1].get("price")
    try:
        return p1 is not None and p2 is not None and float(p1) >= float(p2)
    except (TypeError, ValueError):
        return False


def select_best_position_trendline(
    lines: Any,
    current_time: Optional[float] = None,
) -> Optional[dict]:
    """
    从同一币种的多条 TradingView 画线中筛选最适合作为持仓止损线的 15m 趋势线:
    1. 严格排除非 15m 周期画线（如 60/240/D/1W 等）。
    2. 优先选择 72 小时内的新鲜 15m 下行趋势线，按最近更新时间降序选取。
    3. 若均无新鲜时间戳（如离线/历史 mock 夹具），退化为从 15m（或未标周期）画线中选最新一条。
    """
    if not lines:
        return None
    candidates = lines if isinstance(lines, list) else [lines]
    candidates = [l for l in candidates if isinstance(l, dict)]
    if not candidates:
        return None

    # 1. 排除明确标注为非 15m 的画线
    iv_ok = []
    for line in candidates:
        iv = _extract_line_interval(line)
        if not iv or iv in ("15", "15M"):
            iv_ok.append(line)
    if not iv_ok:
        return None

    # 2. 优先筛选 72 小时内的近期有效线
    now_ts = current_time if current_time is not None else time.time()
    max_age = 72 * 3600
    recent = [
        line for line in iv_ok
        if _extract_line_recency(line) > 0 and (now_ts - _extract_line_recency(line)) <= max_age
    ]
    pool = recent if recent else iv_ok

    # 3. 优先下行趋势线（买点②阻力/支撑转化线），其次按更新时间倒序
    pool.sort(key=lambda l: (1 if _is_descending_line(l) else 0, _extract_line_recency(l)), reverse=True)
    return pool[0]


def match_symbol_drawings(symbol: str, drawings_map: dict[str, Any]) -> Any:
    """严格按标准化币种名称精确匹配画线，杜绝短币名子串串线（如 HUSDT 误匹配 ETHUSDT）。"""
    target = clean_symbol(symbol)
    if not target or not drawings_map:
        return None
    if target in drawings_map:
        return drawings_map[target]
    for k, lines in drawings_map.items():
        if clean_symbol(k) == target:
            return lines
    return None


def sync_binance_positions(
    client: BinanceFuturesClient,
    pos_store: Any,
    layout_id: str = "ig0LuDL8",
    *,
    load_drawings: Optional[Callable[[str], dict[str, list[dict]]]] = None,
) -> dict:
    """
    自动双向同步币安实盘持仓到本地 PositionStore:
    1. 发现新开多单 -> 自动录入实际开仓价/持仓量/标记价，并自动匹配 15m TradingView 对数趋势线。
    2. 发现持仓已平仓 (positionAmt == 0) -> 自动归档至 history 历史记录。
    3. 已存在的持仓 -> 自动更新持仓均价、数量与最新标记价 (mark_price)，供下游风控直接复用。
    """
    if not client.is_configured():
        logger.info("ℹ️ 币安 API 未配置，维持手动/本地持仓管理模式。")
        return {
            "synced": False,
            "reason": "API_NOT_CONFIGURED",
            "active_count": len(pos_store.get_open_positions()),
            "new_added": [],
            "auto_closed": [],
        }

    active_longs = client.get_active_long_positions()
    if active_longs is None:
        return {
            "synced": False,
            "reason": "POSITION_SNAPSHOT_FAILED",
            "active_count": len(pos_store.get_open_positions()),
            "new_added": [],
            "auto_closed": [],
        }
    binance_symbols = {p["symbol"]: p for p in active_longs}
    current_open_positions = {p["symbol"]: p for p in pos_store.get_open_positions()}

    drawings_map = {}
    if binance_symbols.keys() - current_open_positions.keys():
        if load_drawings is None and tve is not None:
            load_drawings = tve.get_layout_drawings
        if load_drawings is not None:
            try:
                drawings_map = load_drawings(layout_id)
            except (RuntimeError, TimeoutError, urllib.error.URLError) as e:
                logger.warning("同步拉取 TV 画线失败: %s", e)

    new_added = []
    auto_closed = []

    for sym, b_pos in binance_symbols.items():
        if sym not in current_open_positions:
            matched_lines = match_symbol_drawings(sym, drawings_map)
            matched_trendline = select_best_position_trendline(matched_lines)

            notes = f"Binance Real-Time Auto-Sync (Leverage: {b_pos['leverage']}x)"
            pos_store.add_position(
                symbol=sym,
                entry_price=b_pos["entry_price"],
                amount=b_pos["amount"],
                trendline=matched_trendline,
                notes=notes,
                source="BINANCE_SYNC",
                leverage=b_pos["leverage"],
                mark_price=b_pos.get("mark_price", b_pos["entry_price"]),
                current_price=b_pos.get("mark_price", b_pos["entry_price"]),
            )
            new_added.append(sym)
            logger.info(
                "⚡ [币安实盘自动同步] 发现新开多仓: %s @ %s (数量: %s)",
                sym, b_pos["entry_price"], b_pos["amount"],
            )
        else:
            local_p = current_open_positions[sym]
            if (
                abs(local_p["entry_price"] - b_pos["entry_price"]) > 1e-6
                or abs(local_p["amount"] - b_pos["amount"]) > 1e-6
            ):
                logger.info(
                    "🔄 [币安实盘自动同步] 更新持仓均价/数量: %s %s -> %s",
                    sym, local_p["entry_price"], b_pos["entry_price"],
                )
                local_p["entry_price"] = b_pos["entry_price"]
                local_p["amount"] = b_pos["amount"]
            local_p["source"] = "BINANCE_SYNC"
            local_p["leverage"] = b_pos["leverage"]
            if b_pos.get("mark_price"):
                local_p["mark_price"] = b_pos["mark_price"]
                local_p["current_price"] = b_pos["mark_price"]

    for p in pos_store.get_open_positions():
        sym = p["symbol"]
        if p.get("source") == "BINANCE_SYNC" and sym not in binance_symbols:
            closed_p = pos_store.close_position(sym, reason="BINANCE_POSITION_CLOSED")
            if closed_p:
                auto_closed.append(sym)
                logger.info("🏁 [币安实盘自动同步] 监测到币安已平仓，自动归档: %s", sym)

    pos_store.save()

    return {
        "synced": True,
        "reason": "OK",
        "active_count": len(active_longs),
        "new_added": new_added,
        "auto_closed": auto_closed,
        "active_positions": active_longs,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    client = BinanceFuturesClient.from_env()
    print(f"Binance Futures Client: Configured={client.is_configured()}")
    if client.is_configured():
        server_ts = client.get_server_time()
        print(f"Server Time: {server_ts}")
        positions = client.get_active_positions(include_short=True)
        if positions is None:
            raise SystemExit("Failed to fetch Binance positions")
        print(f"Active Positions ({len(positions)}):")
        for p in positions:
            print(
                f"  - {p['symbol']} [{p['side']}]: {p['amount']} @ {p['entry_price']} "
                f"(mark: {p['mark_price']}, pnl: {p['unrealized_pnl']})"
            )
    else:
        print("Tip: Set BINANCE_API_KEY and BINANCE_API_SECRET env variables to test live positions.")
