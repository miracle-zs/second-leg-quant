#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Trade & Position Metadata Enrichment and Time/Duration Formatters
=================================================================
Pure normalization helpers with zero network or DB I/O:
1. Timestamp parsing and duration formatting (Beijing UTC+8).
2. Canonical 34-field closed-trade record builder shared across manual and Binance paths.
3. Local/Binance closed-trade and active-position enrichment and merging.
"""

from __future__ import annotations

from datetime import datetime
import logging
import math
from pathlib import Path
import sys
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from config import BEIJING_TZ, clean_symbol, get_beijing_time_str  # noqa: E402

logger = logging.getLogger("account_analytics")


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


def _has_trendline_id(trendline_id: object) -> bool:
    if trendline_id is None or isinstance(trendline_id, bool):
        return False
    return str(trendline_id).strip() != ""


def _extract_trendline_meta(trendline: object) -> Tuple[Optional[Any], str, Optional[dict]]:
    if trendline is None:
        return None, "--", None
    if not isinstance(trendline, Mapping):
        raise ValueError(f"Expected mapping for trendline, got {type(trendline).__name__}")
    trendline_id = trendline.get("id")
    p1_raw = trendline.get("p1")
    p2_raw = trendline.get("p2")
    p1 = float(p1_raw) if p1_raw is not None else None
    p2 = float(p2_raw) if p2_raw is not None else None
    color = trendline.get("line_color") or "#2196f3"
    info = {
        "id": trendline_id,
        "p1": p1,
        "p2": p2,
        "dt1_str": trendline.get("dt1_str") or "",
        "dt2_str": trendline.get("dt2_str") or "",
        "color": color,
        "interval": trendline.get("interval", "15"),
    }
    if _has_trendline_id(trendline_id) and p1 is not None and p2 is not None:
        label = f"TL#{trendline_id} ({p1:.4f} -> {p2:.4f})"
    elif _has_trendline_id(trendline_id):
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
    is_non_loss_exit = entry_price > 0 and exit_price > 0 and (exit_price - entry_price) * direction >= 0

    if status == "PROFIT_LOCKED" or "TRAILING" in status:
        return "动态移动保本锁利", "TRAILING_PROFIT_LOCK"
    if status == "STOP_LOSS_TRIGGERED":
        if is_non_loss_exit:
            return "动态移动保本锁利", "TRAILING_PROFIT_LOCK"
        return "跌破趋势线止损", "TRENDLINE_BREAK_STOP"
    if status == "BREAK_TRENDLINE_STOP":
        return "跌破趋势线止损", "TRENDLINE_BREAK_STOP"
    if status == "MANUAL_CLOSE":
        return "手动平仓", "MANUAL_CLOSE"
    if status == "TAKE_PROFIT":
        return "止盈离场", "TAKE_PROFIT"
    if trailing_triggered and is_non_loss_exit:
        return "动态移动保本锁利", "TRAILING_PROFIT_LOCK"
    if status == "BINANCE_POSITION_CLOSED":
        return "币安实盘平仓", "BINANCE_AUTO_CLOSED"
    return str(raw.get("notes") or status or "平仓归档"), "OTHER_CLOSE"


def build_trade_record(
    *,
    trade_id: str,
    symbol: str,
    side: str,
    entry_price: Optional[float],
    exit_price: Optional[float],
    amount: Optional[float],
    entry_notional: Optional[float],
    exit_notional: Optional[float],
    realized_pnl: Optional[float],
    fee: Optional[float],
    net_pnl: Optional[float],
    pnl_pct: Optional[float],
    roe_pct: Optional[float],
    leverage: Optional[int],
    entry_time: str,
    closed_at: str,
    entry_ts: Optional[int],
    closed_ts: Optional[int],
    duration_seconds: int,
    duration_str: str,
    status: str,
    close_reason: str,
    close_reason_code: str,
    source: str,
    notes: str = "",
    non_usdt_fees: Optional[Dict[str, float]] = None,
    highest_price: Optional[float] = None,
    trailing_stop_triggered: bool = False,
    trailing_stop_price: Optional[Any] = None,
    trendline_id: Optional[Any] = None,
    trendline_label: str = "--",
    trendline_info: Optional[dict] = None,
) -> dict:
    """Build a canonical closed-trade dictionary shared by enrichment and Binance reconstruction."""
    non_usdt = dict(non_usdt_fees) if non_usdt_fees else {}
    fee_asset = "USDT" if (fee is None or fee > 0 or not non_usdt) else next(iter(non_usdt))
    outcome = classify_outcome(net_pnl) if net_pnl is not None else "UNKNOWN"
    return {
        "trade_id": trade_id,
        "symbol": symbol,
        "side": side,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "amount": amount,
        "entry_notional": entry_notional,
        "exit_notional": exit_notional,
        "realized_pnl": realized_pnl,
        "fee": fee,
        "fee_asset": fee_asset,
        "non_usdt_fees": non_usdt,
        "net_pnl": net_pnl,
        "pnl_pct": pnl_pct,
        "roe_pct": roe_pct,
        "leverage": leverage,
        "entry_time": entry_time,
        "closed_at": closed_at,
        "entry_ts": entry_ts,
        "closed_ts": closed_ts,
        "duration_seconds": duration_seconds,
        "duration_str": duration_str,
        "outcome": outcome,
        "status": status,
        "close_reason": close_reason,
        "close_reason_code": close_reason_code,
        "highest_price": highest_price,
        "trailing_stop_triggered": trailing_stop_triggered,
        "trailing_stop_price": trailing_stop_price,
        "trendline_id": trendline_id,
        "trendline_label": trendline_label,
        "trendline_info": trendline_info,
        "source": source,
        "notes": notes,
    }


def enrich_trade_record(
    raw: Mapping[str, Any],
    index: int,
    *,
    fee_rate: float = 0.0005,
    default_leverage: Optional[int] = None,
) -> dict:
    """标准化本地历史平仓交易记录，严格区分 LONG/SHORT 方向，不伪造平仓价/时间戳/杠杆/负数手续费。"""
    sym = clean_symbol(str(raw["symbol"]))
    if not sym:
        raise ValueError(f"Trade record missing valid symbol: {raw!r}")
    entry_price = float(raw["entry_price"])
    if not math.isfinite(entry_price) or entry_price <= 0:
        raise ValueError(f"Trade record for {sym} has invalid entry_price: {raw['entry_price']!r}")
    raw_amount = float(raw["amount"])
    if not math.isfinite(raw_amount):
        raise ValueError(f"Trade record for {sym} has invalid amount: {raw['amount']!r}")
    raw_side = str(raw.get("side") or "").strip().upper()
    side = raw_side if raw_side in ("LONG", "SHORT") else ("SHORT" if raw_amount < 0 else "LONG")
    amount = abs(raw_amount)
    direction = 1.0 if side == "LONG" else -1.0
    raw_lev = raw.get("leverage")
    if raw_lev is not None:
        leverage: Optional[int] = int(raw_lev)
        if leverage <= 0:
            raise ValueError(f"Trade record for {sym} has invalid leverage: {raw_lev!r}")
    elif default_leverage is not None:
        leverage = int(default_leverage)
    else:
        leverage = None

    raw_exit = raw.get("exit_price")
    exit_price: Optional[float] = float(raw_exit) if raw_exit is not None and float(raw_exit) > 0 else None

    entry_time_raw = raw.get("entry_time")
    closed_at_raw = raw.get("closed_at")
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

    if exit_price is not None and entry_price > 0 and amount > 0:
        realized_pnl = round((exit_price - entry_price) * amount * direction, 4)
        pnl_pct = round(((exit_price - entry_price) * direction / entry_price) * 100.0, 2)
    elif raw.get("realized_pnl") is not None:
        realized_pnl = round(float(raw["realized_pnl"]), 4)
        pnl_pct = round(realized_pnl / entry_notional * 100.0, 2) if entry_notional > 0 else None
    else:
        realized_pnl = None
        pnl_pct = None

    roe_pct = round(pnl_pct * leverage, 2) if (pnl_pct is not None and leverage is not None) else None

    if raw.get("fee") is not None:
        fee = round(float(raw["fee"]), 4)
    elif realized_pnl is not None and (entry_notional > 0 or exit_notional > 0):
        fee = round((entry_notional + exit_notional) * fee_rate, 4)
    elif realized_pnl is not None:
        fee = 0.0
    else:
        fee = None

    net_pnl = round(realized_pnl - (fee or 0.0), 4) if realized_pnl is not None else None
    close_reason, close_reason_code = _resolve_close_reason(raw, exit_price or 0.0, entry_price, side)
    trendline_id, trendline_label, trendline_info = _extract_trendline_meta(raw.get("trendline"))

    ts_suffix = int(closed_ts) if closed_ts is not None else index + 1
    return build_trade_record(
        trade_id=f"TRD-{index + 1:03d}-{sym}-{ts_suffix}",
        symbol=sym,
        side=side,
        entry_price=entry_price,
        exit_price=exit_price,
        amount=amount,
        entry_notional=entry_notional,
        exit_notional=exit_notional,
        realized_pnl=realized_pnl,
        fee=fee,
        net_pnl=net_pnl,
        pnl_pct=pnl_pct,
        roe_pct=roe_pct,
        leverage=leverage,
        entry_time=entry_time_str,
        closed_at=closed_at_str,
        entry_ts=int(entry_ts * 1000) if entry_ts is not None else None,
        closed_ts=int(closed_ts * 1000) if closed_ts is not None else None,
        duration_seconds=duration_seconds,
        duration_str=duration_str,
        status=str(raw.get("status") or "").upper(),
        close_reason=close_reason,
        close_reason_code=close_reason_code,
        highest_price=float(raw["highest_price"]) if raw.get("highest_price") is not None else exit_price,
        trailing_stop_triggered=bool(raw.get("trailing_stop_triggered", False)),
        trailing_stop_price=raw.get("trailing_stop_price"),
        trendline_id=trendline_id,
        trendline_label=trendline_label,
        trendline_info=trendline_info,
        source=raw.get("source", "MANUAL"),
        notes=raw.get("notes", ""),
    )


def enrich_active_position(
    raw: Mapping[str, Any],
    index: int,
    *,
    default_leverage: Optional[int] = None,
    now_ts: Optional[float] = None,
) -> dict:
    """标准化当前活跃持仓（基于 get_active_positions / 本地 open_positions 的 snake_case 字段）。"""
    sym = clean_symbol(str(raw["symbol"]))
    if not sym:
        raise ValueError(f"Active position missing valid symbol: {raw!r}")
    raw_entry = raw.get("entry_price")
    if raw_entry is None:
        raise ValueError(f"Active position {sym} missing entry_price: {raw!r}")
    entry_price = float(raw_entry)
    if not math.isfinite(entry_price) or entry_price <= 0:
        raise ValueError(f"Active position {sym} has invalid entry_price: {raw_entry!r}")

    raw_amt_val = raw.get("amount")
    if raw_amt_val is None:
        raise ValueError(f"Active position {sym} missing amount: {raw!r}")
    raw_amt = float(raw_amt_val)
    if not math.isfinite(raw_amt):
        raise ValueError(f"Active position {sym} has invalid amount: {raw_amt_val!r}")
    raw_side = str(raw.get("side") or "").strip().upper()
    side = raw_side if raw_side in ("LONG", "SHORT") else ("SHORT" if raw_amt < 0 else "LONG")
    amount = abs(raw_amt)
    direction = -1.0 if side == "SHORT" else 1.0
    raw_lev = raw.get("leverage")
    if raw_lev is not None:
        leverage: Optional[int] = int(raw_lev)
        if leverage <= 0:
            raise ValueError(f"Active position {sym} has invalid leverage: {raw_lev!r}")
    elif default_leverage is not None:
        leverage = int(default_leverage)
    else:
        leverage = None

    raw_mark = raw.get("mark_price") if raw.get("mark_price") is not None else raw.get("current_price")
    if raw_mark is None:
        raise ValueError(f"Active position {sym} missing mark_price: {raw!r}")
    mark_price = float(raw_mark)
    if not math.isfinite(mark_price) or mark_price <= 0:
        raise ValueError(f"Active position {sym} has invalid mark_price: {raw_mark!r}")

    notional = round(mark_price * amount, 4)
    initial_margin: Optional[float] = (
        round(notional / leverage, 4) if (leverage is not None and leverage > 0) else None
    )

    raw_upnl = raw.get("unrealized_pnl")
    unrealized_pnl = float(raw_upnl) if raw_upnl is not None else ((mark_price - entry_price) * amount * direction)
    unrealized_pnl = round(unrealized_pnl, 4)

    pnl_pct = round(((mark_price - entry_price) * direction / entry_price * 100.0), 2)
    roe_pct: Optional[float] = round(pnl_pct * leverage, 2) if leverage is not None else None

    raw_liq = raw.get("liquidation_price")
    if raw_liq is not None:
        if isinstance(raw_liq, bool):
            raise ValueError(f"Active position {sym} has invalid liquidation_price: {raw_liq!r}")
        liq_val = float(raw_liq)
        if not math.isfinite(liq_val) or liq_val < 0:
            raise ValueError(f"Active position {sym} has invalid liquidation_price: {raw_liq!r}")
        liq_price: Optional[float] = round(liq_val, 6) if liq_val > 0 else None
    else:
        liq_price = None

    entry_time_raw = raw.get("entry_time")
    entry_ts = parse_to_timestamp(entry_time_raw)
    if entry_ts is None:
        entry_ts = parse_to_timestamp(raw.get("update_time"))

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
    if isinstance(trendline, Mapping):
        trendline_id, trendline_label, trendline_info = _extract_trendline_meta(trendline)
    else:
        trendline_id, trendline_label, trendline_info = None, "--", None

    raw_tl_id = raw.get("trendline_id")
    if not _has_trendline_id(trendline_id) and _has_trendline_id(raw_tl_id):
        trendline_id = raw_tl_id
        if trendline_info is not None and trendline_info.get("p1") is not None and trendline_info.get("p2") is not None:
            trendline_label = f"TL#{trendline_id} ({trendline_info['p1']:.4f} -> {trendline_info['p2']:.4f})"
        else:
            raw_tl_label = raw.get("trendline_label")
            if isinstance(raw_tl_label, str) and raw_tl_label.strip() and raw_tl_label.strip() != "--":
                trendline_label = raw_tl_label.strip()
            else:
                trendline_label = f"TL#{trendline_id}"
    elif not _has_trendline_id(trendline_id):
        raw_tl_label = raw.get("trendline_label")
        if isinstance(raw_tl_label, str) and raw_tl_label.strip() and raw_tl_label.strip() != "--":
            trendline_label = raw_tl_label.strip()

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
        "liquidation_price": liq_price,
        "entry_time": entry_time_str,
        "entry_ts": entry_ts_ms,
        "holding_seconds": holding_seconds,
        "holding_str": holding_str,
        "highest_price": float(raw["highest_price"]) if raw.get("highest_price") is not None else mark_price,
        "trailing_stop_triggered": bool(raw.get("trailing_stop_triggered", False)),
        "trailing_stop_price": raw.get("trailing_stop_price"),
        "stop_loss_notified": bool(raw.get("stop_loss_notified", False)),
        "trendline_id": trendline_id,
        "trendline_label": trendline_label,
        "trendline": trendline,
        "source": raw.get(
            "source",
            "BINANCE_SYNC" if raw.get("position_side") is not None else "MANUAL",
        ),
        "status": "OPEN",
        "notes": raw.get("notes", ""),
    }


def merge_active_positions(
    active_positions_binance: Sequence[Mapping[str, Any]],
    local_positions: Sequence[Mapping[str, Any]],
    positions_available: bool,
    *,
    default_leverage: Optional[int] = None,
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
        sym = clean_symbol(str(lp["symbol"]))
        if not sym:
            raise ValueError(f"Local open position missing valid symbol: {lp!r}")
        raw_side = str(lp.get("side") or "").upper()
        amt = float(lp["amount"])
        lp_side = raw_side if raw_side in ("LONG", "SHORT") else ("SHORT" if amt < 0 else "LONG")
        local_by_key[(sym, lp_side)] = lp

    merged_raw: List[Mapping[str, Any]] = []
    for bp in active_positions_binance:
        sym = clean_symbol(str(bp["symbol"]))
        if not sym:
            raise ValueError(f"Binance active position missing valid symbol: {bp!r}")
        raw_amt_val = bp.get("signed_amount") if bp.get("signed_amount") is not None else bp.get("amount")
        if raw_amt_val is None:
            raise ValueError(f"Binance active position {sym} missing amount: {bp!r}")
        raw_amt = float(raw_amt_val)
        raw_side = str(bp.get("side") or bp.get("position_side") or "BOTH").upper()
        bp_side = raw_side if raw_side in ("LONG", "SHORT") else ("SHORT" if raw_amt < 0 else "LONG")
        local = local_by_key.get((sym, bp_side))
        combined = dict(bp)
        combined.setdefault("source", "BINANCE_SYNC")
        if local:
            for meta_key in (
                "trendline",
                "trendline_id",
                "trendline_label",
                "trailing_stop_price",
                "trailing_stop_triggered",
                "stop_loss_notified",
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
