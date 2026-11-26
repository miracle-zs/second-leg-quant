#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Automated Test Suite for Binance USDⓈ-M Futures Client & Position Auto-Sync
"""

import hashlib
import hmac
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import binance_client as bc
import binance_position_client as bpc
import db_store
import http_client
import trade_alert_daemon as tad


class TestBinanceFuturesClient(unittest.TestCase):
    """1. Test BinanceFuturesClient auth, signatures, strict error propagation, and retries."""

    def test_client_configuration_detection(self):
        c1 = bc.BinanceFuturesClient(api_key="", api_secret="")
        self.assertFalse(c1.is_configured())

        c2 = bc.BinanceFuturesClient(api_key="my_key", api_secret="")
        self.assertFalse(c2.is_configured())

        c3 = bc.BinanceFuturesClient(api_key="my_key", api_secret="my_secret")
        self.assertTrue(c3.is_configured())

    def test_hmac_sha256_signature_generation(self):
        api_key = "test_api_key_123"
        api_secret = "test_secret_abc_xyz_789"
        client = bc.BinanceFuturesClient(api_key=api_key, api_secret=api_secret)

        query_str = "recvWindow=5000&timestamp=1700000000000"
        expected_sig = hmac.new(
            api_secret.encode("utf-8"),
            query_str.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        actual_sig = client._generate_signature(query_str)
        self.assertEqual(actual_sig, expected_sig)
        self.assertEqual(len(actual_sig), 64)

    def test_server_time_caching_and_explicit_error(self):
        client = bc.BinanceFuturesClient(api_key="key", api_secret="sec")

        with patch.object(bc.http_client, "request_json", return_value={"serverTime": 1795200000123}):
            t = client.get_server_time()
            self.assertEqual(t, 1795200000123)

        fresh_client = bc.BinanceFuturesClient(api_key="key", api_secret="sec")
        with patch.object(bc.http_client, "request_json", side_effect=http_client.HttpError("Timeout")), patch("time.sleep"):
            with self.assertRaises(bc.BinanceAPIError):
                fresh_client.get_server_time()

        with patch.object(bc.http_client, "request_json", return_value={}):
            with self.assertRaises(bc.BinanceAPIError):
                fresh_client.get_server_time()

    def test_unconfigured_and_error_responses_raise_binance_api_error(self):
        unconfigured = bc.BinanceFuturesClient(api_key="", api_secret="")
        with self.assertRaises(bc.BinanceAPIError) as ctx:
            unconfigured.get_account_positions()
        self.assertEqual(ctx.exception.code, "NOT_CONFIGURED")

        configured = bc.BinanceFuturesClient(api_key="k", api_secret="s")
        with patch.object(
            configured,
            "_send_signed_request",
            side_effect=bc.BinanceAPIError("Invalid key", code=-2015, binance_code=-2015),
        ):
            with self.assertRaises(bc.BinanceAPIError):
                configured.get_account_info()
            with self.assertRaises(bc.BinanceAPIError):
                configured.get_income()
            with self.assertRaises(bc.BinanceAPIError):
                configured.get_user_trades("BTCUSDT")

        with patch.object(bc.http_client, "request_json", side_effect=http_client.HttpError("down")), patch("time.sleep"):
            with self.assertRaises(bc.BinanceAPIError):
                configured.get_ticker_prices(["BTCUSDT"])

    def test_send_signed_request_retries_429_and_1021_fails_fast_on_401(self):
        client = bc.BinanceFuturesClient(api_key="k", api_secret="s")
        client._time_offset_ms = 0
        client._time_synced_at = time.monotonic()

        err_429 = http_client.HttpError(
            "HTTP 429", status_code=429, body='{"code":-1003}', url="https://fapi.binance.com/fapi/v2/account"
        )
        ok_payload = {"totalWalletBalance": "100"}

        with patch.object(bc.http_client, "request_json", side_effect=[err_429, ok_payload]) as mock_req, patch("time.sleep"):
            data = client._send_signed_request("/fapi/v2/account")
        self.assertEqual(data["totalWalletBalance"], "100")
        self.assertEqual(mock_req.call_count, 2)

        err_401 = http_client.HttpError(
            "HTTP 401", status_code=401, body='{"code":-2015}', url="https://fapi.binance.com/fapi/v2/account"
        )
        with patch.object(bc.http_client, "request_json", side_effect=err_401) as mock_req, patch("time.sleep"):
            with self.assertRaises(bc.BinanceAPIError) as ctx:
                client._send_signed_request("/fapi/v2/account")
        self.assertEqual(ctx.exception.code, 401)
        self.assertEqual(mock_req.call_count, 1)

        err_1021 = http_client.HttpError(
            "HTTP 400", status_code=400, body='{"code":-1021,"msg":"ts"}', url="https://fapi.binance.com/fapi/v2/account"
        )
        time_payload = {"serverTime": int(time.time() * 1000)}
        with patch.object(bc.http_client, "request_json", side_effect=[err_1021, time_payload, ok_payload]) as mock_req:
            data = client._send_signed_request("/fapi/v2/account")
        self.assertEqual(data["totalWalletBalance"], "100")
        self.assertEqual(mock_req.call_count, 3)

    def test_get_active_positions_filtering_and_required_fields(self):
        client = bc.BinanceFuturesClient(api_key="test_key", api_secret="test_sec")

        mock_raw_positions = [
            {
                "symbol": "BTCUSDT",
                "positionAmt": "0.000",
                "entryPrice": "0.0",
                "markPrice": "90000.0",
                "unRealizedProfit": "0.0",
                "liquidationPrice": "0",
                "leverage": "20",
                "positionSide": "BOTH",
                "updateTime": 0,
                "notional": "0",
            },
            {
                "symbol": "YBUSDT",
                "positionAmt": "2000.0",
                "entryPrice": "0.0520",
                "markPrice": "0.0550",
                "unRealizedProfit": "6.0",
                "liquidationPrice": "0.0468",
                "leverage": "10",
                "positionSide": "BOTH",
                "updateTime": 1795260000000,
                "notional": "110.0",
            },
            {
                "symbol": "ETHUSDT",
                "positionAmt": "-1.500",
                "entryPrice": "3100.0",
                "markPrice": "3050.0",
                "unRealizedProfit": "75.0",
                "liquidationPrice": "3255.0",
                "leverage": "20",
                "positionSide": "BOTH",
                "updateTime": 1795260001000,
                "notional": "-4575.0",
            },
            {
                "symbol": "BINANCE:RAREUSDT.P",
                "positionAmt": "500.0",
                "entryPrice": "0.0150",
                "markPrice": "0.0160",
                "unRealizedProfit": "0.5",
                "liquidationPrice": "0.0120",
                "leverage": "5",
                "positionSide": "BOTH",
                "updateTime": 1795260002000,
                "notional": "8.0",
            },
        ]

        with patch.object(client, "get_account_positions", return_value=mock_raw_positions):
            active_longs = client.get_active_positions(include_short=False)
            self.assertEqual(len(active_longs), 2)

            yb = next(p for p in active_longs if p["symbol"] == "YBUSDT")
            self.assertEqual(yb["amount"], 2000.0)
            self.assertEqual(yb["entry_price"], 0.0520)
            self.assertEqual(yb["mark_price"], 0.0550)
            self.assertEqual(yb["unrealized_pnl"], 6.0)
            self.assertEqual(yb["liquidation_price"], 0.0468)
            self.assertEqual(yb["leverage"], 10)

            rare = next(p for p in active_longs if p["symbol"] == "RAREUSDT")
            self.assertEqual(rare["amount"], 500.0)
            self.assertEqual(rare["entry_price"], 0.0150)

        malformed = [{"symbol": "BTCUSDT", "positionAmt": "1.0"}]
        with patch.object(client, "get_account_positions", return_value=malformed):
            with self.assertRaises(bc.BinanceAPIError):
                client.get_active_positions()


class TestBinanceAutoSyncLifecycle(unittest.TestCase):
    """2. Test auto-syncing lifecycle between Binance API and PositionStore."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = Path(self.test_dir) / "test.sqlite3"
        self.pos_store = tad.PositionStore(self.db_path)
        self.client = bc.BinanceFuturesClient(api_key="mock_key", api_secret="mock_sec")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_sync_unconfigured_graceful_degradation(self):
        unconfigured_client = bc.BinanceFuturesClient(api_key="", api_secret="")
        res = bpc.sync_binance_positions(unconfigured_client, self.pos_store)
        self.assertFalse(res["synced"])
        self.assertEqual(res["reason"], "API_NOT_CONFIGURED")

    def test_select_best_position_trendline_rejects_stale_and_missing_interval(self):
        now = time.time()
        missing_iv = {"id": "NO_IV", "p1": 10.0, "t1": now - 3600, "p2": 9.0, "t2": now - 600}
        stale_15m = {"id": "OLD_15M", "interval": "15", "p1": 10.0, "t1": now - 90 * 3600, "p2": 9.0, "t2": now - 80 * 3600}
        malformed_15m = {"id": "BAD_15M", "interval": "15", "p1": None, "t1": now - 3600, "p2": 9.0, "t2": now - 100}
        self.assertIsNone(bpc.select_best_position_trendline([missing_iv, stale_15m, malformed_15m], current_time=now))

        fresh_15m = {"id": "GOOD_15M", "interval": "15", "p1": 10.0, "t1": now - 3600, "p2": 9.0, "t2": now - 600}
        chosen = bpc.select_best_position_trendline([missing_iv, stale_15m, malformed_15m, fresh_15m], current_time=now)
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["id"], "GOOD_15M")

    def test_select_best_position_trendline_uses_time_aware_slope(self):
        now = int(time.time())
        rtl_ascending = {"id": "RTL_ASC", "interval": "15", "p1": 11.0, "t1": now - 300, "p2": 9.0, "t2": now - 3600}
        rtl_descending = {"id": "RTL_DESC", "interval": "15", "p1": 9.0, "t1": now - 600, "p2": 11.0, "t2": now - 3600}
        chosen = bpc.select_best_position_trendline([rtl_ascending, rtl_descending], current_time=now)
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["id"], "RTL_DESC")

    def test_entry_changed_resets_highest_price_without_carrying_old_peak(self):
        self.pos_store.add_position(
            "YBUSDT",
            entry_price=1.0,
            amount=100.0,
            source="BINANCE_SYNC",
            highest_price=1.5,
            trailing_stop_triggered=True,
            trailing_stop_price=1.01,
        )
        reopened_lower = [
            {
                "symbol": "YBUSDT",
                "amount": 100.0,
                "entry_price": 0.60,
                "mark_price": 0.62,
                "unrealized_pnl": 2.0,
                "leverage": 10,
                "notional": 62.0,
            }
        ]
        with patch.object(self.client, "get_active_positions", return_value=reopened_lower), patch(
            "tv_trendline_engine.get_layout_drawings", return_value={}
        ):
            res = bpc.sync_binance_positions(self.client, self.pos_store)
        self.assertTrue(res["synced"])
        pos = self.pos_store.get_open_positions()[0]
        self.assertEqual(pos["highest_price"], 0.62)

        zero_mark_reentry = [
            {
                "symbol": "YBUSDT",
                "amount": 100.0,
                "entry_price": 0.50,
                "mark_price": 0.0,
                "unrealized_pnl": 0.0,
                "leverage": 10,
                "notional": 50.0,
            }
        ]
        with patch.object(self.client, "get_active_positions", return_value=zero_mark_reentry), patch(
            "tv_trendline_engine.get_layout_drawings", return_value={}
        ):
            bpc.sync_binance_positions(self.client, self.pos_store)
        pos2 = self.pos_store.get_open_positions()[0]
        self.assertEqual(pos2["highest_price"], 0.50)

    def test_auto_register_new_position_and_match_trendline(self):
        now = int(time.time())
        mock_active_longs = [
            {
                "symbol": "YBUSDT",
                "amount": 2500.0,
                "entry_price": 0.0515,
                "mark_price": 0.0530,
                "unrealized_pnl": 3.75,
                "leverage": 10,
                "position_side": "BOTH",
                "update_time": now * 1000,
                "notional": 132.5,
            }
        ]
        mock_trendline = {
            "id": "SFzHy9",
            "interval": "15",
            "p1": 0.06306,
            "t1": now - 7200,
            "p2": 0.05757,
            "t2": now - 1800,
            "symbol": "YBUSDT",
        }
        mock_drawings = {"YBUSDT": [mock_trendline]}

        with patch.object(self.client, "get_active_positions", return_value=mock_active_longs):
            load_drawings = MagicMock(return_value=mock_drawings)
            res = bpc.sync_binance_positions(
                self.client, self.pos_store, layout_id="ig0LuDL8", load_drawings=load_drawings
            )
            load_drawings.assert_called_once_with("ig0LuDL8")
            bpc.sync_binance_positions(
                self.client, self.pos_store, layout_id="ig0LuDL8", load_drawings=load_drawings
            )
            load_drawings.assert_called_once()

            self.assertTrue(res["synced"])
            self.assertIn("YBUSDT", res["new_added"])
            self.assertEqual(res["active_count"], 1)

            open_positions = self.pos_store.get_open_positions()
            self.assertEqual(len(open_positions), 1)
            pos = open_positions[0]
            self.assertEqual(pos["symbol"], "YBUSDT")
            self.assertEqual(pos["entry_price"], 0.0515)
            self.assertEqual(pos["amount"], 2500.0)
            self.assertEqual(pos["source"], "BINANCE_SYNC")
            self.assertEqual(pos["leverage"], 10)
            self.assertEqual(pos["trendline"]["id"], "SFzHy9")

    def test_auto_update_on_scale_in_resets_trailing_and_stop_flags(self):
        now = int(time.time())
        existing_tl = {"id": "TL1", "interval": "15", "p1": 0.06, "t1": now - 3600, "p2": 0.055, "t2": now - 900}
        self.pos_store.add_position(
            "YBUSDT",
            entry_price=0.0520,
            amount=1000.0,
            trendline=existing_tl,
            source="BINANCE_SYNC",
            trailing_stop_triggered=True,
            trailing_stop_price=0.0525,
            profit_lock_notified=True,
            stop_loss_notified=True,
            highest_price=0.0580,
            pending_profit_lock_price=0.0524,
        )

        scaled_in_longs = [
            {
                "symbol": "YBUSDT",
                "amount": 2000.0,
                "entry_price": 0.0540,
                "mark_price": 0.0550,
                "unrealized_pnl": 2.0,
                "leverage": 10,
                "notional": 110.0,
            }
        ]

        with patch.object(self.client, "get_active_positions", return_value=scaled_in_longs):
            with patch("tv_trendline_engine.get_layout_drawings", return_value={}) as load_drawings:
                res = bpc.sync_binance_positions(self.client, self.pos_store)
                load_drawings.assert_not_called()
                self.assertTrue(res["synced"])
                self.assertEqual(len(res["new_added"]), 0)

                open_pos = self.pos_store.get_open_positions()[0]
                self.assertEqual(open_pos["entry_price"], 0.0540)
                self.assertEqual(open_pos["amount"], 2000.0)
                self.assertFalse(open_pos["trailing_stop_triggered"])
                self.assertIsNone(open_pos["trailing_stop_price"])
                self.assertFalse(open_pos["profit_lock_notified"])
                self.assertFalse(open_pos["stop_loss_notified"])
                self.assertNotIn("pending_profit_lock_price", open_pos)
                self.assertEqual(open_pos["highest_price"], 0.0550)

    def test_micro_priced_entry_change_and_manual_source_current_price_preservation(self):
        self.pos_store.add_position(
            "PEPEUSDT",
            entry_price=4.5e-7,
            amount=1000000.0,
            source="MANUAL",
            current_price=4.9e-7,
            highest_price=5.2e-7,
            trailing_stop_triggered=True,
        )
        updated_longs = [
            {
                "symbol": "PEPEUSDT",
                "amount": 1500000.0,
                "entry_price": 4.6e-7,
                "mark_price": 4.7e-7,
                "unrealized_pnl": 0.015,
                "leverage": 10,
                "notional": 0.705,
            }
        ]
        with patch.object(self.client, "get_active_positions", return_value=updated_longs):
            with patch("tv_trendline_engine.get_layout_drawings", return_value={}):
                res = bpc.sync_binance_positions(self.client, self.pos_store)

        self.assertTrue(res["synced"])
        open_pos = self.pos_store.get_open_positions()[0]
        self.assertEqual(open_pos["entry_price"], 4.6e-7)
        self.assertFalse(open_pos["trailing_stop_triggered"])
        self.assertEqual(open_pos["highest_price"], 4.7e-7)
        self.assertEqual(open_pos["source"], "MANUAL")
        self.assertEqual(open_pos["mark_price"], 4.7e-7)
        self.assertEqual(open_pos["current_price"], 4.9e-7)

    def test_sync_retries_missing_trendline_for_existing_position(self):
        now = int(time.time())
        self.pos_store.add_position("YBUSDT", entry_price=0.0520, amount=1000.0, source="BINANCE_SYNC", trendline=None)
        active = [
            {
                "symbol": "YBUSDT",
                "amount": 1000.0,
                "entry_price": 0.0520,
                "mark_price": 0.0525,
                "unrealized_pnl": 0.5,
                "leverage": 10,
                "notional": 52.5,
            }
        ]
        fresh_tl = {"id": "NEW_TL", "interval": "15", "p1": 0.06, "t1": now - 3600, "p2": 0.055, "t2": now - 600}
        with patch.object(self.client, "get_active_positions", return_value=active):
            load_drawings = MagicMock(return_value={"YBUSDT": [fresh_tl]})
            res = bpc.sync_binance_positions(self.client, self.pos_store, load_drawings=load_drawings)
            self.assertTrue(res["synced"])
            load_drawings.assert_called_once()
            reloaded = tad.PositionStore(self.db_path)
            self.assertEqual(reloaded.get_open_positions()[0]["trendline"]["id"], "NEW_TL")

    def test_auto_archive_closed_position(self):
        self.pos_store.add_position(
            "YBUSDT",
            entry_price=0.0500,
            amount=2000.0,
            source="BINANCE_SYNC",
            mark_price=0.0550,
            current_price=0.0540,
        )
        self.assertEqual(len(self.pos_store.get_open_positions()), 1)

        with patch.object(self.client, "get_active_positions", return_value=[]):
            with patch("tv_trendline_engine.get_layout_drawings", return_value={}) as load_drawings:
                res = bpc.sync_binance_positions(self.client, self.pos_store)
                load_drawings.assert_not_called()
                self.assertTrue(res["synced"])
                self.assertIn("YBUSDT", res["auto_closed"])
                self.assertEqual(len(self.pos_store.get_open_positions()), 0)
                history = self.pos_store.get_history()
                self.assertEqual(len(history), 1)
                self.assertEqual(history[0]["symbol"], "YBUSDT")
                self.assertEqual(history[0]["status"], "BINANCE_POSITION_CLOSED")
                self.assertEqual(history[0]["exit_price"], 0.0550)
                self.assertAlmostEqual(history[0]["realized_pnl_pct"], 10.0, places=2)

    def test_manual_positions_not_auto_closed(self):
        self.pos_store.add_position("MANUALUSDT", entry_price=1.5, amount=100.0)
        self.assertIsNone(self.pos_store.get_open_positions()[0].get("source"))

        with patch.object(self.client, "get_active_positions", return_value=[]):
            with patch("tv_trendline_engine.get_layout_drawings", return_value={}) as load_drawings:
                res = bpc.sync_binance_positions(self.client, self.pos_store)
                load_drawings.assert_not_called()
                self.assertEqual(len(res["auto_closed"]), 0)
                self.assertEqual(len(self.pos_store.get_open_positions()), 1)
                self.assertEqual(self.pos_store.get_open_positions()[0]["symbol"], "MANUALUSDT")


