#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for P0/P1 daemon, web, and systemd fixes."""

import io
import json
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from logging.handlers import RotatingFileHandler
from pathlib import Path
from unittest.mock import patch

ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import alert_risk_checks as arc  # noqa: E402
import binance_client as bc  # noqa: E402
import db_store  # noqa: E402
import trade_alert_daemon as tad  # noqa: E402
import tv_trendline_engine as tve  # noqa: E402


class TestDaemonTransactionDecouplingAndFixes(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = Path(self.test_dir) / "test.sqlite3"
        self.notifier = tad.ServerChanNotifier(dry_run=True)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _assert_sqlite_unlocked(self):
        """Prove no BEGIN IMMEDIATE write lock is currently held by writing from a second connection."""
        with db_store.transaction(self.db_path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS lock_probe (id INTEGER PRIMARY KEY)")

    def test_feature_1_releases_db_lock_during_http_and_guards_malformed_records(self):
        db_store.publish_candidates(
            self.db_path,
            [
                "not-a-dict",
                {"symbol": "", "is_resonance": True},
                {"symbol": "BADPRICE", "is_resonance": True, "price": -1},
                {"symbol": "BADTYPE", "is_resonance": True, "turnover_ratio": "nan_str"},
                {"symbol": "MICROUSDT", "is_resonance": True, "price": 0.00004567},
            ],
        )
        store = tad.ResonanceStateStore(self.db_path)
        store.data["initialized"] = True
        store.save()

        observed_unlock = []

        def fake_send(title, desp):
            self._assert_sqlite_unlocked()
            self.assertIsNone(store._tx_conn)
            observed_unlock.append((title, desp))
            return True

        with patch.object(self.notifier, "send_alert", side_effect=fake_send):
            alerts = tad.check_feature_1_resonance(self.notifier, store, database_path=self.db_path)

        self.assertEqual([a["symbol"] for a in alerts], ["MICROUSDT"])
        self.assertEqual(len(observed_unlock), 1)
        body = observed_unlock[0][1]
        self.assertIn("$0.00004567", body)
        self.assertIn("`--`", body)  # turnover_ratio & funding omitted -> '--', not 0.0x / +0.0000%
        self.assertIsNone(store.data["history"]["MICROUSDT"]["res_score"])

    def test_feature_2_releases_db_lock_and_does_not_break_on_failed_send(self):
        store = tad.ResonanceStateStore(self.db_path)
        store.data["initialized"] = True
        store.data["active_resonance_symbols"] = ["ETHUSDT", "YBUSDT"]
        store.save()

        now = 1795234500
        eth_line = {"id": "E1", "interval": "15", "p1": 3000.0, "t1": 1795104900, "p2": 2900.0, "t2": 1795155300}
        line1 = {"id": "L1", "interval": "15", "p1": 0.06306, "t1": 1795104900, "p2": 0.05757, "t2": 1795155300}
        line2 = {"id": "L2", "interval": "15", "p1": 0.06400, "t1": 1795104900, "p2": 0.05800, "t2": 1795155300}
        candle_eth = {
            "symbol": "ETHUSDT",
            "open": 2700.0,
            "close": 2800.0,
            "open_time": now,
            "close_time": now + 899,
            "candle_time": "2026-11-21 12:15",
            "prev_candle": {"close": 2650.0, "open_time": now - 900, "close_time": now - 1},
        }
        candle_yb = {
            "symbol": "YBUSDT",
            "open": 0.04982,
            "close": 0.05030,
            "open_time": now,
            "close_time": now + 899,
            "candle_time": "2026-11-21 12:15",
            "prev_candle": {"close": 0.04975, "open_time": now - 900, "close_time": now - 1},
        }

        def fake_drawings(_layout, **_kwargs):
            self._assert_sqlite_unlocked()
            self.assertIsNone(store._tx_conn)
            return {"ETHUSDT": [eth_line], "YBUSDT": [line1, line2]}

        def fake_candle(sym):
            self._assert_sqlite_unlocked()
            self.assertIsNone(store._tx_conn)
            return candle_eth if sym == "ETHUSDT" else candle_yb

        send_calls = []

        def fake_send(title, _desp):
            self._assert_sqlite_unlocked()
            self.assertIsNone(store._tx_conn)
            send_calls.append(title)
            return "YBUSDT" not in title

        with patch.object(tve, "get_layout_drawings", side_effect=fake_drawings), patch.object(
            arc, "get_closed_15m_candle", side_effect=fake_candle
        ), patch.object(self.notifier, "send_alert", side_effect=fake_send):
            alerts = tad.check_feature_2_breakout(self.notifier, store)

        self.assertEqual([a["symbol"] for a in alerts], ["ETHUSDT"])
        self.assertEqual(len(send_calls), 2)
        reloaded = tad.ResonanceStateStore(self.db_path)
        self.assertEqual(list(reloaded.data["pending_breakouts"].keys()), ["YBUSDT_L1"])
        self.assertTrue(reloaded.has_breakout_notified("ETHUSDT_E1"))
        self.assertFalse(reloaded.has_breakout_notified("YBUSDT_L2"))

    def test_feature_2_queues_at_most_one_pending_breakout_per_symbol_across_retries(self):
        store = tad.ResonanceStateStore(self.db_path)
        store.data["initialized"] = True
        store.data["active_resonance_symbols"] = ["YBUSDT"]
        store.save()

        now = 1795234500
        line1 = {"id": "L1", "interval": "15", "p1": 0.06306, "t1": 1795104900, "p2": 0.05757, "t2": 1795155300}
        line2 = {"id": "L2", "interval": "15", "p1": 0.06400, "t1": 1795104900, "p2": 0.05800, "t2": 1795155300}
        candle = {
            "symbol": "YBUSDT",
            "open": 0.04982,
            "close": 0.05030,
            "open_time": now,
            "close_time": now + 899,
            "candle_time": "2026-11-21 12:15",
            "prev_candle": {"close": 0.04975, "open_time": now - 900, "close_time": now - 1},
        }

        with patch.object(tve, "get_layout_drawings", return_value={"YBUSDT": [line1, line2]}), patch.object(
            arc, "get_closed_15m_candle", return_value=candle
        ), patch.object(self.notifier, "send_alert", side_effect=[False, False, True]) as send_mock:
            # Cycle 1: L1 fails -> only YBUSDT_L1 queued, L2 not queued or notified
            self.assertEqual(tad.check_feature_2_breakout(self.notifier, store), [])
            self.assertEqual(list(tad.ResonanceStateStore(self.db_path).data["pending_breakouts"].keys()), ["YBUSDT_L1"])
            # Cycle 2: L1 retry fails -> L2 still blocked while YBUSDT has pending breakout
            self.assertEqual(tad.check_feature_2_breakout(self.notifier, store), [])
            self.assertEqual(list(tad.ResonanceStateStore(self.db_path).data["pending_breakouts"].keys()), ["YBUSDT_L1"])
            # Cycle 3: L1 retry succeeds -> L1 notified, L2 not also fired in same cycle
            self.assertEqual(tad.check_feature_2_breakout(self.notifier, store), [])
            reloaded = tad.ResonanceStateStore(self.db_path)
            self.assertEqual(reloaded.data["pending_breakouts"], {})
            self.assertTrue(reloaded.has_breakout_notified("YBUSDT_L1"))
            self.assertFalse(reloaded.has_breakout_notified("YBUSDT_L2"))
            self.assertEqual(send_mock.call_count, 3)

    def test_daemon_feature_4_uses_ticker_last_price_consistently_with_check_once(self):
        cfg_map = {
            "serverchan_sendkey": "",
            "binance_api_key": "k",
            "binance_api_secret": "s",
            "tradingview_layout_id": "ig0LuDL8",
            "trailing_activation_pct": 6.0,
            "trailing_lock_pct": 1.0,
            "database_path": str(self.db_path),
        }
        daemon = tad.TradeAlertDaemon(cfg_map, dry_run=True)
        active_longs = [
            {
                "symbol": "ILLIQUSDT",
                "amount": 100.0,
                "entry_price": 1.00,
                "mark_price": 1.00,  # mark_price lagged at +0%
                "unrealized_pnl": 0.0,
                "leverage": 5,
                "notional": 100.0,
            }
        ]
        with patch.object(daemon.binance_client, "get_active_positions", return_value=active_longs), patch.object(
            tve, "get_layout_drawings", return_value={}
        ), patch.object(arc, "fetch_ticker_prices", return_value={"ILLIQUSDT": 1.08}) as mock_tickers, patch.object(
            daemon.notifier, "send_alert", return_value=True
        ) as mock_send:
            daemon.run_iteration(now=1800000005)
        mock_tickers.assert_called_once_with(["ILLIQUSDT"])
        self.assertEqual(mock_send.call_count, 1)
        pos = tad.PositionStore(self.db_path).get_open_positions()[0]
        self.assertTrue(pos["trailing_stop_triggered"])
        self.assertEqual(pos["current_price"], 1.08)
        self.assertEqual(pos["highest_price"], 1.08)

    def test_feature_3_releases_db_lock_requires_open_time_and_retries_only_pending(self):
        pos_store = tad.PositionStore(self.db_path)
        tl = {"id": "SL1", "interval": "15", "p1": 0.00010, "t1": 0, "p2": 0.00008, "t2": 100}
        pos_store.add_position("MICRO1USDT", entry_price=0.00009, amount=1000.0, trendline=tl, source="BINANCE_SYNC")
        pos_store.add_position("SAFE2USDT", entry_price=1.0, amount=10.0, trendline={"id": "SL2", "interval": "15", "p1": 0.5, "t1": 0, "p2": 0.4, "t2": 100}, source="BINANCE_SYNC")

        # Missing open_time must be skipped with had_error=True rather than falling back to close_time
        bad_candle = {"symbol": "MICRO1USDT", "close_time": 50, "close": 0.00005}
        st_bad = {}
        with patch.object(
            arc,
            "get_closed_15m_candle",
            side_effect=lambda sym: bad_candle if sym == "MICRO1USDT" else {"symbol": "SAFE2USDT", "open_time": 50, "close_time": 949, "close": 1.2},
        ):
            res_bad = tad.check_feature_3_stop_loss(self.notifier, pos_store, status=st_bad)
        self.assertEqual(res_bad, [])
        self.assertTrue(st_bad.get("had_error"))

        # Now provide valid open_time and fail send_alert -> sets had_error=True & pending_stop_loss_candle
        hit_candle = {"symbol": "MICRO1USDT", "open_time": 50, "close_time": 949, "close": 0.00005}
        safe_candle = {"symbol": "SAFE2USDT", "open_time": 50, "close_time": 949, "close": 1.2}

        def fake_send(_title, desp):
            self._assert_sqlite_unlocked()
            self.assertIsNone(pos_store._tx_conn)
            self.assertIn("$0.00005000", desp)
            return False

        st_fail = {}
        with patch.object(
            arc,
            "get_closed_15m_candle",
            side_effect=lambda sym: hit_candle if sym == "MICRO1USDT" else safe_candle,
        ), patch.object(
            self.notifier, "send_alert", side_effect=fake_send
        ):
            alerts = tad.check_feature_3_stop_loss(self.notifier, pos_store, status=st_fail)
        self.assertEqual(alerts, [])
        self.assertTrue(st_fail.get("had_error"))

        # Daemon intra-bucket retry must pass retry_only=True so SAFE2USDT does NOT hit Binance/TV every 10s
        daemon = tad.TradeAlertDaemon({"database_path": str(self.db_path)}, dry_run=True)
        with patch.object(arc, "get_closed_15m_candle", side_effect=AssertionError("must not fetch klines on retry_only")), patch.object(
            tve, "get_layout_drawings", side_effect=AssertionError("must not fetch drawings on retry_only")
        ), patch.object(daemon.notifier, "send_alert", return_value=True) as mock_send, patch.object(
            tad, "check_feature_4_trailing_stops", return_value=[]
        ):
            daemon.run_iteration(now=1800000005)  # seconds_into_bucket < 10 -> else branch
        self.assertEqual(mock_send.call_count, 1)
        self.assertTrue(tad.PositionStore(self.db_path).get_open_positions()[0]["stop_loss_notified"])

    def test_feature_4_releases_db_lock_handles_none_highest_and_micro_price(self):
        db_store.save_open_positions(
            self.db_path,
            [
                {
                    "symbol": "PEPEUSDT",
                    "entry_price": 0.00004000,
                    "amount": 100000.0,
                    "highest_price": None,
                    "trailing_stop_triggered": False,
                    "profit_lock_notified": False,
                    "status": "OPEN",
                }
            ],
        )
        pos_store = tad.PositionStore(self.db_path)

        def fake_tickers(_syms):
            self._assert_sqlite_unlocked()
            self.assertIsNone(pos_store._tx_conn)
            return {"PEPEUSDT": 0.00004500}

        sent = []

        def fake_send(_title, desp):
            self._assert_sqlite_unlocked()
            self.assertIsNone(pos_store._tx_conn)
            sent.append(desp)
            return True

        with patch.object(arc, "fetch_ticker_prices", side_effect=fake_tickers), patch.object(
            self.notifier, "send_alert", side_effect=fake_send
        ):
            events = tad.check_feature_4_trailing_stops(self.notifier, pos_store, activation_pct=6.0, lock_pct=1.0)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "TRAILING_ACTIVATED")
        self.assertIn("$0.00004500", sent[0])

    def test_idle_daemon_tick_skips_begin_immediate_and_tracks_state_freshness(self):
        db_store.publish_candidates(
            self.db_path,
            [{"symbol": "BTCUSDT", "is_resonance": False}],
            updated_at="2026-11-25 12:00:00+08:00",
        )
        db_store.save_resonance_state(
            self.db_path,
            {
                "initialized": True,
                "active_resonance_symbols": ["YBUSDT"],
                "notified_breakout_keys": [],
                "notified_breakout_times": {},
                "pending_breakouts": {},
                "history": {},
            },
        )
        daemon = tad.TradeAlertDaemon({"database_path": str(self.db_path)}, dry_run=True)
        daemon._last_pool_stamp = db_store.load_candidate_version(self.db_path)
        self.assertTrue(daemon.state_store._state_loaded)

        with patch.object(db_store, "transaction", wraps=db_store.transaction) as tx_spy, patch.object(
            tad, "check_feature_4_trailing_stops", wraps=tad.check_feature_4_trailing_stops
        ) as f4_spy, patch.object(
            daemon.state_store, "_apply_raw_payload", wraps=daemon.state_store._apply_raw_payload
        ) as decode_spy:
            daemon.run_iteration(now=1800000005)  # idle else-branch, no open positions
            daemon.run_iteration(now=1800000006)  # repeated idle tick with unchanged resonance_state

        self.assertEqual(tx_spy.call_count, 0)
        self.assertEqual(f4_spy.call_count, 0)
        self.assertEqual(decode_spy.call_count, 0)

        # External DB mutation must still take effect immediately on the next idle tick
        now_sec = 1795234500
        db_store.save_resonance_state(
            self.db_path,
            {
                "initialized": True,
                "active_resonance_symbols": ["YBUSDT"],
                "notified_breakout_keys": [],
                "notified_breakout_times": {},
                "pending_breakouts": {
                    "YBUSDT_L1": {
                        "symbol": "YBUSDT",
                        "res_price": 0.05,
                        "close_price": 0.052,
                        "pct": 4.0,
                        "candle": {"open_time": now_sec, "close_time": now_sec + 899, "candle_time": "2026-11-21 12:15"},
                        "layout_id": "ig0LuDL8",
                        "keys": ["YBUSDT_L1"],
                    }
                },
                "history": {},
            },
        )
        with patch.object(
            daemon.state_store, "_apply_raw_payload", wraps=daemon.state_store._apply_raw_payload
        ) as decode_spy2, patch.object(daemon.notifier, "send_alert", return_value=True) as mock_send:
            daemon.run_iteration(now=1800000007)

        self.assertEqual(decode_spy2.call_count, 3)  # 1x load(only_if_changed=True) + 2x transaction reloads in retry_only
        self.assertEqual(mock_send.call_count, 1)
        reloaded = tad.ResonanceStateStore(self.db_path)
        self.assertEqual(reloaded.data.get("pending_breakouts"), {})
        self.assertTrue(reloaded.has_breakout_notified("YBUSDT_L1"))

    def test_daemon_start_reuses_shared_ro_conn_and_detects_external_writes_via_data_version(self):
        db_store.publish_candidates(
            self.db_path,
            [{"symbol": "BTCUSDT", "is_resonance": False}],
            updated_at="2026-11-25 12:00:00+08:00",
        )
        daemon = tad.TradeAlertDaemon({"database_path": str(self.db_path)}, dry_run=True)
        daemon.state_store.data["initialized"] = True
        daemon.state_store.save()
        sleep_calls = 0

        def fake_sleep(_sec):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls == 2:
                # Simulate external writer committing a new resonance candidate snapshot
                db_store.publish_candidates(
                    self.db_path,
                    [{"symbol": "NEWUSDT", "is_resonance": True}],
                    updated_at="2026-11-25 12:05:00+08:00",
                )
            elif sleep_calls >= 3:
                daemon.running = False

        with patch.object(tad.signal, "signal"), patch.object(
            tad,
            "run_evaluation_cycle",
            return_value={"feature_2_had_error": False, "feature_3_had_error": False},
        ), patch.object(tad.time, "sleep", side_effect=fake_sleep), patch.object(
            tad.time, "time", return_value=1800000005
        ), patch.object(
            db_store, "load_candidate_version", wraps=db_store.load_candidate_version
        ) as ver_spy, patch.object(
            daemon.pos_store, "load", wraps=daemon.pos_store.load
        ) as pos_load_spy, patch.object(
            daemon.state_store, "load", wraps=daemon.state_store.load
        ) as state_load_spy, patch.object(
            daemon.notifier, "send_alert", return_value=True
        ) as send_spy:
            daemon.start()

        # Startup sets _last_pool_stamp (1x ver_spy); Tick 1 reads DB (1x); Tick 2 skips all 3 via data_version; Tick 3 detects external write (1x + f1 tx load)
        self.assertEqual(ver_spy.call_count, 3)  # startup + Tick 1 + Tick 3 (Tick 2 skipped)
        self.assertEqual(pos_load_spy.call_count, 2)  # Tick 1 + Tick 3 (Tick 2 skipped)
        self.assertEqual(state_load_spy.call_count, 4)  # Tick 1 + Tick 3 f1 (2 tx loads) + Tick 3 else-load (Tick 2 skipped)
        self.assertEqual(send_spy.call_count, 1)
        self.assertIsNone(daemon._shared_ro_conn)

    def test_feature_4_skips_transaction_when_no_prices_and_no_pending_alerts(self):
        db_store.save_open_positions(
            self.db_path,
            [{"symbol": "PEPEUSDT", "entry_price": 0.00004, "amount": 100.0, "status": "OPEN"}],
        )
        pos_store = tad.PositionStore(self.db_path)
        with patch.object(arc, "fetch_ticker_prices", side_effect=bc.BinanceAPIError("down")), patch.object(
            pos_store, "transaction", wraps=pos_store.transaction
        ) as tx_spy:
            events = tad.check_feature_4_trailing_stops(self.notifier, pos_store)
        self.assertEqual(events, [])
        self.assertEqual(tx_spy.call_count, 0)

    def test_empty_tv_cache_honored_list_pos_warns_and_rotating_log(self):
        layout_id = "empty-layout"
        with tempfile.TemporaryDirectory() as directory:
            runs_dir = Path(directory)
            cache_file = runs_dir / f"tradingview_drawings_{layout_id}.json"
            cache_file.write_text(json.dumps({"drawings_by_symbol": {}}), encoding="utf-8")
            with patch.object(tve, "RUNS_DIR", runs_dir), patch.object(
                tve, "_DRAWINGS_CACHE", {}
            ), patch.object(tve, "get_chart_token", side_effect=AssertionError("should honor empty cache")):
                self.assertEqual(tve.get_layout_drawings(layout_id, cache_ttl_sec=60), {})

        pos_store = tad.PositionStore(self.db_path)
        pos_store.add_position("YBUSDT", 0.05, amount=10.0)
        buf = io.StringIO()
        with patch.object(tad, "configure_logging", return_value=[]), patch.object(
            tad, "load_config", return_value={"database_path": self.db_path}
        ), patch.object(tad, "fetch_ticker_prices", side_effect=bc.BinanceAPIError("down")), patch.object(
            sys, "argv", ["trade_alert_daemon.py", "--list-pos"]
        ), redirect_stdout(buf):
            tad.main()
        self.assertIn("实时行情拉取失败", buf.getvalue())

        log_file = Path(self.test_dir) / "daemon.log"
        handlers = tad.configure_logging(log_file=log_file)
        try:
            self.assertTrue(any(isinstance(h, RotatingFileHandler) for h in handlers))
        finally:
            tad.close_managed_handlers(handlers)

    def test_web_and_systemd_static_contracts(self):
        unit = (ROOT_DIR / "deploy/systemd/ai-coin-selector-account-api.service").read_text(encoding="utf-8")
        self.assertIn("ConditionPathExists=/opt/ai-coin-selector/.env", unit)
        self.assertIn("Environment=COIN_DATABASE=/opt/ai-coin-selector/runs/account.sqlite3", unit)

        css = (ROOT_DIR / "web/styles.css").read_text(encoding="utf-8")
        self.assertIn("tr.resonance-neg-row > td:first-child", css)
        self.assertIn("tr.resonance-gold-row > td:first-child", css)

        index_html = (ROOT_DIR / "web/index.html").read_text(encoding="utf-8")
        self.assertLess(
            index_html.index("static/kline-modal.js"),
            index_html.index("static/candidates-render.js"),
        )
        self.assertLess(
            index_html.index("static/candidates-render.js"),
            index_html.index("static/candidates.js"),
        )

        common_js = (ROOT_DIR / "web/common.js").read_text(encoding="utf-8")
        self.assertIn("toBeijingDate", common_js)
        cand_js = (
            (ROOT_DIR / "web/candidates-render.js").read_text(encoding="utf-8")
            + "\n"
            + (ROOT_DIR / "web/candidates.js").read_text(encoding="utf-8")
        )
        self.assertLess(cand_js.index("const prevSortVal"), cand_js.index("c.turnover_ratio = trVal"))
        self.assertIn("formatBeijingTime", cand_js)
        self.assertIn("checkSnapshotVersion().then", cand_js)

        modal_js = (ROOT_DIR / "web/kline-modal.js").read_text(encoding="utf-8")
        self.assertIn("else if (modalHoverIndex !== -1)", modal_js)

        acct_js = (
            (ROOT_DIR / "web/account-tables.js").read_text(encoding="utf-8")
            + "\n"
            + (ROOT_DIR / "web/account-charts.js").read_text(encoding="utf-8")
            + "\n"
            + (ROOT_DIR / "web/account.js").read_text(encoding="utf-8")
        )
        self.assertIn("mode === 'OFFLINE_FALLBACK'", acct_js)
        self.assertIn("接口状态异常", acct_js)
        self.assertIn("p.risk_monitored === true", acct_js)
        self.assertIn("Invalid /api/account payload", acct_js)
        self.assertNotIn("renderIncomeBanner", acct_js)
        self.assertNotIn("income-page-full-banner", acct_js)


class TestDaemonAndWebFailFastContracts(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = Path(self.test_dir) / "test.sqlite3"
        self.notifier = tad.ServerChanNotifier(dry_run=True)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_position_store_requires_status_field(self):
        db_store.save_open_positions(self.db_path, [{"symbol": "NOSTATUSUSDT", "entry_price": 1.0}])
        store = tad.PositionStore(self.db_path)
        with self.assertRaises(KeyError):
            store.get_open_positions()
        with self.assertRaises(KeyError):
            store.close_position("NOSTATUSUSDT")

    def test_feature_1_rejects_malformed_zones_and_status(self):
        db_store.publish_candidates(
            self.db_path,
            [
                {"symbol": "BADZONES", "is_resonance": True, "zones": "not-a-list"},
                {"symbol": "BADSTATUS", "is_resonance": True, "status": 123},
                {"symbol": "OKUSDT", "is_resonance": True, "price": 1.2, "zones": ["双核黄金共振"]},
            ],
        )
        store = tad.ResonanceStateStore(self.db_path)
        store.data["initialized"] = True
        store.save()
        alerts = tad.check_feature_1_resonance(self.notifier, store, database_path=self.db_path)
        self.assertEqual([a["symbol"] for a in alerts], ["OKUSDT"])

    def test_breakout_notification_requires_timestamp_and_honors_zero(self):
        with self.assertRaises(KeyError):
            arc._send_breakout_notification(
                self.notifier, "YBUSDT", 1.0, 1.1, 10.0, {}, "ig0LuDL8"
            )
        sent = []
        with patch.object(self.notifier, "send_alert", side_effect=lambda t, d: sent.append(d) or True):
            ok = arc._send_breakout_notification(
                self.notifier, "YBUSDT", 1.0, 1.1, 10.0, {"open_time": 0}, "ig0LuDL8"
            )
        self.assertTrue(ok)
        self.assertIn("1970-01-01 08:00:00", sent[0])

    def test_feature_3_requires_amount_and_does_not_mask_corrupt_pending_candle(self):
        tl = {"id": "SL1", "interval": "15", "p1": 2.0, "t1": 0, "p2": 1.5, "t2": 100}
        db_store.save_open_positions(
            self.db_path,
            [
                {
                    "symbol": "CORRUPTPENDINGUSDT",
                    "entry_price": 1.8,
                    "amount": 10.0,
                    "status": "OPEN",
                    "trendline": tl,
                    "pending_stop_loss_candle": {},
                }
            ],
        )
        pos_store = tad.PositionStore(self.db_path)
        st = {}
        with patch.object(arc, "get_closed_15m_candle", side_effect=AssertionError("must not bypass corrupt pending")):
            res = tad.check_feature_3_stop_loss(self.notifier, pos_store, status=st)
        self.assertEqual(res, [])
        self.assertTrue(st.get("had_error"))

        db_store.save_open_positions(
            self.db_path,
            [
                {
                    "symbol": "NOAMTUSDT",
                    "entry_price": 1.8,
                    "status": "OPEN",
                    "trendline": tl,
                }
            ],
        )
        pos_store2 = tad.PositionStore(self.db_path)
        with patch.object(
            arc,
            "get_closed_15m_candle",
            return_value={"symbol": "NOAMTUSDT", "open_time": 50, "close_time": 949, "close": 1.0},
        ):
            with self.assertRaises(KeyError):
                tad.check_feature_3_stop_loss(self.notifier, pos_store2)

    def test_feature_4_fails_fast_on_corrupt_trailing_stop_or_ticker(self):
        db_store.save_open_positions(
            self.db_path,
            [
                {
                    "symbol": "BADLOCKUSDT",
                    "entry_price": 1.0,
                    "amount": 10.0,
                    "status": "OPEN",
                    "trailing_stop_triggered": True,
                    "trailing_stop_price": None,
                    "profit_lock_notified": False,
                }
            ],
        )
        pos_store = tad.PositionStore(self.db_path)
        with patch.object(arc, "fetch_ticker_prices", return_value={"BADLOCKUSDT": 1.05}):
            with self.assertRaises(ValueError):
                tad.check_feature_4_trailing_stops(self.notifier, pos_store)

        db_store.save_open_positions(
            self.db_path,
            [{"symbol": "TICKUSDT", "entry_price": 1.0, "amount": 10.0, "status": "OPEN"}],
        )
        pos_store2 = tad.PositionStore(self.db_path)
        with patch.object(arc, "fetch_ticker_prices", return_value={"TICKUSDT": 0.0}):
            with self.assertRaises(ValueError):
                tad.check_feature_4_trailing_stops(self.notifier, pos_store2)

    def test_format_positions_table_requires_fields_and_does_not_coerce_pnl(self):
        table = tad.format_positions_table(
            [
                {
                    "symbol": "YBUSDT",
                    "entry_price": 1.25,
                    "pnl_pct": None,
                    "trailing_stop_price": None,
                    "status": "OPEN",
                    "entry_time": "2026-11-25 12:00:00",
                }
            ]
        )
        self.assertIn("--", table)
        self.assertNotIn("+0.00%", table)
        with self.assertRaises(KeyError):
            tad.format_positions_table([{"entry_price": 1.25, "status": "OPEN"}])

    def test_web_js_runtime_fail_fast_and_non_coercion(self):
        node_bin = shutil.which("node")
        self.assertIsNotNone(node_bin, "node is required to verify web/*.js runtime behavior")
        script = textwrap.dedent(
            """
            const assert = require('assert');
            const fs = require('fs');
            const path = require('path');
            const vm = require('vm');

            const root = process.cwd();
            const commonCode = fs.readFileSync(path.join(root, 'web/common.js'), 'utf8');
            const klineModalCode = fs.readFileSync(path.join(root, 'web/kline-modal.js'), 'utf8');
            const candRenderCode = fs.readFileSync(path.join(root, 'web/candidates-render.js'), 'utf8');
            const candCode = fs.readFileSync(path.join(root, 'web/candidates.js'), 'utf8');
            const acctTablesCode = fs.readFileSync(path.join(root, 'web/account-tables.js'), 'utf8');
            const acctChartsCode = fs.readFileSync(path.join(root, 'web/account-charts.js'), 'utf8');
            const acctCode = fs.readFileSync(path.join(root, 'web/account.js'), 'utf8');

            // 1. common.js getTvSymbol & getTvUrl fail fast on empty symbol / unconfigured layout ID
            {
              const win = {};
              win.window = win;
              vm.runInNewContext(commonCode, win);
              assert.throws(() => win.SecondLegCommon.getTvSymbol(''), /non-empty symbol/);
              assert.strictEqual(win.SecondLegCommon.getTvSymbol('BTCUSDT'), 'BINANCE:BTCUSDT.P');
              assert.throws(() => win.SecondLegCommon.getTvUrl('BTCUSDT'), /not been configured/);
              win.SecondLegCommon.setTvLayoutId('TestLayout9');
              assert.strictEqual(
                win.SecondLegCommon.getTvUrl('BTCUSDT', null, '15m'),
                'https://cn.tradingview.com/chart/TestLayout9/?symbol=BINANCE%3ABTCUSDT.P&interval=15'
              );
            }

            // 2. candidates.js: null funding_rate_4h_pct / turnover_ratio skipped in KPI, corrupt sessionStorage warns & clears, malformed /api/candidates fails fast
            (async () => {
              const elements = {};
              const makeEl = (id) => ({
                id,
                textContent: '',
                innerHTML: '',
                title: '',
                className: '',
                value: '',
                hidden: false,
                dataset: {},
                style: {},
                classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
                addEventListener() {},
                removeEventListener() {},
                setAttribute() {},
                removeAttribute() {},
                getAttribute() { return null; },
                querySelector() { return null; },
                querySelectorAll() { return []; },
              });
              const getEl = (id) => (elements[id] ||= makeEl(id));
              const warnings = [];
              const errors = [];
              const storage = { slq_candidates_ui_state: '{bad json' };
              let removedKey = null;
              let payloadToServe = null;
              const win = {
                document: {
                  hidden: false,
                  readyState: 'complete',
                  activeElement: null,
                  getElementById: getEl,
                  querySelectorAll: () => [],
                  addEventListener() {},
                },
                sessionStorage: {
                  getItem(k) { return Object.prototype.hasOwnProperty.call(storage, k) ? storage[k] : null; },
                  setItem(k, v) { storage[k] = v; },
                  removeItem(k) { removedKey = k; delete storage[k]; },
                },
                fetch: async (url) => ({
                  ok: true,
                  status: 200,
                  headers: { get: () => null },
                  text: async () => JSON.stringify(payloadToServe),
                }),
                AbortController,
                DOMException,
                setTimeout,
                clearTimeout,
                setInterval: () => 1,
                clearInterval() {},
                addEventListener() {},
                matchMedia: () => ({ matches: false }),
                console: {
                  warn: (...a) => warnings.push(a.join(' ')),
                  error: (...a) => errors.push(a.join(' ')),
                },
              };
              win.window = win;
              vm.runInNewContext(commonCode, win);
              vm.runInNewContext(klineModalCode, win);
              vm.runInNewContext(candRenderCode, win);
              // Expose loadCandidateSnapshot via trigger or run directly by checking initial error path
              payloadToServe = {
                updated_at: '2026-11-25T12:00:00+08:00',
                candidates: [
                  { symbol: 'NULLFRUSDT', is_neg_funding: true, funding_rate_4h_pct: null, is_high_turnover: true, turnover_ratio: null, is_ema_bull: false, is_resonance: false },
                  { symbol: 'REALFRUSDT', is_neg_funding: true, funding_rate_4h_pct: -0.01, is_high_turnover: true, turnover_ratio: 2.5, is_ema_bull: false, is_resonance: false },
                ],
              };
              vm.runInNewContext(candCode, win);
              assert.strictEqual(removedKey, 'slq_candidates_ui_state');
              assert.ok(warnings.some((w) => w.includes('Corrupt UI state')));

              // Test KPI sampling via fetch inside loadCandidateSnapshot (run a fresh context that calls loadCandidateSnapshot)
              const kpiEls = {};
              const win2 = {
                ...win,
                document: {
                  hidden: false,
                  readyState: 'complete',
                  activeElement: null,
                  getElementById: (id) => (kpiEls[id] ||= makeEl(id)),
                  querySelectorAll: () => [],
                  addEventListener() {},
                },
                sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
                fetch: async (url) => {
                  if (url === 'api/candidates') {
                    return { ok: true, status: 200, headers: { get: () => null }, text: async () => JSON.stringify(payloadToServe) };
                  }
                  return { ok: true, status: 200, headers: { get: () => null }, text: async () => '[]' };
                },
              };
              win2.window = win2;
              vm.runInNewContext(commonCode, win2);
              win2.SecondLegCommon.setTvLayoutId('ig0LuDL8');
              vm.runInNewContext(klineModalCode, win2);
              vm.runInNewContext(candRenderCode, win2);
              const candTestCode = candCode.replace(
                /\\}\\)\\(\\);\\s*$/,
                'window.__candTest = { updateKpiCards, loadCandidateSnapshot, getEffectiveSupply, formatUpdatedAtBeijing };})();'
              );
              vm.runInNewContext(candTestCode, win2);
              win2.__candTest.updateKpiCards(payloadToServe.candidates);
              assert.strictEqual(kpiEls['kpi-neg-fr-sample'].textContent, '贴水深: REALFR(-0.01%)');
              assert.strictEqual(kpiEls['kpi-high-tr-sample'].textContent, '最高: REALFR(2.5x)');
              assert.strictEqual(win2.__candTest.formatUpdatedAtBeijing('2026-11-25T12:00:00+08:00'), '2026-11-25 12:00:00 (北京时间)');
              assert.strictEqual(win2.__candTest.getEffectiveSupply({ circulating_supply: 5000000, multiplier: 1000 }), 5000);
              assert.strictEqual(win2.__candTest.getEffectiveSupply({ market_cap_usd: 1000000, price: 2 }), 0);

              // Malformed candidate item (missing symbol) must fail fast
              payloadToServe = { updated_at: '2026-11-25T12:00:00+08:00', candidates: [{ price: 1.0 }] };
              await win2.__candTest.loadCandidateSnapshot({ force: true });
              assert.strictEqual(kpiEls['pool-updated-time'].textContent, '候选池加载失败');
            })().then(() => {
              // 3. account.js: daily_pnl missing net_pnl must throw rather than coerce to 0; missing summary.open_trades shows '--'
              const acctEls = {};
              const makeAcctEl = (id) => ({
                id,
                textContent: '',
                innerHTML: '',
                value: '',
                hidden: false,
                dataset: {},
                style: {},
                classList: { add() {}, remove() {}, toggle() {} },
                addEventListener() {},
              });
              function FakeChart(canvas, config) {
                this.data = config.data;
                this.options = config.options;
                this.update = () => {};
              }
              FakeChart.defaults = { color: '', font: {}, borderColor: '' };
              const win3 = {
                document: {
                  hidden: false,
                  readyState: 'loading',
                  getElementById: (id) => (acctEls[id] ||= makeAcctEl(id)),
                  querySelectorAll: () => [],
                  addEventListener() {},
                },
                Chart: FakeChart,
                fetch: async () => ({ ok: true, status: 200, headers: { get: () => null }, text: async () => '{}' }),
                AbortController,
                DOMException,
                setTimeout,
                clearTimeout,
                setInterval: () => 1,
                console: { error() {}, warn() {} },
              };
              win3.window = win3;
              vm.runInNewContext(commonCode, win3);
              win3.SecondLegCommon.setTvLayoutId('ig0LuDL8');
              vm.runInNewContext(acctTablesCode, win3);
              vm.runInNewContext(acctChartsCode, win3);
              const acctTestCode = acctCode.replace(
                /\\}\\)\\(\\);\\s*$/,
                'window.__acctTest = { renderDashboard, updateKpis };})();'
              );
              vm.runInNewContext(acctTestCode, win3);

              const validBase = {
                api_status: { mode: 'BINANCE_REALTIME' },
                summary: { win_count: 1, loss_count: 0, breakeven_count: 0 },
                active_positions: [{ symbol: 'BTCUSDT', side: 'LONG', risk_monitored: true }],
                trade_history: [],
                equity_curve: [],
                daily_pnl: [{ date: '2026-11-25' }],
                pnl_distribution: { bins: [] },
                income_breakdown: {},
              };
              assert.throws(() => win3.__acctTest.renderDashboard(validBase), /non-finite net_pnl/);

              win3.__acctTest.updateKpis({});
              assert.strictEqual(acctEls['kpi-open-trades'].textContent, '--');

              const missingHistory = { ...validBase, daily_pnl: [], trade_history: null };
              assert.throws(() => win3.__acctTest.renderDashboard(missingHistory), /Invalid \\/api\\/account payload/);
            }).catch((err) => {
              console.error(err);
              process.exit(1);
            });
            """
        )
        res = subprocess.run(
            [node_bin, "-e", script],
            cwd=str(ROOT_DIR),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0, msg=f"stdout={res.stdout}\nstderr={res.stderr}")

    def test_web_js_perf_optimizations_runtime(self):
        node_bin = shutil.which("node")
        self.assertIsNotNone(node_bin, "node is required to verify web/*.js perf behavior")
        script = textwrap.dedent(
            """
            const assert = require('assert');
            const fs = require('fs');
            const path = require('path');
            const vm = require('vm');

            const root = process.cwd();
            const commonCode = fs.readFileSync(path.join(root, 'web/common.js'), 'utf8');
            const klineModalCode = fs.readFileSync(path.join(root, 'web/kline-modal.js'), 'utf8');
            const candRenderCode = fs.readFileSync(path.join(root, 'web/candidates-render.js'), 'utf8');
            const candCode = fs.readFileSync(path.join(root, 'web/candidates.js'), 'utf8');
            const acctTablesCode = fs.readFileSync(path.join(root, 'web/account-tables.js'), 'utf8');
            const acctChartsCode = fs.readFileSync(path.join(root, 'web/account-charts.js'), 'utf8');
            const acctCode = fs.readFileSync(path.join(root, 'web/account.js'), 'utf8');

            (async () => {
              // 1. account.js: one-time Chart.defaults, upsertChart preserves options + dynamic tooltips,
              //    AbortController cancels superseded tryLoadData(), visibilitychange honors 5s cooldown.
              const acctEls = {};
              const makeAcctEl = (id) => ({
                id,
                textContent: '',
                innerHTML: '',
                value: '',
                hidden: false,
                dataset: {},
                style: {},
                classList: { add() {}, remove() {}, toggle() {} },
                addEventListener() {},
              });
              const createdCharts = {};
              const updateCalls = [];
              function FakeChart(canvas, config) {
                this.canvas = canvas;
                this.data = config.data;
                this.options = config.options;
                this.update = (mode) => updateCalls.push({ id: canvas.id, mode });
                createdCharts[canvas.id] = this;
              }
              let defaultsColorWrites = 0;
              let defaultsColorVal = '';
              FakeChart.defaults = {
                font: {},
                borderColor: '',
                get color() { return defaultsColorVal; },
                set color(v) { defaultsColorWrites += 1; defaultsColorVal = v; },
              };

              let fakeNow = 100000;
              class FakeDate extends Date {
                static now() { return fakeNow; }
              }
              const docListeners = {};
              const acctErrors = [];
              let fetchCallIndex = 0;
              const pendingResolvers = [];
              const abortedSignals = [];
              const acctPayload1 = {
                api_status: { mode: 'BINANCE_REALTIME' },
                generated_at: '2026-11-25 12:00:00',
                summary: { win_count: 1, loss_count: 0, breakeven_count: 0, initial_capital: 1000 },
                active_positions: [],
                trade_history: [],
                equity_curve: [{ datetime: '2026-11-25 12:00:00', equity: 1050 }],
                daily_pnl: [{ date: '2026-11-25', net_pnl: 50, trades_count: 1 }],
                pnl_distribution: { bins: [{ label: '0~5%', min: 0, max: 5, count: 1, trades: ['BTCUSDT'] }] },
                income_breakdown: {},
              };
              const acctPayload2 = {
                ...acctPayload1,
                generated_at: '2026-11-25 12:01:00',
                summary: { win_count: 2, loss_count: 0, breakeven_count: 0, initial_capital: 1000 },
                daily_pnl: [{ date: '2026-11-25', net_pnl: 80, trades_count: 2 }],
                pnl_distribution: { bins: [{ label: '0~5%', min: 0, max: 5, count: 2, trades: ['BTCUSDT', 'ETHUSDT'] }] },
              };

              const winAcct = {
                Date: FakeDate,
                document: {
                  hidden: false,
                  readyState: 'loading',
                  getElementById: (id) => (acctEls[id] ||= makeAcctEl(id)),
                  querySelectorAll: () => [],
                  addEventListener(ev, fn) { (docListeners[ev] ||= []).push(fn); },
                },
                Chart: FakeChart,
                fetch: (url, opts) => {
                  const idx = ++fetchCallIndex;
                  return new Promise((resolve, reject) => {
                    if (opts && opts.signal) {
                      opts.signal.addEventListener('abort', () => {
                        abortedSignals.push(idx);
                        reject(new DOMException('Aborted', 'AbortError'));
                      }, { once: true });
                    }
                    pendingResolvers.push({ idx, resolve });
                  });
                },
                AbortController,
                DOMException,
                setTimeout,
                clearTimeout,
                setInterval: () => 1,
                console: { error: (...a) => acctErrors.push(a.join(' ')), warn() {} },
              };
              winAcct.window = winAcct;
              vm.runInNewContext(commonCode, winAcct);
              vm.runInNewContext(acctTablesCode, winAcct);
              vm.runInNewContext(acctChartsCode, winAcct);
              const acctTestCode = acctCode.replace(
                /\\}\\)\\(\\);\\s*$/,
                'window.__acctTest = { renderDashboard, tryLoadData };})();'
              );
              vm.runInNewContext(acctTestCode, winAcct);

              // First render configures Chart.defaults once and creates chart instances
              winAcct.__acctTest.renderDashboard(acctPayload1);
              assert.strictEqual(defaultsColorWrites, 1);
              const eqOptionsRef = createdCharts['chart-equity-curve'].options;
              const dailyOptionsRef = createdCharts['chart-daily-pnl'].options;
              const distOptionsRef = createdCharts['chart-pnl-dist'].options;

              // Second render does NOT re-mutate Chart.defaults or overwrite chart.options
              winAcct.__acctTest.renderDashboard(acctPayload2);
              assert.strictEqual(defaultsColorWrites, 1);
              assert.strictEqual(createdCharts['chart-equity-curve'].options, eqOptionsRef);
              assert.strictEqual(createdCharts['chart-daily-pnl'].options, dailyOptionsRef);
              assert.strictEqual(createdCharts['chart-pnl-dist'].options, distOptionsRef);
              assert.ok(updateCalls.some((u) => u.id === 'chart-daily-pnl' && u.mode === 'none'));

              // Tooltip callbacks read currentAccountData dynamically (reflects acctPayload2, not acctPayload1)
              const dailyTooltip = dailyOptionsRef.plugins.tooltip.callbacks.label({ dataIndex: 0, raw: 80 });
              assert.deepStrictEqual(Array.from(dailyTooltip), ['当日净盈亏: +$80.00', '成交笔数: 2 笔']);
              const distTooltip = distOptionsRef.plugins.tooltip.callbacks.label({ dataIndex: 0, raw: 2 });
              assert.strictEqual(distTooltip, '笔数: 2 笔 (BTCUSDT, ETHUSDT)');

              // AbortController supersedes in-flight tryLoadData cleanly without logging errors
              const p1 = winAcct.__acctTest.tryLoadData();
              const p2 = winAcct.__acctTest.tryLoadData();
              assert.deepStrictEqual(abortedSignals, [1]);
              assert.strictEqual(pendingResolvers.length, 2);
              pendingResolvers[1].resolve({
                ok: true,
                status: 200,
                headers: { get: () => '"etag-2"' },
                text: async () => JSON.stringify(acctPayload2),
              });
              await Promise.all([p1, p2]);
              assert.strictEqual(acctErrors.length, 0);

              // visibilitychange within 5s cooldown must NOT trigger fetch; after 5s it MUST trigger fetch
              const beforeVisCalls = fetchCallIndex;
              docListeners.visibilitychange.forEach((fn) => fn());
              assert.strictEqual(fetchCallIndex, beforeVisCalls);

              fakeNow += 5001;
              const p3 = (docListeners.visibilitychange.forEach((fn) => fn()), pendingResolvers[pendingResolvers.length - 1]);
              assert.strictEqual(fetchCallIndex, beforeVisCalls + 1);
              p3.resolve({
                ok: true,
                status: 304,
                headers: { get: () => '"etag-2"' },
                text: async () => '',
              });
              await new Promise((r) => setTimeout(r, 0));

              // 2. candidates.js: matchMedia cached + change listener, redundant sort/DOM display skipped
              let matchMediaCalls = 0;
              let reducedMotionMatches = false;
              const mediaListeners = [];
              const candEls = {};
              const makeStyleTracker = () => {
                let displayVal = '';
                let displaySets = 0;
                const styleObj = {
                  get display() { return displayVal; },
                  set display(v) { displaySets += 1; displayVal = v; },
                  get _displaySets() { return displaySets; },
                };
                return styleObj;
              };
              const makeCandEl = (id) => ({
                id,
                textContent: '',
                innerHTML: '',
                title: '',
                className: '',
                value: '',
                hidden: false,
                dataset: {},
                style: makeStyleTracker(),
                classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
                addEventListener() {},
                removeEventListener() {},
                setAttribute() {},
                removeAttribute() {},
                getAttribute() { return null; },
                querySelector() { return null; },
                querySelectorAll() { return []; },
              });

              let rowAnimateCalls = 0;
              let appendChildCalls = 0;
              const rowEls = new Map();
              const makeRowEl = (sym) => {
                const rankEl = { textContent: '1' };
                const priceEl = {
                  textContent: '',
                  animate() { rowAnimateCalls += 1; },
                };
                const makeSub = () => ({ textContent: '', className: '', innerHTML: '', style: {}, setAttribute() {} });
                const el = {
                  sym,
                  className: '',
                  style: makeStyleTracker(),
                  getAttribute(k) { return k === 'data-sym' ? sym : null; },
                  querySelector(sel) {
                    if (sel === '.sym-rank') return rankEl;
                    if (sel === '.sym-price') return priceEl;
                    return makeSub();
                  },
                };
                rowEls.set(sym, el);
                return el;
              };
              const tableBodyEl = {
                ...makeCandEl('candidate-table-body'),
                set innerHTML(html) {
                  rowEls.clear();
                  const matches = [...html.matchAll(/data-sym="([^"]+)"/g)];
                  for (const m of matches) {
                    if (!rowEls.has(m[1])) makeRowEl(m[1]);
                  }
                },
                querySelectorAll(sel) {
                  if (sel === 'tr.candidate-row') return [...rowEls.values()];
                  return [];
                },
                appendChild() { appendChildCalls += 1; },
              };
              candEls['candidate-table-body'] = tableBodyEl;

              const candSnapshot = {
                updated_at: '2026-11-25T12:00:00+08:00',
                candidates: [
                  {
                    symbol: 'AAAUSDT',
                    price: 1.0,
                    high_24h: 1.2,
                    low_24h: 0.8,
                    price_chg_24h: 2.0,
                    market_cap_usd: 10000000,
                    volume_24h_usd: 20000000,
                    turnover_ratio: 2.0,
                    circulating_supply: 10000000,
                    multiplier: 1,
                    funding_interval_hours: 4,
                    funding_rate_pct: 0.01,
                    funding_rate_4h_pct: 0.01,
                    ema5: 1.02,
                    ema10: 1.0,
                    ema_spread_pct: 2.0,
                    is_ema_bull: true,
                    is_high_turnover: true,
                    is_neg_funding: false,
                    is_resonance: true,
                  },
                  {
                    symbol: 'BBBUSDT',
                    price: 2.0,
                    high_24h: 2.2,
                    low_24h: 1.8,
                    price_chg_24h: 1.0,
                    market_cap_usd: 20000000,
                    volume_24h_usd: 40000000,
                    turnover_ratio: 2.0,
                    circulating_supply: 10000000,
                    multiplier: 1,
                    funding_interval_hours: 4,
                    funding_rate_pct: 0.01,
                    funding_rate_4h_pct: 0.01,
                    ema5: 2.01,
                    ema10: 2.0,
                    ema_spread_pct: 0.5,
                    is_ema_bull: true,
                    is_high_turnover: true,
                    is_neg_funding: false,
                    is_resonance: true,
                  },
                ],
              };
              let tickerTick = 0;
              const winCand = {
                document: {
                  hidden: false,
                  readyState: 'complete',
                  activeElement: null,
                  createDocumentFragment: () => ({ appendChild() {} }),
                  getElementById: (id) => (candEls[id] ||= makeCandEl(id)),
                  querySelectorAll: () => [],
                  addEventListener() {},
                },
                sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
                fetch: async (url) => {
                  if (url === 'api/candidates') {
                    return { ok: true, status: 200, headers: { get: () => null }, text: async () => JSON.stringify(candSnapshot) };
                  }
                  if (url === 'api/binance/ticker24hr') {
                    const priceA = tickerTick === 0 ? '1.0' : (tickerTick === 1 ? '1.1' : '3.0');
                    return {
                      ok: true,
                      status: 200,
                      headers: { get: () => null },
                      text: async () => JSON.stringify([
                        { symbol: 'AAAUSDT', lastPrice: priceA, priceChangePercent: '2.0', highPrice: '1.2', lowPrice: '0.8', quoteVolume: '20000000' },
                        { symbol: 'BBBUSDT', lastPrice: '2.0', priceChangePercent: '1.0', highPrice: '2.2', lowPrice: '1.8', quoteVolume: '40000000' },
                      ]),
                    };
                  }
                  if (url === 'api/binance/premiumIndex') {
                    return {
                      ok: true,
                      status: 200,
                      headers: { get: () => null },
                      text: async () => JSON.stringify([
                        { symbol: 'AAAUSDT', lastFundingRate: '0.0001' },
                        { symbol: 'BBBUSDT', lastFundingRate: '0.0001' },
                      ]),
                    };
                  }
                  return { ok: true, status: 200, headers: { get: () => null }, text: async () => '{}' };
                },
                AbortController,
                DOMException,
                setTimeout,
                clearTimeout,
                setInterval: () => 1,
                clearInterval() {},
                addEventListener() {},
                matchMedia: () => {
                  matchMediaCalls += 1;
                  return {
                    get matches() { return reducedMotionMatches; },
                    addEventListener(ev, fn) { if (ev === 'change') mediaListeners.push(fn); },
                  };
                },
                console: { warn() {}, error() {} },
              };
              winCand.window = winCand;
              vm.runInNewContext(commonCode, winCand);
              winCand.SecondLegCommon.setTvLayoutId('ig0LuDL8');
              vm.runInNewContext(klineModalCode, winCand);
              vm.runInNewContext(candRenderCode, winCand);
              const candTestCode = candCode.replace(
                /\\}\\)\\(\\);\\s*$/,
                'window.__candTest = { syncBinancePool, applyFilter, loadCandidateSnapshot, setSort: (col) => { sortColumn = col; } };})();'
              );
              vm.runInNewContext(candTestCode, winCand);
              // Wait for initial loadCandidateSnapshot() + initial syncBinancePool()
              await new Promise((r) => setTimeout(r, 20));
              assert.strictEqual(matchMediaCalls, 1);

              // Redundant applyFilter() when visibility did NOT change must skip DOM style.display writes
              const setsAfterLoad = rowEls.get('AAAUSDT').style._displaySets;
              winCand.__candTest.applyFilter();
              assert.strictEqual(rowEls.get('AAAUSDT').style._displaySets, setsAfterLoad);

              // Price change triggers animation when prefersReducedMotion is false, and matchMedia is NOT re-called
              tickerTick = 1;
              await winCand.__candTest.syncBinancePool();
              assert.strictEqual(matchMediaCalls, 1);
              assert.strictEqual(rowAnimateCalls, 1);

              // Sort column 'ema' never changes in syncBinancePool -> applySortOrder skipped (no appendChild)
              winCand.__candTest.setSort('ema');
              const appendBeforeEma = appendChildCalls;
              await winCand.__candTest.syncBinancePool();
              assert.strictEqual(appendChildCalls, appendBeforeEma);

              // MediaQueryList 'change' event updates cached prefersReducedMotion; price change skips animate()
              reducedMotionMatches = true;
              mediaListeners.forEach((fn) => fn({ matches: true }));
              winCand.__candTest.setSort('price');
              tickerTick = 2;
              await winCand.__candTest.syncBinancePool();
              assert.strictEqual(rowAnimateCalls, 1);
              assert.strictEqual(matchMediaCalls, 1);
            })().catch((err) => {
              console.error(err);
              process.exit(1);
            });
            """
        )
        res = subprocess.run(
            [node_bin, "-e", script],
            cwd=str(ROOT_DIR),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0, msg=f"stdout={res.stdout}\nstderr={res.stderr}")

    def test_serverchan_notifier_uses_http_client_and_is_configured(self):
        import alert_notifier  # noqa: E402
        from http_client import HttpError  # noqa: E402

        empty = tad.ServerChanNotifier(sendkey="")
        self.assertFalse(empty.is_configured())
        self.assertTrue(empty.dry_run)

        live = tad.ServerChanNotifier(sendkey="SCTtestkey1234", dry_run=False)
        self.assertTrue(live.is_configured())
        self.assertFalse(live.dry_run)

        with patch.object(alert_notifier, "request_json", return_value={"code": 0}) as req_mock:
            self.assertTrue(live.send_alert("T", "D"))
            self.assertEqual(req_mock.call_count, 1)
            self.assertEqual(req_mock.call_args.kwargs["method"], "POST")

        with patch.object(alert_notifier, "request_json", return_value={"code": 40001}):
            self.assertFalse(live.send_alert("T", "D"))

        with patch.object(alert_notifier, "request_json", side_effect=HttpError("timeout")):
            self.assertFalse(live.send_alert("T", "D"))


if __name__ == "__main__":
    unittest.main()
