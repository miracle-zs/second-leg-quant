#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
交易风控四大核心检查逻辑与 TradingView/Binance 批量行情拉取模块。
"""

from __future__ import annotations

import atexit
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import math
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from alert_notifier import ServerChanNotifier, build_tv_chart_link, format_alert_price  # noqa: E402
from binance_client import (  # noqa: E402
    BinanceAPIError,
    fetch_ticker_prices,
    get_closed_15m_candle,
)
import binance_position_client as bpc  # noqa: E402
from config import (  # noqa: E402
    DEFAULT_TV_LAYOUT_ID,
    RUNS_DIR,
    clean_symbol,
    get_beijing_time_str,
    normalize_futures_symbol,
)
import db_store  # noqa: E402
import tv_trendline_engine as tve  # noqa: E402
from tv_trendline_engine import (  # noqa: E402
    calculate_log_resistance,
    is_valid_15m_trendline,
)

if TYPE_CHECKING:
    from trade_alert_daemon import PositionStore, ResonanceStateStore

logger = logging.getLogger("trade_alert_daemon")

_DRAWINGS_CACHE: dict[str, Any] = {"data": None, "ts": 0.0, "layout_id": ""}

_EXECUTOR_LOCK = threading.Lock()
_CANDLE_EXECUTOR: Optional[ThreadPoolExecutor] = None


def _get_candle_executor() -> ThreadPoolExecutor:
    global _CANDLE_EXECUTOR
    with _EXECUTOR_LOCK:
        if _CANDLE_EXECUTOR is None:
            _CANDLE_EXECUTOR = ThreadPoolExecutor(max_workers=6, thread_name_prefix="candle-fetch")
        return _CANDLE_EXECUTOR


def _shutdown_candle_executor() -> None:
    global _CANDLE_EXECUTOR
    with _EXECUTOR_LOCK:
        executor = _CANDLE_EXECUTOR
        _CANDLE_EXECUTOR = None
    if executor is not None:
        executor.shutdown(wait=False)


atexit.register(_shutdown_candle_executor)


def _parse_optional_finite_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    if isinstance(val, bool):
        raise ValueError(f"Unexpected boolean numeric value: {val!r}")
    num = float(val)
    if not math.isfinite(num):
        raise ValueError(f"Non-finite numeric value: {val!r}")
    return num


def _load_candidate_pool(database: Path) -> Optional[list[dict[str, Any]]]:
    snapshot = db_store.load_candidates(database)
    if snapshot is None:
        logger.warning(f"候选池尚未生成: {database}")
        return None
    _, payload_json = snapshot
    payload = json.loads(payload_json)
    if not isinstance(payload, list):
        raise ValueError(f"Invalid candidate_snapshot payload type: {type(payload).__name__}")
    return payload


def fetch_layout_drawings(
    layout_id: str = DEFAULT_TV_LAYOUT_ID,
    ttl_sec: int = 60,
) -> Dict[str, Any]:
    """
    从 tv_trendline_engine 获取 TradingView 布局中的画线数据 (支持内存与近期快照缓存)。
    在线拉取失败时显式抛出异常，避免覆盖有效状态或误判 15m 窗口完成。
    """
    now = time.time()
    raw_drawings: Optional[dict[str, Any]] = None

    if (
        _DRAWINGS_CACHE["data"] is not None
        and _DRAWINGS_CACHE["layout_id"] == layout_id
        and (now - float(_DRAWINGS_CACHE["ts"])) < ttl_sec
    ):
        raw_drawings = _DRAWINGS_CACHE["data"]
    else:
        local_cache_file = Path(RUNS_DIR) / f"tradingview_drawings_{layout_id}.json"
        if local_cache_file.exists():
            mtime = local_cache_file.stat().st_mtime
            if (now - mtime) < ttl_sec:
                with open(local_cache_file, "r", encoding="utf-8") as f:
                    cdata = json.load(f)
                drawings = cdata.get("drawings_by_symbol") if isinstance(cdata, dict) else None
                if isinstance(drawings, dict):
                    _DRAWINGS_CACHE.update({"data": drawings, "ts": mtime, "layout_id": layout_id})
                    raw_drawings = drawings

        if raw_drawings is None:
            drawings = tve.get_layout_drawings(layout_id)
            if not isinstance(drawings, dict):
                raise RuntimeError(f"Unexpected drawings payload for {layout_id}: {type(drawings).__name__}")
            _DRAWINGS_CACHE.update({"data": drawings, "ts": now, "layout_id": layout_id})
            raw_drawings = drawings

    return raw_drawings


def check_feature_1_resonance(
    notifier: ServerChanNotifier,
    state_store: ResonanceStateStore,
    layout_id: str = DEFAULT_TV_LAYOUT_ID,
    cold_start: bool = True,
    debounce_hours: float = 24.0,
    database_path: Optional[Path] = None,
) -> List[dict]:
    """
    Feature 1 (新共振标的提醒):
    1. 冷启动基准建立: 仅当 state_store 从未初始化过时记录初始基准，市场共振归零后再出现新标的不会重新触发冷启动抑制。
    2. 失败不吞掉: 仅当 Server酱 发送成功或处于 24h 防抖期内才将标的计入 active_resonance_symbols，发信失败时下轮自动重试。
    3. 24h 防抖: 标的掉出后重新进入时重置 first_detected，但 24 小时内不重复打扰。
    """
    db_target = Path(database_path) if database_path is not None else state_store.database
    candidates = _load_candidate_pool(db_target)
    if candidates is None:
        return []

    current_resonance_coins: dict[str, dict[str, Any]] = {}
    for idx, c in enumerate(candidates):
        if not isinstance(c, dict):
            logger.warning(f"跳过第 {idx} 条非字典候选记录: {type(c).__name__}")
            continue
        raw_sym = c.get("symbol")
        sym = normalize_futures_symbol(raw_sym) if isinstance(raw_sym, str) else ""
        if not sym:
            logger.warning(f"跳过缺失有效 symbol 的候选记录: {c!r}")
            continue
        if not c.get("is_resonance"):
            continue
        try:
            price_val = _parse_optional_finite_float(c.get("price"))
            if price_val is not None and price_val <= 0:
                raise ValueError(f"non-positive price={price_val}")
            _parse_optional_finite_float(c.get("turnover_ratio"))
            _parse_optional_finite_float(c.get("funding_rate_4h_pct"))
            raw_score = c.get("resonance_score")
            if raw_score is not None:
                if isinstance(raw_score, bool):
                    raise ValueError(f"invalid resonance_score={raw_score!r}")
                int(raw_score)
            raw_zones = c.get("zones")
            if raw_zones is not None and (
                not isinstance(raw_zones, list) or not all(isinstance(z, str) for z in raw_zones)
            ):
                raise ValueError(f"invalid zones={raw_zones!r}")
            raw_status = c.get("status")
            if raw_status is not None and not isinstance(raw_status, str):
                raise ValueError(f"invalid status={raw_status!r}")
        except (TypeError, ValueError) as e:
            logger.warning(f"跳过 {sym} 畸形共振候选记录: {e}")
            continue
        current_resonance_coins[sym] = c

    now_ts = time.time()
    now_str = get_beijing_time_str(now_ts)
    current_symbols = set(current_resonance_coins.keys())
    pending_notifications: list[tuple[str, dict[str, Any], str, str, str, Optional[int], str]] = []

    with state_store.transaction():
        active_symbols = state_store.get_active_symbols()
        is_initialized = bool(state_store.data.get("initialized"))
        if cold_start and not is_initialized:
            logger.info(
                f"冷启动基准建立: 记录当前 {len(current_symbols)} 个共振标的为初始基准，不发送微信通知。"
            )
            state_store.data["initialized"] = True
            state_store.data["active_resonance_symbols"] = sorted(current_symbols)
            history = state_store.data.setdefault("history", {})
            for sym in current_symbols:
                coin = current_resonance_coins[sym]
                detected_at = coin.get("resonance_detected_at") or now_str
                tv_link = build_tv_chart_link(layout_id, sym)
                if sym not in history:
                    raw_score = coin.get("resonance_score")
                    history[sym] = {
                        "symbol": sym,
                        "first_detected": detected_at,
                        "last_notified_at": now_str,
                        "last_notified_time": int(now_ts),
                        "res_score": int(raw_score) if raw_score is not None else None,
                        "zones": coin.get("zones", []),
                        "tv_link": tv_link,
                    }
            state_store.save()
            return []

        new_entered = current_symbols - active_symbols
        next_active = set(active_symbols & current_symbols)

        if new_entered:
            logger.info(f"⚡ 检测到 {len(new_entered)} 个新进入共振区标的: {sorted(new_entered)}")

        history = state_store.data.setdefault("history", {})
        history_updated = False
        for sym in sorted(new_entered):
            coin = current_resonance_coins[sym]
            detected_at = coin.get("resonance_detected_at") or now_str
            raw_score = coin.get("resonance_score")
            res_score = int(raw_score) if raw_score is not None else None
            fr_pct = _parse_optional_finite_float(coin.get("funding_rate_4h_pct"))
            is_neg_fr = bool(coin.get("is_neg_funding")) or (fr_pct is not None and fr_pct < 0)

            hist = history.get(sym, {})
            last_notified_ts = hist.get("last_notified_time")
            if debounce_hours > 0 and last_notified_ts is not None:
                age_sec = now_ts - float(last_notified_ts)
                if age_sec < (debounce_hours * 3600.0):
                    logger.info(
                        f"24h防抖拦截: {sym} 在前 24 小时内 ({age_sec / 3600:.1f}h 前) 已通知过，跳过重复打扰。"
                    )
                    history[sym] = {
                        **hist,
                        "symbol": sym,
                        "first_detected": detected_at,
                        "last_notified_at": hist.get("last_notified_at") or hist.get("first_detected") or now_str,
                        "last_notified_time": int(last_notified_ts),
                    }
                    history_updated = True
                    next_active.add(sym)
                    continue

            fr_str = f"{fr_pct:+.4f}%" if fr_pct is not None else "--"
            if is_neg_fr:
                res_tag = "⚠️ 贴水双核 (1h顺势+高换手，但合约深度贴水/空头拥挤)"
                res_badge = "⚠️ 贴水双核"
                guidance_block = f"""> ⚠️ **风控严正警示 (合约深度折价/空头拥挤)**:
