#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回归测试集：覆盖 Bug 修复与架构重构的核心模块
"""

import http.client
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import account_analytics_engine as aae
import account_dashboard_api as api
import binance_client as bc
import binance_position_client as bpc
import config as cfg
import tv_trendline_engine as tve


class TestTrendlineMatchingAndSelection(unittest.TestCase):
    """验证短币名精确匹配与 15m 有效趋势线筛选"""

    def test_exact_symbol_matching_prevents_substring_collision(self):
        drawings = {
            "BINANCE:ETHUSDT.P": [{"id": "ETH_LINE", "p1": 3500.0, "t1": 1000, "p2": 3400.0, "t2": 2000}],
            "BINANCE:1000CATUSDT.P": [{"id": "CAT_LINE", "p1": 0.05, "t1": 1000, "p2": 0.04, "t2": 2000}],
        }
        self.assertIsNone(bpc.match_symbol_drawings("HUSDT", drawings))
        self.assertIsNone(bpc.match_symbol_drawings("ATUSDT", drawings))
        eth_match = bpc.match_symbol_drawings("ETHUSDT", drawings)
        self.assertIsNotNone(eth_match)
        self.assertEqual(eth_match[0]["id"], "ETH_LINE")

    def test_select_best_position_trendline_filters_non_15m(self):
        now = time.time()
        lines = [
            {"id": "4H_LINE", "interval": "240", "p1": 10.0, "t1": now - 3600, "p2": 9.0, "t2": now - 600},
            {"id": "1D_LINE", "interval": "1D", "p1": 10.0, "t1": now - 3600, "p2": 9.0, "t2": now - 600},
            {"id": "STALE_15M", "interval": "15", "p1": 10.0, "t1": now - 500000, "p2": 9.0, "t2": now - 400000},
            {"id": "FRESH_15M", "interval": "15", "p1": 10.0, "t1": now - 7200, "p2": 9.5, "t2": now - 1800},
        ]
        chosen = bpc.select_best_position_trendline(lines, current_time=now)
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["id"], "FRESH_15M")
        self.assertIsNone(bpc.select_best_position_trendline([lines[0], lines[1]], current_time=now))

    def test_tv_log_resistance_overflow_guard(self):
        res = tve.calculate_log_resistance(p1=1.0, t1=1000, p2=100.0, t2=1010, target_t=100000)
        self.assertEqual(res, 0.0)


class TestAccountAnalyticsFixes(unittest.TestCase):
    """验证账户分析引擎时区、本金基准、NoneType 防护与空单支持"""

    def test_parse_to_timestamp_utc_z_matches_beijing_plus_8h(self):
        ts_utc = aae.parse_to_timestamp("2026-11-23T00:00:00Z")
        ts_bj = aae.parse_to_timestamp("2026-11-23 08:00:00")
        self.assertAlmostEqual(ts_utc, ts_bj, places=3)

    def test_cached_principal_preserved_when_income_page_becomes_full(self):
        client = bpc.BinanceFuturesClient("demo", "demo")
        engine = aae.AccountAnalyticsEngine(client=client)
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "account_analytics.json"
            prev_report = {
                "summary": {"initial_capital": 1336.0},
                "income_breakdown": {"principal_source": "income_window"},
            }
            out_path.write_text(json.dumps(prev_report), encoding="utf-8")

            full_income = [
                {"incomeType": "REALIZED_PNL", "income": "1", "time": 1795200000000 + i * 1000}
                for i in range(aae.INCOME_PAGE_LIMIT)
            ]
            with patch.object(engine, "load_local_positions_store", return_value=([], [])), patch.object(
                client,
                "_send_signed_request",
                side_effect=[
                    ({"totalWalletBalance": "477.91", "totalMarginBalance": "477.91"}, None),
                    (full_income, None),
                    ([], None),
                ],
            ):
                report = engine.run(out_path)

            self.assertEqual(report["summary"]["initial_capital"], 1336.0)
            self.assertEqual(report["income_breakdown"]["principal_source"], "cached_baseline")
            self.assertTrue(report["income_breakdown"]["page_full"])

    def test_database_first_run_with_existing_equity_table_does_not_crash(self):
        client = bpc.BinanceFuturesClient("demo", "demo")
        engine = aae.AccountAnalyticsEngine(client=client)
        status = {"connected": True, "mode": "BINANCE_REALTIME", "positions_available": True, "income_available": True}
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "account.sqlite3"
            with sqlite3.connect(db_path) as conn:
                conn.execute("CREATE TABLE equity_observations (timestamp INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
            with patch.object(
                engine,
                "fetch_binance_data",
                return_value=({"totalWalletBalance": "500", "totalMarginBalance": "500"}, [], [], status),
            ), patch.object(engine, "load_local_positions_store", return_value=([], [])), patch.object(
                client, "_send_signed_request", return_value=([], None)
            ):
                report = engine.run(database=db_path)
            self.assertEqual(report["recording"]["initial_equity"], 500.0)

    def test_short_position_enrichment_and_pnl_direction(self):
        engine = aae.AccountAnalyticsEngine(offline_mode=True)
        raw_short = {
            "symbol": "ETHUSDT",
            "positionAmt": "-2.0",
            "entryPrice": "3000.0",
            "markPrice": "2850.0",
            "leverage": "10",
        }
        enriched = engine.enrich_active_position(raw_short, 0)
        self.assertEqual(enriched["side"], "SHORT")
        self.assertEqual(enriched["amount"], 2.0)
        self.assertAlmostEqual(enriched["unrealized_pnl"], 300.0, places=2)
        self.assertAlmostEqual(enriched["pnl_pct"], 5.0, places=2)
        self.assertGreater(enriched["liquidation_price"], 3000.0)


class TestBinanceClientAndConfig(unittest.TestCase):
    """验证统一客户端时间偏移缓存与 config.py 工具"""

    def test_server_time_cached_within_ttl(self):
        client = bc.BinanceFuturesClient("demo", "demo", time_sync_ttl=600.0)
        mock_resp = MagicMock()
        mock_resp.__enter__.return_value.read.return_value = json.dumps({"serverTime": int(time.time() * 1000)}).encode()

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
            t1 = client.get_server_time()
            t2 = client.get_server_time()
            self.assertGreater(t1, 0)
            self.assertGreaterEqual(t2, t1 - 5)
            self.assertEqual(mock_open.call_count, 1)

    def test_config_file_lock_and_atomic_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "state.json"
            with cfg.file_lock(p):
                cfg.atomic_write_json(p, {"ok": True, "count": 42})
            loaded = json.loads(p.read_text(encoding="utf-8"))
            self.assertEqual(loaded, {"ok": True, "count": 42})


class TestDashboardAPIRoutes(unittest.TestCase):
    """验证 account_dashboard_api.py 的 query 参数、本地直连与静态资源服务"""

    def _invoke_get(self, handler_cls, raw_path: str, headers: dict = None):
        import io
        handler = handler_cls.__new__(handler_cls)
        handler.path = raw_path
        handler.headers = headers or {}
        handler.wfile = io.BytesIO()
        status_box = {}
        resp_headers = {}

        def fake_send_response(code, message=None):
            status_box["code"] = code

        def fake_send_header(k, v):
            resp_headers[k.lower()] = v

        def fake_end_headers():
            pass

        def fake_send_error(code, message=None, explain=None):
            status_box["code"] = code

        handler.send_response = fake_send_response
        handler.send_header = fake_send_header
        handler.end_headers = fake_end_headers
        handler.send_error = fake_send_error
        handler.log_error = lambda *a, **kw: None
        handler.do_GET()
        return status_box.get("code", 0), resp_headers, handler.wfile.getvalue()

    def test_api_handles_query_params_and_static_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.sqlite3"
            api.publish_report(
                db_path,
                {
                    "summary": {"total_equity": 1234.56},
                    "active_positions": [],
                    "trade_history": [],
                    "equity_curve": [],
                    "daily_pnl": [],
                },
            )
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "CREATE TABLE candidate_snapshot (id INTEGER PRIMARY KEY, updated_at TEXT NOT NULL, payload TEXT NOT NULL)"
                )
                conn.execute(
                    "INSERT INTO candidate_snapshot VALUES (1, '2026-11-23 12:00:00', ?)",
                    (json.dumps([]),),
                )

            handler_cls = api.create_handler(db_path)

            code, _, body = self._invoke_get(handler_cls, "/account?t=123")
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(body)["summary"]["total_equity"], 1234.56)

            code, _, body = self._invoke_get(handler_cls, "/candidate-version?v=1")
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(body)["updated_at"], "2026-11-23 12:00:00")

            code, _, body = self._invoke_get(handler_cls, "/candidates?t=999")
            self.assertEqual(code, 200)
            self.assertIn("api/candidate-version", body.decode("utf-8"))

            code, _, body = self._invoke_get(handler_cls, "/static/account.js")
            self.assertEqual(code, 200)
            self.assertIn("renderDashboard", body.decode("utf-8"))

            code, _, body = self._invoke_get(handler_cls, "/static/styles.css")
            self.assertEqual(code, 200)
            self.assertIn("kpi-card", body.decode("utf-8"))

            code, _, _ = self._invoke_get(handler_cls, "/unknown?x=1")
            self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main()
