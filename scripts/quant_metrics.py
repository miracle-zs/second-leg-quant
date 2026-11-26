#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Quantitative Risk Metrics, Ledger Aggregation & Time-Series Builders
====================================================================
Pure calculation module with zero network or DB I/O:
1. Single-pass `income_list` aggregation for transfers, fees, funding, coverage, and daily buckets.
2. Unified `_compute_sharpe_sortino` with calendar-gap zero-return filling.
3. Equity curve and daily PnL series builders (trade-based and DB-snapshot modes).
4. Summary risk metrics and return distribution binning.
"""

from __future__ import annotations

from datetime import date, timedelta
import math
from pathlib import Path
import sys
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from config import get_beijing_time_str  # noqa: E402
from trade_reconstructor import (  # noqa: E402
    classify_outcome,
    format_duration,
    income_asset,
    parse_to_timestamp,
)

EQUITY_COMPACTION_BUCKET_MS = 300_000  # 5 分钟滚动窗口去重


def _resolve_trade_beijing_date(trade: Mapping[str, Any]) -> Optional[str]:
    """将交易平仓时间解析为东八区 (UTC+8) 日期 YYYY-MM-DD。"""
    c_at = trade.get("closed_at")
    if c_at and str(c_at).strip() != "--":
        ts = parse_to_timestamp(c_at)
        if ts is None:
            raise ValueError(f"Invalid trade closed_at timestamp: {c_at!r}")
        return get_beijing_time_str(ts)[:10]
    raw_closed_ts = trade.get("closed_ts")
    if raw_closed_ts is not None:
        ts = parse_to_timestamp(raw_closed_ts)
        if ts is None:
            raise ValueError(f"Invalid trade closed_ts timestamp: {raw_closed_ts!r}")
        return get_beijing_time_str(ts)[:10]
    return None


def _compute_sharpe_sortino(returns: Sequence[float]) -> Tuple[Optional[float], Optional[float]]:
    """Compute annualized Sharpe and Sortino ratios (365-day basis) over a return series."""
    n = len(returns)
    if n < 2:
        return None, None
    mean_r = sum(returns) / n
    var_r = sum((r - mean_r) ** 2 for r in returns) / (n - 1)
    std_r = math.sqrt(var_r) if var_r > 0 else 0.0
    sharpe = round((mean_r / std_r) * math.sqrt(365), 2) if std_r > 1e-9 else None

    downside_sq = sum(min(0.0, r) ** 2 for r in returns)
    downside_std = math.sqrt(downside_sq / n)
    sortino = round((mean_r / downside_std) * math.sqrt(365), 2) if downside_std > 1e-9 else None
    return sharpe, sortino


def compute_risk_ratios(
    daily_pnl: Sequence[Mapping[str, Any]],
    initial_capital: float,
) -> Tuple[Optional[float], Optional[float], str]:
    """
    统一计算年化夏普比率与索提诺比率:
    - 补齐起止日期间的空仓日 (0 收益)，避免只按交易日虚增年化均值/压低波动;
    - 索提诺分母始终除以总观测期数 N (标准下行半方差)，绝不除以亏损日数;
    - 样本不足 2 个自然日或方差不足时返回 None。
    """
    if len(daily_pnl) < 2:
        return None, None, "NONE"

    by_date: Dict[str, Mapping[str, Any]] = {}
    for d in daily_pnl:
        raw_date = d["date"]
        if not isinstance(raw_date, str) or not raw_date.strip():
            raise ValueError(f"Invalid daily_pnl date entry: {d!r}")
        by_date[raw_date] = d
    sorted_dates = sorted(by_date.keys())
    if len(sorted_dates) < 2:
        return None, None, "NONE"

    start_d = date.fromisoformat(sorted_dates[0])
    end_d = date.fromisoformat(sorted_dates[-1])
    calendar_returns: List[float] = []
    running_eq = initial_capital
    cur = start_d
    while cur <= end_d:
        d_str = cur.isoformat()
        entry = by_date.get(d_str)
        if entry is not None:
            day_net = float(entry["net_pnl"])
            raw_transfer = entry.get("net_transfer")
            day_transfer = float(raw_transfer) if raw_transfer is not None else 0.0
            raw_base = entry.get("capital_base")
            base_eq = float(raw_base) if raw_base is not None else (running_eq + max(0.0, day_transfer))
            ret = (day_net / base_eq) if base_eq > 0 else 0.0
            calendar_returns.append(ret)
            running_eq = (
                (base_eq + day_net + min(0.0, day_transfer))
                if raw_base is not None
                else (running_eq + day_net + day_transfer)
            )
        else:
            calendar_returns.append(0.0)
        cur += timedelta(days=1)

    sharpe, sortino = _compute_sharpe_sortino(calendar_returns)
    return sharpe, sortino, "DAILY_RETURNS"


def aggregate_income_ledger(
    income_list: Sequence[Mapping[str, Any]],
    *,
    start_ms: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Aggregate Binance income events in a single pass for transfers, commissions,
    non-USDT fees, funding fees, coverage window, and per-day Beijing buckets.
    """
    transfers: List[Tuple[int, float]] = []
    net_transfer_usdt = 0.0
    funding_fee_usdt = 0.0
    ledger_fee_usdt = 0.0
    non_usdt_fees: Dict[str, float] = {}
    min_ts_ms: Optional[int] = None
    max_ts_ms: Optional[int] = None
    daily_income: Dict[str, Dict[str, float]] = {}
    transfers_by_date: Dict[str, float] = {}

    for item in income_list:
        raw_time = item["time"]
        if isinstance(raw_time, bool) or raw_time is None:
            raise ValueError(f"Invalid income time in record: {item!r}")
        ts_ms = int(raw_time)
        min_ts_ms = ts_ms if min_ts_ms is None else min(min_ts_ms, ts_ms)
        max_ts_ms = ts_ms if max_ts_ms is None else max(max_ts_ms, ts_ms)

        itype = item["incomeType"]
        if not isinstance(itype, str) or not itype:
            raise ValueError(f"Invalid incomeType in record: {item!r}")
        asset = income_asset(item)
        raw_income = item["income"]
        if isinstance(raw_income, bool) or raw_income is None:
            raise ValueError(f"Invalid income amount in record: {item!r}")
        amount = float(raw_income)
        if not math.isfinite(amount):
            raise ValueError(f"Non-finite income amount in record: {item!r}")

        if asset != "USDT":
            if itype == "COMMISSION":
                non_usdt_fees[asset] = non_usdt_fees.get(asset, 0.0) + abs(amount)
            continue

        if itype == "TRANSFER":
            if start_ms is None or ts_ms > start_ms:
                transfers.append((ts_ms, amount))
                net_transfer_usdt += amount
                d_str = get_beijing_time_str(ts_ms / 1000.0)[:10]
                transfers_by_date[d_str] = transfers_by_date.get(d_str, 0.0) + amount
        elif itype == "FUNDING_FEE":
            funding_fee_usdt += amount
        elif itype == "COMMISSION":
            ledger_fee_usdt += -amount

        if itype in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE"):
            d_str = get_beijing_time_str(ts_ms / 1000.0)[:10]
            bucket = daily_income.setdefault(
                d_str, {"realized_pnl": 0.0, "fee": 0.0, "funding_fee": 0.0}
            )
            if itype == "REALIZED_PNL":
                bucket["realized_pnl"] += amount
            elif itype == "COMMISSION":
                bucket["fee"] -= amount
            else:
                bucket["funding_fee"] += amount

    transfers.sort(key=lambda x: x[0])
    return {
        "transfers": transfers,
        "net_transfer_usdt": round(net_transfer_usdt, 4),
        "funding_fee_usdt": round(funding_fee_usdt, 4),
        "ledger_fee_usdt": round(ledger_fee_usdt, 4),
        "non_usdt_fees": {k: round(v, 6) for k, v in non_usdt_fees.items()},
        "raw_count": len(income_list),
        "coverage_start": get_beijing_time_str(min_ts_ms / 1000.0) if min_ts_ms is not None else None,
        "coverage_end": get_beijing_time_str(max_ts_ms / 1000.0) if max_ts_ms is not None else None,
        "daily_income": daily_income,
        "transfers_by_date": transfers_by_date,
    }