> 该标的 4h 资金费率为 `{fr_str}` (合约价格贴水于现货)。
> 负费率反映衍生品市场做空情绪过度堆积，往往顺应趋势继续阴跌，极少为真逼空！
> 若绘制 15m 趋势线，**必须极度警惕假突破与诱多**，坚决以 15m 阳线实体收盘突破为准，开仓必须以趋势线严设止损！"""
            else:
                res_tag = "👑 双核黄金共振 (1h顺势EMA + 高换手，资金面健康)"
                res_badge = "👑 黄金双核"
                guidance_block = """> 🎯 **操作指引**:
> 该标的资金面健康，已进入第二段量化严选共振区！请立即打开下方 TradingView 专属布局，在 15m 图表连接近期下行高点绘制对数趋势线。
> 系统将持续监控该线，在 15m K 线收盘放量突破时发出买点②开多提醒。"""

            tv_link = build_tv_chart_link(layout_id, sym)
            price_val = _parse_optional_finite_float(coin.get("price"))
            turnover_val = _parse_optional_finite_float(coin.get("turnover_ratio"))
            price_str = f"${format_alert_price(price_val)}" if price_val is not None else "--"
            turnover_str = f"{turnover_val:.1f}x" if turnover_val is not None else "--"
            detected_line = f"\n- **入圈时间**: {detected_at}" if detected_at[:16] != now_str[:16] else ""

            title = f"{res_badge} 提醒: {sym} 进入共振区，请画 15m 趋势线"
            desp = f"""### {res_tag} 选币确认！

