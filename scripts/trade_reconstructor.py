#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FIFO Trade Reconstruction & Position/Trade Metadata Enrichment
==============================================================
Pure reconstruction and normalization functions with zero network or DB I/O:
1. Timestamp parsing and duration formatting (Beijing UTC+8).
2. Local/Binance closed-trade and active-position enrichment.
3. Unified (symbol, positionSide) FIFO lot state machine using `collections.deque`
   and symbol-indexed bisect matching against local position history.
"""

from __future__ import annotations

from bisect import bisect_left
from collections import deque
from datetime import datetime
from decimal import Decimal
import logging
import math
from pathlib import Path
import sys
import time
from typing import Any, Deque, Dict, List, Mapping, Optional, Sequence, Tuple

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from config import BEIJING_TZ, clean_symbol, get_beijing_time_str  # noqa: E402

logger = logging.getLogger("account_analytics")

_LOCAL_MATCH_WINDOW_SEC = 300.0


def parse_to_timestamp(dt_val: object) -> Optional[float]:
    """
    将多种时间格式解析为 Unix 秒级时间戳 (float)。
    空值或无法解析时返回 None，严禁伪造当前时间。
    """
    if dt_val is None or isinstance(dt_val, bool):
        return None
    if isinstance(dt_val, (int, float)):
        num = float(dt_val)
        if not math.isfinite(num) or num <= 0:
            return None
        return num / 1000.0 if num > 1e11 else num

    s = str(dt_val).strip()
    if not s or s == "--":
        return None

    try:
        val = float(s)
        if not math.isfinite(val) or val <= 0:
            return None
        return val / 1000.0 if val > 1e11 else val
    except ValueError:
        pass

    iso_candidate = f"{s[:-1]}+00:00" if s.endswith(("Z", "z")) else s
    try:
        dt_iso = datetime.fromisoformat(iso_candidate)
        if dt_iso.tzinfo is None:
            dt_iso = dt_iso.replace(tzinfo=BEIJING_TZ)
        return dt_iso.timestamp()
    except ValueError:
        pass

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=BEIJING_TZ)
            return dt.timestamp()
        except ValueError:
            continue

    logger.debug("无法解析的时间格式: %r", dt_val)
    return None


def format_duration(seconds: int) -> str:
    """将秒数格式化为人类可读的持仓周期字符串 (如 12m 26s, 3h 15m)。"""
    if seconds <= 0:
        return "0s"
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60

    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if secs > 0 or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


def classify_outcome(net_pnl: float) -> str:
    """Classify net PnL into WIN / LOSS / BREAKEVEN using a 1e-4 USDT tolerance."""
    if net_pnl > 1e-4:
        return "WIN"
    if net_pnl < -1e-4:
        return "LOSS"
    return "BREAKEVEN"


def income_asset(item: Mapping[str, Any]) -> str:
    """Return normalized uppercase asset name for a Binance income record."""
    asset = item.get("asset")
    return str(asset).strip().upper() if asset else "USDT"


def _extract_trendline_meta(trendline: object) -> Tuple[Optional[Any], str, Optional[dict]]:
    if not isinstance(trendline, Mapping):
        return None, "--", None
    trendline_id = trendline.get("id") or trendline.get("line_id")
    p1_raw = trendline.get("p1")
    p2_raw = trendline.get("p2")
    p1 = float(p1_raw) if p1_raw is not None else None
    p2 = float(p2_raw) if p2_raw is not None else None
    state = trendline.get("state")
    state_color = state.get("linecolor") if isinstance(state, Mapping) else None
    color = trendline.get("line_color") or state_color or "#2196f3"
    info = {
        "id": trendline_id,
        "p1": p1,
        "p2": p2,
        "dt1_str": trendline.get("dt1_str") or "",
        "dt2_str": trendline.get("dt2_str") or "",
        "color": color,
        "interval": trendline.get("interval", "15"),
    }
    if trendline_id and p1 is not None and p2 is not None:
        label = f"TL#{trendline_id} ({p1:.4f} -> {p2:.4f})"
    elif trendline_id:
        label = f"TL#{trendline_id}"
    else:
        label = "--"
    return trendline_id, label, info


def _resolve_close_reason(
    raw: Mapping[str, Any], exit_price: float, entry_price: float, side: str
) -> Tuple[str, str]:
    status = str(raw.get("status") or "").upper()
    trailing_triggered = bool(raw.get("trailing_stop_triggered", False))
    direction = 1.0 if side == "LONG" else -1.0

    if trailing_triggered or "TRAILING" in status:
        return "动态移动保本锁利", "TRAILING_PROFIT_LOCK"
    if status == "STOP_LOSS_TRIGGERED":
        if entry_price > 0 and (exit_price - entry_price) * direction >= 0:
            return "动态移动保本锁利", "TRAILING_PROFIT_LOCK"
        return "跌破趋势线止损", "TRENDLINE_BREAK_STOP"
    if status == "BREAK_TRENDLINE_STOP":
        return "跌破趋势线止损", "TRENDLINE_BREAK_STOP"
    if status == "BINANCE_POSITION_CLOSED":
        return "币安实盘平仓", "BINANCE_AUTO_CLOSED"
    if status == "MANUAL_CLOSE":
        return "手动平仓", "MANUAL_CLOSE"
    if status == "TAKE_PROFIT":
        return "止盈离场", "TAKE_PROFIT"
    return str(raw.get("notes") or status or "平仓归档"), "OTHER_CLOSE"


def enrich_trade_record(
    raw: Mapping[str, Any],
    index: int,
    *,
    fee_rate: float = 0.0005,
    default_leverage: int = 5,
) -> dict:
    """标准化本地历史平仓交易记录，严格区分 LONG/SHORT 方向，不伪造平仓价/时间戳/负数手续费。"""
    sym = clean_symbol(str(raw["symbol"]))
    entry_price = float(raw["entry_price"]) if raw.get("entry_price") is not None else 0.0
    raw_amount = float(raw["amount"]) if raw.get("amount") is not None else 0.0
    raw_side = str(raw.get("side") or raw.get("positionSide") or "").strip().upper()
    if raw_side in ("LONG", "SHORT"):
        side = raw_side
    else:
        side = "SHORT" if raw_amount < 0 else "LONG"
    amount = abs(raw_amount)
    direction = 1.0 if side == "LONG" else -1.0
    leverage = int(raw["leverage"]) if raw.get("leverage") else default_leverage

    raw_exit = raw.get("exit_price")
    exit_price: Optional[float] = float(raw_exit) if raw_exit is not None and float(raw_exit) > 0 else None

    entry_time_raw = raw.get("entry_time")
    closed_at_raw = raw.get("closed_at") if raw.get("closed_at") is not None else raw.get("exit_time")
    entry_ts = parse_to_timestamp(entry_time_raw)
    closed_ts = parse_to_timestamp(closed_at_raw)

    if entry_ts is not None and closed_ts is not None:
        duration_seconds = max(0, int(closed_ts - entry_ts))
        duration_str = format_duration(duration_seconds)
    else:
        duration_seconds = 0
        duration_str = "--"

    entry_time_str = (
        get_beijing_time_str(entry_ts)
        if entry_ts is not None
        else (str(entry_time_raw).strip() if entry_time_raw and str(entry_time_raw).strip() != "--" else "--")
    )
    closed_at_str = (
        get_beijing_time_str(closed_ts)
        if closed_ts is not None
        else (str(closed_at_raw).strip() if closed_at_raw and str(closed_at_raw).strip() != "--" else "--")
    )

    entry_notional = round(entry_price * amount, 4) if entry_price > 0 and amount > 0 else 0.0
    exit_notional = round(exit_price * amount, 4) if exit_price is not None and exit_price > 0 and amount > 0 else 0.0

    realized_pnl: Optional[float]
    pnl_pct: Optional[float]
    fee: Optional[float]
    net_pnl: Optional[float]

    if exit_price is not None and entry_price > 0 and amount > 0:
        realized_pnl = round((exit_price - entry_price) * amount * direction, 4)
        pnl_pct = round(((exit_price - entry_price) * direction / entry_price) * 100.0, 2)
    elif raw.get("realized_pnl") is not None:
        realized_pnl = round(float(raw["realized_pnl"]), 4)
        pnl_pct = round(realized_pnl / entry_notional * 100.0, 2) if entry_notional > 0 else None
    else:
        realized_pnl = None
        pnl_pct = None

    roe_pct = round(pnl_pct * leverage, 2) if pnl_pct is not None else None

    if raw.get("fee") is not None:
        fee = round(float(raw["fee"]), 4)
    elif realized_pnl is not None and (entry_notional > 0 or exit_notional > 0):
        fee = round((entry_notional + exit_notional) * fee_rate, 4)
    elif realized_pnl is not None:
        fee = 0.0
    else:
        fee = None

    if realized_pnl is not None:
        net_pnl = round(realized_pnl - (fee or 0.0), 4)
        outcome = classify_outcome(net_pnl)
    else:
        net_pnl = None
        outcome = "UNKNOWN"

    close_reason, close_reason_code = _resolve_close_reason(raw, exit_price or 0.0, entry_price, side)
    trendline_id, trendline_label, trendline_info = _extract_trendline_meta(raw.get("trendline"))

    ts_suffix = int(closed_ts) if closed_ts is not None else index + 1
    trade_id = f"TRD-{index + 1:03d}-{sym}-{ts_suffix}"

    return {
        "trade_id": trade_id,
        "symbol": sym,
        "side": side,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "amount": amount,
        "entry_notional": entry_notional,
        "exit_notional": exit_notional,
        "realized_pnl": realized_pnl,
        "fee": fee,
        "fee_asset": "USDT",
        "non_usdt_fees": {},
        "net_pnl": net_pnl,
        "pnl_pct": pnl_pct,
        "roe_pct": roe_pct,
        "leverage": leverage,
        "entry_time": entry_time_str,
        "closed_at": closed_at_str,
        "entry_ts": int(entry_ts * 1000) if entry_ts is not None else None,
        "closed_ts": int(closed_ts * 1000) if closed_ts is not None else None,
        "duration_seconds": duration_seconds,
        "duration_str": duration_str,
        "outcome": outcome,
        "status": str(raw.get("status") or "").upper(),
        "close_reason": close_reason,
        "close_reason_code": close_reason_code,
        "highest_price": float(raw["highest_price"]) if raw.get("highest_price") is not None else exit_price,
        "trailing_stop_triggered": bool(raw.get("trailing_stop_triggered", False)),
        "trailing_stop_price": raw.get("trailing_stop_price"),
        "trendline_id": trendline_id,
        "trendline_label": trendline_label,
        "trendline_info": trendline_info,
        "source": raw.get("source", "MANUAL"),
        "notes": raw.get("notes", ""),
    }


def enrich_active_position(
    raw: Mapping[str, Any],
    index: int,
    *,
    default_leverage: int = 5,
    now_ts: Optional[float] = None,
) -> dict:
    """标准化当前活跃持仓；支持提取币安 /fapi/v2/positionRisk 的 updateTime 作为时间基准。"""
    sym = clean_symbol(str(raw["symbol"]))
    raw_entry = raw.get("entry_price") if raw.get("entry_price") is not None else raw.get("entryPrice")
    entry_price = float(raw_entry) if raw_entry is not None else 0.0

    raw_amt_val = raw.get("amount") if raw.get("amount") is not None else raw.get("positionAmt")
    raw_amt = float(raw_amt_val) if raw_amt_val is not None else 0.0
    raw_side = str(raw.get("side") or raw.get("positionSide") or "").strip().upper()
    if raw_side in ("LONG", "SHORT"):
        side = raw_side
    else:
        side = "SHORT" if raw_amt < 0 else "LONG"
    amount = abs(raw_amt)
    direction = -1.0 if side == "SHORT" else 1.0
    leverage = int(raw["leverage"]) if raw.get("leverage") else default_leverage

    raw_mark = (
        raw.get("mark_price")
        if raw.get("mark_price") is not None
        else (raw.get("markPrice") if raw.get("markPrice") is not None else raw.get("current_price"))
    )
    mark_price = float(raw_mark) if raw_mark is not None and float(raw_mark) > 0 else entry_price

    notional = round(mark_price * amount, 4)
    initial_margin = round(notional / leverage, 4) if leverage > 0 else notional

    raw_upnl = raw.get("unrealized_pnl") if raw.get("unrealized_pnl") is not None else raw.get("unRealizedProfit")
    unrealized_pnl = float(raw_upnl) if raw_upnl is not None else ((mark_price - entry_price) * amount * direction)
    unrealized_pnl = round(unrealized_pnl, 4)

    pnl_pct = round(((mark_price - entry_price) * direction / entry_price * 100.0), 2) if entry_price > 0 else 0.0
    roe_pct = round(pnl_pct * leverage, 2)

    raw_liq = raw.get("liquidation_price") if raw.get("liquidation_price") is not None else raw.get("liquidationPrice")
    if raw_liq is not None and float(raw_liq) > 0:
        est_liq_price = round(float(raw_liq), 6)
    elif entry_price > 0 and leverage > 1:
        est_liq_price = round(entry_price * (1.0 - 0.9 / leverage * direction), 6)
    else:
        est_liq_price = 0.0

    entry_time_raw = raw.get("entry_time")
    fallback_time_raw = raw.get("updateTime") if raw.get("updateTime") is not None else raw.get("update_time")
    entry_ts = parse_to_timestamp(entry_time_raw)
    if entry_ts is None:
        entry_ts = parse_to_timestamp(fallback_time_raw)

    current_ts = now_ts if now_ts is not None else time.time()
    if entry_ts is not None:
        holding_seconds = max(0, int(current_ts - entry_ts))
        holding_str = format_duration(holding_seconds)
        entry_time_str = (
            str(entry_time_raw).strip()
            if entry_time_raw and str(entry_time_raw).strip() != "--"
            else get_beijing_time_str(entry_ts)
        )
        entry_ts_ms: Optional[int] = int(entry_ts * 1000)
    else:
        holding_seconds = 0
        holding_str = "--"
        entry_time_str = "--"
        entry_ts_ms = None

    trendline = raw.get("trendline")
    trendline_id = trendline.get("id") if isinstance(trendline, Mapping) else raw.get("trendline_id")

    return {
        "position_id": f"POS-{index + 1:02d}-{sym}",
        "symbol": sym,
        "side": side,
        "risk_monitored": side == "LONG",
        "entry_price": entry_price,
        "mark_price": mark_price,
        "amount": amount,
        "notional": notional,
        "initial_margin": initial_margin,
        "leverage": leverage,
        "unrealized_pnl": unrealized_pnl,
        "pnl_pct": pnl_pct,
        "roe_pct": roe_pct,
        "liquidation_price": est_liq_price,
        "entry_time": entry_time_str,
        "entry_ts": entry_ts_ms,
        "holding_seconds": holding_seconds,
        "holding_str": holding_str,
        "highest_price": float(raw["highest_price"]) if raw.get("highest_price") is not None else mark_price,
        "trailing_stop_triggered": bool(raw.get("trailing_stop_triggered", False)),
        "trailing_stop_price": raw.get("trailing_stop_price"),
        "stop_loss_notified": bool(raw.get("stop_loss_notified", False)),
        "trendline_break_sl_status": raw.get("trendline_break_sl_status", "SAFE"),
        "trendline_id": trendline_id,
        "trendline": trendline,
        "source": raw.get(
            "source",
            "BINANCE_SYNC" if (raw.get("positionAmt") is not None or raw.get("position_side") is not None) else "MANUAL",
        ),
        "status": "OPEN",
        "notes": raw.get("notes", ""),
    }


def merge_active_positions(
    active_positions_binance: Sequence[Mapping[str, Any]],
    local_positions: Sequence[Mapping[str, Any]],
    positions_available: bool,
    *,
    default_leverage: int = 5,
    now_ts: Optional[float] = None,
) -> List[dict]:
    """当币安持仓可用时，按 (symbol, side) 继承本地趋势线与移动锁利元数据。"""
    if not positions_available:
        return [
            enrich_active_position(p, idx, default_leverage=default_leverage, now_ts=now_ts)
            for idx, p in enumerate(local_positions)
        ]

    local_by_key: Dict[Tuple[str, str], Mapping[str, Any]] = {}
    for lp in local_positions:
        sym = clean_symbol(str(lp.get("symbol") or ""))
        raw_side = str(lp.get("side") or lp.get("positionSide") or "").upper()
        amt = float(lp.get("amount") or 0.0)
        lp_side = raw_side if raw_side in ("LONG", "SHORT") else ("SHORT" if amt < 0 else "LONG")
        local_by_key[(sym, lp_side)] = lp

    merged_raw: List[Mapping[str, Any]] = []
    for bp in active_positions_binance:
        sym = clean_symbol(str(bp["symbol"]))
        raw_amt_val = bp.get("positionAmt") if bp.get("positionAmt") is not None else bp.get("signed_amount")
        if raw_amt_val is None:
            raw_amt_val = bp.get("amount")
        raw_amt = float(raw_amt_val) if raw_amt_val is not None else 0.0
        raw_side = str(bp.get("side") or bp.get("positionSide") or bp.get("position_side") or "BOTH").upper()
        bp_side = raw_side if raw_side in ("LONG", "SHORT") else ("SHORT" if raw_amt < 0 else "LONG")
        local = local_by_key.get((sym, bp_side))
        combined = dict(bp)
        combined.setdefault("source", "BINANCE_SYNC")
        if local:
            for meta_key in (
                "trendline",
                "trendline_id",
                "trailing_stop_price",
                "trailing_stop_triggered",
                "stop_loss_notified",
                "trendline_break_sl_status",
                "highest_price",
                "notes",
                "entry_time",
            ):
                if local.get(meta_key) is not None and combined.get(meta_key) is None:
                    combined[meta_key] = local[meta_key]
        merged_raw.append(combined)

    return [
        enrich_active_position(p, idx, default_leverage=default_leverage, now_ts=now_ts)
        for idx, p in enumerate(merged_raw)
    ]


def _merge_fee_dicts(a: Mapping[str, float], b: Mapping[str, float]) -> Dict[str, float]:
    out = dict(a)
    for k, v in b.items():
        out[k] = out.get(k, 0.0) + v
    return out


def _infer_local_trade_side(loc: Mapping[str, Any]) -> Optional[str]:
    raw_side = str(loc.get("side") or loc.get("positionSide") or "").strip().upper()
    if raw_side in ("LONG", "SHORT"):
        return raw_side
    raw_amount = loc.get("amount")
    if raw_amount is not None:
        amt = float(raw_amount)
        if amt < 0:
            return "SHORT"
        if amt > 0:
            return "LONG"
    return None


def _build_local_trade_index(
    local_history: Sequence[Mapping[str, Any]],
) -> Dict[str, Tuple[List[float], List[Tuple[int, Optional[str], float, Mapping[str, Any]]]]]:
    """Pre-index local position history by normalized symbol and sorted close timestamp."""
    grouped: Dict[str, List[Tuple[int, Optional[str], float, Mapping[str, Any]]]] = {}
    for idx, loc in enumerate(local_history):
        sym = clean_symbol(str(loc.get("symbol") or ""))
        if not sym:
            continue
        loc_closed_at = loc.get("closed_at") or loc.get("exit_time")
        loc_ts = parse_to_timestamp(loc_closed_at)
        if loc_ts is None:
            continue
        loc_side = _infer_local_trade_side(loc)
        grouped.setdefault(sym, []).append((idx, loc_side, loc_ts, loc))

    indexed: Dict[str, Tuple[List[float], List[Tuple[int, Optional[str], float, Mapping[str, Any]]]]] = {}
    for sym, entries in grouped.items():
        entries.sort(key=lambda entry: (entry[2], entry[0]))
        indexed[sym] = ([entry[2] for entry in entries], entries)
    return indexed


def _pop_indexed_local_trade(
    sym: str,
    closed_sec: float,
    indexed: Dict[str, Tuple[List[float], List[Tuple[int, Optional[str], float, Mapping[str, Any]]]]],
    used_indices: set[int],
    side: Optional[str] = None,
) -> Optional[Mapping[str, Any]]:
    bucket = indexed.get(sym)
    if bucket is None:
        return None
    timestamps, entries = bucket
    pos = bisect_left(timestamps, closed_sec - _LOCAL_MATCH_WINDOW_SEC)
    best_idx: Optional[int] = None
    best_entry: Optional[Mapping[str, Any]] = None
    best_diff = float("inf")

    for i in range(pos, len(entries)):
        idx, loc_side, loc_ts, loc = entries[i]
        if loc_ts - closed_sec > _LOCAL_MATCH_WINDOW_SEC:
            break
        if idx in used_indices:
            continue
        if side in ("LONG", "SHORT") and loc_side is not None and loc_side != side:
            continue
        diff = abs(loc_ts - closed_sec)
        if diff < best_diff or (diff == best_diff and (best_idx is None or idx < best_idx)):
            best_diff = diff
            best_idx = idx
            best_entry = loc

    if best_idx is not None:
        used_indices.add(best_idx)
        return best_entry
    return None


def pop_matching_local_trade(
    sym: str,
    closed_sec: float,
    local_history: Sequence[Mapping[str, Any]],
    used_indices: set[int],
    side: Optional[str] = None,
) -> Optional[Mapping[str, Any]]:
    """Match the closest unused local trade record within +/-300s of `closed_sec`."""
    best_idx: Optional[int] = None
    best_diff = float("inf")
    for idx, loc in enumerate(local_history):
        if idx in used_indices:
            continue
        if clean_symbol(str(loc.get("symbol") or "")) != sym:
            continue
        if side in ("LONG", "SHORT"):
            loc_side = _infer_local_trade_side(loc)
            if loc_side is not None and loc_side != side:
                continue
        loc_closed_at = loc.get("closed_at") or loc.get("exit_time")
        loc_ts = parse_to_timestamp(loc_closed_at)
        if loc_ts is None:
            continue
        diff = abs(loc_ts - closed_sec)
        if diff <= _LOCAL_MATCH_WINDOW_SEC and diff < best_diff:
            best_diff = diff
            best_idx = idx
    if best_idx is not None:
        used_indices.add(best_idx)
        return local_history[best_idx]
    return None


def _precompute_both_zero_close(
    sorted_fills: Sequence[Mapping[str, Any]],
    realized_by_trade: Mapping[Tuple[str, str], float],
) -> set[int]:
    """O(N) 反向扫描预计算单向模式 (BOTH) 空队列 0 盈亏平仓下标集合。"""
    both_indices_by_sym: Dict[str, List[int]] = {}
    for idx, f in enumerate(sorted_fills):
        if str(f.get("positionSide") or "BOTH").upper() == "BOTH":
            sym = clean_symbol(str(f["symbol"]))
            both_indices_by_sym.setdefault(sym, []).append(idx)

    zero_close_indices: set[int] = set()
    for sym, indices in both_indices_by_sym.items():
        has_close_before_opp: Dict[str, bool] = {"BUY": False, "SELL": False}
        next_opp: Dict[str, Optional[Tuple[Optional[float], object, float]]] = {"BUY": None, "SELL": None}
        for idx in reversed(indices):
            g = sorted_fills[idx]
            g_side = str(g["side"]).upper()
            opp_side = "SELL" if g_side == "BUY" else "BUY"
            g_price = float(g["price"])
            g_key = (sym, str(g["id"]))
            g_income_rpnl = realized_by_trade.get(g_key)
            g_raw_rpnl = g.get("realizedPnl")
            g_has_nonzero_rpnl = g_raw_rpnl is not None and abs(float(g_raw_rpnl)) > 1e-12

            is_zero_close = False
            if has_close_before_opp.get(g_side):
                is_zero_close = True
            else:
                opp_info = next_opp.get(g_side)
                if opp_info is not None:
                    opp_inc, opp_raw, opp_price = opp_info
                    if (
                        opp_inc is None
                        and opp_raw is not None
                        and abs(float(opp_raw)) <= 1e-12
                        and abs(opp_price - g_price) > max(1e-8, g_price * 1e-6)
                    ):
                        is_zero_close = True
            if is_zero_close:
                zero_close_indices.add(idx)

            if g_income_rpnl is not None or g_has_nonzero_rpnl or bool(g.get("reduceOnly")):
                has_close_before_opp[g_side] = True
            has_close_before_opp[opp_side] = False
            next_opp[opp_side] = (g_income_rpnl, g_raw_rpnl, g_price)

    return zero_close_indices


def _consume_fifo_lots(
    queue: Deque[dict],
    *,
    sym: str,
    close_side: str,
    order_id: str,
    ts_ms: int,
    qty: Decimal,
    quote_qty: float,
    fill_fee_usdt: float,
    fill_non_usdt: Mapping[str, float],
    fill_realized: Optional[float],
) -> List[dict]:
    """按 FIFO 消耗开仓批次，并为多笔部分平仓与记录起点前已开仓位生成标准 slice。"""
    slices: List[dict] = []
    remaining = qty
    direction = 1.0 if close_side == "LONG" else -1.0
    total_queue_qty = sum((lot["qty"] for lot in queue), Decimal("0"))
    has_pre_recording_remainder = qty > total_queue_qty
    known_realized_sum = 0.0

    while remaining > 0 and queue:
        lot = queue[0]
        take = min(remaining, lot["qty"])
        lot_frac = float(take / lot["qty"])
        fill_frac = float(take / qty)

        open_notional_slice = lot["notional"] * lot_frac
        open_fee_slice = lot["fee_usdt"] * lot_frac
        open_non_usdt_slice = {a: v * lot_frac for a, v in lot["non_usdt_fees"].items()}

        lot["qty"] -= take
        lot["notional"] -= open_notional_slice
        lot["fee_usdt"] -= open_fee_slice
        for asset_k in list(lot["non_usdt_fees"].keys()):
            lot["non_usdt_fees"][asset_k] -= open_non_usdt_slice[asset_k]
        if lot["qty"] == 0:
            queue.popleft()

        close_notional_slice = quote_qty * fill_frac
        close_fee_slice = fill_fee_usdt * fill_frac
        close_non_usdt_slice = {a: v * fill_frac for a, v in fill_non_usdt.items()}
        if has_pre_recording_remainder or fill_realized is None:
            realized_slice = (close_notional_slice - open_notional_slice) * direction
        else:
            realized_slice = fill_realized * fill_frac
        known_realized_sum += realized_slice

        slices.append({
            "symbol": sym,
            "side": close_side,
            "order_id": order_id,
            "qty": float(take),
            "open_notional": open_notional_slice,
            "close_notional": close_notional_slice,
            "realized_pnl": realized_slice,
            "open_fee_usdt": open_fee_slice,
            "close_fee_usdt": close_fee_slice,
            "non_usdt_fees": _merge_fee_dicts(open_non_usdt_slice, close_non_usdt_slice),
            "open_ts": lot["time"],
            "close_ts": ts_ms,
        })
        remaining -= take

    if remaining > 0:
        fill_frac = float(remaining / qty)
        close_notional_slice = quote_qty * fill_frac
        close_fee_slice = fill_fee_usdt * fill_frac
        close_non_usdt_slice = {a: v * fill_frac for a, v in fill_non_usdt.items()}
        realized_slice = (fill_realized - known_realized_sum) if fill_realized is not None else 0.0
        open_notional_slice = close_notional_slice - direction * realized_slice
        slices.append({
            "symbol": sym,
            "side": close_side,
            "order_id": order_id,
            "qty": float(remaining),
            "open_notional": open_notional_slice,
            "close_notional": close_notional_slice,
            "realized_pnl": realized_slice,
            "open_fee_usdt": 0.0,
            "close_fee_usdt": close_fee_slice,
            "non_usdt_fees": close_non_usdt_slice,
            "open_ts": None,
            "close_ts": ts_ms,
        })

    return slices


def reconstruct_binance_trades(
    income_list: Sequence[Mapping[str, Any]],
    local_history: Sequence[Mapping[str, Any]],
    fills: Sequence[Mapping[str, Any]] = (),
    *,
    fee_rate: float = 0.0005,
    default_leverage: int = 5,
) -> Tuple[List[dict], float]:
    """
    统一的 Binance 历史平仓重构状态机:
    - 当提供 fills (/fapi/v1/userTrades) 时，按 (symbol, positionSide) 维护 FIFO 仓位批次，
      精准处理多轮开平、部分平仓、反向反手、单向模式 (BOTH) 及开/平双边手续费归集。
    - 对无成交明细的历史流水，按 (symbol, tradeId / 2s 窗口) 聚合，
      严格隔离非 USDT 手续费 (如 BNB)，并与本地 history 1 对 1 匹配元数据。
    返回: (trade_history, open_position_fee_usdt)
    """
    commissions_usdt: Dict[Tuple[str, str], float] = {}
    non_usdt_comm: Dict[Tuple[str, str], Dict[str, float]] = {}
    realized_by_trade: Dict[Tuple[str, str], float] = {}

    for item in income_list:
        sym = clean_symbol(str(item.get("symbol") or ""))
        if not sym:
            continue
        itype = item.get("incomeType")
        tid = str(item.get("tradeId") or "")
        asset = income_asset(item)
        amount = float(item.get("income") or 0.0)
        key = (sym, tid)

        if itype == "COMMISSION":
            if asset == "USDT":
                commissions_usdt[key] = commissions_usdt.get(key, 0.0) + (-amount)
            else:
                bucket = non_usdt_comm.setdefault(key, {})
                bucket[asset] = bucket.get(asset, 0.0) + abs(amount)
        elif itype == "REALIZED_PNL" and asset == "USDT" and tid:
            realized_by_trade[key] = realized_by_trade.get(key, 0.0) + amount

    open_lots: Dict[Tuple[str, str], Deque[dict]] = {}
    closed_slices: List[dict] = []
    matched_trade_keys: set[Tuple[str, str]] = set()
    consumed_comm_keys: set[Tuple[str, str]] = set()

    sorted_fills = sorted(fills, key=lambda f: (int(f["time"]), int(f["id"])))
    both_zero_close_indices = _precompute_both_zero_close(sorted_fills, realized_by_trade)
    for fill_idx, f in enumerate(sorted_fills):
        sym = clean_symbol(str(f["symbol"]))
        tid = str(f["id"])
        order_id = str(f["orderId"])
        fill_side = str(f["side"]).upper()
        raw_pos_side = str(f.get("positionSide") or "BOTH").upper()
        ts_ms = int(f["time"])
        qty = Decimal(str(f["qty"]))
        price = float(f["price"])
        quote_qty = float(f.get("quoteQty") if f.get("quoteQty") is not None else (price * float(qty)))
        if qty <= 0:
            continue

        t_key = (sym, tid)
        fill_fee_usdt = commissions_usdt.get(t_key, 0.0)
        fill_non_usdt = non_usdt_comm.get(t_key, {})
        if t_key in commissions_usdt or t_key in non_usdt_comm:
            consumed_comm_keys.add(t_key)
        fill_realized = realized_by_trade.get(t_key)
        raw_fill_rpnl = f.get("realizedPnl")
        if fill_realized is None and raw_fill_rpnl is not None:
            parsed_rpnl = float(raw_fill_rpnl)
            if abs(parsed_rpnl) > 1e-12:
                fill_realized = parsed_rpnl
        is_reduce_only = bool(f.get("reduceOnly"))

        if raw_pos_side in ("LONG", "SHORT"):
            queue = open_lots.setdefault((sym, raw_pos_side), deque())
            is_opening = (raw_pos_side == "LONG" and fill_side == "BUY") or (
                raw_pos_side == "SHORT" and fill_side == "SELL"
            )
            if is_opening:
                queue.append({
                    "side": raw_pos_side,
                    "qty": qty,
                    "notional": quote_qty,
                    "fee_usdt": fill_fee_usdt,
                    "non_usdt_fees": dict(fill_non_usdt),
                    "time": ts_ms,
                })
            else:
                matched_trade_keys.add(t_key)
                closed_slices.extend(
                    _consume_fifo_lots(
                        queue,
                        sym=sym,
                        close_side=raw_pos_side,
                        order_id=order_id,
                        ts_ms=ts_ms,
                        qty=qty,
                        quote_qty=quote_qty,
                        fill_fee_usdt=fill_fee_usdt,
                        fill_non_usdt=fill_non_usdt,
                        fill_realized=fill_realized,
                    )
                )
        else:
            queue = open_lots.setdefault((sym, "BOTH"), deque())
            if queue:
                active_side = queue[0]["side"]
                is_same_dir = (active_side == "LONG" and fill_side == "BUY") or (
                    active_side == "SHORT" and fill_side == "SELL"
                )
                if is_same_dir:
                    queue.append({
                        "side": active_side,
                        "qty": qty,
                        "notional": quote_qty,
                        "fee_usdt": fill_fee_usdt,
                        "non_usdt_fees": dict(fill_non_usdt),
                        "time": ts_ms,
                    })
                else:
                    total_open_qty = sum((lot["qty"] for lot in queue), Decimal("0"))
                    if is_reduce_only or qty <= total_open_qty:
                        close_part = qty
                    else:
                        # Distinguish closing pre-recording inventory from an actual position flip.
                        direction = 1.0 if active_side == "LONG" else -1.0
                        known_ratio = float(total_open_qty / qty)
                        known_close_notional = quote_qty * known_ratio
                        known_open_notional = sum(lot["notional"] for lot in queue)
                        expected_known_rpnl = (known_close_notional - known_open_notional) * direction
                        tol = max(1e-3, abs(expected_known_rpnl) * 0.02, known_close_notional * 1e-4)
                        is_pre_recording_close = (
                            (fill_realized is not None and abs(fill_realized - expected_known_rpnl) > tol)
                            or fill_idx in both_zero_close_indices
                        )
                        close_part = qty if is_pre_recording_close else total_open_qty
                    open_rem = qty - close_part
                    close_ratio = float(close_part / qty)
                    matched_trade_keys.add(t_key)
                    closed_slices.extend(
                        _consume_fifo_lots(
                            queue,
                            sym=sym,
                            close_side=active_side,
                            order_id=order_id,
                            ts_ms=ts_ms,
                            qty=close_part,
                            quote_qty=quote_qty * close_ratio,
                            fill_fee_usdt=fill_fee_usdt * close_ratio,
                            fill_non_usdt={a: v * close_ratio for a, v in fill_non_usdt.items()},
                            fill_realized=fill_realized,
                        )
                    )
                    if open_rem > 0:
                        new_side = "LONG" if fill_side == "BUY" else "SHORT"
                        open_ratio = float(open_rem / qty)
                        queue.append({
                            "side": new_side,
                            "qty": open_rem,
                            "notional": quote_qty * open_ratio,
                            "fee_usdt": fill_fee_usdt * open_ratio,
                            "non_usdt_fees": {a: v * open_ratio for a, v in fill_non_usdt.items()},
                            "time": ts_ms,
                        })
            elif (
                fill_realized is not None
                or is_reduce_only
                or fill_idx in both_zero_close_indices
            ):
                effective_realized = (
                    fill_realized
                    if fill_realized is not None
                    else (float(raw_fill_rpnl) if raw_fill_rpnl is not None else 0.0)
                )
                inferred_close_side = "LONG" if fill_side == "SELL" else "SHORT"
                matched_trade_keys.add(t_key)
                closed_slices.extend(
                    _consume_fifo_lots(
                        queue,
                        sym=sym,
                        close_side=inferred_close_side,
                        order_id=order_id,
                        ts_ms=ts_ms,
                        qty=qty,
                        quote_qty=quote_qty,
                        fill_fee_usdt=fill_fee_usdt,
                        fill_non_usdt=fill_non_usdt,
                        fill_realized=effective_realized,
                    )
                )
            else:
                new_side = "LONG" if fill_side == "BUY" else "SHORT"
                queue.append({
                    "side": new_side,
                    "qty": qty,
                    "notional": quote_qty,
                    "fee_usdt": fill_fee_usdt,
                    "non_usdt_fees": dict(fill_non_usdt),
                    "time": ts_ms,
                })

    grouped_orders: Dict[Tuple[str, str, str], List[dict]] = {}
    for s in closed_slices:
        grouped_orders.setdefault((s["symbol"], s["side"], s["order_id"]), []).append(s)

    local_index = _build_local_trade_index(local_history)
    used_local_indices: set[int] = set()
    reconstructed: List[dict] = []

    for idx, ((sym, side, order_id), slices) in enumerate(grouped_orders.items()):
        amount = sum(s["qty"] for s in slices)
        entry_notional = sum(s["open_notional"] for s in slices)
        exit_notional = sum(s["close_notional"] for s in slices)
        entry_price = (entry_notional / amount) if amount > 0 else 0.0
        exit_price = (exit_notional / amount) if amount > 0 else 0.0
        realized_pnl = round(sum(s["realized_pnl"] for s in slices), 4)
        fee = round(sum(s["open_fee_usdt"] + s["close_fee_usdt"] for s in slices), 4)
        net_pnl = round(realized_pnl - fee, 4)
        pnl_pct = round(realized_pnl / entry_notional * 100.0, 2) if entry_notional > 0 else None

        non_usdt: Dict[str, float] = {}
        for s in slices:
            non_usdt = _merge_fee_dicts(non_usdt, s["non_usdt_fees"])
        non_usdt = {k: round(v, 6) for k, v in non_usdt.items() if v > 0}

        closed_ts = max(s["close_ts"] for s in slices)
        all_open_known = all(s["open_ts"] is not None for s in slices)
        if all_open_known:
            entry_ts: Optional[int] = min(int(s["open_ts"]) for s in slices)
            duration_seconds = max(0, (closed_ts - entry_ts) // 1000)
            duration_str = format_duration(duration_seconds)
            entry_time = get_beijing_time_str(entry_ts / 1000.0)
        else:
            entry_ts = None
            duration_seconds = 0
            duration_str = "--"
            entry_time = "--"

        matched_local = _pop_indexed_local_trade(
            sym, closed_ts / 1000.0, local_index, used_local_indices, side=side
        )
        trendline_id, trendline_label, trendline_info = _extract_trendline_meta(
            matched_local.get("trendline") if matched_local else None
        )
        if matched_local:
            close_reason, close_reason_code = _resolve_close_reason(matched_local, exit_price, entry_price, side)
            raw_lev = matched_local.get("leverage")
            leverage = int(raw_lev) if raw_lev is not None and int(raw_lev) > 0 else None
            if pnl_pct is not None and leverage is not None:
                roe_pct: Optional[float] = round(pnl_pct * leverage, 2)
            elif matched_local.get("roe_pct") is not None:
                roe_pct = round(float(matched_local["roe_pct"]), 2)
            else:
                roe_pct = None
            raw_highest = matched_local.get("highest_price")
            highest_price = float(raw_highest) if raw_highest is not None else (exit_price if exit_price > 0 else 0.0)
        else:
            close_reason, close_reason_code = "币安实盘成交", "BINANCE_REALTIME"
            leverage = None
            roe_pct = None
            highest_price = 0.0

        reconstructed.append({
            "trade_id": f"ORD-{idx + 1:03d}-{sym}-{order_id}",
            "symbol": sym,
            "side": side,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "amount": amount,
            "entry_notional": round(entry_notional, 4),
            "exit_notional": round(exit_notional, 4),
            "realized_pnl": realized_pnl,
            "fee": fee,
            "fee_asset": "USDT" if fee > 0 or not non_usdt else next(iter(non_usdt)),
            "non_usdt_fees": non_usdt,
            "net_pnl": net_pnl,
            "pnl_pct": pnl_pct,
            "roe_pct": roe_pct,
            "leverage": leverage,
            "entry_time": entry_time,
            "closed_at": get_beijing_time_str(closed_ts / 1000.0),
            "entry_ts": entry_ts,
            "closed_ts": closed_ts,
            "duration_seconds": duration_seconds,
            "duration_str": duration_str,
            "outcome": classify_outcome(net_pnl),
            "status": "BINANCE_REALTIME_TRADE",
            "close_reason": close_reason,
            "close_reason_code": close_reason_code,
            "highest_price": highest_price,
            "trailing_stop_triggered": bool(matched_local.get("trailing_stop_triggered", False)) if matched_local else False,
            "trailing_stop_price": matched_local.get("trailing_stop_price") if matched_local else None,
            "trendline_id": trendline_id,
            "trendline_label": trendline_label,
            "trendline_info": trendline_info,
            "source": "BINANCE_REALTIME_VERIFIED" if matched_local else "BINANCE_REALTIME",
            "notes": (matched_local.get("notes") or "") if matched_local else "币安成交",
        })

    unmatched_realized = [
        item
        for item in income_list
        if item.get("incomeType") == "REALIZED_PNL"
        and clean_symbol(str(item.get("symbol") or ""))
        and income_asset(item) == "USDT"
        and (
            not item.get("tradeId")
            or (clean_symbol(str(item.get("symbol") or "")), str(item.get("tradeId"))) not in matched_trade_keys
        )
    ]

    by_symbol: Dict[str, List[Mapping[str, Any]]] = {}
    for item in unmatched_realized:
        by_symbol.setdefault(clean_symbol(str(item.get("symbol") or "")), []).append(item)

    income_clusters: List[dict] = []
    for sym in sorted(by_symbol):
        events = sorted(by_symbol[sym], key=lambda x: (int(x.get("time") or 0), str(x.get("tranId") or "")))
        curr: Optional[dict] = None
        for item in events:
            t_ms = int(item.get("time") or 0)
            val = float(item.get("income") or 0.0)
            tid = str(item.get("tradeId") or "")
            if curr is not None and (t_ms - curr["end_t"]) <= 2000:
                curr["end_t"] = t_ms
                curr["realized_pnl"] += val
                if tid:
                    curr["trade_ids"].add(tid)
            else:
                if curr is not None:
                    income_clusters.append(curr)
                curr = {
                    "symbol": sym,
                    "start_t": t_ms,
                    "end_t": t_ms,
                    "realized_pnl": val,
                    "trade_ids": {tid} if tid else set(),
                }
        if curr is not None:
            income_clusters.append(curr)

    for idx, c in enumerate(sorted(income_clusters, key=lambda x: x["end_t"])):
        sym = c["symbol"]
        closed_ts = c["end_t"]
        realized_pnl = round(c["realized_pnl"], 4)
        fee = 0.0
        non_usdt: Dict[str, float] = {}
        for tid in c["trade_ids"]:
            t_key = (sym, tid)
            if t_key not in consumed_comm_keys:
                fee += commissions_usdt.get(t_key, 0.0)
                non_usdt = _merge_fee_dicts(non_usdt, non_usdt_comm.get(t_key, {}))
                consumed_comm_keys.add(t_key)
        fee = round(fee, 4)
        non_usdt = {k: round(v, 6) for k, v in non_usdt.items() if v > 0}
        net_pnl = round(realized_pnl - fee, 4)

        matched_local = _pop_indexed_local_trade(sym, closed_ts / 1000.0, local_index, used_local_indices)
        pnl_consistent = False
        if matched_local:
            enriched = enrich_trade_record(
                matched_local,
                len(reconstructed) + idx,
                fee_rate=fee_rate,
                default_leverage=default_leverage,
            )
            local_realized = enriched["realized_pnl"]
            pnl_consistent = (
                local_realized is not None
                and abs(local_realized - realized_pnl) <= max(0.05, abs(realized_pnl) * 0.05)
            )
            if pnl_consistent:
                enriched["realized_pnl"] = realized_pnl
                enriched["fee"] = fee
                enriched["non_usdt_fees"] = non_usdt
                enriched["net_pnl"] = net_pnl
                enriched["outcome"] = classify_outcome(net_pnl)
                if enriched["entry_notional"] > 0:
                    enriched["pnl_pct"] = round(realized_pnl / enriched["entry_notional"] * 100.0, 2)
                    enriched["roe_pct"] = (
                        round(enriched["pnl_pct"] * enriched["leverage"], 2) if enriched.get("leverage") else None
                    )
                else:
                    enriched["pnl_pct"] = None
                    enriched["roe_pct"] = None
                enriched["source"] = "BINANCE_REALTIME_VERIFIED"
                reconstructed.append(enriched)
        if not matched_local or not pnl_consistent:
            reconstructed.append({
                "trade_id": f"INC-{len(reconstructed) + 1:03d}-{sym}-{closed_ts}",
                "symbol": sym,
                "side": "UNKNOWN",
                "entry_price": 0.0,
                "exit_price": 0.0,
                "amount": 0.0,
                "entry_notional": 0.0,
                "exit_notional": 0.0,
                "realized_pnl": realized_pnl,
                "fee": fee,
                "fee_asset": "USDT" if fee > 0 or not non_usdt else next(iter(non_usdt)),
                "non_usdt_fees": non_usdt,
                "net_pnl": net_pnl,
                "pnl_pct": None,
                "roe_pct": None,
                "leverage": None,
                "entry_time": "--",
                "closed_at": get_beijing_time_str(closed_ts / 1000.0),
                "entry_ts": None,
                "closed_ts": closed_ts,
                "duration_seconds": 0,
                "duration_str": "--",
                "outcome": classify_outcome(net_pnl),
                "status": "BINANCE_REALTIME_TRADE",
                "close_reason": "币安实盘平仓",
                "close_reason_code": "BINANCE_REALTIME",
                "highest_price": 0.0,
                "trailing_stop_triggered": False,
                "trailing_stop_price": None,
                "trendline_id": None,
                "trendline_label": "--",
                "trendline_info": None,
                "source": "BINANCE_REALTIME",
                "notes": "币安已实现盈亏流水",
            })

    reconstructed.sort(key=lambda t: t["closed_ts"] or 0)
    open_position_fee = round(
        sum(lot["fee_usdt"] for queue in open_lots.values() for lot in queue), 4
    )
    return reconstructed, open_position_fee
