#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Automated Test Suite for Account Quant Analytics Engine & Frontend HUD
======================================================================
Covers:
1. Trade & Position Data Enrichment:
   - Notional, fee, realized PnL, PnL %, ROE %, outcome, holding duration
   - Close reason mapping (dynamic trailing stop, trendline breakdown, manual close, binance)
   - Trendline metadata parsing & tolerance for malformed/missing fields
   - Active position mark-to-market, initial margin, and liquidation price estimation
   - Duration formatting across various time scales

2. Quant Risk Metrics Rigor:
   - Total Equity, Available Margin, Position Margin, Total Return %
   - Win Rate %, Win Rate ex-breakeven, Profit Factor, Payoff Ratio
   - Peak-to-trough Max Drawdown ($ and %) under complex multi-peak equity paths
   - Annualized Sharpe Ratio and Sortino Ratio (daily returns vs trade returns)
   - Edge cases: 0 trades, 100% win rate (infinite PF), 100% loss rate (0 PF)
   - Consecutive win/loss streaks and holding duration stats

3. Data Aggregation & Time Series:
   - Equity curve timeseries: baseline origin, chronological trade events, current real-time point
   - Daily PnL aggregation: date grouping, net PnL, return %, wins/losses counts

4. Binance Live Futures API Integration & Offline Fallback:
   - Mocking /fapi/v2/account, /fapi/v1/income, /fapi/v2/positionRisk
   - Graceful degradation on HTTP 451, network timeout, connection error, unconfigured keys
   - Safe handling of corrupted/malformed or missing trade_positions.json files without unhandled exceptions

5. Account HTML Generation & Structural Integrity:
   - Compilation of account.html via build_account_analytics
   - Fallback compilation when runs/account_analytics.json is missing or corrupted
   - Full DOM inspection: Key KPI cards, Chart.js canvas containers, table panels
   - Embedded JSON data schema validation
   - Global navigation bar and branding checks

6. Engine End-to-End Execution & Atomic Writing:
   - Atomic JSON output generation and validation against schema
