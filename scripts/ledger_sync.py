#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Binance Futures Ledger Pagination & Incremental SQLite Sync
===========================================================
Handles binary-window pagination for `/fapi/v1/income` and `/fapi/v1/userTrades`,
parallelized per-symbol fill synchronization (`ThreadPoolExecutor(max_workers=4)`),
and SQLite ledger bundle loading via `db_store`.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from binance_client import BinanceAPIError  # noqa: E402
from config import clean_symbol, get_beijing_time_str  # noqa: E402
import db_store  # noqa: E402

INCOME_PAGE_LIMIT = 1000
USER_TRADES_PAGE_LIMIT = 1000
USER_TRADES_CHUNK_MS = 7 * 86400 * 1000
MAX_BINANCE_LOOKBACK_MS = 89 * 86400 * 1000
USER_TRADES_MAX_WORKERS = 4


def fetch_paginated_range(
    fetch_page: Callable[[int, int], List[dict]],
    start_ms: int,
    end_ms: int,
    *,
    page_limit: int = 1000,
    label: str = "records",
) -> List[dict]:
    """二分时间窗口分页拉取币安接口记录（按时间正序遍历），避免触达单页上限丢数据。"""
    if start_ms > end_ms:
        return []
    collected: List[dict] = []
    ranges: List[Tuple[int, int]] = [(start_ms, end_ms)]
    while ranges:
        w_start, w_end = ranges.pop()
        page = fetch_page(w_start, w_end)
        if len(page) >= page_limit:
            if w_start >= w_end:
                raise RuntimeError(f"同一毫秒 {label} 超过接口单页上限 ({page_limit})")
            middle = (w_start + w_end) // 2
            ranges.extend([(middle + 1, w_end), (w_start, middle)])
        else:
            collected.extend(page)
    return collected


def _fetch_symbol_fills(
    client: Any,
    symbol: str,
    sym_start: int,
    end_ms: int,
) -> Tuple[str, List[dict]]:
    """Fetch all userTrades for a single symbol across 7-day Binance query chunks."""
    collected: List[dict] = []
    window_start = sym_start
    while window_start <= end_ms:
        window_end = min(window_start + USER_TRADES_CHUNK_MS - 1, end_ms)
        try:
            chunk_fills = fetch_paginated_range(
                lambda s, e, _sym=symbol: client.get_user_trades(
                    symbol=_sym, start_time=s, end_time=e, limit=USER_TRADES_PAGE_LIMIT
                ),
                window_start,
                window_end,
                page_limit=USER_TRADES_PAGE_LIMIT,
                label=f"{symbol} 成交",
            )
        except (BinanceAPIError, TimeoutError, OSError) as e:
            raise RuntimeError(f"币安成交读取失败 ({symbol}): {e}") from e
        collected.extend(chunk_fills)
        window_start = window_end + 1
    return symbol, collected


