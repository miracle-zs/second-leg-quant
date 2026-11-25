#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
账户量化风控数据分析引擎 (Account Quant Analytics Engine)
==============================================================================
功能:
1. 读取币安实盘合约 API (/fapi/v2/account, /fapi/v1/income, /fapi/v1/userTrades, /fapi/v2/positionRisk)。
2. 整合本地 runs/trade_positions.json 持仓元数据 (趋势线、移动锁利止损状态等)。
3. 基于统一的 (symbol, positionSide) FIFO 持仓账本重构已平仓交易，支持多轮开平、部分平仓、
   多空双向及单向 (BOTH) 持仓模式、手续费与资金费率精确核算。
4. 计算净盈亏口径下的胜率、盈亏比、峰值回撤、日历填充夏普/索提诺比率与净值曲线。
5. 支持输出 runs/account_analytics.json (原子写入) 或增量持久化到 SQLite。
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import logging
import math
from pathlib import Path
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from binance_client import BinanceAPIError, BinanceFuturesClient  # noqa: E402
from config import (  # noqa: E402
    BEIJING_TZ,
    DEFAULT_ANALYTICS_PATH,
    DEFAULT_POSITIONS_PATH,
    RUNS_DIR,
    atomic_write_json,
    clean_symbol,
    get_beijing_time_str,
    open_sqlite_connection,
    resolve_database_path,
)

INCOME_PAGE_LIMIT = 1000
USER_TRADES_PAGE_LIMIT = 1000
DEFAULT_OUTPUT_PATH = DEFAULT_ANALYTICS_PATH
EQUITY_COMPACTION_BUCKET_MS = 300_000  # 5 分钟滚动窗口去重

logger = logging.getLogger("account_analytics")


def parse_to_timestamp(dt_val: Any) -> Optional[float]:
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


def _classify_outcome(net_pnl: float) -> str:
    if net_pnl > 1e-4:
        return "WIN"
    if net_pnl < -1e-4:
        return "LOSS"
    return "BREAKEVEN"


def _income_asset(item: dict) -> str:
    asset = item.get("asset")
    return str(asset).strip().upper() if asset else "USDT"


def _fetch_paginated_range(
    fetch_page: Callable[[int, int], List[dict]],
    start_ms: int,
    end_ms: int,
    *,
    page_limit: int = 1000,
    label: str = "records",
) -> List[dict]:
    """二分时间窗口分页拉取币安接口记录，避免触达单页上限丢数据。"""
    collected: List[dict] = []
    ranges: List[Tuple[int, int]] = [(start_ms, end_ms)]
    while ranges:
        w_start, w_end = ranges.pop()
        page = fetch_page(w_start, w_end)
        if len(page) >= page_limit:
            if w_start >= w_end:
                raise RuntimeError(f"同一毫秒 {label} 超过接口单页上限 ({page_limit})")
            middle = (w_start + w_end) // 2
            ranges.extend([(w_start, middle), (middle + 1, w_end)])
        else:
            collected.extend(page)
    return collected


def _extract_trendline_meta(trendline: Any) -> Tuple[Optional[Any], str, Optional[dict]]:
    if not isinstance(trendline, dict):
        return None, "--", None
    trendline_id = trendline.get("id") or trendline.get("line_id")
    p1_raw = trendline.get("p1")
    p2_raw = trendline.get("p2")
    p1 = float(p1_raw) if p1_raw is not None else None
    p2 = float(p2_raw) if p2_raw is not None else None
    state = trendline.get("state")
    state_color = state.get("linecolor") if isinstance(state, dict) else None
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


def _resolve_close_reason(raw: dict, exit_price: float, entry_price: float, side: str) -> Tuple[str, str]:
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


