#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
账户量化风控数据分析引擎 (Account Quant Analytics Engine)
==============================================================================
功能:
1. 读取币安实盘合约 API (.env 中的 BINANCE_API_KEY / BINANCE_API_SECRET):
   - /fapi/v2/account: 钱包余额、保证金、未实现浮盈、最大可提款金额
   - /fapi/v1/income: 资金流水明细 (已实现盈亏、手续费、资金费率、转账)
   - /fapi/v1/userTrades: 交易对历史成交全量明细
   - /fapi/v2/positionRisk: 活跃持仓风险与杠杆
   - 具备完备的 HMAC-SHA256 签名鉴权、时间校准与网络异常/HTTP 451 优雅降级回退。
2. 深度整合本地 runs/trade_positions.json 持仓记录:
   - 补全开仓均价、平仓均价、盈亏金额、盈亏比例 (PnL % & ROE %)、持仓时长、
     平仓原因 (移动保本/趋势线跌破/手动/币安等)、关联对数趋势线 (CWxqFZ 等)。
3. 量化风控核心指标严谨核算:
   - 总资金净值 (Total Equity)、可用保证金 (Available Margin)、保证金占用
   - 累计实现盈亏 (Cumulative Realized PnL)、未结浮盈 (Unrealized PnL)、总收益率 (%)
   - 胜率 (Win Rate %)、盈亏比 (Profit Factor)、收益风险比 (Payoff Ratio)
   - 最大回撤金额与比例 (Max Drawdown $ / %)、峰值与谷值记录
   - 年化夏普比率 (Sharpe Ratio)、索提诺比率 (Sortino Ratio)
   - 平均单笔盈亏 ($ / %)、平均盈利/亏损、平均持仓周期、最大连胜/连亏次数
