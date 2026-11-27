#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Binance FIFO Fill & Income-Cluster Trade Reconstruction
=======================================================
Pure reconstruction state machine with zero network or DB I/O:
1. Unified (symbol, positionSide) FIFO lot state machine using `collections.deque`.
2. Symbol-indexed bisect matching against local position history.
3. Income-cluster fallback reconstruction for trades without `/fapi/v1/userTrades` fills.
"""

from __future__ import annotations

from bisect import bisect_left
from collections import deque
from decimal import Decimal
import math
from pathlib import Path
import sys
from typing import Any, Deque, Dict, List, Mapping, Optional, Sequence, Tuple

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from config import clean_symbol, get_beijing_time_str  # noqa: E402
from trade_enrichment import (  # noqa: E402
    _extract_trendline_meta,
    _resolve_close_reason,
    build_trade_record,
    classify_outcome,
    enrich_trade_record,
    format_duration,
    parse_to_timestamp,
)

_LOCAL_MATCH_WINDOW_SEC = 300.0


def income_asset(item: Mapping[str, Any]) -> str:
    """Return normalized uppercase asset name for a Binance income record."""
    asset = item["asset"]
    if asset is None or not str(asset).strip():
        raise ValueError(f"Empty or null asset in income record: {item!r}")
    return str(asset).strip().upper()


def _merge_fee_dicts(a: Mapping[str, float], b: Mapping[str, float]) -> Dict[str, float]:
    out = dict(a)
    for k, v in b.items():
        out[k] = out.get(k, 0.0) + v
    return out


def _append_open_lot(
    queue: Deque[dict],
    side: str,
    qty: Decimal,
    notional: float,
    fee_usdt: float,
    non_usdt_fees: Mapping[str, float],
    ts_ms: int,
) -> None:
    queue.append({
        "side": side,
        "qty": qty,
        "notional": notional,
        "fee_usdt": fee_usdt,
        "non_usdt_fees": dict(non_usdt_fees),
        "time": ts_ms,
    })


def _infer_local_trade_side(loc: Mapping[str, Any]) -> Optional[str]:
    raw_side = str(loc.get("side") or "").strip().upper()
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
        sym = clean_symbol(str(loc["symbol"]))
        if not sym:
            raise ValueError(f"Local position_history record missing valid symbol: {loc!r}")
        loc_closed_at = loc.get("closed_at")
        if loc_closed_at is None or str(loc_closed_at).strip() == "--":
            continue
        loc_ts = parse_to_timestamp(loc_closed_at)
        if loc_ts is None:
            raise ValueError(f"Unparseable closed_at in local_history for {sym}: {loc_closed_at!r}")
        loc_side = _infer_local_trade_side(loc)
        grouped.setdefault(sym, []).append((idx, loc_side, loc_ts, loc))

    indexed: Dict[str, Tuple[List[float], List[Tuple[int, Optional[str], float, Mapping[str, Any]]]]] = {}
    for sym, entries in grouped.items():
        entries.sort(key=lambda entry: (entry[2], entry[0]))
        indexed[sym] = ([entry[2] for entry in entries], entries)
    return indexed


def _find_indexed_local_trade(
    sym: str,
    closed_sec: float,
    indexed: Dict[str, Tuple[List[float], List[Tuple[int, Optional[str], float, Mapping[str, Any]]]]],
    used_indices: set[int],
    side: Optional[str] = None,
) -> Tuple[Optional[int], Optional[Mapping[str, Any]]]:
    bucket = indexed.get(sym)
    if bucket is None:
        return None, None
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

    return best_idx, best_entry


def _precompute_both_zero_close(
    sorted_fills: Sequence[Mapping[str, Any]],
    realized_by_trade: Mapping[Tuple[str, str], float],
) -> set[int]:
    """O(N) 预计算单向模式 (BOTH) 空队列/越界 0 盈亏盘前平仓下标集合。"""
    both_indices_by_sym: Dict[str, List[int]] = {}
    for idx, f in enumerate(sorted_fills):
        if str(f.get("positionSide") or "BOTH").upper() == "BOTH":
            sym = clean_symbol(str(f["symbol"]))
            both_indices_by_sym.setdefault(sym, []).append(idx)

    zero_close_indices: set[int] = set()
    for sym, indices in both_indices_by_sym.items():
        pos_side: Optional[str] = None
        pos_qty = Decimal("0")
        pos_notional = 0.0
        tentative_indices: List[int] = []
        confirmed_open = False

        for idx in indices:
            g = sorted_fills[idx]
            g_side = str(g["side"]).upper()
            fill_dir_side = "LONG" if g_side == "BUY" else "SHORT"
            qty = Decimal(str(g["qty"]))
            price = float(g["price"])
            quote_qty = float(g["quoteQty"]) if g.get("quoteQty") is not None else (price * float(qty))
            g_key = (sym, str(g["id"]))
            g_income_rpnl = realized_by_trade.get(g_key)
            g_raw_rpnl = g.get("realizedPnl")
            g_parsed_rpnl = float(g_raw_rpnl) if g_raw_rpnl is not None else None
            g_has_nonzero_rpnl = g_parsed_rpnl is not None and abs(g_parsed_rpnl) > 1e-12
            g_fill_realized = (
                g_income_rpnl if g_income_rpnl is not None else (g_parsed_rpnl if g_has_nonzero_rpnl else None)
            )
            g_reduce_only = bool(g.get("reduceOnly"))
            g_has_close_signal = g_fill_realized is not None or g_reduce_only
            g_explicit_zero_rpnl = (
                g_income_rpnl is None
                and g_parsed_rpnl is not None
                and abs(g_parsed_rpnl) <= 1e-12
                and not g_reduce_only
            )

            if pos_qty == 0:
                if not g_has_close_signal:
                    pos_side = fill_dir_side
                    pos_qty = qty
                    pos_notional = quote_qty
                    tentative_indices = [idx] if g_explicit_zero_rpnl else []
                    confirmed_open = False
                continue

            assert pos_side is not None
            if fill_dir_side == pos_side:
                if g_has_close_signal:
                    # Same-side fill with realized PnL / reduceOnly proves prior tentative fills closed pre-recording inventory.
                    zero_close_indices.update(tentative_indices)
                    pos_side = None
                    pos_qty = Decimal("0")
                    pos_notional = 0.0
                    tentative_indices = []
                    confirmed_open = False
                else:
                    pos_qty += qty
                    pos_notional += quote_qty
                    if not confirmed_open and g_explicit_zero_rpnl:
                        tentative_indices.append(idx)
                continue

            avg_entry = pos_notional / float(pos_qty)
            if g_explicit_zero_rpnl:
                price_matches = abs(price - avg_entry) <= max(1e-8, avg_entry * 1e-6)
                if price_matches:
                    if qty < pos_qty:
                        pos_qty -= qty
                        pos_notional = avg_entry * float(pos_qty)
                    elif qty == pos_qty:
                        pos_side = None
                        pos_qty = Decimal("0")
                        pos_notional = 0.0
                        tentative_indices = []
                        confirmed_open = False
                    else:
                        rem_qty = qty - pos_qty
                        pos_side = fill_dir_side
                        pos_qty = rem_qty
                        pos_notional = quote_qty * float(rem_qty / qty)
                        tentative_indices = [idx]
                        confirmed_open = False
                else:
                    # Zero-PnL opposite fill at a different price contradicts tentative open lots.
                    zero_close_indices.update(tentative_indices)
                    pos_side = fill_dir_side
                    pos_qty = qty
                    pos_notional = quote_qty
                    tentative_indices = [idx]
                    confirmed_open = False
                continue

            if qty <= pos_qty or g_reduce_only:
                if qty < pos_qty:
                    pos_qty -= qty
                    pos_notional = avg_entry * float(pos_qty)
                else:
                    pos_side = None
                    pos_qty = Decimal("0")
                    pos_notional = 0.0
                    tentative_indices = []
                    confirmed_open = False
            else:
                direction = 1.0 if pos_side == "LONG" else -1.0
                known_ratio = float(pos_qty / qty)
                known_close_notional = quote_qty * known_ratio
                expected_known_rpnl = (known_close_notional - pos_notional) * direction
                tol = max(1e-3, abs(expected_known_rpnl) * 0.02, known_close_notional * 1e-4)
                is_pre_recording_close = g_fill_realized is not None and abs(g_fill_realized - expected_known_rpnl) > tol
                if is_pre_recording_close:
                    pos_side = None
                    pos_qty = Decimal("0")
                    pos_notional = 0.0
                    tentative_indices = []
                    confirmed_open = False
                else:
                    rem_qty = qty - pos_qty
                    pos_side = fill_dir_side
                    pos_qty = rem_qty
                    pos_notional = quote_qty * float(rem_qty / qty)
                    tentative_indices = []
                    confirmed_open = True

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
    default_leverage: Optional[int] = None,
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
        itype = item["incomeType"]
        tid = str(item.get("tradeId") or "")
        asset = income_asset(item)
        amount = float(item["income"])
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
        if not sym:
            raise ValueError(f"Fill record missing valid symbol: {f!r}")
        tid = str(f["id"])
        order_id = str(f["orderId"])
        fill_side = str(f["side"]).upper()
        if fill_side not in ("BUY", "SELL"):
            raise ValueError(f"Fill record for {sym} has invalid side: {f['side']!r}")
        raw_pos_side = str(f.get("positionSide") or "BOTH").upper()
        ts_ms = int(f["time"])
        qty = Decimal(str(f["qty"]))
        price = float(f["price"])
        if not math.isfinite(price) or price <= 0:
            raise ValueError(f"Fill record for {sym} has invalid price: {f['price']!r}")
        quote_qty = float(f["quoteQty"]) if f.get("quoteQty") is not None else (price * float(qty))
        if qty <= 0:
            raise ValueError(f"Fill record for {sym} has non-positive qty: {f['qty']!r}")

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
                _append_open_lot(queue, raw_pos_side, qty, quote_qty, fill_fee_usdt, fill_non_usdt, ts_ms)
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
                    _append_open_lot(queue, active_side, qty, quote_qty, fill_fee_usdt, fill_non_usdt, ts_ms)
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
                        _append_open_lot(
                            queue,
                            new_side,
                            open_rem,
                            quote_qty * open_ratio,
                            fill_fee_usdt * open_ratio,
                            {a: v * open_ratio for a, v in fill_non_usdt.items()},
                            ts_ms,
                        )
            elif (
                fill_realized is not None
                or is_reduce_only
                or fill_idx in both_zero_close_indices
            ):
                if fill_realized is not None:
                    effective_realized = fill_realized
                elif raw_fill_rpnl is not None:
                    effective_realized = float(raw_fill_rpnl)
                else:
                    raise ValueError(
                        f"Closing fill missing realizedPnl for pre-recording position ({sym}): {f!r}"
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
                _append_open_lot(queue, new_side, qty, quote_qty, fill_fee_usdt, fill_non_usdt, ts_ms)

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
        if all(s["open_ts"] is not None for s in slices):
            entry_ts: Optional[int] = min(int(s["open_ts"]) for s in slices)
            duration_seconds = max(0, (closed_ts - entry_ts) // 1000)
            duration_str = format_duration(duration_seconds)
            entry_time = get_beijing_time_str(entry_ts / 1000.0)
        else:
            entry_ts = None
            duration_seconds = 0
            duration_str = "--"
            entry_time = "--"

        matched_idx, matched_local = _find_indexed_local_trade(
            sym, closed_ts / 1000.0, local_index, used_local_indices, side=side
        )
        if matched_idx is not None:
            used_local_indices.add(matched_idx)
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
            highest_price: Optional[float] = (
                float(raw_highest)
                if raw_highest is not None
                else (exit_price if exit_price > 0 else None)
            )
        else:
            close_reason, close_reason_code = "币安实盘成交", "BINANCE_REALTIME"
            leverage = None
            roe_pct = None
            highest_price = None

        reconstructed.append(
            build_trade_record(
                trade_id=f"ORD-{idx + 1:03d}-{sym}-{order_id}",
                symbol=sym,
                side=side,
                entry_price=entry_price,
                exit_price=exit_price,
                amount=amount,
                entry_notional=round(entry_notional, 4),
                exit_notional=round(exit_notional, 4),
                realized_pnl=realized_pnl,
                fee=fee,
                non_usdt_fees=non_usdt,
                net_pnl=net_pnl,
                pnl_pct=pnl_pct,
                roe_pct=roe_pct,
                leverage=leverage,
                entry_time=entry_time,
                closed_at=get_beijing_time_str(closed_ts / 1000.0),
                entry_ts=entry_ts,
                closed_ts=closed_ts,
                duration_seconds=duration_seconds,
                duration_str=duration_str,
                status="BINANCE_REALTIME_TRADE",
                close_reason=close_reason,
                close_reason_code=close_reason_code,
                highest_price=highest_price,
                trailing_stop_triggered=(
                    bool(matched_local.get("trailing_stop_triggered", False)) if matched_local else False
                ),
                trailing_stop_price=matched_local.get("trailing_stop_price") if matched_local else None,
                trendline_id=trendline_id,
                trendline_label=trendline_label,
                trendline_info=trendline_info,
                source="BINANCE_REALTIME_VERIFIED" if matched_local else "BINANCE_REALTIME",
                notes=(matched_local.get("notes") or "") if matched_local else "币安成交",
            )
        )

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
        events = sorted(by_symbol[sym], key=lambda x: (int(x["time"]), str(x.get("tranId") or "")))
        curr: Optional[dict] = None
        for item in events:
            t_ms = int(item["time"])
            val = float(item["income"])
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

        matched_idx, matched_local = _find_indexed_local_trade(
            sym, closed_ts / 1000.0, local_index, used_local_indices
        )
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
                assert matched_idx is not None
                used_local_indices.add(matched_idx)
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
            reconstructed.append(
                build_trade_record(
                    trade_id=f"INC-{len(reconstructed) + 1:03d}-{sym}-{closed_ts}",
                    symbol=sym,
                    side="UNKNOWN",
                    entry_price=None,
                    exit_price=None,
                    amount=None,
                    entry_notional=None,
                    exit_notional=None,
                    realized_pnl=realized_pnl,
                    fee=fee,
                    non_usdt_fees=non_usdt,
                    net_pnl=net_pnl,
                    pnl_pct=None,
                    roe_pct=None,
                    leverage=None,
                    entry_time="--",
                    closed_at=get_beijing_time_str(closed_ts / 1000.0),
                    entry_ts=None,
                    closed_ts=closed_ts,
                    duration_seconds=0,
                    duration_str="--",
                    status="BINANCE_REALTIME_TRADE",
                    close_reason="币安实盘平仓",
                    close_reason_code="BINANCE_REALTIME",
                    source="BINANCE_REALTIME",
                    notes="币安已实现盈亏流水",
                )
            )

    reconstructed.sort(key=lambda t: t["closed_ts"] or 0)
    open_position_fee = round(
        sum(lot["fee_usdt"] for queue in open_lots.values() for lot in queue), 4
    )
    return reconstructed, open_position_fee
