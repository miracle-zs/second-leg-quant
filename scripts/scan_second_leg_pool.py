#!/usr/bin/env python3
"""
第二段战法候选池官方权威筛选引擎 (CoinMarketCap Pro API 实时校验版)
筛选标准:
1. 真实加密货币 (严格从币安官方 exchangeInfo 提取 status == 'TRADING' 且 underlyingType == 'COIN' 且 contractType == 'PERPETUAL'，彻底排除 TradFi 股票/外汇/大宗商品等传统金融通证)
2. 币安合约价格与 CMC 价格严格一致 (容差 < 25%，防止同名代币错配)
3. 官方流通市值 (Circulating Market Cap) 严格 < 3,000 万美元 (3000wu) 或处于 7 天溢出跟踪期内
4. 结合标准化 4h 负费率、换手率与 1h EMA5/10 趋势共振
"""

import os
import re
import json
import time
import datetime
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 排除传统美股代币通证与未上市股权通证 (辅助静态兜底名单)
EXCLUDED_STOCKS = {
    "QQQ", "SPY", "AAPL", "MSFT", "NVDA", "AMD", "GOOGL", "GOOG", "AMZN", "META", 
    "TSLA", "COIN", "TSM", "MU", "MSTR", "QCOM", "MRVL", "NBIS", "SNDK", "LITE", 
    "EWY", "SPCX", "CRCL", "AVGO", "DELL", "BE", "ORCL", "DIS", "NFLX", "INTC",
    "BABA", "PLTR", "ARM", "SMCI", "UBER", "HOOD", "SKHY", "CRWV", "LLY", "OPENAI",
    "ANTHROPIC", "SPACEX", "XAI", "BOT", "TTWO"
}

def load_dotenv(env_path: Path) -> dict:
    res = {}
    if env_path.exists():
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    res[k.strip()] = v.strip().strip("\"'")
    return res

