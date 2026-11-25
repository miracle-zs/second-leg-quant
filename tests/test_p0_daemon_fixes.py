#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for P0/P1 daemon, web, and systemd fixes."""

import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
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

        def fake_drawings(_layout):
            self._assert_sqlite_unlocked()
            self.assertIsNone(store._tx_conn)
            return {"YBUSDT": [line1, line2]}

        def fake_candle(_sym):
            self._assert_sqlite_unlocked()
            self.assertIsNone(store._tx_conn)
            return candle

        send_results = [False, True]

        def fake_send(_title, _desp):
            self._assert_sqlite_unlocked()
            self.assertIsNone(store._tx_conn)
            return send_results.pop(0)

        with patch.object(tad, "fetch_layout_drawings", side_effect=fake_drawings), patch.object(
            tad, "get_closed_15m_candle", side_effect=fake_candle
        ), patch.object(self.notifier, "send_alert", side_effect=fake_send):
            alerts = tad.check_feature_2_breakout(self.notifier, store)

        self.assertEqual([a["trendline_id"] for a in alerts], ["L2"])
        reloaded = tad.ResonanceStateStore(self.db_path)
        self.assertIn("YBUSDT_L1", reloaded.data["pending_breakouts"])
        self.assertTrue(reloaded.has_breakout_notified("YBUSDT_L2"))

    def test_feature_3_releases_db_lock_requires_open_time_and_retries_only_pending(self):
        pos_store = tad.PositionStore(self.db_path)
        tl = {"id": "SL1", "interval": "15", "p1": 0.00010, "t1": 0, "p2": 0.00008, "t2": 100}
        pos_store.add_position("MICRO1USDT", entry_price=0.00009, amount=1000.0, trendline=tl, source="BINANCE_SYNC")
        pos_store.add_position("SAFE2USDT", entry_price=1.0, amount=10.0, trendline={"id": "SL2", "interval": "15", "p1": 0.5, "t1": 0, "p2": 0.4, "t2": 100}, source="BINANCE_SYNC")

        # Missing open_time must be skipped with had_error=True rather than falling back to close_time
        bad_candle = {"symbol": "MICRO1USDT", "close_time": 50, "close": 0.00005}
        st_bad = {}
        with patch.object(tad, "get_closed_15m_candle", side_effect=[bad_candle, {"symbol": "SAFE2USDT", "open_time": 50, "close_time": 949, "close": 1.2}]):
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
        with patch.object(tad, "get_closed_15m_candle", side_effect=[hit_candle, safe_candle]), patch.object(
            self.notifier, "send_alert", side_effect=fake_send
        ):
            alerts = tad.check_feature_3_stop_loss(self.notifier, pos_store, status=st_fail)
        self.assertEqual(alerts, [])
        self.assertTrue(st_fail.get("had_error"))

        # Daemon intra-bucket retry must pass retry_only=True so SAFE2USDT does NOT hit Binance/TV every 10s
        daemon = tad.TradeAlertDaemon({"database_path": str(self.db_path)}, dry_run=True)
        with patch.object(tad, "get_closed_15m_candle", side_effect=AssertionError("must not fetch klines on retry_only")), patch.object(
            tad, "fetch_layout_drawings", side_effect=AssertionError("must not fetch drawings on retry_only")
        ), patch.object(daemon.notifier, "send_alert", return_value=True) as mock_send, patch.object(
            tad, "check_feature_4_trailing_stops", return_value=[]
        ):
            daemon.run_iteration(now=1800000005)  # seconds_into_bucket < 10 -> else branch
        self.assertEqual(mock_send.call_count, 1)
        self.assertTrue(tad.PositionStore(self.db_path).get_open_positions()[0]["stop_loss_notified"])

    def test_feature_4_releases_db_lock_handles_none_highest_and_micro_price(self):
        db_store.upsert_open_position(
            self.db_path,
            {
                "symbol": "PEPEUSDT",
                "entry_price": 0.00004000,
                "amount": 100000.0,
                "highest_price": None,
                "trailing_stop_triggered": False,
                "profit_lock_notified": False,
                "status": "OPEN",
            },
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

        with patch.object(tad, "fetch_ticker_prices", side_effect=fake_tickers), patch.object(
            self.notifier, "send_alert", side_effect=fake_send
        ):
            events = tad.check_feature_4_trailing_stops(self.notifier, pos_store, activation_pct=6.0, lock_pct=1.0)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "TRAILING_ACTIVATED")
        self.assertIn("$0.00004500", sent[0])

    def test_empty_tv_cache_honored_list_pos_warns_and_rotating_log(self):
        layout_id = "empty-layout"
        with tempfile.TemporaryDirectory() as directory:
            runs_dir = Path(directory)
            cache_file = runs_dir / f"tradingview_drawings_{layout_id}.json"
            cache_file.write_text(json.dumps({"drawings_by_symbol": {}}), encoding="utf-8")
            with patch.object(tad, "RUNS_DIR", runs_dir), patch.object(
                tad, "_DRAWINGS_CACHE", {"data": None, "layout_id": "", "ts": 0.0}
            ), patch.object(tve, "get_layout_drawings", side_effect=AssertionError("should honor empty cache")):
                self.assertEqual(tad.fetch_layout_drawings(layout_id, ttl_sec=60), {})

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

        cand_js = (ROOT_DIR / "web/candidates.js").read_text(encoding="utf-8")
        self.assertLess(cand_js.index("const prevTrText"), cand_js.index("c.turnover_ratio = trVal"))
        self.assertIn("toBeijingDate", cand_js)
        self.assertIn("else if (modalHoverIndex !== -1)", cand_js)
        self.assertIn("checkSnapshotVersion().then", cand_js)

        acct_js = (ROOT_DIR / "web/account.js").read_text(encoding="utf-8")
        self.assertIn("mode === 'OFFLINE_FALLBACK'", acct_js)
        self.assertIn("接口状态异常", acct_js)
        self.assertIn("p.risk_monitored === true", acct_js)
        self.assertIn("Invalid /api/account payload", acct_js)