======================================================================
"""

import os
import sys
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Setup import paths
ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import account_analytics_engine as aae
import binance_position_client as bpc
import build_account_analytics as baa


class TestDataEnrichment(unittest.TestCase):
    """1. 测试持仓与历史交易记录深度清洗补全逻辑"""

    def setUp(self):
        self.engine = aae.AccountAnalyticsEngine(offline_mode=True, initial_capital=10000.0)

    def test_enrich_winning_trade_with_trailing_stop(self):
        """测试盈利交易与移动止盈锁利状态清洗"""
        raw = {
            "symbol": "YBUSDT",
            "entry_price": 0.05,
            "amount": 1000.0,
            "entry_time": "2026-11-21 22:05:03",
            "exit_price": 0.05582,
            "closed_at": "2026-11-21 22:17:29",
            "trailing_stop_triggered": True,
            "status": "STOP_LOSS_TRIGGERED",
            "leverage": 5
        }
        enriched = self.engine.enrich_trade_record(raw, 0)
        self.assertEqual(enriched["symbol"], "YBUSDT")
        self.assertEqual(enriched["side"], "LONG")
        self.assertAlmostEqual(enriched["realized_pnl"], 5.82, places=2)
        self.assertAlmostEqual(enriched["pnl_pct"], 11.64, places=2)
        self.assertAlmostEqual(enriched["roe_pct"], 58.2, places=2)
        self.assertEqual(enriched["outcome"], "WIN")
        self.assertEqual(enriched["close_reason"], "动态移动保本锁利")
        self.assertEqual(enriched["duration_seconds"], 746)
        self.assertEqual(enriched["duration_str"], "12m 26s")
        self.assertGreater(enriched["net_pnl"], 5.7)

    def test_enrich_losing_trade_trendline_breakdown(self):
        """测试亏损交易与跌破趋势线止损清洗"""
        raw = {
            "symbol": "TESTUSDT",
            "entry_price": 10.0,
            "amount": 100.0,
            "entry_time": "2026-11-21 10:00:00",
            "exit_price": 9.2,
            "closed_at": "2026-11-21 10:30:00",
            "status": "BREAK_TRENDLINE_STOP",
            "leverage": 10,
            "trendline": {
                "id": "TL99",
                "p1": 11.0,
                "p2": 10.5,
                "dt1_str": "2026-11-20 12:00",
                "dt2_str": "2026-11-21 08:00"
            }
        }
        enriched = self.engine.enrich_trade_record(raw, 1)
        self.assertEqual(enriched["outcome"], "LOSS")
        self.assertAlmostEqual(enriched["realized_pnl"], -80.0, places=2)
        self.assertAlmostEqual(enriched["pnl_pct"], -8.0, places=2)
        self.assertAlmostEqual(enriched["roe_pct"], -80.0, places=2)
        self.assertEqual(enriched["close_reason"], "跌破趋势线止损")
        self.assertEqual(enriched["trendline_id"], "TL99")
        self.assertIn("TL#TL99", enriched["trendline_label"])

    def test_enrich_trade_missing_fields_tolerance(self):
        """测试历史交易缺失字段时的容错与兜底能力"""
        raw = {
            "symbol": "BINANCE:ETHUSDT.P",
            "entry_price": 3000.0,
            "amount": 2.0,
            # exit_price missing, current_price provided
            "current_price": 3150.0,
            # missing closed_at, entry_time
            "status": "MANUAL_CLOSE",
            "trendline": "invalid_trendline_type_str"
        }
        enriched = self.engine.enrich_trade_record(raw, 2)
        self.assertEqual(enriched["symbol"], "ETHUSDT")
        self.assertEqual(enriched["exit_price"], 3150.0)
        self.assertAlmostEqual(enriched["realized_pnl"], 300.0, places=2)
        self.assertEqual(enriched["outcome"], "WIN")
        self.assertEqual(enriched["close_reason"], "手动平仓")
        self.assertIsNone(enriched["trendline_id"])
        self.assertGreaterEqual(enriched["fee"], 0.0)

    def test_enrich_active_position(self):
        """测试活跃多头在途持仓清洗与强平价估算"""
        raw = {
            "symbol": "BTCUSDT",
            "entry_price": 90000.0,
            "amount": 0.5,
            "mark_price": 94500.0,
            "leverage": 10,
            "entry_time": "2026-11-22 12:00:00"
        }
        enriched = self.engine.enrich_active_position(raw, 0)
        self.assertEqual(enriched["symbol"], "BTCUSDT")
        self.assertAlmostEqual(enriched["notional"], 47250.0, places=2)
        self.assertAlmostEqual(enriched["initial_margin"], 4725.0, places=2)
        self.assertAlmostEqual(enriched["unrealized_pnl"], 2250.0, places=2)
        self.assertAlmostEqual(enriched["pnl_pct"], 5.0, places=2)
        self.assertAlmostEqual(enriched["roe_pct"], 50.0, places=2)
        self.assertAlmostEqual(enriched["liquidation_price"], 81900.0, places=1)

    def test_format_duration_edge_cases(self):
        """测试持仓周期文本格式化"""
        self.assertEqual(aae.format_duration(0), "0s")
        self.assertEqual(aae.format_duration(-10), "0s")
        self.assertEqual(aae.format_duration(45), "45s")
        self.assertEqual(aae.format_duration(300), "5m")
        self.assertEqual(aae.format_duration(746), "12m 26s")
        self.assertEqual(aae.format_duration(3665), "1h 1m 5s")
        self.assertEqual(aae.format_duration(86400 * 2 + 3600 * 3 + 120), "2d 3h 2m")


class TestQuantRiskCalculations(unittest.TestCase):
    """2. 测试量化风控指标数学严谨性与边界情况"""

    def setUp(self):
        self.engine = aae.AccountAnalyticsEngine(offline_mode=True, initial_capital=10000.0)

    def test_win_rate_and_profit_factor_standard(self):
        """测试标准样本下的胜率、盈亏比与净值计算"""
        trades = [
            {"realized_pnl": 100.0, "fee": 1.0, "net_pnl": 99.0, "pnl_pct": 10.0, "outcome": "WIN", "closed_ts": 1000, "closed_at": "2026-11-20 10:00:00", "duration_seconds": 60, "symbol": "A", "close_reason": "TP"},
            {"realized_pnl": -50.0, "fee": 1.0, "net_pnl": -51.0, "pnl_pct": -5.0, "outcome": "LOSS", "closed_ts": 2000, "closed_at": "2026-11-20 11:00:00", "duration_seconds": 120, "symbol": "B", "close_reason": "SL"},
            {"realized_pnl": 0.0, "fee": 0.5, "net_pnl": -0.5, "pnl_pct": 0.0, "outcome": "BREAKEVEN", "closed_ts": 3000, "closed_at": "2026-11-21 09:00:00", "duration_seconds": 30, "symbol": "C", "close_reason": "MC"},
            {"realized_pnl": 150.0, "fee": 1.5, "net_pnl": 148.5, "pnl_pct": 15.0, "outcome": "WIN", "closed_ts": 4000, "closed_at": "2026-11-21 12:00:00", "duration_seconds": 90, "symbol": "D", "close_reason": "TP"}
        ]
        summary, eq_curve, daily_pnl = self.engine.calculate_quant_risk_metrics(trades, [], None)

        self.assertEqual(summary["total_trades"], 4)
        self.assertEqual(summary["winning_trades"], 2)
        self.assertEqual(summary["losing_trades"], 1)
        self.assertEqual(summary["breakeven_trades"], 1)
        self.assertEqual(summary["win_rate_pct"], 50.0)
        self.assertAlmostEqual(summary["win_rate_ex_breakeven_pct"], 66.67, places=1)

        # Profit Factor: gross_profit (250) / gross_loss (50) = 5.0
        self.assertEqual(summary["profit_factor"], 5.0)

        # Cumulative PnL: 100 - 50 + 0 + 150 = 200.0
        self.assertEqual(summary["cumulative_realized_pnl"], 200.0)
        self.assertEqual(summary["total_fee"], 4.0)
        self.assertEqual(summary["net_realized_pnl"], 196.0)

        # Total Equity: 10000 + 196 = 10196.0
        self.assertEqual(summary["total_equity"], 10196.0)
        self.assertAlmostEqual(summary["total_return_pct"], 1.96, places=2)

    def test_all_wins_infinite_profit_factor(self):
        """测试全部盈利交易时获利因子设为 999.0 且不发生除零错误"""
        trades = [
            {"realized_pnl": 50.0, "fee": 0.1, "net_pnl": 49.9, "pnl_pct": 5.0, "outcome": "WIN", "closed_ts": 1000, "closed_at": "2026-11-20 10:00:00", "duration_seconds": 60, "symbol": "A", "close_reason": "TP"},
            {"realized_pnl": 80.0, "fee": 0.1, "net_pnl": 79.9, "pnl_pct": 8.0, "outcome": "WIN", "closed_ts": 2000, "closed_at": "2026-11-20 11:00:00", "duration_seconds": 60, "symbol": "B", "close_reason": "TP"}
        ]
        summary, _, _ = self.engine.calculate_quant_risk_metrics(trades, [], None)
        self.assertEqual(summary["win_rate_pct"], 100.0)
        self.assertEqual(summary["profit_factor"], 999.0)
        self.assertEqual(summary["max_consecutive_wins"], 2)
        self.assertEqual(summary["max_consecutive_losses"], 0)

    def test_all_losses_zero_profit_factor(self):
        """测试全部亏损交易时的风控指标"""
        trades = [
            {"realized_pnl": -30.0, "fee": 0.1, "net_pnl": -30.1, "pnl_pct": -3.0, "outcome": "LOSS", "closed_ts": 1000, "closed_at": "2026-11-20 10:00:00", "duration_seconds": 60, "symbol": "A", "close_reason": "SL"},
            {"realized_pnl": -70.0, "fee": 0.1, "net_pnl": -70.1, "pnl_pct": -7.0, "outcome": "LOSS", "closed_ts": 2000, "closed_at": "2026-11-20 11:00:00", "duration_seconds": 60, "symbol": "B", "close_reason": "SL"}
        ]
        summary, _, _ = self.engine.calculate_quant_risk_metrics(trades, [], None)
        self.assertEqual(summary["win_rate_pct"], 0.0)
        self.assertEqual(summary["profit_factor"], 0.0)
        self.assertEqual(summary["max_consecutive_losses"], 2)
        self.assertAlmostEqual(summary["max_drawdown_usd"], 100.2, places=1)

    def test_zero_trades_empty_state_robustness(self):
        """测试零交易空状态时的稳健性，确保不抛出 ZeroDivisionError"""
        summary, eq_curve, daily_pnl = self.engine.calculate_quant_risk_metrics([], [], None)
        self.assertEqual(summary["total_trades"], 0)
        self.assertEqual(summary["win_rate_pct"], 0.0)
        self.assertEqual(summary["profit_factor"], 0.0)
        self.assertEqual(summary["max_drawdown_usd"], 0.0)
        self.assertEqual(summary["max_drawdown_pct"], 0.0)
        self.assertEqual(summary["sharpe_ratio"], 0.0)
        self.assertEqual(summary["sortino_ratio"], 0.0)
        self.assertEqual(summary["total_equity"], 10000.0)
        self.assertEqual(len(daily_pnl), 0)
        self.assertGreaterEqual(len(eq_curve), 2)  # baseline + current real-time

    def test_max_drawdown_calculation_complex(self):
        """测试多峰值与深回撤路径下的最大回撤计算"""
        # 初始 10000 -> +500 (10500) -> -300 (10200, DD=300, DD%=2.86%) -> -200 (10000, DD=500, DD%=4.76%) -> +1000 (11000)
        trades = [
            {"realized_pnl": 500.0, "fee": 0.0, "net_pnl": 500.0, "pnl_pct": 5.0, "outcome": "WIN", "closed_ts": 1000, "closed_at": "2026-11-20 10:00:00", "duration_seconds": 60, "symbol": "A", "close_reason": "TP"},
            {"realized_pnl": -300.0, "fee": 0.0, "net_pnl": -300.0, "pnl_pct": -3.0, "outcome": "LOSS", "closed_ts": 2000, "closed_at": "2026-11-20 11:00:00", "duration_seconds": 60, "symbol": "B", "close_reason": "SL"},
            {"realized_pnl": -200.0, "fee": 0.0, "net_pnl": -200.0, "pnl_pct": -2.0, "outcome": "LOSS", "closed_ts": 3000, "closed_at": "2026-11-20 12:00:00", "duration_seconds": 60, "symbol": "C", "close_reason": "SL"},
            {"realized_pnl": 1000.0, "fee": 0.0, "net_pnl": 1000.0, "pnl_pct": 10.0, "outcome": "WIN", "closed_ts": 4000, "closed_at": "2026-11-20 13:00:00", "duration_seconds": 60, "symbol": "D", "close_reason": "TP"},
        ]
        summary, eq_curve, _ = self.engine.calculate_quant_risk_metrics(trades, [], None)
        self.assertEqual(summary["max_drawdown_usd"], 500.0)
        self.assertAlmostEqual(summary["max_drawdown_pct"], 4.76, places=2)

    def test_streaks_and_durations(self):
        """测试连续连胜/连亏与持仓时长统计"""
        trades = [
            {"realized_pnl": 10.0, "fee": 0.0, "net_pnl": 10.0, "pnl_pct": 1.0, "outcome": "WIN", "closed_ts": 1000, "closed_at": "2026-11-20 10:00:00", "duration_seconds": 100, "symbol": "A", "close_reason": "TP"},
            {"realized_pnl": 20.0, "fee": 0.0, "net_pnl": 20.0, "pnl_pct": 2.0, "outcome": "WIN", "closed_ts": 2000, "closed_at": "2026-11-20 11:00:00", "duration_seconds": 200, "symbol": "B", "close_reason": "TP"},
            {"realized_pnl": 30.0, "fee": 0.0, "net_pnl": 30.0, "pnl_pct": 3.0, "outcome": "WIN", "closed_ts": 3000, "closed_at": "2026-11-20 12:00:00", "duration_seconds": 300, "symbol": "C", "close_reason": "TP"},
            {"realized_pnl": -5.0, "fee": 0.0, "net_pnl": -5.0, "pnl_pct": -0.5, "outcome": "LOSS", "closed_ts": 4000, "closed_at": "2026-11-20 13:00:00", "duration_seconds": 400, "symbol": "D", "close_reason": "SL"},
        ]
        summary, _, _ = self.engine.calculate_quant_risk_metrics(trades, [], None)
        self.assertEqual(summary["max_consecutive_wins"], 3)
        self.assertEqual(summary["max_consecutive_losses"], 1)
        self.assertEqual(summary["avg_hold_duration_seconds"], 250)
        self.assertEqual(summary["avg_hold_duration_str"], "4m 10s")
        self.assertEqual(summary["min_hold_duration_seconds"], 100)
        self.assertEqual(summary["max_hold_duration_seconds"], 400)

    def test_sharpe_and_sortino_daily_multi_day(self):
        """测试多日交易下的日度收益年化夏普比率与索提诺比率"""
        trades = [
            {"realized_pnl": 100.0, "fee": 1.0, "net_pnl": 99.0, "pnl_pct": 1.0, "outcome": "WIN", "closed_ts": 1000, "closed_at": "2026-11-18 10:00:00", "duration_seconds": 60, "symbol": "A", "close_reason": "TP"},
            {"realized_pnl": -40.0, "fee": 1.0, "net_pnl": -41.0, "pnl_pct": -0.4, "outcome": "LOSS", "closed_ts": 2000, "closed_at": "2026-11-19 11:00:00", "duration_seconds": 60, "symbol": "B", "close_reason": "SL"},
            {"realized_pnl": 120.0, "fee": 1.0, "net_pnl": 119.0, "pnl_pct": 1.2, "outcome": "WIN", "closed_ts": 3000, "closed_at": "2026-11-20 12:00:00", "duration_seconds": 60, "symbol": "C", "close_reason": "TP"},
        ]
        summary, _, daily_pnl = self.engine.calculate_quant_risk_metrics(trades, [], None)
        self.assertEqual(summary["sharpe_method"], "DAILY_RETURNS")
        self.assertGreater(summary["sharpe_ratio"], 0.0)
        self.assertGreater(summary["sortino_ratio"], 0.0)
        self.assertEqual(len(daily_pnl), 3)


class TestDataAggregation(unittest.TestCase):
    """3. 测试资金净值时间序列与每日盈亏聚合"""

    def setUp(self):
        self.engine = aae.AccountAnalyticsEngine(offline_mode=True, initial_capital=10000.0)

    def test_equity_curve_timeseries_monotonicity_and_points(self):
        """测试净值曲线时间序列的基准点、平仓记录点与当前点"""
        trades = [
            {"realized_pnl": 200.0, "fee": 2.0, "net_pnl": 198.0, "pnl_pct": 2.0, "outcome": "WIN", "entry_ts": 1795000000000, "closed_ts": 1795001000000, "closed_at": "2026-11-20 10:00:00", "duration_seconds": 1000, "symbol": "BTCUSDT", "close_reason": "TP"},
            {"realized_pnl": -50.0, "fee": 1.0, "net_pnl": -51.0, "pnl_pct": -0.5, "outcome": "LOSS", "entry_ts": 1795002000000, "closed_ts": 1795003000000, "closed_at": "2026-11-20 12:00:00", "duration_seconds": 1000, "symbol": "ETHUSDT", "close_reason": "SL"},
        ]
        active_positions = [
            {"symbol": "SOLUSDT", "unrealized_pnl": 35.0, "initial_margin": 200.0}
        ]
        summary, eq_curve, daily_pnl = self.engine.calculate_quant_risk_metrics(trades, active_positions, None)

        # 检查点数量: 1(基准) + 2(交易) + 1(实时) = 4
        self.assertEqual(len(eq_curve), 4)

        # 检查第 1 点为初始本金
        self.assertEqual(eq_curve[0]["equity"], 10000.0)
        self.assertEqual(eq_curve[0]["cumulative_pnl"], 0.0)

        # 检查第 2 点 (盈利后)
        self.assertEqual(eq_curve[1]["equity"], 10198.0)
        self.assertEqual(eq_curve[1]["cumulative_pnl"], 198.0)

        # 检查第 3 点 (亏损后)
        self.assertEqual(eq_curve[2]["equity"], 10147.0)
        self.assertEqual(eq_curve[2]["drawdown_usd"], 51.0)

        # 检查第 4 点 (当前净值含 35 浮盈)
        self.assertEqual(eq_curve[3]["equity"], 10182.0)

        # 时间戳单调递增
        for i in range(1, len(eq_curve)):
            self.assertGreaterEqual(eq_curve[i]["timestamp"], eq_curve[i-1]["timestamp"])

    def test_daily_pnl_aggregation_grouping(self):
        """测试按自然日分组统计盈亏与交易笔数"""
        trades = [
            {"realized_pnl": 100.0, "fee": 1.0, "net_pnl": 99.0, "pnl_pct": 1.0, "outcome": "WIN", "closed_ts": 1000, "closed_at": "2026-11-20 09:00:00", "duration_seconds": 60, "symbol": "A", "close_reason": "TP"},
            {"realized_pnl": 50.0, "fee": 0.5, "net_pnl": 49.5, "pnl_pct": 0.5, "outcome": "WIN", "closed_ts": 2000, "closed_at": "2026-11-20 15:00:00", "duration_seconds": 60, "symbol": "B", "close_reason": "TP"},
            {"realized_pnl": -30.0, "fee": 0.3, "net_pnl": -30.3, "pnl_pct": -0.3, "outcome": "LOSS", "closed_ts": 3000, "closed_at": "2026-11-21 10:00:00", "duration_seconds": 60, "symbol": "C", "close_reason": "SL"},
        ]
        _, _, daily_pnl = self.engine.calculate_quant_risk_metrics(trades, [], None)
        self.assertEqual(len(daily_pnl), 2)

        day1 = daily_pnl[0]
        self.assertEqual(day1["date"], "2026-11-20")
        self.assertEqual(day1["trades_count"], 2)
        self.assertEqual(day1["wins"], 2)
        self.assertEqual(day1["net_pnl"], 148.5)

        day2 = daily_pnl[1]
        self.assertEqual(day2["date"], "2026-11-21")
        self.assertEqual(day2["trades_count"], 1)
        self.assertEqual(day2["losses"], 1)
        self.assertEqual(day2["net_pnl"], -30.3)


class TestBinanceAPIIntegrationAndFallback(unittest.TestCase):
    """4. 测试币安合约 API 请求、网络故障与本地离线回退机制"""

    def test_client_endpoints_success_mock(self):
        """测试正常响应时 /fapi/v2/account 与 /fapi/v1/income 的解析"""
        client = bpc.BinanceFuturesClient(api_key="mock_key", api_secret="mock_sec")

        mock_acc_data = {
            "totalWalletBalance": "15420.50",
            "totalMarginBalance": "15600.80",
            "availableBalance": "14200.00",
            "totalUnrealizedProfit": "180.30"
        }
        mock_income_data = [
            {"symbol": "YBUSDT", "incomeType": "REALIZED_PNL", "income": "12.50", "time": 1795200000000},
            {"symbol": "YBUSDT", "incomeType": "COMMISSION", "income": "-0.05", "time": 1795200000000},
            {"symbol": "YBUSDT", "incomeType": "FUNDING_FEE", "income": "-0.02", "time": 1795200000000}
        ]

        def fake_send(endpoint, params=None):
            if endpoint == "/fapi/v2/account":
                return mock_acc_data, None
            elif endpoint == "/fapi/v1/income":
                return mock_income_data, None
            elif endpoint == "/fapi/v2/positionRisk":
                return [], None
            return None, {"code": 404, "msg": "Not Found"}

        with patch.object(client, "_send_signed_request", side_effect=fake_send):
            acc = client.get_account_info()
            self.assertEqual(acc["totalWalletBalance"], "15420.50")

            income = client.get_income()
            self.assertEqual(len(income), 3)

            engine = aae.AccountAnalyticsEngine(client=client)
            acc_info, inc_list, active_p, status = engine.fetch_binance_data()
            self.assertTrue(status["connected"])
            self.assertEqual(status["status_code"], 200)

    def test_account_report_distinguishes_empty_positions_from_request_failure(self):
        client = bpc.BinanceFuturesClient("test-key", "test-secret")
        engine = aae.AccountAnalyticsEngine(client=client)
        local_positions = [{"symbol": "YBUSDT", "entry_price": 1, "amount": 1}]
        income = [{"symbol": "YBUSDT", "incomeType": "REALIZED_PNL", "income": "12.50", "time": 1795200000000}]
        with tempfile.TemporaryDirectory() as directory, patch.object(
            engine, "load_local_positions_store", return_value=(local_positions, [])
        ):
            path = Path(directory) / "account.json"
            account_response = ({"totalWalletBalance": "100"}, None)
            with patch.object(client, "_send_signed_request", side_effect=[
                account_response, (income, None), ([], None)
            ]):
                report = engine.run(path)
            self.assertEqual(report["active_positions"], [])
            self.assertEqual(report["positions_source"], "BINANCE_API")
            self.assertIsNone(report["trade_history"][0]["pnl_pct"])
            self.assertIsNone(report["summary"]["avg_trade_pnl_pct"])
            self.assertEqual(report["pnl_distribution"]["scatter"], [])
            coverage = report["income_breakdown"]
            self.assertEqual(coverage["coverage_start"], coverage["coverage_end"])
            self.assertEqual(coverage["raw_count"], 1)

            with patch.object(client, "_send_signed_request", side_effect=[
                account_response, ([], None), (None, {"code": "NETWORK_ERROR"})
            ]):
                report = engine.run(path)
            self.assertEqual(report["active_positions"][0]["symbol"], "YBUSDT")
            self.assertEqual(report["positions_source"], "LOCAL_CACHE")
            self.assertEqual(report["api_status"]["mode"], "BINANCE_PARTIAL")

    def test_full_income_page_reports_coverage_without_inferring_principal(self):
        client = bpc.BinanceFuturesClient("test-key", "test-secret")
        engine = aae.AccountAnalyticsEngine(client=client)
        income = [
            {"incomeType": "TRANSFER", "income": "1", "time": 1795200000000 + i * 1000}
            for i in range(aae.INCOME_PAGE_LIMIT)
        ]
        with tempfile.TemporaryDirectory() as directory, patch.object(
            engine, "load_local_positions_store", return_value=([], [])
        ), patch.object(client, "_send_signed_request", side_effect=[
            ({"totalWalletBalance": "100"}, None), (income, None), ([], None)
        ]):
            report = engine.run(Path(directory) / "account.json")

        coverage = report["income_breakdown"]
        self.assertTrue(coverage["page_full"])
        self.assertLess(coverage["coverage_start"], coverage["coverage_end"])
        self.assertEqual(coverage["principal_source"], "default")
        dashboard = baa.render_account_html(baa.normalize_account_data(report))
        self.assertIn("已达单页上限，更早流水未纳入", dashboard)
        self.assertIn(coverage["coverage_start"], dashboard)
        self.assertIn(coverage["coverage_end"], dashboard)

    def test_client_http_451_graceful_fallback(self):
        """测试遇到 HTTP 451 区域受限时平滑降级至本地离线模式"""
        client = bpc.BinanceFuturesClient(api_key="mock_key", api_secret="mock_sec")

        def fake_send_451(endpoint, params=None):
            return None, {"code": 451, "msg": "Service unavailable from a restricted location"}

        with patch.object(client, "_send_signed_request", side_effect=fake_send_451):
            engine = aae.AccountAnalyticsEngine(client=client)
            acc_info, inc_list, active_p, status = engine.fetch_binance_data()
            self.assertFalse(status["connected"])
            self.assertEqual(status["status_code"], 451)
            self.assertEqual(status["mode"], "OFFLINE_FALLBACK")
            self.assertIsNone(acc_info)

    def test_client_network_timeout_and_exceptions_graceful_fallback(self):
        """测试网络超时或未捕获连接异常时优雅降级，不抛出异常"""
        client = bpc.BinanceFuturesClient(api_key="mock_key", api_secret="mock_sec")

        with patch.object(client, "_send_signed_request", side_effect=TimeoutError("Connection timed out")):
            engine = aae.AccountAnalyticsEngine(client=client)
            acc_info, inc_list, active_p, status = engine.fetch_binance_data()
            self.assertFalse(status["connected"])
            self.assertEqual(status["status_code"], -1)
            self.assertEqual(status["mode"], "OFFLINE_FALLBACK")
            self.assertIn("timed out", status["message"])

    def test_offline_mode_flag_bypasses_network(self):
        """测试 offline_mode=True 时直接绕过网络请求"""
        mock_client = MagicMock()
        engine = aae.AccountAnalyticsEngine(client=mock_client, offline_mode=True)
        acc_info, inc_list, active_p, status = engine.fetch_binance_data()
        self.assertFalse(status["connected"])
        self.assertEqual(status["mode"], "OFFLINE_FALLBACK")
        mock_client._send_signed_request.assert_not_called()

    def test_corrupted_positions_file_handling(self):
        """测试当 trade_positions.json 损坏或格式错误时，降级读取不抛出异常"""
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tf:
            tf.write("CORRUPTED_JSON_DATA {{{}}")
            corrupt_path = Path(tf.name)

        try:
            engine = aae.AccountAnalyticsEngine(positions_path=corrupt_path, offline_mode=True)
            pos, hist = engine.load_local_positions_store()
            self.assertEqual(pos, [])
            self.assertEqual(hist, [])

            # run() 端到端执行亦不应崩溃
            with tempfile.TemporaryDirectory() as tmp_dir:
                out_json = Path(tmp_dir) / "output.json"
                report = engine.run(output_path=out_json)
                self.assertIsNotNone(report)
                self.assertEqual(report["summary"]["total_trades"], 0)
        finally:
            if corrupt_path.exists():
                corrupt_path.unlink()

    def test_missing_positions_file_handling(self):
        """测试当 trade_positions.json 文件不存在时的容错"""
        missing_path = Path("/tmp/definitely_non_existent_file_12345.json")
        engine = aae.AccountAnalyticsEngine(positions_path=missing_path, offline_mode=True)
        pos, hist = engine.load_local_positions_store()
        self.assertEqual(pos, [])
        self.assertEqual(hist, [])


class TestAccountHTMLGenerationAndIntegrity(unittest.TestCase):
    """5. 测试 account.html 生成物与 DOM 结构完整性"""

    def setUp(self):
        self.html_path = ROOT_DIR / "account.html"
        self.analytics_path = ROOT_DIR / "runs" / "account_analytics.json"

    def test_build_account_analytics_execution(self):
        """测试 build_account_analytics 主构建函数正常执行并产出文件"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            temp_out = Path(tmp_dir) / "test_account.html"
            out = baa.build_account_analytics(output_path=temp_out)
            self.assertTrue(temp_out.exists())
            self.assertGreater(temp_out.stat().st_size, 5000)

    def test_account_html_exists_and_valid_size(self):
        """测试主工程根目录下 account.html 存在且体积充分 (> 20KB)"""
        self.assertTrue(self.html_path.exists(), "account.html 必须存在于项目根目录")
        size = self.html_path.stat().st_size
        self.assertGreater(size, 20480, f"account.html 体积过小 ({size} bytes)，可能未正确渲染")

    def test_html_contains_all_key_kpis(self):
        """验证 HTML 包含所有 10 项核心 KPI 矩阵卡片 ID"""
        content = self.html_path.read_text(encoding="utf-8")
        expected_kpis = [
            "kpi-total-equity",
            "kpi-available-margin",
            "kpi-realized-pnl",
            "kpi-unrealized-pnl",
            "kpi-win-rate",
            "kpi-profit-factor",
            "kpi-max-drawdown",
            "kpi-sharpe-ratio",
            "kpi-sortino-ratio",
            "kpi-total-trades"
        ]
        for kpi_id in expected_kpis:
            self.assertIn(f'id="{kpi_id}"', content, f"account.html 缺少关键 KPI 元素: {kpi_id}")

    def test_html_contains_all_chart_containers(self):
        """验证 HTML 包含所有 4 个核心图表 canvas 容器及 Chart.js 引入"""
        content = self.html_path.read_text(encoding="utf-8")
        expected_charts = [
            "chart-equity-curve",
            "chart-pnl-drawdown",
            "chart-daily-pnl",
            "chart-pnl-dist"
        ]
        for chart_id in expected_charts:
            self.assertIn(f'id="{chart_id}"', content, f"account.html 缺少图表容器: {chart_id}")
        self.assertIn("chart.js", content.lower(), "account.html 缺少 Chart.js 库引用")

    def test_html_contains_active_positions_and_history_tables(self):
        """验证活跃持仓与历史开平仓全量流水表格结构"""
        content = self.html_path.read_text(encoding="utf-8")
        self.assertIn('id="active-positions-panel"', content)
        self.assertIn('id="active-positions-tbody"', content)
        self.assertIn('id="trade-history-table"', content)
        self.assertIn('id="trade-history-tbody"', content)
        self.assertIn('id="search-input"', content)

    def test_html_embedded_json_integrity(self):
        """验证 account.html 内部嵌入的 JSON 状态数据完整合法"""
        content = self.html_path.read_text(encoding="utf-8")
        start_tag = '<script id="account-data" type="application/json">'
        end_tag = '</script>'
        self.assertIn(start_tag, content)

        start_idx = content.find(start_tag) + len(start_tag)
        end_idx = content.find(end_tag, start_idx)
        raw_json = content[start_idx:end_idx].strip()

        data = json.loads(raw_json)
        self.assertIsInstance(data, dict)
        self.assertIn("summary", data)
        self.assertIn("active_positions", data)
        self.assertIn("trade_history", data)
        self.assertIn("equity_curve", data)
        self.assertIn("daily_pnl", data)

        s = data["summary"]
        self.assertIn("total_equity", s)
        self.assertIn("win_rate_pct", s)
        self.assertIn("profit_factor", s)
        self.assertIn("max_drawdown_pct", s)

    def test_html_global_nav_header_links(self):
        """验证统一导航栏包含所有三个核心视图链接并高亮账户页面"""
        content = self.html_path.read_text(encoding="utf-8")
        self.assertIn('href="index.html"', content)
        self.assertIn('href="account.html"', content)
        self.assertIn("strategy_second_leg_playbook.html", content)
        self.assertIn("账户数据分析", content)

    def test_build_fallback_when_analytics_json_missing(self):
        """测试当 runs/account_analytics.json 不存在时，build 脚本仍能降级生成有效 HTML"""
        missing_analytics = Path("/tmp/non_existent_analytics_9999.json")
        with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as tf:
            fallback_html_path = Path(tf.name)

        try:
            baa.build_account_analytics(
                analytics_path=missing_analytics,
                output_path=fallback_html_path
            )
            self.assertTrue(fallback_html_path.exists())
            html_text = fallback_html_path.read_text(encoding="utf-8")
            self.assertIn("kpi-total-equity", html_text)
            self.assertIn("chart-equity-curve", html_text)
        finally:
            if fallback_html_path.exists():
                fallback_html_path.unlink()


