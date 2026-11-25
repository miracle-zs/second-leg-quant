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
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Optional

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from binance_client import get_closed_15m_candle  # noqa: E402
from config import (  # noqa: E402
    DEFAULT_TV_LAYOUT_ID,
    RUNS_DIR,
    atomic_write_json,
    clean_symbol,
    file_lock,
    get_beijing_time_str,
)
from http_client import HttpError, request_bytes, request_json  # noqa: E402

logger = logging.getLogger("tv_engine")

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
_TOKEN_FETCH_LOCKS: dict[str, threading.Lock] = {}


def _extract_jwt_exp(token: str) -> int:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError(f"Invalid JWT token format: expected 3 segments, got {len(parts)}")
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Corrupt JWT payload: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"JWT payload must be a JSON object, got {type(payload).__name__}")
    exp = payload.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, (int, float)) or not math.isfinite(exp) or exp <= 0:
        raise ValueError(f"JWT missing valid positive numeric exp: {exp!r}")
    return int(exp)


def _normalize_server_update_time(raw_val: object) -> Optional[int]:
    """Normalize TradingView serverUpdateTime (seconds or ms) into integer epoch seconds."""
    if raw_val is None:
        return None
    if not isinstance(raw_val, (int, float, str)) or isinstance(raw_val, bool):
        raise ValueError(f"Invalid serverUpdateTime type: {raw_val!r}")
    val = float(raw_val)
    if not math.isfinite(val) or val <= 0:
        raise ValueError(f"Invalid non-positive serverUpdateTime: {raw_val!r}")
    return int(val // 1000) if val > 1e11 else int(val)


def get_chart_token(layout_id: str = DEFAULT_TV_LAYOUT_ID, force_refresh: bool = False) -> str:
    """Fetches and caches a TradingView chart JWT token for the given layout (single-flight per layout)."""
    now = int(time.time())
    with _TOKEN_LOCK:
        cached = _TOKEN_CACHE.get(layout_id)
        cached_at = int(cached.get("fetched_at", 0)) if cached else 0
        if not force_refresh and cached and cached["exp"] > now + 60:
            return cached["token"]
        fetch_lock = _TOKEN_FETCH_LOCKS.setdefault(layout_id, threading.Lock())

    with fetch_lock:
        now = int(time.time())
        with _TOKEN_LOCK:
            cached = _TOKEN_CACHE.get(layout_id)
            if cached and cached["exp"] > now + 60:
                if not force_refresh or int(cached.get("fetched_at", 0)) > cached_at:
                    return cached["token"]

        errors: list[str] = []
        for tmpl in TV_TOKEN_URLS:
            url = tmpl.format(layout_id=layout_id)
            try:
                data = request_json(
                    url,
                    headers={"User-Agent": USER_AGENT},
                    timeout=10.0,
                    max_retries=1,
                )
                if not isinstance(data, dict) or not isinstance(data.get("token"), str) or not data["token"].strip():
                    raise ValueError(f"Missing/invalid token field in response: {data!r}")
                token = data["token"].strip()
                exp = _extract_jwt_exp(token)
                with _TOKEN_LOCK:
                    _TOKEN_CACHE[layout_id] = {
                        "token": token,
                        "exp": exp,
                        "fetched_at": now,
                    }
                logger.info("Successfully fetched JWT chart token for layout %s", layout_id)
                return token
            except (HttpError, ValueError) as e:
                errors.append(f"{url}: {e}")
                logger.warning("Failed to fetch token from %s: %s", url, e)

        raise RuntimeError(f"Unable to obtain chart token for TradingView layout {layout_id}: {'; '.join(errors)}")


def get_layout_chart_ids(layout_id: str = DEFAULT_TV_LAYOUT_ID) -> list[str]:
    """Parses TradingView layout HTML to extract chart IDs associated with the layout."""
    chart_ids = ["_shared"]
    url = TV_LAYOUT_PAGE_URL.format(layout_id=layout_id)
    try:
        raw = request_bytes(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=12.0,
            max_retries=1,
        )
    except HttpError as e:
        raise RuntimeError(f"Failed to fetch TradingView layout page {layout_id}: {e}") from e

    try:
        html = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise RuntimeError(f"Invalid UTF-8 in TradingView layout HTML for {layout_id}: {e}") from e
    match = re.search(r"initData\.content\s*=\s*(\{.*?\});\s*initData\.", html, re.S)
    if not match:
        raise RuntimeError(f"Could not locate initData.content in TradingView layout HTML for {layout_id}")
    try:
        content = json.loads(match.group(1))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid initData.content JSON in layout {layout_id}: {e}") from e

    if not isinstance(content, dict) or not isinstance(content.get("charts"), list):
        raise RuntimeError(f"Missing 'charts' list in initData.content for layout {layout_id}")
    for c in content["charts"]:
        if not isinstance(c, dict) or not isinstance(c.get("chartId"), str) or not c["chartId"].strip():
            raise RuntimeError(f"Malformed chart entry in initData.content for layout {layout_id}: {c!r}")
        cid = c["chartId"].strip()
        if cid not in chart_ids:
            chart_ids.append(cid)
    return chart_ids


def _fetch_chart_sources(layout_id: str, chart_id: str, token: str) -> dict[str, Any]:
    query = urllib.parse.urlencode({"chart_id": chart_id, "jwt": token})
    url = f"{TV_SOURCES_URL.format(layout_id=layout_id)}?{query}"
    data = request_json(
        url,
        headers={"User-Agent": USER_AGENT},
        timeout=15.0,
        max_retries=2,
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


def _read_cached_drawings(cache_path: Path, max_age_sec: float) -> Optional[dict[str, list[dict[str, Any]]]]:
    if not cache_path.exists():
        return None
    if (time.time() - cache_path.stat().st_mtime) >= max_age_sec:
        return None
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read TV drawings cache %s: %s", cache_path, exc)
        return None
    drawings = payload.get("drawings_by_symbol") if isinstance(payload, dict) else None
    if isinstance(drawings, dict) and drawings:
        return drawings
    return None


def get_layout_drawings(
    layout_id: str = DEFAULT_TV_LAYOUT_ID,
    force_refresh: bool = False,
    save_cache: bool = True,
    *,
    cache_ttl_sec: Optional[float] = None,
) -> dict[str, list[dict[str, Any]]]:
    """
    Fetches LineToolTrendLine drawings from TradingView cloud storage and normalizes
    them into canonical flat dicts indexed strictly by normalized symbol (e.g. 'YBUSDT').
    When `cache_ttl_sec` is set and `force_refresh` is False, returns fresh disk cache.
    `force_refresh=True` bypasses disk cache and refreshes the JWT token.
    Fails closed if fetching any chart source fails or all trendlines fail parsing.
    """
    cache_path = RUNS_DIR / f"tradingview_drawings_{layout_id}.json"
    if not force_refresh and cache_ttl_sec is not None and cache_ttl_sec > 0:
        cached_drawings = _read_cached_drawings(cache_path, cache_ttl_sec)
        if cached_drawings is not None:
            return cached_drawings

    token = get_chart_token(layout_id=layout_id, force_refresh=force_refresh)
    chart_ids = get_layout_chart_ids(layout_id=layout_id)

    raw_sources: dict[str, dict[str, Any]] = {}
    retried_token = False

    for chart_id in chart_ids:
        try:
            sources = _fetch_chart_sources(layout_id, chart_id, token)
        except HttpError as e:
            if e.status_code in (401, 403) and not retried_token:
                retried_token = True
                token = get_chart_token(layout_id=layout_id, force_refresh=True)
                try:
                    sources = _fetch_chart_sources(layout_id, chart_id, token)
                except HttpError as retry_err:
                    raise RuntimeError(
                        f"Failed to fetch sources for chart_id '{chart_id}': {retry_err}"
                    ) from retry_err
            else:
                raise RuntimeError(f"Failed to fetch sources for chart_id '{chart_id}': {e}") from e

        for sid, sval in sources.items():
            if not isinstance(sval, dict):
                raise RuntimeError(
                    f"Malformed source entry {sid!r} in chart_id '{chart_id}': expected dict, got {type(sval).__name__}"
                )
            if sid not in raw_sources:
                raw_sources[sid] = sval
        logger.info("Retrieved %d sources from chart_id '%s'", len(sources), chart_id)

    symbol_to_trendlines: dict[str, list[dict[str, Any]]] = {}
    trendline_seen_count = 0
    trendline_skipped_count = 0

    for sid, sdata in raw_sources.items():
        state_obj = sdata.get("state")
        if not isinstance(state_obj, dict):
            continue

        tool_type = state_obj.get("type")
        if tool_type != "LineToolTrendLine":
            continue
        trendline_seen_count += 1

        raw_inner_state = state_obj.get("state")
        if raw_inner_state is not None and not isinstance(raw_inner_state, dict):
            trendline_skipped_count += 1
            logger.warning("Skipping trendline %s with invalid inner state type %s", sid, type(raw_inner_state).__name__)
            continue
        inner_state = raw_inner_state if raw_inner_state is not None else {}
        raw_sym = sdata.get("symbol") or inner_state.get("symbol") or ""
        norm_sym = clean_symbol(raw_sym)
        if not norm_sym:
            trendline_skipped_count += 1
            logger.warning("Skipping trendline %s with empty/invalid symbol: %r", sid, raw_sym)
            continue

        points = state_obj.get("points") or []
        if not isinstance(points, list) or len(points) < 2:
            trendline_skipped_count += 1
            logger.warning("Skipping trendline %s (%s) with invalid points: %r", sid, norm_sym, points)
            continue

        pt1, pt2 = points[0], points[1]
        if not isinstance(pt1, dict) or not isinstance(pt2, dict):
            trendline_skipped_count += 1
            logger.warning("Skipping trendline %s (%s) with non-dict points", sid, norm_sym)
            continue
        t1, p1 = pt1.get("time_t"), pt1.get("price")
        t2, p2 = pt2.get("time_t"), pt2.get("price")
        if t1 is None or p1 is None or t2 is None or p2 is None:
            trendline_skipped_count += 1
            logger.warning("Skipping trendline %s (%s) with missing time_t/price", sid, norm_sym)
            continue

        raw_linewidth = inner_state.get("linewidth")
        try:
            p1_flt = float(p1)
            p2_flt = float(p2)
            t1_int = int(t1)
            t2_int = int(t2)
            line_width = int(raw_linewidth) if raw_linewidth is not None else 1
            server_update_time = _normalize_server_update_time(sdata.get("serverUpdateTime"))
        except (ValueError, TypeError) as exc:
            trendline_skipped_count += 1
            logger.warning("Skipping malformed trendline %s (%s): %s", sid, norm_sym, exc)
            continue

        dt1_str = get_beijing_time_str(t1_int)[:16]
        dt2_str = get_beijing_time_str(t2_int)[:16]
        line_iv = (
            inner_state.get("interval")
            or state_obj.get("interval")
            or pt1.get("interval")
            or ""
        )
        raw_linecolor = inner_state.get("linecolor")
        raw_extend_right = inner_state.get("extendRight")

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
            "server_update_time": server_update_time,
            "extend_right": bool(raw_extend_right) if raw_extend_right is not None else True,
            "line_color": str(raw_linecolor) if raw_linecolor is not None else "",
            "line_width": line_width,
        }
        symbol_to_trendlines.setdefault(norm_sym, []).append(drawing_meta)

    if trendline_skipped_count:
        logger.warning(
            "Skipped %d/%d malformed LineToolTrendLine drawings in layout %s",
            trendline_skipped_count,
            trendline_seen_count,
            layout_id,
        )
    if trendline_seen_count > 0 and not symbol_to_trendlines:
        raise RuntimeError(
            f"All {trendline_seen_count} LineToolTrendLine drawings failed parsing for layout {layout_id}; "
            "refusing to overwrite drawings cache"
        )

    if save_cache:
        if raw_sources and not symbol_to_trendlines and _read_cached_drawings(cache_path, float("inf")) is not None:
            logger.warning(
                "Layout %s returned %d raw sources but 0 LineToolTrendLine drawings; preserving non-empty cache %s",
                layout_id,
                len(raw_sources),
                cache_path,
            )
            return symbol_to_trendlines
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


def _is_recent_ts(ts_val: object, now_ts: float, max_age_sec: float) -> bool:
    if ts_val is None or isinstance(ts_val, bool):
        return False
    try:
        val = float(ts_val)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(val) or val <= 0:
        return False
    if val > now_ts + 86400 * 30:
        return False
    return (now_ts - val) <= max_age_sec


def is_valid_15m_trendline(
    line: dict[str, Any],
    current_time: Optional[float] = None,
    max_age_hours: float = 72.0,
) -> bool:
    """
    Validates canonical trendline dict:
    1. Interval MUST explicitly be '15' or '15M'.
    2. Anchor recency (t2 / max(t1, t2)) MUST exist and be within max_age_hours (default 72h),
       and if server_update_time is present it MUST also be within max_age_hours.
    """
    if not isinstance(line, dict):
        return False

    iv = str(line.get("interval") or "").strip().upper()
    if iv not in ("15", "15M"):
        return False

    now_ts = current_time if current_time is not None else time.time()
    max_age_sec = max_age_hours * 3600.0

    s_time = line.get("server_update_time")
    if s_time is not None and not _is_recent_ts(s_time, now_ts, max_age_sec):
        return False

    t2 = line.get("t2")
    t1 = line.get("t1")
    anchor_ts: Optional[float] = None
    if t2 is not None and not isinstance(t2, bool):
        try:
            anchor_ts = float(t2)
        except (TypeError, ValueError):
            return False
    if t1 is not None and not isinstance(t1, bool):
        try:
            t1_val = float(t1)
        except (TypeError, ValueError):
            return False
        if anchor_ts is None or t1_val > anchor_ts:
            anchor_ts = t1_val

    return _is_recent_ts(anchor_ts, now_ts, max_age_sec)


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
        candle = get_closed_15m_candle(clean_sym, interval="15m")

    p_candle = prev_candle if prev_candle is not None else candle.get("prev_candle")
    if not isinstance(p_candle, dict):
        raise ValueError(f"prev_candle is required for strict crossover check ({clean_sym})")

    try:
        close_price = float(candle["close"])
        candle_ts = int(candle["open_time"])
        prev_close = float(p_candle["close"])
        if target_time is not None:
            eval_t = int(target_time)
            step = (
                (int(candle["close_time"]) - int(p_candle["close_time"]))
                if at_close
                else (candle_ts - int(p_candle["open_time"]))
            )
            if step > 0:
                prev_t = eval_t - step
            else:
                prev_t = int(p_candle["close_time"]) if at_close else int(p_candle["open_time"])
        elif at_close:
            eval_t = int(candle["close_time"])
            prev_t = int(p_candle["close_time"])
        else:
            eval_t = candle_ts
            prev_t = int(p_candle["open_time"])
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"Invalid canonical candle/prev_candle payload for {clean_sym}") from e

    candle_time_str = str(candle.get("candle_time") or get_beijing_time_str(candle_ts)[:16])

    def _log_projection(target_ts: int) -> float:
        return math.log(p1) + ((target_ts - t1) / (t2 - t1)) * math.log(p2 / p1)

    try:
        res_price = calculate_log_resistance(p1=p1, t1=t1, p2=p2, t2=t2, target_t=eval_t)
    except ValueError:
        is_runaway_overflow = _log_projection(eval_t) < -50.0
        return {
            "is_breakout": False,
            "is_crossover": False,
            "is_runaway": is_runaway_overflow,
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

    try:
        prev_res: Optional[float] = calculate_log_resistance(p1=p1, t1=t1, p2=p2, t2=t2, target_t=prev_t)
    except ValueError:
        prev_res = None
        is_crossover = _log_projection(prev_t) > 50.0 and close_price > res_price
    else:
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