def fetch_json_with_retry(url: str, headers: dict = None, timeout: int = 15, retries: int = 3, backoff: float = 1.5):
    """带指数退避重试的通用 JSON 请求辅助函数"""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers or {"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(backoff * (attempt + 1))

def scan_pool():
    dotenv = load_dotenv(ROOT / ".env")
    cmc_key = os.environ.get("CMC_API_KEY") or dotenv.get("CMC_API_KEY")
    if not cmc_key:
        raise RuntimeError("Missing CMC_API_KEY in environment or .env")

    now_ts = time.time()
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ts))
    history_file = ROOT / "runs" / "sub_30m_history.json"
    history_file.parent.mkdir(parents=True, exist_ok=True)
    sub_30m_history = {}
    if history_file.exists():
        try:
            with open(history_file, "r", encoding="utf-8") as f:
                sub_30m_history = json.load(f)
        except Exception:
            sub_30m_history = {}

    headers_binance = {"User-Agent": "Mozilla/5.0"}
    
    # 1. 获取币安全部合约元数据 (通过 exchangeInfo 严格过滤 TradFi/股票/大宗商品/外汇合约与非交易中状态)
    e_url = "https://www.binance.com/fapi/v1/exchangeInfo"
    ex_info = fetch_json_with_retry(e_url, headers=headers_binance, timeout=15)
    symbol_meta = {s["symbol"]: s for s in ex_info.get("symbols", [])}

    # 2. 获取币安全部 USDT 永续合约实时行情、费率与结算周期 (fundingIntervalHours)
    t_url = "https://www.binance.com/fapi/v1/ticker/24hr"
    p_url = "https://www.binance.com/fapi/v1/premiumIndex"
    f_url = "https://www.binance.com/fapi/v1/fundingInfo"
    
    tickers_list = fetch_json_with_retry(t_url, headers=headers_binance, timeout=15)
    tickers = {t["symbol"]: t for t in tickers_list if t["symbol"].endswith("USDT")}
    premiums_list = fetch_json_with_retry(p_url, headers=headers_binance, timeout=15)
    premiums = {p["symbol"]: p for p in premiums_list if p["symbol"].endswith("USDT")}
    try:
        funding_infos = fetch_json_with_retry(f_url, headers=headers_binance, timeout=15)
        funding_intervals = {item["symbol"]: int(item.get("fundingIntervalHours") or 8) for item in funding_infos}
    except Exception as e:
        print(f"获取 fundingInfo 警告: {e}，默认按 8h 结算")
        funding_intervals = {}

    targets = []
    base_symbols = []
    for sym, tick in tickers.items():
        meta = symbol_meta.get(sym, {})

        # 【核心风控】：严格底层资产类型与交易状态过滤，彻底排除 TradFi 股票/股权/外汇/大宗商品及已停牌/交割合约
        status_sym = meta.get("status")
        c_type = meta.get("contractType")
        u_type = meta.get("underlyingType")
        sub_types = meta.get("underlyingSubType") or []
        
        # 必须是标准正常交易中的加密货币永续合约 (TRADING + PERPETUAL + COIN + Crypto)，严禁 TradFi
        if status_sym != "TRADING" or c_type != "PERPETUAL" or u_type != "COIN" or "TradFi" in sub_types or "Crypto" not in sub_types:
            continue

        base = sym.removesuffix("USDT")
        mult = 1.0
        if base.startswith("1000000"):
            base = base[7:]; mult = 1000000.0
        elif base.startswith("100000"):
            base = base[6:]; mult = 100000.0
        elif base.startswith("10000"):
            base = base[5:]; mult = 10000.0
        elif base.startswith("1000"):
            base = base[4:]; mult = 1000.0
        elif base.startswith("1M") and len(base) > 2 and base[2].isalpha():
            base = base[2:]; mult = 1000000.0
            
        # 允许标准英文字母及币安官方上线的合法中文加密币标的（如龙虾、哈基米等），排除美股/传统金融通证
        if not re.match(r"^([A-Z0-9]+|[\u4e00-\u9fa5]+)$", base) or base in EXCLUDED_STOCKS:
            continue
            
        fr = float(premiums.get(sym, {}).get("lastFundingRate", 0))
        vol = float(tick.get("quoteVolume", 0))
        p_chg = float(tick.get("priceChangePercent", 0))
        last_p = float(tick.get("lastPrice", 0)) / mult
        high_p = float(tick.get("highPrice", 0)) / mult
        low_p = float(tick.get("lowPrice", 0)) / mult
        interval = max(1, funding_intervals.get(sym, 8))
        
        # 统一折算基准：换算为标准 4 小时资金费率 (4h 等效) 与 24 小时累计资费
        fr_4h = fr * (4.0 / interval)
        fr_24h = fr * (24.0 / interval)
        fr_apr = fr_24h * 365.0
        
        # 【全量纳入】：保证所有币安纯加密标的 100% 进入 CMC 市值核验
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
            "funding_rate_4h": fr_4h,
            "funding_rate_24h": fr_24h,
            "funding_rate_apr": fr_apr,
            "volume_usd": vol
        })
        base_symbols.append(base)

    # 去重
    unique_bases = sorted(list(set(base_symbols)))
    print(f"待向 CMC 官方 Pro API 批量验证纯加密币标的: {len(unique_bases)} 个 (已通过币安 exchangeInfo 纯加密过滤)")

    # 分批向 CMC Pro API 请求实时权威报价与流通市值 (每批 40 个，带针对 429 的指数退避重试，确保安全与完整性)
    cmc_data = {}
    cmc_data_upper = {}
    chunk_size = 40
    for i in range(0, len(unique_bases), chunk_size):
        chunk = unique_bases[i:i+chunk_size]
        chunk_encoded = [urllib.parse.quote(s) for s in chunk]
        url = f"https://pro-api.coinmarketcap.com/v1/cryptocurrency/quotes/latest?symbol={','.join(chunk_encoded)}"
        req = urllib.request.Request(url, headers={"X-CMC_PRO_API_KEY": cmc_key, "Accepts": "application/json"})
        for attempt in range(4):
            try:
                res = json.loads(urllib.request.urlopen(req, timeout=15).read().decode())
                batch_data = res.get("data", {})
                cmc_data.update(batch_data)
                for k, v in batch_data.items():
                    cmc_data_upper[k.upper()] = v
                break
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    # 遭遇 CMC 429 频控限制，专门退避等待
                    wait_sec = 2.5 * (attempt + 1)
                    if attempt == 3:
                        print(f"CMC 频控 429 异常 (batch {i}, 已重试4次): {e}")
                    else:
                        time.sleep(wait_sec)
                else:
                    if attempt == 3:
                        print(f"CMC HTTP 异常 (batch {i}, 已重试4次): {e}")
                    else:
                        time.sleep(1.5 * (attempt + 1))
            except Exception as e:
                if attempt == 3:
                    print(f"CMC 请求异常 (batch {i}, 已重试4次): {e}")
                else:
                    time.sleep(1.5 * (attempt + 1))
        time.sleep(0.12)

    # 严格校验市值与价格吻合度
    verified_candidates = []
    for t in targets:
        base = t["base"]
        c_obj = cmc_data.get(base) or cmc_data_upper.get(base.upper())
        if not c_obj:
            continue
            
        # 若 CMC 返回同名多代币，按价格接近度锁定正主
        if isinstance(c_obj, list):
            best_match = None
            min_diff = 999
            for item in c_obj:
                qp = item.get("quote", {}).get("USD", {}).get("price")
                if qp is None:
                    continue
                diff = abs(qp - t["binance_price"]) / max(t["binance_price"], 1e-6)
                if diff < min_diff:
                    min_diff = diff
                    best_match = item
            c_obj = best_match if min_diff < 0.25 else None
            
        if not c_obj:
            continue
            
        quote = c_obj.get("quote", {}).get("USD", {})
        cmc_price = quote.get("price")
        if cmc_price is None:
            continue
        mcap = quote.get("market_cap") or 0
        cs = c_obj.get("circulating_supply") or 0
        rank = c_obj.get("cmc_rank") or 9999
        
        # 价格容差检验 (必须在 25% 以内，防止同名串币)
        price_diff = abs(cmc_price - t["binance_price"]) / max(t["binance_price"], 1e-6)
        if price_diff > 0.25:
            continue
            
        # 流通市值筛选：当前严格 < 3,000 万美元，或曾经跌破 3000wu 且市值超过后处于 7 天跟踪期内 (< 8000wu 防异常)
        is_curr_sub_30m = (500_000 < mcap <= 30_000_000)
        
        hist_entry = sub_30m_history.get(t["symbol"])
        days_since_sub_30m = 999.0
        is_hist_sub_30m = False
        if hist_entry:
            days_since_sub_30m = (now_ts - hist_entry.get("last_sub_30m_ts", 0)) / 86400.0
            if days_since_sub_30m <= 7.0 and mcap <= 80_000_000:
                is_hist_sub_30m = True

        # 【核心修正】：t["low_price"] 已经除以 mult 为单代币价格，直接乘以流通量 cs，严禁二次乘以 multiplier
        mcap_24h_low = t["low_price"] * cs if cs > 0 else mcap
        is_24h_low_sub_30m = (500_000 < mcap_24h_low <= 30_000_000 and mcap <= 80_000_000)

        if is_curr_sub_30m or is_hist_sub_30m or is_24h_low_sub_30m:
            curr_min = min(mcap, mcap_24h_low)
            if is_curr_sub_30m:
                prev_min = hist_entry.get("min_mcap_usd") if hist_entry else None
                sub_30m_history[t["symbol"]] = {
                    "symbol": t["symbol"],
                    "base": base,
                    "last_sub_30m_ts": now_ts,
                    "last_sub_30m_iso": now_iso,
                    "min_mcap_usd": min(prev_min, curr_min) if prev_min is not None else curr_min
                }
                is_overflow_7d = False
                days_left = 7.0
            else:
                is_overflow_7d = True
                if is_24h_low_sub_30m:
                    # 若无历史记录，或上次跌破在 24h 之前，更新跌破时间戳与低点市值
                    if not hist_entry or (now_ts - hist_entry.get("last_sub_30m_ts", 0)) > 86400:
                        prev_min = hist_entry.get("min_mcap_usd") if hist_entry else None
                        sub_30m_history[t["symbol"]] = {
                            "symbol": t["symbol"],
                            "base": base,
                            "last_sub_30m_ts": now_ts - 3600,
                            "last_sub_30m_iso": now_iso,
                            "min_mcap_usd": min(prev_min, mcap_24h_low) if prev_min is not None else mcap_24h_low
                        }
                        days_since_sub_30m = 3600 / 86400.0
                days_left = max(0.1, round(7.0 - days_since_sub_30m, 1))

            turnover = (t["volume_usd"] / mcap) if mcap > 0 else 0
            interval = t["funding_interval_hours"]
            fr_4h_pct = t["funding_rate_4h"] * 100
            
            # 状态诊断 (基于标准化 4h 费率与结算周期)
            if fr_4h_pct <= -0.25:
                status = f"🔥 超极限逼空贴水 (4h等效{fr_4h_pct:.2f}%, {interval}h期)"
            elif fr_4h_pct <= -0.05:
                status = f"⚡ 强力逼空贴水 (4h等效{fr_4h_pct:.2f}%, {interval}h期)"
            elif turnover > 2.0:
                status = f"巨量高换手清洗中 ({turnover:.1f}x)"
            elif fr_4h_pct < 0:
                status = f"轻度负费率蓄势 ({interval}h期)"
            else:
                status = f"低市值低速收敛观察 ({interval}h期)"

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
                "effective_circulating_supply": (cs / t["multiplier"]) if t["multiplier"] > 0 else cs,
                "cmc_rank": rank,
                "funding_interval_hours": interval,
                "funding_rate_pct": round(t["funding_rate"] * 100, 4),
                "funding_rate_4h_pct": round(fr_4h_pct, 4),
                "funding_rate_24h_pct": round(t["funding_rate_24h"] * 100, 4),
                "funding_rate_annual_pct": round(t["funding_rate_apr"] * 100, 2),
                "volume_24h_usd": t["volume_usd"],
                "turnover_ratio": round(turnover, 2),
                "status": status
            })

    # 并发拉取所有候选标的的 1h 级别 K 线，计算 1h EMA5 / EMA10 顺势指标与共振入场时长
    print(f"\n[*] 正在并发拉取 {len(verified_candidates)} 个候选标的 1h 级别 K 线并计算 EMA5/EMA10 趋势与共振时长...")
    def check_1h_klines(sym):
        encoded_sym = urllib.parse.quote(sym)
        url = f"https://www.binance.com/fapi/v1/klines?symbol={encoded_sym}&interval=1h&limit=168"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        for attempt in range(2):
            try:
                data = json.loads(urllib.request.urlopen(req, timeout=8).read().decode())
                if not data or len(data) < 10:
                    return sym, None, None, False, 0.0, 0, ""
                
                closes = [float(k[4]) for k in data]
                times = [int(k[0]) // 1000 for k in data]
                
                def calc_ema(values, period):
                    if len(values) < period:
                        return values[-1] if values else 0.0
                    alpha = 2.0 / (period + 1)
                    ema = sum(values[:period]) / period
                    for v in values[period:]:
                        ema = alpha * v + (1 - alpha) * ema
                    return ema
                    
                ema5 = calc_ema(closes, 5)
                ema10 = calc_ema(closes, 10)
                is_bull = ema5 > ema10
                spread = ((ema5 - ema10) / ema10) * 100 if ema10 > 0 else 0.0

                # 计算连续 1h EMA5 > EMA10 顺势柱数（共振时长基础）
                bull_streak = 0
                if len(closes) >= 10:
                    for i in range(len(closes) - 1, 9, -1):
                        sub_closes = closes[:i + 1]
                        e5 = calc_ema(sub_closes, 5)
                        e10 = calc_ema(sub_closes, 10)
                        if e5 > e10:
                            bull_streak += 1
                        else:
                            break
                entry_ts = times[len(times) - bull_streak] if bull_streak > 0 else times[-1]
                entry_dt_str = datetime.datetime.fromtimestamp(
                    entry_ts, datetime.timezone(datetime.timedelta(hours=8))
                ).strftime('%m-%d %H:%M') if bull_streak > 0 else ""

                return sym, round(ema5, 8), round(ema10, 8), is_bull, round(spread, 3), bull_streak, entry_dt_str
            except Exception:
                if attempt == 0:
                    time.sleep(0.3)
                    continue
                return sym, None, None, False, 0.0, 0, ""

    symbols_list = [c["symbol"] for c in verified_candidates]
    with ThreadPoolExecutor(max_workers=25) as executor:
        kline_results = {r[0]: r[1:] for r in executor.map(check_1h_klines, symbols_list)}

    # 加载已记录的共振历史记忆 (精确追踪同时满足 1h顺势+换手>=1.5x 的真实共振驻留时长)
    resonance_state_file = ROOT / "runs" / "resonance_state.json"
    res_history = {}
    if resonance_state_file.exists():
        try:
            with open(resonance_state_file, "r", encoding="utf-8") as f:
                res_history = json.load(f).get("history", {})
        except Exception:
            res_history = {}

    for c in verified_candidates:
        sym = c["symbol"]
        res = kline_results.get(sym, (None, None, False, 0.0, 0, ""))
        ema5, ema10, is_ema_bull, ema_spread, bull_streak, entry_dt_str = res
        c["ema5"] = ema5
        c["ema10"] = ema10
        c["is_ema_bull"] = is_ema_bull
        c["ema_spread_pct"] = ema_spread
        c["ema_streak_hours"] = bull_streak if is_ema_bull else 0

        is_neg_fr = c["funding_rate_4h_pct"] < 0
        is_high_tr = c["turnover_ratio"] >= 1.5
        is_resonance = is_ema_bull and is_high_tr  # 黄金双核共振：1h顺势 + 换手>=1.5x

        # 精确计算“共振驻留时长” (而非单纯的1h均线顺势时长):
        if is_resonance:
            res_hist_item = res_history.get(sym)
            if res_hist_item and "first_detected" in res_hist_item:
                try:
                    f_dt = datetime.datetime.strptime(res_hist_item["first_detected"], "%Y-%m-%d %H:%M:%S")
                    f_ts = f_dt.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8))).timestamp()
                    dur_h = max(0, int((now_ts - f_ts) // 3600))
                    ent_str = f_dt.strftime("%m-%d %H:%M")
                except Exception:
                    dur_h = 0
                    ent_str = datetime.datetime.fromtimestamp(now_ts, datetime.timezone(datetime.timedelta(hours=8))).strftime("%m-%d %H:%M")
            else:
                dur_h = 0
                ent_str = datetime.datetime.fromtimestamp(now_ts, datetime.timezone(datetime.timedelta(hours=8))).strftime("%m-%d %H:%M")
            c["resonance_duration_hours"] = dur_h
            c["resonance_entry_time"] = ent_str
        else:
            c["resonance_duration_hours"] = 0
            c["resonance_entry_time"] = ""

        zones = []
        if is_resonance:
            if is_neg_fr:
                zones.append("贴水双核(空头浓厚)")
            else:
                zones.append("双核黄金共振")
        else:
            if is_ema_bull: zones.append("1h顺势")
            if is_high_tr: zones.append("高换手")
            if is_neg_fr: zones.append("负费率贴水")

        c["is_neg_funding"] = is_neg_fr
        c["is_high_turnover"] = is_high_tr
        c["is_resonance"] = is_resonance
        c["zones"] = zones
        
        # 评分机制 (用户风控准则：负费率大多伴随阴跌/空头过度堆积，极少真逼空，故取消逼空加分，负费率扣分降权)：
        # 👑 双核黄金共振 (1h顺势 + 高换手 + 正常费率) = 2 (最高优先级)
        # ⚠️ 贴水双核 (1h顺势 + 高换手 + 负费率贴水) = 1 (降权扣分，置于黄金共振之后)
        # 单机会区 (无负费率) = 1
        # 单纯负费率或其他 = 0
        if is_resonance and not is_neg_fr:
            res_score = 2
        elif is_resonance and is_neg_fr:
            res_score = 1
        elif (is_high_tr or is_ema_bull) and not is_neg_fr:
            res_score = 1
        else:
            res_score = 0
        c["resonance_score"] = res_score

        # 动态更新 status 状态诊断
        overflow_tag = f" ★7d溢出跟踪(剩{c['overflow_days_left']:.1f}天)" if c.get("is_overflow_7d") else ""
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

    # 排序：
    # 1. resonance_score 降序 (2 黄金双核 > 1 贴水双核/单区 > 0)
    # 2. is_neg_funding 升序 (0 无负费率优先排前，1 负费率降权置底)
    # 3. turnover_ratio 降序 (换手越高越优先)
    # 4. funding_rate_4h_pct 降序 (健康正费率优先，深度负费率排在后)
    verified_candidates.sort(key=lambda x: (
        -x["resonance_score"],
        1 if x.get("is_neg_funding") else 0,
        -x["turnover_ratio"],
        -x["funding_rate_4h_pct"]
    ))
    
    # 防灾兜底：严禁在未拉取到任何数据时覆盖破坏历史数据文件
    if not verified_candidates:
        raise RuntimeError("❌ 致命异常：未拉取到任何有效候选标的，拒绝写入 runs/ 以防清空历史数据！")

    out_file = ROOT / "runs" / "second_leg_candidates_pool.json"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(verified_candidates, f, indent=2, ensure_ascii=False)

    with open(history_file, "w", encoding="utf-8") as f:
        json.dump(sub_30m_history, f, indent=2, ensure_ascii=False)
        
    n_overflow = len([c for c in verified_candidates if c.get('is_overflow_7d')])
    print(f"\n✅ 官方权威校验完成！纯加密候选标的共 {len(verified_candidates)} 个 (含 {n_overflow} 个 7 天溢出跟踪标的):")
    print(f"  • 👑 双核黄金共振标的 (健康费率): {len([c for c in verified_candidates if c['resonance_score'] == 2])} 个")
    print(f"  • ⚠️ 贴水双核标的 (负费率警示): {len([c for c in verified_candidates if c.get('is_resonance') and c.get('is_neg_funding')])} 个")
    print(f"  • 第一类 负费率贴水区 (< 0): {len([c for c in verified_candidates if c['is_neg_funding']])} 个")
    print(f"  • 第二类 巨量高换手区 (≥ 1.5x): {len([c for c in verified_candidates if c['is_high_turnover']])} 个")
    print(f"  • 第三类 1h EMA5>EMA10 顺势区: {len([c for c in verified_candidates if c['is_ema_bull']])} 个")
    print(f"  • ★ 7天溢出跟踪标的 (曾<3000wu现突破): {n_overflow} 个")
    print("\n【前 15 大潜在交易机会标的】：")
    for c in verified_candidates[:15]:
        zones_str = "+".join(c["zones"]) if c["zones"] else "基础底池"
        dur_str = f"{c['resonance_duration_hours']}h" if c.get("resonance_duration_hours") else "-"
        print(f"  • {c['symbol']:12s} | 机会区:[{zones_str:^15s}] | 共振时长:{dur_str:>4s} | 4h费率:{c['funding_rate_4h_pct']:+.4f}% | 换手:{c['turnover_ratio']:4.1f}x | 市值:${c['market_cap_usd']/1e6:5.2f}M")

if __name__ == "__main__":
    scan_pool()
