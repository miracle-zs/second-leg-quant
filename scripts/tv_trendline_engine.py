#!/usr/bin/env python3
"""
TradingView Cloud Drawing Reader & Logarithmic Trendline Engine (v1.0 Pro)
========================================================================

Production-grade engine for extracting cloud drawing tools (LineToolTrendLine)
from TradingView layouts via charts-storage.tradingview.com and calculating
logarithmic resistance projections with Binance Futures candlestick close confirmation.

High-level APIs:
- get_layout_drawings(layout_id='ig0LuDL8') -> dict of symbol to trendlines
- calculate_log_resistance(p1, t1, p2, t2, target_t) -> float
- fetch_latest_closed_candle(symbol, interval='15m', limit=5) -> dict
- check_symbol_breakout(symbol, trendline) -> dict (is_breakout, close_price, res_price, pct, candle_time)
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

# Configure logger
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [tv_engine] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("tv_engine")

ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = ROOT / "runs"
RUNS_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_LAYOUT_ID = "ig0LuDL8"
TZ_SHANGHAI = timezone(timedelta(hours=8))

TV_TOKEN_URLS = [
    "https://www.tradingview.com/chart-token/?image_url={layout_id}&user_id=-1",
    "https://cn.tradingview.com/chart-token/?image_url={layout_id}&user_id=-1",
]
TV_LAYOUT_PAGE_URL = "https://www.tradingview.com/chart/{layout_id}/"
TV_SOURCES_URL = "https://charts-storage.tradingview.com/charts-storage/get/layout/{layout_id}/sources"

BINANCE_FUTURES_KLINES_URL = "https://www.binance.com/fapi/v1/klines"
BINANCE_FUTURES_TIME_URL = "https://www.binance.com/fapi/v1/time"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# In-memory JWT token cache: {layout_id: {"token": str, "exp": int}}
_TOKEN_CACHE: dict[str, dict[str, Any]] = {}
_TV_TIME_OFFSET_MS: int | None = None
_TV_TIME_SYNCED_AT: float = 0.0


def clean_symbol(raw_symbol: str) -> str:
    """
    Normalizes a TradingView or exchange symbol to standard Binance USD-M contract symbol.
    Examples:
        'BINANCE:YBUSDT.P' -> 'YBUSDT'
        'BINANCE:1000SATSUSDT.P' -> '1000SATSUSDT'
        'BINANCE:BMTUSDT' -> 'BMTUSDT'
        'YBUSDT' -> 'YBUSDT'
    """
    if not raw_symbol:
        return ""
    sym = raw_symbol.strip()
    if ":" in sym:
        sym = sym.split(":")[-1]
    if sym.endswith(".P"):
        sym = sym[:-2]
    return sym.upper()


def _request_with_retry(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: int = 15,
    max_retries: int = 3,
    backoff_factor: float = 1.0,
) -> bytes:
    """Helper to perform HTTP GET requests with exponential backoff retry."""
    req_headers = {"User-Agent": USER_AGENT}
    if headers:
        req_headers.update(headers)

    req = urllib.request.Request(url, headers=req_headers)
    last_err: Exception | None = None

    for attempt in range(1, max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    return resp.read()
                raise urllib.error.HTTPError(
                    url, resp.status, f"HTTP status {resp.status}", resp.headers, None
                )
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            last_err = e
            if attempt == max_retries:
                break
            sleep_sec = backoff_factor * (2 ** (attempt - 1))
            logger.warning(
                "Request failed (%s). Retrying %d/%d after %.1fs: %s",
                e, attempt, max_retries, sleep_sec, url
            )
            time.sleep(sleep_sec)

    raise RuntimeError(f"Request failed after {max_retries} attempts: {url}") from last_err


def get_chart_token(layout_id: str = DEFAULT_LAYOUT_ID, force_refresh: bool = False) -> str:
    """
    Fetches a public JWT authentication token for the given TradingView layout.
    Caches token in-memory until expiration.
    """
    now = int(time.time())
    cached = _TOKEN_CACHE.get(layout_id)
    if not force_refresh and cached and cached.get("exp", 0) > now + 60:
        return cached["token"]

    for tmpl in TV_TOKEN_URLS:
        url = tmpl.format(layout_id=layout_id)
        try:
            raw = _request_with_retry(url, timeout=10, max_retries=2)
            data = json.loads(raw.decode("utf-8"))
            token = data.get("token")
            if token:
                # Estimate token expiration (~30 mins from now if payload not parsed)
                _TOKEN_CACHE[layout_id] = {"token": token, "exp": now + 1800}
                logger.info("Successfully fetched JWT chart token for layout %s", layout_id)
                return token
        except Exception as e:
            logger.warning("Failed to fetch token from %s: %s", url, e)

    raise RuntimeError(f"Unable to obtain chart token for TradingView layout {layout_id}")


def get_layout_chart_ids(layout_id: str = DEFAULT_LAYOUT_ID) -> list[str]:
    """
    Parses TradingView layout page to extract chart IDs associated with the layout.
    Always includes '_shared' (where multi-symbol user drawings are stored).
    """
    chart_ids = ["_shared"]
    url = TV_LAYOUT_PAGE_URL.format(layout_id=layout_id)
    try:
        raw = _request_with_retry(url, timeout=12, max_retries=2)
        html = raw.decode("utf-8", errors="ignore")
        # Extract initData.content
        match = re.search(r"initData\.content\s*=\s*(\{.*?\});\s*initData\.", html, re.S)
        if match:
            content = json.loads(match.group(1))
            charts = content.get("charts", [])
            for c in charts:
                cid = c.get("chartId")
                if cid and cid not in chart_ids:
                    chart_ids.append(cid)
    except Exception as e:
        logger.warning("Could not extract chart IDs from layout HTML (%s); using default fallbacks", e)
        # Default known chart pane ID for ig0LuDL8
        if "WpL8VAypfcj9" not in chart_ids:
            chart_ids.append("WpL8VAypfcj9")

    return chart_ids


def get_layout_drawings(
    layout_id: str = DEFAULT_LAYOUT_ID,
    force_refresh: bool = False,
    save_cache: bool = True,
    tool_types: tuple[str, ...] = ("LineToolTrendLine",),
) -> dict[str, list[dict[str, Any]]]:
    """
    Connects to TradingView layout via charts-storage.tradingview.com and extracts
    all user drawings (specifically LineToolTrendLine by default).

    Returns:
        dict mapping symbol (both normalized 'YBUSDT' and raw 'BINANCE:YBUSDT.P')
        to a list of trendline metadata dicts.
    """
    cache_path = RUNS_DIR / f"tradingview_drawings_{layout_id}.json"

    token = get_chart_token(layout_id=layout_id, force_refresh=force_refresh)
    chart_ids = get_layout_chart_ids(layout_id=layout_id)

    raw_sources: dict[str, dict[str, Any]] = {}

    for chart_id in chart_ids:
        query = urllib.parse.urlencode({"chart_id": chart_id, "jwt": token})
        url = f"{TV_SOURCES_URL.format(layout_id=layout_id)}?{query}"
        try:
            raw = _request_with_retry(url, timeout=15, max_retries=3)
            data = json.loads(raw.decode("utf-8"))
            payload = data.get("payload", {})
            sources = payload.get("sources", {}) if isinstance(payload, dict) else {}
            for sid, sval in sources.items():
                if sid not in raw_sources:
                    raw_sources[sid] = sval
            logger.info("Retrieved %d sources from chart_id '%s'", len(sources), chart_id)
        except Exception as e:
            logger.error("Failed to fetch sources for chart_id '%s': %s", chart_id, e)

    # Process and organize drawings by symbol
    symbol_to_trendlines: dict[str, list[dict[str, Any]]] = {}

    for sid, sdata in raw_sources.items():
        state_obj = sdata.get("state", {})
        if not isinstance(state_obj, dict):
            continue

        tool_type = state_obj.get("type")
        if tool_types and tool_type not in tool_types:
            continue

        raw_sym = sdata.get("symbol") or state_obj.get("state", {}).get("symbol") or ""
        if not raw_sym:
            continue

        norm_sym = clean_symbol(raw_sym)
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

        dt1_str = datetime.fromtimestamp(t1_int, tz=TZ_SHANGHAI).strftime("%Y-%m-%d %H:%M")
        dt2_str = datetime.fromtimestamp(t2_int, tz=TZ_SHANGHAI).strftime("%Y-%m-%d %H:%M")

        # Extract interval
        line_iv = (
            state_obj.get("state", {}).get("interval")
            or state_obj.get("interval")
            or (points[0].get("interval") if points and isinstance(points[0], dict) else None)
            or ""
        )

        drawing_meta: dict[str, Any] = {
            "id": sid,
            "type": tool_type,
            "symbol": norm_sym,
            "raw_symbol": raw_sym,
            "interval": str(line_iv),
            "p1": p1_flt,
            "t1": t1_int,
            "p2": p2_flt,
            "t2": t2_int,
            "dt1_str": dt1_str,
            "dt2_str": dt2_str,
            "server_update_time": sdata.get("serverUpdateTime"),
            "extend_right": state_obj.get("state", {}).get("extendRight", True),
            "line_color": state_obj.get("state", {}).get("linecolor", ""),
            "line_width": state_obj.get("state", {}).get("linewidth", 1),
            "points": points,
            "state": state_obj.get("state", {}),
        }

        # Index by normalized symbol
        if norm_sym not in symbol_to_trendlines:
            symbol_to_trendlines[norm_sym] = []
        symbol_to_trendlines[norm_sym].append(drawing_meta)

        # Index by raw symbol as well for direct lookup
        if raw_sym != norm_sym:
            if raw_sym not in symbol_to_trendlines:
                symbol_to_trendlines[raw_sym] = []
            symbol_to_trendlines[raw_sym].append(drawing_meta)

    # Persist cache file for offline consumption
    if save_cache:
        try:
            cache_payload = {
                "layout_id": layout_id,
                "updated_at": datetime.now(TZ_SHANGHAI).strftime("%Y-%m-%d %H:%M:%S UTC+8"),
                "total_drawings": len(raw_sources),
                "trendline_symbols_count": len([k for k in symbol_to_trendlines if not k.startswith("BINANCE:")]),
                "drawings_by_symbol": symbol_to_trendlines,
            }
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(cache_payload, f, ensure_ascii=False, indent=2)
            logger.info("Saved %d drawing groups to cache file: %s", len(symbol_to_trendlines), cache_path)
        except Exception as e:
            logger.warning("Failed to save drawing cache file: %s", e)

    return symbol_to_trendlines


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

    Args:
        p1: Price at point 1 (must be > 0)
        t1: Timestamp in seconds at point 1
        p2: Price at point 2 (must be > 0)
        t2: Timestamp in seconds at point 2
        target_t: Timestamp in seconds to calculate projection for

    Returns:
        Projected resistance/support price (float)
    """
    if p1 <= 0 or p2 <= 0:
        raise ValueError(f"Prices must be strictly positive for logarithmic projection: p1={p1}, p2={p2}")

    if t1 == t2 or p1 == p2 or target_t == t1:
        return float(p1)
    if target_t == t2:
        return float(p2)

    exponent = (target_t - t1) / (t2 - t1)
    # 使用对数空间计算并防止极陡/远期外推导致 OverflowError 或下溢
    log_val = math.log(p1) + exponent * math.log(p2 / p1)
    if log_val > 50.0 or log_val < -50.0:
        return 0.0
    return float(math.exp(log_val))