- **标的代码**: `BINANCE:{sym}.P`
- **当前市价**: `{price_str}`
- **24h换手率**: `{turnover_str}`
- **4h资金费率**: `{fr_str}`
- **机会区标签**: {', '.join(coin.get('zones', [])) or '共振区'}
- **形态状态**: {coin.get('status', '多周期趋势共振')}{detected_line}
- **触发时间**: {now_str}

---
{guidance_block}

👉 **[点击立即前往 TradingView 专属画线图表]({tv_link})**
"""
            pending_notifications.append((sym, coin, title, desp, detected_at, res_score, tv_link))

        new_active_sorted = sorted(next_active)
        if (
            new_active_sorted != sorted(active_symbols)
            or history_updated
            or not is_initialized
        ):
            state_store.data["initialized"] = True
            state_store.data["active_resonance_symbols"] = new_active_sorted
            state_store.save()

    sent_items: list[tuple[str, dict[str, Any], str, Optional[int], str]] = []
    alerted_list: list[dict[str, Any]] = []
    for sym, coin, title, desp, detected_at, res_score, tv_link in pending_notifications:
        if notifier.send_alert(title, desp):
            sent_items.append((sym, coin, detected_at, res_score, tv_link))
            alerted_list.append(coin)
        else:
            logger.warning(f"Server酱发送失败，保留 {sym} 待下轮重试。")

    if sent_items:
        with state_store.transaction():
            active_now = set(state_store.get_active_symbols())
            history_now = state_store.data.setdefault("history", {})
            for sym, coin, detected_at, res_score, tv_link in sent_items:
                active_now.add(sym)
                history_now[sym] = {
                    "symbol": sym,
                    "first_detected": detected_at,
                    "last_notified_at": now_str,
                    "last_notified_time": int(now_ts),
                    "res_score": res_score,
                    "zones": coin.get("zones", []),
                    "tv_link": tv_link,
                }
            state_store.data["initialized"] = True
            state_store.data["active_resonance_symbols"] = sorted(active_now)
            state_store.save()

    return alerted_list


def _send_breakout_notification(
    notifier: ServerChanNotifier,
    sym: str,
    res_price: float,
    close_price: float,
    pct: float,
    candle_or_res: dict,
    layout_id: str,
) -> bool:
    """发送突破买点②开多通知。"""
    tv_link = build_tv_chart_link(layout_id, sym)
    candle_time_str = candle_or_res.get("candle_time")
    if not candle_time_str:
        ts_val = candle_or_res.get("open_time")
        if ts_val is None:
            ts_val = candle_or_res.get("close_time")
        if ts_val is None:
            raise KeyError(
                f"Breakout candle payload for {sym} missing candle_time and open_time/close_time"
            )
        candle_time_str = get_beijing_time_str(float(ts_val))

    title = f"🚀 突破开多信号: {sym} 突破 15m 对数趋势线 (+{pct:.2f}%)"
    desp = f"""### 🚀 【买点②确认】15m 阳线实体收盘放量突破！

- **交易标的**: `BINANCE:{sym}.P`
- **K线周期**: 15m (已确定收盘)
- **收盘时间**: {candle_time_str}
- **最新收盘价**: `${format_alert_price(close_price)}`
- **对数阻力线**: `${format_alert_price(res_price)}`
- **突破幅度**: `+{pct:.2f}%`

---
> 🎯 **实战交易指令**:
> 1. **开仓买入**: 建议按纪律执行突破追多开仓。
> 2. **无条件止损**: 以突破的下行趋势线作为铁律生命线，若 15m 收盘价跌回线下方，立即坚决止损！
> 3. **动态止盈保护**: 浮盈达到 +6% 后设立 +1% 锁利提醒线，触及时发送通知。