def _make_initial_curve_point(
    timestamp_ms: int,
    dt_str: str,
    initial_equity: float,
    event: str,
) -> dict[str, Any]:
    """Create the initial baseline point shared by trade and DB equity curves."""
    return {
        "timestamp": timestamp_ms,
        "datetime": dt_str,
        "equity": round(initial_equity, 2),
        "cash": round(initial_equity, 2),
        "cumulative_pnl": 0.0,
        "drawdown_usd": 0.0,
        "drawdown_pct": 0.0,
        "event": event,
    }


def _recompute_curve_drawdowns(
    equity_curve: List[dict],
    initial_equity: float,
    effective_transfers: Sequence[Tuple[int, float]] = (),
    *,
    raw_equities: Optional[Sequence[float]] = None,
) -> Tuple[float, float, float, float, float, float]:
    """Recompute cumulative PnL and peak drawdowns across equity curve points."""
    net_flow = 0.0
    transfer_idx = 0
    peak_adjusted = initial_equity
    peak_capital_base = initial_equity
    last_dd_usd = 0.0
    last_dd_pct = 0.0
    max_dd_usd = 0.0
    max_dd_pct = 0.0

    for idx, pt in enumerate(equity_curve):
        pt_ts = int(pt["timestamp"])
        if raw_equities is not None:
            samples: Sequence[Tuple[int, float]] = ((pt_ts, raw_equities[idx]),)
        elif pt.get("bucket_samples"):
            samples = [(int(s[0]), float(s[1])) for s in pt["bucket_samples"]]
        else:
            raw_eq = float(pt["raw_equity"]) if pt.get("raw_equity") is not None else float(pt["equity"])
            samples = ((pt_ts, raw_eq),)

        bucket_max_dd_usd = 0.0
        bucket_max_dd_pct = 0.0
        adjusted_equity = initial_equity
        dd_usd = 0.0
        dd_pct = 0.0

        for s_ts, s_raw_eq in samples:
            while transfer_idx < len(effective_transfers) and effective_transfers[transfer_idx][0] <= s_ts:
                net_flow += effective_transfers[transfer_idx][1]
                transfer_idx += 1
            adjusted_equity = s_raw_eq - net_flow
            peak_adjusted = max(peak_adjusted, adjusted_equity)
            peak_capital_base = max(peak_capital_base, peak_adjusted, initial_equity + max(0.0, net_flow))

            drawdown = max(0.0, peak_adjusted - adjusted_equity)
            dd_usd = round(drawdown, 2)
            dd_num = dd_usd if raw_equities is not None else drawdown
            dd_pct = round(dd_num / peak_capital_base * 100.0, 2) if peak_capital_base > 0 else 0.0
            bucket_max_dd_usd = max(bucket_max_dd_usd, dd_usd)
            bucket_max_dd_pct = max(bucket_max_dd_pct, dd_pct)
            max_dd_usd = max(max_dd_usd, dd_usd)
            max_dd_pct = max(max_dd_pct, dd_pct)

        pt["cumulative_pnl"] = round(adjusted_equity - initial_equity, 2)
        pt["drawdown_usd"] = dd_usd
        pt["drawdown_pct"] = dd_pct
        if len(samples) > 1 or "peak_adjusted_equity" in pt:
            pt["peak_adjusted_equity"] = peak_adjusted
        if len(samples) > 1 or "peak_capital_base" in pt:
            pt["peak_capital_base"] = peak_capital_base
        if len(samples) > 1 or "bucket_max_dd_usd" in pt:
            pt["bucket_max_dd_usd"] = bucket_max_dd_usd
        if len(samples) > 1 or "bucket_max_dd_pct" in pt:
            pt["bucket_max_dd_pct"] = bucket_max_dd_pct
        last_dd_usd = dd_usd
        last_dd_pct = dd_pct

    return peak_adjusted, peak_capital_base, last_dd_usd, last_dd_pct, max_dd_usd, max_dd_pct


