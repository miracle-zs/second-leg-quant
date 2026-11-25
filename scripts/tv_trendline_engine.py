#!/usr/bin/env python3
"""
TradingView Cloud Drawing Reader & Logarithmic Trendline Engine
========================================================================

Extracts cloud drawing tools (LineToolTrendLine) from TradingView layouts via
charts-storage.tradingview.com and evaluates logarithmic trendline projections
with Binance Futures candlestick close confirmation.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
import urllib.parse

try:
    from binance_client import (
        BinanceAPIError,
        fetch_bytes_with_retry,
        fetch_json_with_retry,
        fetch_klines,
        get_closed_15m_candle,
    )
    from config import (
        BEIJING_TZ,
        DEFAULT_TV_LAYOUT_ID,
        RUNS_DIR,
        atomic_write_json,
        clean_symbol,
        file_lock,
        get_beijing_time_str,
    )
except ImportError:
    from scripts.binance_client import (
        BinanceAPIError,
        fetch_bytes_with_retry,
        fetch_json_with_retry,
        fetch_klines,
        get_closed_15m_candle,
    )
    from scripts.config import (
        BEIJING_TZ,
        DEFAULT_TV_LAYOUT_ID,
        RUNS_DIR,
        atomic_write_json,
        clean_symbol,
        file_lock,
        get_beijing_time_str,
    )

logger = logging.getLogger("tv_engine")

DEFAULT_LAYOUT_ID = DEFAULT_TV_LAYOUT_ID
TZ_SHANGHAI = BEIJING_TZ

TV_TOKEN_URLS = [
    "https://www.tradingview.com/chart-token/?image_url={layout_id}&user_id=-1",
    "https://cn.tradingview.com/chart-token/?image_url={layout_id}&user_id=-1",
]
TV_LAYOUT_PAGE_URL = "https://www.tradingview.com/chart/{layout_id}/"
TV_SOURCES_URL = "https://charts-storage.tradingview.com/charts-storage/get/layout/{layout_id}/sources"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_TOKEN_LOCK = threading.Lock()
_TOKEN_CACHE: dict[str, dict[str, Any]] = {}

_INTERVAL_SECONDS = {
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
}


def _extract_jwt_exp(token: str, fallback_exp: int) -> int:
    parts = token.split(".")
    if len(parts) != 3:
        return fallback_exp
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        exp = payload.get("exp")
        if isinstance(exp, (int, float)) and exp > 0:
            return int(exp)
    except Exception:
        pass
    return fallback_exp


def get_chart_token(layout_id: str = DEFAULT_LAYOUT_ID, force_refresh: bool = False) -> str:
    """Fetches and caches a TradingView chart JWT token for the given layout."""
    now = int(time.time())
    with _TOKEN_LOCK:
        cached = _TOKEN_CACHE.get(layout_id)
        if not force_refresh and cached and cached["exp"] > now + 60:
            return cached["token"]

    errors: list[str] = []
    for tmpl in TV_TOKEN_URLS:
        url = tmpl.format(layout_id=layout_id)
        try:
            data = fetch_json_with_retry(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=10,
                max_retries=2,
            )
            token = data.get("token") if isinstance(data, dict) else None
            if token:
                exp = _extract_jwt_exp(str(token), now + 1800)
                with _TOKEN_LOCK:
                    _TOKEN_CACHE[layout_id] = {"token": str(token), "exp": exp}
                logger.info("Successfully fetched JWT chart token for layout %s", layout_id)
                return str(token)
            errors.append(f"{url}: empty token")
        except Exception as e:
            errors.append(f"{url}: {e}")
            logger.warning("Failed to fetch token from %s: %s", url, e)

    raise RuntimeError(f"Unable to obtain chart token for TradingView layout {layout_id}: {'; '.join(errors)}")


def get_layout_chart_ids(layout_id: str = DEFAULT_LAYOUT_ID) -> list[str]:
    """Parses TradingView layout HTML to extract chart IDs associated with the layout."""
    chart_ids = ["_shared"]
    url = TV_LAYOUT_PAGE_URL.format(layout_id=layout_id)
    raw = fetch_bytes_with_retry(
        url,
        headers={"User-Agent": USER_AGENT},
        timeout=12,
        max_retries=2,
    )
    html = raw.decode("utf-8", errors="ignore")
    match = re.search(r"initData\.content\s*=\s*(\{.*?\});\s*initData\.", html, re.S)
    if not match:
        raise RuntimeError(f"Could not locate initData.content in TradingView layout HTML for {layout_id}")
    try:
        content = json.loads(match.group(1))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid initData.content JSON in layout {layout_id}: {e}") from e

    for c in content.get("charts", []):
        cid = c.get("chartId") if isinstance(c, dict) else None
        if cid and cid not in chart_ids:
            chart_ids.append(cid)
    return chart_ids


def _fetch_chart_sources(layout_id: str, chart_id: str, token: str) -> dict[str, Any]:
    query = urllib.parse.urlencode({"chart_id": chart_id, "jwt": token})
    url = f"{TV_SOURCES_URL.format(layout_id=layout_id)}?{query}"
    data = fetch_json_with_retry(
        url,
        headers={"User-Agent": USER_AGENT},
        timeout=15,
        max_retries=3,
    )
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected sources payload for chart_id '{chart_id}': {type(data).__name__}")
    payload = data.get("payload")
    if not isinstance(payload, dict):
        raise RuntimeError(f"Missing payload dict for chart_id '{chart_id}'")
    sources = payload.get("sources")
    if not isinstance(sources, dict):
        raise RuntimeError(f"Missing sources dict for chart_id '{chart_id}'")
    return sources


def get_layout_drawings(
    layout_id: str = DEFAULT_LAYOUT_ID,
    force_refresh: bool = False,
    save_cache: bool = True,
    tool_types: tuple[str, ...] = ("LineToolTrendLine",),
) -> dict[str, list[dict[str, Any]]]:
    """
    Fetches user drawings from TradingView cloud storage and normalizes them into
    canonical flat dicts indexed strictly by normalized symbol (e.g. 'YBUSDT').
    Fails closed if fetching any chart source fails (never overwrites cache with partial/empty data).
    """
    cache_path = RUNS_DIR / f"tradingview_drawings_{layout_id}.json"
    token = get_chart_token(layout_id=layout_id, force_refresh=force_refresh)
    chart_ids = get_layout_chart_ids(layout_id=layout_id)

    raw_sources: dict[str, dict[str, Any]] = {}
    retried_token = False

    for chart_id in chart_ids:
        try:
            sources = _fetch_chart_sources(layout_id, chart_id, token)
        except BinanceAPIError as e:
            if e.code in (401, 403) and not retried_token:
                retried_token = True
                token = get_chart_token(layout_id=layout_id, force_refresh=True)
                sources = _fetch_chart_sources(layout_id, chart_id, token)
            else:
                raise RuntimeError(f"Failed to fetch sources for chart_id '{chart_id}': {e}") from e

        for sid, sval in sources.items():
            if sid not in raw_sources and isinstance(sval, dict):
                raw_sources[sid] = sval
        logger.info("Retrieved %d sources from chart_id '%s'", len(sources), chart_id)

    symbol_to_trendlines: dict[str, list[dict[str, Any]]] = {}

    for sid, sdata in raw_sources.items():
        state_obj = sdata.get("state")
        if not isinstance(state_obj, dict):
            continue

        tool_type = state_obj.get("type")
        if tool_types and tool_type not in tool_types:
            continue

        inner_state = state_obj.get("state") if isinstance(state_obj.get("state"), dict) else {}
        raw_sym = sdata.get("symbol") or inner_state.get("symbol") or ""
        norm_sym = clean_symbol(raw_sym)
        if not norm_sym:
            continue

        points = state_obj.get("points") or []
        if len(points) < 2:
            continue

        pt1, pt2 = points[0], points[1]
        t1, p1 = pt1.get("time_t"), pt1.get("price")
        t2, p2 = pt2.get("time_t"), pt2.get("price")
        if t1 is None or p1 is None or t2 is None or p2 is None:
            continue

        try:
            p1_flt = float(p1)
            p2_flt = float(p2)
            t1_int = int(t1)
            t2_int = int(t2)
        except (ValueError, TypeError):
            continue

        dt1_str = get_beijing_time_str(t1_int)[:16]
        dt2_str = get_beijing_time_str(t2_int)[:16]
        line_iv = (
            inner_state.get("interval")
            or state_obj.get("interval")
            or (pt1.get("interval") if isinstance(pt1, dict) else None)
            or ""
        )

        drawing_meta: dict[str, Any] = {
            "id": sid,
            "type": tool_type,
            "symbol": norm_sym,
            "raw_symbol": raw_sym,
            "interval": str(line_iv).strip().upper(),
            "p1": p1_flt,
            "t1": t1_int,
            "p2": p2_flt,
            "t2": t2_int,
            "dt1_str": dt1_str,
            "dt2_str": dt2_str,
            "server_update_time": sdata.get("serverUpdateTime"),
            "extend_right": bool(inner_state.get("extendRight", True)),
            "line_color": str(inner_state.get("linecolor", "")),
            "line_width": int(inner_state.get("linewidth", 1)),
        }
        symbol_to_trendlines.setdefault(norm_sym, []).append(drawing_meta)

    if save_cache:
        cache_payload = {
            "layout_id": layout_id,
            "updated_at": get_beijing_time_str(),
            "total_drawings": len(raw_sources),
            "trendline_symbols_count": len(symbol_to_trendlines),
            "drawings_by_symbol": symbol_to_trendlines,
        }
        with file_lock(cache_path):
            atomic_write_json(cache_path, cache_payload)
        logger.info("Saved %d symbol drawing groups to cache: %s", len(symbol_to_trendlines), cache_path)

    return symbol_to_trendlines


def is_valid_15m_trendline(
    line: dict[str, Any],
    current_time: Optional[float] = None,
    max_age_hours: float = 72.0,
) -> bool:
    """
    Validates canonical trendline dict:
    1. Interval MUST explicitly be '15' or '15M'.
    2. Recency (server_update_time or t2) MUST exist and be within max_age_hours (default 72h).
    """
    if not isinstance(line, dict):
        return False

    iv = str(line.get("interval") or "").strip().upper()
    if iv not in ("15", "15M"):
        return False

    now_ts = current_time if current_time is not None else time.time()
    max_age_sec = max_age_hours * 3600.0

    recency_sec: Optional[float] = None
    s_time = line.get("server_update_time")
    if s_time is not None:
        try:
            val = float(s_time)
            recency_sec = val / 1000.0 if val > 1e11 else val
        except (TypeError, ValueError):
            recency_sec = None

    if recency_sec is None or recency_sec <= 0:
        t2 = line.get("t2")
        if t2 is not None:
            try:
                recency_sec = float(t2)
            except (TypeError, ValueError):
                recency_sec = None

    if recency_sec is None or recency_sec <= 0:
        return False
    return (now_ts - recency_sec) <= max_age_sec


def calculate_log_resistance(
    p1: float,
    t1: int | float,
    p2: float,
    t2: int | float,
    target_t: int | float,
) -> float:
    """
    Calculates exact Logarithmic coordinate projection price:
        P(t) = P1 * (P2 / P1) ** ((t - t1) / (t2 - t1))
    Raises ValueError on non-positive prices, degenerate vertical lines (t1 == t2),
    or extreme exponential projections (|log_val| > 50).
    """
    if p1 <= 0 or p2 <= 0:
        raise ValueError(f"Prices must be strictly positive for logarithmic projection: p1={p1}, p2={p2}")
    if t1 == t2:
        raise ValueError(f"Trendline timestamps must differ (t1 == t2 == {t1})")

    if p1 == p2 or target_t == t1:
        return float(p1)
    if target_t == t2:
        return float(p2)

    exponent = (target_t - t1) / (t2 - t1)
    log_val = math.log(p1) + exponent * math.log(p2 / p1)
    if abs(log_val) > 50.0:
        raise ValueError(f"Logarithmic projection out of bounds: log_val={log_val:.2f}")
    return float(math.exp(log_val))


def fetch_latest_closed_candle(
    symbol: str,
    interval: str = "15m",
    limit: int = 5,
) -> dict[str, Any]:
    """Fetches the latest confirmed closed candlestick via unified binance_client."""
    return get_closed_15m_candle(symbol, interval=interval, limit=limit)


def _interval_to_ms(interval: str) -> int:
    iv = interval.strip().lower()
    if len(iv) < 2 or iv[-1] not in _INTERVAL_SECONDS:
        raise ValueError(f"Unsupported kline interval: {interval}")
    count = int(iv[:-1])
    if count <= 0:
        raise ValueError(f"Invalid kline interval: {interval}")
    return count * _INTERVAL_SECONDS[iv[-1]] * 1000


def fetch_candle_at_time(
    symbol: str,
    target_time_s: int | float,
    interval: str = "15m",
) -> dict[str, Any]:
    """Fetches a specific historical candlestick covering the given timestamp."""
    clean_sym = clean_symbol(symbol)
    iv_ms = _interval_to_ms(interval)
    target_ms = int(target_time_s * 1000)
    klines = fetch_klines(
        clean_sym,
        interval=interval,
        limit=10,
        start_time_ms=target_ms - iv_ms,
        end_time_ms=target_ms + 2 * iv_ms,
    )

    for c in klines:
        o_ms, c_ms = int(c[0]), int(c[6])
        if o_ms <= target_ms <= c_ms or abs(o_ms - target_ms) < 60000:
            open_sec = o_ms // 1000
            close_sec = c_ms // 1000
            return {
                "symbol": clean_sym,
                "interval": interval,
                "open_time_ms": o_ms,
                "close_time_ms": c_ms,
                "open_time": open_sec,
                "close_time": close_sec,
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
                "volume": float(c[5]),
                "candle_time": get_beijing_time_str(open_sec)[:16],
                "is_closed": True,
            }

    raise ValueError(f"No candlestick found covering timestamp {target_time_s} for {clean_sym} ({interval})")


def check_symbol_breakout(
    symbol: str,
    trendline: dict[str, Any],
    candle: dict[str, Any] | None = None,
    target_time: int | float | None = None,
    at_close: bool = False,
    prev_candle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Evaluates strict logarithmic trendline crossover breakout:
    - prev_candle close <= prev log resistance AND current candle close > current log resistance.
    - Filters out runaway / extreme-decay lines (close > res_price * 1.25 or out-of-bounds exponent).
    Requires canonical flat trendline ('id', 'p1', 't1', 'p2', 't2') and 'prev_candle'.
    """
    clean_sym = clean_symbol(symbol)
    try:
        trendline_id = str(trendline["id"])
        p1 = float(trendline["p1"])
        t1 = int(trendline["t1"])
        p2 = float(trendline["p2"])
        t2 = int(trendline["t2"])
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"Invalid canonical trendline dict: {trendline}") from e

    if p1 <= 0 or p2 <= 0 or t1 == t2:
        raise ValueError(f"Invalid trendline anchor geometry: p1={p1}, t1={t1}, p2={p2}, t2={t2}")

    if candle is None:
        candle = fetch_latest_closed_candle(clean_sym, interval="15m")

    p_candle = prev_candle if prev_candle is not None else candle.get("prev_candle")
    if not isinstance(p_candle, dict):
        raise ValueError(f"prev_candle is required for strict crossover check ({clean_sym})")

    try:
        close_price = float(candle["close"])
        candle_ts = int(candle["open_time"])
        prev_close = float(p_candle["close"])
        prev_t = int(p_candle["open_time"])
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"Invalid canonical candle/prev_candle payload for {clean_sym}") from e

    if target_time is not None:
        eval_t = int(target_time)
    elif at_close:
        eval_t = int(candle["close_time"])
    else:
        eval_t = candle_ts

    candle_time_str = str(candle.get("candle_time") or get_beijing_time_str(candle_ts)[:16])

    try:
        res_price = calculate_log_resistance(p1=p1, t1=t1, p2=p2, t2=t2, target_t=eval_t)
        prev_res = calculate_log_resistance(p1=p1, t1=t1, p2=p2, t2=t2, target_t=prev_t)
    except ValueError:
        return {
            "is_breakout": False,
            "is_crossover": False,
            "is_runaway": True,
            "close_price": close_price,
            "res_price": 0.0,
            "prev_close": prev_close,
            "prev_res": None,
            "pct": 0.0,
            "candle_time": candle_time_str,
            "open_time": candle_ts,
            "target_t": eval_t,
            "trendline_id": trendline_id,
            "symbol": clean_sym,
            "open_price": candle.get("open"),
            "high_price": candle.get("high"),
            "low_price": candle.get("low"),
            "p1": p1,
            "t1": t1,
            "p2": p2,
            "t2": t2,
        }

    is_crossover = prev_close <= prev_res and close_price > res_price
    is_runaway = close_price > res_price * 1.25
    is_breakout = is_crossover and not is_runaway
    pct = round(((close_price - res_price) / res_price) * 100.0, 4)

    return {
        "is_breakout": is_breakout,
        "is_crossover": is_crossover,
        "is_runaway": is_runaway,
        "close_price": close_price,
        "res_price": res_price,
        "prev_close": prev_close,
        "prev_res": prev_res,
        "pct": pct,
        "candle_time": candle_time_str,
        "open_time": candle_ts,
        "target_t": eval_t,
        "trendline_id": trendline_id,
        "symbol": clean_sym,
        "open_price": candle.get("open"),
        "high_price": candle.get("high"),
        "low_price": candle.get("low"),
        "p1": p1,
        "t1": t1,
        "p2": p2,
        "t2": t2,
    }