class AccountAnalyticsEngine:
    """账户量化风控数据分析与指标聚合引擎。"""

    def __init__(
        self,
        client: Optional[Any] = None,
        positions_path: Path = DEFAULT_POSITIONS_PATH,
        initial_capital: Optional[float] = None,
        fee_rate: float = 0.0005,
        default_leverage: int = 5,
        offline_mode: bool = False,
    ):
        self.positions_path = Path(positions_path)
        self.custom_initial_capital = float(initial_capital) if initial_capital is not None else None
        self.initial_capital = self.custom_initial_capital if self.custom_initial_capital is not None else 10000.0
        self.fee_rate = float(fee_rate)
        self.default_leverage = int(default_leverage)
        self.offline_mode = bool(offline_mode)

        if client is not None:
            self.client = client
        elif not self.offline_mode:
            self.client = BinanceFuturesClient.from_env()
        else:
            self.client = None

    def fetch_binance_data(
        self, *, include_income: bool = True
    ) -> Tuple[Optional[dict], List[dict], List[dict], dict]:
        """
        拉取币安实盘合约账户、资金流水与活跃持仓快照。
        仅捕获明确的接口/网络异常 (BinanceAPIError / TimeoutError / OSError)，不吞掉内部逻辑 Bug。
        """
        api_status = {
            "configured": False,
            "connected": False,
            "positions_available": False,
            "income_available": False,
            "status_code": 0,
            "message": "Offline Mode / Client Unconfigured",
            "mode": "OFFLINE_FALLBACK",
            "last_sync": get_beijing_time_str(),
        }

        if self.offline_mode or not self.client or not self.client.is_configured():
            logger.info("ℹ️ 运行在离线/本地回退模式 (Offline Mode)。")
            return None, [], [], api_status

        api_status["configured"] = True

        # 1. /fapi/v2/account
        try:
            account_info = self.client.get_account_info()
            if not isinstance(account_info, dict):
                raise BinanceAPIError(
                    "Invalid /fapi/v2/account response",
                    code="INVALID_RESPONSE",
                    endpoint="/fapi/v2/account",
                )
        except (BinanceAPIError, TimeoutError, OSError) as e:
            code = e.code if isinstance(e, BinanceAPIError) and e.code is not None else -1
            api_status["status_code"] = code
            api_status["message"] = str(e)
            logger.warning("⚠️ 币安 /fapi/v2/account 请求异常 [%s]: %s，降级至本地离线模式。", code, e)
            return None, [], [], api_status

        # 2. /fapi/v1/income (DB 模式下由后续增量分页一次性拉取，避免重复请求)
        income_list: List[dict] = []
        if include_income:
            try:
                raw_income = self.client.get_income(limit=INCOME_PAGE_LIMIT)
                if isinstance(raw_income, list):
                    income_list = raw_income
                    api_status["income_available"] = True
            except (BinanceAPIError, TimeoutError, OSError) as e:
                api_status["income_error"] = str(e)
                logger.warning("⚠️ 币安 /fapi/v1/income 查询失败: %s", e)

        # 3. /fapi/v2/positionRisk
        active_positions_binance: List[dict] = []
        try:
            pos_data = self.client.get_account_positions()
            if isinstance(pos_data, list):
                api_status["positions_available"] = True
            else:
                pos_data = []
        except (BinanceAPIError, TimeoutError, OSError) as e:
            pos_data = None
            api_status["positions_error"] = str(e)
            logger.warning("⚠️ 币安 /fapi/v2/positionRisk 查询失败: %s", e)

        if pos_data is not None:
            for p in pos_data:
                if float(p["positionAmt"]) != 0.0:
                    active_positions_binance.append(p)

        api_status["connected"] = True
        api_status["status_code"] = 200
        if not include_income:
            if api_status["positions_available"]:
                api_status["mode"] = "BINANCE_SNAPSHOT_PENDING_INCOME"
                api_status["message"] = "Connected; pending incremental income sync"
            else:
                api_status["mode"] = "BINANCE_PARTIAL"
                api_status["message"] = "Binance position data unavailable"
        elif api_status["income_available"] and api_status["positions_available"]:
            api_status["mode"] = "BINANCE_REALTIME"
            api_status["message"] = "Connected to Binance Live Futures API"
            logger.info("✅ 成功连接币安实盘合约 API 并同步账户、流水与持仓数据。")
        else:
            api_status["mode"] = "BINANCE_PARTIAL"
            api_status["message"] = "Binance income or position data unavailable"
            logger.warning("币安账户已连接，持仓或资金流水查询失败。")

        return account_info, income_list, active_positions_binance, api_status

    def load_local_positions_store(self) -> Tuple[List[dict], List[dict]]:
        """
        从 runs/trade_positions.json 加载本地活跃持仓与历史记录。
        文件不存在返回 ([], [])；文件损坏或结构非法直接抛出异常。
        """
        if not self.positions_path.exists():
            logger.warning("持仓数据文件不存在: %s", self.positions_path)
            return [], []

        with open(self.positions_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(f"Expected JSON object in {self.positions_path}, got {type(data).__name__}")
        positions = data.get("positions", [])
        history = data.get("history", [])
        if not isinstance(positions, list) or not isinstance(history, list):
            raise ValueError(f"Invalid positions/history lists in {self.positions_path}")
        return positions, history

    def enrich_trade_record(self, raw: dict, index: int) -> dict:
        """
        标准化本地历史平仓交易记录，严格区分 LONG/SHORT 方向，不伪造平仓价/时间戳/负数手续费。
        """
        sym = clean_symbol(raw["symbol"])
        entry_price = float(raw["entry_price"]) if raw.get("entry_price") is not None else 0.0
        raw_amount = float(raw["amount"]) if raw.get("amount") is not None else 0.0
        raw_side = str(raw.get("side") or raw.get("positionSide") or "").strip().upper()
        if raw_side in ("LONG", "SHORT"):
            side = raw_side
        else:
            side = "SHORT" if raw_amount < 0 else "LONG"
        amount = abs(raw_amount)
        direction = 1.0 if side == "LONG" else -1.0
        leverage = int(raw["leverage"]) if raw.get("leverage") else self.default_leverage

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
            str(entry_time_raw).strip()
            if entry_time_raw and str(entry_time_raw).strip() != "--"
            else (get_beijing_time_str(entry_ts) if entry_ts is not None else "--")
        )
        closed_at_str = (
            str(closed_at_raw).strip()
            if closed_at_raw and str(closed_at_raw).strip() != "--"
            else (get_beijing_time_str(closed_ts) if closed_ts is not None else "--")
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

        # 允许负值手续费返佣；仅在已知成交盈亏且未提供 fee 时按费率估算
        if raw.get("fee") is not None:
            fee = round(float(raw["fee"]), 4)
        elif realized_pnl is not None and (entry_notional > 0 or exit_notional > 0):
            fee = round((entry_notional + exit_notional) * self.fee_rate, 4)
        elif realized_pnl is not None:
            fee = 0.0
        else:
            fee = None

        if realized_pnl is not None:
            net_pnl = round(realized_pnl - (fee or 0.0), 4)
            outcome = _classify_outcome(net_pnl)
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

    def enrich_active_position(self, raw: dict, index: int) -> dict:
        """
        标准化当前活跃持仓；支持提取币安 /fapi/v2/positionRisk 的 updateTime 作为时间基准。
        """
        sym = clean_symbol(raw["symbol"])
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
        leverage = int(raw["leverage"]) if raw.get("leverage") else self.default_leverage

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

        # 优先取本地 entry_time，其次回退币安 positionRisk 的 updateTime
        entry_time_raw = raw.get("entry_time")
        fallback_time_raw = raw.get("updateTime") if raw.get("updateTime") is not None else raw.get("update_time")
        entry_ts = parse_to_timestamp(entry_time_raw)
        if entry_ts is None:
            entry_ts = parse_to_timestamp(fallback_time_raw)

        if entry_ts is not None:
            holding_seconds = max(0, int(time.time() - entry_ts))
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
        trendline_id = trendline.get("id") if isinstance(trendline, dict) else raw.get("trendline_id")

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
            "source": raw.get("source", "BINANCE_SYNC" if raw.get("positionAmt") is not None else "MANUAL"),
            "status": "OPEN",
            "notes": raw.get("notes", ""),
        }

    def _merge_active_positions(
        self,
        active_positions_binance: List[dict],
        local_positions: List[dict],
        positions_available: bool,
    ) -> List[dict]:
        """当币安持仓可用时，按 (symbol, side) 继承本地趋势线与移动锁利元数据。"""
        if not positions_available:
            return [self.enrich_active_position(p, idx) for idx, p in enumerate(local_positions)]

        local_by_key: Dict[Tuple[str, str], dict] = {}
        for lp in local_positions:
            sym = clean_symbol(lp.get("symbol", ""))
            raw_side = str(lp.get("side") or lp.get("positionSide") or "").upper()
            amt = float(lp.get("amount") or 0.0)
            lp_side = raw_side if raw_side in ("LONG", "SHORT") else ("SHORT" if amt < 0 else "LONG")
            local_by_key[(sym, lp_side)] = lp

        merged_raw: List[dict] = []
        for bp in active_positions_binance:
            sym = clean_symbol(bp["symbol"])
            raw_amt = float(bp["positionAmt"])
            raw_side = str(bp.get("positionSide") or "BOTH").upper()
            bp_side = raw_side if raw_side in ("LONG", "SHORT") else ("SHORT" if raw_amt < 0 else "LONG")
            local = local_by_key.get((sym, bp_side))
            if local:
                combined = dict(bp)
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
            else:
                merged_raw.append(bp)

        return [self.enrich_active_position(p, idx) for idx, p in enumerate(merged_raw)]

    def _consume_fifo_lots(
        self,
        queue: List[dict],
        *,
        sym: str,
        close_side: str,
        order_id: str,
        ts_ms: int,
        qty: Decimal,
        quote_qty: float,
        fill_fee_usdt: float,
        fill_non_usdt: Dict[str, float],
        fill_realized: Optional[float],
    ) -> List[dict]:
        """按 FIFO 消耗开仓批次，并为多笔部分平仓与记录起点前已开仓位生成标准 slice。"""
        slices: List[dict] = []
        remaining = qty
        direction = 1.0 if close_side == "LONG" else -1.0

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
                queue.pop(0)

            close_notional_slice = quote_qty * fill_frac
            close_fee_slice = fill_fee_usdt * fill_frac
            close_non_usdt_slice = {a: v * fill_frac for a, v in fill_non_usdt.items()}
            if fill_realized is not None:
                realized_slice = fill_realized * fill_frac
            else:
                realized_slice = (close_notional_slice - open_notional_slice) * direction

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
            realized_slice = (fill_realized * fill_frac) if fill_realized is not None else 0.0
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
        self,
        income_list: List[dict],
        local_history: List[dict],
        fills: Sequence[dict] = (),
    ) -> Tuple[List[dict], float]:
        """
        统一的 Binance 历史平仓重构状态机 (DB 与非 DB 模式共用):
        - 当提供 fills (/fapi/v1/userTrades) 时，按 (symbol, positionSide) 维护 FIFO 仓位批次，
          精准处理多轮开平、部分平仓、反向反手、单向模式 (BOTH) 及开/平双边手续费归集。
        - 对无成交明细的历史流水 (或非 DB 模式)，按 (symbol, tradeId / 2s 窗口) 聚合，
          严格隔离非 USDT 手续费 (如 BNB)，并与本地 history 1 对 1 匹配元数据。
        返回: (trade_history, open_position_fee_usdt)
        """
        commissions_usdt: Dict[Tuple[str, str], float] = {}
        non_usdt_comm: Dict[Tuple[str, str], Dict[str, float]] = {}
        realized_by_trade: Dict[Tuple[str, str], float] = {}
        realized_tran_ids: Dict[Tuple[str, str], str] = {}

        for item in income_list:
            sym = clean_symbol(item.get("symbol") or "")
            if not sym:
                continue
            itype = item.get("incomeType")
            tid = str(item.get("tradeId") or "")
            asset = _income_asset(item)
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
                if key not in realized_tran_ids and item.get("tranId") is not None:
                    realized_tran_ids[key] = str(item["tranId"])

        open_lots: Dict[Tuple[str, str], List[dict]] = {}
        closed_slices: List[dict] = []
        matched_trade_keys: set[Tuple[str, str]] = set()
        consumed_comm_keys: set[Tuple[str, str]] = set()

        sorted_fills = sorted(fills, key=lambda f: (int(f["time"]), int(f["id"])))
        for f in sorted_fills:
            sym = clean_symbol(f["symbol"])
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

            if raw_pos_side in ("LONG", "SHORT"):
                queue = open_lots.setdefault((sym, raw_pos_side), [])
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
                        self._consume_fifo_lots(
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
                # 单向持仓模式 (BOTH)
                queue = open_lots.setdefault((sym, "BOTH"), [])
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
                        total_open_qty = sum(lot["qty"] for lot in queue)
                        close_part = min(qty, total_open_qty)
                        open_rem = qty - close_part
                        close_ratio = float(close_part / qty)
                        matched_trade_keys.add(t_key)
                        closed_slices.extend(
                            self._consume_fifo_lots(
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
                elif fill_realized is not None:
                    inferred_close_side = "LONG" if fill_side == "SELL" else "SHORT"
                    matched_trade_keys.add(t_key)
                    closed_slices.extend(
                        self._consume_fifo_lots(
                            queue,
                            sym=sym,
                            close_side=inferred_close_side,
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
                    new_side = "LONG" if fill_side == "BUY" else "SHORT"
                    queue.append({
                        "side": new_side,
                        "qty": qty,
                        "notional": quote_qty,
                        "fee_usdt": fill_fee_usdt,
                        "non_usdt_fees": dict(fill_non_usdt),
                        "time": ts_ms,
                    })

        # 按同一平仓订单聚合 slices
        grouped_orders: Dict[Tuple[str, str, str], List[dict]] = {}
        for s in closed_slices:
            grouped_orders.setdefault((s["symbol"], s["side"], s["order_id"]), []).append(s)

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
            pnl_pct = round(net_pnl / entry_notional * 100.0, 2) if entry_notional > 0 else None

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

            matched_local = _pop_matching_local_trade(sym, closed_ts / 1000.0, local_history, used_local_indices)
            trendline_id, trendline_label, trendline_info = _extract_trendline_meta(
                matched_local.get("trendline") if matched_local else None
            )
            if matched_local:
                close_reason, close_reason_code = _resolve_close_reason(matched_local, exit_price, entry_price, side)
            else:
                close_reason, close_reason_code = "币安实盘成交", "BINANCE_REALTIME"

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
                "roe_pct": None,
                "leverage": None,
                "entry_time": entry_time,
                "closed_at": get_beijing_time_str(closed_ts / 1000.0),
                "entry_ts": entry_ts,
                "closed_ts": closed_ts,
                "duration_seconds": duration_seconds,
                "duration_str": duration_str,
                "outcome": _classify_outcome(net_pnl),
                "status": "BINANCE_REALTIME_TRADE",
                "close_reason": close_reason,
                "close_reason_code": close_reason_code,
                "highest_price": 0.0,
                "trailing_stop_triggered": bool(matched_local.get("trailing_stop_triggered", False)) if matched_local else False,
                "trailing_stop_price": matched_local.get("trailing_stop_price") if matched_local else None,
                "trendline_id": trendline_id,
                "trendline_label": trendline_label,
                "trendline_info": trendline_info,
                "source": "BINANCE_REALTIME_VERIFIED" if matched_local else "BINANCE_REALTIME",
                "notes": (matched_local.get("notes") or "") if matched_local else "币安成交",
            })

        # 回退处理无对应 fill 的 REALIZED_PNL 流水 (如非 DB 模式或遗漏成交的历史记录)
        unmatched_realized = [
            item
            for item in income_list
            if item.get("incomeType") == "REALIZED_PNL"
            and clean_symbol(item.get("symbol") or "")
            and _income_asset(item) == "USDT"
            and (
                not item.get("tradeId")
                or (clean_symbol(item.get("symbol") or ""), str(item.get("tradeId"))) not in matched_trade_keys
            )
        ]

        # 按 symbol 分组再按时间排序，避免多币种交叉打断同一平仓单聚类
        by_symbol: Dict[str, List[dict]] = {}
        for item in unmatched_realized:
            by_symbol.setdefault(clean_symbol(item.get("symbol") or ""), []).append(item)

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

            matched_local = _pop_matching_local_trade(sym, closed_ts / 1000.0, local_history, used_local_indices)
            if matched_local:
                enriched = self.enrich_trade_record(matched_local, len(reconstructed) + idx)
                local_realized = enriched["realized_pnl"]
                pnl_consistent = (
                    local_realized is not None
                    and abs(local_realized - realized_pnl) <= max(0.05, abs(realized_pnl) * 0.05)
                )
                enriched["realized_pnl"] = realized_pnl
                enriched["fee"] = fee
                enriched["non_usdt_fees"] = non_usdt
                enriched["net_pnl"] = net_pnl
                enriched["outcome"] = _classify_outcome(net_pnl)
                if pnl_consistent and enriched["entry_notional"] > 0:
                    enriched["pnl_pct"] = round(net_pnl / enriched["entry_notional"] * 100.0, 2)
                    enriched["roe_pct"] = (
                        round(enriched["pnl_pct"] * enriched["leverage"], 2) if enriched.get("leverage") else None
                    )
                else:
                    enriched["entry_price"] = 0.0
                    enriched["exit_price"] = 0.0
                    enriched["amount"] = 0.0
                    enriched["entry_notional"] = 0.0
                    enriched["exit_notional"] = 0.0
                    enriched["pnl_pct"] = None
                    enriched["roe_pct"] = None
                enriched["source"] = "BINANCE_REALTIME_VERIFIED"
                reconstructed.append(enriched)
            else:
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
                    "outcome": _classify_outcome(net_pnl),
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

    def _compute_risk_ratios(
        self,
        daily_pnl: List[dict],
        trades: List[dict],
        initial_capital: float,
    ) -> Tuple[Optional[float], Optional[float], str]:
        """
        统一计算年化夏普比率与索提诺比率:
        - 多日情形: 补齐起止日期间的空仓日 (0 收益)，避免只按交易日虚增年化均值/压低波动;
        - 索提诺分母: 始终除以总观测期数 N (标准下行半方差)，绝不除以亏损日数;
        - 方差或样本不足时返回 None，绝不返回 2.5 或 999.0 等伪造值。
        """
        if len(daily_pnl) >= 2:
            by_date = {d["date"]: d for d in daily_pnl if d.get("date")}
            sorted_dates = sorted(by_date.keys())
            if len(sorted_dates) >= 2:
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
                        ret = (day_net / running_eq) if running_eq > 0 else 0.0
                        calendar_returns.append(ret)
                        running_eq += day_net
                    else:
                        calendar_returns.append(0.0)
                    cur += timedelta(days=1)

                n = len(calendar_returns)
                if n >= 2:
                    mean_r = sum(calendar_returns) / n
                    var_r = sum((r - mean_r) ** 2 for r in calendar_returns) / (n - 1)
                    std_r = math.sqrt(var_r) if var_r > 0 else 0.0
                    sharpe = round((mean_r / std_r) * math.sqrt(365), 2) if std_r > 1e-9 else None

                    downside_sq = sum(min(0.0, r) ** 2 for r in calendar_returns)
                    downside_std = math.sqrt(downside_sq / n)
                    sortino = round((mean_r / downside_std) * math.sqrt(365), 2) if downside_std > 1e-9 else None
                    return sharpe, sortino, "DAILY_RETURNS"

        if len(trades) >= 2 and initial_capital > 0:
            trade_returns = [float(t["net_pnl"]) / initial_capital for t in trades]
            n = len(trade_returns)
            mean_tr = sum(trade_returns) / n
            var_tr = sum((r - mean_tr) ** 2 for r in trade_returns) / (n - 1)
            std_tr = math.sqrt(var_tr) if var_tr > 0 else 0.0
            sharpe = round((mean_tr / std_tr) * math.sqrt(365), 2) if std_tr > 1e-9 else None

            downside_sq = sum(min(0.0, r) ** 2 for r in trade_returns)
            downside_std = math.sqrt(downside_sq / n)
            sortino = round((mean_tr / downside_std) * math.sqrt(365), 2) if downside_std > 1e-9 else None
            return sharpe, sortino, "TRADE_RETURNS_ANNUALIZED"

        return None, None, "NONE"

    def calculate_quant_risk_metrics(
        self,
        trades: List[dict],
        active_positions: List[dict],
        account_info: Optional[dict],
        net_transfers: Optional[float] = None,
        *,
        total_funding_fee: float = 0.0,
        daily_pnl_override: Optional[List[dict]] = None,
        equity_curve_override: Optional[List[dict]] = None,
        initial_capital_override: Optional[float] = None,
    ) -> Tuple[dict, List[dict], List[dict]]:
        """
        基于统一净盈亏 (net_pnl) 口径计算全部账户绩效、风控比值、净值曲线与逐日盈亏。
        """
        sorted_trades = sorted(trades, key=lambda x: x.get("closed_ts") or 0)

        # 所有胜负与盈亏比严格按 net_pnl 口径一致划分；无成交价/盈亏的记录标记为 UNKNOWN 且不混入收益统计
        normalized_trades: List[dict] = []
        for t in sorted_trades:
            t_copy = dict(t)
            raw_net = t.get("net_pnl")
            raw_realized = t.get("realized_pnl")
            if raw_net is None and raw_realized is None:
                t_copy["outcome"] = "UNKNOWN"
                normalized_trades.append(t_copy)
                continue
            net_pnl = float(raw_net) if raw_net is not None else float(raw_realized) - float(t.get("fee") or 0.0)
            outcome = _classify_outcome(net_pnl)
            t_copy["net_pnl"] = round(net_pnl, 4)
            t_copy["outcome"] = outcome
            normalized_trades.append(t_copy)

        closed_trades = len(normalized_trades)
        priced_trades = [t for t in normalized_trades if t["outcome"] != "UNKNOWN"]
        winning_trades = [t for t in priced_trades if t["outcome"] == "WIN"]
        losing_trades = [t for t in priced_trades if t["outcome"] == "LOSS"]
        breakeven_trades = [t for t in priced_trades if t["outcome"] == "BREAKEVEN"]

        win_count = len(winning_trades)
        loss_count = len(losing_trades)
        breakeven_count = len(breakeven_trades)

        cumulative_realized_pnl = round(sum(float(t.get("realized_pnl") or 0.0) for t in priced_trades), 4)
        total_fee = round(sum(float(t.get("fee") or 0.0) for t in priced_trades), 4)
        net_realized_pnl = round(sum(float(t["net_pnl"]) for t in priced_trades), 4)

        total_unrealized_pnl = round(sum(float(p["unrealized_pnl"]) for p in active_positions), 4)
        total_position_margin = round(sum(float(p["initial_margin"]) for p in active_positions), 4)

        if account_info and isinstance(account_info, dict):
            raw_wb = account_info.get("totalWalletBalance")
            wallet_balance = float(raw_wb) if raw_wb is not None else 0.0
            raw_mb = account_info.get("totalMarginBalance")
            total_equity = float(raw_mb) if raw_mb is not None else (wallet_balance + total_unrealized_pnl)
            raw_avail = account_info.get("availableBalance")
            available_margin = (
                float(raw_avail) if raw_avail is not None else max(0.0, total_equity - total_position_margin)
            )
            if initial_capital_override is not None:
                initial_capital = float(initial_capital_override)
            elif self.custom_initial_capital is not None:
                initial_capital = self.custom_initial_capital
            elif net_transfers is not None and net_transfers > 0:
                initial_capital = round(net_transfers, 2)
            else:
                initial_capital = self.initial_capital
        else:
            initial_capital = float(initial_capital_override) if initial_capital_override is not None else self.initial_capital
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

        if gross_loss > 1e-4:
            profit_factor: Optional[float] = round(gross_profit / gross_loss, 2)
        else:
            profit_factor = None

        avg_trade_pnl = round((net_realized_pnl / priced_count), 2) if priced_count > 0 else 0.0
        trade_returns_pct = [t["pnl_pct"] for t in priced_trades if t.get("pnl_pct") is not None]
        avg_trade_pnl_pct = round(sum(trade_returns_pct) / len(trade_returns_pct), 2) if trade_returns_pct else None

        avg_win = round((gross_profit / win_count), 2) if win_count > 0 else 0.0
        avg_loss = round((gross_loss / loss_count), 2) if loss_count > 0 else 0.0
        payoff_ratio: Optional[float] = round(avg_win / avg_loss, 2) if avg_loss > 1e-4 else None

        largest_win_usd = max((float(t["net_pnl"]) for t in priced_trades), default=0.0)
        largest_loss_usd = min((float(t["net_pnl"]) for t in priced_trades), default=0.0)

        # 连胜与连亏 (net_pnl 口径)
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
            if t.get("entry_time") != "--"
            and (t.get("entry_time") not in (None, "") or int(t.get("duration_seconds") or 0) > 0)
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

        # 净值曲线与最大回撤
        if equity_curve_override is not None:
            equity_curve = equity_curve_override
            max_drawdown_usd = max((float(pt.get("drawdown_usd") or 0.0) for pt in equity_curve), default=0.0)
            max_drawdown_pct = max((float(pt.get("drawdown_pct") or 0.0) for pt in equity_curve), default=0.0)
        else:
            equity_curve, max_drawdown_usd, max_drawdown_pct = self._build_trade_equity_curve(
                priced_trades, initial_capital, total_unrealized_pnl
            )

        # 逐日盈亏
        if daily_pnl_override is not None:
            daily_pnl = daily_pnl_override
        else:
            daily_pnl = self._build_trade_daily_pnl(priced_trades, initial_capital)

        # 夏普 / 索提诺严格在最终 daily_pnl 上计算
        sharpe_ratio, sortino_ratio, sharpe_method = self._compute_risk_ratios(
            daily_pnl, priced_trades, initial_capital
        )

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

    def _build_trade_equity_curve(
        self,
        sorted_trades: List[dict],
        initial_capital: float,
        total_unrealized_pnl: float,
    ) -> Tuple[List[dict], float, float]:
        equity_curve: List[dict] = []
        running_cash = initial_capital
        peak_equity = initial_capital
        max_dd_usd = 0.0
        max_dd_pct = 0.0

        now_ms = int(time.time() * 1000)
        first_ts = None
        if sorted_trades:
            first_ts = sorted_trades[0].get("entry_ts") or sorted_trades[0].get("closed_ts")
        base_ts = int(first_ts if first_ts is not None else now_ms) - 3600 * 1000
        equity_curve.append({
            "timestamp": base_ts,
            "datetime": get_beijing_time_str(base_ts / 1000.0),
            "equity": round(initial_capital, 2),
            "cash": round(initial_capital, 2),
            "cumulative_pnl": 0.0,
            "drawdown_usd": 0.0,
            "drawdown_pct": 0.0,
            "event": "初始基准资金 (Initial Capital)",
        })

        for t in sorted_trades:
            net_pnl = float(t["net_pnl"])
            running_cash += net_pnl
            cur_eq = running_cash
            peak_equity = max(peak_equity, cur_eq)
            dd_usd = round(max(0.0, peak_equity - cur_eq), 2)
            dd_pct = round((dd_usd / peak_equity * 100.0), 2) if peak_equity > 0 else 0.0
            max_dd_usd = max(max_dd_usd, dd_usd)
            max_dd_pct = max(max_dd_pct, dd_pct)

            sign = "+" if net_pnl >= 0 else ""
            c_ts = int(t["closed_ts"]) if t.get("closed_ts") is not None else now_ms
            c_at = t.get("closed_at") if t.get("closed_at") not in (None, "--") else get_beijing_time_str(c_ts / 1000.0)
            equity_curve.append({
                "timestamp": c_ts,
                "datetime": c_at,
                "equity": round(cur_eq, 2),
                "cash": round(running_cash, 2),
                "cumulative_pnl": round(running_cash - initial_capital, 2),
                "drawdown_usd": dd_usd,
                "drawdown_pct": dd_pct,
                "event": f"平仓 {t.get('symbol', 'UNKNOWN')} ({sign}{net_pnl:.2f} USDT, {t.get('close_reason', '平仓')})",
            })

        current_eq = running_cash + total_unrealized_pnl
        peak_equity = max(peak_equity, current_eq)
        cur_dd_usd = round(max(0.0, peak_equity - current_eq), 2)
        cur_dd_pct = round((cur_dd_usd / peak_equity * 100.0), 2) if peak_equity > 0 else 0.0
        max_dd_usd = max(max_dd_usd, cur_dd_usd)
        max_dd_pct = max(max_dd_pct, cur_dd_pct)

        equity_curve.append({
            "timestamp": now_ms,
            "datetime": get_beijing_time_str(now_ms / 1000.0),
            "equity": round(current_eq, 2),
            "cash": round(running_cash, 2),
            "cumulative_pnl": round(current_eq - initial_capital, 2),
            "drawdown_usd": cur_dd_usd,
            "drawdown_pct": cur_dd_pct,
            "event": "当前实时净值 (Current Real-Time Equity)",
        })
        return equity_curve, max_dd_usd, max_dd_pct

    def _build_trade_daily_pnl(self, sorted_trades: List[dict], initial_capital: float) -> List[dict]:
        daily_groups: Dict[str, List[dict]] = {}
        for t in sorted_trades:
            c_at = t.get("closed_at") or ""
            if not c_at or c_at == "--":
                continue
            day_str = str(c_at)[:10]
            daily_groups.setdefault(day_str, []).append(t)

        daily_pnl: List[dict] = []
        day_equity = initial_capital
        for day_str in sorted(daily_groups):
            d_trades = daily_groups[day_str]
            d_realized = round(sum(float(x["realized_pnl"]) for x in d_trades), 2)
            d_fee = round(sum(float(x.get("fee", 0.0)) for x in d_trades), 2)
            d_net = round(sum(float(x["net_pnl"]) for x in d_trades), 2)
            d_wins = sum(1 for x in d_trades if x["outcome"] == "WIN")
            d_losses = sum(1 for x in d_trades if x["outcome"] == "LOSS")
            d_be = sum(1 for x in d_trades if x["outcome"] == "BREAKEVEN")
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

    def _build_db_equity_curve(
        self,
        recording: dict,
        recording_start_ms: Optional[int],
        previous_report: Optional[dict],
        account_info: dict,
        income_list: List[dict],
    ) -> Tuple[List[dict], float]:
        initial_equity = float(recording["initial_equity"])
        raw_mb = account_info.get("totalMarginBalance")
        equity = float(raw_mb) if raw_mb is not None else float(account_info["totalWalletBalance"])
        raw_wb = account_info.get("totalWalletBalance")
        wallet_balance = float(raw_wb) if raw_wb is not None else equity
        now_ms = int(time.time() * 1000)

        if recording_start_ms is not None:
            equity_curve: List[dict] = [{
                "timestamp": recording["start_ms"],
                "datetime": recording["started_at"],
                "equity": round(initial_equity, 2),
                "cash": round(initial_equity, 2),
                "cumulative_pnl": 0.0,
                "drawdown_usd": 0.0,
                "drawdown_pct": 0.0,
                "event": "开始记录",
            }]
        else:
            prev_curve = list((previous_report or {}).get("equity_curve") or [])
            if not prev_curve:
                equity_curve = [{
                    "timestamp": recording["start_ms"],
                    "datetime": recording["started_at"],
                    "equity": round(initial_equity, 2),
                    "cash": round(initial_equity, 2),
                    "cumulative_pnl": 0.0,
                    "drawdown_usd": 0.0,
                    "drawdown_pct": 0.0,
                    "event": "开始记录",
                }]
            else:
                equity_curve = [dict(pt) for pt in prev_curve]

        new_point = {
            "timestamp": now_ms,
            "datetime": get_beijing_time_str(now_ms / 1000.0),
            "equity": round(equity, 2),
            "cash": round(wallet_balance, 2),
            "event": "币安账户快照",
        }
        # 5 分钟滚动窗口去重: 同桶内的重复快照就地刷新，避免高频同步无限膨胀
        if (
            len(equity_curve) >= 2
            and equity_curve[-1].get("event") == "币安账户快照"
            and (int(equity_curve[-1]["timestamp"]) // EQUITY_COMPACTION_BUCKET_MS)
            == (now_ms // EQUITY_COMPACTION_BUCKET_MS)
        ):
            equity_curve[-1] = new_point
        elif equity_curve and int(equity_curve[-1]["timestamp"]) == now_ms:
            equity_curve[-1] = new_point
        else:
            equity_curve.append(new_point)

        transfers = sorted(
            (int(item["time"]), float(item["income"]))
            for item in income_list
            if item.get("incomeType") == "TRANSFER" and _income_asset(item) == "USDT"
        )
        net_flow = 0.0
        transfer_idx = 0
        peak_adjusted = initial_equity
        peak_capital_base = initial_equity

        for pt in equity_curve:
            while transfer_idx < len(transfers) and transfers[transfer_idx][0] <= int(pt["timestamp"]):
                net_flow += transfers[transfer_idx][1]
                transfer_idx += 1
            adjusted_equity = float(pt["equity"]) - net_flow
            peak_adjusted = max(peak_adjusted, adjusted_equity)
            peak_capital_base = max(peak_capital_base, peak_adjusted, initial_equity + max(0.0, net_flow))
            drawdown = max(0.0, peak_adjusted - adjusted_equity)
            pt["cumulative_pnl"] = round(adjusted_equity - initial_equity, 2)
            pt["drawdown_usd"] = round(drawdown, 2)
            pt["drawdown_pct"] = round(drawdown / peak_capital_base * 100.0, 2) if peak_capital_base > 0 else 0.0

        total_net_flow = sum(amt for _, amt in transfers)
        return equity_curve, total_net_flow

    def _build_db_daily_pnl(
        self,
        income_list: List[dict],
        trades: List[dict],
        initial_equity: float,
    ) -> List[dict]:
        daily_by_date: Dict[str, dict] = {}
        for t in trades:
            c_at = t.get("closed_at") or ""
            if not c_at or c_at == "--":
                continue
            d_str = str(c_at)[:10]
            entry = daily_by_date.setdefault(d_str, {
                "date": d_str,
                "realized_pnl": 0.0,
                "fee": 0.0,
                "funding_fee": 0.0,
                "trades_count": 0,
                "wins": 0,
                "losses": 0,
                "breakeven": 0,
            })
            entry["trades_count"] += 1
            if t["outcome"] == "WIN":
                entry["wins"] += 1
            elif t["outcome"] == "LOSS":
                entry["losses"] += 1
            elif t["outcome"] == "BREAKEVEN":
                entry["breakeven"] += 1

        for item in income_list:
            itype = item.get("incomeType")
            if _income_asset(item) != "USDT" or itype not in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE"):
                continue
            d_str = get_beijing_time_str(int(item["time"]) / 1000.0)[:10]
            entry = daily_by_date.setdefault(d_str, {
                "date": d_str,
                "realized_pnl": 0.0,
                "fee": 0.0,
                "funding_fee": 0.0,
                "trades_count": 0,
                "wins": 0,
                "losses": 0,
                "breakeven": 0,
            })
            amt = float(item["income"])
            if itype == "REALIZED_PNL":
                entry["realized_pnl"] += amt
            elif itype == "COMMISSION":
                entry["fee"] -= amt
            else:
                entry["funding_fee"] += amt

        daily_pnl: List[dict] = []
        day_equity = initial_equity
        for d_str in sorted(daily_by_date):
            entry = daily_by_date[d_str]
            entry["realized_pnl"] = round(entry["realized_pnl"], 2)
            entry["fee"] = round(entry["fee"], 2)
            entry["funding_fee"] = round(entry["funding_fee"], 2)
            entry["net_pnl"] = round(entry["realized_pnl"] - entry["fee"] + entry["funding_fee"], 2)
            entry["return_pct"] = round((entry["net_pnl"] / day_equity * 100.0), 2) if day_equity > 0 else 0.0
            day_equity += entry["net_pnl"]
            daily_pnl.append(entry)
        return daily_pnl

    def _sync_db_ledger(
        self,
        database: Path,
        account_info: dict,
        active_positions_binance: List[dict],
        recording_start_ms: Optional[int],
    ) -> Tuple[dict, List[dict], List[dict], List[dict], List[dict], Optional[dict], Optional[int]]:
        from account_dashboard_api import load_report

        previous_report = load_report(database)
        recording = previous_report.get("recording") if previous_report is not None else None
        effective_start_ms = recording_start_ms
        if recording is None and effective_start_ms is None:
            effective_start_ms = int(time.time() * 1000)
        if effective_start_ms is not None:
            if recording is not None:
                raise ValueError("账户记录起点已设置")
            recording = {
                "start_ms": effective_start_ms,
                "started_at": get_beijing_time_str(effective_start_ms / 1000.0),
                "initial_equity": float(account_info["totalMarginBalance"]),
            }

        start_ms = int(recording["start_ms"])
        end_ms = int(time.time() * 1000)
        last_sync_ms = int(recording.get("ledger_synced_until_ms", start_ms))
        query_start = max(start_ms, last_sync_ms - 86400 * 1000)
        if end_ms - query_start > 89 * 86400 * 1000:
            raise RuntimeError("账户流水同步间断超过币安接口可查询范围，请核对历史数据")

        try:
            new_income = _fetch_paginated_range(
                lambda s, e: self.client.get_income(start_time=s, end_time=e, limit=INCOME_PAGE_LIMIT),
                query_start,
                end_ms,
                page_limit=INCOME_PAGE_LIMIT,
                label="资金流水",
            )
        except (BinanceAPIError, TimeoutError, OSError) as e:
            raise RuntimeError(f"币安资金流水读取失败: {e}") from e

        existing_income: List[dict] = []
        existing_fills: List[dict] = []
        if database.exists():
            with closing(open_sqlite_connection(database, readonly=True)) as connection:
                tables = {
                    row[0]
                    for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
                }
                if "income_ledger" in tables:
                    existing_income = [
                        json.loads(row[0])
                        for row in connection.execute(
                            "SELECT payload FROM income_ledger WHERE timestamp >= ?", (start_ms,)
                        )
                    ]
                if "trade_fills" in tables:
                    existing_fills = [
                        json.loads(row[0])
                        for row in connection.execute(
                            "SELECT payload FROM trade_fills WHERE timestamp >= ?", (start_ms,)
                        )
                    ]
                if previous_report is not None and "equity_observations" in tables:
                    previous_report["equity_curve"] = [
                        json.loads(row[0])
                        for row in connection.execute(
                            "SELECT payload FROM equity_observations WHERE timestamp >= ? ORDER BY timestamp",
                            (start_ms,),
                        )
                    ]

        income_by_id = {
            (item["incomeType"], str(item["tranId"])): item
            for item in existing_income + new_income
        }
        income_list = list(income_by_id.values())

        # 仅扫描有新流水或仍在持仓的币种，避免全量历史 symbol 每次重复拉取 userTrades
        fills_cursor: Dict[str, int] = dict(recording.get("fills_cursor_ms") or {})
        symbols_to_sync = {
            clean_symbol(item["symbol"])
            for item in new_income
            if item.get("symbol") and item.get("incomeType") in ("REALIZED_PNL", "COMMISSION")
        }
        symbols_to_sync.update(clean_symbol(p["symbol"]) for p in active_positions_binance)
        if not existing_fills:
            symbols_to_sync.update(
                clean_symbol(item["symbol"])
                for item in existing_income
                if item.get("symbol") and item.get("incomeType") in ("REALIZED_PNL", "COMMISSION")
            )

        new_fills: List[dict] = []
        for symbol in sorted(s for s in symbols_to_sync if s):
            if symbol in fills_cursor:
                sym_start = max(start_ms, min(query_start, int(fills_cursor[symbol]) - 86400 * 1000))
            else:
                sym_start = start_ms
            window_start = sym_start
            while window_start <= end_ms:
                window_end = min(window_start + 7 * 86400 * 1000 - 1, end_ms)
                try:
                    chunk_fills = _fetch_paginated_range(
                        lambda s, e, _sym=symbol: self.client.get_user_trades(
                            symbol=_sym, start_time=s, end_time=e, limit=USER_TRADES_PAGE_LIMIT
                        ),
                        window_start,
                        window_end,
                        page_limit=USER_TRADES_PAGE_LIMIT,
                        label=f"{symbol} 成交",
                    )
                except (BinanceAPIError, TimeoutError, OSError) as e:
                    raise RuntimeError(f"币安成交读取失败 ({symbol}): {e}") from e
                new_fills.extend(chunk_fills)
                window_start = window_end + 1
            fills_cursor[symbol] = end_ms

        fills_by_id = {
            (clean_symbol(item["symbol"]), str(item["id"])): item
            for item in existing_fills + new_fills
        }
        recording["ledger_synced_until_ms"] = end_ms
        recording["fills_cursor_ms"] = fills_cursor
        return (
            recording,
            income_list,
            list(fills_by_id.values()),
            new_income,
            new_fills,
            previous_report,
            effective_start_ms,
        )

    def run(
        self,
        output_path: Optional[Path] = None,
        *,
        database: Optional[Path] = None,
        recording_start_ms: Optional[int] = None,
    ) -> dict:
        """执行账户数据拉取、交易重构、风控指标核算与持久化。"""
        out_file = Path(output_path or DEFAULT_OUTPUT_PATH)

        # 1. 获取币安实盘数据 (DB 模式跳过预拉 income，由后续增量同步一次性获取)
        account_info, income_list, active_positions_binance, api_status = self.fetch_binance_data(
            include_income=(database is None)
        )

        previous_report: Optional[dict] = None
        recording: Optional[dict] = None
        fills_list: List[dict] = []
        new_income: List[dict] = []
        new_fills: List[dict] = []
        effective_recording_start_ms = recording_start_ms

        if database is not None:
            from account_dashboard_api import publish_report

            if api_status["mode"] not in ("BINANCE_REALTIME", "BINANCE_SNAPSHOT_PENDING_INCOME"):
                raise RuntimeError("币安账户快照不完整，保留上一次完整实盘快照")

            assert account_info is not None
            (
                recording,
                income_list,
                fills_list,
                new_income,
                new_fills,
                previous_report,
                effective_recording_start_ms,
            ) = self._sync_db_ledger(
                database,
                account_info,
                active_positions_binance,
                recording_start_ms,
            )
            api_status["income_available"] = True
            api_status["mode"] = "BINANCE_REALTIME"
            api_status["message"] = "Connected to Binance Live Futures API"

        income_page_full = False if recording is not None else len(income_list) >= INCOME_PAGE_LIMIT

        # 2. 获取本地 trade_positions.json
        local_positions, local_history = self.load_local_positions_store()
        if recording is not None:
            cutoff = int(recording["start_ms"])
            income_list = [item for item in income_list if int(item["time"]) >= cutoff]
            filtered_local: List[dict] = []
            for trade in local_history:
                ts = parse_to_timestamp(trade.get("closed_at") or trade.get("exit_time"))
                if ts is not None and ts * 1000 >= cutoff:
                    filtered_local.append(trade)
            local_history = filtered_local

        # 3. 活跃持仓与历史平仓重构
        active_positions = self._merge_active_positions(
            active_positions_binance, local_positions, api_status["positions_available"]
        )

        open_position_fee = 0.0
        if recording is not None or api_status["income_available"]:
            trade_history, open_position_fee = self.reconstruct_binance_trades(
                income_list, local_history, fills=fills_list
            )
        else:
            trade_history = [self.enrich_trade_record(t, idx) for idx, t in enumerate(local_history)]

        # 4. 本金基准推导
        income_times = [int(item["time"]) for item in income_list]
        net_transfers: Optional[float] = None
        cached_baseline_capital: Optional[float] = None
        if income_list and not income_page_full:
            net_transfers = sum(
                float(x["income"])
                for x in income_list
                if x.get("incomeType") == "TRANSFER" and _income_asset(x) == "USDT"
            )
        elif database is None and self.custom_initial_capital is None and out_file.exists():
            try:
                with open(out_file, "r", encoding="utf-8") as f:
                    prev_json = json.load(f)
                prev_src = (prev_json.get("income_breakdown") or {}).get("principal_source")
                prev_cap = float((prev_json.get("summary") or {}).get("initial_capital") or 0.0)
                if prev_src in ("income_window", "recording_start", "configured", "cached_baseline") and prev_cap > 0:
                    cached_baseline_capital = prev_cap
            except (OSError, json.JSONDecodeError, ValueError, TypeError):
                cached_baseline_capital = None

        effective_transfers = net_transfers if (net_transfers is not None and net_transfers > 0) else cached_baseline_capital

        # 5. 计算量化指标与时间序列
        funding_fee_usdt = round(
            sum(
                float(item.get("income") or 0.0)
                for item in income_list
                if item.get("incomeType") == "FUNDING_FEE" and _income_asset(item) == "USDT"
            ),
            4,
        )

        if recording is not None:
            assert account_info is not None
            initial_equity = float(recording["initial_equity"])
            equity_curve_db, net_flow = self._build_db_equity_curve(
                recording, effective_recording_start_ms, previous_report, account_info, income_list
            )
            daily_pnl_db = self._build_db_daily_pnl(income_list, trade_history, initial_equity)
            summary, equity_curve, daily_pnl = self.calculate_quant_risk_metrics(
                trades=trade_history,
                active_positions=active_positions,
                account_info=account_info,
                net_transfers=effective_transfers,
                total_funding_fee=funding_fee_usdt,
                daily_pnl_override=daily_pnl_db,
                equity_curve_override=equity_curve_db,
                initial_capital_override=initial_equity,
            )
            equity_now = float(account_info["totalMarginBalance"])
            capital_base = initial_equity + max(0.0, net_flow)
            summary["initial_capital"] = round(initial_equity, 2)
            summary["total_return_pct"] = (
                round((equity_now - initial_equity - net_flow) / capital_base * 100.0, 2)
                if capital_base > 0
                else 0.0
            )
            summary["net_external_transfers"] = round(net_flow, 4)
        else:
            summary, equity_curve, daily_pnl = self.calculate_quant_risk_metrics(
                trades=trade_history,
                active_positions=active_positions,
                account_info=account_info,
                net_transfers=effective_transfers,
                total_funding_fee=funding_fee_usdt,
            )

        # 6. 流水汇总明细
        ledger_fee_usdt = round(
            sum(
                -float(item.get("income") or 0.0)
                for item in income_list
                if item.get("incomeType") == "COMMISSION" and _income_asset(item) == "USDT"
            ),
            4,
        )
        non_usdt_totals: Dict[str, float] = {}
        for item in income_list:
            if item.get("incomeType") == "COMMISSION":
                asset = _income_asset(item)
                if asset != "USDT":
                    non_usdt_totals[asset] = round(non_usdt_totals.get(asset, 0.0) + abs(float(item.get("income") or 0.0)), 6)

        income_breakdown = {
            "realized_pnl": summary["cumulative_realized_pnl"],
            "fee": summary["total_fee"],
            "open_position_fee": open_position_fee,
            "ledger_fee": ledger_fee_usdt,
            "non_usdt_fees": non_usdt_totals,
            "funding_fee": round(funding_fee_usdt, 4),
            "transfer": round(
                sum(
                    float(item.get("income") or 0.0)
                    for item in income_list
                    if item.get("incomeType") == "TRANSFER" and _income_asset(item) == "USDT"
                ),
                4,
            ),
            "raw_count": len(income_list),
            "coverage_start": get_beijing_time_str(min(income_times) / 1000.0) if income_times else None,
            "coverage_end": get_beijing_time_str(max(income_times) / 1000.0) if income_times else None,
            "page_full": income_page_full,
            "available": api_status["income_available"],
            "principal_source": (
                "recording_start"
                if recording is not None
                else "configured"
                if self.custom_initial_capital is not None
                else "income_window"
                if net_transfers is not None and net_transfers > 0
                else "cached_baseline"
                if cached_baseline_capital is not None
                else "default"
            ),
        }

        # 7. 收益率分布统计
        bins_def = [
            {"label": "<-5%", "min": -999999.0, "max": -5.0, "count": 0, "trades": []},
            {"label": "-5% ~ -2%", "min": -5.0, "max": -2.0, "count": 0, "trades": []},
            {"label": "-2% ~ 0%", "min": -2.0, "max": -0.0001, "count": 0, "trades": []},
            {"label": "保本 (0%)", "min": -0.0001, "max": 0.0001, "count": 0, "trades": []},
            {"label": "0% ~ +2%", "min": 0.0001, "max": 2.0, "count": 0, "trades": []},
            {"label": "+2% ~ +5%", "min": 2.0, "max": 5.0, "count": 0, "trades": []},
            {"label": "+5% ~ +10%", "min": 5.0, "max": 10.0, "count": 0, "trades": []},
            {"label": ">+10%", "min": 10.0, "max": 999999.0, "count": 0, "trades": []},
        ]
        scatter_points = []
        for t in trade_history:
            pct = t["pnl_pct"]
            if pct is None:
                continue
            placed = False
            for b in bins_def:
                if b["min"] <= pct < b["max"] or (b["max"] == 999999.0 and pct >= b["min"]):
                    b["count"] += 1
                    b["trades"].append(t["symbol"])
                    placed = True
                    break
            if not placed:
                target_bin = bins_def[0] if pct < bins_def[0]["min"] else bins_def[-1]
                target_bin["count"] += 1
                target_bin["trades"].append(t["symbol"])

            scatter_points.append({
                "trade_id": t["trade_id"],
                "symbol": t["symbol"],
                "pnl_pct": t["pnl_pct"],
                "roe_pct": t["roe_pct"],
                "net_pnl": t["net_pnl"],
                "duration_minutes": round(t["duration_seconds"] / 60.0, 1),
                "closed_at": t["closed_at"],
                "outcome": t["outcome"],
            })

        report = {
            "version": 1,
            "generated_at": get_beijing_time_str(),
            "data_source": {
                "BINANCE_REALTIME": "BINANCE_API",
                "BINANCE_PARTIAL": "BINANCE_PARTIAL",
                "OFFLINE_FALLBACK": "LOCAL_OFFLINE_FALLBACK",
            }[api_status["mode"]],
            "positions_source": "BINANCE_API" if api_status["positions_available"] else "LOCAL_CACHE",
            "api_status": api_status,
            "summary": summary,
            "active_positions": active_positions,
            "trade_history": trade_history,
            "equity_curve": equity_curve,
            "daily_pnl": daily_pnl,
            "pnl_distribution": {"bins": bins_def, "scatter": scatter_points},
            "income_breakdown": income_breakdown,
        }
        if recording is not None:
            report["recording"] = recording

        if database is None:
            atomic_write_json(out_file, report, allow_nan=False)
            logger.info("✅ 账户量化分析报表已成功生成并写入: %s", out_file)
        else:
            publish_report(database, report, new_income, new_fills)
            logger.info("✅ 账户实盘快照已写入 SQLite: %s", database)

        return report


def _merge_fee_dicts(a: Dict[str, float], b: Dict[str, float]) -> Dict[str, float]:
    out = dict(a)
    for k, v in b.items():
        out[k] = out.get(k, 0.0) + v
    return out


def _pop_matching_local_trade(
    sym: str,
    closed_sec: float,
    local_history: List[dict],
    used_indices: set[int],
) -> Optional[dict]:
    for idx, loc in enumerate(local_history):
        if idx in used_indices:
            continue
        if clean_symbol(loc.get("symbol", "")) != sym:
            continue
        loc_closed_at = loc.get("closed_at") or loc.get("exit_time")
        loc_ts = parse_to_timestamp(loc_closed_at)
        if loc_ts is not None and abs(loc_ts - closed_sec) <= 300:
            used_indices.add(idx)
            return loc
    return None


def print_cli_summary(report: dict) -> None:
    """格式化打印终端量化风控仪表盘。"""
    summary = report["summary"]
    api_status = report["api_status"]
    active_positions = report["active_positions"]
    trade_history = report["trade_history"]

    print("\n" + "=" * 78)
    print(" 🚀 币安合约账户量化风控指标与交易评测仪表盘 (HUD)")
    print("=" * 78)
    print(f" 状态模式: {'🟢 币安实盘在线' if api_status.get('connected') else '🟡 本地离线容灾回退'} | 更新时间: {report.get('generated_at')}")
    print("-" * 78)

    eq = summary["total_equity"]
    init_cap = summary["initial_capital"]
    ret_pct = summary["total_return_pct"]
    avail = summary["available_margin"]
    pnl = summary["cumulative_realized_pnl"]
    net_pnl = summary["net_realized_pnl"]
    fee = summary["total_fee"]

    ret_color = "\033[32m" if ret_pct >= 0 else "\033[31m"
    reset = "\033[0m"

    print(f" 💰 总资金净值 (Equity):     ${eq:,.2f} USDT  (初始本金: ${init_cap:,.2f})")
    print(f" 📈 账户总收益率 (Return):    {ret_color}{ret_pct:+.2f}%{reset}  (累计实现盈亏: ${pnl:+,.2f}, 净利润: ${net_pnl:+,.2f})")
    print(f" 🛡️  可用保证金 (Available):  ${avail:,.2f} USDT  (手续费支出: ${fee:,.2f})")
    print("-" * 78)

    wr = summary["win_rate_pct"]
    pf = summary["profit_factor"]
    if pf is None:
        pf_str = "∞ (全胜)" if summary["win_count"] > 0 and summary["loss_count"] == 0 else "--"
    else:
        pf_str = f"{pf:.2f}"
    mdd_pct = summary["max_drawdown_pct"]
    mdd_usd = summary["max_drawdown_usd"]
    sharpe = summary["sharpe_ratio"]
    sortino = summary["sortino_ratio"]
    sharpe_str = f"{sharpe:.2f}" if sharpe is not None else "--"
    sortino_str = f"{sortino:.2f}" if sortino is not None else "--"
    payoff_str = f"{summary['payoff_ratio']:.2f}" if summary["payoff_ratio"] is not None else "--"

    print(f" 🎯 交易胜率 (Win Rate):     {wr:.1f}%  (盈亏笔数: {summary['win_count']}胜 / {summary['loss_count']}负 / {summary['breakeven_count']}平)")
    print(f" ⚖️  盈亏比 (Profit Factor):  {pf_str}  (盈亏比率 Payoff: {payoff_str})")
    print(f" ⚠️  最大回撤 (Max Drawdown): {mdd_pct:.2f}% (${mdd_usd:,.2f} USDT)")
    print(f" 📊 夏普比率 (Sharpe Ratio): {sharpe_str}  |  索提诺比率 (Sortino): {sortino_str}")
    print(f" ⏳ 平均持仓周期:             {summary['avg_hold_duration_str']}  | 最大连胜: {summary['max_consecutive_wins']} 笔")
    print("-" * 78)

    print(f" ⚡ 活跃持仓监控 (Active Positions: {len(active_positions)}):")
    if active_positions:
        for p in active_positions:
            pnl_c = "\033[32m" if p["unrealized_pnl"] >= 0 else "\033[31m"
            print(
                f"   • {p['symbol']} ({p['leverage']}x): {p['amount']} @ ${p['entry_price']:.4f} -> 现价 ${p['mark_price']:.4f} | "
                f"浮盈: {pnl_c}{p['unrealized_pnl']:+.2f} USDT ({p['roe_pct']:+.2f}% ROE){reset} | 周期: {p['holding_str']}"
            )
    else:
        print("   (当前无活跃持仓)")
    print("-" * 78)

    print(f" 📜 历史平仓明细 (Recent Closed Trades: {len(trade_history)}):")
    for t in trade_history[-5:]:
        net_val = t.get("net_pnl")
        pnl_c = "\033[32m" if (net_val is not None and net_val >= 0) else "\033[31m"
        tl = f"[{t['trendline_label']}] " if t.get("trendline_id") else ""
        roe = f"{t['roe_pct']:+.2f}% ROE" if t["roe_pct"] is not None else "ROE 未知"
        exit_str = f"${t['exit_price']:.4f}" if t.get("exit_price") is not None else "--"
        net_str = f"{net_val:+.2f} USDT" if net_val is not None else "--"
        print(
            f"   [{t['closed_at']}] {t['symbol']} | 开: ${t['entry_price']:.4f} -> 平: {exit_str} | "
            f"净盈: {pnl_c}{net_str} ({roe}){reset} | 原因: {t['close_reason']} {tl}"
        )
    print("=" * 78 + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="币安合约账户量化风控数据分析引擎")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--output", "-o", type=Path, help="输出 JSON 文件路径")
    output.add_argument("--database", type=Path, help="将完整实盘快照保存到 SQLite")
    parser.add_argument(
        "--initial-capital",
        type=float,
        default=None,
        help="初始基准资金 (USDT)，缺省时自动根据币安历史转账充值净额推导",
    )
    parser.add_argument("--offline", action="store_true", help="强制启用离线回退模式 (不请求网络)")
    parser.add_argument("--json", action="store_true", help="仅输出 JSON 内容到终端")
    parser.add_argument("--recording-start-ms", type=int, help="开始记录的 Unix 时间戳（毫秒）")
    args = parser.parse_args()

    db_path = None if args.output is not None else resolve_database_path(args.database)
    if args.recording_start_ms is not None and db_path is None:
        parser.error("--recording-start-ms requires --database")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    engine = AccountAnalyticsEngine(
        initial_capital=args.initial_capital,
        offline_mode=args.offline,
    )
    report = engine.run(
        output_path=args.output,
        database=db_path,
        recording_start_ms=args.recording_start_ms,
    )

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif db_path is None:
        print_cli_summary(report)


if __name__ == "__main__":
    main()