def build_trade_equity_curve(
    sorted_trades: Sequence[Mapping[str, Any]],
    initial_capital: float,
    total_unrealized_pnl: float,
    *,
    now_ms: Optional[int] = None,
) -> Tuple[List[dict], float, float]:
    """Build equity curve and peak drawdown from sorted closed trades."""
    running_cash = initial_capital
    current_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    first_ts: Optional[int] = None
    if sorted_trades:
        first_t = sorted_trades[0]
        raw_entry_ts = first_t.get("entry_ts")
        if raw_entry_ts is not None:
            first_ts = int(raw_entry_ts)
        elif first_t.get("closed_ts") is not None:
            first_ts = int(first_t["closed_ts"])
        elif first_t.get("closed_at") is not None:
            parsed_first = parse_to_timestamp(first_t["closed_at"])
            if parsed_first is not None:
                first_ts = int(parsed_first * 1000)
    base_ts = (first_ts if first_ts is not None else current_ms) - 3600 * 1000
    equity_curve: List[dict] = [
        _make_initial_curve_point(
            base_ts,
            get_beijing_time_str(base_ts / 1000.0),
            initial_capital,
            "初始基准资金 (Initial Capital)",
        )
    ]
    raw_equities: List[float] = [initial_capital]

    for t in sorted_trades:
        net_pnl = float(t["net_pnl"])
        running_cash += net_pnl
        sign = "+" if net_pnl >= 0 else ""
        raw_c_ts = t.get("closed_ts")
        if raw_c_ts is not None:
            if isinstance(raw_c_ts, bool):
                raise ValueError(f"Invalid boolean closed_ts in trade: {t!r}")
            c_ts = int(raw_c_ts)
        else:
            parsed_c = parse_to_timestamp(t.get("closed_at"))
            if parsed_c is None:
                raise ValueError(f"Trade missing valid closed_ts/closed_at for equity curve: {t!r}")
            c_ts = int(parsed_c * 1000)
        c_at = (
            str(t["closed_at"])
            if t.get("closed_at") not in (None, "--", "")
            else get_beijing_time_str(c_ts / 1000.0)
        )
        sym = str(t["symbol"]).strip()
        close_reason = str(t["close_reason"]).strip()
        if not sym or not close_reason:
            raise ValueError(f"Trade missing symbol or close_reason for equity curve: {t!r}")
        raw_equities.append(running_cash)
        equity_curve.append({
            "timestamp": c_ts,
            "datetime": c_at,
            "equity": round(running_cash, 2),
            "cash": round(running_cash, 2),
            "event": f"平仓 {sym} ({sign}{net_pnl:.2f} USDT, {close_reason})",
        })

    current_eq = running_cash + total_unrealized_pnl
    raw_equities.append(current_eq)
    equity_curve.append({
        "timestamp": current_ms,
        "datetime": get_beijing_time_str(current_ms / 1000.0),
        "equity": round(current_eq, 2),
        "cash": round(running_cash, 2),
        "event": "当前实时净值 (Current Real-Time Equity)",
    })
    _, _, _, _, max_dd_usd, max_dd_pct = _recompute_curve_drawdowns(
        equity_curve, initial_capital, raw_equities=raw_equities
    )
    return equity_curve, max_dd_usd, max_dd_pct