👉 **[查看 TradingView 15m 实战图表]({tv_link})**
"""
    return notifier.send_alert(title, desp)


def _is_candle_for_closed_bucket(candle: dict[str, Any], current_bucket: int) -> bool:
    expected_open = current_bucket - 900
    open_t = int(candle["open_time"])
    close_t = int(candle["close_time"])
    return open_t == expected_open and expected_open <= close_t < current_bucket


def _fetch_15m_candles_batch(
    symbols: Sequence[str],
    *,
    candle_cache: Optional[Dict[str, dict]] = None,
    status: Optional[Dict[str, Any]] = None,
    current_bucket: Optional[int] = None,
    error_label: str = "15m K线",
) -> dict[str, dict[str, Any]]:
    """
    批量并发拉取多个币种的最新已收盘 15m K线，并在主线程安全校验窗口对齐与写入 candle_cache。
    """
    resolved: dict[str, dict[str, Any]] = {}
    to_fetch: list[str] = []
    seen: set[str] = set()

    for sym_clean in symbols:
        if not sym_clean or sym_clean in seen:
            continue
        seen.add(sym_clean)
        cached = candle_cache.get(sym_clean) if candle_cache is not None else None
        if cached is not None:
            if current_bucket is not None and not _is_candle_for_closed_bucket(cached, current_bucket):
                logger.warning(
                    f"{sym_clean} {error_label}尚未对齐当前已收盘窗口 "
                    f"(open={cached.get('open_time')}, close={cached.get('close_time')}, expected_open={current_bucket - 900})"
                )
                if status is not None:
                    status["had_error"] = True
                continue
            resolved[sym_clean] = cached
        else:
            to_fetch.append(sym_clean)

    if not to_fetch:
        return resolved

    def _fetch_one(sym_clean: str) -> tuple[str, Optional[dict[str, Any]], Optional[BinanceAPIError]]:
        try:
            return sym_clean, get_closed_15m_candle(sym_clean), None
        except BinanceAPIError as exc:
            return sym_clean, None, exc

    if len(to_fetch) == 1:
        results = [_fetch_one(to_fetch[0])]
    else:
        results = list(_get_candle_executor().map(_fetch_one, to_fetch))

    for sym_clean, candle, err in results:
        if err is not None:
            logger.warning(f"拉取 {sym_clean} {error_label}失败: {err}")
            if status is not None:
                status["had_error"] = True
            continue
        assert candle is not None
        if current_bucket is not None and not _is_candle_for_closed_bucket(candle, current_bucket):
            logger.warning(
                f"{sym_clean} {error_label}尚未对齐当前已收盘窗口 "
                f"(open={candle.get('open_time')}, close={candle.get('close_time')}, expected_open={current_bucket - 900})"
            )
            if status is not None:
                status["had_error"] = True
            continue
        resolved[sym_clean] = candle
        if candle_cache is not None:
            candle_cache[sym_clean] = candle

    return resolved


def _flush_retried_breakouts(
    state_store: ResonanceStateStore,
    retried_succeeded: Sequence[tuple[str, list[str]]],
    *,
    active_line_keys: Optional[set[str]] = None,
) -> None:
    if not retried_succeeded:
        return
    with state_store.transaction():
        pending = state_store.data.setdefault("pending_breakouts", {})
        for key, keys_to_mark in retried_succeeded:
            for k in keys_to_mark:
                state_store.mark_breakout_notified(
                    k,
                    active_line_keys=active_line_keys,
                    save=False,
                )
            pending.pop(key, None)
        state_store.save()


def check_feature_2_breakout(
    notifier: ServerChanNotifier,
    state_store: ResonanceStateStore,
    layout_id: str = DEFAULT_TV_LAYOUT_ID,
    *,
    retry_only: bool = False,
    candle_cache: Optional[Dict[str, dict]] = None,
    status: Optional[Dict[str, Any]] = None,
    current_bucket: Optional[int] = None,
) -> List[dict]:
    """
    Feature 2 (15m 突破开多提醒):
    直接复用 tv_trendline_engine 校验严格 Crossover、15m 周期与 72h 时效。
    所有网络 I/O（TV 画线、15m K线、Server酱 通知）均在 SQLite 写事务外执行。
    """
    with state_store.transaction():
        pending_snapshot = [
            (key, dict(event))
            for key, event in state_store.data.get("pending_breakouts", {}).items()
        ]
        active_symbols = sorted(state_store.get_active_symbols())
        notified_keys = set(state_store.data.get("notified_breakout_keys", []))

    retried_succeeded: list[tuple[str, list[str]]] = []
    for key, event in pending_snapshot:
        if _send_breakout_notification(
            notifier,
            event["symbol"],
            event["res_price"],
            event["close_price"],
            event["pct"],
            event["candle"],
            event["layout_id"],
        ):
            retried_succeeded.append((key, list(event["keys"])))

    if retry_only:
        _flush_retried_breakouts(state_store, retried_succeeded)
        return []

    try:
        drawings_map = fetch_layout_drawings(layout_id)
    except (BinanceAPIError, tve.TradingViewError, TimeoutError, OSError) as e:
        logger.warning(f"获取 TV 布局画线异常，跳过本轮突破扫描: {e}")
        _flush_retried_breakouts(state_store, retried_succeeded)
        if status is not None:
            status["had_error"] = True
        return []

    if not drawings_map:
        _flush_retried_breakouts(state_store, retried_succeeded)
        logger.debug(f"TV 布局 {layout_id} 暂无可用画线数据")
        return []

    active_line_keys: set[str] = set()
    for sym_k, lines in drawings_map.items():
        for line in lines:
            if isinstance(line, dict) and line.get("id"):
                active_line_keys.add(f"{sym_k}_{line['id']}")

    _flush_retried_breakouts(state_store, retried_succeeded, active_line_keys=active_line_keys)
    pending_keys_set = {key for key, _ in pending_snapshot} - {key for key, _ in retried_succeeded}
    blocked_symbols = {
        clean_symbol(str(event["symbol"]))
        for _, event in pending_snapshot
        if event.get("symbol")
    }

    target_symbols = [
        clean_symbol(sym)
        for sym in active_symbols
        if clean_symbol(sym) not in blocked_symbols and drawings_map.get(clean_symbol(sym))
    ]
    candles_by_symbol = _fetch_15m_candles_batch(
        target_symbols,
        candle_cache=candle_cache,
        status=status,
        current_bucket=current_bucket,
        error_label="15m K线",
    )

    candidates_by_sym: list[list[dict[str, Any]]] = []
    for sym in active_symbols:
        sym_clean = clean_symbol(sym)
        if sym_clean in blocked_symbols:
            continue
        matched_lines = drawings_map.get(sym_clean)
        candle = candles_by_symbol.get(sym_clean)
        if not matched_lines or candle is None:
            continue

        c_ts = int(candle["open_time"])
        sym_candidates: list[dict[str, Any]] = []
        for line in matched_lines:
            if not isinstance(line, dict):
                logger.warning(f"跳过 {sym} 非字典画线数据: {type(line).__name__}")
                continue
            line_id = line.get("id")
            if not line_id:
                logger.warning(f"跳过 {sym} 缺失 id 的趋势线: {line!r}")
                continue
            trendline_key = f"{sym}_{line_id}"

            if trendline_key in pending_keys_set or trendline_key in notified_keys:
                continue
            if not is_valid_15m_trendline(line, current_time=c_ts):
                continue
            try:
                raw_p1 = line["p1"]
                raw_p2 = line["p2"]
                raw_t1 = line["t1"]
                raw_t2 = line["t2"]
                if any(isinstance(v, bool) for v in (raw_p1, raw_p2, raw_t1, raw_t2)):
                    raise TypeError("boolean trendline coordinate")
                p1_val = float(raw_p1)
                p2_val = float(raw_p2)
                t1_val = int(raw_t1)
                t2_val = int(raw_t2)
            except (KeyError, TypeError, ValueError) as e:
                logger.warning(f"跳过 {sym} 畸形趋势线 ({line_id}): {e}")
                continue
            if (
                not math.isfinite(p1_val)
                or not math.isfinite(p2_val)
                or p1_val <= 0
                or p2_val <= 0
                or t1_val == t2_val
            ):
                logger.warning(
                    f"跳过 {sym} 无效几何趋势线 ({line_id}): p1={p1_val!r} t1={t1_val!r} p2={p2_val!r} t2={t2_val!r}"
                )
                continue
            if (p2_val - p1_val) * (t2_val - t1_val) > 0:
                continue

            try:
                res = tve.check_symbol_breakout(sym_clean, line, candle=candle)
            except ValueError as e:
                logger.warning(f"跳过 {sym} 无效几何趋势线 ({line_id}): {e}")
                continue
            if not res["is_breakout"]:
                continue

            res_price = float(res["res_price"])
            close_price = float(res["close_price"])
            pct = float(res["pct"])

            info = {
                "symbol": sym,
                "close_price": close_price,
                "res_price": res_price,
                "pct": pct,
                "candle_time": res["candle_time"],
                "trendline": line,
                "trendline_id": line_id,
            }
            pending_record = {
                "symbol": sym,
                "res_price": res_price,
                "close_price": close_price,
                "pct": pct,
                "candle": res,
                "layout_id": layout_id,
                "keys": [trendline_key],
            }
            sym_candidates.append({
                "trendline_key": trendline_key,
                "info": info,
                "pending_record": pending_record,
                "res_price": res_price,
                "close_price": close_price,
                "pct": pct,
                "res": res,
            })

        if sym_candidates:
            candidates_by_sym.append(sym_candidates)

    breakout_alerts: list[dict[str, Any]] = []
    for sym_candidates in candidates_by_sym:
        for cand in sym_candidates:
            trendline_key = cand["trendline_key"]
            sym = cand["info"]["symbol"]
            sym_clean = clean_symbol(sym)
            with state_store.transaction():
                pending = state_store.data.setdefault("pending_breakouts", {})
                if any(clean_symbol(str(ev.get("symbol", ""))) == sym_clean for ev in pending.values()):
                    break
                if trendline_key in pending or state_store.has_breakout_notified(trendline_key):
                    continue
                pending[trendline_key] = cand["pending_record"]
                state_store.save()

            if _send_breakout_notification(
                notifier,
                sym,
                cand["res_price"],
                cand["close_price"],
                cand["pct"],
                cand["res"],
                layout_id,
            ):
                with state_store.transaction():
                    state_store.mark_breakout_notified(
                        trendline_key,
                        active_line_keys=active_line_keys,
                        save=False,
                    )
                    state_store.data.setdefault("pending_breakouts", {}).pop(trendline_key, None)
                    state_store.save()
                breakout_alerts.append(cand["info"])
            break

    return breakout_alerts


def check_feature_3_stop_loss(
    notifier: ServerChanNotifier,
    pos_store: PositionStore,
    layout_id: str = DEFAULT_TV_LAYOUT_ID,
    *,
    retry_only: bool = False,
    candle_cache: Optional[Dict[str, dict]] = None,
    status: Optional[Dict[str, Any]] = None,
    current_bucket: Optional[int] = None,
) -> List[dict]:
    """
    Feature 3 (持仓破线无条件止损):
    在每个 15m K 线收盘后校验收盘价是否跌破趋势线支撑位；新匹配到的趋势线自动持久化。
    所有网络 I/O（TV 画线、15m K线、Server酱 通知）均在 SQLite 写事务外执行。
    """
    open_positions = [dict(p) for p in pos_store.get_open_positions()]
    if not open_positions:
        return []

    eligible = [p for p in open_positions if not p.get("stop_loss_notified", False)]
    if retry_only:
        eligible = [p for p in eligible if p.get("pending_stop_loss_candle") is not None]
    if not eligible:
        return []

    drawings_map: dict[str, Any] = {}
    candles_by_symbol: dict[str, dict[str, Any]] = {}
    if not retry_only:
        if any(p.get("pending_stop_loss_candle") is None and not p.get("trendline") for p in eligible):
            try:
                drawings_map = fetch_layout_drawings(layout_id)
            except (BinanceAPIError, tve.TradingViewError, TimeoutError, OSError) as e:
                logger.warning(f"获取 TV 画线失败，仅校验已缓存趋势线的持仓: {e}")
                if status is not None:
                    status["had_error"] = True

        symbols_to_fetch = [
            clean_symbol(pos["symbol"])
            for pos in eligible
            if pos.get("pending_stop_loss_candle") is None
        ]
        if symbols_to_fetch:
            candles_by_symbol = _fetch_15m_candles_batch(
                symbols_to_fetch,
                candle_cache=candle_cache,
                status=status,
                current_bucket=current_bucket,
                error_label="止损校验 15m K线",
            )

    matched_trendlines: dict[str, dict[str, Any]] = {}
    triggered_alerts: list[tuple[str, dict[str, Any], float, str, str]] = []

    for pos in eligible:
        sym = pos["symbol"]
        sym_clean = clean_symbol(sym)
        pending_candle = pos.get("pending_stop_loss_candle")
        candle = pending_candle if pending_candle is not None else candles_by_symbol.get(sym_clean)
        if candle is None:
            continue

        try:
            close_time = int(candle["close_time"])
            candle_open_time = int(candle["open_time"])
            close_price = float(candle["close"])
        except (KeyError, TypeError, ValueError) as e:
            logger.warning(f"跳过 {sym} 缺失关键时间/收盘价字段的 15m K线: {e}")
            if status is not None:
                status["had_error"] = True
            continue

        entry_price = float(pos["entry_price"])
        amount_val = _parse_optional_finite_float(pos["amount"])
        if amount_val is None or amount_val < 0:
            raise ValueError(f"持仓 {sym} amount={pos['amount']!r} 非法")
        line = pos.get("trendline")
        if not line and drawings_map:
            matched = drawings_map.get(sym_clean)
            if matched:
                line = bpc.select_best_position_trendline(matched, current_time=candle_open_time)
                if line:
                    matched_trendlines[sym] = line

        if not line:
            continue

        try:
            p1_price = float(line["p1"])
            p1_time = int(line["t1"])
            p2_price = float(line["p2"])
            p2_time = int(line["t2"])
            support_price = calculate_log_resistance(p1_price, p1_time, p2_price, p2_time, candle_open_time)
        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"跳过 {sym} 无效止损趋势线: {e}")
            continue

        if support_price <= 0 or close_price >= support_price:
            continue

        if entry_price <= 0:
            logger.error(f"持仓 {sym} entry_price={entry_price} 非法，跳过破线止损计算")
            continue

        drop_pct = (support_price - close_price) / support_price * 100.0
        pnl_pct = (close_price - entry_price) / entry_price * 100.0
        tv_link = build_tv_chart_link(layout_id, sym_clean)

        title = f"🚨 【破线止损预警】{sym} 15m 跌破趋势线，请立即平多止损！"
        desp = f"""### 🚨 【破线无条件止损】纪律执行警报！

