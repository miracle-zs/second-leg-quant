#!/usr/bin/env python3
"""
第二段战法候选池官方权威筛选引擎 (CoinMarketCap Pro API 实时校验版)
筛选标准:
1. 真实加密货币 (严格从币安官方 exchangeInfo 提取 status == 'TRADING' 且 underlyingType == 'COIN' 且 contractType == 'PERPETUAL'，彻底排除 TradFi 股票/外汇/大宗商品)
2. 币安合约价格与 CMC 价格严格一致 (容差 <= 25%，防止同名代币错配)
3. 官方流通市值 (Circulating Market Cap) 严格 < 3,000 万美元 (3000wu) 或处于 7 天溢出跟踪期内
4. 结合标准化 4h 资金费率、换手率与 1h EMA5/10 趋势共振
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional, Sequence, TypedDict

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from binance_client import BINANCE_FUTURES_BASE_URL, get_server_time_ms  # noqa: E402
from config import (  # noqa: E402
    BEIJING_TZ,
    DEFAULT_DB_PATH,
    get_env_setting,
    resolve_database_path,
)
import db_store  # noqa: E402
from http_client import HttpClient, HttpError  # noqa: E402

CMC_QUOTES_URL = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"


class _TargetRecord(TypedDict):
    symbol: str
    base: str
    multiplier: float
    binance_price: float
    high_price: float
    low_price: float
    price_chg: float
    funding_rate: float
    funding_interval_hours: int
    funding_rate_4h: float
    volume_usd: float


def extract_base_and_multiplier(symbol: str) -> tuple[str, float]:
    """剥离 USDT 后缀及 1000/1M 合约面值乘数，得到底层代币代号与乘数。"""
    base = symbol.removesuffix("USDT")
    for prefix, mult in (
        ("1000000", 1_000_000.0),
        ("100000", 100_000.0),
        ("10000", 10_000.0),
        ("1000", 1_000.0),
    ):
        if base.startswith(prefix):
            return base[len(prefix):], mult
    if base.startswith("1M") and len(base) > 2 and base[2].isalpha():
        return base[2:], 1_000_000.0
    return base, 1.0


def calc_ema(values: Sequence[float], period: int) -> Optional[float]:
    """单趟 O(N) 计算 SMA 种子启动的最终 EMA 标量值。"""
    if period <= 0:
        raise ValueError(f"EMA period must be positive, got {period}")
    if len(values) < period:
        return None
    alpha = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = alpha * v + (1.0 - alpha) * ema
    return ema


def _is_cmc_invalid_symbol_400(exc: HttpError) -> bool:
    """Return True only when a CMC HTTP 400 indicates an unrecognized symbol parameter."""
    body = exc.body.strip()
    if not body:
        return True
    try:
        payload = json.loads(body)
    except ValueError:
        return "symbol" in body.lower()
    if not isinstance(payload, dict):
        return False
    status = payload.get("status")
    if isinstance(status, dict):
        err_msg = str(status.get("error_message") or "").lower()
        err_code = status.get("error_code")
        return "symbol" in err_msg and err_code in (None, 400)
    return "symbol" in body.lower()


def _fetch_cmc_batch(
    chunk: Sequence[str],
    cmc_key: str,
    client: HttpClient,
) -> dict[str, dict[str, object]]:
    """拉取 CMC Pro 批量行情；仅当确认是无效符号触发的 HTTP 400 时二分定位并跳过无效符号。"""
    if not chunk:
        return {}
    chunk_encoded = ",".join(urllib.parse.quote(s) for s in chunk)
    url = f"{CMC_QUOTES_URL}?symbol={chunk_encoded}"
    headers = {"X-CMC_PRO_API_KEY": cmc_key, "Accepts": "application/json"}
    try:
        res = client.request_json(
            url,
            headers=headers,
            timeout=15.0,
            max_retries=3,
            backoff_base=2.5,
        )
    except HttpError as exc:
        if exc.status_code == 400 and _is_cmc_invalid_symbol_400(exc):
            if len(chunk) == 1:
                print(f"⚠️ CMC HTTP 400 跳过无法识别的符号: {chunk[0]}")
                return {}
            mid = len(chunk) // 2
            merged: dict[str, dict[str, object]] = {}
            merged.update(_fetch_cmc_batch(chunk[:mid], cmc_key, client))
            merged.update(_fetch_cmc_batch(chunk[mid:], cmc_key, client))
            return merged
        raise
    if not isinstance(res, dict) or not isinstance(res.get("data"), dict):
        raise RuntimeError(f"Unexpected CMC response payload for batch {list(chunk[:3])}...: {res}")
    normalized: dict[str, dict[str, object]] = {}
    for sym_key, entry in res["data"].items():
        if isinstance(entry, dict):
            normalized[str(sym_key).upper()] = entry
    return normalized


def _parse_detected_datetime(
    sym: str,
    active_res_symbols: set[str],
    res_history: dict[str, object],
) -> Optional[datetime.datetime]:
    if sym not in active_res_symbols or sym not in res_history:
        return None
    res_hist_item = res_history[sym]
    if not isinstance(res_hist_item, dict) or not isinstance(res_hist_item.get("first_detected"), str):
        print(f"⚠️ 共振状态历史缺少有效 first_detected ({sym}): {res_hist_item!r}，将使用当前时间")
        return None
    candidate_detected = res_hist_item["first_detected"].strip()
    try:
        return datetime.datetime.strptime(candidate_detected, "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        print(f"⚠️ 共振状态 first_detected 格式非法 ({sym}={candidate_detected!r}): {exc}，将使用当前时间")
        return None


def scan_pool(database: Optional[Path] = None) -> list[dict[str, object]]:
    """执行 CMC 校验 + 1h EMA 趋势共振筛选并将候选池与 7 天溢出历史写入 SQLite。"""
    cmc_key = get_env_setting("CMC_API_KEY")
    if not cmc_key:
        raise RuntimeError("Missing CMC_API_KEY in environment or .env")

    db_path = resolve_database_path(database) or DEFAULT_DB_PATH
    now_ts = time.time()
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ts))
    sub_30m_history = db_store.load_sub30m_history(db_path)

    with HttpClient(max_per_host=16) as client:
        # 1. 并发获取币安全部合约元数据与实时行情/费率/结算周期
        binance_endpoints = {
            "exchangeInfo": f"{BINANCE_FUTURES_BASE_URL}/fapi/v1/exchangeInfo",
            "ticker24hr": f"{BINANCE_FUTURES_BASE_URL}/fapi/v1/ticker/24hr",
            "premiumIndex": f"{BINANCE_FUTURES_BASE_URL}/fapi/v1/premiumIndex",
            "fundingInfo": f"{BINANCE_FUTURES_BASE_URL}/fapi/v1/fundingInfo",
        }
        with ThreadPoolExecutor(max_workers=4) as executor:
            futs = {
                key: executor.submit(
                    client.request_json,
                    url,
                    timeout=15.0,
                    max_retries=2,
                    backoff_base=0.8,
                )
                for key, url in binance_endpoints.items()
            }
            ex_info = futs["exchangeInfo"].result()
            tickers_list = futs["ticker24hr"].result()
            premiums_list = futs["premiumIndex"].result()
            funding_infos = futs["fundingInfo"].result()

        if not isinstance(ex_info, dict) or not isinstance(ex_info.get("symbols"), list):
            raise RuntimeError(f"Unexpected /fapi/v1/exchangeInfo payload: {type(ex_info).__name__}")
        if not isinstance(tickers_list, list):
            raise RuntimeError(f"Unexpected /fapi/v1/ticker/24hr payload: {type(tickers_list).__name__}")
        if not isinstance(premiums_list, list):
            raise RuntimeError(f"Unexpected /fapi/v1/premiumIndex payload: {type(premiums_list).__name__}")
        if not isinstance(funding_infos, list):
            raise RuntimeError(f"Unexpected /fapi/v1/fundingInfo payload: {type(funding_infos).__name__}")

        symbol_meta = {s["symbol"]: s for s in ex_info["symbols"]}
        tickers = {t["symbol"]: t for t in tickers_list if t["symbol"].endswith("USDT")}
        premiums = {p["symbol"]: p for p in premiums_list if p["symbol"].endswith("USDT")}
        # /fapi/v1/fundingInfo 仅返回调整过费率上下限或非标准周期的合约，不在其中的交易对为币安标准 8h 周期
        funding_intervals = {item["symbol"]: int(item["fundingIntervalHours"]) for item in funding_infos}

        targets: list[_TargetRecord] = []
        base_symbols: list[str] = []
        for sym, tick in tickers.items():
            meta = symbol_meta.get(sym)
            if meta is None:
                continue

            # 严格底层资产类型与交易状态过滤：TRADING + PERPETUAL + COIN + Crypto，彻底排除 TradFi
            sub_types = meta.get("underlyingSubType") or []
            if (
                meta.get("status") != "TRADING"
                or meta.get("contractType") != "PERPETUAL"
                or meta.get("underlyingType") != "COIN"
                or "TradFi" in sub_types
                or "Crypto" not in sub_types
            ):
                continue

            base, mult = extract_base_and_multiplier(sym)
            if not re.match(r"^([A-Z0-9]+|[\u4e00-\u9fa5]+)$", base):
                continue
            if sym not in premiums:
                continue

            fr = float(premiums[sym]["lastFundingRate"])
            vol = float(tick["quoteVolume"])
            p_chg = float(tick["priceChangePercent"])
            last_p = float(tick["lastPrice"]) / mult
            high_p = float(tick["highPrice"]) / mult
            low_p = float(tick["lowPrice"]) / mult
            interval = funding_intervals.get(sym, 8)
            if interval <= 0:
                raise ValueError(f"Invalid fundingIntervalHours for {sym}: {interval}")

            targets.append({
                "symbol": sym,
                "base": base,
                "multiplier": mult,
                "binance_price": last_p,
                "high_price": high_p,
                "low_price": low_p,
                "price_chg": p_chg,
                "funding_rate": fr,
                "funding_interval_hours": interval,
                "funding_rate_4h": fr * (4.0 / interval),
                "volume_usd": vol,
            })
            base_symbols.append(base)

        unique_bases = sorted(set(base_symbols))
        print(f"待向 CMC 官方 Pro API 批量验证纯加密币标的: {len(unique_bases)} 个 (已通过币安 exchangeInfo 纯加密过滤)")

        cmc_data: dict[str, dict[str, object]] = {}
        chunk_size = 40
        chunks = [unique_bases[i:i + chunk_size] for i in range(0, len(unique_bases), chunk_size)]
        if chunks:
            with ThreadPoolExecutor(max_workers=min(4, len(chunks))) as executor:
                chunk_futs = [executor.submit(_fetch_cmc_batch, chunk, cmc_key, client) for chunk in chunks]
                for fut in as_completed(chunk_futs):
                    cmc_data.update(fut.result())

        verified_candidates: list[dict[str, object]] = []
        for t in targets:
            sym = t["symbol"]
            base = t["base"]
            mult = t["multiplier"]
            binance_price = t["binance_price"]
            high_price = t["high_price"]
            low_price = t["low_price"]
            price_chg = t["price_chg"]
            fr = t["funding_rate"]
            fr_4h = t["funding_rate_4h"]
            vol_usd = t["volume_usd"]
            interval = t["funding_interval_hours"]

            c_obj = cmc_data.get(base.upper())
            if not c_obj or binance_price <= 0:
                continue

            quote_map = c_obj["quote"]
            if not isinstance(quote_map, dict) or not isinstance(quote_map.get("USD"), dict):
                continue
            quote = quote_map["USD"]
            cmc_price = quote.get("price")
            mcap = quote.get("market_cap")
            cs = c_obj.get("circulating_supply")
            if (
                not isinstance(cmc_price, (int, float))
                or not isinstance(mcap, (int, float))
                or not isinstance(cs, (int, float))
                or cmc_price <= 0
                or mcap <= 0
                or cs <= 0
            ):
                continue

            price_diff = abs(float(cmc_price) - binance_price) / binance_price
            if price_diff > 0.25:
                continue

            mcap_f = float(mcap)
            cs_f = float(cs)
            is_curr_sub_30m = 500_000 < mcap_f <= 30_000_000

            hist_entry = sub_30m_history.get(sym)
            days_since_sub_30m = 999.0
            is_hist_sub_30m = False
            if hist_entry:
                hist_ts = float(str(hist_entry["last_sub_30m_ts"]))
                days_since_sub_30m = (now_ts - hist_ts) / 86400.0
                if days_since_sub_30m <= 7.0 and 500_000 < mcap_f <= 80_000_000:
                    is_hist_sub_30m = True

            mcap_24h_low = low_price * cs_f
            is_24h_low_sub_30m = (500_000 < mcap_24h_low <= 30_000_000) and (500_000 < mcap_f <= 80_000_000)

            if not (is_curr_sub_30m or is_hist_sub_30m or is_24h_low_sub_30m):
                continue

            curr_min = min(mcap_f, mcap_24h_low)
            if is_curr_sub_30m:
                prev_min = hist_entry["min_mcap_usd"] if hist_entry else None
                sub_30m_history[sym] = {
                    "symbol": sym,
                    "base": base,
                    "last_sub_30m_ts": now_ts,
                    "last_sub_30m_iso": now_iso,
                    "min_mcap_usd": min(float(str(prev_min)), curr_min) if prev_min is not None else curr_min,
                }
                is_overflow_7d = False
                days_left = 7.0
            else:
                is_overflow_7d = True
                if is_24h_low_sub_30m:
                    prev_min = hist_entry["min_mcap_usd"] if hist_entry else None
                    new_min = min(float(str(prev_min)), mcap_24h_low) if prev_min is not None else mcap_24h_low
                    if not hist_entry or (now_ts - float(str(hist_entry["last_sub_30m_ts"]))) > 86400:
                        sub_30m_history[sym] = {
                            "symbol": sym,
                            "base": base,
                            "last_sub_30m_ts": now_ts,
                            "last_sub_30m_iso": now_iso,
                            "min_mcap_usd": new_min,
                        }
                        days_since_sub_30m = 0.0
                    elif prev_min is not None and new_min < float(str(prev_min)):
                        sub_30m_history[sym] = {
                            **hist_entry,
                            "min_mcap_usd": new_min,
                        }
                days_left = max(0.1, round(7.0 - days_since_sub_30m, 1))

            turnover = vol_usd / mcap_f
            fr_4h_pct = fr_4h * 100

            verified_candidates.append({
                "symbol": sym,
                "base": base,
                "multiplier": mult,
                "price": binance_price * mult,
                "high_24h": round(high_price * mult, 8),
                "low_24h": round(low_price * mult, 8),
                "price_chg_24h": price_chg,
                "market_cap_usd": mcap_f,
                "market_cap_wu": round(mcap_f / 10000, 1),
                "is_overflow_7d": is_overflow_7d,
                "overflow_days_left": days_left,
                "circulating_supply": cs_f,
                "funding_interval_hours": interval,
                "funding_rate_pct": round(fr * 100, 4),
                "funding_rate_4h_pct": round(fr_4h_pct, 4),
                "volume_24h_usd": vol_usd,
                "turnover_ratio": round(turnover, 2),
            })

        if not verified_candidates:
            raise RuntimeError("❌ 致命异常：未拉取到任何有效候选标的，拒绝写入 SQLite 以防清空历史数据！")

        # 清理超过 7 天的过期 sub_30m_history 记录，避免无限膨胀
        sub_30m_history = {
            sym: entry
            for sym, entry in sub_30m_history.items()
            if (now_ts - float(str(entry["last_sub_30m_ts"]))) / 86400.0 <= 7.0
        }

        print(f"\n[*] 正在并发拉取 {len(verified_candidates)} 个候选标的 1h 级别 K 线并计算 EMA5/EMA10 趋势与共振时长...")
        kline_results: dict[str, tuple[Optional[float], Optional[float], bool, Optional[float]]] = {}
        kline_errors: dict[str, str] = {}
        symbols_list = [str(c["symbol"]) for c in verified_candidates]
        # Pre-warm Binance server time offset cache before fan-out
        get_server_time_ms()

        def _eval_symbol(sym: str) -> tuple[str, Optional[float], Optional[float], bool, Optional[float]]:
            query = urllib.parse.urlencode({"symbol": sym, "interval": "1h", "limit": 168})
            url = f"{BINANCE_FUTURES_BASE_URL}/fapi/v1/klines?{query}"
            data = client.request_json(url, timeout=10.0, max_retries=2, backoff_base=0.5)
            if not isinstance(data, list):
                raise RuntimeError(f"Unexpected klines response type for {sym}: {type(data).__name__}")
            now_ms = get_server_time_ms()
            closed_bars = list(data)
            while closed_bars and int(closed_bars[-1][6]) > now_ms:
                closed_bars.pop()
            if len(closed_bars) < 10:
                return sym, None, None, False, None
            closes = [float(k[4]) for k in closed_bars]
            ema5 = calc_ema(closes, 5)
            ema10 = calc_ema(closes, 10)
            if ema5 is None or ema10 is None:
                return sym, None, None, False, None
            is_bull = ema5 > ema10
            spread = ((ema5 - ema10) / ema10) * 100 if ema10 > 0 else 0.0
            return sym, round(ema5, 8), round(ema10, 8), is_bull, round(spread, 3)

        with ThreadPoolExecutor(max_workers=16) as executor:
            future_to_sym = {executor.submit(_eval_symbol, sym): sym for sym in symbols_list}
            for fut in as_completed(future_to_sym):
                sym = future_to_sym[fut]
                try:
                    res = fut.result()
                    kline_results[sym] = res[1:]
                except Exception as exc:
                    kline_errors[sym] = str(exc)

        if kline_errors:
            preview = "; ".join(f"{sym}: {msg}" for sym, msg in list(kline_errors.items())[:5])
            if not kline_results:
                raise RuntimeError(f"❌ 1h K线全部拉取失败 ({len(kline_errors)} 个标的): {preview}")
            print(f"⚠️ 跳过 1h K线拉取失败标的 ({len(kline_errors)} 个): {preview}")
            verified_candidates = [c for c in verified_candidates if str(c["symbol"]) in kline_results]

    res_state = db_store.load_resonance_state(db_path)
    raw_history = res_state.get("history", {})
    if isinstance(raw_history, dict):
        res_history = raw_history
    else:
        print(f"⚠️ resonance_state history 格式异常 ({type(raw_history).__name__})，已忽略")
        res_history = {}
    raw_active = res_state.get("active_resonance_symbols", [])
    if isinstance(raw_active, list):
        active_res_symbols = {str(s) for s in raw_active}
    else:
        print(f"⚠️ resonance_state active_resonance_symbols 格式异常 ({type(raw_active).__name__})，已忽略")
        active_res_symbols = set()

    for c in verified_candidates:
        sym = str(c["symbol"])
        ema5, ema10, is_ema_bull, ema_spread = kline_results[sym]
        c["ema5"] = ema5
        c["ema10"] = ema10
        c["is_ema_bull"] = is_ema_bull
        c["ema_spread_pct"] = ema_spread

        fr_4h_pct = float(str(c["funding_rate_4h_pct"]))
        turnover_ratio = float(str(c["turnover_ratio"]))
        is_neg_fr = fr_4h_pct < 0
        is_high_tr = turnover_ratio >= 1.5
        is_resonance = is_ema_bull and is_high_tr

        if is_resonance:
            f_dt = _parse_detected_datetime(sym, active_res_symbols, res_history)
            if f_dt is not None:
                f_ts = f_dt.replace(tzinfo=BEIJING_TZ).timestamp()
                dur_h = max(0, int((now_ts - f_ts) // 3600))
                detected_str = f_dt.strftime("%Y-%m-%d %H:%M:%S")
                ent_str = f_dt.strftime("%m-%d %H:%M")
            else:
                dur_h = 0
                now_dt = datetime.datetime.fromtimestamp(now_ts, BEIJING_TZ)
                detected_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
                ent_str = now_dt.strftime("%m-%d %H:%M")
            c["resonance_duration_hours"] = dur_h
            c["resonance_entry_time"] = ent_str
            c["resonance_detected_at"] = detected_str
        else:
            c["resonance_duration_hours"] = 0
            c["resonance_entry_time"] = ""
            c["resonance_detected_at"] = ""

        zones: list[str] = []
        if is_resonance:
            zones.append("贴水双核(空头浓厚)" if is_neg_fr else "双核黄金共振")
        else:
            if is_ema_bull:
                zones.append("1h顺势")
            if is_high_tr:
                zones.append("高换手")
            if is_neg_fr:
                zones.append("负费率贴水")

        c["is_neg_funding"] = is_neg_fr
        c["is_high_turnover"] = is_high_tr
        c["is_resonance"] = is_resonance
        c["zones"] = zones

        if is_resonance and not is_neg_fr:
            res_score = 2
        elif is_resonance and is_neg_fr:
            res_score = 1
        elif (is_high_tr or is_ema_bull) and not is_neg_fr:
            res_score = 1
        else:
            res_score = 0
        c["resonance_score"] = res_score

        days_left_f = float(str(c["overflow_days_left"]))
        overflow_tag = f" ★7d溢出跟踪(剩{days_left_f:.1f}天)" if c["is_overflow_7d"] else ""
        if is_resonance and not is_neg_fr:
            c["status"] = f"👑 双核黄金共振：1h顺势+换手{turnover_ratio:.1f}x (资金面健康){overflow_tag}"
        elif is_resonance and is_neg_fr:
            c["status"] = f"⚠️ 贴水双核：1h顺势+换手{turnover_ratio:.1f}x+负费率{fr_4h_pct:+.4f}% (空头主导){overflow_tag}"
        elif is_ema_bull and is_neg_fr:
            c["status"] = f"顺势负费率：1h金叉但贴水{overflow_tag}"
        elif is_ema_bull:
            c["status"] = f"1h顺势多头{overflow_tag}"
        elif is_high_tr:
            c["status"] = f"巨量高换手 ({turnover_ratio:.1f}x){overflow_tag}"
        elif is_neg_fr:
            c["status"] = f"负费率贴水 (4h等效{fr_4h_pct:+.4f}%){overflow_tag}"
        else:
            c["status"] = f"常规收敛{overflow_tag}"

    verified_candidates.sort(
        key=lambda x: (
            -int(str(x["resonance_score"])),
            1 if x["is_neg_funding"] else 0,
            -float(str(x["turnover_ratio"])),
            -float(str(x["funding_rate_4h_pct"])),
        )
    )

    updated_at = db_store.now_beijing_iso()
    db_store.publish_candidates(db_path, verified_candidates, updated_at=updated_at)
    db_store.save_sub30m_history(db_path, sub_30m_history)

    n_overflow = len([c for c in verified_candidates if c["is_overflow_7d"]])
    print(f"\n✅ 官方权威校验完成！纯加密候选标的共 {len(verified_candidates)} 个 (含 {n_overflow} 个 7 天溢出跟踪标的):")
    print(f"  • 👑 双核黄金共振标的 (健康费率): {len([c for c in verified_candidates if c['resonance_score'] == 2])} 个")
    print(f"  • ⚠️ 贴水双核标的 (负费率警示): {len([c for c in verified_candidates if c['is_resonance'] and c['is_neg_funding']])} 个")
    print(f"  • 第一类 负费率贴水区 (< 0): {len([c for c in verified_candidates if c['is_neg_funding']])} 个")
    print(f"  • 第二类 巨量高换手区 (≥ 1.5x): {len([c for c in verified_candidates if c['is_high_turnover']])} 个")
    print(f"  • 第三类 1h EMA5>EMA10 顺势区: {len([c for c in verified_candidates if c['is_ema_bull']])} 个")
    print(f"  • ★ 7天溢出跟踪标的 (曾<3000wu现突破): {n_overflow} 个")
    print("\n【前 15 大潜在交易机会标的】：")
    for c in verified_candidates[:15]:
        zones_list = c["zones"]
        zones_str = "+".join(str(z) for z in zones_list) if isinstance(zones_list, list) and zones_list else "基础底池"
        dur_h = c["resonance_duration_hours"]
        dur_str = f"{dur_h}h" if dur_h else "-"
        print(
            f"  • {str(c['symbol']):12s} | 机会区:[{zones_str:^15s}] | 共振时长:{dur_str:>4s} | "
            f"4h费率:{float(str(c['funding_rate_4h_pct'])):+.4f}% | 换手:{float(str(c['turnover_ratio'])):4.1f}x | "
            f"市值:${float(str(c['market_cap_usd']))/1e6:5.2f}M"
        )
    return verified_candidates


def main() -> None:
    parser = argparse.ArgumentParser(description="第二段战法候选池官方权威筛选引擎")
    parser.add_argument(
        "--database",
        type=Path,
        default=None,
        help="SQLite 数据库路径（默认读取 COIN_DATABASE 或 runs/account.sqlite3）",
    )
    args = parser.parse_args()
    scan_pool(database=args.database)


if __name__ == "__main__":
    main()