def sync_db_ledger(
    client: Any,
    database: Path,
    account_info: Mapping[str, Any],
    active_positions_binance: Sequence[Mapping[str, Any]],
    recording_start_ms: Optional[int],
) -> Tuple[
    Dict[str, Any],
    List[dict],
    List[dict],
    List[dict],
    List[dict],
    Optional[Dict[str, Any]],
    Optional[int],
]:
    """
    Incrementally sync Binance `/fapi/v1/income` and `/fapi/v1/userTrades` against SQLite
    without holding a SQLite write lock across network I/O.
    """
    recording: Optional[Dict[str, Any]] = db_store.load_recording(database)
    previous_report: Optional[Dict[str, Any]] = None
    if recording is None:
        previous_report = db_store.load_report(database)
        if previous_report is not None and previous_report.get("recording") is not None:
            raw_prev_rec = previous_report["recording"]
            if not isinstance(raw_prev_rec, Mapping):
                raise ValueError(
                    f"Corrupt previous_report['recording'] type: {type(raw_prev_rec).__name__}"
                )
            recording = dict(raw_prev_rec)

    try:
        end_ms = int(client.get_server_time())
    except (BinanceAPIError, TimeoutError, OSError) as e:
        raise RuntimeError(f"币安服务器时间读取失败: {e}") from e

    effective_start_ms = recording_start_ms
    if recording is None and effective_start_ms is None:
        effective_start_ms = end_ms
    if effective_start_ms is not None:
        if recording is not None:
            raise ValueError("账户记录起点已设置")
        recording = {
            "start_ms": effective_start_ms,
            "started_at": get_beijing_time_str(effective_start_ms / 1000.0),
            "initial_equity": float(account_info["totalMarginBalance"]),
        }

    assert recording is not None
    start_ms = int(recording["start_ms"])
    last_sync_ms = int(recording.get("ledger_synced_until_ms", start_ms))
    query_start = max(start_ms, last_sync_ms - 86400 * 1000)
    if end_ms - query_start > MAX_BINANCE_LOOKBACK_MS:
        raise RuntimeError("账户流水同步间断超过币安接口可查询范围，请核对历史数据")

    try:
        new_income = fetch_paginated_range(
            lambda s, e: client.get_income(start_time=s, end_time=e, limit=INCOME_PAGE_LIMIT),
            query_start,
            end_ms,
            page_limit=INCOME_PAGE_LIMIT,
            label="资金流水",
        )
    except (BinanceAPIError, TimeoutError, OSError) as e:
        raise RuntimeError(f"币安资金流水读取失败: {e}") from e

    bundle = db_store.load_ledger_bundle(database, start_ms=start_ms)
    raw_existing_income = bundle["income"]
    raw_existing_fills = bundle["fills"]
    raw_existing_obs = bundle["observations"]
    if not isinstance(raw_existing_income, list):
        raise TypeError(f"Expected list for bundle['income'], got {type(raw_existing_income).__name__}")
    if not isinstance(raw_existing_fills, list):
        raise TypeError(f"Expected list for bundle['fills'], got {type(raw_existing_fills).__name__}")
    if not isinstance(raw_existing_obs, list):
        raise TypeError(f"Expected list for bundle['observations'], got {type(raw_existing_obs).__name__}")
    existing_income: List[dict] = list(raw_existing_income)
    existing_fills: List[dict] = list(raw_existing_fills)
    if raw_existing_obs:
        if previous_report is None:
            previous_report = {"equity_curve": list(raw_existing_obs)}
        else:
            previous_report["equity_curve"] = list(raw_existing_obs)

    income_by_id = {
        (item["incomeType"], str(item["tranId"])): item
        for item in existing_income + new_income
    }
    income_list = list(income_by_id.values())

    # Scan only symbols with newly synced PnL/commission events or currently open positions.
    raw_cursor = recording.get("fills_cursor_ms")
    if raw_cursor is not None and not isinstance(raw_cursor, Mapping):
        raise TypeError(f"Expected mapping for recording['fills_cursor_ms'], got {type(raw_cursor).__name__}")
    fills_cursor: Dict[str, int] = dict(raw_cursor) if raw_cursor is not None else {}
    symbols_to_sync = {
        clean_symbol(item["symbol"])
        for item in new_income
        if item.get("symbol") and item["incomeType"] in ("REALIZED_PNL", "COMMISSION")
    }
    symbols_to_sync.update(clean_symbol(str(p["symbol"])) for p in active_positions_binance)
    if not existing_fills:
        symbols_to_sync.update(
            clean_symbol(item["symbol"])
            for item in existing_income
            if item.get("symbol") and item["incomeType"] in ("REALIZED_PNL", "COMMISSION")
        )

    sorted_symbols = sorted(s for s in symbols_to_sync if s)
    symbol_starts = {
        symbol: (
            max(start_ms, query_start, int(fills_cursor[symbol]) - 86400 * 1000)
            if symbol in fills_cursor
            else max(start_ms, query_start)
        )
        for symbol in sorted_symbols
    }

    new_fills: List[dict] = []
    if sorted_symbols:
        max_workers = min(USER_TRADES_MAX_WORKERS, len(sorted_symbols))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(_fetch_symbol_fills, client, symbol, symbol_starts[symbol], end_ms)
                for symbol in sorted_symbols
            ]
            for fut in futures:
                sym, sym_fills = fut.result()
                new_fills.extend(sym_fills)
                fills_cursor[sym] = end_ms

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