def build_trade_daily_pnl(
    sorted_trades: Sequence[Mapping[str, Any]],
    initial_capital: float,
) -> List[dict]:
    """Aggregate closed trades by Beijing date (UTC+8) for offline mode."""
    daily_groups: Dict[str, List[Mapping[str, Any]]] = {}
    for t in sorted_trades:
        day_str = _resolve_trade_beijing_date(t)
        if not day_str:
            continue
        daily_groups.setdefault(day_str, []).append(t)

    daily_pnl: List[dict] = []
    day_equity = initial_capital
    for day_str in sorted(daily_groups):
        d_trades = daily_groups[day_str]
        d_realized = round(
            sum(float(x["realized_pnl"]) if x.get("realized_pnl") is not None else 0.0 for x in d_trades), 2
        )
        d_fee = round(
            sum(float(x["fee"]) if x.get("fee") is not None else 0.0 for x in d_trades), 2
        )
        d_net = round(
            sum(float(x["net_pnl"]) if x.get("net_pnl") is not None else 0.0 for x in d_trades), 2
        )
        d_wins = sum(1 for x in d_trades if x.get("outcome") == "WIN")
        d_losses = sum(1 for x in d_trades if x.get("outcome") == "LOSS")
        d_be = sum(1 for x in d_trades if x.get("outcome") == "BREAKEVEN")
        d_ret_pct = round((d_net / day_equity * 100.0), 2) if day_equity > 0 else 0.0
        daily_pnl.append({
            "date": day_str,
            "realized_pnl": d_realized,
            "fee": d_fee,
            "funding_fee": 0.0,
            "net_pnl": d_net,
            "return_pct": d_ret_pct,
            "trades_count": len(d_trades),
            "wins": d_wins,
            "losses": d_losses,
            "breakeven": d_be,
        })
        day_equity += d_net
    return daily_pnl


