#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Regression test suite for P0/P1 account pipeline fixes:
- trade_reconstructor: One-Way (BOTH) pre-recording close vs flip, partial-queue FIFO
  notional attribution, unified gross pnl_pct, O(N) zero-close precomputation, snake_case merge.
- db_store & ledger_sync: snapshot-loss preservation of equity_observations via load_recording,
  load_position_history latest-N ordering, normalize_beijing_iso empty guard, chronological
  bisect pagination, server-time end_ms.
- binance_position_client: TV drawings fetched outside DB write lock, single save() per sync,
  relative amount_changed tolerance for micro-size positions.
- account_analytics_engine: NOT_CONFIGURED vs BinanceAPIError propagation.
- account_dashboard_api: raw-payload /api/account ETag caching, uniform JSON errors,
  uppercase klines cache key normalization.
"""

from contextlib import closing
import gzip
import http.client
import io
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import account_analytics_engine as aae  # noqa: E402
import account_dashboard_api as api  # noqa: E402
import binance_client as bc  # noqa: E402
import binance_position_client as bpc  # noqa: E402
import db_store  # noqa: E402
import http_client  # noqa: E402
import ledger_sync  # noqa: E402
import quant_metrics as qm  # noqa: E402
import trade_alert_daemon as tad  # noqa: E402
import trade_enrichment as te  # noqa: E402
import trade_reconstructor as tr  # noqa: E402


class TestTradeReconstructorP0Fixes(unittest.TestCase):
    def test_both_mode_pre_recording_close_not_misclassified_as_flip(self):
        # Pre-recording LONG 2 BTC @ 50000, post-start add LONG 1 BTC @ 60000,
        # then close all 3 BTC @ 62000 in One-Way (BOTH) mode with reduceOnly=False.
        fills = [
            {
                "symbol": "BTCUSDT",
                "id": 1,
                "orderId": 10,
                "side": "BUY",
                "positionSide": "BOTH",
                "time": 1000,
                "price": "60000",
                "qty": "1",
                "quoteQty": "60000",
                "reduceOnly": False,
            },
            {
                "symbol": "BTCUSDT",
                "id": 2,
                "orderId": 20,
                "side": "SELL",
                "positionSide": "BOTH",
                "time": 2000,
                "price": "62000",
                "qty": "3",
                "quoteQty": "186000",
                "reduceOnly": False,
            },
        ]
        income = [
            {"symbol": "BTCUSDT", "incomeType": "COMMISSION", "tranId": 1, "tradeId": "1", "time": 1000, "income": "-30", "asset": "USDT"},
            {"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "tranId": 2, "tradeId": "2", "time": 2000, "income": "26000", "asset": "USDT"},
            {"symbol": "BTCUSDT", "incomeType": "COMMISSION", "tranId": 3, "tradeId": "2", "time": 2000, "income": "-93", "asset": "USDT"},
        ]

        trades, open_fee = tr.reconstruct_binance_trades(income, [], fills)
        self.assertEqual(open_fee, 0.0)
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t["side"], "LONG")
        self.assertAlmostEqual(t["amount"], 3.0, places=6)
        self.assertAlmostEqual(t["realized_pnl"], 26000.0, places=4)
        self.assertAlmostEqual(t["fee"], 123.0, places=4)
        self.assertAlmostEqual(t["net_pnl"], 25877.0, places=4)
        # Combined entry notional = 60000 (known 1 BTC) + 100000 (pre-recording 2 BTC) = 160000
        self.assertAlmostEqual(t["entry_notional"], 160000.0, places=2)
        self.assertAlmostEqual(t["entry_price"], 160000.0 / 3.0, places=2)
        # Unified gross pnl_pct = 26000 / 160000 * 100 = 16.25%
        self.assertAlmostEqual(t["pnl_pct"], 16.25, places=2)

    def test_both_mode_genuine_flip_still_opens_reverse_lot(self):
        # Open LONG 1 @ 100, then SELL 3 @ 110 (closes 1 LONG with +10 PnL, opens 2 SHORT @ 110),
        # then BUY 2 @ 105 (closes 2 SHORT with +10 PnL).
        fills = [
            {"symbol": "ETHUSDT", "id": 1, "orderId": 1, "side": "BUY", "positionSide": "BOTH", "time": 1000, "price": "100", "qty": "1", "quoteQty": "100"},
            {"symbol": "ETHUSDT", "id": 2, "orderId": 2, "side": "SELL", "positionSide": "BOTH", "time": 2000, "price": "110", "qty": "3", "quoteQty": "330", "realizedPnl": "10"},
            {"symbol": "ETHUSDT", "id": 3, "orderId": 3, "side": "BUY", "positionSide": "BOTH", "time": 3000, "price": "105", "qty": "2", "quoteQty": "210", "realizedPnl": "10"},
        ]
        trades, open_fee = tr.reconstruct_binance_trades([], [], fills)
        self.assertEqual(open_fee, 0.0)
        self.assertEqual(len(trades), 2)
        self.assertEqual(trades[0]["side"], "LONG")
        self.assertAlmostEqual(trades[0]["amount"], 1.0)
        self.assertAlmostEqual(trades[0]["realized_pnl"], 10.0)
        self.assertEqual(trades[1]["side"], "SHORT")
        self.assertAlmostEqual(trades[1]["amount"], 2.0)
        self.assertAlmostEqual(trades[1]["entry_price"], 110.0)
        self.assertAlmostEqual(trades[1]["exit_price"], 105.0)
        self.assertAlmostEqual(trades[1]["realized_pnl"], 10.0)

    def test_hedge_partial_queue_consume_preserves_exact_open_notional(self):
        # Hedge mode: pre-recording LONG 1 @ 50000, post-start LONG 1 @ 60000, close 2 @ 65000 (PnL = 15000 + 5000 = 20000).
        fills = [
            {"symbol": "BTCUSDT", "id": 1, "orderId": 1, "side": "BUY", "positionSide": "LONG", "time": 1000, "price": "60000", "qty": "1", "quoteQty": "60000"},
            {"symbol": "BTCUSDT", "id": 2, "orderId": 2, "side": "SELL", "positionSide": "LONG", "time": 2000, "price": "65000", "qty": "2", "quoteQty": "130000"},
        ]
        income = [
            {"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "tranId": 1, "tradeId": "2", "time": 2000, "income": "20000", "asset": "USDT"},
        ]
        trades, _ = tr.reconstruct_binance_trades(income, [], fills)
        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(trades[0]["entry_notional"], 110000.0, places=2)
        self.assertAlmostEqual(trades[0]["entry_price"], 55000.0, places=2)
        self.assertAlmostEqual(trades[0]["realized_pnl"], 20000.0, places=2)
        self.assertAlmostEqual(trades[0]["pnl_pct"], round(20000.0 / 110000.0 * 100.0, 2), places=2)

    def test_pnl_pct_unified_gross_with_fees_across_all_paths(self):
        # Fills path with fee: entry 1000, exit 1100, fee 10 -> realized_pnl=100, net_pnl=90, gross pnl_pct=10.0
        fills = [
            {"symbol": "SOLUSDT", "id": 1, "orderId": 1, "side": "BUY", "positionSide": "LONG", "time": 1000, "price": "100", "qty": "10", "quoteQty": "1000"},
            {"symbol": "SOLUSDT", "id": 2, "orderId": 2, "side": "SELL", "positionSide": "LONG", "time": 2000, "price": "110", "qty": "10", "quoteQty": "1100"},
        ]
        income = [
            {"symbol": "SOLUSDT", "incomeType": "REALIZED_PNL", "tranId": 1, "tradeId": "2", "time": 2000, "income": "100", "asset": "USDT"},
            {"symbol": "SOLUSDT", "incomeType": "COMMISSION", "tranId": 2, "tradeId": "2", "time": 2000, "income": "-10", "asset": "USDT"},
        ]
        trades_fills, _ = tr.reconstruct_binance_trades(income, [], fills)
        self.assertAlmostEqual(trades_fills[0]["net_pnl"], 90.0)
        self.assertAlmostEqual(trades_fills[0]["pnl_pct"], 10.0, places=2)

        # Income-cluster path with matched local trade: same gross pnl_pct=10.0
        local_hist = [{
            "symbol": "SOLUSDT",
            "side": "LONG",
            "entry_price": 100.0,
            "exit_price": 110.0,
            "amount": 10.0,
            "leverage": 5,
            "closed_at": "2026-11-25 10:00:00+08:00",
            "status": "MANUAL_CLOSE",
        }]
        t_ms = int(te.parse_to_timestamp("2026-11-25 10:00:00+08:00") * 1000)
        inc_only = [
            {"symbol": "SOLUSDT", "incomeType": "REALIZED_PNL", "tranId": 1, "tradeId": "9", "time": t_ms, "income": "100", "asset": "USDT"},
            {"symbol": "SOLUSDT", "incomeType": "COMMISSION", "tranId": 2, "tradeId": "9", "time": t_ms, "income": "-10", "asset": "USDT"},
        ]
        trades_inc, _ = tr.reconstruct_binance_trades(inc_only, local_hist, [])
        self.assertAlmostEqual(trades_inc[0]["net_pnl"], 90.0)
        self.assertAlmostEqual(trades_inc[0]["pnl_pct"], 10.0, places=2)
        self.assertAlmostEqual(trades_inc[0]["roe_pct"], 50.0, places=2)

    def test_merge_active_positions_supports_normalized_snake_case_payload(self):
        normalized_binance = [{
            "symbol": "BTCUSDT",
            "side": "LONG",
            "amount": 0.5,
            "signed_amount": 0.5,
            "entry_price": 90000.0,
            "mark_price": 92000.0,
            "unrealized_pnl": 1000.0,
            "leverage": 10,
            "position_side": "BOTH",
            "update_time": 1795260000000,
        }]
        local_pos = [{
            "symbol": "BTCUSDT",
            "side": "LONG",
            "amount": 0.5,
            "trendline": {"id": "TL_BTC"},
            "highest_price": 93000.0,
        }]
        merged = te.merge_active_positions(normalized_binance, local_pos, positions_available=True)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["source"], "BINANCE_SYNC")
        self.assertEqual(merged[0]["trendline_id"], "TL_BTC")
        self.assertEqual(merged[0]["highest_price"], 93000.0)
        self.assertAlmostEqual(merged[0]["unrealized_pnl"], 1000.0)


class TestDbStoreAndLedgerSyncP0Fixes(unittest.TestCase):
    def test_normalize_beijing_iso_rejects_empty_string(self):
        with self.assertRaises(ValueError):
            db_store.normalize_beijing_iso("")
        with self.assertRaises(ValueError):
            db_store.normalize_beijing_iso("   ")
        self.assertTrue(db_store.normalize_beijing_iso(None).endswith("+08:00"))

    def test_load_position_history_limit_returns_latest_n_chronologically(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "hist.sqlite3"
            for i in range(1, 6):
                db_store.append_position_history(
                    db_path,
                    {
                        "symbol": "BTCUSDT" if i % 2 == 1 else "ETHUSDT",
                        "seq": i,
                        "closed_at": f"2026-11-25 10:0{i}:00+08:00",
                    },
                )
            latest2 = db_store.load_position_history(db_path, limit=2)
            self.assertEqual([r["seq"] for r in latest2], [4, 5])

            latest2_btc = db_store.load_position_history(db_path, symbol="BTCUSDT", limit=2)
            self.assertEqual([r["seq"] for r in latest2_btc], [3, 5])
            self.assertEqual(db_store.load_position_history(db_path, limit=0), [])

    def test_snapshot_loss_preserves_historical_equity_observations(self):
        start_ms = 1795200000000
        t1 = start_ms + 600_000
        t2 = start_ms + 1_200_000
        client = bc.BinanceFuturesClient("k", "s")
        engine = aae.AccountAnalyticsEngine(client=client)

        initial_report = {
            "recording": {"start_ms": start_ms, "started_at": "2026-11-21 00:00:00", "initial_equity": 1000.0},
            "equity_curve": [
                {"timestamp": start_ms, "datetime": "2026-11-21 00:00:00", "equity": 1000.0, "cash": 1000.0, "drawdown_usd": 0.0, "drawdown_pct": 0.0, "event": "开始记录"},
                {"timestamp": t1, "datetime": "2026-11-21 00:10:00", "equity": 1150.0, "cash": 1150.0, "drawdown_usd": 0.0, "drawdown_pct": 0.0, "event": "币安账户快照"},
            ],
            "summary": {"total_equity": 1150.0},
        }
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "account.sqlite3"
            db_store.publish_report(db_path, initial_report)
            # Simulate account_snapshot being lost/deleted while account_recording & equity_observations remain.
            with db_store.transaction(db_path) as conn:
                conn.execute("DELETE FROM account_snapshot")

            self.assertIsNone(db_store.load_raw_report(db_path))
            self.assertIsNotNone(db_store.load_recording(db_path))

            status = {"connected": True, "mode": "BINANCE_SNAPSHOT_PENDING_INCOME", "positions_available": True, "income_available": False}
            with patch.object(
                engine,
                "fetch_binance_data",
                return_value=({"totalWalletBalance": "1200", "totalMarginBalance": "1200"}, [], status),
            ), patch.object(client, "get_server_time", return_value=t2), patch.object(
                client, "_send_signed_request", return_value=[]
            ), patch.object(ledger_sync.db_store, "load_ledger_bundle", wraps=db_store.load_ledger_bundle) as spy_bundle:
                report = engine.run(database=db_path)

            # Must not call load_ledger_bundle twice (uses load_recording first).
            self.assertEqual(spy_bundle.call_count, 1)
            timestamps = [pt["timestamp"] for pt in report["equity_curve"]]
            self.assertIn(start_ms, timestamps)
            self.assertIn(t1, timestamps)
            with closing(sqlite3.connect(db_path)) as conn:
                obs_rows = conn.execute("SELECT timestamp FROM equity_observations ORDER BY timestamp").fetchall()
            self.assertIn((t1,), obs_rows)

    def test_fetch_paginated_range_chronological_order_and_server_time_end_ms(self):
        records = [{"time": i, "id": i} for i in range(1, 11)]

        def fake_fetch(s: int, e: int) -> list[dict]:
            return [r for r in records if s <= r["time"] <= e]

        got = ledger_sync.fetch_paginated_range(fake_fetch, 1, 10, page_limit=4)
        self.assertEqual([r["time"] for r in got], list(range(1, 11)))

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sync.sqlite3"
            start_ms = 1795200000000
            server_now_ms = start_ms + 500_000
            db_store.publish_report(
                db_path,
                {
                    "recording": {"start_ms": start_ms, "started_at": "2026-11-21 00:00:00", "initial_equity": 1000.0},
                    "equity_curve": [],
                },
            )
            fake_client = MagicMock()
            fake_client.get_server_time.return_value = server_now_ms
            fake_client.get_income.return_value = []
            rec, _, _, _, _, _, _ = ledger_sync.sync_db_ledger(
                fake_client, db_path, {"totalMarginBalance": "1000"}, [], None
            )
            fake_client.get_server_time.assert_called_once()
            self.assertEqual(rec["ledger_synced_until_ms"], server_now_ms)


class TestBinancePositionSyncAndEngineP0Fixes(unittest.TestCase):
    def test_drawings_fetched_outside_tx_single_save_and_relative_amount_tolerance(self):
        now = int(1795200000)
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "pos.sqlite3"
            store = tad.PositionStore(db_path)
            store.add_position("ETHUSDT", entry_price=3000.0, amount=10.0, source="BINANCE_SYNC")
            store.add_position("BTCUSDT", entry_price=90000.0, amount=0.001, source="BINANCE_SYNC", trendline={"id": "TL_BTC"})

            client = bc.BinanceFuturesClient("k", "s")
            active_longs = [
                # BTCUSDT micro-amount changed by 5e-7 (< 1e-6 absolute, > 1e-6 relative)
                {"symbol": "BTCUSDT", "amount": 0.0010005, "entry_price": 90000.0, "mark_price": 90500.0, "unrealized_pnl": 0.5, "leverage": 10},
                # SOLUSDT is brand new
                {"symbol": "SOLUSDT", "amount": 5.0, "entry_price": 150.0, "mark_price": 152.0, "unrealized_pnl": 10.0, "leverage": 5},
                # ETHUSDT removed -> auto-closed
            ]
            tx_conn_during_drawings = []

            def fake_drawings(_layout_id: str):
                tx_conn_during_drawings.append(store._tx_conn)
                return {"SOLUSDT": [{"id": "TL_SOL", "interval": "15", "p1": 160.0, "t1": now - 3600, "p2": 155.0, "t2": now - 600}]}

            with patch.object(client, "get_active_positions", return_value=active_longs), patch.object(
                store, "save", wraps=store.save
            ) as spy_save:
                res = bpc.sync_binance_positions(client, store, load_drawings=fake_drawings)

            self.assertEqual(tx_conn_during_drawings, [None])
            self.assertEqual(spy_save.call_count, 1)
            self.assertEqual(res["new_added"], ["SOLUSDT"])
            self.assertEqual(res["auto_closed"], ["ETHUSDT"])
            btc = next(p for p in store.get_open_positions() if p["symbol"] == "BTCUSDT")
            self.assertAlmostEqual(btc["amount"], 0.0010005, places=7)

    def test_engine_unconfigured_vs_api_error_propagation(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "eng.sqlite3"
            unconfigured = bc.BinanceFuturesClient("", "")
            eng_unconf = aae.AccountAnalyticsEngine(client=unconfigured, offline_mode=False)
            _, _, status = eng_unconf.fetch_binance_data()
            self.assertEqual(status["mode"], "NOT_CONFIGURED")
            with self.assertRaises(bc.BinanceAPIError) as ctx_unconf:
                eng_unconf.run(database=db_path)
            self.assertEqual(ctx_unconf.exception.code, "NOT_CONFIGURED")

            configured = bc.BinanceFuturesClient("k", "s")
            eng_err = aae.AccountAnalyticsEngine(client=configured, offline_mode=False)
            with patch.object(
                configured,
                "_send_signed_request",
                side_effect=bc.BinanceAPIError("IP banned", code=418),
            ):
                with self.assertRaises(RuntimeError) as ctx_err:
                    eng_err.run(database=db_path)
            self.assertIn("418", str(ctx_err.exception))
            self.assertIn("IP banned", str(ctx_err.exception))


class TestDashboardAPIP0Fixes(unittest.TestCase):
    def _invoke(self, handler_cls, method: str, raw_path: str, headers: dict | None = None):
        handler = handler_cls.__new__(handler_cls)
        handler.path = raw_path
        handler.headers = headers or {}
        handler.wfile = io.BytesIO()
        status_box = {}
        resp_headers = {}
        handler.send_response = lambda code, message=None: status_box.__setitem__("code", code)
        handler.send_header = lambda k, v: resp_headers.__setitem__(k.lower(), v)
        handler.end_headers = lambda: None
        handler.send_error = lambda code, message=None, explain=None: status_box.__setitem__("code", code)
        handler.log_error = lambda *a, **kw: None
        if method == "HEAD":
            handler.do_HEAD()
        else:
            handler.do_GET()
        return status_box.get("code", 0), resp_headers, handler.wfile.getvalue()

    def test_account_uses_raw_cache_and_uniform_json_errors_and_upper_klines_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "api.sqlite3"
            handler_cls = api.create_handler(db_path)

            # 503 on missing DB returns JSON
            code503, hdr503, body503 = self._invoke(handler_cls, "GET", "/api/account")
            self.assertEqual(code503, 503)
            self.assertIn("application/json", hdr503["content-type"])
            self.assertIn("error", json.loads(body503))

            # 404 on unknown /api/* returns JSON
            code404, hdr404, body404 = self._invoke(handler_cls, "GET", "/api/does-not-exist")
            self.assertEqual(code404, 404)
            self.assertIn("application/json", hdr404["content-type"])
            self.assertIn("error", json.loads(body404))

            db_store.publish_report(
                db_path,
                {"summary": {"total_equity": 888.0}, "active_positions": [], "trade_history": [], "equity_curve": [], "daily_pnl": []},
            )
            with patch.object(json, "loads", side_effect=AssertionError("must not parse full report")):
                c1, h1, b1 = self._invoke(handler_cls, "GET", "/api/account")
                c2, _, b2 = self._invoke(handler_cls, "GET", "/api/account", headers={"If-None-Match": h1["etag"]})
            self.assertEqual(c1, 200)
            self.assertEqual(json.loads(b1)["summary"]["total_equity"], 888.0)
            self.assertEqual(c2, 304)
            self.assertEqual(b2, b"")

            # klines normalizes symbol case in cache key & wraps non-JSON 4xx proxy errors
            calls = []

            def fake_req(url: str, **_kwargs) -> bytes:
                calls.append(url)
                return b"[[1,2,3]]"

            with patch.object(http_client, "request_bytes", side_effect=fake_req):
                ck1, _, _ = self._invoke(handler_cls, "GET", "/api/binance/klines?symbol=btcusdt&interval=15m&limit=10")
                ck2, _, _ = self._invoke(handler_cls, "GET", "/api/binance/klines?symbol=BTCUSDT&interval=15m&limit=10")
            self.assertEqual(ck1, 200)
            self.assertEqual(ck2, 200)
            self.assertEqual(len(calls), 1)

            with patch.object(
                http_client,
                "request_bytes",
                side_effect=http_client.HttpError("HTTP 403", status_code=403, body="<html>Forbidden</html>"),
            ):
                c403, h403, b403 = self._invoke(handler_cls, "GET", "/api/binance/klines?symbol=ETHUSDT&interval=15m&limit=10")
            self.assertEqual(c403, 403)
            self.assertIn("application/json", h403["content-type"])
            self.assertIn("Forbidden", json.loads(b403)["error"])

    def test_binance_proxy_singleflight_coalesces_concurrent_requests_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler_cls = api.create_handler(Path(tmp) / "sf.sqlite3")

            entered = threading.Event()
            release = threading.Event()
            call_count = 0
            count_lock = threading.Lock()

            def slow_upstream(_url: str, **_kwargs) -> bytes:
                nonlocal call_count
                with count_lock:
                    call_count += 1
                entered.set()
                release.wait(timeout=5.0)
                return b'[{"symbol":"BTCUSDT","lastPrice":"95000"}]'

            results: list[tuple[int, dict, bytes]] = []
            res_lock = threading.Lock()

            def worker(path: str) -> None:
                res = self._invoke(handler_cls, "GET", path)
                with res_lock:
                    results.append(res)

            with patch.object(http_client, "request_bytes", side_effect=slow_upstream):
                t1 = threading.Thread(target=worker, args=("/api/binance/ticker24hr",))
                t1.start()
                self.assertTrue(entered.wait(timeout=2.0))
                waiters = [
                    threading.Thread(target=worker, args=("/api/binance/ticker24hr",))
                    for _ in range(3)
                ]
                for w in waiters:
                    w.start()
                # Give waiters a moment to block on the singleflight event before releasing.
                release.wait(timeout=0.05)
                release.set()
                t1.join(timeout=5.0)
                for w in waiters:
                    w.join(timeout=5.0)

            self.assertEqual(call_count, 1)
            self.assertEqual(len(results), 4)
            for code, hdrs, body in results:
                self.assertEqual(code, 200)
                self.assertEqual(hdrs["content-type"], "application/json; charset=utf-8")
                self.assertEqual(json.loads(body), [{"symbol": "BTCUSDT", "lastPrice": "95000"}])

            # Error path coalesces & cleans up inflight slot in finally so subsequent calls retry.
            err_entered = threading.Event()
            err_release = threading.Event()
            err_calls = 0

            def failing_upstream(_url: str, **_kwargs) -> bytes:
                nonlocal err_calls
                with count_lock:
                    err_calls += 1
                err_entered.set()
                err_release.wait(timeout=5.0)
                raise http_client.HttpError("HTTP 503", status_code=503, body="upstream down")

            err_results: list[tuple[int, dict, bytes]] = []

            def err_worker() -> None:
                res = self._invoke(handler_cls, "GET", "/api/binance/premiumIndex")
                with res_lock:
                    err_results.append(res)

            with patch.object(http_client, "request_bytes", side_effect=failing_upstream):
                et1 = threading.Thread(target=err_worker)
                et1.start()
                self.assertTrue(err_entered.wait(timeout=2.0))
                et2 = threading.Thread(target=err_worker)
                et2.start()
                err_release.wait(timeout=0.05)
                err_release.set()
                et1.join(timeout=5.0)
                et2.join(timeout=5.0)

            self.assertEqual(err_calls, 1)
            self.assertEqual([r[0] for r in err_results], [502, 502])

            with patch.object(http_client, "request_bytes", return_value=b'[{"symbol":"BTCUSDT"}]') as mock_recover:
                code_ok, _, body_ok = self._invoke(handler_cls, "GET", "/api/binance/premiumIndex")
            self.assertEqual(mock_recover.call_count, 1)
            self.assertEqual(code_ok, 200)
            self.assertEqual(body_ok, b'[{"symbol":"BTCUSDT"}]')

    def test_proxy_endpoints_filter_by_candidate_snapshot_and_refresh_on_version_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "proxy_filter.sqlite3"
            db_store.publish_candidates(
                db_path,
                [{"symbol": "YBUSDT"}],
                updated_at="2026-11-26T10:00:00+08:00",
            )
            handler_cls = api.create_handler(db_path)
            raw_tickers = json.dumps([
                {"symbol": "BTCUSDT", "lastPrice": "95000"},
                {"symbol": "YBUSDT", "lastPrice": "0.05"},
                {"symbol": "ETHUSDT", "lastPrice": "3500"},
            ]).encode("utf-8")
            raw_premiums = json.dumps([
                {"symbol": "BTCUSDT", "lastFundingRate": "0.0001"},
                {"symbol": "YBUSDT", "lastFundingRate": "-0.0002"},
            ]).encode("utf-8")

            def fake_upstream(url: str, **_kwargs) -> bytes:
                if "ticker/24hr" in url:
                    return raw_tickers
                if "premiumIndex" in url:
                    return raw_premiums
                raise AssertionError(f"Unexpected URL: {url}")

            with patch.object(http_client, "request_bytes", side_effect=fake_upstream) as mock_req:
                code1, _, body1 = self._invoke(handler_cls, "GET", "/api/binance/ticker24hr")
                code_p, _, body_p = self._invoke(handler_cls, "GET", "/api/binance/premiumIndex")
                self.assertEqual(code1, 200)
                self.assertEqual(code_p, 200)
                self.assertEqual(json.loads(body1), [{"symbol": "YBUSDT", "lastPrice": "0.05"}])
                self.assertEqual(json.loads(body_p), [{"symbol": "YBUSDT", "lastFundingRate": "-0.0002"}])

                # Updating candidate_snapshot must immediately re-filter cached upstream payload without refetching
                db_store.publish_candidates(
                    db_path,
                    [{"symbol": "YBUSDT"}, {"symbol": "ETHUSDT"}],
                    updated_at="2026-11-26T10:05:00+08:00",
                )
                code2, _, body2 = self._invoke(handler_cls, "GET", "/api/binance/ticker24hr")
                self.assertEqual(code2, 200)
                self.assertEqual(
                    json.loads(body2),
                    [
                        {"symbol": "YBUSDT", "lastPrice": "0.05"},
                        {"symbol": "ETHUSDT", "lastPrice": "3500"},
                    ],
                )
            self.assertEqual(mock_req.call_count, 2)

    def test_gzip_precompression_for_snapshots_static_and_proxy(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "gzip.sqlite3"
            big_trades = [{"symbol": f"COIN{i}USDT", "pnl": i * 1.5} for i in range(60)]
            db_store.publish_report(
                db_path,
                {
                    "summary": {"total_equity": 1234.0},
                    "active_positions": [],
                    "trade_history": big_trades,
                    "equity_curve": [],
                    "daily_pnl": [],
                },
            )
            db_store.publish_candidates(
                db_path,
                [{"symbol": f"CAND{i}USDT", "score": i} for i in range(60)],
                updated_at="2026-11-25T12:00:00+08:00",
            )
            handler_cls = api.create_handler(db_path)

            # 1. /api/account & /api/candidates: gzip vs raw + ETag/304 preservation
            for endpoint in ("/api/account", "/api/candidates"):
                c_gz, h_gz, b_gz = self._invoke(
                    handler_cls, "GET", endpoint, headers={"Accept-Encoding": "deflate, gzip"}
                )
                c_raw, h_raw, b_raw = self._invoke(handler_cls, "GET", endpoint)
                self.assertEqual(c_gz, 200)
                self.assertEqual(c_raw, 200)
                self.assertEqual(h_gz.get("content-encoding"), "gzip")
                self.assertEqual(h_gz.get("vary"), "Accept-Encoding")
                self.assertEqual(h_gz.get("content-length"), str(len(b_gz)))
                self.assertNotIn("content-encoding", h_raw)
                self.assertEqual(gzip.decompress(b_gz), b_raw)
                self.assertGreater(len(b_raw), 1024)

                c304_gz, _, b304_gz = self._invoke(
                    handler_cls,
                    "GET",
                    endpoint,
                    headers={"Accept-Encoding": "gzip", "If-None-Match": h_gz["etag"]},
                )
                c304_raw, _, b304_raw = self._invoke(
                    handler_cls,
                    "GET",
                    endpoint,
                    headers={"If-None-Match": h_raw["etag"]},
                )
                self.assertEqual(c304_gz, 304)
                self.assertEqual(b304_gz, b"")
                self.assertEqual(c304_raw, 304)
                self.assertEqual(b304_raw, b"")

            # 2. Static file (/static/account.js)
            cs_gz, hs_gz, bs_gz = self._invoke(
                handler_cls, "GET", "/static/account.js", headers={"Accept-Encoding": "gzip"}
            )
            cs_raw, hs_raw, bs_raw = self._invoke(handler_cls, "GET", "/static/account.js")
            self.assertEqual(cs_gz, 200)
            self.assertEqual(cs_raw, 200)
            self.assertEqual(hs_gz.get("content-encoding"), "gzip")
            self.assertEqual(hs_gz.get("vary"), "Accept-Encoding")
            self.assertNotIn("content-encoding", hs_raw)
            self.assertEqual(gzip.decompress(bs_gz), bs_raw)

            # 3. Proxy endpoint: large (>=1024B) compresses, small (<1024B) stays raw
            big_proxy_payload = json.dumps([[i, "1.0"] for i in range(120)]).encode("utf-8")
            self.assertGreater(len(big_proxy_payload), 1024)
            with patch.object(http_client, "request_bytes", return_value=big_proxy_payload):
                cp_gz, hp_gz, bp_gz = self._invoke(
                    handler_cls,
                    "GET",
                    "/api/binance/klines?symbol=BTCUSDT&interval=1h&limit=100",
                    headers={"Accept-Encoding": "gzip"},
                )
                cp_raw, hp_raw, bp_raw = self._invoke(
                    handler_cls,
                    "GET",
                    "/api/binance/klines?symbol=BTCUSDT&interval=1h&limit=100",
                )
            self.assertEqual(cp_gz, 200)
            self.assertEqual(cp_raw, 200)
            self.assertEqual(hp_gz.get("content-encoding"), "gzip")
            self.assertEqual(hp_gz.get("vary"), "Accept-Encoding")
            self.assertNotIn("content-encoding", hp_raw)
            self.assertEqual(gzip.decompress(bp_gz), big_proxy_payload)
            self.assertEqual(bp_raw, big_proxy_payload)

            with patch.object(http_client, "request_bytes", return_value=b"[[1,2,3]]"):
                c_small, h_small, b_small = self._invoke(
                    handler_cls,
                    "GET",
                    "/api/binance/klines?symbol=ETHUSDT&interval=1h&limit=10",
                    headers={"Accept-Encoding": "gzip"},
                )
            self.assertEqual(c_small, 200)
            self.assertNotIn("content-encoding", h_small)
            self.assertEqual(b_small, b"[[1,2,3]]")

    def test_load_ledger_bundle_partial_and_empty_dbs_single_schema_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 1. Missing DB file
            missing_db = Path(tmp) / "missing.sqlite3"
            self.assertEqual(
                db_store.load_ledger_bundle(missing_db),
                {"recording": None, "income": [], "fills": [], "observations": []},
            )

            # 2. Empty DB (no tables at all)
            empty_db = Path(tmp) / "empty.sqlite3"
            sqlite3.connect(empty_db).close()
            self.assertEqual(
                db_store.load_ledger_bundle(empty_db),
                {"recording": None, "income": [], "fills": [], "observations": []},
            )

            # 3. Partial DB (only account_recording & income_ledger) + single sqlite_master query check
            partial_db = Path(tmp) / "partial.sqlite3"
            with closing(sqlite3.connect(partial_db)) as conn:
                conn.execute("CREATE TABLE account_recording (id INTEGER PRIMARY KEY CHECK (id = 1), payload TEXT NOT NULL)")
                conn.execute(
                    "CREATE TABLE income_ledger (income_type TEXT NOT NULL, transaction_id TEXT NOT NULL, "
                    "timestamp INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY (income_type, transaction_id))"
                )
                conn.execute(
                    "INSERT INTO account_recording (id, payload) VALUES (1, ?)",
                    (json.dumps({"start_ms": 1000, "initial_equity": 500.0}),),
                )
                conn.execute(
                    "INSERT INTO income_ledger (income_type, transaction_id, timestamp, payload) VALUES (?, ?, ?, ?)",
                    ("REALIZED_PNL", "1", 900, json.dumps({"tranId": "1", "time": 900, "income": "10"})),
                )
                conn.execute(
                    "INSERT INTO income_ledger (income_type, transaction_id, timestamp, payload) VALUES (?, ?, ?, ?)",
                    ("REALIZED_PNL", "2", 1500, json.dumps({"tranId": "2", "time": 1500, "income": "25"})),
                )
                conn.commit()

            traced_sql: list[str] = []
            real_open = db_store.open_sqlite_connection

            def traced_open(database: Path, *, readonly: bool = False):
                conn = real_open(database, readonly=readonly)
                conn.set_trace_callback(traced_sql.append)
                return conn

            with patch.object(db_store, "open_sqlite_connection", side_effect=traced_open):
                bundle = db_store.load_ledger_bundle(partial_db, start_ms=1000)

            schema_queries = [q for q in traced_sql if "sqlite_master" in q]
            self.assertEqual(len(schema_queries), 1)
            self.assertEqual(bundle["recording"], {"start_ms": 1000, "initial_equity": 500.0})
            self.assertEqual(bundle["income"], [{"tranId": "2", "time": 1500, "income": "25"}])
            self.assertEqual(bundle["fills"], [])
            self.assertEqual(bundle["observations"], [])


class TestAccountFailFastRegressionFixes(unittest.TestCase):
    def test_trade_reconstructor_rejects_corrupt_required_fields_and_magic_leverage(self):
        # Missing/empty/null asset fails fast
        with self.assertRaises(KeyError):
            tr.income_asset({})
        with self.assertRaises(ValueError):
            tr.income_asset({"asset": ""})
        with self.assertRaises(ValueError):
            tr.income_asset({"asset": None})
        self.assertEqual(tr.income_asset({"asset": " bnb "}), "BNB")

        # enrich_trade_record requires entry_price & amount, does not fabricate leverage=5
        with self.assertRaises(KeyError):
            te.enrich_trade_record({"symbol": "BTCUSDT", "amount": 1.0}, 0)
        with self.assertRaises(KeyError):
            te.enrich_trade_record({"symbol": "BTCUSDT", "entry_price": 100.0}, 0)
        with self.assertRaises(ValueError):
            te.enrich_trade_record({"symbol": "BTCUSDT", "entry_price": 0.0, "amount": 1.0}, 0)

        no_lev_trade = te.enrich_trade_record(
            {"symbol": "BTCUSDT", "entry_price": 100.0, "exit_price": 110.0, "amount": 1.0},
            0,
        )
        self.assertIsNone(no_lev_trade["leverage"])
        self.assertIsNone(no_lev_trade["roe_pct"])

        # enrich_active_position requires entry_price, amount, and mark_price; no magic leverage=5
        with self.assertRaises(ValueError):
            te.enrich_active_position({"symbol": "BTCUSDT", "amount": 1.0, "mark_price": 100.0}, 0)
        with self.assertRaises(ValueError):
            te.enrich_active_position({"symbol": "BTCUSDT", "entry_price": 100.0, "mark_price": 100.0}, 0)
        with self.assertRaises(ValueError):
            te.enrich_active_position({"symbol": "BTCUSDT", "entry_price": 100.0, "amount": 1.0}, 0)
        with self.assertRaises(ValueError):
            te.enrich_active_position({"symbol": "BTCUSDT", "entry_price": 100.0, "amount": 1.0, "mark_price": 0.0}, 0)

        no_lev_pos = te.enrich_active_position(
            {"symbol": "BTCUSDT", "entry_price": 100.0, "amount": 2.0, "mark_price": 105.0},
            0,
        )
        self.assertIsNone(no_lev_pos["leverage"])
        self.assertIsNone(no_lev_pos["roe_pct"])
        self.assertIsNone(no_lev_pos["liquidation_price"])
        self.assertIsNone(no_lev_pos["initial_margin"])

    def test_reconstruct_binance_trades_fails_fast_on_malformed_income_fills_or_local_history(self):
        # Missing income amount or incomeType or time fails fast
        with self.assertRaises(KeyError):
            tr.reconstruct_binance_trades(
                [{"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "asset": "USDT", "time": 1000}],
                [],
            )
        with self.assertRaises(KeyError):
            tr.reconstruct_binance_trades(
                [{"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "asset": "USDT", "income": "10"}],
                [],
            )

        # Empty-queue reduceOnly fill without realizedPnl or REALIZED_PNL income fails fast
        with self.assertRaises(ValueError):
            tr.reconstruct_binance_trades(
                [],
                [],
                fills=[{
                    "symbol": "BTCUSDT",
                    "id": 1,
                    "orderId": 1,
                    "side": "SELL",
                    "positionSide": "BOTH",
                    "time": 1000,
                    "price": "100",
                    "qty": "1",
                    "reduceOnly": True,
                }],
            )

        # Unparseable closed_at in local_history fails fast
        with self.assertRaises(ValueError):
            tr.reconstruct_binance_trades(
                [],
                [{"symbol": "BTCUSDT", "closed_at": "not-a-timestamp"}],
                [],
            )

    def test_account_analytics_engine_rejects_missing_or_invalid_initial_capital_and_bad_closed_at(self):
        for bad_cap in (0.0, -50.0, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                aae.AccountAnalyticsEngine(offline_mode=True, initial_capital=bad_cap)

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "offline_no_cap.sqlite3"
            eng = aae.AccountAnalyticsEngine(offline_mode=True)
            with self.assertRaises(ValueError):
                eng.run(database=db_path)

            # Offline mode uses persisted recording initial_equity if present
            db_store.publish_report(
                db_path,
                {
                    "recording": {"start_ms": 1795200000000, "started_at": "2026-11-21 00:00:00", "initial_equity": 3333.0},
                    "equity_curve": [{"timestamp": 1795200000000, "equity": 3333.0, "drawdown_usd": 0.0, "drawdown_pct": 0.0}],
                },
            )
            report = eng.run(database=db_path)
            self.assertEqual(report["summary"]["initial_capital"], 3333.0)
            self.assertEqual(report["income_breakdown"]["principal_source"], "recording_start")

        # Online mode must fail fast if position_history has unparseable closed_at instead of dropping it
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "bad_hist.sqlite3"
            start_ms = 1795200000000
            with db_store.transaction(db_path) as conn:
                conn.execute(
                    "INSERT INTO position_history (symbol, closed_at, payload) VALUES (?, ?, ?)",
                    ("BTCUSDT", "CORRUPT_TIME", json.dumps({"symbol": "BTCUSDT", "closed_at": "CORRUPT_TIME"})),
                )
            client = bc.BinanceFuturesClient("k", "s")
            eng = aae.AccountAnalyticsEngine(client=client)
            status = {"connected": True, "mode": "BINANCE_SNAPSHOT_PENDING_INCOME", "positions_available": True, "income_available": False}
            with patch.object(
                eng,
                "fetch_binance_data",
                return_value=({"totalWalletBalance": "1000", "totalMarginBalance": "1000"}, [], status),
            ), patch.object(client, "get_server_time", return_value=start_ms), patch.object(
                client, "_send_signed_request", return_value=[]
            ):
                with self.assertRaises(ValueError):
                    eng.run(database=db_path, recording_start_ms=start_ms)

    def test_binance_position_client_logs_malformed_trendlines_and_rejects_invalid_drawings_return(self):
        import time as _time
        now = _time.time()
        malformed = {"id": "BAD_15M", "interval": "15", "p1": None, "t1": now - 3600, "p2": 9.0, "t2": now - 100}
        with self.assertLogs("binance_client", level="WARNING") as cm:
            chosen = bpc.select_best_position_trendline([malformed], current_time=now)
        self.assertIsNone(chosen)
        self.assertTrue(any("BAD_15M" in msg for msg in cm.output))

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "pos.sqlite3"
            store = tad.PositionStore(db_path)
            client = bc.BinanceFuturesClient("k", "s")
            active_longs = [
                {"symbol": "SOLUSDT", "amount": 5.0, "entry_price": 150.0, "mark_price": 152.0, "unrealized_pnl": 10.0, "leverage": 5},
            ]
            with patch.object(client, "get_active_positions", return_value=active_longs):
                with self.assertRaises(TypeError):
                    bpc.sync_binance_positions(client, store, load_drawings=lambda _lid: None)

    def test_db_store_and_ledger_sync_reject_corrupt_recording_and_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "bad_rec.sqlite3"
            with self.assertRaises(ValueError):
                db_store.publish_report(db_path, {"recording": ["invalid"]})

            db_store.publish_report(
                db_path,
                {
                    "recording": {
                        "start_ms": 1795200000000,
                        "started_at": "2026-11-21 00:00:00",
                        "initial_equity": 1000.0,
                        "fills_cursor_ms": ["not-a-dict"],
                    },
                    "equity_curve": [],
                },
            )
            fake_client = MagicMock()
            fake_client.get_server_time.return_value = 1795200500000
            fake_client.get_income.return_value = []
            with self.assertRaises(TypeError):
                ledger_sync.sync_db_ledger(fake_client, db_path, {"totalMarginBalance": "1000"}, [], None)


class TestLedgerBugfixRegressions(unittest.TestCase):
    def test_both_mode_multi_fill_breakeven_close_not_misclassified_as_phantom_short(self):
        # BUY 1@100 + BUY 1@102 then SELL 2@101 (realizedPnl=0) must reconstruct 1 LONG breakeven trade.
        fills = [
            {"symbol": "BTCUSDT", "id": 1, "orderId": 1, "side": "BUY", "positionSide": "BOTH", "time": 1000, "price": "100", "qty": "1", "quoteQty": "100", "realizedPnl": "0"},
            {"symbol": "BTCUSDT", "id": 2, "orderId": 2, "side": "BUY", "positionSide": "BOTH", "time": 2000, "price": "102", "qty": "1", "quoteQty": "102", "realizedPnl": "0"},
            {"symbol": "BTCUSDT", "id": 3, "orderId": 3, "side": "SELL", "positionSide": "BOTH", "time": 3000, "price": "101", "qty": "2", "quoteQty": "202", "realizedPnl": "0"},
        ]
        trades, open_fee = tr.reconstruct_binance_trades([], [], fills)
        self.assertEqual(open_fee, 0.0)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["side"], "LONG")
        self.assertAlmostEqual(trades[0]["amount"], 2.0)
        self.assertAlmostEqual(trades[0]["entry_price"], 101.0)
        self.assertAlmostEqual(trades[0]["exit_price"], 101.0)
        self.assertAlmostEqual(trades[0]["realized_pnl"], 0.0)

        # Partial close at weighted-average breakeven followed by profit close on remaining qty
        fills_partial = [
            {"symbol": "ETHUSDT", "id": 10, "orderId": 10, "side": "BUY", "positionSide": "BOTH", "time": 1000, "price": "100", "qty": "1", "quoteQty": "100", "realizedPnl": "0"},
            {"symbol": "ETHUSDT", "id": 11, "orderId": 11, "side": "BUY", "positionSide": "BOTH", "time": 2000, "price": "102", "qty": "1", "quoteQty": "102", "realizedPnl": "0"},
            {"symbol": "ETHUSDT", "id": 12, "orderId": 12, "side": "SELL", "positionSide": "BOTH", "time": 3000, "price": "101", "qty": "1", "quoteQty": "101", "realizedPnl": "0"},
            {"symbol": "ETHUSDT", "id": 13, "orderId": 13, "side": "SELL", "positionSide": "BOTH", "time": 4000, "price": "105", "qty": "1", "quoteQty": "105", "realizedPnl": "4"},
        ]
        trades_partial, _ = tr.reconstruct_binance_trades([], [], fills_partial)
        self.assertEqual(len(trades_partial), 2)
        self.assertTrue(all(t["side"] == "LONG" for t in trades_partial))
        self.assertAlmostEqual(sum(t["realized_pnl"] for t in trades_partial), 5.0)

        # Genuine multi-fill pre-recording 0-PnL close followed by opposite-side open still works
        fills_pre = [
            {"symbol": "SOLUSDT", "id": 20, "orderId": 20, "side": "SELL", "positionSide": "BOTH", "time": 1000, "price": "100", "qty": "1", "quoteQty": "100", "realizedPnl": "0"},
            {"symbol": "SOLUSDT", "id": 21, "orderId": 21, "side": "SELL", "positionSide": "BOTH", "time": 2000, "price": "102", "qty": "1", "quoteQty": "102", "realizedPnl": "0"},
            {"symbol": "SOLUSDT", "id": 22, "orderId": 22, "side": "BUY", "positionSide": "BOTH", "time": 3000, "price": "105", "qty": "1", "quoteQty": "105", "realizedPnl": "0"},
        ]
        trades_pre, _ = tr.reconstruct_binance_trades([], [], fills_pre)
        self.assertEqual(len(trades_pre), 2)
        self.assertTrue(all(t["side"] == "LONG" for t in trades_pre))
        self.assertTrue(all(t["realized_pnl"] == 0.0 for t in trades_pre))

    def test_inconsistent_local_match_does_not_burn_record_for_next_cluster(self):
        t0 = 1795200000
        local_history = [
            {
                "symbol": "ETHUSDT",
                "side": "LONG",
                "entry_price": 100.0,
                "exit_price": 150.0,
                "amount": 1.0,  # realized_pnl = +50
                "entry_time": aae.get_beijing_time_str(t0 - 600),
                "closed_at": aae.get_beijing_time_str(t0 + 2),
                "status": "PROFIT_LOCKED",
                "trendline": {"id": "TL_GOOD", "p1": 110, "p2": 105},
            }
        ]
        # Cluster 1 at t0 (+5 PnL) fails pnl_consistent; Cluster 2 at t0+10s (+50 PnL) must still match TL_GOOD.
        income = [
            {"symbol": "ETHUSDT", "incomeType": "REALIZED_PNL", "tranId": 1, "tradeId": "1", "time": t0 * 1000, "income": "5", "asset": "USDT"},
            {"symbol": "ETHUSDT", "incomeType": "REALIZED_PNL", "tranId": 2, "tradeId": "2", "time": (t0 + 10) * 1000, "income": "50", "asset": "USDT"},
        ]
        trades, _ = tr.reconstruct_binance_trades(income, local_history, fills=[])
        self.assertEqual(len(trades), 2)
        self.assertEqual(trades[0]["source"], "BINANCE_REALTIME")
        self.assertIsNone(trades[0]["trendline_id"])
        self.assertEqual(trades[1]["source"], "BINANCE_REALTIME_VERIFIED")
        self.assertEqual(trades[1]["trendline_id"], "TL_GOOD")
        self.assertEqual(trades[1]["close_reason_code"], "TRAILING_PROFIT_LOCK")

    def test_resolve_close_reason_handles_profit_locked_and_stop_losses_with_trailing_flag(self):
        # Losing STOP_LOSS_TRIGGERED even if trailing_stop_triggered=True must remain trendline stop loss.
        losing_sl = te.enrich_trade_record(
            {
                "symbol": "BTCUSDT",
                "side": "LONG",
                "entry_price": 100.0,
                "exit_price": 95.0,
                "amount": 1.0,
                "trailing_stop_triggered": True,
                "status": "STOP_LOSS_TRIGGERED",
            },
            0,
        )
        self.assertEqual(losing_sl["close_reason"], "跌破趋势线止损")
        self.assertEqual(losing_sl["close_reason_code"], "TRENDLINE_BREAK_STOP")

        # Missing exit_price on SHORT STOP_LOSS_TRIGGERED must not treat 0.0 as profitable exit.
        short_no_exit = te.enrich_trade_record(
            {
                "symbol": "BTCUSDT",
                "side": "SHORT",
                "entry_price": 100.0,
                "amount": 1.0,
                "trailing_stop_triggered": True,
                "status": "STOP_LOSS_TRIGGERED",
            },
            1,
        )
        self.assertEqual(short_no_exit["close_reason_code"], "TRENDLINE_BREAK_STOP")

        # BREAK_TRENDLINE_STOP and MANUAL_CLOSE take precedence over trailing_stop_triggered.
        break_sl = te.enrich_trade_record(
            {
                "symbol": "ETHUSDT",
                "side": "LONG",
                "entry_price": 100.0,
                "exit_price": 101.0,
                "amount": 1.0,
                "trailing_stop_triggered": True,
                "status": "BREAK_TRENDLINE_STOP",
            },
            2,
        )
        self.assertEqual(break_sl["close_reason_code"], "TRENDLINE_BREAK_STOP")

        manual = te.enrich_trade_record(
            {
                "symbol": "ETHUSDT",
                "side": "LONG",
                "entry_price": 100.0,
                "exit_price": 101.0,
                "amount": 1.0,
                "trailing_stop_triggered": True,
                "status": "MANUAL_CLOSE",
            },
            3,
        )
        self.assertEqual(manual["close_reason_code"], "MANUAL_CLOSE")

        # Explicit PROFIT_LOCKED status maps to TRAILING_PROFIT_LOCK.
        profit_locked = te.enrich_trade_record(
            {
                "symbol": "SOLUSDT",
                "side": "LONG",
                "entry_price": 100.0,
                "exit_price": 101.0,
                "amount": 1.0,
                "status": "PROFIT_LOCKED",
            },
            4,
        )
        self.assertEqual(profit_locked["close_reason"], "动态移动保本锁利")
        self.assertEqual(profit_locked["close_reason_code"], "TRAILING_PROFIT_LOCK")

        # BINANCE_POSITION_CLOSED only becomes trailing lock when exit is non-losing.
        bin_win = te._resolve_close_reason(
            {"status": "BINANCE_POSITION_CLOSED", "trailing_stop_triggered": True},
            exit_price=102.0,
            entry_price=100.0,
            side="LONG",
        )
        self.assertEqual(bin_win, ("动态移动保本锁利", "TRAILING_PROFIT_LOCK"))
        bin_loss = te._resolve_close_reason(
            {"status": "BINANCE_POSITION_CLOSED", "trailing_stop_triggered": True},
            exit_price=98.0,
            entry_price=100.0,
            side="LONG",
        )
        self.assertEqual(bin_loss, ("币安实盘平仓", "BINANCE_AUTO_CLOSED"))

    def test_largest_win_and_loss_default_to_zero_when_one_sided(self):
        all_losses = [
            {"symbol": "A", "net_pnl": -50.0, "realized_pnl": -50.0, "fee": 0.0, "closed_at": "2026-11-20 10:00:00", "close_reason": "SL"},
            {"symbol": "B", "net_pnl": -20.0, "realized_pnl": -20.0, "fee": 0.0, "closed_at": "2026-11-20 11:00:00", "close_reason": "SL"},
        ]
        loss_summary, _, _ = qm.calculate_quant_risk_metrics(all_losses, [], None, initial_capital=1000.0)
        self.assertEqual(loss_summary["largest_win_usd"], 0.0)
        self.assertEqual(loss_summary["largest_loss_usd"], -50.0)

        all_wins = [
            {"symbol": "A", "net_pnl": 20.0, "realized_pnl": 20.0, "fee": 0.0, "closed_at": "2026-11-20 10:00:00", "close_reason": "TP"},
            {"symbol": "B", "net_pnl": 80.0, "realized_pnl": 80.0, "fee": 0.0, "closed_at": "2026-11-20 11:00:00", "close_reason": "TP"},
        ]
        win_summary, _, _ = qm.calculate_quant_risk_metrics(all_wins, [], None, initial_capital=1000.0)
        self.assertEqual(win_summary["largest_win_usd"], 80.0)
        self.assertEqual(win_summary["largest_loss_usd"], 0.0)

    def test_start_ms_transfer_boundary_consistent_strictly_gt_everywhere(self):
        start_ms = int(te.parse_to_timestamp("2026-11-20 10:00:00") * 1000)
        client = bc.BinanceFuturesClient("k", "s")
        engine = aae.AccountAnalyticsEngine(client=client)
        previous = {
            "recording": {
                "start_ms": start_ms,
                "started_at": aae.get_beijing_time_str(start_ms / 1000.0),
                "initial_equity": 1000.0,
            },
            "equity_curve": [],
        }
        income = [
            {"incomeType": "TRANSFER", "tranId": 1, "time": start_ms, "income": "500", "asset": "USDT"},
            {"incomeType": "TRANSFER", "tranId": 2, "time": start_ms + 1000, "income": "200", "asset": "USDT"},
            {"incomeType": "REALIZED_PNL", "symbol": "BTCUSDT", "tranId": 3, "tradeId": "10", "time": start_ms + 2000, "income": "60", "asset": "USDT"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "boundary.sqlite3"
            db_store.publish_report(db_path, previous)
            with patch.object(client, "get_server_time", return_value=start_ms + 10_000), patch.object(
                client,
                "_send_signed_request",
                side_effect=lambda endpoint, params=None: (
                    {"totalWalletBalance": "1260", "totalMarginBalance": "1260"}
                    if endpoint == "/fapi/v2/account"
                    else (income if endpoint == "/fapi/v1/income" else [])
                ),
            ):
                report = engine.run(database=db_path)

        self.assertAlmostEqual(report["summary"]["net_external_transfers"], 200.0)
        self.assertAlmostEqual(report["income_breakdown"]["transfer"], 200.0)
        self.assertEqual(len(report["daily_pnl"]), 1)
        self.assertAlmostEqual(report["daily_pnl"][0]["net_transfer"], 200.0)
        self.assertAlmostEqual(report["daily_pnl"][0]["return_pct"], 5.0, places=2)
        self.assertAlmostEqual(report["summary"]["total_return_pct"], 5.0, places=2)

    def test_delayed_transfer_clears_artificial_drawdown_while_preserving_genuine_intra_bucket_dd(self):
        start_ms = 1_800_000_000_000
        recording = {
            "start_ms": start_ms,
            "started_at": aae.get_beijing_time_str(start_ms / 1000.0),
            "initial_equity": 1000.0,
        }

        # 1) Genuine trading gain (+200 -> 1200), genuine trading drop (-150 -> 1050),
        #    then delayed withdrawal (-400 at +35s -> 650 at +40s) before income API syncs it.
        eq1, _ = qm.build_db_equity_curve(
            recording, start_ms, None, {"totalMarginBalance": "1200", "totalWalletBalance": "1200"}, [], now_ms=start_ms + 20_000
        )
        eq2, _ = qm.build_db_equity_curve(
            recording, None, {"recording": recording, "equity_curve": eq1}, {"totalMarginBalance": "1050", "totalWalletBalance": "1050"}, [], now_ms=start_ms + 30_000
        )
        eq3, _ = qm.build_db_equity_curve(
            recording, None, {"recording": recording, "equity_curve": eq2}, {"totalMarginBalance": "650", "totalWalletBalance": "650"}, [], now_ms=start_ms + 40_000
        )
        # Before transfer sync, artificial drawdown is 550.
        self.assertEqual(eq3[-1]["bucket_max_dd_usd"], 550.0)

        # Next 5m bucket: delayed withdrawal (start_ms + 35_000, -400) finally syncs!
        transfers = [(start_ms + 35_000, -400.0)]
        eq4, flow4 = qm.build_db_equity_curve(
            recording, None, {"recording": recording, "equity_curve": eq3}, {"totalMarginBalance": "650", "totalWalletBalance": "650"}, transfers, now_ms=start_ms + 360_000
        )
        self.assertEqual(flow4, -400.0)
        self.assertEqual(eq4[1]["bucket_max_dd_usd"], 150.0)
        self.assertEqual(eq4[1]["drawdown_usd"], 150.0)
        summary, _, _ = qm.calculate_quant_risk_metrics([], [], None, initial_capital=1000.0, equity_curve_override=eq4)
        self.assertEqual(summary["max_drawdown_usd"], 150.0)

        # 2) Delayed deposit: +500 arrives at +20s before income syncs it, inflating peak to 1500,
        #    then syncs at +360s -> peak_adjusted_equity must correct back to 1000 with 0 drawdown.
        dep1, _ = qm.build_db_equity_curve(
            recording, start_ms, None, {"totalMarginBalance": "1500", "totalWalletBalance": "1500"}, [], now_ms=start_ms + 20_000
        )
        dep2, _ = qm.build_db_equity_curve(
            recording, None, {"recording": recording, "equity_curve": dep1}, {"totalMarginBalance": "1500", "totalWalletBalance": "1500"}, [], now_ms=start_ms + 40_000
        )
        self.assertEqual(dep2[-1]["peak_adjusted_equity"], 1500.0)

        dep3, _ = qm.build_db_equity_curve(
            recording, None, {"recording": recording, "equity_curve": dep2}, {"totalMarginBalance": "1500", "totalWalletBalance": "1500"}, [(start_ms + 10_000, 500.0)], now_ms=start_ms + 360_000
        )
        self.assertEqual(dep3[1]["peak_adjusted_equity"], 1000.0)
        self.assertEqual(dep3[1]["drawdown_usd"], 0.0)
        self.assertEqual(dep3[-1]["drawdown_usd"], 0.0)

    def test_api_config_returns_env_tradingview_layout_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "cfg.sqlite3"
            server = api.DashboardHTTPServer(("127.0.0.1", 0), api.create_handler(db_path))
            port = server.server_address[1]
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            try:
                with patch.dict("os.environ", {"TRADINGVIEW_LAYOUT_ID": "CustomLayout99"}):
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    conn.request("GET", "/api/config")
                    resp = conn.getresponse()
                    body = resp.read().decode("utf-8")
                    conn.close()
                self.assertEqual(resp.status, 200)
                payload = json.loads(body)
                self.assertEqual(payload["tradingview_layout_id"], "CustomLayout99")
                self.assertEqual(payload["high_turnover_threshold"], api.HIGH_TURNOVER_THRESHOLD)
                self.assertEqual(payload["chinese_tv_map"], api.CHINESE_TV_MAP)
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
