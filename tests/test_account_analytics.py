#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Automated Test Suite for Account Quant Analytics Engine & Static Frontend Shell
==============================================================================
Covers:
1. Trade & Position Data Enrichment:
   - LONG & SHORT PnL / PnL % / ROE %, negative fee rebates, strict no-fabrication on missing fields
   - Active position Binance updateTime extraction & local trendline/trailing metadata preservation
2. Quant Risk Metrics Rigor:
   - Unified net-PnL basis across outcome, profit factor, streaks, daily PnL, Sharpe, Sortino
   - Calendar-gap zero-return filling for daily Sharpe & standard N-period Sortino denominator
   - Zero equity/margin non-coercion and None for undefined ratios
3. Unified FIFO Trade Reconstruction:
   - Multi-roundtrip, partial close, Hedge (LONG/SHORT) & One-Way (BOTH) modes
   - Opening + closing USDT fee attribution & non-USDT (BNB) fee isolation
   - Interleaved-symbol income-only reconstruction
4. Binance Live Futures API Integration & DB Recording:
   - Graceful degradation on BinanceAPIError (451/network) without swallowing logic bugs
   - Corrupt trade_positions.json failing loudly
   - SQLite WAL sync, compaction, and post-DB consistency
5. Static Account HTML Shell Integrity:
   - Pure static shell verification (static/styles.css, static/account.js, no inline CSS/JS/data)
   - All KPI, chart, 9-column active positions, and history filter DOM IDs