def build_db_equity_curve(
    recording: Mapping[str, Any],
    recording_start_ms: Optional[int],
    previous_report: Optional[Mapping[str, Any]],
    account_info: Mapping[str, Any],
    transfers: Sequence[Tuple[int, float]],
    *,
    now_ms: Optional[int] = None,
) -> Tuple[List[dict], float]:
    """Build compacted, transfer-adjusted equity curve from SQLite observations."""
    initial_equity = float(recording["initial_equity"])
    start_ms = int(recording["start_ms"])
    effective_transfers = [(ts, amt) for ts, amt in transfers if ts > start_ms]

    equity = float(account_info["totalMarginBalance"])
    wallet_balance = float(account_info["totalWalletBalance"])
    current_ms = now_ms if now_ms is not None else int(time.time() * 1000)

    prev_curve = [] if recording_start_ms is not None else list((previous_report or {}).get("equity_curve") or [])
    if prev_curve:
        equity_curve = [dict(pt) for pt in prev_curve]
    else:
        equity_curve = [
            _make_initial_curve_point(start_ms, recording["started_at"], initial_equity, "开始记录")
        ]

    new_point: dict[str, Any] = {
        "timestamp": current_ms,
        "datetime": get_beijing_time_str(current_ms / 1000.0),
        "equity": round(equity, 2),
        "raw_equity": equity,
        "cash": round(wallet_balance, 2),
        "event": "币安账户快照",
    }
    same_bucket = (
        len(equity_curve) >= 2
        and equity_curve[-1].get("event") == "币安账户快照"
        and (int(equity_curve[-1]["timestamp"]) // EQUITY_COMPACTION_BUCKET_MS)
        == (current_ms // EQUITY_COMPACTION_BUCKET_MS)
    )
    same_ts = bool(equity_curve) and int(equity_curve[-1]["timestamp"]) == current_ms
    if same_bucket or same_ts:
        prev_pt = equity_curve[-1]
        if prev_pt.get("bucket_samples"):
            samples = [[int(s[0]), float(s[1])] for s in prev_pt["bucket_samples"]]
        else:
            prev_raw_eq = float(prev_pt["raw_equity"]) if prev_pt.get("raw_equity") is not None else float(prev_pt["equity"])
            samples = [[int(prev_pt["timestamp"]), prev_raw_eq]]
        if samples and int(samples[-1][0]) == current_ms:
            samples[-1][1] = equity
        elif len(samples) >= 2 and samples[-1][1] == equity and samples[-2][1] == equity and current_ms >= int(samples[-1][0]):
            samples[-1][0] = current_ms
        else:
            samples.append([current_ms, equity])
            if len(samples) >= 2 and int(samples[-1][0]) < int(samples[-2][0]):
                samples.sort(key=lambda s: int(s[0]))
        new_point["bucket_samples"] = samples
        equity_curve[-1] = new_point
    else:
        new_point["bucket_samples"] = [[current_ms, equity]]
        equity_curve.append(new_point)

    _recompute_curve_drawdowns(equity_curve, initial_equity, effective_transfers)
    total_net_flow = sum(amt for _, amt in effective_transfers)
    return equity_curve, total_net_flow


def build_db_daily_pnl(
    income_list: Sequence[Mapping[str, Any]],
    trades: Sequence[Mapping[str, Any]],
    initial_equity: float,
    *,
    income_agg: Optional[Mapping[str, Any]] = None,
    start_ms: Optional[int] = None,
) -> List[dict]:
    """Build transfer-aware Beijing-date daily PnL combining trade outcomes and ledger cashflows."""
    agg = income_agg if income_agg is not None else aggregate_income_ledger(income_list, start_ms=start_ms)
    daily_income: Mapping[str, Mapping[str, float]] = agg["daily_income"]
    if start_ms is not None and income_agg is not None:
        transfers_by_date: Dict[str, float] = {}
        for ts_ms, amt in agg["transfers"]:
            if int(ts_ms) > start_ms:
                d_str = get_beijing_time_str(int(ts_ms) / 1000.0)[:10]
                transfers_by_date[d_str] = transfers_by_date.get(d_str, 0.0) + float(amt)
    else:
        transfers_by_date = agg["transfers_by_date"]

    def _get_day_bucket(d_str: str) -> dict[str, Any]:
        return daily_by_date.setdefault(d_str, {
            "date": d_str,
            "realized_pnl": 0.0,
            "fee": 0.0,
            "funding_fee": 0.0,
            "trades_count": 0,
            "wins": 0,
            "losses": 0,
            "breakeven": 0,
        })

    daily_by_date: Dict[str, dict] = {}
    outcome_key_map = {"WIN": "wins", "LOSS": "losses", "BREAKEVEN": "breakeven"}
    for t in trades:
        d_str = _resolve_trade_beijing_date(t)
        if not d_str:
            continue
        entry = _get_day_bucket(d_str)
        entry["trades_count"] += 1
        outcome_key = outcome_key_map.get(t.get("outcome"))
        if outcome_key:
            entry[outcome_key] += 1

    for d_str, inc in daily_income.items():
        entry = _get_day_bucket(d_str)
        entry["realized_pnl"] += inc["realized_pnl"]
        entry["fee"] += inc["fee"]
        entry["funding_fee"] += inc["funding_fee"]

    daily_pnl: List[dict] = []
    day_equity = initial_equity
    all_dates = sorted(set(daily_by_date.keys()) | set(transfers_by_date.keys()))
    for d_str in all_dates:
        day_transfer = transfers_by_date.get(d_str, 0.0)
        entry = daily_by_date.get(d_str)
        if entry is None:
            day_equity += day_transfer
            continue
        base_equity = day_equity + max(0.0, day_transfer)
        entry["realized_pnl"] = round(entry["realized_pnl"], 2)
        entry["fee"] = round(entry["fee"], 2)
        entry["funding_fee"] = round(entry["funding_fee"], 2)
        entry["net_pnl"] = round(entry["realized_pnl"] - entry["fee"] + entry["funding_fee"], 2)
        entry["net_transfer"] = round(day_transfer, 2)
        entry["capital_base"] = round(base_equity, 6)
        entry["return_pct"] = round((entry["net_pnl"] / base_equity * 100.0), 2) if base_equity > 0 else 0.0
        day_equity += entry["net_pnl"] + day_transfer
        daily_pnl.append(entry)
    return daily_pnl


def calculate_quant_risk_metrics(
    trades: Sequence[Mapping[str, Any]],
    active_positions: Sequence[Mapping[str, Any]],
    account_info: Optional[Mapping[str, Any]],
    *,
    default_initial_capital: float = 10000.0,
    custom_initial_capital: Optional[float] = None,
    net_transfers: Optional[float] = None,
    total_funding_fee: float = 0.0,
    daily_pnl_override: Optional[List[dict]] = None,
    equity_curve_override: Optional[List[dict]] = None,
    initial_capital_override: Optional[float] = None,
) -> Tuple[dict, List[dict], List[dict]]:
    """基于统一净盈亏 (net_pnl) 口径计算全部账户绩效、风控比值、净值曲线与逐日盈亏。"""
    sorted_trades = sorted(trades, key=lambda x: x.get("closed_ts") or 0)

    normalized_trades: List[dict] = []
    for t in sorted_trades:
        t_copy = dict(t)
        raw_net = t.get("net_pnl")
        raw_realized = t.get("realized_pnl")
        if raw_net is None and raw_realized is None:
            t_copy["outcome"] = "UNKNOWN"
            normalized_trades.append(t_copy)
            continue
        raw_fee = t.get("fee")
        fee_val = float(raw_fee) if raw_fee is not None else 0.0
        net_pnl = float(raw_net) if raw_net is not None else float(raw_realized) - fee_val
        t_copy["net_pnl"] = round(net_pnl, 4)
        t_copy["outcome"] = classify_outcome(net_pnl)
        normalized_trades.append(t_copy)

    closed_trades = len(normalized_trades)
    priced_trades = [t for t in normalized_trades if t["outcome"] != "UNKNOWN"]
    winning_trades = [t for t in priced_trades if t["outcome"] == "WIN"]
    losing_trades = [t for t in priced_trades if t["outcome"] == "LOSS"]
    breakeven_trades = [t for t in priced_trades if t["outcome"] == "BREAKEVEN"]

    win_count = len(winning_trades)
    loss_count = len(losing_trades)
    breakeven_count = len(breakeven_trades)

    cumulative_realized_pnl = round(
        sum(float(t["realized_pnl"]) if t.get("realized_pnl") is not None else 0.0 for t in priced_trades), 4
    )
    total_fee = round(
        sum(float(t["fee"]) if t.get("fee") is not None else 0.0 for t in priced_trades), 4
    )
    net_realized_pnl = round(sum(float(t["net_pnl"]) for t in priced_trades), 4)

    total_unrealized_pnl = round(sum(float(p["unrealized_pnl"]) for p in active_positions), 4)
    total_position_margin = round(sum(float(p["initial_margin"]) for p in active_positions), 4)

    if account_info is not None:
        if not isinstance(account_info, Mapping):
            raise TypeError(f"account_info must be a Mapping, got {type(account_info).__name__}")
        wallet_balance = float(account_info["totalWalletBalance"])
        total_equity = float(account_info["totalMarginBalance"])
        raw_avail = account_info.get("availableBalance")
        available_margin = (
            float(raw_avail) if raw_avail is not None else max(0.0, total_equity - total_position_margin)
        )
        if initial_capital_override is not None:
            initial_capital = float(initial_capital_override)
        elif custom_initial_capital is not None:
            initial_capital = custom_initial_capital
        elif net_transfers is not None and net_transfers > 0:
            initial_capital = round(net_transfers, 2)
        else:
            initial_capital = default_initial_capital
    else:
        initial_capital = float(initial_capital_override) if initial_capital_override is not None else default_initial_capital
        wallet_balance = round(initial_capital + net_realized_pnl + total_funding_fee, 4)
        total_equity = round(wallet_balance + total_unrealized_pnl, 4)
        available_margin = round(max(0.0, total_equity - total_position_margin), 4)

    total_return_pct = (
        round(((total_equity - initial_capital) / initial_capital * 100.0), 2) if initial_capital > 0 else 0.0
    )

    priced_count = len(priced_trades)
    win_rate_pct = round((win_count / priced_count * 100.0), 2) if priced_count > 0 else 0.0
    decisive = win_count + loss_count
    win_rate_ex_be = round((win_count / decisive * 100.0), 2) if decisive > 0 else 0.0

    gross_profit = sum(float(t["net_pnl"]) for t in winning_trades)
    gross_loss = abs(sum(float(t["net_pnl"]) for t in losing_trades))
    profit_factor: Optional[float] = round(gross_profit / gross_loss, 2) if gross_loss > 1e-4 else None

    avg_trade_pnl = round((net_realized_pnl / priced_count), 2) if priced_count > 0 else 0.0
    trade_returns_pct = [float(t["pnl_pct"]) for t in priced_trades if t.get("pnl_pct") is not None]
    avg_trade_pnl_pct = round(sum(trade_returns_pct) / len(trade_returns_pct), 2) if trade_returns_pct else None

    avg_win = round((gross_profit / win_count), 2) if win_count > 0 else 0.0
    avg_loss = round((gross_loss / loss_count), 2) if loss_count > 0 else 0.0
    payoff_ratio: Optional[float] = round(avg_win / avg_loss, 2) if avg_loss > 1e-4 else None

    largest_win_usd = max((float(t["net_pnl"]) for t in winning_trades), default=0.0)
    largest_loss_usd = min((float(t["net_pnl"]) for t in losing_trades), default=0.0)

    max_consecutive_wins = 0
    max_consecutive_losses = 0
    curr_w = 0
    curr_l = 0
    for t in priced_trades:
        if t["outcome"] == "WIN":
            curr_w += 1
            curr_l = 0
            max_consecutive_wins = max(max_consecutive_wins, curr_w)
        elif t["outcome"] == "LOSS":
            curr_l += 1
            curr_w = 0
            max_consecutive_losses = max(max_consecutive_losses, curr_l)
        else:
            curr_w = 0
            curr_l = 0

    known_durations = [
        int(t["duration_seconds"])
        for t in normalized_trades
        if t.get("duration_seconds") is not None
        and t.get("entry_time") != "--"
        and (t.get("entry_time") not in (None, "") or int(t["duration_seconds"]) > 0)
    ]
    if known_durations:
        avg_hold_sec = int(sum(known_durations) / len(known_durations))
        avg_hold_str = format_duration(avg_hold_sec)
        max_hold_sec = max(known_durations)
        min_hold_sec = min(known_durations)
    else:
        avg_hold_sec = 0
        avg_hold_str = "--"
        max_hold_sec = 0
        min_hold_sec = 0

    if equity_curve_override is not None:
        equity_curve = equity_curve_override
        max_drawdown_usd = max(
            (
                max(
                    float(pt["drawdown_usd"]),
                    float(pt["bucket_max_dd_usd"]) if pt.get("bucket_max_dd_usd") is not None else 0.0,
                )
                for pt in equity_curve
            ),
            default=0.0,
        )
        max_drawdown_pct = max(
            (
                max(
                    float(pt["drawdown_pct"]),
                    float(pt["bucket_max_dd_pct"]) if pt.get("bucket_max_dd_pct") is not None else 0.0,
                )
                for pt in equity_curve
            ),
            default=0.0,
        )
    else:
        equity_curve, max_drawdown_usd, max_drawdown_pct = build_trade_equity_curve(
            priced_trades, initial_capital, total_unrealized_pnl
        )

    daily_pnl = (
        daily_pnl_override
        if daily_pnl_override is not None
        else build_trade_daily_pnl(priced_trades, initial_capital)
    )
    sharpe_ratio, sortino_ratio, sharpe_method = compute_risk_ratios(daily_pnl, initial_capital)

    summary = {
        "initial_capital": round(initial_capital, 2),
        "total_equity": round(total_equity, 2),
        "wallet_balance": round(wallet_balance, 2),
        "available_margin": round(available_margin, 2),
        "position_margin": round(total_position_margin, 2),
        "total_unrealized_pnl": round(total_unrealized_pnl, 2),
        "cumulative_realized_pnl": round(cumulative_realized_pnl, 2),
        "total_fee": round(total_fee, 4),
        "total_funding_fee": round(total_funding_fee, 2),
        "net_realized_pnl": round(net_realized_pnl, 2),
        "total_return_pct": total_return_pct,
        "open_trades": len(active_positions),
        "closed_trades": closed_trades,
        "win_count": win_count,
        "loss_count": loss_count,
        "breakeven_count": breakeven_count,
        "win_rate_pct": win_rate_pct,
        "win_rate_ex_breakeven_pct": win_rate_ex_be,
        "profit_factor": profit_factor,
        "payoff_ratio": payoff_ratio,
        "avg_trade_pnl": avg_trade_pnl,
        "avg_trade_pnl_pct": avg_trade_pnl_pct,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "max_drawdown_usd": round(max_drawdown_usd, 2),
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "sharpe_ratio": sharpe_ratio,
        "sortino_ratio": sortino_ratio,
        "sharpe_method": sharpe_method,
        "avg_hold_duration_seconds": avg_hold_sec,
        "avg_hold_duration_str": avg_hold_str,
        "max_hold_duration_seconds": max_hold_sec,
        "min_hold_duration_seconds": min_hold_sec,
        "max_consecutive_wins": max_consecutive_wins,
        "max_consecutive_losses": max_consecutive_losses,
        "largest_win_usd": round(largest_win_usd, 2),
        "largest_loss_usd": round(largest_loss_usd, 2),
    }
    return summary, equity_curve, daily_pnl


def build_pnl_distribution(trade_history: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Bucket closed trade percentage returns into dashboard histogram bins."""
    bins_def: List[Dict[str, Any]] = [
        {"label": "<-5%", "min": -999999.0, "max": -5.0, "count": 0, "trades": []},
        {"label": "-5% ~ -2%", "min": -5.0, "max": -2.0, "count": 0, "trades": []},
        {"label": "-2% ~ 0%", "min": -2.0, "max": -0.0001, "count": 0, "trades": []},
        {"label": "保本 (0%)", "min": -0.0001, "max": 0.0001, "count": 0, "trades": []},
        {"label": "0% ~ +2%", "min": 0.0001, "max": 2.0, "count": 0, "trades": []},
        {"label": "+2% ~ +5%", "min": 2.0, "max": 5.0, "count": 0, "trades": []},
        {"label": "+5% ~ +10%", "min": 5.0, "max": 10.0, "count": 0, "trades": []},
        {"label": ">+10%", "min": 10.0, "max": 999999.0, "count": 0, "trades": []},
    ]
    scatter_points: List[Dict[str, Any]] = []
    for t in trade_history:
        pct = t.get("pnl_pct")
        if pct is None:
            continue
        pct_val = float(pct)
        if pct_val < -5.0:
            bin_idx = 0
        elif pct_val < -2.0:
            bin_idx = 1
        elif pct_val < -0.0001:
            bin_idx = 2
        elif pct_val <= 0.0001:
            bin_idx = 3
        elif pct_val < 2.0:
            bin_idx = 4
        elif pct_val < 5.0:
            bin_idx = 5
        elif pct_val < 10.0:
            bin_idx = 6
        else:
            bin_idx = 7
        bins_def[bin_idx]["count"] += 1
        bins_def[bin_idx]["trades"].append(t["symbol"])

        scatter_points.append({
            "trade_id": t["trade_id"],
            "symbol": t["symbol"],
            "pnl_pct": t["pnl_pct"],
            "roe_pct": t["roe_pct"],
            "net_pnl": t["net_pnl"],
            "duration_minutes": round(int(t["duration_seconds"]) / 60.0, 1),
            "closed_at": t["closed_at"],
            "outcome": t["outcome"],
        })

    return {"bins": bins_def, "scatter": scatter_points}


def build_income_breakdown(
    *,
    summary: Mapping[str, Any],
    open_position_fee: float,
    income_agg: Mapping[str, Any],
    income_available: bool,
    principal_source: str,
) -> Dict[str, Any]:
    """Assemble the report's `income_breakdown` section from pre-aggregated ledger totals."""
    return {
        "realized_pnl": summary["cumulative_realized_pnl"],
        "fee": summary["total_fee"],
        "open_position_fee": open_position_fee,
        "ledger_fee": income_agg["ledger_fee_usdt"],
        "non_usdt_fees": income_agg["non_usdt_fees"],
        "funding_fee": income_agg["funding_fee_usdt"],
        "transfer": income_agg["net_transfer_usdt"],
        "raw_count": income_agg["raw_count"],
        "coverage_start": income_agg["coverage_start"],
        "coverage_end": income_agg["coverage_end"],
        "available": income_available,
        "principal_source": principal_source,
    }
