#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
币安合约实时持仓同步客户端 (Binance USDⓈ-M Futures Position Client)
==============================================================================
1. 复用 scripts/binance_client.py 的统一 BinanceFuturesClient。
2. 与本地 SQLite 风控持仓存储 (PositionStore / open_positions + position_history) 执行双向自动同步:
   - 新开多仓 -> 录入实际开仓均价、持仓数量与标记价，匹配有效 15m TradingView 趋势线。
   - 加仓/减仓导致开仓均价变化 -> 重置移动止盈与止损提醒状态，重新跟踪最高价。
   - 已有仓位缺少趋势线 -> 自动重试匹配有效 15m 趋势线。
   - 币安已平仓 -> 自动归档至 position_history 历史记录。
"""

from __future__ import annotations

import logging
import math
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from binance_client import BinanceAPIError, BinanceFuturesClient  # noqa: E402
from config import DEFAULT_TV_LAYOUT_ID, get_beijing_time_str  # noqa: E402
import db_store  # noqa: E402
import tv_trendline_engine as tve  # noqa: E402

logger = logging.getLogger("binance_client")


def select_best_position_trendline(
    lines: Optional[list[dict[str, Any]]],
    current_time: Optional[float] = None,
) -> Optional[dict[str, Any]]:
    """
    从同一币种的多条规范化 TradingView 画线中筛选最适合作为持仓止损线的 15m 趋势线:
    1. 严格要求 15m 周期且更新时间/锚点在 72 小时以内（拒绝非 15m 与陈旧线）。
    2. 优先选择下行趋势线 ((p2 - p1) * (t2 - t1) <= 0)，其次按最近更新时间降序选取。
    """
    if not lines:
        return None
    valid: list[tuple[int, int, dict[str, Any]]] = []
    for line in lines:
        if not isinstance(line, Mapping):
            logger.warning("跳过非字典格式的趋势线条目: %r", line)
            continue
        if not tve.is_valid_15m_trendline(line, current_time=current_time):
            continue
        try:
            raw_p1 = line["p1"]
            raw_p2 = line["p2"]
            raw_t1 = line["t1"]
            raw_t2 = line["t2"]
            if any(isinstance(v, bool) for v in (raw_p1, raw_p2, raw_t1, raw_t2)):
                raise TypeError("boolean trendline coordinate")
            p1 = float(raw_p1)
            p2 = float(raw_p2)
            t1 = int(raw_t1)
            t2 = int(raw_t2)
            s_time = line.get("server_update_time")
            recency = int(s_time) if s_time is not None else max(t1, t2)
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("跳过畸形 15m 趋势线 (%s): %s", line.get("id"), exc)
            continue
        if not math.isfinite(p1) or not math.isfinite(p2) or p1 <= 0 or p2 <= 0 or t1 == t2:
            logger.warning("跳过无效几何参数 15m 趋势线 (%s): p1=%r p2=%r t1=%r t2=%r", line.get("id"), p1, p2, t1, t2)
            continue
        descending = 1 if (p2 - p1) * (t2 - t1) <= 0 else 0
        valid.append((descending, recency, dict(line)))

    if not valid:
        return None
    valid.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return valid[0][2]


def sync_binance_positions(
    client: BinanceFuturesClient,
    pos_store: Any,
    layout_id: str = DEFAULT_TV_LAYOUT_ID,
    *,
    load_drawings: Optional[Callable[[str], dict[str, list[dict[str, Any]]]]] = None,
) -> dict[str, Any]:
    """
    自动双向同步币安实盘多头持仓到本地 PositionStore。
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

    try:
        active_longs = client.get_active_positions(include_short=False)
    except BinanceAPIError as e:
        logger.warning("同步币安持仓快照失败: %s", e)
        return {
            "synced": False,
            "reason": "POSITION_SNAPSHOT_FAILED",
            "active_count": len(pos_store.get_open_positions()),
            "new_added": [],
            "auto_closed": [],
        }

    binance_symbols = {p["symbol"]: p for p in active_longs}

    new_added: list[str] = []
    auto_closed: list[str] = []

    pre_open_positions = {p["symbol"]: p for p in pos_store.get_open_positions()}
    missing_trendline_syms = {
        sym for sym, p in pre_open_positions.items()
        if sym in binance_symbols and not p.get("trendline")
    }
    need_drawings = bool(binance_symbols.keys() - pre_open_positions.keys()) or bool(missing_trendline_syms)

    drawings_map: dict[str, list[dict[str, Any]]] = {}
    if need_drawings:
        fetcher = load_drawings if load_drawings is not None else tve.get_layout_drawings
        try:
            raw_drawings = fetcher(layout_id)
        except (BinanceAPIError, RuntimeError, TimeoutError, OSError) as e:
            logger.warning("同步拉取 TV 画线失败: %s", e)
        else:
            if not isinstance(raw_drawings, Mapping):
                raise TypeError(
                    f"Expected mapping from load_drawings({layout_id!r}), got {type(raw_drawings).__name__}"
                )
            drawings_map = dict(raw_drawings)

    with pos_store.transaction():
        current_open_positions = {p["symbol"]: p for p in pos_store.get_open_positions()}
        can_batch_store = hasattr(pos_store, "_open_positions") and hasattr(pos_store, "database")
        dirty = False

        for sym, b_pos in binance_symbols.items():
            if sym not in current_open_positions:
                matched_lines = drawings_map.get(sym)
                matched_trendline = select_best_position_trendline(matched_lines)
                notes = f"Binance Real-Time Auto-Sync (Leverage: {b_pos['leverage']}x)"
                if can_batch_store:
                    pos_store._open_positions.append({
                        "symbol": sym,
                        "entry_price": float(b_pos["entry_price"]),
                        "amount": float(b_pos["amount"]),
                        "entry_time": get_beijing_time_str(),
                        "highest_price": float(b_pos["entry_price"]),
                        "current_price": b_pos["mark_price"],
                        "mark_price": b_pos["mark_price"],
                        "trailing_stop_triggered": False,
                        "trailing_stop_price": None,
                        "stop_loss_notified": False,
                        "profit_lock_notified": False,
                        "trendline": matched_trendline,
                        "status": "OPEN",
                        "notes": notes,
                        "source": "BINANCE_SYNC",
                        "leverage": b_pos["leverage"],
                    })
                    dirty = True
                else:
                    pos_store.add_position(
                        symbol=sym,
                        entry_price=b_pos["entry_price"],
                        amount=b_pos["amount"],
                        trendline=matched_trendline,
                        notes=notes,
                        source="BINANCE_SYNC",
                        leverage=b_pos["leverage"],
                        mark_price=b_pos["mark_price"],
                        current_price=b_pos["mark_price"],
                    )
                new_added.append(sym)
                logger.info(
                    "⚡ [币安实盘自动同步] 发现新开多仓: %s @ %s (数量: %s)",
                    sym, b_pos["entry_price"], b_pos["amount"],
                )
                continue

            local_p = current_open_positions[sym]
            old_entry = float(local_p["entry_price"])
            new_entry = float(b_pos["entry_price"])
            entry_changed = abs(old_entry - new_entry) / max(abs(new_entry), 1e-12) > 1e-5
            old_amount = float(local_p["amount"])
            new_amount = float(b_pos["amount"])
            amount_changed = abs(old_amount - new_amount) > max(1e-12, max(abs(old_amount), abs(new_amount)) * 1e-6)

            if entry_changed:
                logger.info(
                    "🔄 [币安实盘自动同步] 更新持仓均价/数量: %s %s -> %s",
                    sym, local_p["entry_price"], b_pos["entry_price"],
                )
                local_p["entry_price"] = b_pos["entry_price"]
                local_p["trailing_stop_triggered"] = False
                local_p["trailing_stop_price"] = None
                local_p["profit_lock_notified"] = False
                local_p["stop_loss_notified"] = False
                mark_val = float(b_pos["mark_price"])
                entry_val = float(b_pos["entry_price"])
                local_p["highest_price"] = max(mark_val, entry_val) if mark_val > 0 else entry_val
                for key in (
                    "trailing_stop_activated_at",
                    "pending_stop_loss_candle",
                    "pending_trailing_activation_price",
                    "pending_profit_lock_price",
                ):
                    local_p.pop(key, None)
                dirty = True

            if amount_changed:
                local_p["amount"] = b_pos["amount"]
                dirty = True

            if not local_p.get("trendline") and drawings_map:
                matched_trendline = select_best_position_trendline(drawings_map.get(sym))
                if matched_trendline is not None:
                    local_p["trendline"] = matched_trendline
                    dirty = True

            if local_p.get("leverage") != b_pos["leverage"]:
                local_p["leverage"] = b_pos["leverage"]
                dirty = True
            if b_pos["mark_price"] > 0:
                if local_p.get("mark_price") != b_pos["mark_price"]:
                    local_p["mark_price"] = b_pos["mark_price"]
                    dirty = True
                if not local_p.get("current_price") or float(local_p["current_price"]) <= 0:
                    local_p["current_price"] = b_pos["mark_price"]
                    dirty = True

        for p in pos_store.get_open_positions():
            sym = p["symbol"]
            if p.get("source") == "BINANCE_SYNC" and sym not in binance_symbols:
                exit_price = p.get("mark_price") or p.get("current_price")
                if can_batch_store:
                    p["status"] = "BINANCE_POSITION_CLOSED"
                    p["closed_at"] = db_store.now_beijing_iso()
                    if exit_price is not None and float(exit_price) > 0:
                        p["exit_price"] = float(exit_price)
                        entry_val = float(p["entry_price"])
                        if entry_val > 0:
                            p["realized_pnl_pct"] = round((p["exit_price"] - entry_val) / entry_val * 100.0, 2)
                    pos_store._open_positions.remove(p)
                    db_store.append_position_history(
                        pos_store.database, p, conn=getattr(pos_store, "_tx_conn", None)
                    )
                    dirty = True
                    closed_p = p
                else:
                    closed_p = pos_store.close_position(
                        sym,
                        reason="BINANCE_POSITION_CLOSED",
                        exit_price=exit_price,
                    )
                if closed_p:
                    auto_closed.append(sym)
                    logger.info("🏁 [币安实盘自动同步] 监测到币安已平仓，自动归档: %s", sym)

        if dirty:
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
        print(f"Active Positions ({len(positions)}):")
        for p in positions:
            print(
                f"  - {p['symbol']} [{p['side']}]: {p['amount']} @ {p['entry_price']} "
                f"(mark: {p['mark_price']}, pnl: {p['unrealized_pnl']})"
            )
    else:
        print("Tip: Set BINANCE_API_KEY and BINANCE_API_SECRET env variables to test live positions.")