class TestDaemonAndWebFailFastContracts(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = Path(self.test_dir) / "test.sqlite3"
        self.notifier = tad.ServerChanNotifier(dry_run=True)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_position_store_requires_status_field(self):
        db_store.upsert_open_position(self.db_path, {"symbol": "NOSTATUSUSDT", "entry_price": 1.0})
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
            tad._send_breakout_notification(
                self.notifier, "YBUSDT", 1.0, 1.1, 10.0, {}, "ig0LuDL8"
            )
        sent = []
        with patch.object(self.notifier, "send_alert", side_effect=lambda t, d: sent.append(d) or True):
            ok = tad._send_breakout_notification(
                self.notifier, "YBUSDT", 1.0, 1.1, 10.0, {"open_time": 0}, "ig0LuDL8"
            )
        self.assertTrue(ok)
        self.assertIn("1970-01-01 08:00:00", sent[0])

    def test_feature_3_requires_amount_and_does_not_mask_corrupt_pending_candle(self):
        tl = {"id": "SL1", "interval": "15", "p1": 2.0, "t1": 0, "p2": 1.5, "t2": 100}
        db_store.upsert_open_position(
            self.db_path,
            {
                "symbol": "CORRUPTPENDINGUSDT",
                "entry_price": 1.8,
                "amount": 10.0,
                "status": "OPEN",
                "trendline": tl,
                "pending_stop_loss_candle": {},
            },
        )
        pos_store = tad.PositionStore(self.db_path)
        st = {}
        with patch.object(tad, "get_closed_15m_candle", side_effect=AssertionError("must not bypass corrupt pending")):
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
            tad,
            "get_closed_15m_candle",
            return_value={"symbol": "NOAMTUSDT", "open_time": 50, "close_time": 949, "close": 1.0},
        ):
            with self.assertRaises(KeyError):
                tad.check_feature_3_stop_loss(self.notifier, pos_store2)

    def test_feature_4_fails_fast_on_corrupt_trailing_stop_or_ticker(self):
        db_store.upsert_open_position(
            self.db_path,
            {
                "symbol": "BADLOCKUSDT",
                "entry_price": 1.0,
                "amount": 10.0,
                "status": "OPEN",
                "trailing_stop_triggered": True,
                "trailing_stop_price": None,
                "profit_lock_notified": False,
            },
        )
        pos_store = tad.PositionStore(self.db_path)
        with patch.object(tad, "fetch_ticker_prices", return_value={"BADLOCKUSDT": 1.05}):
            with self.assertRaises(ValueError):
                tad.check_feature_4_trailing_stops(self.notifier, pos_store)

        db_store.save_open_positions(
            self.db_path,
            [{"symbol": "TICKUSDT", "entry_price": 1.0, "amount": 10.0, "status": "OPEN"}],
        )
        pos_store2 = tad.PositionStore(self.db_path)
        with patch.object(tad, "fetch_ticker_prices", return_value={"TICKUSDT": 0.0}):
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
            const candCode = fs.readFileSync(path.join(root, 'web/candidates.js'), 'utf8');
            const acctCode = fs.readFileSync(path.join(root, 'web/account.js'), 'utf8');

            // 1. common.js getTvSymbol fails fast on empty symbol
            {
              const win = {};
              win.window = win;
              vm.runInNewContext(commonCode, win);
              assert.throws(() => win.SecondLegCommon.getTvSymbol(''), /non-empty symbol/);
              assert.strictEqual(win.SecondLegCommon.getTvSymbol('BTCUSDT'), 'BINANCE:BTCUSDT.P');
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
              // Expose loadCandidateSnapshot via trigger or run directly by checking initial error path
              payloadToServe = {
                updated_at: '2026-11-25 12:00:00+08:00',
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
              const candTestCode = candCode.replace(
                /\\}\\)\\(\\);\\s*$/,
                'window.__candTest = { updateKpiCards, loadCandidateSnapshot };})();'
              );
              vm.runInNewContext(candTestCode, win2);
              win2.__candTest.updateKpiCards(payloadToServe.candidates);
              assert.strictEqual(kpiEls['kpi-neg-fr-sample'].textContent, '贴水深: REALFR(-0.01%)');
              assert.strictEqual(kpiEls['kpi-high-tr-sample'].textContent, '最高: REALFR(2.5x)');

              // Malformed candidate item (missing symbol) must fail fast
              payloadToServe = { updated_at: '2026-11-25 12:00:00+08:00', candidates: [{ price: 1.0 }] };
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

              win3.__acctTest.updateKpis({}, [{ symbol: 'BTCUSDT', side: 'LONG' }]);
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


if __name__ == "__main__":
    unittest.main()