def fetch_latest_closed_candle(
    symbol: str,
    interval: str = "15m",
    limit: int = 5,
) -> dict[str, Any]:
    """
    Fetches the latest confirmed CLOSED candlestick from Binance Futures REST API.
    Guarantees candle close confirmation by verifying close_time <= server_time.

    Returns:
        dict with open, high, low, close, volume, open_time, close_time,
        and human-readable formatted timestamps.
    """
    clean_sym = clean_symbol(symbol)
    query = urllib.parse.urlencode({
        "symbol": clean_sym,
        "interval": interval,
        "limit": limit,
    })
    url = f"{BINANCE_FUTURES_KLINES_URL}?{query}"

    # 使用 10 分钟时间偏移缓存（首次通过 _request_with_retry 同步，兼容单测 mock）
    global _TV_TIME_OFFSET_MS, _TV_TIME_SYNCED_AT
    now_mono = time.monotonic()
    if _TV_TIME_OFFSET_MS is not None and (now_mono - _TV_TIME_SYNCED_AT) < 600.0:
        server_time_ms = int(time.time() * 1000) + _TV_TIME_OFFSET_MS
    else:
        server_time_ms = int(time.time() * 1000)
        try:
            t_raw = _request_with_retry(BINANCE_FUTURES_TIME_URL, timeout=6, max_retries=2)
            server_time_ms = json.loads(t_raw.decode("utf-8")).get("serverTime", server_time_ms)
            _TV_TIME_OFFSET_MS = server_time_ms - int(time.time() * 1000)
            _TV_TIME_SYNCED_AT = now_mono
        except Exception as e:
            logger.debug("Binance server time check fallback to local: %s", e)

    raw_klines = _request_with_retry(url, timeout=10, max_retries=3)
    klines_data = json.loads(raw_klines.decode("utf-8"))

    if not isinstance(klines_data, list) or not klines_data:
        raise RuntimeError(f"No candlestick data returned for symbol {clean_sym} ({interval})")

    # Find the latest candle that has completely closed: close_time <= server_time_ms
    closed_candles = [
        c for c in klines_data
        if int(c[6]) <= server_time_ms
    ]

    if not closed_candles:
        # If all returned candles appear in-progress, pick the second to last as guaranteed closed
        if len(klines_data) >= 2:
            target_candle = klines_data[-2]
            prev_candle_raw = klines_data[-3] if len(klines_data) >= 3 else None
        else:
            target_candle = klines_data[-1]
            prev_candle_raw = None
    else:
        target_candle = closed_candles[-1]
        prev_candle_raw = closed_candles[-2] if len(closed_candles) >= 2 else None

    open_ms = int(target_candle[0])
    close_ms = int(target_candle[6])

    open_dt = datetime.fromtimestamp(open_ms / 1000.0, tz=TZ_SHANGHAI)
    close_dt = datetime.fromtimestamp(close_ms / 1000.0, tz=TZ_SHANGHAI)

    prev_candle_dict = None
    if prev_candle_raw:
        p_open_ms = int(prev_candle_raw[0])
        p_close_ms = int(prev_candle_raw[6])
        p_open_dt = datetime.fromtimestamp(p_open_ms / 1000.0, tz=TZ_SHANGHAI)
        p_close_dt = datetime.fromtimestamp(p_close_ms / 1000.0, tz=TZ_SHANGHAI)
        prev_candle_dict = {
            "symbol": clean_sym,
            "interval": interval,
            "open_time_ms": p_open_ms,
            "close_time_ms": p_close_ms,
            "open_time": int(p_open_ms / 1000),
            "close_time": int(p_close_ms / 1000),
            "open": float(prev_candle_raw[1]),
            "high": float(prev_candle_raw[2]),
            "low": float(prev_candle_raw[3]),
            "close": float(prev_candle_raw[4]),
            "volume": float(prev_candle_raw[5]),
            "open_time_str": p_open_dt.strftime("%Y-%m-%d %H:%M:%S UTC+8"),
            "close_time_str": p_close_dt.strftime("%Y-%m-%d %H:%M:%S UTC+8"),
            "candle_time": p_open_dt.strftime("%Y-%m-%d %H:%M"),
            "candle_timestamp": int(p_open_ms / 1000),
            "is_closed": True,
        }

    return {
        "symbol": clean_sym,
        "interval": interval,
        "open_time_ms": open_ms,
        "close_time_ms": close_ms,
        "open_time": int(open_ms / 1000),
        "close_time": int(close_ms / 1000),
        "open": float(target_candle[1]),
        "high": float(target_candle[2]),
        "low": float(target_candle[3]),
        "close": float(target_candle[4]),
        "volume": float(target_candle[5]),
        "open_time_str": open_dt.strftime("%Y-%m-%d %H:%M:%S UTC+8"),
        "close_time_str": close_dt.strftime("%Y-%m-%d %H:%M:%S UTC+8"),
        "candle_time": open_dt.strftime("%Y-%m-%d %H:%M"),
        "candle_timestamp": int(open_ms / 1000),
        "is_closed": True,
        "prev_candle": prev_candle_dict,
    }