class TestFullEngineExecution(unittest.TestCase):
    """6. 测试引擎端到端执行、文件输出与原子化写入"""

    def test_run_generates_valid_json(self):
        """测试 AccountAnalyticsEngine.run() 生成符合规范的 JSON 文件"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_file = Path(tmp_dir) / "account_analytics.json"
            engine = aae.AccountAnalyticsEngine(offline_mode=True, initial_capital=10000.0)
            report = engine.run(output_path=out_file)

            self.assertTrue(out_file.exists())
            with open(out_file, "r", encoding="utf-8") as f:
                saved_data = json.load(f)

            self.assertEqual(saved_data["version"], 1)
            self.assertIn("summary", saved_data)
            self.assertIn("equity_curve", saved_data)
            self.assertIn("daily_pnl", saved_data)
            self.assertIn("trade_history", saved_data)
            self.assertIn("active_positions", saved_data)
            self.assertIn("income_breakdown", saved_data)

            # 验证 summary 必含指标
            s = saved_data["summary"]
            for field in [
                "total_equity", "wallet_balance", "available_margin", "cumulative_realized_pnl",
                "win_rate_pct", "profit_factor", "max_drawdown_pct", "sharpe_ratio", "sortino_ratio"
            ]:
                self.assertIn(field, s)

    def test_atomic_write_json_atomicity(self):
        """测试 atomic_write_json 原子化写入"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "data.json"
            aae.atomic_write_json(target, {"status": "ok", "step": 1})
            self.assertTrue(target.exists())
            with open(target, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.assertEqual(d["status"], "ok")


if __name__ == "__main__":
    unittest.main()
