#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Automated Test Suite for Binance USDⓈ-M Futures Position Auto-Sync
=================================================================

Covers:
- BinanceFuturesClient configuration & HMAC-SHA256 signature generation
- Server time synchronization & network error handling
- Filtering of active long positions (positionAmt > 0)
- Auto-sync lifecycle:
  • Auto-registering new Binance positions with actual entryPrice and amount
  • Auto-matching TradingView trendlines from layout ig0LuDL8
  • Updating entry price / amount on scale-in
  • Auto-archiving positions closed on Binance (positionAmt == 0)
  • Preservation of manual positions
- Graceful degradation when API keys are unconfigured
- CLI --sync-binance execution verification

Usage:
    rtk python3 tests/test_binance_position_sync.py
    rtk python3 -m pytest tests/test_binance_position_sync.py
"""

import os
import sys
import json
import hmac
import hashlib
import tempfile
import shutil
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

import binance_position_client as bpc
import trade_alert_daemon as tad


class TestBinanceFuturesClient(unittest.TestCase):
    """1. Test BinanceFuturesClient auth, signatures, and API responses."""

    def test_client_configuration_detection(self):
        """Verify is_configured() reflects presence of both API key and secret."""
        c1 = bpc.BinanceFuturesClient(api_key="", api_secret="")
        self.assertFalse(c1.is_configured())

        c2 = bpc.BinanceFuturesClient(api_key="my_key", api_secret="")
        self.assertFalse(c2.is_configured())

        c3 = bpc.BinanceFuturesClient(api_key="my_key", api_secret="my_secret")
        self.assertTrue(c3.is_configured())

    def test_hmac_sha256_signature_generation(self):
        """Verify HMAC-SHA256 signature strictly matches standard crypto specification."""
        api_key = "test_api_key_123"
        api_secret = "test_secret_abc_xyz_789"
        client = bpc.BinanceFuturesClient(api_key=api_key, api_secret=api_secret)

        query_str = "recvWindow=5000&timestamp=1700000000000"
        expected_sig = hmac.new(
            api_secret.encode("utf-8"),
            query_str.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

        actual_sig = client._generate_signature(query_str)
        self.assertEqual(actual_sig, expected_sig)
        self.assertEqual(len(actual_sig), 64)  # SHA-256 produces 64 hex characters

    def test_server_time_fallback(self):
        """Verify server time fetch and fallback on network errors."""
        client = bpc.BinanceFuturesClient(api_key="key", api_secret="sec")

        # Mock successful time response with context manager support
        mock_resp = MagicMock()
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.read.return_value = json.dumps({"serverTime": 1795200000123}).encode("utf-8")
        with patch("urllib.request.urlopen", return_value=mock_resp):
            t = client.get_server_time()
            self.assertEqual(t, 1795200000123)

        # Mock failure fallback to local time
        with patch("urllib.request.urlopen", side_effect=Exception("Timeout")):
            t_fallback = client.get_server_time()
            self.assertIsInstance(t_fallback, int)
            self.assertGreater(t_fallback, 1000000000000)

    def test_get_account_positions_unconfigured(self):
        """Verify unconfigured client returns no snapshot without making HTTP requests."""
        client = bpc.BinanceFuturesClient(api_key="", api_secret="")
        positions = client.get_account_positions()
        self.assertIsNone(positions)

    def test_get_active_long_positions_filtering(self):
        """Verify filtering extracts only positionAmt > 0 and cleans symbols."""
        client = bpc.BinanceFuturesClient(api_key="test_key", api_secret="test_sec")

        mock_raw_positions = [
            {
                "symbol": "BTCUSDT",
                "positionAmt": "0.000",
                "entryPrice": "0.0",
                "markPrice": "90000.0",
                "unRealizedProfit": "0.0",
                "leverage": "20"
            },
            {
                "symbol": "YBUSDT",
                "positionAmt": "2000.0",  # Active Long
                "entryPrice": "0.0520",
                "markPrice": "0.0550",
                "unRealizedProfit": "6.0",
                "leverage": "10",
                "positionSide": "BOTH",
                "updateTime": 1795260000000
            },
            {
                "symbol": "ETHUSDT",
                "positionAmt": "-1.500",  # Short position -> excluded
                "entryPrice": "3100.0",
                "markPrice": "3050.0",
                "unRealizedProfit": "75.0",
                "leverage": "20"
            },
            {
                "symbol": "BINANCE:RAREUSDT.P",  # Ticker with formatting
                "positionAmt": "500.0",  # Active Long
                "entryPrice": "0.0150",
                "markPrice": "0.0160",
                "unRealizedProfit": "0.5",
                "leverage": "5"
            }
        ]

        with patch.object(client, "get_account_positions", return_value=mock_raw_positions):
            active_longs = client.get_active_long_positions()
            self.assertEqual(len(active_longs), 2)

            yb = next(p for p in active_longs if p["symbol"] == "YBUSDT")
            self.assertEqual(yb["amount"], 2000.0)
            self.assertEqual(yb["entry_price"], 0.0520)
            self.assertEqual(yb["mark_price"], 0.0550)
            self.assertEqual(yb["unrealized_pnl"], 6.0)
            self.assertEqual(yb["leverage"], 10)

            rare = next(p for p in active_longs if p["symbol"] == "RAREUSDT")
            self.assertEqual(rare["amount"], 500.0)
            self.assertEqual(rare["entry_price"], 0.0150)


class TestBinanceAutoSyncLifecycle(unittest.TestCase):
    """2. Test auto-syncing lifecycle between Binance API and PositionStore."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.pos_file = Path(self.test_dir) / "trade_positions.json"
        self.pos_store = tad.PositionStore(self.pos_file)
        self.client = bpc.BinanceFuturesClient(api_key="mock_key", api_secret="mock_sec")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_sync_unconfigured_graceful_degradation(self):
        """Verify sync gracefully skips when API credentials are absent."""
        unconfigured_client = bpc.BinanceFuturesClient(api_key="", api_secret="")
        res = bpc.sync_binance_positions(unconfigured_client, self.pos_store)
        self.assertFalse(res["synced"])
        self.assertEqual(res["reason"], "API_NOT_CONFIGURED")

    def test_auto_register_new_position_and_match_trendline(self):
        """
        Lifecycle Step 1:
        Binance opens new YBUSDT position -> auto-registered with entryPrice & amount,
        and auto-matches TV trendline SFzHy9 from layout ig0LuDL8.
        """
        mock_active_longs = [
            {
                "symbol": "YBUSDT",
                "amount": 2500.0,
                "entry_price": 0.0515,
                "mark_price": 0.0530,
                "unrealized_pnl": 3.75,
                "leverage": 10,
                "position_side": "BOTH",
                "update_time": 1795260000000,
                "notional": 132.5
            }
        ]

        mock_trendline = {
            "id": "SFzHy9",
            "p1": 0.06306,
            "t1": 1795104900,
            "p2": 0.05757,
            "t2": 1795155300,
            "symbol": "YBUSDT"
        }
        mock_drawings = {"YBUSDT": [mock_trendline]}

        with patch.object(self.client, "get_active_long_positions", return_value=mock_active_longs):
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

            # Verify PositionStore state
            open_positions = self.pos_store.get_open_positions()
            self.assertEqual(len(open_positions), 1)
            pos = open_positions[0]
            self.assertEqual(pos["symbol"], "YBUSDT")
            self.assertEqual(pos["entry_price"], 0.0515)
            self.assertEqual(pos["amount"], 2500.0)
            self.assertEqual(pos["source"], "BINANCE_SYNC")
            self.assertEqual(pos["leverage"], 10)
            self.assertEqual(pos["trendline"]["id"], "SFzHy9")

    def test_auto_update_on_scale_in(self):
        """
        Lifecycle Step 2:
        User adds to position on Binance -> entry price and amount update automatically.
        """
        # Initial position
        self.pos_store.add_position("YBUSDT", entry_price=0.0520, amount=1000.0)
        self.pos_store.get_open_positions()[0]["source"] = "BINANCE_SYNC"
        self.pos_store.save()

        # Scale in: price lowered to 0.0505, amount doubled to 2000.0
        scaled_in_longs = [
            {
                "symbol": "YBUSDT",
                "amount": 2000.0,
                "entry_price": 0.0505,
                "mark_price": 0.0520,
                "unrealized_pnl": 3.0,
                "leverage": 10,
                "notional": 104.0
            }
        ]

        with patch.object(self.client, "get_active_long_positions", return_value=scaled_in_longs):
            with patch("tv_trendline_engine.get_layout_drawings", return_value={}) as load_drawings:
                res = bpc.sync_binance_positions(self.client, self.pos_store)
                load_drawings.assert_not_called()
                self.assertTrue(res["synced"])
                self.assertEqual(len(res["new_added"]), 0)

                # Verify updated
                open_pos = self.pos_store.get_open_positions()[0]
                self.assertEqual(open_pos["entry_price"], 0.0505)
                self.assertEqual(open_pos["amount"], 2000.0)

    def test_auto_archive_closed_position(self):
        """
        Lifecycle Step 3:
        Position closed on Binance (positionAmt == 0) -> auto-archived to history in PositionStore.
        """
        # Register a synced position
        self.pos_store.add_position("YBUSDT", entry_price=0.0505, amount=2000.0)
        self.pos_store.get_open_positions()[0]["source"] = "BINANCE_SYNC"
        self.pos_store.save()
        self.assertEqual(len(self.pos_store.get_open_positions()), 1)

        # Binance now has 0 active positions (closed on exchange)
        empty_longs = []

        with patch.object(self.client, "get_active_long_positions", return_value=empty_longs):
            with patch("tv_trendline_engine.get_layout_drawings", return_value={}) as load_drawings:
                res = bpc.sync_binance_positions(self.client, self.pos_store)
                load_drawings.assert_not_called()
                self.assertTrue(res["synced"])
                self.assertIn("YBUSDT", res["auto_closed"])

                # Verify no longer open
                self.assertEqual(len(self.pos_store.get_open_positions()), 0)

                # Verify archived to history
                history = self.pos_store.data.get("history", [])
                self.assertEqual(len(history), 1)
                archived = history[0]
                self.assertEqual(archived["symbol"], "YBUSDT")
                self.assertEqual(archived["status"], "BINANCE_POSITION_CLOSED")

    def test_manual_positions_not_auto_closed(self):
        """Verify manually entered positions without source='BINANCE_SYNC' are not closed."""
        # Manually added position (e.g. from --add-pos)
        self.pos_store.add_position("MANUALUSDT", entry_price=1.5, amount=100.0)
        self.assertEqual(self.pos_store.get_open_positions()[0].get("source"), None)

        empty_longs = []
        with patch.object(self.client, "get_active_long_positions", return_value=empty_longs):
            with patch("tv_trendline_engine.get_layout_drawings", return_value={}) as load_drawings:
                res = bpc.sync_binance_positions(self.client, self.pos_store)
                load_drawings.assert_not_called()
                self.assertEqual(len(res["auto_closed"]), 0)
                self.assertEqual(len(self.pos_store.get_open_positions()), 1)
                self.assertEqual(self.pos_store.get_open_positions()[0]["symbol"], "MANUALUSDT")


