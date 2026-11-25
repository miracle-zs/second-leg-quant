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

import datetime
import http.client
import json
import re
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from binance_client import (  # noqa: E402
    BINANCE_FUTURES_BASE_URL,
    BinanceAPIError,
    fetch_json_with_retry,
)
from config import (  # noqa: E402
    BEIJING_TZ,
    DEFAULT_POOL_PATH,
    DEFAULT_RESONANCE_STATE_PATH,
    DEFAULT_SUB30M_HISTORY_PATH,
    atomic_write_json,
    file_lock,
    get_env_setting,
    open_sqlite_connection,
    resolve_database_path,
)

CMC_QUOTES_URL = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest"
FAPI_HOST = urllib.parse.urlparse(BINANCE_FUTURES_BASE_URL).netloc


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


def calc_ema_series(values: list[float], period: int) -> list[float | None]:
    """单趟 O(N) 计算 SMA 种子启动的完整 EMA 序列。"""
    if len(values) < period:
        return [None] * len(values)
    alpha = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    series: list[float | None] = [None] * (period - 1) + [ema]
    for v in values[period:]:
        ema = alpha * v + (1.0 - alpha) * ema
        series.append(ema)
    return series


def _load_json_dict(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {path}, got {type(data).__name__}")
    return data


def _fetch_cmc_batch(chunk: list[str], cmc_key: str) -> dict[str, Any]:
    """拉取 CMC Pro 批量行情；若因单个未识别符号触发 HTTP 400，则二分定位并跳过无效符号。"""
    if not chunk:
        return {}
    chunk_encoded = ",".join(urllib.parse.quote(s) for s in chunk)
    url = f"{CMC_QUOTES_URL}?symbol={chunk_encoded}"
    headers = {"X-CMC_PRO_API_KEY": cmc_key, "Accepts": "application/json"}
    try:
        res = fetch_json_with_retry(url, headers=headers, timeout=15, max_retries=4, backoff=2.5)
    except BinanceAPIError as exc:
        if exc.code == 400:
            if len(chunk) == 1:
                print(f"⚠️ CMC HTTP 400 跳过无法识别的符号: {chunk[0]}")
                return {}
            mid = len(chunk) // 2
            merged: dict[str, Any] = {}
            merged.update(_fetch_cmc_batch(chunk[:mid], cmc_key))
            merged.update(_fetch_cmc_batch(chunk[mid:], cmc_key))
            return merged
        raise
    if not isinstance(res, dict) or "data" not in res or not isinstance(res["data"], dict):
        raise RuntimeError(f"Unexpected CMC response payload for batch {chunk[:3]}...: {res}")
    return res["data"]


class _KlineConnectionPool:
    """线程局部 HTTP/1.1 Keep-Alive 连接池，用于并发复用 fapi.binance.com TLS 连接。"""

    def __init__(self, host: str = FAPI_HOST, timeout: int = 10):
        self.host = host
        self.timeout = timeout
        self._local = threading.local()
        self._lock = threading.Lock()
        self._conns: list[http.client.HTTPSConnection] = []

    def _get_conn(self) -> http.client.HTTPSConnection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = http.client.HTTPSConnection(self.host, timeout=self.timeout)
            self._local.conn = conn
            with self._lock:
                self._conns.append(conn)
        return conn

    def _reset_conn(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            with self._lock:
                try:
                    self._conns.remove(conn)
                except ValueError:
                    pass
            self._local.conn = None

    def fetch_1h_klines(self, symbol: str, limit: int = 168, max_retries: int = 3) -> list[list[Any]]:
        path = f"/fapi/v1/klines?{urllib.parse.urlencode({'symbol': symbol, 'interval': '1h', 'limit': limit})}"
        headers = {"User-Agent": "TradeAlertDaemon/1.0", "Connection": "keep-alive"}
        last_err: Exception | None = None
        for attempt in range(max_retries):
            conn = self._get_conn()
            retry_after_sec: float | None = None
            try:
                conn.request("GET", path, headers=headers)
                resp = conn.getresponse()
                body = resp.read()
                if resp.status == 200:
                    data = json.loads(body.decode("utf-8"))
                    if not isinstance(data, list):
                        raise RuntimeError(f"Unexpected klines response type for {symbol}: {type(data).__name__}")
                    return data
                err_text = body.decode("utf-8", errors="replace")
                if resp.status not in (429, 500, 502, 503, 504):
                    raise BinanceAPIError(f"HTTP {resp.status} for {symbol}: {err_text}", code=resp.status, endpoint="/fapi/v1/klines")
                if resp.status == 429:
                    raw_ra = resp.getheader("Retry-After")
                    if raw_ra is not None:
                        try:
                            retry_after_sec = min(float(raw_ra), 10.0)
                        except ValueError:
                            retry_after_sec = None
                last_err = BinanceAPIError(f"HTTP {resp.status} for {symbol}: {err_text}", code=resp.status, endpoint="/fapi/v1/klines")
            except (http.client.HTTPException, OSError, TimeoutError, json.JSONDecodeError) as exc:
                last_err = exc
                self._reset_conn()
            if attempt < max_retries - 1:
                delay = 0.5 * (2 ** attempt)
                if retry_after_sec is not None:
                    delay = max(delay, retry_after_sec)
                time.sleep(delay)
        raise RuntimeError(f"1h kline fetch failed for {symbol} after {max_retries} attempts: {last_err}") from last_err

    def close(self) -> None:
        with self._lock:
            for conn in self._conns:
                try:
                    conn.close()
                except Exception:
                    pass
            self._conns.clear()


def scan_pool() -> list[dict[str, Any]]:
    cmc_key = get_env_setting("CMC_API_KEY")
    if not cmc_key:
        raise RuntimeError("Missing CMC_API_KEY in environment or .env")

    now_ts = time.time()
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ts))
    history_file = DEFAULT_SUB30M_HISTORY_PATH
    with file_lock(history_file):
        sub_30m_history = _load_json_dict(history_file)

    headers_binance = {"User-Agent": "TradeAlertDaemon/1.0"}

    # 1. 获取币安全部合约元数据与实时行情/费率/结算周期
    ex_info = fetch_json_with_retry(f"{BINANCE_FUTURES_BASE_URL}/fapi/v1/exchangeInfo", headers=headers_binance, timeout=15)
    symbol_meta = {s["symbol"]: s for s in ex_info["symbols"]}

    tickers_list = fetch_json_with_retry(f"{BINANCE_FUTURES_BASE_URL}/fapi/v1/ticker/24hr", headers=headers_binance, timeout=15)
    tickers = {t["symbol"]: t for t in tickers_list if t["symbol"].endswith("USDT")}

    premiums_list = fetch_json_with_retry(f"{BINANCE_FUTURES_BASE_URL}/fapi/v1/premiumIndex", headers=headers_binance, timeout=15)
    premiums = {p["symbol"]: p for p in premiums_list if p["symbol"].endswith("USDT")}

    # /fapi/v1/fundingInfo 仅返回调整过费率上下限或非标准周期的合约，不在其中的交易对为币安标准 8h 周期
    funding_infos = fetch_json_with_retry(f"{BINANCE_FUTURES_BASE_URL}/fapi/v1/fundingInfo", headers=headers_binance, timeout=15)
    if not isinstance(funding_infos, list):
        raise RuntimeError(f"Unexpected /fapi/v1/fundingInfo payload: {type(funding_infos).__name__}")
    funding_intervals = {item["symbol"]: int(item["fundingIntervalHours"]) for item in funding_infos}

    targets: list[dict[str, Any]] = []
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
        interval = max(1, funding_intervals.get(sym, 8))

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

    cmc_data: dict[str, Any] = {}
    cmc_data_upper: dict[str, Any] = {}
    chunk_size = 40
    for i in range(0, len(unique_bases), chunk_size):
        batch_data = _fetch_cmc_batch(unique_bases[i:i + chunk_size], cmc_key)
        cmc_data.update(batch_data)
        for k, v in batch_data.items():
            cmc_data_upper[k.upper()] = v
        time.sleep(0.12)

    verified_candidates: list[dict[str, Any]] = []
    for t in targets:
        base = t["base"]
        c_obj = cmc_data.get(base) or cmc_data_upper.get(base.upper())
        if not c_obj or t["binance_price"] <= 0:
            continue

        if isinstance(c_obj, list):
            best_match = None
            min_diff = float("inf")
            for item in c_obj:
                qp = item.get("quote", {}).get("USD", {}).get("price")
                if qp is None or qp <= 0:
                    continue
                diff = abs(qp - t["binance_price"]) / t["binance_price"]
                if diff < min_diff:
                    min_diff = diff
                    best_match = item
            c_obj = best_match if min_diff <= 0.25 else None
        if not c_obj:
            continue

        quote = c_obj["quote"]["USD"]
        cmc_price = quote.get("price")
        mcap = quote.get("market_cap")
        cs = c_obj.get("circulating_supply")
        if cmc_price is None or mcap is None or cs is None or cmc_price <= 0 or mcap <= 0 or cs <= 0:
            continue

        price_diff = abs(cmc_price - t["binance_price"]) / t["binance_price"]
        if price_diff > 0.25:
            continue

        is_curr_sub_30m = 500_000 < mcap <= 30_000_000

        hist_entry = sub_30m_history.get(t["symbol"])
        days_since_sub_30m = 999.0
        is_hist_sub_30m = False
        if hist_entry:
            days_since_sub_30m = (now_ts - float(hist_entry["last_sub_30m_ts"])) / 86400.0
            if days_since_sub_30m <= 7.0 and 500_000 < mcap <= 80_000_000:
                is_hist_sub_30m = True

        mcap_24h_low = t["low_price"] * cs
        is_24h_low_sub_30m = (500_000 < mcap_24h_low <= 30_000_000) and (500_000 < mcap <= 80_000_000)

        if not (is_curr_sub_30m or is_hist_sub_30m or is_24h_low_sub_30m):
            continue

        curr_min = min(mcap, mcap_24h_low)
        if is_curr_sub_30m:
            prev_min = hist_entry["min_mcap_usd"] if hist_entry else None
            sub_30m_history[t["symbol"]] = {
                "symbol": t["symbol"],
                "base": base,
                "last_sub_30m_ts": now_ts,
                "last_sub_30m_iso": now_iso,
                "min_mcap_usd": min(prev_min, curr_min) if prev_min is not None else curr_min,
            }
            is_overflow_7d = False
            days_left = 7.0
        else:
            is_overflow_7d = True
            if is_24h_low_sub_30m and (not hist_entry or (now_ts - float(hist_entry["last_sub_30m_ts"])) > 86400):
                dip_ts = now_ts - 3600
                dip_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(dip_ts))
                prev_min = hist_entry["min_mcap_usd"] if hist_entry else None
                sub_30m_history[t["symbol"]] = {
                    "symbol": t["symbol"],
                    "base": base,
                    "last_sub_30m_ts": dip_ts,
                    "last_sub_30m_iso": dip_iso,
                    "min_mcap_usd": min(prev_min, mcap_24h_low) if prev_min is not None else mcap_24h_low,
                }
                days_since_sub_30m = 3600 / 86400.0
            days_left = max(0.1, round(7.0 - days_since_sub_30m, 1))

        turnover = t["volume_usd"] / mcap
        fr_4h_pct = t["funding_rate_4h"] * 100

        verified_candidates.append({
            "symbol": t["symbol"],
            "base": base,
            "multiplier": t["multiplier"],
            "price": t["binance_price"] * t["multiplier"],
            "high_24h": round(t["high_price"] * t["multiplier"], 8),
            "low_24h": round(t["low_price"] * t["multiplier"], 8),
            "price_chg_24h": t["price_chg"],
            "market_cap_usd": mcap,
            "market_cap_wu": round(mcap / 10000, 1),
            "is_overflow_7d": is_overflow_7d,
            "overflow_days_left": days_left,
            "circulating_supply": cs,
            "funding_interval_hours": t["funding_interval_hours"],
            "funding_rate_pct": round(t["funding_rate"] * 100, 4),
            "funding_rate_4h_pct": round(fr_4h_pct, 4),
            "volume_24h_usd": t["volume_usd"],
            "turnover_ratio": round(turnover, 2),
        })

    if not verified_candidates:
        raise RuntimeError("❌ 致命异常：未拉取到任何有效候选标的，拒绝写入 runs/ 以防清空历史数据！")

    # 清理超过 7 天的过期 sub_30m_history 记录，避免无限膨胀
    sub_30m_history = {
        sym: entry
        for sym, entry in sub_30m_history.items()
        if (now_ts - float(entry["last_sub_30m_ts"])) / 86400.0 <= 7.0
    }

    print(f"\n[*] 正在并发拉取 {len(verified_candidates)} 个候选标的 1h 级别 K 线并计算 EMA5/EMA10 趋势与共振时长...")
    kline_results: dict[str, tuple[float | None, float | None, bool, float | None]] = {}
    kline_errors: dict[str, str] = {}
    symbols_list = [c["symbol"] for c in verified_candidates]

    with closing(_KlineConnectionPool(timeout=10)) as pool, ThreadPoolExecutor(max_workers=16) as executor:
        def _eval_symbol(sym: str) -> tuple[str, float | None, float | None, bool, float | None]:
            data = pool.fetch_1h_klines(sym, limit=168)
            if len(data) < 10:
                return sym, None, None, False, None
            closes = [float(k[4]) for k in data]
            ema5 = calc_ema_series(closes, 5)[-1]
            ema10 = calc_ema_series(closes, 10)[-1]
            if ema5 is None or ema10 is None:
                return sym, None, None, False, None
            is_bull = ema5 > ema10
            spread = ((ema5 - ema10) / ema10) * 100 if ema10 > 0 else 0.0
            return sym, round(ema5, 8), round(ema10, 8), is_bull, round(spread, 3)

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
        raise RuntimeError(f"❌ 1h K线拉取失败 ({len(kline_errors)} 个标的): {preview}")

    resonance_state_file = DEFAULT_RESONANCE_STATE_PATH
    with file_lock(resonance_state_file):
        res_state = _load_json_dict(resonance_state_file)
    res_history = res_state.get("history", {}) if res_state else {}
    active_res_symbols = set(res_state.get("active_resonance_symbols", [])) if res_state else set()

    for c in verified_candidates:
        sym = c["symbol"]
        ema5, ema10, is_ema_bull, ema_spread = kline_results[sym]
        c["ema5"] = ema5
        c["ema10"] = ema10
        c["is_ema_bull"] = is_ema_bull
        c["ema_spread_pct"] = ema_spread

        is_neg_fr = c["funding_rate_4h_pct"] < 0
        is_high_tr = c["turnover_ratio"] >= 1.5
        is_resonance = is_ema_bull and is_high_tr

        if is_resonance:
            if sym in active_res_symbols and sym in res_history:
                res_hist_item = res_history[sym]
                f_dt = datetime.datetime.strptime(res_hist_item["first_detected"], "%Y-%m-%d %H:%M:%S")
                f_ts = f_dt.replace(tzinfo=BEIJING_TZ).timestamp()
                dur_h = max(0, int((now_ts - f_ts) // 3600))
                ent_str = f_dt.strftime("%m-%d %H:%M")
            else:
                dur_h = 0
                ent_str = datetime.datetime.fromtimestamp(now_ts, BEIJING_TZ).strftime("%m-%d %H:%M")
            c["resonance_duration_hours"] = dur_h
            c["resonance_entry_time"] = ent_str
        else:
            c["resonance_duration_hours"] = 0
            c["resonance_entry_time"] = ""

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

        overflow_tag = f" ★7d溢出跟踪(剩{c['overflow_days_left']:.1f}天)" if c["is_overflow_7d"] else ""
        if is_resonance and not is_neg_fr:
            c["status"] = f"👑 双核黄金共振：1h顺势+换手{c['turnover_ratio']:.1f}x (资金面健康){overflow_tag}"
        elif is_resonance and is_neg_fr:
            c["status"] = f"⚠️ 贴水双核：1h顺势+换手{c['turnover_ratio']:.1f}x+负费率{c['funding_rate_4h_pct']:+.4f}% (空头主导){overflow_tag}"
        elif is_ema_bull and is_neg_fr:
            c["status"] = f"顺势负费率：1h金叉但贴水{overflow_tag}"
        elif is_ema_bull:
            c["status"] = f"1h顺势多头{overflow_tag}"
        elif is_high_tr:
            c["status"] = f"巨量高换手 ({c['turnover_ratio']:.1f}x){overflow_tag}"
        elif is_neg_fr:
            c["status"] = f"负费率贴水 (4h等效{c['funding_rate_4h_pct']:+.4f}%){overflow_tag}"
        else:
            c["status"] = f"常规收敛{overflow_tag}"

    verified_candidates.sort(
        key=lambda x: (
            -x["resonance_score"],
            1 if x["is_neg_funding"] else 0,
            -x["turnover_ratio"],
            -x["funding_rate_4h_pct"],
        )
    )

    database = resolve_database_path()
    if database:
        payload = json.dumps(verified_candidates, ensure_ascii=False, allow_nan=False)
        with closing(open_sqlite_connection(database, readonly=False)) as connection, connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS candidate_snapshot (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
            """)
            connection.execute(
                """
                INSERT INTO candidate_snapshot (id, updated_at, payload)
                VALUES (1, datetime('now'), ?)
                ON CONFLICT(id) DO UPDATE SET
                    updated_at = excluded.updated_at, payload = excluded.payload
                """,
                (payload,),
            )

    atomic_write_json(DEFAULT_POOL_PATH, verified_candidates, allow_nan=False)
    with file_lock(history_file):
        atomic_write_json(history_file, sub_30m_history, allow_nan=False)

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
        zones_str = "+".join(c["zones"]) if c["zones"] else "基础底池"
        dur_str = f"{c['resonance_duration_hours']}h" if c["resonance_duration_hours"] else "-"
        print(
            f"  • {c['symbol']:12s} | 机会区:[{zones_str:^15s}] | 共振时长:{dur_str:>4s} | "
            f"4h费率:{c['funding_rate_4h_pct']:+.4f}% | 换手:{c['turnover_ratio']:4.1f}x | 市值:${c['market_cap_usd']/1e6:5.2f}M"
        )
    return verified_candidates


if __name__ == "__main__":
    scan_pool()