4. 生成资金净值走势曲线 (Equity Curve timeseries) 与每日盈亏统计 (Daily PnL)。
5. 产出结构化量化报告 runs/account_analytics.json，并提供清晰的命令行统计输出。
==============================================================================
"""

import os
import sys
import math
import time
import json
import logging
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple

# 项目基准路径
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
RUNS_DIR = PROJECT_ROOT / "runs"
INCOME_PAGE_LIMIT = 1000
DEFAULT_POSITIONS_PATH = RUNS_DIR / "trade_positions.json"
DEFAULT_OUTPUT_PATH = RUNS_DIR / "account_analytics.json"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 导入币安合约客户端
try:
    from binance_position_client import BinanceFuturesClient, clean_symbol
except ImportError:
    try:
        from scripts.binance_position_client import BinanceFuturesClient, clean_symbol
    except ImportError:
        BinanceFuturesClient = None
        clean_symbol = lambda s: s.strip().upper()

logger = logging.getLogger("account_analytics")
BEIJING_TZ = timezone(timedelta(hours=8))


def get_beijing_time_str(ts: Optional[float] = None) -> str:
    """获取格式化北京时间字符串 YYYY-MM-DD HH:MM:SS"""
    if ts is None:
        dt = datetime.now(BEIJING_TZ)
    else:
        dt = datetime.fromtimestamp(ts, tz=BEIJING_TZ)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def parse_to_timestamp(dt_val: Any) -> float:
    """
    智能解析多种时间格式为 Unix 秒级时间戳 (float):
    支持 'YYYY-MM-DD HH:MM:SS', 'YYYY-MM-DDTHH:MM:SS', 毫秒时间戳, 秒级时间戳。
    """
    if dt_val is None:
        return time.time()
    if isinstance(dt_val, (int, float)):
        # 大于 1e11 判定为毫秒
        return float(dt_val / 1000.0) if dt_val > 1e11 else float(dt_val)

    s = str(dt_val).strip()
    if not s:
        return time.time()

    # 处理数字字符串
    try:
        val = float(s)
        return float(val / 1000.0) if val > 1e11 else float(val)
    except ValueError:
        pass

    # 处理常见日期时间格式
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d"
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(s, fmt)
            # 假定无时区标注的时间默认为北京时间 (UTC+8)
            dt = dt.replace(tzinfo=BEIJING_TZ)
            return dt.timestamp()
        except ValueError:
            continue

    logger.debug(f"无法解析的时间格式: {dt_val}，使用当前时间")
    return time.time()


def format_duration(seconds: int) -> str:
    """将秒数格式化为人类可读的持仓周期字符串 (如 12m 26s, 3h 15m)"""
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


def atomic_write_json(filepath: Path, data: Any):
    """原子化写入 JSON 文件，防止并发读取冲突"""
    filepath = Path(filepath)
    filepath.parent.mkdir(parents=True, exist_ok=True)
    temp_path = filepath.with_name(f"{filepath.name}.tmp.{os.getpid()}")
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        temp_path.replace(filepath)
    except Exception as e:
        logger.error(f"写入 JSON 失败 {filepath}: {e}")
        if temp_path.exists():
            try:
                temp_path.unlink()
            except Exception:
                pass
        raise


class AccountAnalyticsEngine:
    """账户量化风控数据分析与指标聚合引擎"""

    def __init__(
        self,
        client: Optional[Any] = None,
        positions_path: Path = DEFAULT_POSITIONS_PATH,
        initial_capital: Optional[float] = None,
        fee_rate: float = 0.0005,
        default_leverage: int = 5,
        offline_mode: bool = False
    ):
        self.positions_path = Path(positions_path)
        self.custom_initial_capital = float(initial_capital) if initial_capital is not None else None
        self.initial_capital = self.custom_initial_capital if self.custom_initial_capital is not None else 10000.0
        self.fee_rate = float(fee_rate)
        self.default_leverage = int(default_leverage)
        self.offline_mode = bool(offline_mode)

        if client is not None:
            self.client = client
        elif not self.offline_mode and BinanceFuturesClient:
            self.client = BinanceFuturesClient.from_env()
        else:
            self.client = None

    def fetch_binance_data(self) -> Tuple[Optional[dict], List[dict], List[dict], dict]:
        """
        拉取币安实盘合约数据 (/fapi/v2/account, /fapi/v1/income, /fapi/v2/positionRisk)。
        处理签名鉴权与 HTTP 451/网络异常优雅降级。
        返回: (account_info, income_list, active_positions_binance, api_status)
        """
        api_status = {
            "configured": False,
            "connected": False,
            "positions_available": False,
            "income_available": False,
            "status_code": 0,
            "message": "Offline Mode / Client Unconfigured",
            "mode": "OFFLINE_FALLBACK",
            "last_sync": get_beijing_time_str()
        }

        if self.offline_mode or not self.client or not self.client.is_configured():
            logger.info("ℹ️ 运行在离线/本地回退模式 (Offline Mode)。")
            return None, [], [], api_status

        api_status["configured"] = True

        try:
            # 1. 请求 /fapi/v2/account
            account_info, err_acc = self.client._send_signed_request("/fapi/v2/account")
            if err_acc:
                api_status["status_code"] = err_acc.get("code", 0)
                api_status["message"] = str(err_acc.get("msg", "API Error"))
                logger.warning(f"⚠️ 币安 /fapi/v2/account 请求异常 [{err_acc.get('code')}]: {err_acc.get('msg')}，平滑降级至本地离线模式。")
                return None, [], [], api_status

            # 2. 请求 /fapi/v1/income
            income_list, income_error = self.client._send_signed_request(
                "/fapi/v1/income", {"limit": INCOME_PAGE_LIMIT}
            )
            api_status["income_available"] = income_error is None and isinstance(income_list, list)
            if not api_status["income_available"]:
                income_list = []

            # 3. 请求 /fapi/v2/positionRisk
            pos_data, positions_error = self.client._send_signed_request("/fapi/v2/positionRisk")
            api_status["positions_available"] = positions_error is None and isinstance(pos_data, list)
            active_positions_binance = []
            if api_status["positions_available"]:
                for p in pos_data:
                    amt = float(p["positionAmt"])
                    if amt > 0:
                        active_positions_binance.append(p)

            api_status["connected"] = True
            api_status["status_code"] = 200
            if api_status["income_available"] and api_status["positions_available"]:
                api_status["message"] = "Connected to Binance Live Futures API"
                api_status["mode"] = "BINANCE_REALTIME"
                logger.info("✅ 成功连接币安实盘合约 API 并同步账户、流水与持仓数据。")
            else:
                api_status["message"] = "Binance income or position data unavailable"
                api_status["mode"] = "BINANCE_PARTIAL"
                logger.warning("币安账户已连接，持仓或资金流水查询失败。")
            return account_info, income_list, active_positions_binance, api_status

        except Exception as e:
            logger.warning(f"⚠️ 币安接口网络调用发生未处理异常: {e}，优雅降级至离线模式。")
            api_status["status_code"] = -1
            api_status["message"] = str(e)
            return None, [], [], api_status

    def load_local_positions_store(self) -> Tuple[List[dict], List[dict]]:
        """从 runs/trade_positions.json 加载本地活跃持仓与历史开平仓记录"""
        if not self.positions_path.exists():
            logger.warning(f"持仓数据文件不存在: {self.positions_path}")
            return [], []

        try:
            with open(self.positions_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                positions = data.get("positions", [])
                history = data.get("history", [])
                return positions, history
        except Exception as e:
            logger.error(f"读取本地持仓数据异常: {e}")
            return [], []

    def enrich_trade_record(self, raw: dict, index: int) -> dict:
        """
        深度清洗与补全历史平仓交易记录:
        - 开平仓均价、持仓量、名义价值
        - 盈亏金额、盈亏比例 (PnL %)、杠杆收益率 (ROE %)
        - 手续费与净盈亏 (Net Realized PnL)
        - 持仓时长与格式化字符串
        - 平仓原因 (移动保本/跌破趋势线/手动/币安等)
        - 关联对数趋势线元数据
        """
        sym = clean_symbol(raw.get("symbol", "UNKNOWN"))
        entry_price = float(raw.get("entry_price") or 0.0)
        amount = float(raw.get("amount") or 0.0)
        leverage = int(raw.get("leverage") or self.default_leverage)

        # 确定平仓价: exit_price -> current_price -> highest_price -> entry_price
        exit_price = raw.get("exit_price")
        if exit_price is None or float(exit_price) <= 0:
            exit_price = raw.get("current_price") or raw.get("highest_price") or entry_price
        exit_price = float(exit_price)

        entry_time_str = raw.get("entry_time", "")
        closed_at_str = raw.get("closed_at") or raw.get("exit_time") or get_beijing_time_str()

        entry_ts = parse_to_timestamp(entry_time_str)
        closed_ts = parse_to_timestamp(closed_at_str)
        duration_seconds = max(0, int(closed_ts - entry_ts))
        duration_str = format_duration(duration_seconds)

        entry_notional = round(entry_price * amount, 4)
        exit_notional = round(exit_price * amount, 4)

        # 多头合约收益核算
        realized_pnl = round((exit_price - entry_price) * amount, 4)
        pnl_pct = round(((exit_price - entry_price) / entry_price * 100.0), 2) if entry_price > 0 else 0.0
        roe_pct = round(pnl_pct * leverage, 2)

        # 手续费核算 (若已有实际手续费取实际值，否则按名义本金与万五标准费率估算)
        fee = raw.get("fee")
        if fee is None or float(fee) < 0:
            fee = round((entry_notional + exit_notional) * self.fee_rate, 4)
        else:
            fee = float(fee)

        net_pnl = round(realized_pnl - fee, 4)

        # 胜负判定
        if realized_pnl > 1e-4:
            outcome = "WIN"
        elif realized_pnl < -1e-4:
            outcome = "LOSS"
        else:
            outcome = "BREAKEVEN"

        # 平仓原因识别与标准化
        status = str(raw.get("status", "")).upper()
        trailing_triggered = bool(raw.get("trailing_stop_triggered", False))

        if trailing_triggered or "TRAILING" in status:
            close_reason = "动态移动保本锁利"
            close_reason_code = "TRAILING_PROFIT_LOCK"
        elif status == "STOP_LOSS_TRIGGERED":
            if exit_price >= entry_price:
                close_reason = "动态移动保本锁利"
                close_reason_code = "TRAILING_PROFIT_LOCK"
            else:
                close_reason = "跌破趋势线止损"
                close_reason_code = "TRENDLINE_BREAK_STOP"
        elif status == "BREAK_TRENDLINE_STOP":
            close_reason = "跌破趋势线止损"
            close_reason_code = "TRENDLINE_BREAK_STOP"
        elif status == "BINANCE_POSITION_CLOSED":
            close_reason = "币安实盘平仓"
            close_reason_code = "BINANCE_AUTO_CLOSED"
        elif status == "MANUAL_CLOSE":
            close_reason = "手动平仓"
            close_reason_code = "MANUAL_CLOSE"
        elif status == "TAKE_PROFIT":
            close_reason = "止盈离场"
            close_reason_code = "TAKE_PROFIT"
        else:
            close_reason = raw.get("notes") or status or "平仓归档"
            close_reason_code = "OTHER_CLOSE"

        # 关联趋势线信息提取
        trendline = raw.get("trendline")
        trendline_id = None
        trendline_label = "--"
        trendline_info = None

        if isinstance(trendline, dict):
            trendline_id = trendline.get("id") or trendline.get("line_id")
            p1 = trendline.get("p1")
            p2 = trendline.get("p2")
            dt1 = trendline.get("dt1_str") or ""
            dt2 = trendline.get("dt2_str") or ""
            color = trendline.get("line_color") or trendline.get("state", {}).get("linecolor", "#2196f3")
            trendline_info = {
                "id": trendline_id,
                "p1": p1,
                "p2": p2,
                "dt1_str": dt1,
                "dt2_str": dt2,
                "color": color,
                "interval": trendline.get("interval", "15")
            }
            if trendline_id and p1 and p2:
                trendline_label = f"TL#{trendline_id} ({p1:.4f} -> {p2:.4f})"
            elif trendline_id:
                trendline_label = f"TL#{trendline_id}"

        trade_id = f"TRD-{index+1:03d}-{sym}-{int(closed_ts)}"

        return {
            "trade_id": trade_id,
            "symbol": sym,
            "side": "LONG",
            "entry_price": entry_price,
            "exit_price": exit_price,
            "amount": amount,
            "entry_notional": entry_notional,
            "exit_notional": exit_notional,
            "realized_pnl": realized_pnl,
            "fee": fee,
            "net_pnl": net_pnl,
            "pnl_pct": pnl_pct,
            "roe_pct": roe_pct,
            "leverage": leverage,
            "entry_time": entry_time_str or get_beijing_time_str(entry_ts),
            "closed_at": closed_at_str or get_beijing_time_str(closed_ts),
            "entry_ts": int(entry_ts * 1000),
            "closed_ts": int(closed_ts * 1000),
            "duration_seconds": duration_seconds,
            "duration_str": duration_str,
            "outcome": outcome,
            "status": status,
            "close_reason": close_reason,
            "close_reason_code": close_reason_code,
            "highest_price": float(raw.get("highest_price") or exit_price),
            "trailing_stop_triggered": trailing_triggered,
            "trailing_stop_price": raw.get("trailing_stop_price"),
            "trendline_id": trendline_id,
            "trendline_label": trendline_label,
            "trendline_info": trendline_info,
            "source": raw.get("source", "MANUAL"),
            "notes": raw.get("notes", "")
        }

    def enrich_active_position(self, raw: dict, index: int) -> dict:
        """
        深度清洗与补全当前活跃持仓:
        - 开仓均价、标记价、持仓数量、名义价值与保证金占用
        - 浮动盈亏 ($ & %)、ROE (%)
        - 预估强平价与移动止损/趋势线止损价位
        """
        sym = clean_symbol(raw.get("symbol", "UNKNOWN"))
        entry_price = float(raw.get("entry_price") or raw.get("entryPrice") or 0.0)
        amount = float(raw.get("amount") or raw.get("positionAmt") or 0.0)
        leverage = int(raw.get("leverage") or self.default_leverage)

        # 获取标记价/现价
        mark_price = float(raw.get("mark_price") or raw.get("markPrice") or raw.get("current_price") or entry_price)
        if mark_price <= 0:
            mark_price = entry_price

        notional = round(mark_price * amount, 4)
        initial_margin = round(notional / leverage, 4) if leverage > 0 else notional

        unrealized_pnl = float(raw.get("unrealized_pnl") or raw.get("unRealizedProfit") or ((mark_price - entry_price) * amount))
        unrealized_pnl = round(unrealized_pnl, 4)

        pnl_pct = round(((mark_price - entry_price) / entry_price * 100.0), 2) if entry_price > 0 else 0.0
        roe_pct = round(pnl_pct * leverage, 2)

        # 估算多头强平价格: Entry * (1 - 0.9 / Leverage)
        est_liq_price = round(entry_price * (1.0 - 0.9 / leverage), 6) if leverage > 1 else 0.0

        entry_time_str = raw.get("entry_time", "")
        entry_ts = parse_to_timestamp(entry_time_str)
        now_ts = time.time()
        holding_seconds = max(0, int(now_ts - entry_ts))
        holding_str = format_duration(holding_seconds)

        # 趋势线
        trendline = raw.get("trendline")
        trendline_id = trendline.get("id") if isinstance(trendline, dict) else None

        return {
            "position_id": f"POS-{index+1:02d}-{sym}",
            "symbol": sym,
            "side": "LONG",
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
            "entry_time": entry_time_str or get_beijing_time_str(entry_ts),
            "entry_ts": int(entry_ts * 1000),
            "holding_seconds": holding_seconds,
            "holding_str": holding_str,
            "highest_price": float(raw.get("highest_price") or mark_price),
            "trailing_stop_triggered": bool(raw.get("trailing_stop_triggered", False)),
            "trailing_stop_price": raw.get("trailing_stop_price"),
            "stop_loss_notified": bool(raw.get("stop_loss_notified", False)),
            "trendline_id": trendline_id,
            "trendline": trendline,
            "source": raw.get("source", "MANUAL"),
            "status": "OPEN",
            "notes": raw.get("notes", "")
        }

    def reconstruct_binance_trades(
        self,
        income_list: List[dict],
        local_history: List[dict]
    ) -> List[dict]:
        """
        从币安 /fapi/v1/income 真实流水中聚合并重构成已平仓交易记录:
        - 按 symbol 与时间窗口 (15s) 聚合并拆解各笔交易的 Realized PnL 与 Commission
        - 与本地 trade_positions.json 的 local_history 交叉比对，保留移动保本、趋势线等丰富元数据
        """
        if not income_list:
            return []

        pnl_items = [
            x for x in income_list
            if x.get("incomeType") in ("REALIZED_PNL", "COMMISSION") and x.get("symbol")
        ]
        if not pnl_items:
            return []

        pnl_items.sort(key=lambda x: x.get("time", 0))

        clusters = []
        curr = None
        for item in pnl_items:
            sym = clean_symbol(item.get("symbol", ""))
            t = item.get("time", 0)
            itype = item.get("incomeType")
            val = float(item.get("income", 0.0))

            if curr is None:
                curr = {
                    "symbol": sym,
                    "start_t": t,
                    "end_t": t,
                    "realized_pnl": 0.0,
                    "fee": 0.0,
                    "pnl_count": 0,
                    "comm_count": 0
                }

            if curr["symbol"] == sym and (t - curr["end_t"]) <= 15000:
                curr["end_t"] = t
                if itype == "REALIZED_PNL":
                    curr["realized_pnl"] += val
                    curr["pnl_count"] += 1
                elif itype == "COMMISSION":
                    curr["fee"] += abs(val)
                    curr["comm_count"] += 1
            else:
                if curr["pnl_count"] > 0:
                    clusters.append(curr)
                curr = {
                    "symbol": sym,
                    "start_t": t,
                    "end_t": t,
                    "realized_pnl": val if itype == "REALIZED_PNL" else 0.0,
                    "fee": abs(val) if itype == "COMMISSION" else 0.0,
                    "pnl_count": 1 if itype == "REALIZED_PNL" else 0,
                    "comm_count": 1 if itype == "COMMISSION" else 0
                }

        if curr and curr["pnl_count"] > 0:
            clusters.append(curr)

        # 转换为标准化 trade dict
        reconstructed = []
        for idx, c in enumerate(clusters):
            sym = c["symbol"]
            closed_ts = c["end_t"]
            realized_pnl = round(c["realized_pnl"], 4)
            fee = round(c["fee"], 4)
            net_pnl = round(realized_pnl - fee, 4)
            closed_at = get_beijing_time_str(closed_ts / 1000.0)

            # 尝试在 local_history 中寻找最匹配的记录以继承趋势线和开平仓语义
            matched_local = None
            for loc in local_history:
                if clean_symbol(loc.get("symbol", "")) == sym:
                    loc_closed_at = loc.get("closed_at") or loc.get("exit_time")
                    if loc_closed_at:
                        loc_ts = parse_to_timestamp(loc_closed_at)
                        if abs(loc_ts - (closed_ts / 1000.0)) <= 300:  # 5分钟内
                            matched_local = loc
                            break

            if matched_local:
                enriched = self.enrich_trade_record(matched_local, idx)
                # 校准真实盈亏为币安清算值
                enriched["realized_pnl"] = realized_pnl
                enriched["fee"] = fee
                enriched["net_pnl"] = net_pnl
                enriched["source"] = "BINANCE_REALTIME_VERIFIED"
                reconstructed.append(enriched)
            else:
                outcome = "WIN" if realized_pnl > 1e-4 else ("LOSS" if realized_pnl < -1e-4 else "BREAKEVEN")
                reconstructed.append({
                    "trade_id": f"TRD-{idx+1:03d}-{sym}-{int(closed_ts)}",
                    "symbol": sym,
                    "side": "LONG",
                    "entry_price": 0.0,
                    "exit_price": 0.0,
                    "amount": 0.0,
                    "entry_notional": 0.0,
                    "exit_notional": 0.0,
                    "realized_pnl": realized_pnl,
                    "fee": fee,
                    "net_pnl": net_pnl,
                    "pnl_pct": None,
                    "roe_pct": None,
                    "leverage": self.default_leverage,
                    "entry_time": "--",
                    "closed_at": closed_at,
                    "entry_ts": int(closed_ts),
                    "closed_ts": int(closed_ts),
                    "duration_seconds": 0,
                    "duration_str": "--",
                    "outcome": outcome,
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
                    "notes": "Binance Live Contract Trade"
                })

        return reconstructed

    def calculate_quant_risk_metrics(
        self,
        trades: List[dict],
        active_positions: List[dict],
        account_info: Optional[dict],
        net_transfers: Optional[float] = None
    ) -> Tuple[dict, List[dict], List[dict]]:
        """
        严谨量化评价指标计算引擎:
        - 资产总额与保证金 (Total Equity, Available Margin, Position Margin)
        - 累计收益与胜率 (Realized PnL, Win Rate %, Profit Factor, Payoff Ratio)
        - 最大回撤时间序列 (Max Drawdown $ / %, Underwater Curve)
        - 年化夏普比率 (Sharpe Ratio) 与索提诺比率 (Sortino Ratio)
        - 连胜连亏与持仓时长分布
        返回: (summary_dict, equity_curve_timeseries, daily_pnl_timeseries)
        """
        # 1. 基础盈亏聚合
        total_closed_trades = len(trades)
        winning_trades = [t for t in trades if t["outcome"] == "WIN"]
        losing_trades = [t for t in trades if t["outcome"] == "LOSS"]
        breakeven_trades = [t for t in trades if t["outcome"] == "BREAKEVEN"]

        cumulative_realized_pnl = round(sum(t["realized_pnl"] for t in trades), 4)
        total_fee = round(sum(t["fee"] for t in trades), 4)
        net_realized_pnl = round(cumulative_realized_pnl - total_fee, 4)

        total_unrealized_pnl = round(sum(p["unrealized_pnl"] for p in active_positions), 4)
        total_position_margin = round(sum(p["initial_margin"] for p in active_positions), 4)

        # 2. 资金账户推导 (实盘与离线回退自适应)
        if account_info and isinstance(account_info, dict):
            wallet_balance = float(account_info.get("totalWalletBalance") or 0.0)
            total_equity = float(account_info.get("totalMarginBalance") or (wallet_balance + total_unrealized_pnl))
            available_margin = float(account_info.get("availableBalance") or max(0.0, total_equity - total_position_margin))
            
            if self.custom_initial_capital is not None:
                initial_capital = self.custom_initial_capital
            elif net_transfers is not None and net_transfers > 0:
                initial_capital = round(net_transfers, 2)
            else:
                initial_capital = self.initial_capital
        else:
            initial_capital = self.initial_capital
            wallet_balance = round(initial_capital + net_realized_pnl, 4)
            total_equity = round(wallet_balance + total_unrealized_pnl, 4)
            available_margin = round(max(0.0, total_equity - total_position_margin), 4)

        total_return_pct = round(((total_equity - initial_capital) / initial_capital * 100.0), 2) if initial_capital > 0 else 0.0

        # 3. 胜率与获利因子
        win_rate_pct = round((len(winning_trades) / total_closed_trades * 100.0), 2) if total_closed_trades > 0 else 0.0
        decisive_trades_count = len(winning_trades) + len(losing_trades)
        win_rate_ex_breakeven = round((len(winning_trades) / decisive_trades_count * 100.0), 2) if decisive_trades_count > 0 else 0.0

        gross_profit = sum(t["realized_pnl"] for t in winning_trades)
        gross_loss = abs(sum(t["realized_pnl"] for t in losing_trades))

        if gross_loss > 1e-4:
            profit_factor = round(gross_profit / gross_loss, 2)
        elif gross_profit > 1e-4:
            profit_factor = 999.0  # 无亏损交易，以 999.0 代表无限大
        else:
            profit_factor = 0.0

        avg_trade_pnl = round((cumulative_realized_pnl / total_closed_trades), 2) if total_closed_trades > 0 else 0.0
        trade_returns = [t["pnl_pct"] for t in trades if t["pnl_pct"] is not None]
        avg_trade_pnl_pct = round(sum(trade_returns) / len(trade_returns), 2) if trade_returns else None

        avg_winning_trade = round((gross_profit / len(winning_trades)), 2) if winning_trades else 0.0
        avg_losing_trade = round((gross_loss / len(losing_trades)), 2) if losing_trades else 0.0
        payoff_ratio = round(avg_winning_trade / avg_losing_trade, 2) if avg_losing_trade > 1e-4 else (999.0 if avg_winning_trade > 0 else 0.0)

        largest_win_usd = max([t["realized_pnl"] for t in trades], default=0.0)
        largest_loss_usd = min([t["realized_pnl"] for t in trades], default=0.0)

        # 4. 连胜与连亏统计
        sorted_trades = sorted(trades, key=lambda x: x.get("closed_ts", 0))
        max_consecutive_wins = 0
        max_consecutive_losses = 0
        curr_wins = 0
        curr_losses = 0

        for t in sorted_trades:
            pnl = t.get("realized_pnl", 0.0)
            if pnl > 1e-4:
                curr_wins += 1
                curr_losses = 0
                max_consecutive_wins = max(max_consecutive_wins, curr_wins)
            elif pnl < -1e-4:
                curr_losses += 1
                curr_wins = 0
                max_consecutive_losses = max(max_consecutive_losses, curr_losses)
            else:
                curr_wins = 0
                curr_losses = 0

        # 5. 持仓周期统计
        durations = [t.get("duration_seconds", 0) for t in trades]
        avg_hold_duration_seconds = int(sum(durations) / len(durations)) if durations else 0
        avg_hold_duration_str = format_duration(avg_hold_duration_seconds)
        max_hold_duration_seconds = max(durations) if durations else 0
        min_hold_duration_seconds = min(durations) if durations else 0

        # 6. 生成资金净值走势曲线 (Equity Curve timeseries) & 最大回撤
        equity_curve: List[dict] = []
        running_cash = initial_capital
        peak_equity = initial_capital
        max_drawdown_usd = 0.0
        max_drawdown_pct = 0.0

        # 基准起始点 (首笔交易前或当前时间前)
        first_ts = (
            sorted_trades[0].get("entry_ts")
            or sorted_trades[0].get("closed_ts")
            or int(time.time() * 1000)
        ) if sorted_trades else int(time.time() * 1000)
        base_ts = first_ts - 3600 * 1000  # 起始点提前 1 小时
        equity_curve.append({
            "timestamp": base_ts,
            "datetime": get_beijing_time_str(base_ts / 1000.0),
            "equity": round(initial_capital, 2),
            "cash": round(initial_capital, 2),
            "cumulative_pnl": 0.0,
            "drawdown_usd": 0.0,
            "drawdown_pct": 0.0,
            "event": "初始基准资金 (Initial Capital)"
        })

        for t in sorted_trades:
            net_pnl = t.get("net_pnl", t.get("realized_pnl", 0.0))
            running_cash += net_pnl
            cur_eq = running_cash
            peak_equity = max(peak_equity, cur_eq)
            dd_usd = round(max(0.0, peak_equity - cur_eq), 2)
            dd_pct = round((dd_usd / peak_equity * 100.0), 2) if peak_equity > 0 else 0.0

            max_drawdown_usd = max(max_drawdown_usd, dd_usd)
            max_drawdown_pct = max(max_drawdown_pct, dd_pct)

            sign = "+" if net_pnl >= 0 else ""
            c_ts = t.get("closed_ts") or int(time.time() * 1000)
            c_at = t.get("closed_at") or get_beijing_time_str(c_ts / 1000.0)
            sym = t.get("symbol", "UNKNOWN")
            reason = t.get("close_reason", "平仓")

            equity_curve.append({
                "timestamp": c_ts,
                "datetime": c_at,
                "equity": round(cur_eq, 2),
                "cash": round(running_cash, 2),
                "cumulative_pnl": round(running_cash - initial_capital, 2),
                "drawdown_usd": dd_usd,
                "drawdown_pct": dd_pct,
                "event": f"平仓 {sym} ({sign}{net_pnl:.2f} USDT, {reason})"
            })

        # 加入当前实时净值点 (若有未实现浮盈或更新当前时间)
        now_ts_ms = int(time.time() * 1000)
        current_eq = running_cash + total_unrealized_pnl
        peak_equity = max(peak_equity, current_eq)
        cur_dd_usd = round(max(0.0, peak_equity - current_eq), 2)
        cur_dd_pct = round((cur_dd_usd / peak_equity * 100.0), 2) if peak_equity > 0 else 0.0
        max_drawdown_usd = max(max_drawdown_usd, cur_dd_usd)
        max_drawdown_pct = max(max_drawdown_pct, cur_dd_pct)

        equity_curve.append({
            "timestamp": now_ts_ms,
            "datetime": get_beijing_time_str(now_ts_ms / 1000.0),
            "equity": round(current_eq, 2),
            "cash": round(running_cash, 2),
            "cumulative_pnl": round(current_eq - initial_capital, 2),
            "drawdown_usd": cur_dd_usd,
            "drawdown_pct": cur_dd_pct,
            "event": "当前实时净值 (Current Real-Time Equity)"
        })

        # 7. 每日盈亏统计 (Daily PnL)
        daily_groups: Dict[str, List[dict]] = {}
        for t in sorted_trades:
            c_at = t.get("closed_at") or get_beijing_time_str()
            day_str = c_at[:10]  # YYYY-MM-DD
            if day_str not in daily_groups:
                daily_groups[day_str] = []
            daily_groups[day_str].append(t)

        daily_pnl: List[dict] = []
        day_equity = initial_capital
        daily_returns: List[float] = []

        for day_str in sorted(daily_groups.keys()):
            d_trades = daily_groups[day_str]
            d_realized = round(sum(x["realized_pnl"] for x in d_trades), 2)
            d_fee = round(sum(x["fee"] for x in d_trades), 2)
            d_net = round(d_realized - d_fee, 2)
            d_wins = len([x for x in d_trades if x["outcome"] == "WIN"])
            d_losses = len([x for x in d_trades if x["outcome"] == "LOSS"])
            d_be = len([x for x in d_trades if x["outcome"] == "BREAKEVEN"])

            d_ret_pct = round((d_net / day_equity * 100.0), 2) if day_equity > 0 else 0.0
            daily_returns.append(d_net / day_equity if day_equity > 0 else 0.0)

            daily_pnl.append({
                "date": day_str,
                "realized_pnl": d_realized,
                "fee": d_fee,
                "net_pnl": d_net,
                "return_pct": d_ret_pct,
                "trades_count": len(d_trades),
                "wins": d_wins,
                "losses": d_losses,
                "breakeven": d_be
            })
            day_equity += d_net

        # 8. 夏普比率 (Sharpe Ratio) 与索提诺比率 (Sortino Ratio) 计算
        # 若有 2 天及以上按日度收益率年化 (sqrt(365))；若只有 1 天按单笔交易收益率年化
        if len(daily_returns) >= 2:
            mean_r = sum(daily_returns) / len(daily_returns)
            var_r = sum((r - mean_r) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
            std_r = math.sqrt(var_r) if var_r > 0 else 0.0
            sharpe_ratio = round((mean_r / std_r * math.sqrt(365)), 2) if std_r > 1e-6 else 0.0

            downside_r = [r ** 2 for r in daily_returns if r < 0]
            if downside_r:
                downside_std = math.sqrt(sum(downside_r) / len(downside_r))
                sortino_ratio = round((mean_r / downside_std * math.sqrt(365)), 2) if downside_std > 1e-6 else 0.0
            else:
                sortino_ratio = 999.0 if mean_r > 0 else 0.0
            sharpe_method = "DAILY_RETURNS"
        elif sorted_trades:
            # 单笔交易收益率年化 (假定年交易频率 365 笔)
            trade_returns = [t["net_pnl"] / initial_capital for t in sorted_trades]
            mean_tr = sum(trade_returns) / len(trade_returns)
            if len(trade_returns) > 1:
                var_tr = sum((r - mean_tr) ** 2 for r in trade_returns) / (len(trade_returns) - 1)
                std_tr = math.sqrt(var_tr) if var_tr > 0 else 0.0
            else:
                std_tr = 0.0

            sharpe_ratio = round((mean_tr / std_tr * math.sqrt(365)), 2) if std_tr > 1e-6 else (2.5 if mean_tr > 0 else 0.0)

            downside_tr = [r ** 2 for r in trade_returns if r < 0]
            if downside_tr:
                downside_std = math.sqrt(sum(downside_tr) / len(downside_tr))
                sortino_ratio = round((mean_tr / downside_std * math.sqrt(365)), 2) if downside_std > 1e-6 else 0.0
            else:
                sortino_ratio = 999.0 if mean_tr > 0 else 0.0
            sharpe_method = "TRADE_RETURNS_ANNUALIZED"
        else:
            sharpe_ratio = 0.0
            sortino_ratio = 0.0
            sharpe_method = "NONE"

        summary = {
            "initial_capital": round(initial_capital, 2),
            "total_equity": round(total_equity, 2),
            "wallet_balance": round(wallet_balance, 2),
            "available_margin": round(available_margin, 2),
            "position_margin": round(total_position_margin, 2),
            "total_unrealized_pnl": round(total_unrealized_pnl, 2),
            "cumulative_realized_pnl": round(cumulative_realized_pnl, 2),
            "total_fee": round(total_fee, 2),
            "net_realized_pnl": round(net_realized_pnl, 2),
            "total_return_pct": total_return_pct,
            "win_rate_pct": win_rate_pct,
            "win_rate_ex_breakeven_pct": win_rate_ex_breakeven,
            "profit_factor": profit_factor,
            "payoff_ratio": payoff_ratio,
            "max_drawdown_usd": round(max_drawdown_usd, 2),
            "max_drawdown_pct": round(max_drawdown_pct, 2),
            "sharpe_ratio": sharpe_ratio,
            "sortino_ratio": sortino_ratio,
            "sharpe_method": sharpe_method,
            "total_trades": total_closed_trades,
            "closed_trades": total_closed_trades,
            "open_trades": len(active_positions),
            "winning_trades": len(winning_trades),
            "losing_trades": len(losing_trades),
            "breakeven_trades": len(breakeven_trades),
            "avg_trade_pnl": avg_trade_pnl,
            "avg_trade_pnl_pct": avg_trade_pnl_pct,
            "avg_winning_trade": avg_winning_trade,
            "avg_losing_trade": avg_losing_trade,
            "avg_hold_duration_seconds": avg_hold_duration_seconds,
            "avg_hold_duration_str": avg_hold_duration_str,
            "max_hold_duration_seconds": max_hold_duration_seconds,
            "min_hold_duration_seconds": min_hold_duration_seconds,
            "max_consecutive_wins": max_consecutive_wins,
            "max_consecutive_losses": max_consecutive_losses,
            "largest_win_usd": round(largest_win_usd, 2),
            "largest_loss_usd": round(largest_loss_usd, 2)
        }

        return summary, equity_curve, daily_pnl

    def run(
        self, output_path: Optional[Path] = None, *, database: Optional[Path] = None,
        recording_start_ms: Optional[int] = None,
    ) -> dict:
        """执行全套账户数据清洗、接口拉取、风控指标核算与报表持久化"""
        out_file = Path(output_path or DEFAULT_OUTPUT_PATH)

        # 1. 获取币安实盘合约数据 (含优雅降级)
        account_info, income_list, active_positions_binance, api_status = self.fetch_binance_data()

        previous_report = None
        recording = None
        if database is not None:
            from account_dashboard_api import load_report, publish_report

            if api_status["mode"] != "BINANCE_REALTIME":
                raise RuntimeError("币安账户快照不完整，保留上一次完整实盘快照")
            previous_report = load_report(database)
            recording = previous_report.get("recording") if previous_report is not None else None
            if previous_report is None and recording_start_ms is None:
                recording_start_ms = int(time.time() * 1000)
            if recording_start_ms is not None:
                if recording is not None:
                    raise ValueError("账户记录起点已设置")
                recording = {
                    "start_ms": recording_start_ms,
                    "started_at": get_beijing_time_str(recording_start_ms / 1000),
                    "initial_equity": float(account_info["totalMarginBalance"]),
                }

        # 从记录起点分段读取资金流水，满页时继续缩小区间，避免漏掉同一批流水。
        new_income = []
        new_fills = []
        income_page_full = len(income_list) >= INCOME_PAGE_LIMIT
        if database is not None and recording is not None:
            import sqlite3
            from contextlib import closing

            start_ms = recording["start_ms"]
            end_ms = int(time.time() * 1000)
            last_sync_ms = recording.get("ledger_synced_until_ms", start_ms)
            query_start = max(start_ms, last_sync_ms - 86400 * 1000)
            if end_ms - query_start > 89 * 86400 * 1000:
                raise RuntimeError("账户流水同步间断超过币安接口可查询范围，请核对历史数据")
            ranges = [(query_start, end_ms)]
            while ranges:
                start, end = ranges.pop()
                page, error = self.client._send_signed_request(
                    "/fapi/v1/income",
                    {"startTime": start, "endTime": end, "limit": INCOME_PAGE_LIMIT},
                )
                if error is not None or not isinstance(page, list):
                    raise RuntimeError(f"币安资金流水读取失败: {error}")
                if len(page) >= INCOME_PAGE_LIMIT:
                    if start == end:
                        raise RuntimeError("同一毫秒资金流水超过接口单页上限")
                    middle = (start + end) // 2
                    ranges.extend([(start, middle), (middle + 1, end)])
                else:
                    new_income.extend(page)

            existing_income = []
            existing_fills = []
            if database.exists():
                with closing(sqlite3.connect(database)) as connection:
                    tables = {row[0] for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )}
                    if "income_ledger" in tables:
                        existing_income = [json.loads(row[0]) for row in connection.execute(
                            "SELECT payload FROM income_ledger WHERE timestamp >= ?", (start_ms,)
                        )]
                    if "trade_fills" in tables:
                        existing_fills = [json.loads(row[0]) for row in connection.execute(
                            "SELECT payload FROM trade_fills WHERE timestamp >= ?", (start_ms,)
                        )]
                    if "equity_observations" in tables:
                        previous_report["equity_curve"] = [
                            json.loads(row[0]) for row in connection.execute(
                                "SELECT payload FROM equity_observations WHERE timestamp >= ? ORDER BY timestamp",
                                (start_ms,),
                            )
                        ]
            income_by_id = {
                (item["incomeType"], str(item["tranId"])): item
                for item in existing_income + new_income
            }
            income_list = list(income_by_id.values())
            symbols = {
                item["symbol"] for item in income_list
                if item.get("symbol") and item["incomeType"] in ("REALIZED_PNL", "COMMISSION")
            }
            symbols.update(item["symbol"] for item in existing_fills)
            for symbol in sorted(symbols):
                window_start = query_start
                while window_start <= end_ms:
                    window_end = min(window_start + 7 * 86400 * 1000 - 1, end_ms)
                    trade_ranges = [(window_start, window_end)]
                    while trade_ranges:
                        start, end = trade_ranges.pop()
                        page, error = self.client._send_signed_request(
                            "/fapi/v1/userTrades",
                            {"symbol": symbol, "startTime": start, "endTime": end, "limit": 1000},
                        )
                        if error is not None or not isinstance(page, list):
                            raise RuntimeError(f"币安成交读取失败 ({symbol}): {error}")
                        if len(page) >= 1000:
                            if start == end:
                                raise RuntimeError(f"{symbol} 同一毫秒成交超过接口单页上限")
                            middle = (start + end) // 2
                            trade_ranges.extend([(start, middle), (middle + 1, end)])
                        else:
                            new_fills.extend(page)
                    window_start = window_end + 1
            fills_by_id = {
                (item["symbol"], str(item["id"])): item
                for item in existing_fills + new_fills
            }
            recording["ledger_synced_until_ms"] = end_ms
            income_page_full = False

        # 2. 获取本地 trade_positions.json 持仓
        local_positions, local_history = self.load_local_positions_store()
        if recording is not None:
            cutoff = recording["start_ms"]
            income_list = [item for item in income_list if int(item["time"]) >= cutoff]
            local_history = [
                trade for trade in local_history
                if parse_to_timestamp(trade.get("closed_at") or trade.get("exit_time")) * 1000 >= cutoff
            ]

        # 3. 持仓与流水合并:
        raw_active_positions = active_positions_binance if api_status["positions_available"] else local_positions

        # 优先从币安真实流水重构实盘历史交易
        if database is not None and recording is not None:
            commissions = {}
            for item in income_list:
                if item["incomeType"] == "COMMISSION" and item.get("tradeId"):
                    key = (item["symbol"], str(item["tradeId"]))
                    if item["asset"] == "USDT":
                        commissions[key] = commissions.get(key, 0.0) - float(item["income"])
            trade_history = []
            for item in sorted(income_list, key=lambda value: value["time"]):
                if item["incomeType"] != "REALIZED_PNL" or item["asset"] != "USDT":
                    continue
                key = (item["symbol"], str(item.get("tradeId", "")))
                fill = fills_by_id.get(key)
                realized = float(item["income"])
                fee = commissions.get(key, 0.0)
                closed_ts = int(item["time"])
                trade_history.append({
                    "trade_id": f"{item['incomeType']}-{item['tranId']}",
                    "symbol": item["symbol"],
                    "side": fill["positionSide"] if fill else "UNKNOWN",
                    "entry_price": 0.0,
                    "exit_price": float(fill["price"]) if fill else 0.0,
                    "amount": float(fill["qty"]) if fill else 0.0,
                    "entry_notional": 0.0,
                    "exit_notional": float(fill["quoteQty"]) if fill else 0.0,
                    "realized_pnl": realized,
                    "fee": fee,
                    "net_pnl": round(realized - fee, 4),
                    "pnl_pct": None,
                    "roe_pct": None,
                    "leverage": self.default_leverage,
                    "entry_time": "--",
                    "closed_at": get_beijing_time_str(closed_ts / 1000),
                    "entry_ts": closed_ts,
                    "closed_ts": closed_ts,
                    "duration_seconds": 0,
                    "duration_str": "--",
                    "outcome": "WIN" if realized - fee > 0 else "LOSS" if realized - fee < 0 else "BREAKEVEN",
                    "status": "BINANCE_REALTIME_TRADE",
                    "close_reason": "币安实盘成交",
                    "close_reason_code": "BINANCE_REALTIME",
                    "highest_price": 0.0,
                    "trailing_stop_triggered": False,
                    "trailing_stop_price": None,
                    "trendline_id": None,
                    "trendline_label": "--",
                    "trendline_info": None,
                    "source": "BINANCE_REALTIME",
                    "notes": "币安成交" if fill else "币安已实现盈亏流水",
                })
        elif api_status["income_available"]:
            trade_history = self.reconstruct_binance_trades(income_list, local_history)
        else:
            trade_history = [self.enrich_trade_record(t, idx) for idx, t in enumerate(local_history)]

        # 4. 活跃持仓清洗
        active_positions = [self.enrich_active_position(p, idx) for idx, p in enumerate(raw_active_positions)]

        # 提取币安历史转账净充值作为默认本金基准
        income_times = [int(item["time"]) for item in income_list]
        net_transfers = None
        if income_list and not income_page_full:
            net_transfers = sum(float(x["income"]) for x in income_list if x["incomeType"] == "TRANSFER")

        # 5. 计算全套量化风控指标、资金走势与每日统计
        summary, equity_curve, daily_pnl = self.calculate_quant_risk_metrics(
            trades=trade_history,
            active_positions=active_positions,
            account_info=account_info,
            net_transfers=net_transfers
        )

        if recording is not None:
            # 汇总全部 USDT 手续费，包括尚未平仓的开仓成交。
            ledger_fee = -sum(
                float(item["income"]) for item in income_list
                if item["incomeType"] == "COMMISSION" and item["asset"] == "USDT"
            )
            summary["total_fee"] = round(ledger_fee, 2)
            summary["net_realized_pnl"] = round(
                summary["cumulative_realized_pnl"] - ledger_fee, 2
            )
            initial_equity = recording["initial_equity"]
            equity = float(account_info["totalMarginBalance"])
            now_ms = int(time.time() * 1000)
            if recording_start_ms is not None:
                equity_curve = [{
                    "timestamp": recording["start_ms"],
                    "datetime": recording["started_at"],
                    "equity": initial_equity,
                    "cash": summary["wallet_balance"],
                    "cumulative_pnl": 0.0,
                    "drawdown_usd": 0.0,
                    "drawdown_pct": 0.0,
                    "event": "开始记录",
                }]
            else:
                equity_curve = previous_report["equity_curve"]

            equity_curve.append({
                "timestamp": now_ms,
                "datetime": get_beijing_time_str(now_ms / 1000),
                "equity": equity,
                "cash": summary["wallet_balance"],
                "event": "币安账户快照",
            })
            transfers = sorted(
                (int(item["time"]), float(item["income"]))
                for item in income_list
                if item["incomeType"] == "TRANSFER" and item["asset"] == "USDT"
            )
            net_flow = 0.0
            transfer_index = 0
            peak_equity = initial_equity
            for point in equity_curve:
                while transfer_index < len(transfers) and transfers[transfer_index][0] <= point["timestamp"]:
                    net_flow += transfers[transfer_index][1]
                    transfer_index += 1
                adjusted_equity = point["equity"] - net_flow
                peak_equity = max(peak_equity, adjusted_equity)
                drawdown = peak_equity - adjusted_equity
                point["cumulative_pnl"] = round(adjusted_equity - initial_equity, 2)
                point["drawdown_usd"] = round(drawdown, 2)
                point["drawdown_pct"] = round(drawdown / peak_equity * 100, 2) if peak_equity > 0 else 0.0
            net_flow = sum(amount for _, amount in transfers)
            summary["initial_capital"] = initial_equity
            summary["total_return_pct"] = round(
                (equity - initial_equity - net_flow) / initial_equity * 100, 2
            ) if initial_equity > 0 else 0.0
            summary["net_external_transfers"] = round(net_flow, 4)
            summary["max_drawdown_usd"] = max(point["drawdown_usd"] for point in equity_curve)
            summary["max_drawdown_pct"] = max(point["drawdown_pct"] for point in equity_curve)

            # 每日汇总以资金流水为准，计入开仓手续费和资金费。
            daily_by_date = {entry["date"]: entry for entry in daily_pnl}
            for entry in daily_by_date.values():
                entry["realized_pnl"] = 0.0
                entry["fee"] = 0.0
                entry["funding_fee"] = 0.0
            for item in income_list:
                if item["asset"] != "USDT" or item["incomeType"] not in (
                    "REALIZED_PNL", "COMMISSION", "FUNDING_FEE"
                ):
                    continue
                date = get_beijing_time_str(int(item["time"]) / 1000)[:10]
                entry = daily_by_date.setdefault(date, {
                    "date": date, "realized_pnl": 0.0, "fee": 0.0,
                    "funding_fee": 0.0, "trades_count": 0, "wins": 0,
                    "losses": 0, "breakeven": 0,
                })
                amount = float(item["income"])
                if item["incomeType"] == "REALIZED_PNL":
                    entry["realized_pnl"] += amount
                elif item["incomeType"] == "COMMISSION":
                    entry["fee"] -= amount
                else:
                    entry["funding_fee"] += amount
            daily_pnl = []
            day_equity = initial_equity
            for date in sorted(daily_by_date):
                entry = daily_by_date[date]
                entry["realized_pnl"] = round(entry["realized_pnl"], 2)
                entry["fee"] = round(entry["fee"], 2)
                entry["funding_fee"] = round(entry["funding_fee"], 2)
                entry["net_pnl"] = round(
                    entry["realized_pnl"] - entry["fee"] + entry["funding_fee"], 2
                )
                entry["return_pct"] = round(
                    entry["net_pnl"] / day_equity * 100, 2
                ) if day_equity > 0 else 0.0
                day_equity += entry["net_pnl"]
                daily_pnl.append(entry)

        # 6. 流水明细分类聚合 (Income Breakdown)
        income_breakdown = {
            "realized_pnl": summary["cumulative_realized_pnl"],
            "fee": summary["total_fee"],
            "funding_fee": 0.0,
            "transfer": 0.0,
            "raw_count": len(income_list),
            "coverage_start": get_beijing_time_str(min(income_times) / 1000) if income_times else None,
            "coverage_end": get_beijing_time_str(max(income_times) / 1000) if income_times else None,
            "page_full": income_page_full,
            "available": api_status["income_available"],
            "principal_source": (
                "recording_start" if recording is not None
                else "configured" if self.custom_initial_capital is not None
                else "income_window" if net_transfers is not None and net_transfers > 0
                else "default"
            ),
        }
        if income_list:
            for item in income_list:
                itype = item.get("incomeType")
                amt = float(item.get("income", 0.0))
                if itype == "FUNDING_FEE":
                    income_breakdown["funding_fee"] += amt
                elif itype == "TRANSFER":
                    income_breakdown["transfer"] += amt
            income_breakdown["funding_fee"] = round(income_breakdown["funding_fee"], 4)
            income_breakdown["transfer"] = round(income_breakdown["transfer"], 4)

        # 7. 收益率分布统计 (PnL Distribution for Histogram & Scatter)
        bins_def = [
            {"label": "<-5%", "min": -999999.0, "max": -5.0, "count": 0, "trades": []},
            {"label": "-5% ~ -2%", "min": -5.0, "max": -2.0, "count": 0, "trades": []},
            {"label": "-2% ~ 0%", "min": -2.0, "max": -0.0001, "count": 0, "trades": []},
            {"label": "保本 (0%)", "min": -0.0001, "max": 0.0001, "count": 0, "trades": []},
            {"label": "0% ~ +2%", "min": 0.0001, "max": 2.0, "count": 0, "trades": []},
            {"label": "+2% ~ +5%", "min": 2.0, "max": 5.0, "count": 0, "trades": []},
            {"label": "+5% ~ +10%", "min": 5.0, "max": 10.0, "count": 0, "trades": []},
            {"label": ">+10%", "min": 10.0, "max": 999999.0, "count": 0, "trades": []}
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
                bins_def[-1]["count"] += 1

            scatter_points.append({
                "trade_id": t["trade_id"],
                "symbol": t["symbol"],
                "pnl_pct": t["pnl_pct"],
                "roe_pct": t["roe_pct"],
                "net_pnl": t["net_pnl"],
                "duration_minutes": round(t["duration_seconds"] / 60.0, 1),
                "closed_at": t["closed_at"],
                "outcome": t["outcome"]
            })

        pnl_distribution = {
            "bins": bins_def,
            "scatter": scatter_points
        }

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
            "pnl_distribution": pnl_distribution,
            "income_breakdown": income_breakdown
        }

        if recording is not None:
            report["recording"] = recording

        # 7. 原子化写入结果文件
        if database is None:
            atomic_write_json(out_file, report)
            logger.info(f"✅ 账户量化分析报表已成功生成并写入: {out_file}")
        else:
            publish_report(database, report, new_income, new_fills)
            logger.info("✅ 账户实盘快照已写入 SQLite: %s", database)

        return report


def print_cli_summary(report: dict):
    """格式化打印终端量化风控仪表盘"""
    summary = report.get("summary", {})
    api_status = report.get("api_status", {})
    active_positions = report.get("active_positions", [])
    trade_history = report.get("trade_history", [])

    print("\n" + "=" * 78)
    print(" 🚀 币安合约账户量化风控指标与交易评测仪表盘 (HUD)")
    print("=" * 78)
    print(f" 状态模式: {'🟢 币安实盘在线' if api_status.get('connected') else '🟡 本地离线容灾回退'} | 更新时间: {report.get('generated_at')}")
    print("-" * 78)

    # 第一行: 资产与资金状况
    eq = summary.get("total_equity", 0.0)
    init_cap = summary.get("initial_capital", 0.0)
    ret_pct = summary.get("total_return_pct", 0.0)
    avail = summary.get("available_margin", 0.0)
    pnl = summary.get("cumulative_realized_pnl", 0.0)
    net_pnl = summary.get("net_realized_pnl", 0.0)
    fee = summary.get("total_fee", 0.0)

    ret_color = "\033[32m" if ret_pct >= 0 else "\033[31m"
    reset = "\033[0m"

    print(f" 💰 总资金净值 (Equity):     ${eq:,.2f} USDT  (初始本金: ${init_cap:,.2f})")
    print(f" 📈 账户总收益率 (Return):    {ret_color}{ret_pct:+.2f}%{reset}  (累计实现盈亏: ${pnl:+,.2f}, 净利润: ${net_pnl:+,.2f})")
    print(f" 🛡️  可用保证金 (Available):  ${avail:,.2f} USDT  (手续费支出: ${fee:,.2f})")
    print("-" * 78)

    # 第二行: 核心量化胜率与风控指标
    wr = summary.get("win_rate_pct", 0.0)
    pf = summary.get("profit_factor", 0.0)
    pf_str = "∞ (全胜)" if pf >= 999.0 else f"{pf:.2f}"
    mdd_pct = summary.get("max_drawdown_pct", 0.0)
    mdd_usd = summary.get("max_drawdown_usd", 0.0)
    sharpe = summary.get("sharpe_ratio", 0.0)
    sortino = summary.get("sortino_ratio", 0.0)
    sortino_str = "∞" if sortino >= 999.0 else f"{sortino:.2f}"

    print(f" 🎯 交易胜率 (Win Rate):     {wr:.1f}%  (盈亏笔数: {summary.get('winning_trades')}胜 / {summary.get('losing_trades')}负 / {summary.get('breakeven_trades')}平)")
    print(f" ⚖️  盈亏比 (Profit Factor):  {pf_str}  (盈亏比率 Payoff: {summary.get('payoff_ratio', 0.0)})")
    print(f" ⚠️  最大回撤 (Max Drawdown): {mdd_pct:.2f}% (${mdd_usd:,.2f} USDT)")
    print(f" 📊 夏普比率 (Sharpe Ratio): {sharpe:.2f}  |  索提诺比率 (Sortino): {sortino_str}")
    print(f" ⏳ 平均持仓周期:             {summary.get('avg_hold_duration_str')}  | 最大连胜: {summary.get('max_consecutive_wins')} 笔")
    print("-" * 78)

    # 第三行: 活跃持仓面板
    print(f" ⚡ 活跃持仓监控 (Active Positions: {len(active_positions)}):")
    if active_positions:
        for p in active_positions:
            pnl_c = "\033[32m" if p['unrealized_pnl'] >= 0 else "\033[31m"
            print(f"   • {p['symbol']} ({p['leverage']}x): {p['amount']} @ ${p['entry_price']:.4f} -> 现价 ${p['mark_price']:.4f} | "
                  f"浮盈: {pnl_c}{p['unrealized_pnl']:+.2f} USDT ({p['roe_pct']:+.2f}% ROE){reset} | 周期: {p['holding_str']}")
    else:
        print("   (当前无活跃多头持仓)")
    print("-" * 78)

    # 第四行: 最近平仓流水
    print(f" 📜 历史平仓明细 (Recent Closed Trades: {len(trade_history)}):")
    for t in trade_history[-5:]:
        pnl_c = "\033[32m" if t['net_pnl'] >= 0 else "\033[31m"
        tl = f"[{t['trendline_label']}] " if t.get('trendline_id') else ""
        roe = f"{t['roe_pct']:+.2f}% ROE" if t["roe_pct"] is not None else "ROE 未知"
        print(f"   [{t['closed_at']}] {t['symbol']} | 开: ${t['entry_price']:.4f} -> 平: ${t['exit_price']:.4f} | "
              f"净盈: {pnl_c}{t['net_pnl']:+.2f} USDT ({roe}){reset} | 原因: {t['close_reason']} {tl}")
    print("=" * 78 + "\n")


def main():
    parser = argparse.ArgumentParser(description="币安合约账户量化风控数据分析引擎")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--output", "-o", type=Path, help="输出 JSON 文件路径")
    output.add_argument("--database", type=Path, help="将完整实盘快照保存到 SQLite")
    parser.add_argument("--initial-capital", type=float, default=None, help="初始基准资金 (USDT)，缺省时自动根据币安历史转账充值净额推导")
    parser.add_argument("--offline", action="store_true", help="强制启用离线回退模式 (不请求网络)")
    parser.add_argument("--sync-binance", action="store_true", help="显式触发币安实盘合约 API 同步")
    parser.add_argument("--json", action="store_true", help="仅输出 JSON 内容到终端")
    parser.add_argument("--recording-start-ms", type=int, help="开始记录的 Unix 时间戳（毫秒）")
    args = parser.parse_args()
    if args.recording_start_ms is not None and args.database is None:
        parser.error("--recording-start-ms requires --database")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    engine = AccountAnalyticsEngine(
        initial_capital=args.initial_capital,
        offline_mode=args.offline
    )

    report = engine.run(output_path=args.output, database=args.database, recording_start_ms=args.recording_start_ms)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.database is None:
        print_cli_summary(report)


if __name__ == "__main__":
    main()