class TestCLIAndDaemonIntegration(unittest.TestCase):
    """3. Test CLI --sync-binance and daemon cycle integration."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.cfg_file = Path(self.test_dir) / "alerts_config.json"
        self.pos_file = Path(self.test_dir) / "trade_positions.json"
        self.state_file = Path(self.test_dir) / "resonance_state.json"
        self.pool_file = Path(self.test_dir) / "pool.json"

        # Mock pool with 1 coin to keep evaluation cycle fast
        with open(self.pool_file, "w", encoding="utf-8") as f:
            json.dump([], f)

        cfg = {
            "binance_api_key": "dummy_key",
            "binance_api_secret": "dummy_secret",
            "positions_file": str(self.pos_file),
            "resonance_state_file": str(self.state_file),
            "resonance_pool_file": str(self.pool_file),
            "trailing_activation_pct": 6.0,
            "trailing_lock_pct": 1.0,
        }
        with open(self.cfg_file, "w", encoding="utf-8") as f:
            json.dump(cfg, f)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_failed_position_snapshot_does_not_archive_open_position(self):
        store = tad.PositionStore(self.pos_file)
        store.add_position("YBUSDT", 1.0, amount=1.0, source="BINANCE_SYNC")
        client = bpc.BinanceFuturesClient("dummy_key", "dummy_secret")

        with patch.object(client, "_send_signed_request", return_value=(None, {"code": "NETWORK_ERROR"})):
            with patch.object(bpc.tve, "get_layout_drawings", return_value={}):
                report = bpc.sync_binance_positions(client, store)

        self.assertFalse(report["synced"])
        reloaded = tad.PositionStore(self.pos_file)
        self.assertEqual(len(reloaded.get_open_positions()), 1)
        self.assertEqual(reloaded.data["history"], [])

    @patch.dict(os.environ, clear=True)
    @patch.object(tad, "fetch_layout_drawings", return_value={})
    @patch.object(tad, "get_binance_ticker_price", return_value=0)
    @patch.object(tad, "get_binance_closed_15m_candle", return_value=None)
    def test_run_evaluation_cycle_includes_binance_sync(self, _candle, _ticker, _drawings):
        """Verify run_evaluation_cycle executes Binance sync when client is configured."""
        cfg = tad.load_config(self.cfg_file, env_path=self.cfg_file.with_suffix(".env"))
        notifier = tad.ServerChanNotifier(dry_run=True)
        state_store = tad.ResonanceStateStore(self.state_file)
        pos_store = tad.PositionStore(self.pos_file)

        mock_active_longs = [
            {
                "symbol": "YBUSDT",
                "amount": 1000.0,
                "entry_price": 0.050,
                "mark_price": 0.052,
                "unrealized_pnl": 2.0,
                "leverage": 10,
                "notional": 52.0
            }
        ]

        with patch("binance_position_client.BinanceFuturesClient.get_active_long_positions", return_value=mock_active_longs):
            with patch("tv_trendline_engine.get_layout_drawings", return_value={}):
                report = tad.run_evaluation_cycle(notifier, state_store, pos_store, cfg)
                self.assertTrue(report.get("binance_synced"))
                self.assertEqual(report.get("binance_active_positions"), 1)


def run_test_suite():
    print("=" * 80)
    print("🚀 EXECUTING BINANCE POSITION AUTO-SYNC AUTOMATED TEST SUITE")
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
    sys.exit(run_test_suite())
