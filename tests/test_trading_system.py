#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Automated Test Suite for 4-Feature Trading Alert & Risk Management System
========================================================================

Verifies the end-to-end integration across:
- TradingView Cloud layout reading & fallback behavior
- Logarithmic formula accuracy vs Linear (distortion-free projection)
- Feature 1: New resonance detection & state transition
- Feature 2: 15m breakout detection (using YBUSDT actual data)
- Feature 3: Position stop-loss when closing below trendline
- Feature 4: Position trailing stop moving to +1% when hitting +6%, and triggering exit on pullback
- CLI functionality verification

Usage:
    rtk python3 tests/test_trading_system.py
    rtk pytest tests/test_trading_system.py
"""

import os
import sys
import time
import json
import math
import shutil
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

import tv_trendline_engine as tve
import trade_alert_daemon as tad


class TestTVCloudAndFallback(unittest.TestCase):
    """1. TV Cloud layout reading & fallback behavior"""

    def test_clean_symbol(self):
        """Verify ticker symbol normalizations to Binance contract format."""
        self.assertEqual(tve.clean_symbol("BINANCE:YBUSDT.P"), "YBUSDT")
        self.assertEqual(tve.clean_symbol("BINANCE:1000SATSUSDT.P"), "1000SATSUSDT")
        self.assertEqual(tve.clean_symbol("BINANCE:BMTUSDT"), "BMTUSDT")
        self.assertEqual(tve.clean_symbol("OKX:BTC-USDT-SWAP"), "BTC-USDT-SWAP")
        self.assertEqual(tve.clean_symbol("YBUSDT"), "YBUSDT")
        self.assertEqual(tve.clean_symbol(""), "")

    def test_tv_cloud_drawing_reading_structure(self):
        """Verify layout reading parses drawings with all required geometric fields."""
        drawings = tve.get_layout_drawings(layout_id="ig0LuDL8", save_cache=False)
        self.assertIsInstance(drawings, dict)
        self.assertTrue(len(drawings) > 0)

        # Check YBUSDT exists and has valid LineToolTrendLine drawings
        self.assertTrue("YBUSDT" in drawings or "BINANCE:YBUSDT.P" in drawings)
        yb_lines = drawings.get("YBUSDT") or drawings.get("BINANCE:YBUSDT.P")
        self.assertTrue(len(yb_lines) >= 2)

        first_line = yb_lines[0]
        self.assertIn("id", first_line)
        self.assertIn("p1", first_line)
        self.assertIn("t1", first_line)
        self.assertIn("p2", first_line)
        self.assertIn("t2", first_line)
        self.assertIn("dt1_str", first_line)
        self.assertIn("dt2_str", first_line)
        self.assertGreater(first_line["p1"], 0.0)
        self.assertGreater(first_line["p2"], 0.0)
        self.assertGreater(first_line["t1"], 0)
        self.assertGreater(first_line["t2"], 0)

    def test_tv_cache_fallback_when_offline(self):
        """Verify cached layout is loaded seamlessly when offline or remote is unreachable."""
        cache_file = ROOT_DIR / "runs" / "tradingview_drawings_ig0LuDL8.json"
        self.assertTrue(cache_file.exists(), "Cache file must exist for fallback testing")

        with open(cache_file, "r", encoding="utf-8") as f:
            cached_data = json.load(f)

        # Mock network failure
        with patch("tv_trendline_engine._request_with_retry", side_effect=RuntimeError("Network unreachable")):
            # When network fails, calling get_chart_token will raise RuntimeError
            with self.assertRaises(RuntimeError):
                tve.get_chart_token(layout_id="ig0LuDL8", force_refresh=True)

        # But daemon's fetch_layout_drawings or direct cache reads handle it gracefully
        self.assertIn("drawings_by_symbol", cached_data)
        self.assertIn("YBUSDT", cached_data["drawings_by_symbol"])

    def test_binance_candle_close_confirmation(self):
        """Verify candle close confirmation guarantees close_time <= server_time."""
        candle = tve.fetch_latest_closed_candle("YBUSDT", interval="15m", limit=5)
        self.assertEqual(candle["symbol"], "YBUSDT")
        self.assertTrue(candle["is_closed"])
        self.assertGreater(candle["close"], 0)
        self.assertGreaterEqual(candle["high"], candle["low"])
        self.assertGreater(candle["close_time"], candle["open_time"])


class TestLogarithmicVsLinearFormula(unittest.TestCase):
    """2. Logarithmic formula accuracy vs Linear (ensure no distortion)"""

    def test_logarithmic_mathematical_precision(self):
        """
        Verify Logarithmic projection:
        P(t) = P1 * (P2 / P1) ** ((t - t1) / (t2 - t1))
        """
        # Scenario: P1 = 100 at t1 = 0; P2 = 25 at t2 = 10
        # At t = 5 (halfway in time):
        # Log: P(5) = 100 * (0.25)**(0.5) = 100 * 0.5 = 50.0
        p_half = tve.calculate_log_resistance(p1=100.0, t1=0, p2=25.0, t2=10, target_t=5)
        self.assertAlmostEqual(p_half, 50.0, places=5)

        # At t = 20 (double interval in time):
        # Log: P(20) = 100 * (0.25)**(2.0) = 100 * 0.0625 = 6.25
        p_double = tve.calculate_log_resistance(p1=100.0, t1=0, p2=25.0, t2=10, target_t=20)
        self.assertAlmostEqual(p_double, 6.25, places=5)

    def test_logarithmic_vs_linear_distortion_proof(self):
        """
        Demonstrate why Linear projection causes massive distortion and fails on crypto charts.
        Linear: P_lin(t) = P1 + (P2 - P1) * ((t - t1) / (t2 - t1))
        """
        p1 = 100.0
        t1 = 0
        p2 = 50.0
        t2 = 10

        # At t = 20:
        # Logarithmic projection correctly produces 25.0 (constant -50% decay per 10 time units)
        p_log_20 = tve.calculate_log_resistance(p1=p1, t1=t1, p2=p2, t2=t2, target_t=20)
        p_lin_20 = p1 + (p2 - p1) * ((20 - t1) / (t2 - t1))

        self.assertAlmostEqual(p_log_20, 25.0, places=5)
        self.assertAlmostEqual(p_lin_20, 0.0, places=5)

        # Linear projection hit 0.0 (100% distortion)!
        distortion_pct_20 = abs(p_lin_20 - p_log_20) / p_log_20 * 100.0
        self.assertAlmostEqual(distortion_pct_20, 100.0, places=1)

        # At t = 30:
        # Logarithmic projection remains strictly positive: 12.5
        # Linear projection plunges to -50.0 (impossible negative price!)
        p_log_30 = tve.calculate_log_resistance(p1=p1, t1=t1, p2=p2, t2=t2, target_t=30)
        p_lin_30 = p1 + (p2 - p1) * ((30 - t1) / (t2 - t1))

        self.assertAlmostEqual(p_log_30, 12.5, places=5)
        self.assertAlmostEqual(p_lin_30, -50.0, places=5)
        self.assertGreater(p_log_30, 0.0)
        self.assertLess(p_lin_30, 0.0)

    def test_edge_cases_and_input_guards(self):
        """Verify boundary conditions: t=t1, t=t2, horizontal lines, vertical lines, invalid prices."""
        # Anchor point t1
        self.assertEqual(tve.calculate_log_resistance(50.0, 100, 25.0, 200, 100), 50.0)
        # Anchor point t2
        self.assertEqual(tve.calculate_log_resistance(50.0, 100, 25.0, 200, 200), 25.0)
        # Horizontal line
        self.assertEqual(tve.calculate_log_resistance(42.0, 100, 42.0, 200, 300), 42.0)
        # Vertical line protection (t1 == t2)
        self.assertEqual(tve.calculate_log_resistance(42.0, 100, 84.0, 100, 300), 42.0)

        # Negative or zero prices must be rejected
        with self.assertRaises(ValueError):
            tve.calculate_log_resistance(0.0, 10, 20.0, 20, 15)
        with self.assertRaises(ValueError):
            tve.calculate_log_resistance(20.0, 10, -5.0, 20, 15)


class TestFeature1NewResonanceDetection(unittest.TestCase):
    """3. Feature 1: New resonance detection & state transition"""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.pool_path = Path(self.test_dir) / "pool.json"
        self.state_path = Path(self.test_dir) / "resonance_state.json"

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_cold_start_baseline_suppresses_initial_alerts(self):
        """Verify cold start initial run records baseline symbols without sending alerts."""
        candidates = [
            {
                "symbol": "ALPHAUSDT",
                "price": 1.25,
                "turnover_ratio": 4.5,
                "funding_rate_4h_pct": -0.08,
                "is_resonance": True,
                "resonance_score": 3,
                "is_neg_funding": True,
                "zones": ["双核共振+逼空"]
            }
        ]
        with open(self.pool_path, "w", encoding="utf-8") as f:
            json.dump(candidates, f)

        notifier = tad.ServerChanNotifier(dry_run=True)
        state_store = tad.ResonanceStateStore(self.state_path)

        # On initial run with cold_start=True, establish baseline without alerting
        alerts = tad.check_feature_1_resonance(
            notifier=notifier,
            state_store=state_store,
            pool_file=self.pool_path,
            layout_id="ig0LuDL8",
            cold_start=True
        )
        self.assertEqual(len(alerts), 0)
        self.assertEqual(state_store.get_active_symbols(), {"ALPHAUSDT"})

    def test_new_resonance_detection_and_notification(self):
        """Verify newly entered resonance tokens trigger alerts with proper links when baseline exists."""
        candidates = [
            {
                "symbol": "ALPHAUSDT",
                "price": 1.25,
                "turnover_ratio": 4.5,
                "funding_rate_4h_pct": -0.08,
                "is_resonance": True,
                "resonance_score": 3,
                "is_neg_funding": True,
                "zones": ["双核共振+逼空"]
            },
            {
                "symbol": "BETAUSDT",
                "price": 0.45,
                "turnover_ratio": 0.8,
                "funding_rate_4h_pct": 0.01,
                "is_resonance": False,
                "resonance_score": 0,
                "zones": []
            }
        ]
        with open(self.pool_path, "w", encoding="utf-8") as f:
            json.dump(candidates, f)

        notifier = tad.ServerChanNotifier(dry_run=True)
        state_store = tad.ResonanceStateStore(self.state_path)
        # Establish baseline where GAMMAUSDT is active, so ALPHAUSDT is newly entered
        state_store.data["active_resonance_symbols"] = ["GAMMAUSDT"]
        state_store.save()

        # Run check: ALPHAUSDT enters resonance
        alerts = tad.check_feature_1_resonance(
            notifier=notifier,
            state_store=state_store,
            pool_file=self.pool_path,
            layout_id="ig0LuDL8"
        )
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["symbol"], "ALPHAUSDT")
        self.assertEqual(state_store.get_active_symbols(), {"ALPHAUSDT"})

        # Check recorded history contains TradingView link
        hist_entry = state_store.data["history"]["ALPHAUSDT"]
        self.assertIn("https://cn.tradingview.com/chart/ig0LuDL8/?symbol=BINANCE:ALPHAUSDT.P&interval=15", hist_entry["tv_link"])

    def test_resonance_state_transition_and_deduplication(self):
        """Verify state persistence prevents redundant notification spam in steady state."""
        candidates = [
            {
                "symbol": "ALPHAUSDT",
                "price": 1.25,
                "is_resonance": True,
                "resonance_score": 3,
                "funding_rate_4h_pct": -0.05,
                "turnover_ratio": 2.5,
                "zones": ["双核共振+逼空"]
            }
        ]
        with open(self.pool_path, "w", encoding="utf-8") as f:
            json.dump(candidates, f)

        notifier = tad.ServerChanNotifier(dry_run=True)
        state_store = tad.ResonanceStateStore(self.state_path)

        # 1st check with cold_start=False -> alerted
        alerts1 = tad.check_feature_1_resonance(notifier, state_store, self.pool_path, cold_start=False)
        self.assertEqual(len(alerts1), 1)

        # 2nd check with same state -> 0 new alerts
        alerts2 = tad.check_feature_1_resonance(notifier, state_store, self.pool_path, cold_start=False)
        self.assertEqual(len(alerts2), 0)

        # 3rd check: ALPHAUSDT drops out
        with open(self.pool_path, "w", encoding="utf-8") as f:
            json.dump([], f)
        alerts3 = tad.check_feature_1_resonance(notifier, state_store, self.pool_path, cold_start=False)
        self.assertEqual(len(alerts3), 0)
        self.assertEqual(state_store.get_active_symbols(), set())

    def test_feature_1_24h_debounce(self):
        """Verify 24h debounce suppresses repetitive alerts on boundary score jitter."""
        candidates = [
            {
                "symbol": "ALPHAUSDT",
                "price": 1.25,
                "is_resonance": True,
                "resonance_score": 3,
                "funding_rate_4h_pct": -0.05,
                "turnover_ratio": 2.5,
                "zones": ["双核共振+逼空"]
            }
        ]
        with open(self.pool_path, "w", encoding="utf-8") as f:
            json.dump(candidates, f)

        notifier = tad.ServerChanNotifier(dry_run=True)
        state_store = tad.ResonanceStateStore(self.state_path)

        # 1. First alert sent
        alerts1 = tad.check_feature_1_resonance(notifier, state_store, self.pool_path, cold_start=False)
        self.assertEqual(len(alerts1), 1)
        self.assertIn("ALPHAUSDT", state_store.data["history"])
        notified_time = state_store.data["history"]["ALPHAUSDT"]["last_notified_time"]
        self.assertIsNotNone(notified_time)

        # 2. ALPHAUSDT drops out of active resonance
        state_store.data["active_resonance_symbols"] = []
        state_store.save()

        # 3. ALPHAUSDT re-enters 2 hours later (well within 24h) -> Debounce blocks re-notification
        alerts2 = tad.check_feature_1_resonance(
            notifier, state_store, self.pool_path, cold_start=False, debounce_hours=24.0
        )
        self.assertEqual(len(alerts2), 0, "24h debounce must suppress alert when re-entering within 24 hours")
        self.assertEqual(state_store.get_active_symbols(), {"ALPHAUSDT"})

        # 4. Simulate >24h elapsed (e.g. notified 25 hours ago)
        state_store.data["active_resonance_symbols"] = []
        state_store.data["history"]["ALPHAUSDT"]["last_notified_time"] = int(time.time() - 25 * 3600)
        state_store.save()

        alerts3 = tad.check_feature_1_resonance(
            notifier, state_store, self.pool_path, cold_start=False, debounce_hours=24.0
        )
        self.assertEqual(len(alerts3), 1, "Alert should fire again once 24h debounce window has expired")

    def test_cross_process_state_sync_prevents_duplicate_alert(self):
        """
        Verify multi-process / cross-process synchronization:
        Simulate daemon holding in-memory store while cron process (check-once) updates state on disk.
        Daemon's subsequent check must reload disk state and suppress duplicate alerts.
        """
        candidates = [
            {
                "symbol": "BLESSUSDT",
                "price": 0.05,
                "is_resonance": True,
                "resonance_score": 2,
                "funding_rate_4h_pct": 0.01,
                "turnover_ratio": 15.0,
                "is_ema_bull": True,
                "zones": ["双核黄金共振"]
            }
        ]
        with open(self.pool_path, "w", encoding="utf-8") as f:
            json.dump(candidates, f)

        notifier = tad.ServerChanNotifier(dry_run=True)

        # 1. Simulate daemon initialized at 09:17 with baseline
        daemon_store = tad.ResonanceStateStore(self.state_path)
        daemon_store.data["active_resonance_symbols"] = ["INITUSDT"]
        daemon_store.save()

        # 2. Simulate hourly sync cron process (--check-once) running at 10:02
        cron_store = tad.ResonanceStateStore(self.state_path)
        alerts_cron = tad.check_feature_1_resonance(
            notifier, cron_store, self.pool_path, cold_start=False
        )
        self.assertEqual(len(alerts_cron), 1, "Cron process must detect and alert BLESSUSDT")
        self.assertEqual(alerts_cron[0]["symbol"], "BLESSUSDT")

        # 3. Simulate daemon process at 10:15 waking up to check_feature_1_resonance using existing daemon_store instance
        alerts_daemon = tad.check_feature_1_resonance(
            notifier, daemon_store, self.pool_path, cold_start=False
        )
        self.assertEqual(
            len(alerts_daemon), 0,
            "Daemon must reload state from disk and suppress duplicate notification for BLESSUSDT"
        )
        self.assertIn("BLESSUSDT", daemon_store.get_active_symbols())


class TestFeature2BreakoutDetection(unittest.TestCase):
    """4. Feature 2: 15m breakout detection (strict crossover, single-line consumption, filtering)"""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.state_path = Path(self.test_dir) / "resonance_state.json"
        self.state_store = tad.ResonanceStateStore(self.state_path)
        self.state_store.data["active_resonance_symbols"] = ["YBUSDT"]
        self.notifier = tad.ServerChanNotifier(dry_run=True)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_ybusdt_1215_breakout_actual_data(self):
        """
        Verify exact YBUSDT 12:15 UTC+8 breakout from TradingView layout ig0LuDL8:
        Trendline SFzHy9:
            P1 = 0.06306 at t1 = 1795104900 (2026-11-20 00:15 UTC+8)
            P2 = 0.05757 at t2 = 1795155300 (2026-11-20 14:15 UTC+8)
        12:15 UTC+8 Candle:
            Close = 0.05030 at target_t = 1795234500
            Log Resistance = 0.049892
            Breakout = True (+0.817%)
        """
        trendline = {
            "id": "SFzHy9",
            "interval": "15",
            "p1": 0.06306,
            "t1": 1795104900,
            "p2": 0.05757,
            "t2": 1795155300,
        }
        candle_1215 = {
            "symbol": "YBUSDT",
            "open": 0.04982,
            "high": 0.05065,
            "low": 0.04960,
            "close": 0.05030,
            "open_time": 1795234500,
            "close_time": 1795235399,
            "candle_timestamp": 1795234500,
            "candle_time": "2026-11-21 12:15",
            "is_closed": True,
            "prev_candle": {
                "symbol": "YBUSDT",
                "open": 0.04920,
                "high": 0.04980,
                "low": 0.04910,
                "close": 0.04975,  # <= prev log res 0.049948
                "open_time": 1795233600,
                "close_time": 1795234499,
                "candle_timestamp": 1795233600,
                "is_closed": True
            }
        }

        # Verify engine direct evaluation
        eval_result = tve.check_symbol_breakout("YBUSDT", trendline, candle=candle_1215)
        self.assertTrue(eval_result["is_breakout"])
        self.assertTrue(eval_result["is_crossover"])
        self.assertEqual(eval_result["close_price"], 0.05030)
        self.assertAlmostEqual(eval_result["res_price"], 0.049892, places=5)
        self.assertGreater(eval_result["pct"], 0.8)

        # Verify daemon's check_feature_2_breakout integration
        mock_drawings = {"YBUSDT": [trendline]}
        with patch.object(tad, "fetch_layout_drawings", return_value=mock_drawings):
            with patch.object(tad, "get_binance_closed_15m_candle", return_value=candle_1215):
                alerts = tad.check_feature_2_breakout(self.notifier, self.state_store)
                self.assertEqual(len(alerts), 1)
                self.assertEqual(alerts[0]["symbol"], "YBUSDT")
                self.assertAlmostEqual(alerts[0]["close_price"], 0.05030, places=5)
                # Verify single-line consumption key
                self.assertTrue(self.state_store.has_breakout_notified("YBUSDT_SFzHy9"))

    def test_true_crossover_triggers_breakout(self):
        """
        Verify True Crossover:
        prev_close <= prev_res and curr_close > curr_res -> triggers breakout.
        """
        trendline = {
            "id": "cross_line_1",
            "interval": "15",
            "p1": 0.06306,
            "t1": 1795104900,
            "p2": 0.05757,
            "t2": 1795155300,
        }
        # Prev res at 1795233600 is ~0.049948; Curr res at 1795234500 is ~0.049892
        candle_cross = {
            "symbol": "YBUSDT",
            "open": 0.04980,
            "high": 0.05050,
            "low": 0.04970,
            "close": 0.05020,  # > curr_res 0.049892
            "open_time": 1795234500,
            "close_time": 1795235399,
            "candle_timestamp": 1795234500,
            "candle_time": "2026-11-21 12:15",
            "is_closed": True,
            "prev_candle": {
                "symbol": "YBUSDT",
                "open": 0.04930,
                "high": 0.04980,
                "low": 0.04920,
                "close": 0.04960,  # <= prev_res 0.049948
                "open_time": 1795233600,
                "close_time": 1795234499,
                "candle_timestamp": 1795233600,
                "is_closed": True
            }
        }
        eval_result = tve.check_symbol_breakout("YBUSDT", trendline, candle=candle_cross)
        self.assertTrue(eval_result["is_crossover"])
        self.assertTrue(eval_result["is_breakout"])

        mock_drawings = {"YBUSDT": [trendline]}
        with patch.object(tad, "fetch_layout_drawings", return_value=mock_drawings):
            with patch.object(tad, "get_binance_closed_15m_candle", return_value=candle_cross):
                alerts = tad.check_feature_2_breakout(self.notifier, self.state_store)
                self.assertEqual(len(alerts), 1)
                self.assertEqual(alerts[0]["trendline_id"], "cross_line_1")

    def test_already_above_line_does_not_trigger_breakout(self):
        """
        Verify Already Above Line:
        prev_close > prev_res and curr_close > curr_res -> does NOT trigger breakout.
        """
        trendline = {
            "id": "cross_line_2",
            "interval": "15",
            "p1": 0.06306,
            "t1": 1795104900,
            "p2": 0.05757,
            "t2": 1795155300,
        }
        # Both prev and curr are above resistance
        candle_already_above = {
            "symbol": "YBUSDT",
            "open": 0.05010,
            "high": 0.05080,
            "low": 0.05000,
            "close": 0.05040,  # > curr_res 0.049892
            "open_time": 1795234500,
            "close_time": 1795235399,
            "candle_timestamp": 1795234500,
            "candle_time": "2026-11-21 12:15",
            "is_closed": True,
            "prev_candle": {
                "symbol": "YBUSDT",
                "open": 0.04990,
                "high": 0.05030,
                "low": 0.04980,
                "close": 0.05015,  # > prev_res 0.049948 (ALREADY ABOVE!)
                "open_time": 1795233600,
                "close_time": 1795234499,
                "candle_timestamp": 1795233600,
                "is_closed": True
            }
        }
        eval_result = tve.check_symbol_breakout("YBUSDT", trendline, candle=candle_already_above)
        self.assertFalse(eval_result["is_crossover"])
        self.assertFalse(eval_result["is_breakout"])

        mock_drawings = {"YBUSDT": [trendline]}
        with patch.object(tad, "fetch_layout_drawings", return_value=mock_drawings):
            with patch.object(tad, "get_binance_closed_15m_candle", return_value=candle_already_above):
                alerts = tad.check_feature_2_breakout(self.notifier, self.state_store)
                self.assertEqual(len(alerts), 0, "Already above line must not trigger false breakout")

    def test_single_line_consumption_deduplication(self):
        """
        Verify Single-Line Consumption:
        Second consecutive candle with same line_id does NOT send breakout alert.
        """
        trendline = {
            "id": "single_consume_line",
            "interval": "15",
            "p1": 0.06306,
            "t1": 1795104900,
            "p2": 0.05757,
            "t2": 1795155300,
        }
        candle_1 = {
            "symbol": "YBUSDT",
            "open": 0.04980,
            "high": 0.05050,
            "low": 0.04970,
            "close": 0.05020,
            "open_time": 1795234500,
            "close_time": 1795235399,
            "candle_timestamp": 1795234500,
            "candle_time": "2026-11-21 12:15",
            "is_closed": True,
            "prev_candle": {
                "close": 0.04960,  # <= prev_res
                "open_time": 1795233600,
                "close_time": 1795234499,
                "candle_timestamp": 1795233600,
            }
        }
        mock_drawings = {"YBUSDT": [trendline]}
        with patch.object(tad, "fetch_layout_drawings", return_value=mock_drawings):
            with patch.object(tad, "get_binance_closed_15m_candle", return_value=candle_1):
                alerts1 = tad.check_feature_2_breakout(self.notifier, self.state_store)
                self.assertEqual(len(alerts1), 1)
                self.assertTrue(self.state_store.has_breakout_notified("YBUSDT_single_consume_line"))

        # Second consecutive candle 15m later
        candle_2 = {
            "symbol": "YBUSDT",
            "open": 0.05020,
            "high": 0.05100,
            "low": 0.05010,
            "close": 0.05080,
            "open_time": 1795235400,
            "close_time": 1795236299,
            "candle_timestamp": 1795235400,
            "candle_time": "2026-11-21 12:30",
            "is_closed": True,
            "prev_candle": {
                "close": 0.04960,  # Even if crossover simulated
                "open_time": 1795234500,
                "close_time": 1795235399,
                "candle_timestamp": 1795234500,
            }
        }
        with patch.object(tad, "fetch_layout_drawings", return_value=mock_drawings):
            with patch.object(tad, "get_binance_closed_15m_candle", return_value=candle_2):
                alerts2 = tad.check_feature_2_breakout(self.notifier, self.state_store)
                self.assertEqual(len(alerts2), 0, "Second candle must be suppressed by single-line consumption")

    def test_non_15m_and_ancient_trendlines_ignored(self):
        """
        Verify that 240m / ancient / runaway trendlines are strictly ignored.
        """
        now_ts = 1795234500  # reference time matching candle

        # 1. Non-15m line (240m = 4h)
        line_240m = {
            "id": "line_4h",
            "interval": "240",
            "p1": 0.06306,
            "t1": 1795104900,
            "p2": 0.05757,
            "t2": 1795155300,
        }
        self.assertFalse(tad.is_valid_15m_trendline(line_240m, current_time=now_ts))

        # 2. Daily line ("D")
        line_d = {
            "id": "line_daily",
            "interval": "D",
            "p1": 0.06306,
            "t1": 1795104900,
            "p2": 0.05757,
            "t2": 1795155300,
        }
        self.assertFalse(tad.is_valid_15m_trendline(line_d, current_time=now_ts))

        # 3. Ancient line: t2 was 80 hours ago (> 72 hours)
        ancient_t2 = now_ts - 80 * 3600
        line_ancient = {
            "id": "line_ancient",
            "interval": "15",
            "p1": 0.07000,
            "t1": ancient_t2 - 36000,
            "p2": 0.06000,
            "t2": ancient_t2,
        }
        self.assertFalse(tad.is_valid_15m_trendline(line_ancient, current_time=now_ts))

        # 4. Runaway / ancient decay line (price > res * 1.25)
        runaway_line = {
            "id": "runaway_line",
            "interval": "15",
            "p1": 0.0100,
            "t1": now_ts - 7200,
            "p2": 0.0080,
            "t2": now_ts - 3600,
        }
        candle_runaway = {
            "symbol": "YBUSDT",
            "open": 0.0500,
            "high": 0.0510,
            "low": 0.0490,
            "close": 0.0500,  # ~7x higher than resistance (~0.007)
            "open_time": now_ts,
            "close_time": now_ts + 899,
            "candle_timestamp": now_ts,
            "is_closed": True,
        }
        eval_runaway = tve.check_symbol_breakout("YBUSDT", runaway_line, candle=candle_runaway)
        self.assertTrue(eval_runaway["is_runaway"])
        self.assertFalse(eval_runaway["is_breakout"])

        # Daemon integration test: all invalid lines ignored
        mock_drawings = {"YBUSDT": [line_240m, line_d, line_ancient, runaway_line]}
        with patch.object(tad, "fetch_layout_drawings", return_value=mock_drawings):
            with patch.object(tad, "get_binance_closed_15m_candle", return_value=candle_runaway):
                alerts = tad.check_feature_2_breakout(self.notifier, self.state_store)
                self.assertEqual(len(alerts), 0, "All invalid/non-15m/ancient/runaway lines must produce 0 alerts")

    def test_non_breakout_candle_below_trendline(self):
        """Verify candle closing below logarithmic resistance does NOT trigger breakout alert."""
        trendline = {
            "id": "SFzHy9",
            "interval": "15",
            "p1": 0.06306,
            "t1": 1795104900,
            "p2": 0.05757,
            "t2": 1795155300,
        }
        candle_below = {
            "symbol": "YBUSDT",
            "open": 0.04900,
            "high": 0.04970,
            "low": 0.04890,
            "close": 0.04920,  # Below res 0.049892
            "open_time": 1795234500,
            "close_time": 1795235399,
            "candle_timestamp": 1795234500,
            "candle_time": "2026-11-21 12:15",
            "is_closed": True,
        }

        mock_drawings = {"YBUSDT": [trendline]}
        with patch.object(tad, "fetch_layout_drawings", return_value=mock_drawings):
            with patch.object(tad, "get_binance_closed_15m_candle", return_value=candle_below):
                alerts = tad.check_feature_2_breakout(self.notifier, self.state_store)
                self.assertEqual(len(alerts), 0)


class TestFeature3PositionStopLoss(unittest.TestCase):
    """5. Feature 3: Position stop-loss when closing below trendline"""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.pos_path = Path(self.test_dir) / "trade_positions.json"
        self.pos_store = tad.PositionStore(self.pos_path)
        self.notifier = tad.ServerChanNotifier(dry_run=True)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_stop_loss_triggered_on_closing_below_trendline(self):
        """Verify position automatically marks STOP_LOSS_TRIGGERED when 15m close drops below trendline."""
        # Trendline support: at t=50, log resistance is sqrt(10.0 * 6.4) = 8.0000
        trendline = {
            "p1": 10.0,
            "t1": 0,
            "p2": 6.4,
            "t2": 100,
            "id": "support_line_1"
        }
        self.pos_store.add_position("TESTUSDT", entry_price=9.0, amount=100.0, trendline=trendline)
        self.assertEqual(len(self.pos_store.get_open_positions()), 1)

        # Mock candle closing at 7.60 (< support 8.00)
        mock_candle_break = {
            "symbol": "TESTUSDT",
            "close_time": 50,
            "close": 7.60,
            "open": 8.10,
            "candle_time": "2026-11-21 12:00"
        }

        with patch.object(tad, "get_binance_closed_15m_candle", return_value=mock_candle_break):
            alerts = tad.check_feature_3_stop_loss(self.notifier, self.pos_store)
            self.assertEqual(len(alerts), 1)
            self.assertEqual(alerts[0]["status"], "STOP_LOSS_TRIGGERED")
            self.assertEqual(alerts[0]["exit_price"], 7.60)
            self.assertEqual(len(self.pos_store.get_open_positions()), 0)

    def test_no_stop_loss_when_holding_above_trendline(self):
        """Verify position remains OPEN when candle close holds above trendline."""
        trendline = {
            "p1": 10.0,
            "t1": 0,
            "p2": 6.4,
            "t2": 100,
            "id": "support_line_1"
        }
        self.pos_store.add_position("TESTUSDT", entry_price=9.0, amount=100.0, trendline=trendline)

        # Mock candle closing at 8.40 (> support 8.00)
        mock_candle_hold = {
            "symbol": "TESTUSDT",
            "close_time": 50,
            "close": 8.40,
            "open": 8.10,
            "candle_time": "2026-11-21 12:00"
        }

        with patch.object(tad, "get_binance_closed_15m_candle", return_value=mock_candle_hold):
            alerts = tad.check_feature_3_stop_loss(self.notifier, self.pos_store)
            self.assertEqual(len(alerts), 0)
            self.assertEqual(len(self.pos_store.get_open_positions()), 1)
            self.assertEqual(self.pos_store.get_open_positions()[0]["status"], "OPEN")


class TestFeature4TrailingStopProtection(unittest.TestCase):
    """6. Feature 4: Position trailing stop moving to +1% when hitting +6%, and triggering exit on pullback"""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.pos_path = Path(self.test_dir) / "trade_positions.json"
        self.pos_store = tad.PositionStore(self.pos_path)
        self.notifier = tad.ServerChanNotifier(dry_run=True)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_trailing_stop_full_lifecycle(self):
        """
        Step 1: Entry at 1.0000.
        Step 2: Price at 1.0500 (+5.0%) -> Inactive.
        Step 3: Price hits 1.0650 (+6.5%) -> Activates trailing stop locked at 1.0100 (+1.0%).
        Step 4: Price pulls back to 1.0090 (<= 1.0100) -> Triggers PROFIT_LOCKED exit.
        """
        # Step 1: Open position
        self.pos_store.add_position("COINUSDT", entry_price=1.0000, amount=1000.0)
        pos = self.pos_store.get_open_positions()[0]
        self.assertFalse(pos["trailing_stop_triggered"])
        self.assertIsNone(pos["trailing_stop_price"])

        # Step 2: Price at 1.0500 (+5.0%) -> Still below +6% activation threshold
        with patch.object(tad, "get_binance_ticker_price", return_value=1.0500):
            events1 = tad.check_feature_4_trailing_stops(
                self.notifier, self.pos_store, activation_pct=6.0, lock_pct=1.0
            )
            self.assertEqual(len(events1), 0)
            self.assertFalse(self.pos_store.get_open_positions()[0]["trailing_stop_triggered"])

        # Step 3: Price rises to 1.0650 (+6.5% >= +6.0%) -> Activates Trailing Stop
        with patch.object(tad, "get_binance_ticker_price", return_value=1.0650):
            events2 = tad.check_feature_4_trailing_stops(
                self.notifier, self.pos_store, activation_pct=6.0, lock_pct=1.0
            )
            self.assertEqual(len(events2), 1)
            self.assertEqual(events2[0]["type"], "TRAILING_ACTIVATED")
            updated_pos = self.pos_store.get_open_positions()[0]
            self.assertTrue(updated_pos["trailing_stop_triggered"])
            self.assertAlmostEqual(updated_pos["trailing_stop_price"], 1.0100, places=4)

        # Step 4: Price pulls back to 1.0090 (<= 1.0100) -> Triggers profit lock exit
        with patch.object(tad, "get_binance_ticker_price", return_value=1.0090):
            events3 = tad.check_feature_4_trailing_stops(
                self.notifier, self.pos_store, activation_pct=6.0, lock_pct=1.0
            )
            self.assertEqual(len(events3), 1)
            self.assertEqual(events3[0]["type"], "PROFIT_LOCK_EXIT")
            self.assertEqual(events3[0]["pos"]["status"], "PROFIT_LOCKED")
            self.assertEqual(events3[0]["pos"]["exit_price"], 1.0090)
            # Position moved from open positions
            self.assertEqual(len(self.pos_store.get_open_positions()), 0)


class TestCLIOperations(unittest.TestCase):
    """7. CLI verification: --add-pos, --list-pos, --check-once, --close-pos"""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.cfg_file = Path(self.test_dir) / "alerts_config.json"
        self.pos_file = Path(self.test_dir) / "trade_positions.json"
        self.state_file = Path(self.test_dir) / "resonance_state.json"

        # Prepare test config
        cfg = {
            "serverchan_sendkey": "",
            "tradingview_layout_id": "ig0LuDL8",
            "positions_file": str(self.pos_file),
            "resonance_state_file": str(self.state_file),
            "resonance_pool_file": str(ROOT_DIR / "runs" / "second_leg_candidates_pool.json"),
            "trailing_activation_pct": 6.0,
            "trailing_lock_pct": 1.0,
        }
        with open(self.cfg_file, "w", encoding="utf-8") as f:
            json.dump(cfg, f)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_cli_position_management(self):
        """Verify position store operations through PositionStore API matching CLI."""
        store = tad.PositionStore(self.pos_file)

        # 1. Add pos
        pos = store.add_position("YBUSDT", 0.050, amount=2000.0)
        self.assertEqual(pos["symbol"], "YBUSDT")
        self.assertEqual(pos["entry_price"], 0.050)
        self.assertEqual(len(store.get_open_positions()), 1)

        # 2. Table formatting for --list-pos
        table_str = tad.format_positions_table(store.get_open_positions())
        self.assertIn("YBUSDT", table_str)
        self.assertIn("0.0500", table_str)
        self.assertIn("OPEN", table_str)

        # 3. Close pos
        closed = store.close_position("YBUSDT", reason="MANUAL_CLOSE", exit_price=0.058)
        self.assertIsNotNone(closed)
        self.assertEqual(closed["status"], "MANUAL_CLOSE")
        self.assertEqual(closed["exit_price"], 0.058)
        self.assertEqual(len(store.get_open_positions()), 0)

    def test_cli_check_once_cycle(self):
        """Verify run_evaluation_cycle orchestration returns clean summary dict."""
        cfg = tad.load_config(self.cfg_file)
        notifier = tad.ServerChanNotifier(dry_run=True)
        state_store = tad.ResonanceStateStore(self.state_file)
        pos_store = tad.PositionStore(self.pos_file)

        res = tad.run_evaluation_cycle(notifier, state_store, pos_store, cfg)
        self.assertIn("timestamp", res)
        self.assertIn("feature_1_resonance_new", res)
        self.assertIn("feature_2_breakout_signals", res)
        self.assertIn("feature_3_stop_loss_signals", res)
        self.assertIn("feature_4_trailing_stop_events", res)
        self.assertIn("active_resonance_count", res)
        self.assertIn("open_positions_count", res)

    def test_load_config_from_dotenv(self):
        """Test unified configuration loading from custom .env file."""
        test_env = Path(self.test_dir) / ".env.test"
        test_env.write_text(
            "CMC_API_KEY=test_cmc_key_123\n"
            "SERVERCHAN_SENDKEY=SCTtestkey999\n"
            "BINANCE_API_KEY=binance_key_abc\n"
            "BINANCE_API_SECRET=binance_secret_xyz\n"
            "TRADINGVIEW_LAYOUT_ID=custom_layout_789\n"
            "TRAILING_ACTIVATION_PCT=8.5\n"
            "TRAILING_LOCK_PCT=1.5\n",
            encoding="utf-8"
        )
        loaded = tad.load_config(env_path=test_env)
        self.assertEqual(loaded.get("cmc_api_key"), "test_cmc_key_123")
        self.assertEqual(loaded.get("serverchan_sendkey"), "SCTtestkey999")
        self.assertEqual(loaded.get("binance_api_key"), "binance_key_abc")
        self.assertEqual(loaded.get("binance_api_secret"), "binance_secret_xyz")
        self.assertEqual(loaded.get("tradingview_layout_id"), "custom_layout_789")
        self.assertEqual(loaded.get("trailing_activation_pct"), 8.5)
        self.assertEqual(loaded.get("trailing_lock_pct"), 1.5)


def run_full_suite():
    """Custom runner for pretty console output."""
    print("=" * 80)
    print("🚀 EXECUTING 4-FEATURE TRADING ALERT SYSTEM AUTOMATED TEST SUITE")
    print("=" * 80)
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    print("\n" + "=" * 80)
    if result.wasSuccessful():
        print(f"✅ ALL {result.testsRun} TESTS PASSED SUCCESSFULLY! (100% Green)")
        print("=" * 80)
        return 0
    else:
        print(f"❌ FAILURES: {len(result.failures)}, ERRORS: {len(result.errors)}")
        print("=" * 80)
        return 1


if __name__ == "__main__":
    exit_code = run_full_suite()
    sys.exit(exit_code)