- **持仓标的**: `{sym}`
- **开仓价格**: `${format_alert_price(entry_price)}`
- **15m 收盘价**: `${format_alert_price(close_price)}`
- **趋势线基准**: `${format_alert_price(support_price)}`
- **跌破幅度**: `-{drop_pct:.2f}%`
- **持仓盈亏**: `{pnl_pct:+.2f}%`
- **持仓数量**: `{amount_val}`
- **收盘时间**: {get_beijing_time_str(close_time)}

---
> ⚠️ **风控铁律提醒**:
> 15m K 线收盘价已有效跌破对数趋势线支撑，说明假突破或行情反转，买点②逻辑失效！
> 请立即执行市价平多止损，杜绝任何抗单侥幸心理！

👉 **[查看 TradingView 图表]({tv_link})**
"""
        triggered_alerts.append((sym, candle, close_price, title, desp))

    if matched_trendlines or triggered_alerts:
        triggered_candles = {sym: candle for sym, candle, _, _, _ in triggered_alerts}
        with pos_store.transaction():
            dirty = False
            for pos in pos_store.get_open_positions():
                sym = pos["symbol"]
                if sym in matched_trendlines and not pos.get("trendline"):
                    pos["trendline"] = matched_trendlines[sym]
                    dirty = True
                if sym in triggered_candles and not pos.get("stop_loss_notified", False):
                    if "pending_stop_loss_candle" not in pos:
                        pos["pending_stop_loss_candle"] = triggered_candles[sym]
                        dirty = True
            if dirty:
                pos_store.save()

    succeeded: list[tuple[str, float]] = []
    for sym, _candle, close_price, title, desp in triggered_alerts:
        if notifier.send_alert(title, desp):
            succeeded.append((sym, close_price))
        else:
            if status is not None:
                status["had_error"] = True

    if not succeeded:
        return []

    stop_loss_alerts: list[dict[str, Any]] = []
    with pos_store.transaction():
        dirty = False
        for sym, close_price in succeeded:
            pos = next(
                (p for p in pos_store.get_open_positions() if p["symbol"] == sym),
                None,
            )
            if pos is None:
                continue
            pos.pop("pending_stop_loss_candle", None)
            pos["stop_loss_notified"] = True
            if pos.get("source") != "BINANCE_SYNC":
                closed_pos = pos_store.close_position(
                    sym,
                    reason="STOP_LOSS_TRIGGERED",
                    exit_price=close_price,
                )
                if closed_pos is None:
                    raise RuntimeError(f"Position {sym} disappeared before close_position")
                stop_loss_alerts.append(closed_pos)
            else:
                dirty = True
                stop_loss_alerts.append(pos)
            logger.warning(f"🚨 标的 {sym} 跌破趋势线，已发送止损警报！")
        if dirty:
            pos_store.save()

    return stop_loss_alerts


def check_feature_4_trailing_stops(
    notifier: ServerChanNotifier,
    pos_store: PositionStore,
    activation_pct: float = 6.0,
    lock_pct: float = 1.0,
    price_cache: Optional[Dict[str, float]] = None,
) -> List[dict]:
    """
    Feature 4 (持仓动态移动止盈保护):
    统一使用实时批量 ticker 最新成交价 (lastPrice) 获取现价，持久化 highest_price/current_price/pnl_pct 状态变更。
    所有网络 I/O（批量行情拉取、Server酱 通知）均在 SQLite 写事务外执行。
    """
    initial_open = pos_store.get_open_positions()
    if not initial_open:
        return []

    resolved_prices: dict[str, float] = dict(price_cache) if price_cache else {}
    missing_syms = [
        clean_symbol(p["symbol"])
        for p in initial_open
        if not resolved_prices.get(p["symbol"]) and not resolved_prices.get(clean_symbol(p["symbol"]))
    ]
    if missing_syms:
        try:
            fetched = fetch_ticker_prices(missing_syms)
            resolved_prices.update(fetched)
        except BinanceAPIError as e:
            logger.warning(f"批量获取移动止盈实时价格失败: {e}")

    if not resolved_prices and not any(
        "pending_trailing_activation_price" in p or "pending_profit_lock_price" in p
        for p in initial_open
    ):
        return []

    activation_alerts: list[tuple[str, float, str, str]] = []
    profit_lock_alerts: list[tuple[str, float, float, str, str]] = []

    with pos_store.transaction():
        open_positions = pos_store.get_open_positions()
        if not open_positions:
            return []

        dirty = False
        for pos in open_positions:
            sym = pos["symbol"]
            sym_clean = clean_symbol(sym)
            raw_curr = resolved_prices.get(sym)
            if raw_curr is None:
                raw_curr = resolved_prices.get(sym_clean)
            curr_price = _parse_optional_finite_float(raw_curr)
            if curr_price is not None and curr_price <= 0:
                raise ValueError(f"Invalid ticker price for {sym}: {curr_price}")
            if curr_price is None and not (
                "pending_trailing_activation_price" in pos or "pending_profit_lock_price" in pos
            ):
                continue

            entry_price = float(pos["entry_price"])
            if entry_price <= 0:
                logger.error(f"持仓 {sym} entry_price={entry_price} 非法，跳过移动止盈计算")
                continue

            raw_highest = pos.get("highest_price")
            if raw_highest is None:
                prev_highest = entry_price
            else:
                prev_highest = float(raw_highest)
                if not math.isfinite(prev_highest) or prev_highest <= 0:
                    raise ValueError(f"Invalid highest_price for {sym}: {raw_highest!r}")
            if curr_price is not None:
                highest_price = max(prev_highest, curr_price)
                new_pnl_pct = round((curr_price - entry_price) / entry_price * 100.0, 2)
                if (
                    highest_price != pos.get("highest_price")
                    or curr_price != pos.get("current_price")
                    or new_pnl_pct != pos.get("pnl_pct")
                ):
                    pos["highest_price"] = highest_price
                    pos["current_price"] = curr_price
                    pos["pnl_pct"] = new_pnl_pct
                    dirty = True
            else:
                highest_price = prev_highest

            target_activation_price = entry_price * (1.0 + activation_pct / 100.0)
            if (
                not pos.get("trailing_stop_triggered", False)
                and curr_price is not None
                and curr_price >= target_activation_price
            ):
                pos["trailing_stop_triggered"] = True
                pos["trailing_stop_price"] = round(entry_price * (1.0 + lock_pct / 100.0), 8)
                pos["trailing_stop_activated_at"] = get_beijing_time_str()
                pos["pending_trailing_activation_price"] = curr_price
                dirty = True

            needs_activation = "pending_trailing_activation_price" in pos
            needs_profit_lock = (
                pos.get("trailing_stop_triggered", False) and not pos.get("profit_lock_notified", False)
            )
            lock_price: Optional[float] = None
            if needs_activation or needs_profit_lock:
                raw_lock = pos["trailing_stop_price"] if "trailing_stop_price" in pos else None
                lock_price = _parse_optional_finite_float(raw_lock)
                if lock_price is None or lock_price <= 0:
                    raise ValueError(
                        f"持仓 {sym} 已触发移动止盈但缺失有效的 trailing_stop_price: {raw_lock!r}"
                    )

            if needs_activation:
                assert lock_price is not None
                activation_price = float(pos["pending_trailing_activation_price"])
                activation_gain = (activation_price - entry_price) / entry_price * 100.0
                title = f"🎯 【移动止盈锁定】{sym} 浮盈达 +{activation_gain:.1f}%，已启动 +{lock_pct:.0f}% 保本锁利！"
                desp = f"""### 🎯 【动态移动止盈启动】移动锁利提醒

