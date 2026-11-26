#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Automated Test Suite for 4-Feature Trading Alert & Risk Management System
"""

import io
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import alert_risk_checks as arc
import binance_client as bc
import config as cfg
import db_store
import http_client
import tv_trendline_engine as tve
import trade_alert_daemon as tad


class TestTVCloudAndFallback(unittest.TestCase):
    """1. TV Cloud layout reading, fail-closed caching, and kline confirmation"""

    def test_clean_symbol(self):
        self.assertEqual(cfg.clean_symbol("BINANCE:YBUSDT.P"), "YBUSDT")
        self.assertEqual(cfg.clean_symbol("BINANCE:1000SATSUSDT.P"), "1000SATSUSDT")
        self.assertEqual(cfg.clean_symbol("BINANCE:BMTUSDT"), "BMTUSDT")
        self.assertEqual(cfg.clean_symbol("OKX:BTC-USDT-SWAP"), "BTC-USDT-SWAP")
        self.assertEqual(cfg.clean_symbol("YBUSDT"), "YBUSDT")
        self.assertEqual(cfg.clean_symbol(""), "")

    def test_tv_cloud_drawing_reading_canonical_structure(self):
        sources = {
            f"line_{i}": {
                "symbol": "BINANCE:YBUSDT.P",
                "state": {
                    "type": "LineToolTrendLine",
                    "points": [
                        {"time_t": 1795200000 + i * 900, "price": 1.2},
                        {"time_t": 1795210000 + i * 900, "price": 1.0},
                    ],
                    "state": {"interval": "15", "linecolor": "#ff0000", "linewidth": 2},
                },
            }
            for i in range(2)
        }
        with patch.object(tve, "get_chart_token", return_value="test-token"), patch.object(
            tve, "get_layout_chart_ids", return_value=["test-chart"]
        ), patch.object(tve, "request_json", return_value={"payload": {"sources": sources}}):
            drawings = tve.get_layout_drawings(layout_id="test-layout", save_cache=False)

        self.assertEqual(list(drawings.keys()), ["YBUSDT"])
        yb_lines = drawings["YBUSDT"]
        self.assertEqual(len(yb_lines), 2)
        first_line = yb_lines[0]
        self.assertEqual(first_line["interval"], "15")
        self.assertNotIn("points", first_line)
        self.assertNotIn("state", first_line)
        self.assertGreater(first_line["p1"], 0.0)
        self.assertGreater(first_line["p2"], 0.0)

    def test_tv_drawings_fail_closed_preserves_cache_and_chart_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            runs_dir = Path(directory)
            cache_file = runs_dir / "tradingview_drawings_test-layout.json"
            original = {"drawings_by_symbol": {"YBUSDT": [{"id": "KEEP"}]}}
            cache_file.write_text(json.dumps(original), encoding="utf-8")

            with patch.object(tve, "RUNS_DIR", runs_dir), patch.object(
                tve, "get_chart_token", return_value="tok"
            ), patch.object(tve, "get_layout_chart_ids", return_value=["_shared"]), patch.object(
                tve, "request_json", side_effect=http_client.HttpError("down", status_code=503)
            ):
                with self.assertRaises(RuntimeError):
                    tve.get_layout_drawings("test-layout", save_cache=True)

            self.assertEqual(json.loads(cache_file.read_text(encoding="utf-8")), original)

        with patch.object(tve, "request_bytes", return_value=b"<html>no initData</html>"):
            with self.assertRaises(RuntimeError):
                tve.get_layout_chart_ids("any-layout")

    def test_tv_cache_fallback_when_offline(self):
        layout_id = "test-layout"
        cached_drawings = {"YBUSDT": [{"id": "L1", "interval": "15", "p1": 1.2, "t1": 100, "p2": 1.0, "t2": 200}]}
        with tempfile.TemporaryDirectory() as directory:
            runs_dir = Path(directory)
            cache_file = runs_dir / f"tradingview_drawings_{layout_id}.json"
            cache_file.write_text(json.dumps({"drawings_by_symbol": cached_drawings}), encoding="utf-8")
            with patch.object(arc, "RUNS_DIR", runs_dir), patch.object(
                arc, "_DRAWINGS_CACHE", {"data": None, "layout_id": "", "ts": 0.0}
            ), patch.object(tve, "get_layout_drawings", side_effect=AssertionError("unexpected request")) as fetch_fn:
                self.assertEqual(arc.fetch_layout_drawings(layout_id), cached_drawings)
                fetch_fn.assert_not_called()

    def test_binance_candle_close_confirmation_and_unclosed_guard(self):
        klines = [
            [1795200000000, "1.0", "1.2", "0.9", "1.1", "100", 1795200899999, "100", 10],
            [1795200900000, "1.1", "1.3", "1.0", "1.2", "100", 1795201799999, "100", 10],
        ]
        client = bc.BinanceFuturesClient()
        with patch.object(bc, "fetch_json_with_retry", side_effect=[{"serverTime": 1795201000000}, klines]):
            candle = client.get_closed_15m_candle("YBUSDT", interval="15m", limit=5)
        self.assertEqual(candle["close"], 1.1)
        self.assertEqual(candle["symbol"], "YBUSDT")
        self.assertTrue(candle["is_closed"])
        self.assertNotIn("candle_timestamp", candle)
        self.assertGreater(candle["close_time"], candle["open_time"])

        single_unclosed = [[1795200900000, "1.1", "1.3", "1.0", "1.2", "100", 1795201799999, "100", 10]]
        with patch.object(client, "get_server_time", return_value=1795201000000), patch.object(
            client, "fetch_klines", return_value=single_unclosed
        ):
            with self.assertRaises(bc.BinanceAPIError):
                client.get_closed_15m_candle("YBUSDT")

    def test_tv_drawings_handles_malformed_points_and_none_linewidth(self):
        sources = {
            "bad_pts": {
                "symbol": "BINANCE:YBUSDT.P",
                "state": {
                    "type": "LineToolTrendLine",
                    "points": [None, {"time_t": 1795210000, "price": 1.0}],
                    "state": {"interval": "15"},
                },
            },
            "none_lw": {
                "symbol": "BINANCE:YBUSDT.P",
                "state": {
                    "type": "LineToolTrendLine",
                    "points": [
                        {"time_t": 1795200000, "price": 1.2},
                        {"time_t": 1795210000, "price": 1.0},
                    ],
                    "state": {"interval": "15", "linewidth": None},
                },
            },
        }
        with patch.object(tve, "get_chart_token", return_value="tok"), patch.object(
            tve, "get_layout_chart_ids", return_value=["_shared"]
        ), patch.object(tve, "request_json", return_value={"payload": {"sources": sources}}):
            drawings = tve.get_layout_drawings(layout_id="test-layout", save_cache=False)

        self.assertEqual(len(drawings["YBUSDT"]), 1)
        self.assertEqual(drawings["YBUSDT"][0]["id"], "none_lw")
        self.assertEqual(drawings["YBUSDT"][0]["line_width"], 1)

    def test_tv_drawings_normalizes_right_to_left_anchors_and_updates_cache_when_trendlines_deleted(self):
        rtl_sources = {
            "rtl_desc": {
                "symbol": "BINANCE:YBUSDT.P",
                "state": {
                    "type": "LineToolTrendLine",
                    "points": [
                        {"time_t": 1795210000, "price": 1.0},
                        {"time_t": 1795200000, "price": 1.2},
                    ],
                    "state": {"interval": "15"},
                },
            },
            "bad_zero": {
                "symbol": "BINANCE:YBUSDT.P",
                "state": {
                    "type": "LineToolTrendLine",
                    "points": [
                        {"time_t": 1795200000, "price": 0.0},
                        {"time_t": 1795210000, "price": 1.0},
                    ],
                    "state": {"interval": "15"},
                },
            },
            "bad_same_t": {
                "symbol": "BINANCE:YBUSDT.P",
                "state": {
                    "type": "LineToolTrendLine",
                    "points": [
                        {"time_t": 1795200000, "price": 1.2},
                        {"time_t": 1795200000, "price": 1.0},
                    ],
                    "state": {"interval": "15"},
                },
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            runs_dir = Path(directory)
            cache_file = runs_dir / "tradingview_drawings_test-layout.json"
            with patch.object(tve, "RUNS_DIR", runs_dir), patch.object(
                tve, "get_chart_token", return_value="tok"
            ), patch.object(tve, "get_layout_chart_ids", return_value=["_shared"]), patch.object(
                tve, "request_json", return_value={"payload": {"sources": rtl_sources}}
            ):
                drawings = tve.get_layout_drawings("test-layout", save_cache=True)

            self.assertEqual(len(drawings["YBUSDT"]), 1)
            norm = drawings["YBUSDT"][0]
            self.assertEqual(norm["t1"], 1795200000)
            self.assertEqual(norm["p1"], 1.2)
            self.assertEqual(norm["t2"], 1795210000)
            self.assertEqual(norm["p2"], 1.0)
            self.assertLessEqual(norm["dt1_str"], norm["dt2_str"])
            self.assertIn("YBUSDT", json.loads(cache_file.read_text(encoding="utf-8"))["drawings_by_symbol"])

            # User deletes all trendlines while keeping other drawing objects -> cache overwritten with {}
            other_only_sources = {
                "hline_1": {"symbol": "BINANCE:YBUSDT.P", "state": {"type": "LineToolHorzLine", "points": []}}
            }
            with patch.object(tve, "RUNS_DIR", runs_dir), patch.object(
                tve, "get_chart_token", return_value="tok"
            ), patch.object(tve, "get_layout_chart_ids", return_value=["_shared"]), patch.object(
                tve, "request_json", return_value={"payload": {"sources": other_only_sources}}
            ):
                drawings_after = tve.get_layout_drawings("test-layout", save_cache=True)

            self.assertEqual(drawings_after, {})
            self.assertEqual(json.loads(cache_file.read_text(encoding="utf-8"))["drawings_by_symbol"], {})

    def test_fetch_layout_drawings_uses_disk_mtime_and_respects_ttl(self):
        layout_id = "ttl-layout"
        stale_drawings = {"YBUSDT": [{"id": "OLD", "interval": "15", "p1": 1.2, "t1": 100, "p2": 1.0, "t2": 200}]}
        fresh_drawings = {"YBUSDT": [{"id": "NEW", "interval": "15", "p1": 1.3, "t1": 100, "p2": 1.1, "t2": 200}]}
        with tempfile.TemporaryDirectory() as directory:
            runs_dir = Path(directory)
            cache_file = runs_dir / f"tradingview_drawings_{layout_id}.json"
            cache_file.write_text(json.dumps({"drawings_by_symbol": stale_drawings}), encoding="utf-8")
            base_now = 1800000000.0
            file_mtime = base_now - 50.0  # 50s old (< 60s ttl)
            os.utime(cache_file, (file_mtime, file_mtime))

            with patch.object(arc, "RUNS_DIR", runs_dir), patch.object(
                arc, "_DRAWINGS_CACHE", {"data": None, "layout_id": "", "ts": 0.0}
            ), patch.object(tve, "get_layout_drawings", return_value=fresh_drawings) as fetch_fn:
                with patch("time.time", return_value=base_now):
                    res1 = arc.fetch_layout_drawings(layout_id, ttl_sec=60)
                    self.assertEqual(res1, stale_drawings)
                    self.assertAlmostEqual(arc._DRAWINGS_CACHE["ts"], file_mtime, places=3)
                    fetch_fn.assert_not_called()

                # 15s later -> total age from file_mtime is 65s >= 60s -> must refresh
                with patch("time.time", return_value=base_now + 15.0):
                    res2 = arc.fetch_layout_drawings(layout_id, ttl_sec=60)
                    self.assertEqual(res2, fresh_drawings)
                    fetch_fn.assert_called_once_with(layout_id)


class TestSharedCandidateSnapshot(unittest.TestCase):
    def test_resonance_check_reads_database_candidate_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "account.sqlite3"
            store = tad.ResonanceStateStore(database)
            notifier = tad.ServerChanNotifier(dry_run=True)
            batch = [{"symbol": "TESTUSDT", "is_resonance": True}]
            db_store.publish_candidates(database, batch, updated_at="2026-11-23 00:00:00+08:00")
            tad.check_feature_1_resonance(notifier, store, database_path=database)
            self.assertEqual(store.get_active_symbols(), {"TESTUSDT"})

    def test_resonance_check_reads_dotenv_database_and_handles_missing_table(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "account.sqlite3"
            env_file = root / ".env"
            env_file.write_text(f"COIN_DATABASE={database}\n", encoding="utf-8")
            with closing(cfg.open_sqlite_connection(database, readonly=False)) as conn:
                conn.execute("CREATE TABLE other_table (id INTEGER PRIMARY KEY)")
                conn.commit()
            with patch.dict(os.environ, clear=True):
                loaded = tad.load_config(env_path=env_file)
            self.assertEqual(loaded["database_path"], database)

            store = tad.ResonanceStateStore(database)
            notifier = tad.ServerChanNotifier(dry_run=True)
            alerts = tad.check_feature_1_resonance(
                notifier, store, database_path=loaded["database_path"]
            )
            self.assertEqual(alerts, [])


class TestLogarithmicVsLinearFormula(unittest.TestCase):
    """2. Logarithmic formula precision, guards, and micro-cap preservation"""

    def test_logarithmic_mathematical_precision(self):
        p_half = tve.calculate_log_resistance(p1=100.0, t1=0, p2=25.0, t2=10, target_t=5)
        self.assertAlmostEqual(p_half, 50.0, places=5)

        p_double = tve.calculate_log_resistance(p1=100.0, t1=0, p2=25.0, t2=10, target_t=20)
        self.assertAlmostEqual(p_double, 6.25, places=5)

    def test_logarithmic_vs_linear_distortion_proof(self):
        p1, t1, p2, t2 = 100.0, 0, 50.0, 10
        p_log_20 = tve.calculate_log_resistance(p1=p1, t1=t1, p2=p2, t2=t2, target_t=20)
        p_lin_20 = p1 + (p2 - p1) * ((20 - t1) / (t2 - t1))
        self.assertAlmostEqual(p_log_20, 25.0, places=5)
        self.assertAlmostEqual(p_lin_20, 0.0, places=5)

        p_log_30 = tve.calculate_log_resistance(p1=p1, t1=t1, p2=p2, t2=t2, target_t=30)
        self.assertAlmostEqual(p_log_30, 12.5, places=5)

    def test_edge_cases_and_input_guards(self):
        self.assertEqual(tve.calculate_log_resistance(50.0, 100, 25.0, 200, 100), 50.0)
        self.assertEqual(tve.calculate_log_resistance(50.0, 100, 25.0, 200, 200), 25.0)
        self.assertEqual(tve.calculate_log_resistance(42.0, 100, 42.0, 200, 300), 42.0)

        with self.assertRaises(ValueError):
            tve.calculate_log_resistance(42.0, 100, 84.0, 100, 300)
        with self.assertRaises(ValueError):
            tve.calculate_log_resistance(0.0, 10, 20.0, 20, 15)
        with self.assertRaises(ValueError):
            tve.calculate_log_resistance(20.0, 10, -5.0, 20, 15)
        with self.assertRaises(ValueError):
            tve.calculate_log_resistance(1.0, 1000, 100.0, 1010, 100000)

    def test_microcap_breakout_precision_not_truncated(self):
        trendline = {
            "id": "micro_line",
            "interval": "15",
            "p1": 4.5e-7,
            "t1": 1000,
            "p2": 4.0e-7,
            "t2": 2000,
        }
        candle = {
            "symbol": "PEPEUSDT",
            "open": 3.5e-7,
            "high": 4.1e-7,
            "low": 3.4e-7,
            "close": 3.8e-7,
            "open_time": 3000,
            "close_time": 3899,
            "prev_candle": {
                "close": 3.4e-7,
                "open_time": 2100,
                "close_time": 2999,
            },
        }
        res = tve.check_symbol_breakout("PEPEUSDT", trendline, candle=candle)
        self.assertTrue(res["is_breakout"])
        self.assertGreater(res["res_price"], 3.0e-7)
        self.assertLess(res["res_price"], 3.8e-7)

    def test_check_symbol_breakout_requires_prev_candle(self):
        trendline = {"id": "L1", "p1": 10.0, "t1": 100, "p2": 9.0, "t2": 200}
        candle = {"open": 9.0, "close": 9.5, "open_time": 300, "close_time": 1199}
        with self.assertRaises(ValueError):
            tve.check_symbol_breakout("YBUSDT", trendline, candle=candle)

    def test_check_symbol_breakout_at_close_uses_prev_close_time(self):
        trendline = {"id": "L_close", "interval": "15", "p1": 100.0, "t1": 0, "p2": 50.0, "t2": 900}
        candle = {
            "symbol": "YBUSDT",
            "open": 26.0,
            "close": 27.0,
            "open_time": 900,
            "close_time": 1799,
            "prev_candle": {
                "close": 49.0,
                "open_time": 0,
                "close_time": 899,
            },
        }
        res = tve.check_symbol_breakout("YBUSDT", trendline, candle=candle, at_close=True)
        self.assertEqual(res["target_t"], 1799)
        expected_prev_res = tve.calculate_log_resistance(100.0, 0, 50.0, 900, 899)
        self.assertAlmostEqual(res["prev_res"], expected_prev_res, places=6)
        self.assertEqual(res["target_t"] - 899, 900)
        self.assertTrue(res["is_breakout"])


class TestFeature1NewResonanceDetection(unittest.TestCase):
    """3. Feature 1: New resonance detection, cold start stability, and retry semantics"""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = Path(self.test_dir) / "test.sqlite3"

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_cold_start_baseline_suppresses_initial_alerts(self):
        candidates = [
            {
                "symbol": "ALPHAUSDT",
                "price": 1.25,
                "turnover_ratio": 4.5,
                "funding_rate_4h_pct": -0.08,
                "is_resonance": True,
                "is_neg_funding": True,
                "zones": ["双核共振+逼空"],
            }
        ]
        db_store.publish_candidates(self.db_path, candidates)
        notifier = tad.ServerChanNotifier(dry_run=True)
        state_store = tad.ResonanceStateStore(self.db_path)

        alerts = tad.check_feature_1_resonance(
            notifier=notifier,
            state_store=state_store,
            layout_id="ig0LuDL8",
            cold_start=True,
        )
        self.assertEqual(len(alerts), 0)
        self.assertTrue(state_store.data["initialized"])
        self.assertEqual(state_store.get_active_symbols(), {"ALPHAUSDT"})

    def test_empty_pool_does_not_rearm_cold_start(self):
        notifier = tad.ServerChanNotifier(dry_run=True)
        state_store = tad.ResonanceStateStore(self.db_path)

        db_store.publish_candidates(self.db_path, [{"symbol": "INITUSDT", "is_resonance": True}])
        self.assertEqual(tad.check_feature_1_resonance(notifier, state_store, cold_start=True), [])

        db_store.publish_candidates(self.db_path, [])
        self.assertEqual(tad.check_feature_1_resonance(notifier, state_store, cold_start=True), [])
        self.assertEqual(state_store.get_active_symbols(), set())

        db_store.publish_candidates(self.db_path, [{"symbol": "NEWUSDT", "is_resonance": True}])
        alerts = tad.check_feature_1_resonance(notifier, state_store, cold_start=True)
        self.assertEqual([a["symbol"] for a in alerts], ["NEWUSDT"])

    def test_failed_send_alert_retries_next_cycle(self):
        db_store.publish_candidates(self.db_path, [{"symbol": "FAILUSDT", "is_resonance": True}])
        notifier = tad.ServerChanNotifier(dry_run=True)
        state_store = tad.ResonanceStateStore(self.db_path)
        state_store.data["initialized"] = True
        state_store.save()

        with patch.object(notifier, "send_alert", side_effect=[False, True]) as mock_send:
            alerts1 = tad.check_feature_1_resonance(notifier, state_store)
            self.assertEqual(alerts1, [])
            self.assertNotIn("FAILUSDT", state_store.get_active_symbols())

            alerts2 = tad.check_feature_1_resonance(notifier, state_store)
            self.assertEqual([a["symbol"] for a in alerts2], ["FAILUSDT"])
            self.assertIn("FAILUSDT", state_store.get_active_symbols())
            self.assertEqual(mock_send.call_count, 2)

    def test_new_resonance_detection_and_notification(self):
        candidates = [
            {
                "symbol": "ALPHAUSDT",
                "price": 1.25,
                "turnover_ratio": 4.5,
                "funding_rate_4h_pct": -0.08,
                "is_resonance": True,
                "is_neg_funding": True,
                "zones": ["双核共振+逼空"],
            },
            {
                "symbol": "BETAUSDT",
                "price": 0.45,
                "turnover_ratio": 0.8,
                "funding_rate_4h_pct": 0.01,
                "is_resonance": False,
                "zones": [],
            },
        ]
        db_store.publish_candidates(self.db_path, candidates)

        notifier = tad.ServerChanNotifier(dry_run=True)
        state_store = tad.ResonanceStateStore(self.db_path)
        state_store.data["active_resonance_symbols"] = ["GAMMAUSDT"]
        state_store.data["initialized"] = True
        state_store.save()

        alerts = tad.check_feature_1_resonance(
            notifier=notifier,
            state_store=state_store,
            layout_id="ig0LuDL8",
        )
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["symbol"], "ALPHAUSDT")
        self.assertEqual(state_store.get_active_symbols(), {"ALPHAUSDT"})
        hist_entry = state_store.data["history"]["ALPHAUSDT"]
        self.assertIn("https://cn.tradingview.com/chart/ig0LuDL8/?symbol=BINANCE:ALPHAUSDT.P&interval=15", hist_entry["tv_link"])

    def test_resonance_state_transition_and_deduplication(self):
        candidates = [
            {
                "symbol": "ALPHAUSDT",
                "price": 1.25,
                "is_resonance": True,
                "funding_rate_4h_pct": -0.05,
                "turnover_ratio": 2.5,
                "zones": ["双核共振+逼空"],
            }
        ]
        db_store.publish_candidates(self.db_path, candidates)
        notifier = tad.ServerChanNotifier(dry_run=True)
        state_store = tad.ResonanceStateStore(self.db_path)

        alerts1 = tad.check_feature_1_resonance(notifier, state_store, cold_start=False)
        self.assertEqual(len(alerts1), 1)
        alerts2 = tad.check_feature_1_resonance(notifier, state_store, cold_start=False)
        self.assertEqual(len(alerts2), 0)

        db_store.publish_candidates(self.db_path, [])
        alerts3 = tad.check_feature_1_resonance(notifier, state_store, cold_start=False)
        self.assertEqual(len(alerts3), 0)
        self.assertEqual(state_store.get_active_symbols(), set())

    def test_feature_1_24h_debounce(self):
        candidates = [{"symbol": "ALPHAUSDT", "price": 1.25, "is_resonance": True, "turnover_ratio": 2.5}]
        db_store.publish_candidates(self.db_path, candidates)

        notifier = tad.ServerChanNotifier(dry_run=True)
        state_store = tad.ResonanceStateStore(self.db_path)

        alerts1 = tad.check_feature_1_resonance(notifier, state_store, cold_start=False)
        self.assertEqual(len(alerts1), 1)

        state_store.data["active_resonance_symbols"] = []
        state_store.save()

        alerts2 = tad.check_feature_1_resonance(
            notifier, state_store, cold_start=False, debounce_hours=24.0
        )
        self.assertEqual(len(alerts2), 0)
        self.assertEqual(state_store.get_active_symbols(), {"ALPHAUSDT"})

        state_store.data["active_resonance_symbols"] = []
        state_store.data["history"]["ALPHAUSDT"]["last_notified_time"] = int(time.time() - 25 * 3600)
        state_store.save()

        alerts3 = tad.check_feature_1_resonance(
            notifier, state_store, cold_start=False, debounce_hours=24.0
        )
        self.assertEqual(len(alerts3), 1)

    def test_feature_1_preserves_detected_entry_time_and_resets_on_debounced_reentry(self):
        candidates = [
            {
                "symbol": "NEWCOINUSDT",
                "price": 0.12,
                "turnover_ratio": 3.0,
                "funding_rate_4h_pct": 0.01,
                "is_resonance": True,
                "resonance_detected_at": "2026-11-25 13:02:15",
                "resonance_entry_time": "11-25 13:02",
                "zones": ["双核黄金共振"],
            }
        ]
        db_store.publish_candidates(self.db_path, candidates)
        notifier = tad.ServerChanNotifier(dry_run=True)
        state_store = tad.ResonanceStateStore(self.db_path)

        with patch.object(notifier, "send_alert", return_value=True) as mock_send:
            alerts = tad.check_feature_1_resonance(notifier, state_store, cold_start=False)
        self.assertEqual(len(alerts), 1)
        hist = state_store.data["history"]["NEWCOINUSDT"]
        self.assertEqual(hist["first_detected"], "2026-11-25 13:02:15")
        self.assertIn("入圈时间", mock_send.call_args.args[1])
        self.assertIn("2026-11-25 13:02:15", mock_send.call_args.args[1])

        # Drop out and re-enter within 24h debounce: first_detected updates to new entry while last_notified stays intact
        prev_notified_at = hist["last_notified_at"]
        prev_notified_ts = hist["last_notified_time"]
        state_store.data["active_resonance_symbols"] = []
        state_store.save()

        candidates[0]["resonance_detected_at"] = "2026-11-25 18:02:10"
        candidates[0]["resonance_entry_time"] = "11-25 18:02"
        db_store.publish_candidates(self.db_path, candidates)
        alerts2 = tad.check_feature_1_resonance(
            notifier, state_store, cold_start=False, debounce_hours=24.0
        )
        self.assertEqual(alerts2, [])
        hist2 = state_store.data["history"]["NEWCOINUSDT"]
        self.assertEqual(hist2["first_detected"], "2026-11-25 18:02:10")
        self.assertEqual(hist2["last_notified_at"], prev_notified_at)
        self.assertEqual(hist2["last_notified_time"], prev_notified_ts)

    def test_cross_process_state_sync_prevents_duplicate_alert(self):
        candidates = [
            {
                "symbol": "BLESSUSDT",
                "price": 0.05,
                "is_resonance": True,
                "funding_rate_4h_pct": 0.01,
                "turnover_ratio": 15.0,
                "is_ema_bull": True,
                "zones": ["双核黄金共振"],
            }
        ]
        db_store.publish_candidates(self.db_path, candidates)
        notifier = tad.ServerChanNotifier(dry_run=True)

        daemon_store = tad.ResonanceStateStore(self.db_path)
        daemon_store.data["active_resonance_symbols"] = ["INITUSDT"]
        daemon_store.data["initialized"] = True
        daemon_store.save()

        cron_store = tad.ResonanceStateStore(self.db_path)
        alerts_cron = tad.check_feature_1_resonance(notifier, cron_store, cold_start=False)
        self.assertEqual(len(alerts_cron), 1)

        alerts_daemon = tad.check_feature_1_resonance(notifier, daemon_store, cold_start=False)
        self.assertEqual(len(alerts_daemon), 0)
        self.assertIn("BLESSUSDT", daemon_store.get_active_symbols())


class TestFeature2BreakoutDetection(unittest.TestCase):
    """4. Feature 2: 15m breakout detection"""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = Path(self.test_dir) / "test.sqlite3"
        self.state_store = tad.ResonanceStateStore(self.db_path)
        self.state_store.data["active_resonance_symbols"] = ["YBUSDT"]
        self.state_store.data["initialized"] = True
        self.state_store.save()
        self.notifier = tad.ServerChanNotifier(dry_run=True)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_ybusdt_1215_breakout_actual_data(self):
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
            "candle_time": "2026-11-21 12:15",
            "is_closed": True,
            "prev_candle": {
                "symbol": "YBUSDT",
                "open": 0.04920,
                "high": 0.04980,
                "low": 0.04910,
                "close": 0.04975,
                "open_time": 1795233600,
                "close_time": 1795234499,
                "is_closed": True,
            },
        }

        eval_result = tve.check_symbol_breakout("YBUSDT", trendline, candle=candle_1215)
        self.assertTrue(eval_result["is_breakout"])
        self.assertTrue(eval_result["is_crossover"])
        self.assertAlmostEqual(eval_result["res_price"], 0.049892, places=5)

        with patch.object(arc, "fetch_layout_drawings", return_value={"YBUSDT": [trendline]}):
            with patch.object(arc, "get_closed_15m_candle", return_value=candle_1215):
                alerts = tad.check_feature_2_breakout(self.notifier, self.state_store)
                self.assertEqual(len(alerts), 1)
                self.assertEqual(alerts[0]["symbol"], "YBUSDT")
                self.assertTrue(self.state_store.has_breakout_notified("YBUSDT_SFzHy9"))

    def test_already_above_line_does_not_trigger_breakout(self):
        trendline = {
            "id": "cross_line_2",
            "interval": "15",
            "p1": 0.06306,
            "t1": 1795104900,
            "p2": 0.05757,
            "t2": 1795155300,
        }
        candle_already_above = {
            "symbol": "YBUSDT",
            "open": 0.05010,
            "close": 0.05040,
            "open_time": 1795234500,
            "close_time": 1795235399,
            "is_closed": True,
            "prev_candle": {
                "close": 0.05015,
                "open_time": 1795233600,
                "close_time": 1795234499,
            },
        }
        with patch.object(arc, "fetch_layout_drawings", return_value={"YBUSDT": [trendline]}):
            with patch.object(arc, "get_closed_15m_candle", return_value=candle_already_above):
                alerts = tad.check_feature_2_breakout(self.notifier, self.state_store)
                self.assertEqual(len(alerts), 0)

    def test_true_crossover_triggers_breakout(self):
        trendline = {
            "id": "cross_line_1",
            "interval": "15",
            "p1": 0.06306,
            "t1": 1795104900,
            "p2": 0.05757,
            "t2": 1795155300,
        }
        candle_cross = {
            "symbol": "YBUSDT",
            "open": 0.04980,
            "close": 0.05020,
            "open_time": 1795234500,
            "close_time": 1795235399,
            "candle_time": "2026-11-21 12:15",
            "is_closed": True,
            "prev_candle": {
                "close": 0.04960,
                "open_time": 1795233600,
                "close_time": 1795234499,
            },
        }
        eval_result = tve.check_symbol_breakout("YBUSDT", trendline, candle=candle_cross)
        self.assertTrue(eval_result["is_crossover"])
        self.assertTrue(eval_result["is_breakout"])

        with patch.object(arc, "fetch_layout_drawings", return_value={"YBUSDT": [trendline]}):
            with patch.object(arc, "get_closed_15m_candle", return_value=candle_cross):
                alerts = tad.check_feature_2_breakout(self.notifier, self.state_store)
                self.assertEqual(len(alerts), 1)
                self.assertEqual(alerts[0]["trendline_id"], "cross_line_1")

    def test_single_line_consumption_deduplication(self):
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
            "close": 0.05020,
            "open_time": 1795234500,
            "close_time": 1795235399,
            "candle_time": "2026-11-21 12:15",
            "prev_candle": {"close": 0.04960, "open_time": 1795233600, "close_time": 1795234499},
        }
        candle_2 = {
            "symbol": "YBUSDT",
            "open": 0.05020,
            "close": 0.05080,
            "open_time": 1795235400,
            "close_time": 1795236299,
            "candle_time": "2026-11-21 12:30",
            "prev_candle": {"close": 0.04960, "open_time": 1795234500, "close_time": 1795235399},
        }
        with patch.object(arc, "fetch_layout_drawings", return_value={"YBUSDT": [trendline]}):
            with patch.object(arc, "get_closed_15m_candle", return_value=candle_1):
                self.assertEqual(len(tad.check_feature_2_breakout(self.notifier, self.state_store)), 1)
            with patch.object(arc, "get_closed_15m_candle", return_value=candle_2):
                self.assertEqual(len(tad.check_feature_2_breakout(self.notifier, self.state_store)), 0)

    def test_non_breakout_candle_below_trendline(self):
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
            "close": 0.04920,
            "open_time": 1795234500,
            "close_time": 1795235399,
            "prev_candle": {"close": 0.04910, "open_time": 1795233600, "close_time": 1795234499},
        }
        with patch.object(arc, "fetch_layout_drawings", return_value={"YBUSDT": [trendline]}):
            with patch.object(arc, "get_closed_15m_candle", return_value=candle_below):
                self.assertEqual(len(tad.check_feature_2_breakout(self.notifier, self.state_store)), 0)

    def test_notified_keys_not_evicted_at_200_limit(self):
        now = int(time.time())
        with self.state_store.transaction():
            for i in range(250):
                self.state_store.mark_breakout_notified(f"SYM{i}_L{i}", timestamp_sec=now - 3600)

        reloaded = tad.ResonanceStateStore(self.db_path)
        self.assertTrue(reloaded.has_breakout_notified("SYM0_L0"))
        self.assertEqual(len(reloaded.data["notified_breakout_keys"]), 250)

    def test_non_15m_and_ancient_trendlines_ignored(self):
        now_ts = 1795234500
        line_240m = {"id": "line_4h", "interval": "240", "p1": 0.063, "t1": now_ts - 36000, "p2": 0.057, "t2": now_ts - 3600}
        line_d = {"id": "line_daily", "interval": "D", "p1": 0.063, "t1": now_ts - 36000, "p2": 0.057, "t2": now_ts - 3600}
        line_ancient = {"id": "line_ancient", "interval": "15", "p1": 0.07, "t1": now_ts - 90 * 3600, "p2": 0.06, "t2": now_ts - 80 * 3600}
        line_far_future = {"id": "line_future", "interval": "15", "p1": 0.07, "t1": now_ts - 3600, "p2": 0.06, "t2": now_ts + 86400 * 60}
        line_near_future = {"id": "line_near_future", "interval": "15", "p1": 0.07, "t1": now_ts - 3600, "p2": 0.06, "t2": now_ts + 3600}

        self.assertFalse(tve.is_valid_15m_trendline(line_240m, current_time=now_ts))
        self.assertFalse(tve.is_valid_15m_trendline(line_d, current_time=now_ts))
        self.assertFalse(tve.is_valid_15m_trendline(line_ancient, current_time=now_ts))
        self.assertFalse(tve.is_valid_15m_trendline(line_far_future, current_time=now_ts))
        self.assertTrue(tve.is_valid_15m_trendline(line_near_future, current_time=now_ts))

        runaway_line = {"id": "runaway", "interval": "15", "p1": 0.010, "t1": now_ts - 7200, "p2": 0.008, "t2": now_ts - 3600}
        candle_runaway = {
            "symbol": "YBUSDT",
            "open": 0.050,
            "close": 0.050,
            "open_time": now_ts,
            "close_time": now_ts + 899,
            "prev_candle": {"close": 0.005, "open_time": now_ts - 900, "close_time": now_ts - 1},
        }
        eval_runaway = tve.check_symbol_breakout("YBUSDT", runaway_line, candle=candle_runaway)
        self.assertTrue(eval_runaway["is_runaway"])
        self.assertFalse(eval_runaway["is_breakout"])

        with patch.object(arc, "fetch_layout_drawings", return_value={"YBUSDT": [line_240m, line_d, line_ancient, runaway_line]}):
            with patch.object(arc, "get_closed_15m_candle", return_value=candle_runaway):
                alerts = tad.check_feature_2_breakout(self.notifier, self.state_store)
                self.assertEqual(len(alerts), 0)

    def test_malformed_drawing_does_not_abort_remaining_symbols(self):
        self.state_store.data["active_resonance_symbols"] = ["AAAUSDT", "YBUSDT"]
        self.state_store.save()
        bad_line = {"id": "bad_1", "interval": "15", "p1": None, "t1": 1795104900, "p2": 0.05, "t2": 1795155300}
        good_line = {"id": "SFzHy9", "interval": "15", "p1": 0.06306, "t1": 1795104900, "p2": 0.05757, "t2": 1795155300}
        candle_aaa = {
            "symbol": "AAAUSDT",
            "open": 1.0,
            "close": 1.1,
            "open_time": 1795234500,
            "close_time": 1795235399,
            "prev_candle": {"close": 0.9, "open_time": 1795233600, "close_time": 1795234499},
        }
        candle_yb = {
            "symbol": "YBUSDT",
            "open": 0.04982,
            "close": 0.05030,
            "open_time": 1795234500,
            "close_time": 1795235399,
            "candle_time": "2026-11-21 12:15",
            "prev_candle": {"close": 0.04975, "open_time": 1795233600, "close_time": 1795234499},
        }
        with patch.object(arc, "fetch_layout_drawings", return_value={"AAAUSDT": [bad_line], "YBUSDT": [good_line]}), patch.object(
            arc, "get_closed_15m_candle", side_effect=lambda sym: candle_aaa if sym == "AAAUSDT" else candle_yb
        ):
            alerts = tad.check_feature_2_breakout(self.notifier, self.state_store)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["symbol"], "YBUSDT")

    def test_stale_candle_rejected_and_marks_had_error_for_f2(self):
        current_bucket = 1795235400  # expected open_time = 1795234500
        trendline = {"id": "SFzHy9", "interval": "15", "p1": 0.06306, "t1": 1795104900, "p2": 0.05757, "t2": 1795155300}
        stale_candle = {
            "symbol": "YBUSDT",
            "open": 0.04982,
            "close": 0.05030,
            "open_time": 1795233600,  # one bucket too old!
            "close_time": 1795234499,
            "prev_candle": {"close": 0.04975, "open_time": 1795232700, "close_time": 1795233599},
        }
        status = {}
        candle_cache = {}
        with patch.object(arc, "fetch_layout_drawings", return_value={"YBUSDT": [trendline]}), patch.object(
            arc, "get_closed_15m_candle", return_value=stale_candle
        ):
            alerts = tad.check_feature_2_breakout(
                self.notifier, self.state_store, candle_cache=candle_cache, status=status, current_bucket=current_bucket
            )
        self.assertEqual(alerts, [])
        self.assertTrue(status.get("had_error"))
        self.assertNotIn("YBUSDT", candle_cache)

    def test_feature_2_time_aware_slope_handles_right_to_left_cached_lines(self):
        now = 1795234500
        rtl_desc = {"id": "RTL_DESC", "interval": "15", "p1": 0.05757, "t1": 1795155300, "p2": 0.06306, "t2": 1795104900}
        rtl_asc = {"id": "RTL_ASC", "interval": "15", "p1": 0.05000, "t1": 1795155300, "p2": 0.04500, "t2": 1795104900}
        candle = {
            "symbol": "YBUSDT",
            "open": 0.04982,
            "close": 0.05030,
            "open_time": now,
            "close_time": now + 899,
            "candle_time": "2026-11-21 12:15",
            "prev_candle": {"close": 0.04975, "open_time": now - 900, "close_time": now - 1},
        }
        with patch.object(arc, "fetch_layout_drawings", return_value={"YBUSDT": [rtl_asc, rtl_desc]}), patch.object(
            arc, "get_closed_15m_candle", return_value=candle
        ):
            alerts = tad.check_feature_2_breakout(self.notifier, self.state_store)
        self.assertEqual([a["trendline_id"] for a in alerts], ["RTL_DESC"])


class TestFeature3PositionStopLoss(unittest.TestCase):
    """5. Feature 3: Position stop-loss and trendline persistence"""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = Path(self.test_dir) / "test.sqlite3"
        self.pos_store = tad.PositionStore(self.db_path)
        self.notifier = tad.ServerChanNotifier(dry_run=True)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_pending_breakout_retries_after_restart(self):
        store = tad.ResonanceStateStore(self.db_path)
        store.data["pending_breakouts"] = {
            "TEST_line": {
                "symbol": "TEST",
                "res_price": 1.0,
                "close_price": 1.1,
                "pct": 10.0,
                "candle": {"open_time": 50, "candle_time": "2026-11-21 12:00"},
                "layout_id": "test",
                "keys": ["TEST_line"],
            }
        }
        store.save()
        with patch.object(self.notifier, "send_alert", side_effect=[False, True]) as send, patch.object(
            arc, "fetch_layout_drawings", return_value={}
        ):
            tad.check_feature_2_breakout(self.notifier, store)
            store = tad.ResonanceStateStore(self.db_path)
            self.assertIn("TEST_line", store.data["pending_breakouts"])
            self.assertFalse(store.has_breakout_notified("TEST_line"))

            tad.check_feature_2_breakout(self.notifier, store)
            store = tad.ResonanceStateStore(self.db_path)
            self.assertNotIn("TEST_line", store.data["pending_breakouts"])
            self.assertTrue(store.has_breakout_notified("TEST_line"))
            self.assertEqual(send.call_count, 2)

    def test_stop_loss_triggered_on_closing_below_trendline(self):
        trendline = {"p1": 10.0, "t1": 0, "p2": 6.4, "t2": 100, "id": "support_line_1"}
        self.pos_store.add_position("TESTUSDT", entry_price=9.0, amount=100.0, trendline=trendline)

        mock_candle_break = {
            "symbol": "TESTUSDT",
            "open_time": 50,
            "close_time": 949,
            "close": 7.60,
            "open": 8.10,
        }
        with patch.object(arc, "get_closed_15m_candle", return_value=mock_candle_break):
            alerts = tad.check_feature_3_stop_loss(self.notifier, self.pos_store)
            self.assertEqual(len(alerts), 1)
            self.assertEqual(alerts[0]["status"], "STOP_LOSS_TRIGGERED")
            self.assertEqual(alerts[0]["exit_price"], 7.60)
            self.assertAlmostEqual(alerts[0]["realized_pnl_pct"], -15.56, places=2)
            self.assertEqual(len(self.pos_store.get_open_positions()), 0)
            self.assertAlmostEqual(self.pos_store.get_history()[0]["realized_pnl_pct"], -15.56, places=2)

    def test_failed_stop_loss_retries_and_keeps_live_position_open(self):
        trendline = {"p1": 10.0, "t1": 0, "p2": 6.4, "t2": 100}
        self.pos_store.add_position("TESTUSDT", 9.0, amount=1.0, trendline=trendline, source="BINANCE_SYNC")
        candle = {"open_time": 50, "close_time": 949, "close": 7.6}
        with patch.object(self.notifier, "send_alert", side_effect=[False, True]) as send:
            with patch.object(arc, "get_closed_15m_candle", return_value=candle):
                self.assertEqual(tad.check_feature_3_stop_loss(self.notifier, self.pos_store), [])

            store = tad.PositionStore(self.db_path)
            self.assertFalse(store.get_open_positions()[0]["stop_loss_notified"])
            with patch.object(arc, "get_closed_15m_candle", side_effect=bc.BinanceAPIError("unused")):
                self.assertEqual(len(tad.check_feature_3_stop_loss(self.notifier, store)), 1)
                self.assertTrue(store.get_open_positions()[0]["stop_loss_notified"])
                self.assertEqual(store.get_history(), [])
                self.assertEqual(tad.check_feature_3_stop_loss(self.notifier, store), [])
            self.assertEqual(send.call_count, 2)

    def test_stop_loss_uses_candle_open_time_for_trendline(self):
        trendline = {"p1": 10.0, "t1": 0, "p2": 5.0, "t2": 100}
        self.pos_store.add_position("TESTUSDT", 9.0, amount=1, trendline=trendline)
        candle = {"open_time": 50, "close_time": 100, "close": 6.0}
        with patch.object(arc, "get_closed_15m_candle", return_value=candle):
            alerts = tad.check_feature_3_stop_loss(self.notifier, self.pos_store)
        self.assertEqual(len(alerts), 1)

    def test_no_stop_loss_when_holding_above_trendline(self):
        trendline = {"p1": 10.0, "t1": 0, "p2": 6.4, "t2": 100, "id": "support_line_1"}
        self.pos_store.add_position("TESTUSDT", entry_price=9.0, amount=100.0, trendline=trendline)
        mock_candle_hold = {"symbol": "TESTUSDT", "open_time": 50, "close_time": 949, "close": 8.40}
        with patch.object(arc, "get_closed_15m_candle", return_value=mock_candle_hold):
            alerts = tad.check_feature_3_stop_loss(self.notifier, self.pos_store)
            self.assertEqual(len(alerts), 0)
            self.assertEqual(len(self.pos_store.get_open_positions()), 1)
            self.assertEqual(self.pos_store.get_open_positions()[0]["status"], "OPEN")

    def test_stop_loss_persists_newly_matched_trendline_even_when_safe(self):
        now = int(time.time())
        self.pos_store.add_position("TESTUSDT", 9.0, amount=1.0, trendline=None)
        matched_line = {"id": "AUTO_TL", "interval": "15", "p1": 10.0, "t1": now - 3600, "p2": 8.0, "t2": now - 600}
        candle = {"open_time": now - 600, "close_time": now - 1, "close": 9.5}
        with patch.object(arc, "fetch_layout_drawings", return_value={"TESTUSDT": [matched_line]}), patch.object(
            arc, "get_closed_15m_candle", return_value=candle
        ):
            alerts = tad.check_feature_3_stop_loss(self.notifier, self.pos_store)
        self.assertEqual(alerts, [])
        reloaded = tad.PositionStore(self.db_path)
        self.assertEqual(reloaded.get_open_positions()[0]["trendline"]["id"], "AUTO_TL")

    def test_stale_candle_rejected_and_marks_had_error_for_f3(self):
        current_bucket = 1795235400
        trendline = {"p1": 10.0, "t1": 0, "p2": 6.4, "t2": 100, "id": "support_line_1"}
        self.pos_store.add_position("TESTUSDT", entry_price=9.0, amount=100.0, trendline=trendline)
        stale_candle = {"symbol": "TESTUSDT", "open_time": 1795233600, "close_time": 1795234499, "close": 1.0}
        status = {}
        candle_cache = {}
        with patch.object(arc, "get_closed_15m_candle", return_value=stale_candle):
            alerts = tad.check_feature_3_stop_loss(
                self.notifier, self.pos_store, candle_cache=candle_cache, status=status, current_bucket=current_bucket
            )
        self.assertEqual(alerts, [])
        self.assertTrue(status.get("had_error"))
        self.assertNotIn("TESTUSDT", candle_cache)
        self.assertEqual(len(self.pos_store.get_open_positions()), 1)


class TestFeature4TrailingStopProtection(unittest.TestCase):
    """6. Feature 4: Trailing stop protection and highest_price persistence"""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = Path(self.test_dir) / "test.sqlite3"
        self.pos_store = tad.PositionStore(self.db_path)
        self.notifier = tad.ServerChanNotifier(dry_run=True)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_trailing_stop_full_lifecycle_and_highest_price_persistence(self):
        self.pos_store.add_position("COINUSDT", entry_price=1.0000, amount=1000.0)

        with patch.object(arc, "fetch_ticker_prices", return_value={"COINUSDT": 1.0500}):
            events1 = tad.check_feature_4_trailing_stops(
                self.notifier, self.pos_store, activation_pct=6.0, lock_pct=1.0
            )
            self.assertEqual(len(events1), 0)
            reloaded = tad.PositionStore(self.db_path)
            self.assertEqual(reloaded.get_open_positions()[0]["highest_price"], 1.0500)
            self.assertEqual(reloaded.get_open_positions()[0]["current_price"], 1.0500)

        with patch.object(arc, "fetch_ticker_prices", return_value={"COINUSDT": 1.0650}):
            events2 = tad.check_feature_4_trailing_stops(
                self.notifier, self.pos_store, activation_pct=6.0, lock_pct=1.0
            )
            self.assertEqual(len(events2), 1)
            self.assertEqual(events2[0]["type"], "TRAILING_ACTIVATED")
            self.assertAlmostEqual(self.pos_store.get_open_positions()[0]["trailing_stop_price"], 1.0100, places=4)

        with patch.object(arc, "fetch_ticker_prices", return_value={"COINUSDT": 1.0090}):
            events3 = tad.check_feature_4_trailing_stops(
                self.notifier, self.pos_store, activation_pct=6.0, lock_pct=1.0
            )
            self.assertEqual(len(events3), 1)
            self.assertEqual(events3[0]["type"], "PROFIT_LOCK_EXIT")
            self.assertEqual(len(self.pos_store.get_open_positions()), 0)
            self.assertAlmostEqual(self.pos_store.get_history()[0]["realized_pnl_pct"], 0.90, places=2)

    def test_failed_profit_lock_retries_after_price_rebounds(self):
        self.pos_store.add_position("COINUSDT", 1.0, amount=1.0, source="BINANCE_SYNC")
        with patch.object(self.notifier, "send_alert", side_effect=[False, True, False, True]) as send:
            with patch.object(
                arc,
                "fetch_ticker_prices",
                side_effect=[{"COINUSDT": 1.07}, {"COINUSDT": 1.009}, {"COINUSDT": 1.03}],
            ):
                self.assertEqual(tad.check_feature_4_trailing_stops(self.notifier, self.pos_store), [])
                store = tad.PositionStore(self.db_path)
                events = tad.check_feature_4_trailing_stops(self.notifier, store)
                self.assertEqual([e["type"] for e in events], ["TRAILING_ACTIVATED"])
                store = tad.PositionStore(self.db_path)
                events = tad.check_feature_4_trailing_stops(self.notifier, store)
                self.assertEqual([e["type"] for e in events], ["PROFIT_LOCK_EXIT"])
            self.assertTrue(store.get_open_positions()[0]["profit_lock_notified"])
            self.assertIn("$1.0090", send.call_args.args[1])

    def test_zero_entry_price_guarded_without_crash(self):
        db_store.upsert_open_position(self.db_path, {"symbol": "BADUSDT", "entry_price": 0.0, "status": "OPEN"})
        with patch.object(arc, "fetch_ticker_prices", return_value={"BADUSDT": 1.5}):
            events = tad.check_feature_4_trailing_stops(self.notifier, self.pos_store)
        self.assertEqual(events, [])

    def test_add_position_entry_change_resets_trailing_and_pending_state(self):
        self.pos_store.add_position(
            "COINUSDT",
            entry_price=1.0,
            amount=10.0,
            highest_price=1.2,
            trailing_stop_triggered=True,
            trailing_stop_price=1.01,
            trailing_stop_activated_at="2026-11-25 12:00:00",
            profit_lock_notified=True,
            stop_loss_notified=True,
            pending_stop_loss_candle={"open_time": 100},
            pending_trailing_activation_price=1.07,
            pending_profit_lock_price=1.005,
        )
        # Same entry_price preserves existing tracking state
        self.pos_store.add_position("COINUSDT", entry_price=1.0, amount=15.0)
        unchanged = self.pos_store.get_open_positions()[0]
        self.assertEqual(unchanged["highest_price"], 1.2)
        self.assertTrue(unchanged["trailing_stop_triggered"])
        self.assertEqual(unchanged["amount"], 15.0)

        # Changed entry_price resets highest_price, flags, and pending_* / activated_at
        self.pos_store.add_position("COINUSDT", entry_price=0.80, amount=20.0)
        reset_pos = self.pos_store.get_open_positions()[0]
        self.assertEqual(reset_pos["entry_price"], 0.80)
        self.assertEqual(reset_pos["highest_price"], 0.80)
        self.assertFalse(reset_pos["trailing_stop_triggered"])
        self.assertIsNone(reset_pos["trailing_stop_price"])
        self.assertFalse(reset_pos["profit_lock_notified"])
        self.assertFalse(reset_pos["stop_loss_notified"])
        self.assertNotIn("trailing_stop_activated_at", reset_pos)
        self.assertNotIn("pending_stop_loss_candle", reset_pos)
        self.assertNotIn("pending_trailing_activation_price", reset_pos)
        self.assertNotIn("pending_profit_lock_price", reset_pos)


class TestDaemonLoggingConfigAndCLI(unittest.TestCase):
    """7. Logging isolation, corrupt-DB fail-fast, daemon bucket control, and CLI main()"""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = Path(self.test_dir) / "test.sqlite3"
        db_store.publish_candidates(self.db_path, [], updated_at="2026-11-25 11:00:00+08:00")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_daemon_logging_not_hijacked_by_tv_engine(self):
        log_file = Path(self.test_dir) / "daemon.log"
        handlers = tad.configure_logging(log_file=log_file)
        try:
            tad.logger.info("daemon_log_probe_message")
        finally:
            tad.close_managed_handlers(handlers)

        content = log_file.read_text(encoding="utf-8")
        self.assertIn("daemon_log_probe_message", content)
        self.assertNotIn("[tv_engine]", content)

    def test_corrupt_json_store_raises_instead_of_wiping(self):
        corrupt_pos_db = Path(self.test_dir) / "corrupt_pos.sqlite3"
        with db_store.transaction(corrupt_pos_db) as conn:
            conn.execute("INSERT INTO open_positions (symbol, payload) VALUES ('BAD', '{corrupt json')")
        with self.assertRaises(json.JSONDecodeError):
            tad.PositionStore(corrupt_pos_db)

        corrupt_res_db = Path(self.test_dir) / "corrupt_res.sqlite3"
        with db_store.transaction(corrupt_res_db) as conn:
            conn.execute("INSERT INTO resonance_state (id, payload) VALUES (1, '[1, 2, 3]')")
        with self.assertRaises(ValueError):
            tad.ResonanceStateStore(corrupt_res_db)

    def test_daemon_startup_avoids_double_run_and_retries_failed_15m_bucket(self):
        cfg_map = {
            "serverchan_sendkey": "",
            "binance_api_key": "",
            "binance_api_secret": "",
            "tradingview_layout_id": "ig0LuDL8",
            "trailing_activation_pct": 6.0,
            "trailing_lock_pct": 1.0,
            "database_path": self.db_path,
        }
        daemon = tad.TradeAlertDaemon(cfg_map, dry_run=True)

        bucket_ts = 1800000000  # divisible by 900
        with patch.object(tad, "check_feature_1_resonance", return_value=[]) as f1_mock, patch.object(
            tad, "check_feature_2_breakout", side_effect=lambda *a, status=None, **kw: status.update({"had_error": True}) or []
        ) as f2_mock, patch.object(tad, "check_feature_3_stop_loss", return_value=[]), patch.object(
            tad, "check_feature_4_trailing_stops", return_value=[]
        ):
            daemon.run_iteration(now=bucket_ts + 15)
            self.assertEqual(daemon.last_evaluated_15m_bucket, 0)
            self.assertEqual(f1_mock.call_count, 1)
            daemon.run_iteration(now=bucket_ts + 25)
            self.assertEqual(f2_mock.call_count, 1)  # 60s 退避内不重复刷屏
            self.assertEqual(f1_mock.call_count, 1)  # 快照未更新不重复扫描 Feature 1
            # 模拟小时扫描更新了 SQLite 候选池快照：即使不在 15m 收盘点，10s 节拍也会立即触发 Feature 1
            db_store.publish_candidates(
                self.db_path,
                [{"symbol": "NEWUSDT"}],
                updated_at="2026-11-25 12:00:00+08:00",
            )
            daemon.run_iteration(now=bucket_ts + 35)
            self.assertEqual(f1_mock.call_count, 2)
            daemon.run_iteration(now=bucket_ts + 80)
            self.assertEqual(f2_mock.call_count, 2)

        with patch.object(tad, "check_feature_1_resonance", return_value=[]), patch.object(
            tad, "check_feature_2_breakout", return_value=[]
        ) as f2_ok, patch.object(tad, "check_feature_3_stop_loss", return_value=[]), patch.object(
            tad, "check_feature_4_trailing_stops", return_value=[]
        ):
            daemon.run_iteration(now=bucket_ts + 145)
            self.assertEqual(daemon.last_evaluated_15m_bucket, bucket_ts)
            daemon.run_iteration(now=bucket_ts + 155)
            self.assertEqual(f2_ok.call_count, 1)

    def test_daemon_retries_feature_3_independently_on_transient_or_stale_error(self):
        cfg_map = {
            "tradingview_layout_id": "ig0LuDL8",
            "database_path": self.db_path,
        }
        daemon = tad.TradeAlertDaemon(cfg_map, dry_run=True)
        bucket_ts = 1800000000
        f3_calls = []

        def fake_f3(*args, status=None, **kwargs):
            f3_calls.append(1)
            if len(f3_calls) == 1 and status is not None:
                status["had_error"] = True
            return []

        with patch.object(tad, "check_feature_1_resonance", return_value=[]), patch.object(
            tad, "check_feature_2_breakout", return_value=[]
        ) as f2_mock, patch.object(tad, "check_feature_3_stop_loss", side_effect=fake_f3), patch.object(
            tad, "check_feature_4_trailing_stops", return_value=[]
        ):
            daemon.run_iteration(now=bucket_ts + 15)
            self.assertEqual(len(f3_calls), 1)
            self.assertEqual(f2_mock.call_count, 1)
            self.assertEqual(daemon.last_evaluated_15m_bucket, 0)  # F3 failed -> bucket NOT marked done

            daemon.run_iteration(now=bucket_ts + 25)  # within 60s cooldown
            self.assertEqual(len(f3_calls), 1)

            daemon.run_iteration(now=bucket_ts + 80)  # F3 retries and succeeds; F2 already done
            self.assertEqual(len(f3_calls), 2)
            self.assertEqual(f2_mock.call_count, 1)
            self.assertEqual(daemon.last_evaluated_15m_bucket, bucket_ts)

    def test_daemon_start_survives_transient_errors_and_raises_fatal_errors(self):
        cfg_map = {
            "tradingview_layout_id": "ig0LuDL8",
            "database_path": self.db_path,
        }
        daemon = tad.TradeAlertDaemon(cfg_map, dry_run=True)

        def transient_then_stop():
            if not getattr(transient_then_stop, "called", False):
                transient_then_stop.called = True
                raise bc.BinanceAPIError("temporary 503", code=503)
            daemon.running = False

        with patch.object(tad.signal, "signal"), patch.object(
            tad, "run_evaluation_cycle", side_effect=bc.BinanceAPIError("startup 503", code=503)
        ), patch.object(daemon, "run_iteration", side_effect=transient_then_stop), patch("time.sleep"):
            daemon.start()
        self.assertFalse(daemon.running)

        daemon2 = tad.TradeAlertDaemon(cfg_map, dry_run=True)
        with patch.object(tad.signal, "signal"), patch.object(
            tad, "run_evaluation_cycle", return_value={"feature_2_had_error": False, "feature_3_had_error": False}
        ), patch.object(daemon2, "run_iteration", side_effect=ValueError("corrupt state")), patch("time.sleep"):
            with self.assertRaises(ValueError):
                daemon2.start()

    def test_dotenv_inline_comments_and_quoted_hash(self):
        env_path = Path(self.test_dir) / ".env"
        env_path.write_text(
            'KEY_A=alpha # trailing comment\n'
            'KEY_B="secret#hash" # comment after quote\n'
            "KEY_C='single#hash'\n"
            "export KEY_D=exported_val\n",
            encoding="utf-8",
        )
        parsed = cfg.load_dotenv(env_path)
        self.assertEqual(parsed["KEY_A"], "alpha")
        self.assertEqual(parsed["KEY_B"], "secret#hash")
        self.assertEqual(parsed["KEY_C"], "single#hash")
        self.assertEqual(parsed["KEY_D"], "exported_val")

    @patch.dict(os.environ, clear=True)
    def test_cli_main_add_list_close_and_check_once(self):
        cfg_map = {
            "serverchan_sendkey": "",
            "binance_api_key": "",
            "binance_api_secret": "",
            "tradingview_layout_id": "ig0LuDL8",
            "trailing_activation_pct": 6.0,
            "trailing_lock_pct": 1.0,
            "database_path": self.db_path,
        }
        with patch.object(tad, "configure_logging", return_value=[]), patch.object(
            tad, "load_config", return_value=cfg_map
        ), patch.object(tad, "fetch_ticker_prices", return_value={"YBUSDT": 0.055}), patch.object(
            arc, "fetch_ticker_prices", return_value={"YBUSDT": 0.055}
        ), patch.object(
            arc, "fetch_layout_drawings", return_value={}
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                with patch.object(sys, "argv", ["trade_alert_daemon.py", "--add-pos", "BINANCE:YBUSDT.P", "0.05", "--amount", "2000"]):
                    tad.main()
                with patch.object(sys, "argv", ["trade_alert_daemon.py", "--list-pos"]):
                    tad.main()
                with patch.object(sys, "argv", ["trade_alert_daemon.py", "--check-once", "--dry-run"]):
                    tad.main()
                with patch.object(sys, "argv", ["trade_alert_daemon.py", "--close-pos", "YBUSDT"]):
                    tad.main()

        out = buf.getvalue()
        self.assertIn("YBUSDT", out)
        self.assertIn("0.0550", out)
        self.assertIn("feature_1_resonance_new", out)
        self.assertIn("已手动平仓并归档", out)

    @patch.dict(os.environ, {"TRAILING_ACTIVATION_PCT": "9.0"}, clear=True)
    def test_load_config_from_dotenv_and_env_override(self):
        test_env = Path(self.test_dir) / ".env.test"
        test_env.write_text(
            "SERVERCHAN_SENDKEY=SCTtestkey999\n"
            "BINANCE_API_KEY=binance_key_abc\n"
            "BINANCE_API_SECRET=binance_secret_xyz\n"
            "TRADINGVIEW_LAYOUT_ID=custom_layout_789\n"
            "TRAILING_ACTIVATION_PCT=8.5\n"
            "TRAILING_LOCK_PCT=1.5\n",
            encoding="utf-8",
        )
        loaded = tad.load_config(env_path=test_env)
        self.assertEqual(loaded["serverchan_sendkey"], "SCTtestkey999")
        self.assertEqual(loaded["binance_api_key"], "binance_key_abc")
        self.assertEqual(loaded["binance_api_secret"], "binance_secret_xyz")
        self.assertEqual(loaded["tradingview_layout_id"], "custom_layout_789")
        self.assertEqual(loaded["trailing_activation_pct"], 9.0)
        self.assertEqual(loaded["trailing_lock_pct"], 1.5)


if __name__ == "__main__":
    unittest.main()
