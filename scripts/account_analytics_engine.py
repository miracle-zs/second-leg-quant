#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
账户量化风控数据分析引擎 (Account Quant Analytics Engine)
==============================================================================
Orchestrator & CLI tying together:
1. `ledger_sync`: Binance `/fapi/v1/income` & `/fapi/v1/userTrades` incremental SQLite sync.
2. `trade_reconstructor` & `trade_enrichment`: FIFO trade reconstruction & metadata enrichment.
3. `quant_metrics`: Single-pass income aggregation, Sharpe/Sortino, equity curve & daily PnL.
4. `db_store`: Unified SQLite persistence (positions, history, ledger, snapshots).
"""

from __future__ import annotations

import argparse
import json
import logging
import math
from pathlib import Path
import sys
from typing import Any, Dict, List, Mapping, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from binance_client import BinanceAPIError, BinanceFuturesClient  # noqa: E402
from config import (  # noqa: E402
    DEFAULT_DB_PATH,
    get_beijing_time_str,
    resolve_database_path,
)
import db_store  # noqa: E402
from ledger_sync import sync_db_ledger  # noqa: E402
from quant_metrics import (  # noqa: E402
    aggregate_income_ledger,
    build_db_daily_pnl,
    build_db_equity_curve,
    build_income_breakdown,
    build_pnl_distribution,
    calculate_quant_risk_metrics,
)
from trade_enrichment import (  # noqa: E402
    enrich_trade_record,
    merge_active_positions,
    parse_to_timestamp,
)
from trade_reconstructor import reconstruct_binance_trades  # noqa: E402

logger = logging.getLogger("account_analytics")


class AccountAnalyticsEngine:
    """账户量化风控数据分析与指标聚合引擎。"""

    def __init__(
        self,
        client: Optional[Any] = None,
        initial_capital: Optional[float] = None,
        fee_rate: float = 0.0005,
        default_leverage: Optional[int] = None,
        offline_mode: bool = False,
    ) -> None:
        if initial_capital is not None:
            cap_val = float(initial_capital)
            if not math.isfinite(cap_val) or cap_val <= 0:
                raise ValueError(f"initial_capital must be a positive finite number, got {initial_capital!r}")
            self.initial_capital: Optional[float] = cap_val
        else:
            self.initial_capital = None
        self.fee_rate = float(fee_rate)
        if default_leverage is not None:
            lev_val = int(default_leverage)
            if lev_val <= 0:
                raise ValueError(f"default_leverage must be positive, got {default_leverage!r}")
            self.default_leverage: Optional[int] = lev_val
        else:
            self.default_leverage = None
        self.offline_mode = bool(offline_mode)

        if client is not None:
            self.client = client
        elif not self.offline_mode:
            self.client = BinanceFuturesClient.from_env()
        else:
            self.client = None

    def fetch_binance_data(self) -> Tuple[Optional[dict], List[dict], dict]:
        """
        拉取币安实盘合约账户与活跃持仓快照（资金流水由 ledger_sync 增量同步）。
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

        if self.offline_mode:
            logger.info("ℹ️ 运行在离线/本地回退模式 (Offline Mode)。")
            return None, [], api_status

        if not self.client or not self.client.is_configured():
            api_status["status_code"] = "NOT_CONFIGURED"
            api_status["message"] = "Binance API keys not configured"
            api_status["mode"] = "NOT_CONFIGURED"
            logger.warning("⚠️ 币安 API 凭证未配置 (NOT_CONFIGURED)。")
            return None, [], api_status

        api_status["configured"] = True

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
            return None, [], api_status

        active_positions_binance: List[dict] = []
        try:
            pos_data = self.client.get_active_positions(include_short=True)
            if not isinstance(pos_data, list):
                raise BinanceAPIError(
                    "Invalid /fapi/v2/positionRisk response",
                    code="INVALID_RESPONSE",
                    endpoint="/fapi/v2/positionRisk",
                )
            active_positions_binance = list(pos_data)
            api_status["positions_available"] = True
        except (BinanceAPIError, TimeoutError, OSError) as e:
            api_status["positions_error"] = str(e)
            if isinstance(e, BinanceAPIError) and e.code is not None and api_status["status_code"] == 0:
                api_status["status_code"] = e.code
            logger.warning("⚠️ 币安 /fapi/v2/positionRisk 查询失败: %s", e)

        api_status["connected"] = True
        api_status["status_code"] = 200
        if api_status["positions_available"]:
            api_status["mode"] = "BINANCE_SNAPSHOT_PENDING_INCOME"
            api_status["message"] = "Connected; pending incremental income sync"
        else:
            api_status["mode"] = "BINANCE_PARTIAL"
            api_status["message"] = "Binance position data unavailable"

        return account_info, active_positions_binance, api_status

    def load_local_positions_store(
        self, database: Optional[Path] = None
    ) -> Tuple[List[dict], List[dict]]:
        """Load open positions and closed position history from SQLite (`db_store`)."""
        db_path = Path(database) if database is not None else (resolve_database_path() or DEFAULT_DB_PATH)
        return (
            db_store.load_open_positions(db_path),
            db_store.load_position_history(db_path),
        )

    def run(
        self,
        database: Optional[Path] = None,
        *,
        recording_start_ms: Optional[int] = None,
    ) -> dict:
        """执行账户数据拉取、交易重构、风控指标核算并持久化到 SQLite。"""
        db_path = Path(database) if database is not None else (resolve_database_path() or DEFAULT_DB_PATH)

        account_info, active_positions_binance, api_status = self.fetch_binance_data()

        previous_report: Optional[Dict[str, Any]] = None
        recording: Optional[Dict[str, Any]] = None
        income_list: List[dict] = []
        fills_list: List[dict] = []
        new_income: List[dict] = []
        new_fills: List[dict] = []
        effective_recording_start_ms = recording_start_ms

        if not self.offline_mode:
            if api_status["mode"] == "NOT_CONFIGURED":
                raise BinanceAPIError(
                    "币安 API 凭证未配置 (NOT_CONFIGURED)；如需本地离线分析请传入 --offline",
                    code="NOT_CONFIGURED",
                )
            if api_status["mode"] != "BINANCE_SNAPSHOT_PENDING_INCOME":
                detail = (
                    api_status.get("positions_error")
                    or api_status.get("message")
                    or "Unknown error"
                )
                code = api_status.get("status_code")
                raise RuntimeError(
                    f"币安账户快照不完整 [{code}]: {detail}（保留上一次完整实盘快照）"
                )
            assert account_info is not None
            (
                recording,
                income_list,
                fills_list,
                new_income,
                new_fills,
                previous_report,
                effective_recording_start_ms,
            ) = sync_db_ledger(
                self.client,
                db_path,
                account_info,
                active_positions_binance,
                recording_start_ms,
            )
            api_status["income_available"] = True
            api_status["mode"] = "BINANCE_REALTIME"
            api_status["message"] = "Connected to Binance Live Futures API"

        local_positions, local_history = self.load_local_positions_store(db_path)
        if recording is not None:
            cutoff = int(recording["start_ms"])
            income_list = [item for item in income_list if int(item["time"]) >= cutoff]
            filtered_local: List[dict] = []
            for trade in local_history:
                raw_closed = trade.get("closed_at")
                ts = parse_to_timestamp(raw_closed)
                if ts is None:
                    raise ValueError(
                        f"Local position_history record for {trade.get('symbol')!r} has missing or unparseable closed_at: {raw_closed!r}"
                    )
                if ts * 1000 >= cutoff:
                    filtered_local.append(trade)
            local_history = filtered_local

        active_positions = merge_active_positions(
            active_positions_binance,
            local_positions,
            api_status["positions_available"],
            default_leverage=self.default_leverage,
        )

        open_position_fee = 0.0
        if recording is not None:
            assert account_info is not None
            trade_history, open_position_fee = reconstruct_binance_trades(
                income_list,
                local_history,
                fills=fills_list,
                fee_rate=self.fee_rate,
                default_leverage=self.default_leverage,
            )
            income_agg = aggregate_income_ledger(income_list, start_ms=cutoff)
            funding_fee_usdt = float(income_agg["funding_fee_usdt"])
            initial_equity = float(recording["initial_equity"])
            if not math.isfinite(initial_equity) or initial_equity < 0:
                raise ValueError(f"Invalid recording.initial_equity: {recording['initial_equity']!r}")
            curve_now_ms = (
                int(recording["ledger_synced_until_ms"])
                if recording.get("ledger_synced_until_ms") is not None
                else None
            )
            equity_curve_db, net_flow = build_db_equity_curve(
                recording,
                effective_recording_start_ms,
                previous_report,
                account_info,
                income_agg["transfers"],
                now_ms=curve_now_ms,
            )
            daily_pnl_db = build_db_daily_pnl(
                income_list,
                trade_history,
                initial_equity,
                income_agg=income_agg,
                start_ms=cutoff,
            )
            summary, equity_curve, daily_pnl = calculate_quant_risk_metrics(
                trades=trade_history,
                active_positions=active_positions,
                account_info=account_info,
                initial_capital=initial_equity,
                total_funding_fee=funding_fee_usdt,
                daily_pnl_override=daily_pnl_db,
                equity_curve_override=equity_curve_db,
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
            principal_source = "recording_start"
        else:
            trade_history = [
                enrich_trade_record(
                    t, idx, fee_rate=self.fee_rate, default_leverage=self.default_leverage
                )
                for idx, t in enumerate(local_history)
            ]
            income_agg = aggregate_income_ledger(())
            if self.initial_capital is not None:
                offline_initial_capital = self.initial_capital
                principal_source = "configured"
            else:
                persisted_recording = db_store.load_recording(db_path)
                if persisted_recording is not None:
                    offline_initial_capital = float(persisted_recording["initial_equity"])
                    if not math.isfinite(offline_initial_capital) or offline_initial_capital <= 0:
                        raise ValueError(
                            f"Invalid persisted recording.initial_equity: {persisted_recording['initial_equity']!r}"
                        )
                    principal_source = "recording_start"
                else:
                    raise ValueError(
                        "离线模式需要显式传入 --initial-capital（或数据库中已存在有效的 account_recording）"
                    )
            summary, equity_curve, daily_pnl = calculate_quant_risk_metrics(
                trades=trade_history,
                active_positions=active_positions,
                account_info=None,
                initial_capital=offline_initial_capital,
            )

        income_breakdown = build_income_breakdown(
            summary=summary,
            open_position_fee=open_position_fee,
            income_agg=income_agg,
            income_available=api_status["income_available"],
            principal_source=principal_source,
        )

        report: Dict[str, Any] = {
            "version": 1,
            "generated_at": get_beijing_time_str(),
            "data_source": {
                "BINANCE_REALTIME": "BINANCE_API",
                "OFFLINE_FALLBACK": "LOCAL_OFFLINE_FALLBACK",
            }[api_status["mode"]],
            "positions_source": "BINANCE_API" if api_status["positions_available"] else "LOCAL_CACHE",
            "api_status": api_status,
            "summary": summary,
            "active_positions": active_positions,
            "trade_history": trade_history,
            "equity_curve": equity_curve,
            "daily_pnl": daily_pnl,
            "pnl_distribution": build_pnl_distribution(trade_history),
            "income_breakdown": income_breakdown,
        }
        if recording is not None:
            report["recording"] = recording

        db_store.publish_report(db_path, report, new_income, new_fills)
        logger.info("✅ 账户实盘快照已写入 SQLite: %s", db_path)
        return report


def print_cli_summary(report: Mapping[str, Any]) -> None:
    """格式化打印终端量化风控仪表盘。"""
    summary = report["summary"]
    api_status = report["api_status"]
    active_positions = report["active_positions"]
    trade_history = report["trade_history"]

    print("\n" + "=" * 78)
    print(" 🚀 币安合约账户量化风控指标与交易评测仪表盘 (HUD)")
    print("=" * 78)
    print(f" 状态模式: {'🟢 币安实盘在线' if api_status['connected'] else '🟡 本地离线容灾回退'} | 更新时间: {report['generated_at']}")
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
            lev_str = f"{p['leverage']}x" if p["leverage"] is not None else "--"
            roe_str = f"{p['roe_pct']:+.2f}% ROE" if p["roe_pct"] is not None else "ROE 未知"
            p_tl_id = p.get("trendline_id")
            p_tl_label = p.get("trendline_label")
            p_has_tl = (p_tl_id is not None and not isinstance(p_tl_id, bool) and str(p_tl_id).strip() != "") or (
                isinstance(p_tl_label, str) and p_tl_label.strip() not in ("", "--")
            )
            p_tl = f" [{p_tl_label if (isinstance(p_tl_label, str) and p_tl_label.strip() not in ('', '--')) else p_tl_id}]" if p_has_tl else ""
            print(
                f"   • {p['symbol']} ({lev_str}): {p['amount']} @ ${p['entry_price']:.4f} -> 现价 ${p['mark_price']:.4f} | "
                f"浮盈: {pnl_c}{p['unrealized_pnl']:+.2f} USDT ({roe_str}){reset} | 周期: {p['holding_str']}{p_tl}"
            )
    else:
        print("   (当前无活跃持仓)")
    print("-" * 78)

    print(f" 📜 历史平仓明细 (Recent Closed Trades: {len(trade_history)}):")
    for t in trade_history[-5:]:
        net_val = t.get("net_pnl")
        pnl_c = "\033[32m" if (net_val is not None and net_val >= 0) else "\033[31m"
        t_tl_id = t.get("trendline_id")
        t_tl_label = t.get("trendline_label")
        t_has_tl = (t_tl_id is not None and not isinstance(t_tl_id, bool) and str(t_tl_id).strip() != "") or (
            isinstance(t_tl_label, str) and t_tl_label.strip() not in ("", "--")
        )
        tl = f"[{t_tl_label if (isinstance(t_tl_label, str) and t_tl_label.strip() not in ('', '--')) else t_tl_id}] " if t_has_tl else ""
        roe = f"{t['roe_pct']:+.2f}% ROE" if t["roe_pct"] is not None else "ROE 未知"
        entry_str = f"${t['entry_price']:.4f}" if t.get("entry_price") is not None else "--"
        exit_str = f"${t['exit_price']:.4f}" if t.get("exit_price") is not None else "--"
        net_str = f"{net_val:+.2f} USDT" if net_val is not None else "--"
        print(
            f"   [{t['closed_at']}] {t['symbol']} | 开: {entry_str} -> 平: {exit_str} | "
            f"净盈: {pnl_c}{net_str} ({roe}){reset} | 原因: {t['close_reason']} {tl}"
        )
    print("=" * 78 + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="币安合约账户量化风控数据分析引擎")
    parser.add_argument(
        "--database",
        type=Path,
        help="将完整实盘快照保存到 SQLite (默认: COIN_DATABASE 或 runs/account.sqlite3)",
    )
    parser.add_argument(
        "--initial-capital",
        type=float,
        default=None,
        help="初始基准资金 (USDT)",
    )
    parser.add_argument("--offline", action="store_true", help="强制启用离线回退模式 (不请求网络)")
    parser.add_argument("--json", action="store_true", help="仅输出 JSON 内容到终端")
    parser.add_argument("--summary", action="store_true", help="打印终端量化风控摘要")
    parser.add_argument("--recording-start-ms", type=int, help="开始记录的 Unix 时间戳（毫秒）")
    args = parser.parse_args()

    db_path = resolve_database_path(args.database) or DEFAULT_DB_PATH
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    engine = AccountAnalyticsEngine(
        initial_capital=args.initial_capital,
        offline_mode=args.offline,
    )
    report = engine.run(
        database=db_path,
        recording_start_ms=args.recording_start_ms,
    )

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.summary:
        print_cli_summary(report)


if __name__ == "__main__":
    main()