- **持仓标的**: `{sym}`
- **开仓价格**: `${format_alert_price(entry_price)}`
- **激活时行情价**: `${format_alert_price(activation_price)}` (最高: `${format_alert_price(highest_price)}`)
- **激活时涨幅**: `+{activation_gain:.2f}%` (已突破 +{activation_pct:.0f}% 门槛)
- **移动止损新价**: `${format_alert_price(lock_price)}` (+{lock_pct:.1f}% 利润安全线)
- **触发时间**: {get_beijing_time_str()}

---
> 系统已记录锁利预警线 `${format_alert_price(lock_price)}`。实时巡检发现价格触线时会发出离场通知。
"""
                activation_alerts.append((sym, lock_price, title, desp))

            if needs_profit_lock:
                assert lock_price is not None
                if "pending_profit_lock_price" in pos or (curr_price is not None and curr_price <= lock_price):
                    if "pending_profit_lock_price" not in pos:
                        assert curr_price is not None
                        pos["pending_profit_lock_price"] = curr_price
                        dirty = True
                    trigger_price = float(pos["pending_profit_lock_price"])
                    trigger_pct = (trigger_price - entry_price) / entry_price * 100.0

                    title = f"🔔 【移动止盈离场】{sym} 触及移动止盈线，请平仓锁利离场！"
                    desp = f"""### 🔔 【移动止盈触发】建议市价平多落袋为安！