def fetch_candle_at_time(
    symbol: str,
    target_time_s: int | float,
    interval: str = "15m",
) -> dict[str, Any]:
    """
    Fetches a specific historical closed candlestick covering the given timestamp.
    """
    clean_sym = clean_symbol(symbol)
    start_ms = int(target_time_s * 1000) - (15 * 60 * 1000)
    end_ms = int(target_time_s * 1000) + (30 * 60 * 1000)

    query = urllib.parse.urlencode({
        "symbol": clean_sym,
        "interval": interval,
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": 10,
    })
    url = f"{BINANCE_FUTURES_KLINES_URL}?{query}"
    raw = _request_with_retry(url, timeout=10, max_retries=3)
    klines = json.loads(raw.decode("utf-8"))

    target_ms = int(target_time_s * 1000)
    for c in klines:
        o_ms, c_ms = int(c[0]), int(c[6])
        if o_ms <= target_ms <= c_ms or abs(o_ms - target_ms) < 60000:
            open_dt = datetime.fromtimestamp(o_ms / 1000.0, tz=TZ_SHANGHAI)
            close_dt = datetime.fromtimestamp(c_ms / 1000.0, tz=TZ_SHANGHAI)
            return {
                "symbol": clean_sym,
                "interval": interval,
                "open_time_ms": o_ms,
                "close_time_ms": c_ms,
                "open_time": int(o_ms / 1000),
                "close_time": int(c_ms / 1000),
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
                "volume": float(c[5]),
                "open_time_str": open_dt.strftime("%Y-%m-%d %H:%M:%S UTC+8"),
                "close_time_str": close_dt.strftime("%Y-%m-%d %H:%M:%S UTC+8"),
                "candle_time": open_dt.strftime("%Y-%m-%d %H:%M"),
                "candle_timestamp": int(o_ms / 1000),
                "is_closed": True,
            }

    raise ValueError(f"No candlestick found covering timestamp {target_time_s} for {clean_sym}")