"""

from contextlib import closing
import json
import math
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import account_analytics_engine as aae  # noqa: E402
import binance_client as bc  # noqa: E402


class TestDataEnrichment(unittest.TestCase):
    """1. 测试持仓与历史交易记录清洗及严格防伪造逻辑"""

    def setUp(self):
        self.engine = aae.AccountAnalyticsEngine(offline_mode=True, initial_capital=10000.0)

    def test_enrich_winning_trade_with_trailing_stop(self):
        raw = {
            "symbol": "YBUSDT",
            "entry_price": 0.05,
            "amount": 1000.0,
            "entry_time": "2026-11-21 22:05:03",
            "exit_price": 0.05582,
            "closed_at": "2026-11-21 22:17:29",
            "trailing_stop_triggered": True,
            "status": "STOP_LOSS_TRIGGERED",
            "leverage": 5,
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
                "dt2_str": "2026-11-21 08:00",
            },
        }
        enriched = self.engine.enrich_trade_record(raw, 1)
        self.assertEqual(enriched["outcome"], "LOSS")
        self.assertAlmostEqual(enriched["realized_pnl"], -80.0, places=2)
        self.assertAlmostEqual(enriched["pnl_pct"], -8.0, places=2)
        self.assertAlmostEqual(enriched["roe_pct"], -80.0, places=2)
        self.assertEqual(enriched["close_reason"], "跌破趋势线止损")
        self.assertEqual(enriched["trendline_id"], "TL99")
        self.assertIn("TL#TL99", enriched["trendline_label"])

    def test_enrich_short_trade_and_negative_fee_rebate(self):
        raw = {
            "symbol": "BINANCE:ETHUSDT.P",
            "side": "SHORT",
            "entry_price": 3000.0,
            "exit_price": 2850.0,
            "amount": 2.0,
            "leverage": 5,
            "fee": -0.5,
            "entry_time": "2026-11-21 10:00:00",
            "closed_at": "2026-11-21 10:10:00",
            "status": "MANUAL_CLOSE",
        }
        enriched = self.engine.enrich_trade_record(raw, 0)
        self.assertEqual(enriched["symbol"], "ETHUSDT")
        self.assertEqual(enriched["side"], "SHORT")
        self.assertAlmostEqual(enriched["realized_pnl"], 300.0, places=2)
        self.assertAlmostEqual(enriched["fee"], -0.5, places=2)
        self.assertAlmostEqual(enriched["net_pnl"], 300.5, places=2)
        self.assertAlmostEqual(enriched["pnl_pct"], 5.0, places=2)
        self.assertAlmostEqual(enriched["roe_pct"], 25.0, places=2)
        self.assertEqual(enriched["outcome"], "WIN")

    def test_enrich_trade_does_not_fabricate_missing_exit_or_time(self):
        raw = {
            "symbol": "ETHUSDT",
            "entry_price": 3000.0,
            "amount": 2.0,
            "current_price": 3150.0,
            "highest_price": 3200.0,
            "status": "MANUAL_CLOSE",
            "trendline": {"id": "TL1", "p1": "1.23", "p2": "1.11", "state": None},
        }
        enriched = self.engine.enrich_trade_record(raw, 2)
        self.assertIsNone(enriched["exit_price"])
        self.assertIsNone(enriched["realized_pnl"])
        self.assertIsNone(enriched["net_pnl"])
        self.assertIsNone(enriched["pnl_pct"])
        self.assertEqual(enriched["outcome"], "UNKNOWN")
        self.assertIsNone(enriched["entry_ts"])
        self.assertIsNone(enriched["closed_ts"])
        self.assertEqual(enriched["entry_time"], "--")
        self.assertEqual(enriched["closed_at"], "--")
        self.assertEqual(enriched["duration_str"], "--")
        self.assertEqual(enriched["trendline_label"], "TL#TL1 (1.2300 -> 1.1100)")

    def test_enrich_active_position(self):
        raw = {
            "symbol": "BTCUSDT",
            "entry_price": 90000.0,
            "amount": 0.5,
            "mark_price": 94500.0,
            "leverage": 10,
            "entry_time": "2026-11-22 12:00:00",
        }
        enriched = self.engine.enrich_active_position(raw, 0)
        self.assertEqual(enriched["symbol"], "BTCUSDT")
        self.assertAlmostEqual(enriched["notional"], 47250.0, places=2)
        self.assertAlmostEqual(enriched["initial_margin"], 4725.0, places=2)
        self.assertAlmostEqual(enriched["unrealized_pnl"], 2250.0, places=2)
        self.assertAlmostEqual(enriched["pnl_pct"], 5.0, places=2)
        self.assertAlmostEqual(enriched["roe_pct"], 50.0, places=2)
        self.assertAlmostEqual(enriched["liquidation_price"], 81900.0, places=1)

    def test_enrich_active_position_uses_update_time_and_merges_local_meta(self):
        update_ms = int((time.time() - 600) * 1000)
        binance_pos = [
            {
                "symbol": "SOLUSDT",
                "positionAmt": "10",
                "entryPrice": "150",
                "markPrice": "165",
                "unRealizedProfit": "150",
                "leverage": "5",
                "positionSide": "BOTH",
                "updateTime": update_ms,
            }
        ]
        local_pos = [
            {
                "symbol": "SOLUSDT",
                "side": "LONG",
                "amount": 10,
                "trailing_stop_triggered": True,
                "trailing_stop_price": 151.5,
                "stop_loss_notified": False,
                "highest_price": 168.0,
                "trendline": {"id": "TL_SOL", "p1": 160, "p2": 152},
            }
        ]
        merged = self.engine._merge_active_positions(binance_pos, local_pos, positions_available=True)
        self.assertEqual(len(merged), 1)
        pos = merged[0]
        self.assertEqual(pos["symbol"], "SOLUSDT")
        self.assertGreaterEqual(pos["holding_seconds"], 595)
        self.assertNotEqual(pos["entry_time"], "--")
        self.assertEqual(pos["trendline_id"], "TL_SOL")
        self.assertTrue(pos["trailing_stop_triggered"])
        self.assertEqual(pos["trailing_stop_price"], 151.5)
        self.assertEqual(pos["highest_price"], 168.0)

    def test_format_duration_edge_cases(self):
        self.assertEqual(aae.format_duration(0), "0s")
        self.assertEqual(aae.format_duration(-10), "0s")
        self.assertEqual(aae.format_duration(45), "45s")
        self.assertEqual(aae.format_duration(300), "5m")
        self.assertEqual(aae.format_duration(746), "12m 26s")
        self.assertEqual(aae.format_duration(3665), "1h 1m 5s")
        self.assertEqual(aae.format_duration(86400 * 2 + 3600 * 3 + 120), "2d 3h 2m")


class TestQuantRiskCalculations(unittest.TestCase):
    """2. 测试量化风控指标统一净盈亏口径、日历夏普与标准索提诺比率"""

    def setUp(self):
        self.engine = aae.AccountAnalyticsEngine(offline_mode=True, initial_capital=10000.0)

    def test_win_rate_and_profit_factor_standard(self):
        trades = [
            {"realized_pnl": 100.0, "fee": 1.0, "net_pnl": 99.0, "pnl_pct": 10.0, "closed_ts": 1000, "closed_at": "2026-11-20 10:00:00", "duration_seconds": 60, "symbol": "A", "close_reason": "TP"},
            {"realized_pnl": -50.0, "fee": 1.0, "net_pnl": -51.0, "pnl_pct": -5.0, "closed_ts": 2000, "closed_at": "2026-11-20 11:00:00", "duration_seconds": 120, "symbol": "B", "close_reason": "SL"},
            {"realized_pnl": 0.0, "fee": 0.0, "net_pnl": 0.0, "pnl_pct": 0.0, "closed_ts": 3000, "closed_at": "2026-11-21 09:00:00", "duration_seconds": 30, "symbol": "C", "close_reason": "MC"},
            {"realized_pnl": 150.0, "fee": 1.5, "net_pnl": 148.5, "pnl_pct": 15.0, "closed_ts": 4000, "closed_at": "2026-11-21 12:00:00", "duration_seconds": 90, "symbol": "D", "close_reason": "TP"},
        ]
        summary, _, _ = self.engine.calculate_quant_risk_metrics(trades, [], None)

        self.assertEqual(summary["closed_trades"], 4)
        self.assertEqual(summary["win_count"], 2)
        self.assertEqual(summary["loss_count"], 1)
        self.assertEqual(summary["breakeven_count"], 1)
        self.assertEqual(summary["win_rate_pct"], 50.0)
        self.assertAlmostEqual(summary["win_rate_ex_breakeven_pct"], 66.67, places=1)
        # 净盈亏口径: (99 + 148.5) / 51 = 4.85
        self.assertAlmostEqual(summary["profit_factor"], 4.85, places=2)
        self.assertEqual(summary["cumulative_realized_pnl"], 200.0)
        self.assertEqual(summary["total_fee"], 3.5)
        self.assertEqual(summary["net_realized_pnl"], 196.5)
        self.assertEqual(summary["total_equity"], 10196.5)

    def test_all_wins_returns_none_profit_factor_and_sortino(self):
        trades = [
            {"realized_pnl": 50.0, "fee": 0.1, "net_pnl": 49.9, "pnl_pct": 5.0, "closed_ts": 1000, "closed_at": "2026-11-20 10:00:00", "duration_seconds": 60, "symbol": "A", "close_reason": "TP"},
            {"realized_pnl": 80.0, "fee": 0.1, "net_pnl": 79.9, "pnl_pct": 8.0, "closed_ts": 2000, "closed_at": "2026-11-20 11:00:00", "duration_seconds": 60, "symbol": "B", "close_reason": "TP"},
        ]
        summary, _, _ = self.engine.calculate_quant_risk_metrics(trades, [], None)
        self.assertEqual(summary["win_rate_pct"], 100.0)
        self.assertIsNone(summary["profit_factor"])
        self.assertIsNone(summary["sortino_ratio"])
        self.assertEqual(summary["max_consecutive_wins"], 2)
        self.assertEqual(summary["max_consecutive_losses"], 0)

    def test_all_losses_zero_profit_factor(self):
        trades = [
            {"realized_pnl": -30.0, "fee": 0.1, "net_pnl": -30.1, "pnl_pct": -3.0, "closed_ts": 1000, "closed_at": "2026-11-20 10:00:00", "duration_seconds": 60, "symbol": "A", "close_reason": "SL"},
            {"realized_pnl": -70.0, "fee": 0.1, "net_pnl": -70.1, "pnl_pct": -7.0, "closed_ts": 2000, "closed_at": "2026-11-20 11:00:00", "duration_seconds": 60, "symbol": "B", "close_reason": "SL"},
        ]
        summary, _, _ = self.engine.calculate_quant_risk_metrics(trades, [], None)
        self.assertEqual(summary["win_rate_pct"], 0.0)
        self.assertEqual(summary["profit_factor"], 0.0)
        self.assertEqual(summary["max_consecutive_losses"], 2)
        self.assertAlmostEqual(summary["max_drawdown_usd"], 100.2, places=1)

    def test_zero_trades_empty_state_robustness(self):
        summary, eq_curve, daily_pnl = self.engine.calculate_quant_risk_metrics([], [], None)
        self.assertEqual(summary["closed_trades"], 0)
        self.assertEqual(summary["win_rate_pct"], 0.0)
        self.assertIsNone(summary["profit_factor"])
        self.assertEqual(summary["max_drawdown_usd"], 0.0)
        self.assertEqual(summary["max_drawdown_pct"], 0.0)
        self.assertIsNone(summary["sharpe_ratio"])
        self.assertIsNone(summary["sortino_ratio"])
        self.assertEqual(summary["sharpe_method"], "NONE")
        self.assertEqual(summary["total_equity"], 10000.0)
        self.assertEqual(len(daily_pnl), 0)
        self.assertGreaterEqual(len(eq_curve), 2)

    def test_zero_equity_and_zero_margin_not_coerced(self):
        account_info = {
            "totalWalletBalance": "0.0",
            "totalMarginBalance": "0.0",
            "availableBalance": "0.0",
        }
        summary, _, _ = self.engine.calculate_quant_risk_metrics([], [], account_info)
        self.assertEqual(summary["wallet_balance"], 0.0)
        self.assertEqual(summary["total_equity"], 0.0)
        self.assertEqual(summary["available_margin"], 0.0)

    def test_max_drawdown_calculation_complex(self):
        trades = [
            {"realized_pnl": 500.0, "fee": 0.0, "net_pnl": 500.0, "pnl_pct": 5.0, "closed_ts": 1000, "closed_at": "2026-11-20 10:00:00", "duration_seconds": 60, "symbol": "A", "close_reason": "TP"},
            {"realized_pnl": -300.0, "fee": 0.0, "net_pnl": -300.0, "pnl_pct": -3.0, "closed_ts": 2000, "closed_at": "2026-11-20 11:00:00", "duration_seconds": 60, "symbol": "B", "close_reason": "SL"},
            {"realized_pnl": -200.0, "fee": 0.0, "net_pnl": -200.0, "pnl_pct": -2.0, "closed_ts": 3000, "closed_at": "2026-11-20 12:00:00", "duration_seconds": 60, "symbol": "C", "close_reason": "SL"},
            {"realized_pnl": 1000.0, "fee": 0.0, "net_pnl": 1000.0, "pnl_pct": 10.0, "closed_ts": 4000, "closed_at": "2026-11-20 13:00:00", "duration_seconds": 60, "symbol": "D", "close_reason": "TP"},
        ]
        summary, _, _ = self.engine.calculate_quant_risk_metrics(trades, [], None)
        self.assertEqual(summary["max_drawdown_usd"], 500.0)
        self.assertAlmostEqual(summary["max_drawdown_pct"], 4.76, places=2)

    def test_streaks_and_durations(self):
        trades = [
            {"realized_pnl": 10.0, "fee": 0.0, "net_pnl": 10.0, "pnl_pct": 1.0, "closed_ts": 1000, "closed_at": "2026-11-20 10:00:00", "duration_seconds": 100, "symbol": "A", "close_reason": "TP"},
            {"realized_pnl": 20.0, "fee": 0.0, "net_pnl": 20.0, "pnl_pct": 2.0, "closed_ts": 2000, "closed_at": "2026-11-20 11:00:00", "duration_seconds": 200, "symbol": "B", "close_reason": "TP"},
            {"realized_pnl": 30.0, "fee": 0.0, "net_pnl": 30.0, "pnl_pct": 3.0, "closed_ts": 3000, "closed_at": "2026-11-20 12:00:00", "duration_seconds": 300, "symbol": "C", "close_reason": "TP"},
            {"realized_pnl": -5.0, "fee": 0.0, "net_pnl": -5.0, "pnl_pct": -0.5, "closed_ts": 4000, "closed_at": "2026-11-20 13:00:00", "duration_seconds": 400, "symbol": "D", "close_reason": "SL"},
        ]
        summary, _, _ = self.engine.calculate_quant_risk_metrics(trades, [], None)
        self.assertEqual(summary["max_consecutive_wins"], 3)
        self.assertEqual(summary["max_consecutive_losses"], 1)
        self.assertEqual(summary["avg_hold_duration_seconds"], 250)
        self.assertEqual(summary["avg_hold_duration_str"], "4m 10s")
        self.assertEqual(summary["min_hold_duration_seconds"], 100)
        self.assertEqual(summary["max_hold_duration_seconds"], 400)

    def test_sortino_denominator_and_calendar_gap_sharpe(self):
        # 2026-11-01 (+200 on 10000 = +0.02), 3 idle days (0.0), 2026-11-05 (-102 on 10200 = -0.01)
        trades = [
            {"realized_pnl": 200.0, "fee": 0.0, "net_pnl": 200.0, "pnl_pct": 2.0, "closed_ts": 1000, "closed_at": "2026-11-01 10:00:00", "duration_seconds": 60, "symbol": "A", "close_reason": "TP"},
            {"realized_pnl": -102.0, "fee": 0.0, "net_pnl": -102.0, "pnl_pct": -1.0, "closed_ts": 2000, "closed_at": "2026-11-05 10:00:00", "duration_seconds": 60, "symbol": "B", "close_reason": "SL"},
        ]
        summary, _, _ = self.engine.calculate_quant_risk_metrics(trades, [], None)
        rets = [0.02, 0.0, 0.0, 0.0, -0.01]
        mean_r = sum(rets) / 5
        std_r = math.sqrt(sum((r - mean_r) ** 2 for r in rets) / 4)
        down_std = math.sqrt((0.01 ** 2) / 5)  # 分母必须为总天数 5，而非亏损天数 1
        self.assertAlmostEqual(summary["sharpe_ratio"], round(mean_r / std_r * math.sqrt(365), 2), places=2)
        self.assertAlmostEqual(summary["sortino_ratio"], round(mean_r / down_std * math.sqrt(365), 2), places=2)
        self.assertEqual(summary["sharpe_method"], "DAILY_RETURNS")

    def test_zero_variance_trades_returns_none_sharpe_instead_of_magic_2_5(self):
        trades = [
            {"realized_pnl": 100.0, "fee": 0.0, "net_pnl": 100.0, "pnl_pct": 1.0, "closed_ts": 1000, "closed_at": "2026-11-20 10:00:00", "duration_seconds": 60, "symbol": "A", "close_reason": "TP"},
            {"realized_pnl": 100.0, "fee": 0.0, "net_pnl": 100.0, "pnl_pct": 1.0, "closed_ts": 2000, "closed_at": "2026-11-20 11:00:00", "duration_seconds": 60, "symbol": "B", "close_reason": "TP"},
        ]
        summary, _, _ = self.engine.calculate_quant_risk_metrics(trades, [], None)
        self.assertIsNone(summary["sharpe_ratio"])
        self.assertIsNone(summary["sortino_ratio"])


class TestDataAggregation(unittest.TestCase):
    """3. 测试资金净值时间序列与每日盈亏聚合"""

    def setUp(self):
        self.engine = aae.AccountAnalyticsEngine(offline_mode=True, initial_capital=10000.0)

    def test_equity_curve_timeseries_monotonicity_and_points(self):
        now_ms = int(time.time() * 1000)
        trades = [
            {
                "realized_pnl": 200.0,
                "fee": 2.0,
                "net_pnl": 198.0,
                "pnl_pct": 2.0,
                "entry_ts": now_ms - 7200_000,
                "closed_ts": now_ms - 5400_000,
                "closed_at": aae.get_beijing_time_str((now_ms - 5400_000) / 1000.0),
                "duration_seconds": 1800,
                "symbol": "BTCUSDT",
                "close_reason": "TP",
            },
            {
                "realized_pnl": -50.0,
                "fee": 1.0,
                "net_pnl": -51.0,
                "pnl_pct": -0.5,
                "entry_ts": now_ms - 3600_000,
                "closed_ts": now_ms - 1800_000,
                "closed_at": aae.get_beijing_time_str((now_ms - 1800_000) / 1000.0),
                "duration_seconds": 1800,
                "symbol": "ETHUSDT",
                "close_reason": "SL",
            },
        ]
        active_positions = [{"symbol": "SOLUSDT", "unrealized_pnl": 35.0, "initial_margin": 200.0}]
        summary, eq_curve, _ = self.engine.calculate_quant_risk_metrics(trades, active_positions, None)

        self.assertEqual(len(eq_curve), 4)
        self.assertEqual(eq_curve[0]["equity"], 10000.0)
        self.assertEqual(eq_curve[0]["cumulative_pnl"], 0.0)
        self.assertEqual(eq_curve[1]["equity"], 10198.0)
        self.assertEqual(eq_curve[1]["cumulative_pnl"], 198.0)
        self.assertEqual(eq_curve[2]["equity"], 10147.0)
        self.assertEqual(eq_curve[2]["drawdown_usd"], 51.0)
        self.assertEqual(eq_curve[3]["equity"], 10182.0)
        self.assertEqual(summary["open_trades"], 1)

        for i in range(1, len(eq_curve)):
            self.assertGreaterEqual(eq_curve[i]["timestamp"], eq_curve[i - 1]["timestamp"])

    def test_daily_pnl_aggregation_grouping(self):
        trades = [
            {"realized_pnl": 100.0, "fee": 1.0, "net_pnl": 99.0, "pnl_pct": 1.0, "closed_ts": 1000, "closed_at": "2026-11-20 09:00:00", "duration_seconds": 60, "symbol": "A", "close_reason": "TP"},
            {"realized_pnl": 50.0, "fee": 0.5, "net_pnl": 49.5, "pnl_pct": 0.5, "closed_ts": 2000, "closed_at": "2026-11-20 15:00:00", "duration_seconds": 60, "symbol": "B", "close_reason": "TP"},
            {"realized_pnl": -30.0, "fee": 0.3, "net_pnl": -30.3, "pnl_pct": -0.3, "closed_ts": 3000, "closed_at": "2026-11-21 10:00:00", "duration_seconds": 60, "symbol": "C", "close_reason": "SL"},
        ]
        _, _, daily_pnl = self.engine.calculate_quant_risk_metrics(trades, [], None)
        self.assertEqual(len(daily_pnl), 2)
        self.assertEqual(daily_pnl[0]["date"], "2026-11-20")
        self.assertEqual(daily_pnl[0]["trades_count"], 2)
        self.assertEqual(daily_pnl[0]["wins"], 2)
        self.assertEqual(daily_pnl[0]["net_pnl"], 148.5)
        self.assertEqual(daily_pnl[1]["date"], "2026-11-21")
        self.assertEqual(daily_pnl[1]["losses"], 1)
        self.assertEqual(daily_pnl[1]["net_pnl"], -30.3)


class TestBinanceAPIIntegrationAndFallback(unittest.TestCase):
    """4. 测试币安合约 API 请求、FIFO 交易重构、异常降级与 SQLite 增量同步"""

    def test_client_endpoints_success_mock(self):
        client = bc.BinanceFuturesClient(api_key="mock_key", api_secret="mock_sec")
        mock_acc_data = {
            "totalWalletBalance": "15420.50",
            "totalMarginBalance": "15600.80",
            "availableBalance": "14200.00",
            "totalUnrealizedProfit": "180.30",
        }
        mock_income_data = [
            {"symbol": "YBUSDT", "incomeType": "REALIZED_PNL", "income": "12.50", "time": 1795200000000},
            {"symbol": "YBUSDT", "incomeType": "COMMISSION", "income": "-0.05", "time": 1795200000000},
            {"symbol": "YBUSDT", "incomeType": "FUNDING_FEE", "income": "-0.02", "time": 1795200000000},
        ]

        def fake_send(endpoint, params=None):
            if endpoint == "/fapi/v2/account":
                return mock_acc_data, None
            if endpoint == "/fapi/v1/income":
                return mock_income_data, None
            if endpoint == "/fapi/v2/positionRisk":
                return [], None
            return None, {"code": 404, "msg": "Not Found"}

        with patch.object(client, "_send_signed_request", side_effect=fake_send):
            acc = client.get_account_info()
            self.assertEqual(acc["totalWalletBalance"], "15420.50")
            income = client.get_income()
            self.assertEqual(len(income), 3)

            engine = aae.AccountAnalyticsEngine(client=client)
            _, _, _, status = engine.fetch_binance_data()
            self.assertTrue(status["connected"])
            self.assertEqual(status["status_code"], 200)

    def test_account_report_distinguishes_empty_positions_from_request_failure(self):
        client = bc.BinanceFuturesClient("test-key", "test-secret")
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
                account_response, ([], None), (None, {"code": "NETWORK_ERROR", "msg": "err"})
            ]):
                report = engine.run(path)
            self.assertEqual(report["active_positions"][0]["symbol"], "YBUSDT")
            self.assertEqual(report["positions_source"], "LOCAL_CACHE")
            self.assertEqual(report["api_status"]["mode"], "BINANCE_PARTIAL")

    def test_full_income_page_reports_coverage_without_inferring_principal(self):
        client = bc.BinanceFuturesClient("test-key", "test-secret")
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

    def test_database_with_candidates_initializes_account_recording(self):
        client = bc.BinanceFuturesClient("test", "test")
        engine = aae.AccountAnalyticsEngine(client=client)
        status = {"connected": True, "mode": "BINANCE_REALTIME", "positions_available": True, "income_available": True}
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "account.sqlite3"
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute("CREATE TABLE candidate_snapshot (id INTEGER PRIMARY KEY, updated_at TEXT, payload TEXT)")
            with patch.object(engine, "fetch_binance_data", return_value=(
                {"totalWalletBalance": "1000", "totalMarginBalance": "1000"}, [], [], status
            )), patch.object(engine, "load_local_positions_store", return_value=([], [])), patch.object(
                client, "_send_signed_request", return_value=([], None)
            ):
                report = engine.run(database=database)
            self.assertEqual(report["recording"]["initial_equity"], 1000)
            self.assertGreater(report["recording"]["start_ms"], 0)
            self.assertEqual(report["equity_curve"][0]["cash"], 1000)

    def test_recording_sync_persists_and_deduplicates_exchange_events(self):
        from account_dashboard_api import publish_report

        client = bc.BinanceFuturesClient("test", "test")
        engine = aae.AccountAnalyticsEngine(client=client)
        start = 1795200000000
        previous = {
            "recording": {"start_ms": start, "started_at": "2026-11-21 00:00:00", "initial_equity": 1000},
            "equity_curve": [{"timestamp": start, "equity": 1000, "drawdown_usd": 0, "drawdown_pct": 0}],
        }
        income = [
            {"incomeType": "TRANSFER", "tranId": 1, "time": start + 1, "income": "100", "asset": "USDT", "symbol": ""},
            {"incomeType": "REALIZED_PNL", "tranId": 2, "tradeId": "5", "time": start + 2, "income": "10", "asset": "USDT", "symbol": "BTCUSDT"},
            {"incomeType": "COMMISSION", "tranId": 3, "tradeId": "5", "time": start + 2, "income": "-0.2", "asset": "USDT", "symbol": "BTCUSDT"},
        ]
        fill = {"symbol": "BTCUSDT", "id": 5, "orderId": 1, "side": "SELL", "time": start + 2, "positionSide": "LONG", "price": "110", "qty": "1", "quoteQty": "110"}
        status = {"connected": True, "mode": "BINANCE_REALTIME", "positions_available": True, "income_available": True}
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "account.sqlite3"
            publish_report(database, previous)
            with patch.object(engine, "fetch_binance_data", return_value=(
                {"totalWalletBalance": "1110", "totalMarginBalance": "1110"}, income, [], status
            )), patch.object(engine, "load_local_positions_store", return_value=([], [])), patch.object(
                client, "_send_signed_request", side_effect=lambda endpoint, params: (
                    income if endpoint == "/fapi/v1/income" else [fill], None
                )
            ):
                report = engine.run(database=database)
                engine.run(database=database)
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM income_ledger").fetchone()[0], 3)
                self.assertEqual(connection.execute("SELECT count(*) FROM trade_fills").fetchone()[0], 1)
                # 5 分钟桶内重复快照就地刷新，避免无界增长
                self.assertEqual(connection.execute("SELECT count(*) FROM equity_observations").fetchone()[0], 2)
                self.assertEqual(connection.execute("SELECT count(*) FROM account_recording").fetchone()[0], 1)
            self.assertEqual(report["trade_history"][0]["exit_price"], 110)
            self.assertEqual(report["trade_history"][0]["fee"], 0.2)
            self.assertEqual(report["trade_history"][0]["net_pnl"], 9.8)
            self.assertEqual(report["summary"]["total_fee"], 0.2)
            self.assertEqual(report["daily_pnl"][0]["net_pnl"], 9.8)
            self.assertEqual(report["summary"]["net_external_transfers"], 100)
            self.assertAlmostEqual(report["summary"]["total_return_pct"], 0.91, places=2)
            self.assertEqual(report["equity_curve"][0]["equity"], 1000)
            self.assertEqual(report["equity_curve"][-1]["cash"], 1110)
            self.assertEqual(report["equity_curve"][-1]["cumulative_pnl"], 10)
            self.assertEqual(report["equity_curve"][-1]["drawdown_usd"], 0)

    def test_multiple_fills_form_one_closed_order(self):
        from account_dashboard_api import publish_report

        client = bc.BinanceFuturesClient("test", "test")
        engine = aae.AccountAnalyticsEngine(client=client)
        start = 1795200000000
        previous = {"recording": {"start_ms": start, "started_at": "2026-11-21 00:00:00", "initial_equity": 1000}, "equity_curve": []}
        income = [
            {"incomeType": "REALIZED_PNL", "tranId": 1, "tradeId": "2", "time": start + 4000, "income": "10", "asset": "USDT", "symbol": "BTCUSDT"},
            {"incomeType": "REALIZED_PNL", "tranId": 2, "tradeId": "3", "time": start + 5000, "income": "20", "asset": "USDT", "symbol": "BTCUSDT"},
        ]
        fills = [
            {"symbol": "BTCUSDT", "id": 1, "orderId": 1, "side": "BUY", "time": start + 1000, "positionSide": "LONG", "price": "100", "qty": "3", "quoteQty": "300"},
            {"symbol": "BTCUSDT", "id": 2, "orderId": 2, "side": "SELL", "time": start + 4000, "positionSide": "LONG", "price": "110", "qty": "1", "quoteQty": "110"},
            {"symbol": "BTCUSDT", "id": 3, "orderId": 2, "side": "SELL", "time": start + 5000, "positionSide": "LONG", "price": "110", "qty": "2", "quoteQty": "220"},
        ]
        status = {"connected": True, "mode": "BINANCE_REALTIME", "positions_available": True, "income_available": True}
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "account.sqlite3"
            publish_report(database, previous)
            with patch.object(
                engine,
                "fetch_binance_data",
                return_value=({"totalWalletBalance": "1030", "totalMarginBalance": "1030"}, income, [], status),
            ), patch.object(engine, "load_local_positions_store", return_value=([], [])), patch.object(
                client,
                "_send_signed_request",
                side_effect=lambda endpoint, params: (income if endpoint == "/fapi/v1/income" else fills, None),
            ):
                report = engine.run(database=database)
        self.assertEqual(report["summary"]["closed_trades"], 1)
        trade = report["trade_history"][0]
        self.assertEqual(trade["amount"], 3)
        self.assertEqual(trade["entry_price"], 100)
        self.assertEqual(trade["exit_price"], 110)
        self.assertEqual(trade["net_pnl"], 30)
        self.assertEqual(trade["duration_seconds"], 4)

    def test_multi_roundtrip_partial_close_short_and_bnb_fee_isolation_in_db_mode(self):
        from account_dashboard_api import publish_report

        client = bc.BinanceFuturesClient("test", "test")
        engine = aae.AccountAnalyticsEngine(client=client)
        day1 = 1795000000000  # 2026-11-18
        day2 = day1 + 86400_000
        previous = {"recording": {"start_ms": day1, "started_at": "2026-11-18 00:00:00", "initial_equity": 1000}, "equity_curve": []}
        income = [
            # Roundtrip 1: BTCUSDT LONG open 2 @100 (fee 0.2 USDT), partial close 1 @110 (fee 0.1), partial close 1 @120 (fee 0.1)
            {"incomeType": "COMMISSION", "tranId": 1, "tradeId": "1", "time": day1 + 1000, "income": "-0.2", "asset": "USDT", "symbol": "BTCUSDT"},
            {"incomeType": "REALIZED_PNL", "tranId": 2, "tradeId": "2", "time": day1 + 2000, "income": "10", "asset": "USDT", "symbol": "BTCUSDT"},
            {"incomeType": "COMMISSION", "tranId": 3, "tradeId": "2", "time": day1 + 2000, "income": "-0.1", "asset": "USDT", "symbol": "BTCUSDT"},
            {"incomeType": "REALIZED_PNL", "tranId": 4, "tradeId": "3", "time": day1 + 4000, "income": "20", "asset": "USDT", "symbol": "BTCUSDT"},
            {"incomeType": "COMMISSION", "tranId": 5, "tradeId": "3", "time": day1 + 4000, "income": "-0.1", "asset": "USDT", "symbol": "BTCUSDT"},
            # Roundtrip 2: BTCUSDT LONG open 4 @130, close 4 @125 (BNB fee must NOT pollute USDT fee)
            {"incomeType": "COMMISSION", "tranId": 6, "tradeId": "4", "time": day2 + 1000, "income": "-0.002", "asset": "BNB", "symbol": "BTCUSDT"},
            {"incomeType": "REALIZED_PNL", "tranId": 7, "tradeId": "5", "time": day2 + 5000, "income": "-20", "asset": "USDT", "symbol": "BTCUSDT"},
            {"incomeType": "COMMISSION", "tranId": 8, "tradeId": "5", "time": day2 + 5000, "income": "-0.002", "asset": "BNB", "symbol": "BTCUSDT"},
            # Roundtrip 3: ETHUSDT SHORT open 1 @3000 (fee 0.3), close 1 @2900 (fee 0.3), plus funding +1.4
            {"incomeType": "COMMISSION", "tranId": 9, "tradeId": "11", "time": day2 + 10000, "income": "-0.3", "asset": "USDT", "symbol": "ETHUSDT"},
            {"incomeType": "REALIZED_PNL", "tranId": 10, "tradeId": "12", "time": day2 + 16000, "income": "100", "asset": "USDT", "symbol": "ETHUSDT"},
            {"incomeType": "COMMISSION", "tranId": 11, "tradeId": "12", "time": day2 + 16000, "income": "-0.3", "asset": "USDT", "symbol": "ETHUSDT"},
            {"incomeType": "FUNDING_FEE", "tranId": 12, "time": day2 + 18000, "income": "1.4", "asset": "USDT", "symbol": "ETHUSDT"},
        ]
        btc_fills = [
            {"symbol": "BTCUSDT", "id": 1, "orderId": 1, "side": "BUY", "time": day1 + 1000, "positionSide": "LONG", "price": "100", "qty": "2", "quoteQty": "200"},
            {"symbol": "BTCUSDT", "id": 2, "orderId": 2, "side": "SELL", "time": day1 + 2000, "positionSide": "LONG", "price": "110", "qty": "1", "quoteQty": "110"},
            {"symbol": "BTCUSDT", "id": 3, "orderId": 3, "side": "SELL", "time": day1 + 4000, "positionSide": "LONG", "price": "120", "qty": "1", "quoteQty": "120"},
            {"symbol": "BTCUSDT", "id": 4, "orderId": 4, "side": "BUY", "time": day2 + 1000, "positionSide": "LONG", "price": "130", "qty": "4", "quoteQty": "520"},
            {"symbol": "BTCUSDT", "id": 5, "orderId": 5, "side": "SELL", "time": day2 + 5000, "positionSide": "LONG", "price": "125", "qty": "4", "quoteQty": "500"},
        ]
        eth_fills = [
            {"symbol": "ETHUSDT", "id": 11, "orderId": 11, "side": "SELL", "time": day2 + 10000, "positionSide": "SHORT", "price": "3000", "qty": "1", "quoteQty": "3000"},
            {"symbol": "ETHUSDT", "id": 12, "orderId": 12, "side": "BUY", "time": day2 + 16000, "positionSide": "SHORT", "price": "2900", "qty": "1", "quoteQty": "2900"},
        ]
        status = {"connected": True, "mode": "BINANCE_REALTIME", "positions_available": True, "income_available": True}

        def fake_signed(endpoint, params=None):
            if endpoint == "/fapi/v1/income":
                return income, None
            sym = (params or {}).get("symbol")
            return (btc_fills if sym == "BTCUSDT" else eth_fills), None

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "account.sqlite3"
            publish_report(database, previous)
            with patch.object(
                engine,
                "fetch_binance_data",
                return_value=({"totalWalletBalance": "1110.4", "totalMarginBalance": "1110.4"}, [], [], status),
            ), patch.object(engine, "load_local_positions_store", return_value=([], [])), patch.object(
                client, "_send_signed_request", side_effect=fake_signed
            ):
                report = engine.run(database=database)

        trades = report["trade_history"]
        self.assertEqual(len(trades), 4)
        # 1st & 2nd partial closes split opening fee (0.1 each) + closing fee (0.1 each) = 0.2 each
        self.assertAlmostEqual(trades[0]["entry_price"], 100.0)
        self.assertAlmostEqual(trades[0]["fee"], 0.2)
        self.assertEqual(trades[0]["duration_seconds"], 1)
        self.assertAlmostEqual(trades[1]["entry_price"], 100.0)
        self.assertAlmostEqual(trades[1]["fee"], 0.2)
        self.assertEqual(trades[1]["duration_seconds"], 3)
        # 2nd BTCUSDT roundtrip
        self.assertAlmostEqual(trades[2]["entry_price"], 130.0)
        self.assertAlmostEqual(trades[2]["exit_price"], 125.0)
        self.assertEqual(trades[2]["fee"], 0.0)
        self.assertEqual(trades[2]["non_usdt_fees"], {"BNB": 0.004})
        self.assertEqual(trades[2]["duration_seconds"], 4)
        # ETHUSDT SHORT
        self.assertEqual(trades[3]["side"], "SHORT")
        self.assertAlmostEqual(trades[3]["entry_price"], 3000.0)
        self.assertAlmostEqual(trades[3]["exit_price"], 2900.0)
        self.assertAlmostEqual(trades[3]["fee"], 0.6)
        self.assertAlmostEqual(trades[3]["net_pnl"], 99.4)
        self.assertEqual(trades[3]["duration_seconds"], 6)

        self.assertAlmostEqual(sum(t["fee"] for t in trades), report["summary"]["total_fee"], places=4)
        self.assertAlmostEqual(report["summary"]["total_funding_fee"], 1.4, places=2)
        self.assertEqual(report["income_breakdown"]["non_usdt_fees"], {"BNB": 0.004})
        # Sharpe must reflect post-DB daily_pnl (not stale None)
        self.assertIsNotNone(report["summary"]["sharpe_ratio"])

    def test_reconstruct_binance_trades_interleaved_symbols_and_bnb_fees(self):
        engine = aae.AccountAnalyticsEngine(offline_mode=True)
        income = [
            {"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": "10", "tradeId": "1", "time": 1000, "asset": "USDT"},
            {"symbol": "ETHUSDT", "incomeType": "REALIZED_PNL", "income": "-5", "tradeId": "2", "time": 1200, "asset": "USDT"},
            {"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": "15", "tradeId": "3", "time": 1500, "asset": "USDT"},
            {"symbol": "BTCUSDT", "incomeType": "COMMISSION", "income": "-0.001", "tradeId": "1", "time": 1000, "asset": "BNB"},
        ]
        trades, open_fee = engine.reconstruct_binance_trades(income, [])
        self.assertEqual(open_fee, 0.0)
        self.assertEqual(len(trades), 2)
        btc_trade = [t for t in trades if t["symbol"] == "BTCUSDT"][0]
        self.assertEqual(btc_trade["realized_pnl"], 25.0)
        self.assertEqual(btc_trade["fee"], 0.0)
        self.assertEqual(btc_trade["non_usdt_fees"], {"BNB": 0.001})

    def test_client_http_451_graceful_fallback(self):
        client = bc.BinanceFuturesClient(api_key="mock_key", api_secret="mock_sec")
        with patch.object(
            client,
            "_send_signed_request",
            return_value=(None, {"code": 451, "msg": "Service unavailable from a restricted location"}),
        ):
            engine = aae.AccountAnalyticsEngine(client=client)
            acc_info, _, _, status = engine.fetch_binance_data()
            self.assertFalse(status["connected"])
            self.assertEqual(status["status_code"], 451)
            self.assertEqual(status["mode"], "OFFLINE_FALLBACK")
            self.assertIsNone(acc_info)

    def test_client_network_timeout_graceful_fallback(self):
        client = bc.BinanceFuturesClient(api_key="mock_key", api_secret="mock_sec")
        with patch.object(client, "_send_signed_request", side_effect=TimeoutError("Connection timed out")):
            engine = aae.AccountAnalyticsEngine(client=client)
            _, _, _, status = engine.fetch_binance_data()
            self.assertFalse(status["connected"])
            self.assertEqual(status["status_code"], -1)
            self.assertEqual(status["mode"], "OFFLINE_FALLBACK")
            self.assertIn("timed out", status["message"])

    def test_offline_mode_flag_bypasses_network(self):
        mock_client = MagicMock()
        engine = aae.AccountAnalyticsEngine(client=mock_client, offline_mode=True)
        _, _, _, status = engine.fetch_binance_data()
        self.assertFalse(status["connected"])
        self.assertEqual(status["mode"], "OFFLINE_FALLBACK")
        mock_client.get_account_info.assert_not_called()

    def test_corrupted_positions_file_raises_loudly(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            corrupt_path = Path(tmp_dir) / "corrupt.json"
            corrupt_path.write_text("CORRUPTED_JSON_DATA {{{}}", encoding="utf-8")
            engine = aae.AccountAnalyticsEngine(positions_path=corrupt_path, offline_mode=True)
            with self.assertRaises(json.JSONDecodeError):
                engine.load_local_positions_store()
            with self.assertRaises(json.JSONDecodeError):
                engine.run(output_path=Path(tmp_dir) / "out.json")

    def test_missing_positions_file_handling(self):
        missing_path = Path("/tmp/definitely_non_existent_file_12345.json")
        engine = aae.AccountAnalyticsEngine(positions_path=missing_path, offline_mode=True)
        pos, hist = engine.load_local_positions_store()
        self.assertEqual(pos, [])
        self.assertEqual(hist, [])


class TestAccountHTMLGenerationAndIntegrity(unittest.TestCase):
    """5. 测试纯静态 web/account.html 外壳与 DOM 契约完整性"""

    def setUp(self):
        self.html_path = ROOT_DIR / "web" / "account.html"
        self.html = self.html_path.read_text(encoding="utf-8")

    def test_static_account_html_exists_and_compact_size(self):
        self.assertTrue(self.html_path.exists())
        size = self.html_path.stat().st_size
        self.assertGreater(size, 5000)
        self.assertLess(size, 35000)

    def test_html_is_pure_static_shell_without_inline_assets_or_data(self):
        self.assertIn('href="static/styles.css"', self.html)
        self.assertIn('src="static/account.js"', self.html)
        self.assertIn("chart.js", self.html.lower())
        self.assertNotIn("<style>", self.html)
        self.assertNotIn('id="account-data"', self.html)
        self.assertNotIn("顶级稳健", self.html)
        self.assertNotIn("$10,000", self.html)

    def test_html_contains_all_kpi_and_submetric_ids(self):
        expected_ids = [
            "sync-time-text",
            "api-status-badge",
            "btn-manual-refresh",
            "refresh-spinner",
            "account-load-status",
            "account-report",
            "kpi-matrix-section",
            "kpi-total-equity",
            "kpi-initial-capital",
            "kpi-total-return",
            "kpi-available-margin",
            "kpi-position-margin",
            "kpi-margin-badge",
            "kpi-net-realized-pnl",
            "kpi-total-fee",
            "kpi-funding-fee",
            "kpi-unrealized-pnl",
            "kpi-open-trades",
            "kpi-win-rate",
            "kpi-wl-record",
            "kpi-total-trades",
            "kpi-profit-factor",
            "kpi-avg-win",
            "kpi-avg-loss",
            "kpi-max-dd-pct",
            "kpi-max-dd-usd",
            "kpi-sharpe",
            "kpi-sharpe-rating",
            "kpi-sortino",
            "kpi-sortino-rating",
            "kpi-max-streak",
            "kpi-avg-hold",
        ]
        for dom_id in expected_ids:
            self.assertIn(f'id="{dom_id}"', self.html, f"account.html 缺少元素: {dom_id}")

    def test_html_contains_all_chart_containers_and_badges(self):
        expected_ids = [
            "chart-equity-curve",
            "chart-daily-pnl",
            "chart-win-loss",
            "chart-pnl-dist",
            "equity-range-label",
            "equity-peak-label",
            "equity-current-label",
            "daily-days-badge",
            "win-loss-summary-badge",
            "dist-summary-badge",
        ]
        for dom_id in expected_ids:
            self.assertIn(f'id="{dom_id}"', self.html, f"account.html 缺少图表或标签: {dom_id}")

    def test_html_contains_active_positions_9_cols_and_history_controls(self):
        for dom_id in (
            "active-positions-panel",
            "active-pos-count-badge",
            "short-position-warning",
            "active-positions-empty",
            "active-positions-table-wrap",
            "active-positions-tbody",
            "trade-history-table",
            "history-tbody",
            "history-search-input",
            "history-outcome-filter",
            "history-count-badge",
            "btn-reset-history-filter",
        ):
            self.assertIn(f'id="{dom_id}"', self.html)

        pos_section = self.html.split('id="active-positions-table-wrap"')[1].split("</table>")[0]
        self.assertEqual(len(re.findall(r"<th\b", pos_section)), 9)

        for sort_col in ("closed_ts", "symbol", "net_pnl", "pnl_pct"):
            self.assertIn(f'data-sort="{sort_col}"', self.html)

    def test_html_global_nav_header_links(self):
        self.assertIn('href="index.html"', self.html)
        self.assertIn('href="account.html"', self.html)
        self.assertIn('href="playbook.html"', self.html)
        self.assertIn("账户数据分析", self.html)


class TestFullEngineExecution(unittest.TestCase):
    """6. 测试引擎端到端执行、文件输出与原子化写入"""

    def test_run_generates_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_file = Path(tmp_dir) / "account_analytics.json"
            engine = aae.AccountAnalyticsEngine(offline_mode=True, initial_capital=10000.0)
            engine.run(output_path=out_file)

            self.assertTrue(out_file.exists())
            with open(out_file, "r", encoding="utf-8") as f:
                saved_data = json.load(f)

            self.assertEqual(saved_data["version"], 1)
            for key in ("summary", "equity_curve", "daily_pnl", "trade_history", "active_positions", "income_breakdown"):
                self.assertIn(key, saved_data)

            s = saved_data["summary"]
            for field in (
                "initial_capital",
                "total_equity",
                "wallet_balance",
                "available_margin",
                "position_margin",
                "net_realized_pnl",
                "total_unrealized_pnl",
                "total_funding_fee",
                "total_fee",
                "total_return_pct",
                "open_trades",
                "closed_trades",
                "win_count",
                "loss_count",
                "breakeven_count",
                "win_rate_pct",
                "profit_factor",
                "avg_win",
                "avg_loss",
                "max_drawdown_pct",
                "max_drawdown_usd",
                "sharpe_ratio",
                "sortino_ratio",
                "sharpe_method",
                "max_consecutive_wins",
                "max_consecutive_losses",
                "avg_hold_duration_str",
            ):
                self.assertIn(field, s)

    def test_atomic_write_json_atomicity(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "data.json"
            aae.atomic_write_json(target, {"status": "ok", "step": 1})
            self.assertTrue(target.exists())
            with open(target, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.assertEqual(d["status"], "ok")


if __name__ == "__main__":
    unittest.main()