class TestCLIAndDaemonIntegration(unittest.TestCase):
    """3. Test error resilience and daemon evaluation cycle integration."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = Path(self.test_dir) / "test.sqlite3"
        db_store.publish_candidates(self.db_path, [])

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_failed_position_snapshot_does_not_archive_open_position(self):
        store = tad.PositionStore(self.db_path)
        store.add_position("YBUSDT", 1.0, amount=1.0, source="BINANCE_SYNC")
        client = bc.BinanceFuturesClient("dummy_key", "dummy_secret")

        with patch.object(
            client,
            "_send_signed_request",
            side_effect=bc.BinanceAPIError("err", code="NETWORK_ERROR"),
        ):
            with patch.object(bpc.tve, "get_layout_drawings", return_value={}):
                report = bpc.sync_binance_positions(client, store)

        self.assertFalse(report["synced"])
        self.assertEqual(report["reason"], "POSITION_SNAPSHOT_FAILED")
        reloaded = tad.PositionStore(self.db_path)
        self.assertEqual(len(reloaded.get_open_positions()), 1)
        self.assertEqual(reloaded.get_history(), [])

    @patch.dict(os.environ, clear=True)
    @patch.object(tad, "fetch_layout_drawings", return_value={})
    @patch.object(tad, "fetch_ticker_prices", return_value={})
    @patch.object(tad, "get_closed_15m_candle", side_effect=bc.BinanceAPIError("skip"))
    def test_run_evaluation_cycle_includes_binance_sync(self, _candle, _tickers, _drawings):
        env_file = Path(self.test_dir) / ".env"
        env_file.write_text(
            "BINANCE_API_KEY=dummy_key\nBINANCE_API_SECRET=dummy_secret\n",
            encoding="utf-8",
        )
        cfg = tad.load_config(env_path=env_file)
        cfg["database_path"] = self.db_path

        notifier = tad.ServerChanNotifier(dry_run=True)
        state_store = tad.ResonanceStateStore(self.db_path)
        pos_store = tad.PositionStore(self.db_path)

        mock_active_longs = [
            {
                "symbol": "YBUSDT",
                "amount": 1000.0,
                "entry_price": 0.050,
                "mark_price": 0.052,
                "unrealized_pnl": 2.0,
                "leverage": 10,
                "notional": 52.0,
            }
        ]

        with patch("binance_client.BinanceFuturesClient.get_active_positions", return_value=mock_active_longs):
            with patch("tv_trendline_engine.get_layout_drawings", return_value={}):
                report = tad.run_evaluation_cycle(notifier, state_store, pos_store, cfg)
                self.assertTrue(report["binance_synced"])
                self.assertEqual(report["binance_active_positions"], 1)


if __name__ == "__main__":
    unittest.main()