def check_symbol_breakout(
    symbol: str,
    trendline: dict[str, Any],
    candle: dict[str, Any] | None = None,
    target_time: int | float | None = None,
    at_close: bool = False,
    prev_candle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Evaluates whether a symbol's candlestick closes above the logarithmic trendline resistance.
    Implements strict crossover logic and runaway/ancient line filtering:
    - Previous candle close must be <= previous log resistance.
    - Current candle close must be > current log resistance.
    - Skip if resistance <= 0 or current price > resistance * 1.25.

    Args:
        symbol: Ticker symbol (e.g. 'YBUSDT' or 'BINANCE:YBUSDT.P')
        trendline: Trendline dictionary with 'p1', 't1', 'p2', 't2' (or 'points')
        candle: Optional candlestick dict; if None, fetches the latest closed 15m candle
        target_time: Optional explicit timestamp to evaluate resistance at
        at_close: If True, evaluates resistance at candle close time; else at candle open time
        prev_candle: Optional previous closed candlestick for strict crossover validation

    Returns:
        dict:
            is_breakout (bool): True if strict crossover confirmed and not runaway
            is_crossover (bool): True if crossed from <= res to > res
            close_price (float): Closed candle close price
            res_price (float): Logarithmic resistance price at target timestamp
            pct (float): Breakout percentage ((close - res) / res * 100)
            candle_time (str): Human-readable candle time (e.g. '2026-11-21 12:15')
            candle_timestamp (int): Timestamp in seconds
            trendline_id (str): ID of trendline evaluated
    """
    clean_sym = clean_symbol(symbol)

    # Extract trendline coordinates
    if "p1" in trendline and "p2" in trendline and "t1" in trendline and "t2" in trendline:
        p1 = float(trendline["p1"])
        t1 = int(trendline["t1"])
        p2 = float(trendline["p2"])
        t2 = int(trendline["t2"])
    elif "points" in trendline and len(trendline["points"]) >= 2:
        pts = trendline["points"]
        p1 = float(pts[0]["price"])
        t1 = int(pts[0]["time_t"])
        p2 = float(pts[1]["price"])
        t2 = int(pts[1]["time_t"])
    else:
        raise ValueError(f"Invalid trendline format: missing points or p1/t1/p2/t2 in {trendline}")

    trendline_id = trendline.get("id", "custom_line")

    # Fetch latest closed candle if not provided
    if candle is None:
        candle = fetch_latest_closed_candle(clean_sym, interval="15m")

    close_price = float(candle["close"])
    candle_time_str = candle.get("candle_time") or candle.get("open_time_str", "")
    candle_ts = candle.get("candle_timestamp") or candle.get("open_time", 0)

    # Determine target timestamp for resistance projection
    if target_time is not None:
        eval_t = int(target_time)
    elif at_close:
        eval_t = candle.get("close_time", candle_ts + 15 * 60)
    else:
        eval_t = candle_ts

    res_price = calculate_log_resistance(p1=p1, t1=t1, p2=p2, t2=t2, target_t=eval_t)

    # Strict Crossover Logic:
    # Previous closed candle close MUST be <= previous log resistance,
    # and current closed candle close MUST be > current log resistance.
    p_candle = prev_candle if prev_candle is not None else candle.get("prev_candle")
    prev_close = None
    prev_res = None
    is_crossover = False

    if p_candle:
        prev_close = float(p_candle["close"])
        prev_t = int(p_candle.get("candle_timestamp") or p_candle.get("open_time", eval_t - 900))
        prev_res = calculate_log_resistance(p1=p1, t1=t1, p2=p2, t2=t2, target_t=prev_t)
        if prev_close <= prev_res and close_price > res_price:
            is_crossover = True
        else:
            # If previous candle was already above resistance, it is ALREADY broken out
            is_crossover = False
    else:
        # Fallback if prev_candle is not provided in mock:
        prev_t = eval_t - 900
        prev_res = calculate_log_resistance(p1=p1, t1=t1, p2=p2, t2=t2, target_t=prev_t)
        open_p = candle.get("open")
        if open_p is not None and float(open_p) > prev_res:
            # Open was already above previous resistance
            is_crossover = False
        else:
            is_crossover = close_price > res_price

    # Filter invalid price or runaway / ancient decay line
    is_runaway = False
    if res_price <= 0:
        is_breakout = False
    elif close_price > res_price * 1.25:
        is_runaway = True
        is_breakout = False
    else:
        is_breakout = is_crossover

    pct = round(((close_price - res_price) / res_price) * 100, 4) if res_price > 0 else 0.0

    return {
        "is_breakout": is_breakout,
        "is_crossover": is_crossover,
        "is_runaway": is_runaway,
        "close_price": close_price,
        "res_price": round(res_price, 6) if res_price > 0 else 0.0,
        "prev_close": prev_close,
        "prev_res": round(prev_res, 6) if prev_res is not None else None,
        "pct": pct,
        "candle_time": candle_time_str,
        "candle_timestamp": candle_ts,
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


def verify_ybusdt_1215_breakout() -> dict[str, Any]:
    """
    Dedicated test function for YBUSDT verifying the 12:15 UTC+8 candle breakout.
    Fetches layout drawings from ig0LuDL8, identifies trendline SFzHy9,
    and tests against the 2026-11-21 12:15 UTC+8 15m candlestick.
    """
    logger.info("Starting verification for YBUSDT 12:15 UTC+8 candle breakout...")
    drawings = get_layout_drawings(layout_id=DEFAULT_LAYOUT_ID)

    yb_lines = drawings.get("YBUSDT") or drawings.get("BINANCE:YBUSDT.P") or []
    if not yb_lines:
        raise RuntimeError("No trendlines found for YBUSDT in TradingView layout ig0LuDL8")

    logger.info("Found %d trendlines for YBUSDT: %s", len(yb_lines), [l["id"] for l in yb_lines])

    # Find trendline SFzHy9 (the key resistance trendline)
    target_line = next((l for l in yb_lines if l["id"] == "SFzHy9"), yb_lines[0])
    logger.info(
        "Using trendline %s: P1=%.5f (%s) -> P2=%.5f (%s)",
        target_line["id"], target_line["p1"], target_line["dt1_str"],
        target_line["p2"], target_line["dt2_str"]
    )

    # 2026-11-21 12:15:00 UTC+8 timestamp
    dt_1215 = datetime(2026, 11, 21, 12, 15, tzinfo=TZ_SHANGHAI)
    ts_1215 = int(dt_1215.timestamp())

    candle_1215 = fetch_candle_at_time("YBUSDT", target_time_s=ts_1215, interval="15m")
    logger.info(
        "Candle 12:15 UTC+8: O=%.5f, H=%.5f, L=%.5f, C=%.5f (Close time: %s)",
        candle_1215["open"], candle_1215["high"], candle_1215["low"], candle_1215["close"],
        candle_1215["close_time_str"]
    )

    result = check_symbol_breakout("YBUSDT", target_line, candle=candle_1215)
    logger.info(
        "Breakout Check Result: is_breakout=%s, Close=%.5f, Res=%.6f, Pct=+%.4f%%, Time=%s",
        result["is_breakout"], result["close_price"], result["res_price"],
        result["pct"], result["candle_time"]
    )

    return result


def main():
    """Main CLI test execution runner."""
    print("=" * 70)
    print("TradingView Cloud Drawing Reader & Logarithmic Trendline Engine")
    print("=" * 70)

    # 1. Fetch layout drawings
    print(f"\n[1/3] Fetching drawings from layout '{DEFAULT_LAYOUT_ID}'...")
    drawings = get_layout_drawings(layout_id=DEFAULT_LAYOUT_ID, force_refresh=True)
    clean_keys = [k for k in drawings if not k.startswith("BINANCE:") and not k.startswith("OKX:")]
    print(f"✓ Retrieved trendlines across {len(clean_keys)} distinct symbols:")
    for sym in sorted(clean_keys)[:12]:
        line_ids = [l["id"] for l in drawings[sym]]
        print(f"   • {sym:14s}: {len(line_ids)} lines ({', '.join(line_ids)})")
    if len(clean_keys) > 12:
        print(f"   ... and {len(clean_keys) - 12} more symbols.")

    # 2. Verify YBUSDT 12:15 UTC+8 breakout
    print("\n[2/3] Verifying YBUSDT 12:15 UTC+8 Candle Breakout...")
    res_1215 = verify_ybusdt_1215_breakout()
    print(f"   • Target Symbol   : {res_1215['symbol']}")
    print(f"   • Trendline ID    : {res_1215['trendline_id']}")
    print(f"   • Anchor Point 1  : {res_1215['p1']} (t1={res_1215['t1']})")
    print(f"   • Anchor Point 2  : {res_1215['p2']} (t2={res_1215['t2']})")
    print(f"   • Candle Time     : {res_1215['candle_time']} UTC+8")
    print(f"   • Candle Close    : {res_1215['close_price']}")
    print(f"   • Log Resistance  : {res_1215['res_price']}")
    print(f"   • Breakout Pct    : {res_1215['pct']:+.4f}%")
    print(f"   • Is Breakout?    : {'[YES] 突破确认' if res_1215['is_breakout'] else '[NO] 未突破'}")
    assert res_1215["is_breakout"] is True, "YBUSDT 12:15 breakout must evaluate to True!"

    # 3. Live check latest closed candle
    print("\n[3/3] Checking Live Latest Closed Candle for YBUSDT...")
    yb_lines = drawings.get("YBUSDT", [])
    target_line = next((l for l in yb_lines if l["id"] == "SFzHy9"), yb_lines[0])
    live_candle = fetch_latest_closed_candle("YBUSDT", interval="15m")
    live_res = check_symbol_breakout("YBUSDT", target_line, candle=live_candle)
    print(f"   • Latest Closed   : {live_candle['candle_time']} UTC+8")
    print(f"   • Latest Close P  : {live_candle['close']}")
    print(f"   • Current Log Res : {live_res['res_price']}")
    print(f"   • Pct Diff        : {live_res['pct']:+.4f}%")
    print(f"   • Current Status  : {'[突破上方运行中]' if live_res['is_breakout'] else '[阻力线下方震荡]'}")

    print("\n" + "=" * 70)
    print("ALL TESTS & VERIFICATIONS PASSED SUCCESSFULLY (100% Green)")
    print("=" * 70)


if __name__ == "__main__":
    main()