- **持仓标的**: `{sym}`
- **开仓价格**: `${format_alert_price(entry_price)}`
- **触发行情价**: `${format_alert_price(trigger_price)}`
- **锁利触发线**: `${format_alert_price(lock_price)}` (+{lock_pct:.1f}%)
- **触发时浮动收益率**: `{trigger_pct:+.2f}%`
- **触发时间**: {get_beijing_time_str()}

---
> 💰 **操作指引**:
> 行情出现回踩并触及移动保本线，请在币安确认当前仓位与可执行价格。
"""
                    profit_lock_alerts.append((sym, trigger_price, lock_price, title, desp))

        if dirty:
            pos_store.save()

    activated_syms: list[tuple[str, float]] = []
    for sym, lock_price, title, desp in activation_alerts:
        if notifier.send_alert(title, desp):
            activated_syms.append((sym, lock_price))

    exited_items: list[tuple[str, float, float]] = []
    for sym, trigger_price, lock_price, title, desp in profit_lock_alerts:
        if notifier.send_alert(title, desp):
            exited_items.append((sym, trigger_price, lock_price))

    if not activated_syms and not exited_items:
        return []

    events: list[dict[str, Any]] = []
    with pos_store.transaction():
        dirty = False
        for sym, lock_price in activated_syms:
            pos = next((p for p in pos_store.get_open_positions() if p["symbol"] == sym), None)
            if pos is None:
                continue
            if "pending_trailing_activation_price" in pos:
                del pos["pending_trailing_activation_price"]
                dirty = True
            events.append({"type": "TRAILING_ACTIVATED", "pos": dict(pos)})
            logger.info(f"🎯 {sym} 已发送移动止损激活通知: {lock_price}")

        for sym, trigger_price, lock_price in exited_items:
            pos = next((p for p in pos_store.get_open_positions() if p["symbol"] == sym), None)
            if pos is None:
                continue
            pos.pop("pending_profit_lock_price", None)
            pos["profit_lock_notified"] = True
            if pos.get("source") != "BINANCE_SYNC":
                closed_pos = pos_store.close_position(
                    sym,
                    reason="PROFIT_LOCKED",
                    exit_price=trigger_price,
                )
                if closed_pos is None:
                    raise RuntimeError(f"Position {sym} disappeared before close_position")
                events.append({"type": "PROFIT_LOCK_EXIT", "pos": closed_pos})
            else:
                dirty = True
                events.append({"type": "PROFIT_LOCK_EXIT", "pos": pos})
            logger.info(f"🔔 {sym} 触及移动止盈线 {lock_price}，已通知平仓锁利离场！")

        if dirty:
            pos_store.save()

    return events
