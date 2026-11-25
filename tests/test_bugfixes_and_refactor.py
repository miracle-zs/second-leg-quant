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
from contextlib import closing
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
import db_store
import http_client
import tv_trendline_engine as tve


class TestTrendlineMatchingAndSelection(unittest.TestCase):
    """验证短币名精确匹配与 15m 有效趋势线筛选"""

    def test_exact_symbol_matching_prevents_substring_collision(self):
        drawings = {
            cfg.clean_symbol("BINANCE:ETHUSDT.P"): [{"id": "ETH_LINE", "p1": 3500.0, "t1": 1000, "p2": 3400.0, "t2": 2000}],
            cfg.clean_symbol("BINANCE:1000CATUSDT.P"): [{"id": "CAT_LINE", "p1": 0.05, "t1": 1000, "p2": 0.04, "t2": 2000}],
        }
        self.assertIsNone(bpc.match_symbol_drawings("HUSDT", drawings))
        self.assertIsNone(bpc.match_symbol_drawings("ATUSDT", drawings))
        eth_match = bpc.match_symbol_drawings("BINANCE:ETHUSDT.P", drawings)
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
        with self.assertRaises(ValueError):
            tve.calculate_log_resistance(p1=1.0, t1=1000, p2=100.0, t2=1010, target_t=100000)


class TestAccountAnalyticsFixes(unittest.TestCase):
    """验证账户分析引擎时区、本金基准、NoneType 防护与空单支持"""

    def test_parse_to_timestamp_utc_z_matches_beijing_plus_8h(self):
        ts_utc = aae.parse_to_timestamp("2026-11-23T00:00:00Z")
        ts_bj = aae.parse_to_timestamp("2026-11-23 08:00:00")
        self.assertAlmostEqual(ts_utc, ts_bj, places=3)

    def test_cached_principal_preserved_when_income_page_becomes_full(self):
        client = bpc.BinanceFuturesClient("demo", "demo")
        engine = aae.AccountAnalyticsEngine(client=client)
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - 2_000_000
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "account.sqlite3"
            prev_report = {
                "recording": {
                    "start_ms": start_ms,
                    "started_at": cfg.get_beijing_time_str(start_ms / 1000.0),
                    "initial_equity": 1336.0,
                },
                "equity_curve": [
                    {"timestamp": start_ms, "equity": 1336.0, "drawdown_usd": 0.0, "drawdown_pct": 0.0}
                ],
                "summary": {"initial_capital": 1336.0},
                "income_breakdown": {"principal_source": "recording_start"},
            }
            db_store.publish_report(db_path, prev_report)

            full_income = [
                {"incomeType": "REALIZED_PNL", "tranId": i + 1, "income": "1", "time": start_ms + i * 1000}
                for i in range(aae.INCOME_PAGE_LIMIT)
            ]

            def fake_signed(endpoint, params=None):
                if endpoint == "/fapi/v2/account":
                    return {"totalWalletBalance": "477.91", "totalMarginBalance": "477.91"}
                if endpoint == "/fapi/v1/income":
                    s_t = int(params["startTime"])
                    e_t = int(params["endTime"])
                    return [item for item in full_income if s_t <= int(item["time"]) <= e_t]
                return []

            with patch.object(engine, "load_local_positions_store", return_value=([], [])), patch.object(
                client,
                "_send_signed_request",
                side_effect=fake_signed,
            ):
                report = engine.run(database=db_path)

            self.assertEqual(report["summary"]["initial_capital"], 1336.0)
            self.assertEqual(report["income_breakdown"]["principal_source"], "recording_start")
            self.assertFalse(report["income_breakdown"]["page_full"])
            self.assertEqual(report["income_breakdown"]["raw_count"], aae.INCOME_PAGE_LIMIT)

    def test_database_first_run_with_existing_equity_table_does_not_crash(self):
        client = bpc.BinanceFuturesClient("demo", "demo")
        engine = aae.AccountAnalyticsEngine(client=client)
        status = {"connected": True, "mode": "BINANCE_REALTIME", "positions_available": True, "income_available": True}
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "account.sqlite3"
            with closing(sqlite3.connect(db_path)) as conn, conn:
                conn.execute("CREATE TABLE equity_observations (timestamp INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
            with patch.object(
                engine,
                "fetch_binance_data",
                return_value=({"totalWalletBalance": "500", "totalMarginBalance": "500"}, [], [], status),
            ), patch.object(engine, "load_local_positions_store", return_value=([], [])), patch.object(
                client, "_send_signed_request", return_value=[]
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
        self.assertFalse(enriched["risk_monitored"])
        self.assertEqual(enriched["amount"], 2.0)
        self.assertAlmostEqual(enriched["unrealized_pnl"], 300.0, places=2)
        self.assertAlmostEqual(enriched["pnl_pct"], 5.0, places=2)
        self.assertGreater(enriched["liquidation_price"], 3000.0)

        # 验证纯静态壳 + account.js 明确提示空单未纳入自动风控
        html_out = (ROOT_DIR / "web" / "account.html").read_text(encoding="utf-8")
        self.assertIn("static/styles.css", html_out)
        self.assertIn("static/account.js", html_out)
        self.assertNotIn('<script id="account-data"', html_out)
        self.assertIn("short-position-warning", html_out)
        self.assertIn("仅多头仓位纳入 15m 破线止损与移动保本风控", html_out)
        account_js = (ROOT_DIR / "web" / "account.js").read_text(encoding="utf-8")
        self.assertIn("未受风控监控", account_js)
        self.assertIn("未纳入风控", account_js)

    def test_decimal_fill_quantities_preserve_opening_time(self):
        engine = aae.AccountAnalyticsEngine(offline_mode=True)
        fills = [
            {"symbol": "ACEUSDT", "id": 1, "orderId": 1, "side": "BUY", "time": 1000, "positionSide": "LONG", "price": "1", "qty": "25922.21"},
            {"symbol": "ACEUSDT", "id": 2, "orderId": 2, "side": "SELL", "time": 2000, "positionSide": "LONG", "price": "1", "qty": "8806.15"},
            {"symbol": "ACEUSDT", "id": 3, "orderId": 2, "side": "SELL", "time": 3000, "positionSide": "LONG", "price": "1", "qty": "17116.06"},
        ]
        trades, _ = engine.reconstruct_binance_trades([], [], fills)
        self.assertEqual(trades[0]["entry_ts"], 1000)
        self.assertEqual(trades[0]["duration_seconds"], 2)

    def test_one_way_both_mode_multi_fill_close_reconstructs_side_prices_and_entry_time(self):
        """单向持仓 (positionSide=BOTH) 下多笔平仓合并应推断方向、重算加权平仓均价并补全开仓价与开仓时间"""
        client = bpc.BinanceFuturesClient("demo", "demo")
        engine = aae.AccountAnalyticsEngine(client=client)
        start = 1795200000000
        previous = {
            "recording": {"start_ms": start, "started_at": "2026-11-21 00:00:00", "initial_equity": 1000},
            "equity_curve": [{"timestamp": start, "equity": 1000, "drawdown_usd": 0, "drawdown_pct": 0}],
        }
        # 单向持仓开多 3 个 @100，分两笔平多：1 个 @110 (盈利 10)，2 个 @116 (盈利 32)，加权平仓均价 = (110+232)/3 = 114.0
        income = [
            {"incomeType": "REALIZED_PNL", "tranId": 11, "tradeId": "102", "time": start + 4000, "income": "10", "asset": "USDT", "symbol": "BTCUSDT"},
            {"incomeType": "REALIZED_PNL", "tranId": 12, "tradeId": "103", "time": start + 5000, "income": "32", "asset": "USDT", "symbol": "BTCUSDT"},
            {"incomeType": "COMMISSION", "tranId": 13, "tradeId": "102", "time": start + 4000, "income": "-0.1", "asset": "USDT", "symbol": "BTCUSDT"},
            {"incomeType": "COMMISSION", "tranId": 14, "tradeId": "103", "time": start + 5000, "income": "-0.2", "asset": "USDT", "symbol": "BTCUSDT"},
        ]
        fills = [
            {"symbol": "BTCUSDT", "id": 101, "orderId": 10, "side": "BUY", "time": start + 1000, "positionSide": "BOTH", "price": "100", "qty": "3", "quoteQty": "300"},
            {"symbol": "BTCUSDT", "id": 102, "orderId": 20, "side": "SELL", "time": start + 4000, "positionSide": "BOTH", "price": "110", "qty": "1", "quoteQty": "110"},
            {"symbol": "BTCUSDT", "id": 103, "orderId": 20, "side": "SELL", "time": start + 5000, "positionSide": "BOTH", "price": "116", "qty": "2", "quoteQty": "232"},
        ]
        status = {"connected": True, "mode": "BINANCE_REALTIME", "positions_available": True, "income_available": True}
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "account.sqlite3"
            db_store.publish_report(db_path, previous)
            with patch.object(
                engine,
                "fetch_binance_data",
                return_value=({"totalWalletBalance": "1041.7", "totalMarginBalance": "1041.7"}, income, [], status),
            ), patch.object(engine, "load_local_positions_store", return_value=([], [])), patch.object(
                client,
                "_send_signed_request",
                side_effect=lambda endpoint, params=None: income if endpoint == "/fapi/v1/income" else fills,
            ):
                report = engine.run(database=db_path)

        self.assertEqual(len(report["trade_history"]), 1)
        trade = report["trade_history"][0]
        self.assertEqual(trade["side"], "LONG")
        self.assertAlmostEqual(trade["amount"], 3.0, places=6)
        self.assertAlmostEqual(trade["exit_price"], 114.0, places=6)
        self.assertAlmostEqual(trade["entry_price"], 100.0, places=6)
        self.assertNotEqual(trade["entry_time"], "--")
        self.assertEqual(trade["duration_seconds"], 4)
        self.assertIsNotNone(trade["pnl_pct"])
        self.assertAlmostEqual(trade["net_pnl"], 41.7, places=2)


class TestConcurrentTransactionLocking(unittest.TestCase):
    """验证跨进程事务锁覆盖完整读-改-写过程，避免并发丢更新"""

    def test_position_store_transaction_prevents_lost_updates_across_processes(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.sqlite3"
            worker_code = """
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1])))
import trade_alert_daemon as tad
store = tad.PositionStore(Path(sys.argv[2]))
sym = sys.argv[3]
with store.transaction():
    time.sleep(0.05)
    store.add_position(sym, 1.23, amount=10.0)
"""
            p1 = subprocess.Popen([sys.executable, "-c", worker_code, str(SCRIPTS_DIR), str(db_path), "AAAUSDT"])
            p2 = subprocess.Popen([sys.executable, "-c", worker_code, str(SCRIPTS_DIR), str(db_path), "BBBUSDT"])
            self.assertEqual(p1.wait(timeout=10), 0)
            self.assertEqual(p2.wait(timeout=10), 0)

            import trade_alert_daemon as tad
            final_store = tad.PositionStore(db_path)
            syms = sorted(p["symbol"] for p in final_store.get_open_positions())
            self.assertEqual(syms, ["AAAUSDT", "BBBUSDT"])


class TestBinanceClientAndConfig(unittest.TestCase):
    """验证统一客户端时间偏移缓存与 config.py / db_store 工具"""

    def test_server_time_cached_within_ttl(self):
        client = bc.BinanceFuturesClient("demo", "demo", time_sync_ttl=600.0)
        with patch.object(
            bc.http_client,
            "request_json",
            return_value={"serverTime": int(time.time() * 1000)},
        ) as mock_req:
            t1 = client.get_server_time()
            t2 = client.get_server_time()
            self.assertGreater(t1, 0)
            self.assertGreaterEqual(t2, t1 - 5)
            self.assertEqual(mock_req.call_count, 1)

    def test_sqlite_transaction_and_dotenv_unclosed_quote(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.sqlite3"
            with db_store.transaction(db_path) as conn:
                conn.execute(
                    "INSERT INTO resonance_state (id, payload) VALUES (1, ?)",
                    (json.dumps({"initialized": True}),),
                )
            with self.assertRaises(RuntimeError):
                with db_store.transaction(db_path) as conn:
                    conn.execute("UPDATE resonance_state SET payload = '{}' WHERE id = 1")
                    raise RuntimeError("abort tx")
            loaded = db_store.load_resonance_state(db_path)
            self.assertTrue(loaded["initialized"])

            bad_env = Path(tmp) / ".env"
            bad_env.write_text('BINANCE_API_KEY="unclosed\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                cfg.load_dotenv(bad_env)


class TestDashboardAPIRoutes(unittest.TestCase):
    """验证 account_dashboard_api.py 静态壳路由、ETag 缓存、HEAD 请求与安全边界"""

    def _invoke_request(self, handler_cls, method: str, raw_path: str, headers: dict = None):
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
        if method == "HEAD":
            handler.do_HEAD()
        else:
            handler.do_GET()
        return status_box.get("code", 0), resp_headers, handler.wfile.getvalue()

    def _invoke_get(self, handler_cls, raw_path: str, headers: dict = None):
        return self._invoke_request(handler_cls, "GET", raw_path, headers=headers)

    def test_static_shells_redirects_apis_and_etags(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.sqlite3"
            db_store.publish_report(
                db_path,
                {
                    "summary": {"total_equity": 1234.56},
                    "active_positions": [],
                    "trade_history": [],
                    "equity_curve": [],
                    "daily_pnl": [],
                },
            )
            db_store.publish_candidates(
                db_path,
                [{"symbol": "BTCUSDT"}],
                updated_at="2026-11-23T12:00:00+08:00",
            )

            handler_cls = api.create_handler(db_path)

            # 1. Pure static shells & XSS immunity against X-Forwarded-Prefix
            xss_headers = {"X-Forwarded-Prefix": "</script><script>alert(1)</script>"}
            for route in ("/", "/index.html"):
                code, headers, body = self._invoke_get(handler_cls, route, headers=xss_headers)
                self.assertEqual(code, 200)
                self.assertEqual(headers["content-type"], "text/html; charset=utf-8")
                text = body.decode("utf-8")
                self.assertIn("static/styles.css", text)
                self.assertIn("static/candidates.js", text)
                self.assertNotIn("BTCUSDT", text)
                self.assertNotIn("<script>alert(1)</script>", text)

            code, headers, body = self._invoke_get(handler_cls, "/account.html")
            self.assertEqual(code, 200)
            account_html = body.decode("utf-8")
            self.assertIn("static/styles.css", account_html)
            self.assertIn("static/account.js", account_html)
            self.assertNotIn('<script id="account-data"', account_html)

            code, headers, body = self._invoke_get(handler_cls, "/playbook.html")
            self.assertEqual(code, 200)
            self.assertIn("static/styles.css", body.decode("utf-8"))

            # 2. Deleted legacy routes all return 404
            for deleted_route in (
                "/candidates",
                "/account/",
                "/strategy/",
                "/reports/strategy_second_leg_playbook.html",
                "/account?t=123",
                "/strategy",
                "/candidate-version",
            ):
                code, _, _ = self._invoke_get(handler_cls, deleted_route)
                self.assertEqual(code, 404, f"Expected 404 for deleted legacy route {deleted_route}")

            # 3. JSON APIs & ETag / 304 Not Modified + HEAD support
            code, headers, body = self._invoke_get(handler_cls, "/api/account?t=123")
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(body)["summary"]["total_equity"], 1234.56)
            etag = headers["etag"]
            code_304, headers_304, body_304 = self._invoke_get(
                handler_cls, "/api/account?t=123", headers={"If-None-Match": etag}
            )
            self.assertEqual(code_304, 304)
            self.assertEqual(headers_304["etag"], etag)
            self.assertEqual(body_304, b"")

            code_head, headers_head, body_head = self._invoke_request(handler_cls, "HEAD", "/api/account")
            self.assertEqual(code_head, 200)
            self.assertEqual(headers_head["etag"], etag)
            self.assertEqual(headers_head["content-length"], str(len(body)))
            self.assertEqual(body_head, b"")

            code_head_root, headers_head_root, body_head_root = self._invoke_request(handler_cls, "HEAD", "/")
            self.assertEqual(code_head_root, 200)
            self.assertEqual(headers_head_root["content-type"], "text/html; charset=utf-8")
            self.assertGreater(int(headers_head_root["content-length"]), 0)
            self.assertEqual(body_head_root, b"")

            code, headers, body = self._invoke_get(handler_cls, "/api/candidates")
            self.assertEqual(code, 200)
            cand_payload = json.loads(body)
            self.assertEqual(cand_payload["updated_at"], "2026-11-23T12:00:00+08:00")
            self.assertEqual(cand_payload["candidates"], [{"symbol": "BTCUSDT"}])
            cand_etag = headers["etag"]
            code_304, _, body_304 = self._invoke_get(
                handler_cls, "/api/candidates", headers={"If-None-Match": cand_etag}
            )
            self.assertEqual(code_304, 304)
            self.assertEqual(body_304, b"")

            code, _, body = self._invoke_get(handler_cls, "/api/candidate-version?v=1")
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(body)["updated_at"], "2026-11-23T12:00:00+08:00")

            # 4. Static assets & 404
            for asset in ("/static/account.js", "/static/candidates.js", "/static/styles.css"):
                code, headers, _ = self._invoke_get(handler_cls, asset)
                self.assertEqual(code, 200)
                self.assertIn("charset=utf-8", headers["content-type"])

            code, _, _ = self._invoke_get(handler_cls, "/unknown?x=1")
            self.assertEqual(code, 404)

            # 5. Missing static HTML shell returns 404 cleanly (no FileNotFoundError crash)
            empty_web = Path(tmp) / "empty_web"
            empty_web.mkdir()
            with patch.object(api, "WEB_DIR", empty_web):
                missing_handler_cls = api.create_handler(db_path)
                for shell_route in ("/", "/index.html", "/account.html", "/playbook.html"):
                    code, _, _ = self._invoke_get(missing_handler_cls, shell_route)
                    self.assertEqual(code, 404, f"Expected 404 when static HTML missing for {shell_route}")

    def test_static_path_traversal_and_binary_mime_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            web_dir = root / "web"
            sibling_secret = root / "web_secret"
            web_dir.mkdir()
            sibling_secret.mkdir()
            (web_dir / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            (sibling_secret / "secret.txt").write_text("TOP_SECRET", encoding="utf-8")
            db_path = root / "empty.sqlite3"

            with patch.object(api, "WEB_DIR", web_dir):
                handler_cls = api.create_handler(db_path)

                code, headers, body = self._invoke_get(handler_cls, "/static/logo.png")
                self.assertEqual(code, 200)
                self.assertEqual(headers["content-type"], "image/png")
                self.assertNotIn("charset", headers["content-type"])
                self.assertEqual(body, b"\x89PNG\r\n\x1a\n")

                # Sibling prefix directory bypass & absolute path bypass must both return 404
                code, _, _ = self._invoke_get(handler_cls, "/static/../web_secret/secret.txt")
                self.assertEqual(code, 404)
                code, _, _ = self._invoke_get(handler_cls, "/static//etc/passwd")
                self.assertEqual(code, 404)
                code, _, _ = self._invoke_get(handler_cls, "/static/")
                self.assertEqual(code, 404)

                # Missing DB returns 503 cleanly
                code, _, _ = self._invoke_get(handler_cls, "/api/account")
                self.assertEqual(code, 503)

    def test_http11_keepalive_server_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "live.sqlite3"
            db_store.publish_report(
                db_path,
                {
                    "summary": {"total_equity": 999.0},
                    "active_positions": [],
                    "trade_history": [],
                    "equity_curve": [],
                    "daily_pnl": [],
                },
            )
            handler_cls = api.create_handler(db_path)
            with api.DashboardHTTPServer(("127.0.0.1", 0), handler_cls) as server:
                port = server.server_address[1]
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                try:
                    conn.request("GET", "/api/account")
                    resp1 = conn.getresponse()
                    raw1 = resp1.read()
                    self.assertEqual(resp1.status, 200)
                    etag = resp1.getheader("ETag")
                    self.assertIsNotNone(etag)
                    self.assertEqual(json.loads(raw1)["summary"]["total_equity"], 999.0)

                    # Reuse same HTTP/1.1 connection for 304, HEAD, and 404
                    conn.request("GET", "/api/account", headers={"If-None-Match": etag})
                    resp2 = conn.getresponse()
                    raw2 = resp2.read()
                    self.assertEqual(resp2.status, 304)
                    self.assertEqual(raw2, b"")

                    conn.request("HEAD", "/api/account")
                    resp3 = conn.getresponse()
                    raw3 = resp3.read()
                    self.assertEqual(resp3.status, 200)
                    self.assertEqual(resp3.getheader("ETag"), etag)
                    self.assertEqual(raw3, b"")

                    conn.request("GET", "/account/")
                    resp4 = conn.getresponse()
                    resp4.read()
                    self.assertEqual(resp4.status, 404)
                finally:
                    conn.close()
                    server.shutdown()

    def test_binance_public_proxy_validation_caching_and_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "empty.sqlite3"
            handler_cls = api.create_handler(db_path)

            calls = []

            def fake_fetch(url: str, **_kwargs) -> bytes:
                calls.append(url)
                if "ticker/24hr" in url:
                    return b'[{"symbol":"YBUSDT","lastPrice":"0.5"}]'
                if "premiumIndex" in url:
                    return b'[{"symbol":"YBUSDT","lastFundingRate":"0.0001"}]'
                if "klines" in url:
                    return b'[[1700000000000,"0.5","0.6","0.4","0.55","1000"]]'
                raise AssertionError(f"Unexpected URL: {url}")

            with patch.object(http_client, "request_bytes", side_effect=fake_fetch):
                # ticker24hr is cached within TTL
                code1, _, body1 = self._invoke_get(handler_cls, "/api/binance/ticker24hr")
                code2, _, body2 = self._invoke_get(handler_cls, "/api/binance/ticker24hr")
                self.assertEqual(code1, 200)
                self.assertEqual(code2, 200)
                self.assertEqual(body1, body2)
                self.assertEqual(len(calls), 1)

                # premiumIndex works
                code, _, body = self._invoke_get(handler_cls, "/api/binance/premiumIndex")
                self.assertEqual(code, 200)
                self.assertEqual(json.loads(body)[0]["symbol"], "YBUSDT")

                # klines valid request is cached within 12s TTL
                kline_route = "/api/binance/klines?symbol=YBUSDT&interval=15m&limit=168"
                code_k1, _, body_k1 = self._invoke_get(handler_cls, kline_route)
                code_k2, _, body_k2 = self._invoke_get(handler_cls, kline_route)
                self.assertEqual(code_k1, 200)
                self.assertEqual(code_k2, 200)
                self.assertEqual(body_k1, body_k2)
                self.assertEqual(len(json.loads(body_k1)), 1)
                self.assertEqual(sum(1 for u in calls if "klines" in u), 1)

                # klines invalid parameters -> 400
                for bad_route in (
                    "/api/binance/klines?symbol=&interval=15m&limit=168",
                    "/api/binance/klines?symbol=YB/USDT&interval=15m&limit=168",
                    "/api/binance/klines?symbol=YBUSDT&interval=5m&limit=168",
                    "/api/binance/klines?symbol=YBUSDT&interval=15m&limit=999",
                    "/api/binance/klines?symbol=YBUSDT&interval=15m&limit=abc",
                ):
                    code, _, _ = self._invoke_get(handler_cls, bad_route)
                    self.assertEqual(code, 400, f"Expected 400 for {bad_route}")

            # 4xx HttpError forwards upstream status & body cleanly
            with patch.object(
                http_client,
                "request_bytes",
                side_effect=http_client.HttpError(
                    "HTTP 400",
                    status_code=400,
                    body='{"code":-1121,"msg":"Invalid symbol."}',
                ),
            ):
                code, _, body = self._invoke_get(
                    handler_cls, "/api/binance/klines?symbol=INVALIDUSDT&interval=15m&limit=60"
                )
                self.assertEqual(code, 400)
                self.assertEqual(json.loads(body)["code"], -1121)

            # Upstream HTTPException / transport failure -> 502
            with patch.object(
                http_client, "request_bytes", side_effect=http.client.HTTPException("bad gateway")
            ):
                code, _, body = self._invoke_get(
                    handler_cls, "/api/binance/klines?symbol=YBUSDT&interval=15m&limit=60"
                )
                self.assertEqual(code, 502)
                self.assertIn("error", json.loads(body))

    def test_static_assets_include_compiled_tailwind_and_vendored_chartjs(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler_cls = api.create_handler(Path(tmp) / "empty.sqlite3")
            for asset in ("/static/tailwind.css", "/static/vendor/chart.js"):
                code, headers, body = self._invoke_get(handler_cls, asset)
                self.assertEqual(code, 200)
                self.assertGreater(len(body), 1000)
                self.assertIn("charset=utf-8", headers["content-type"])

            for name in ("index.html", "account.html", "playbook.html"):
                html = (ROOT_DIR / "web" / name).read_text(encoding="utf-8")
                self.assertIn('href="static/tailwind.css"', html)
                self.assertNotIn("gstatic.com/antigravity/web/dev/tailwindcss", html)

            index_html = (ROOT_DIR / "web" / "index.html").read_text(encoding="utf-8")
            self.assertIn('<dialog id="chart-modal"', index_html)
            self.assertIn('aria-labelledby="modal-chart-title"', index_html)
            self.assertNotIn('role="dialog"', index_html)

            tailwind_css = (ROOT_DIR / "web" / "tailwind.css").read_text(encoding="utf-8")
            for required_class in ("max-w-5xl", "overflow-x-auto", "backdrop-blur-sm", "tabular-nums"):
                self.assertIn(required_class, tailwind_css)


class TestSecondPassRegressions(unittest.TestCase):
    """验证二次审查修复：daemon SQLite 持久化/退避、DB Sharpe 本金、scanner 工具与连接池"""

    def test_feature1_legacy_first_detected_does_not_raise_name_error(self):
        import trade_alert_daemon as tad

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.sqlite3"
            db_store.publish_candidates(
                db_path,
                [{"symbol": "YBUSDT", "is_resonance": True, "resonance_score": 2, "funding_rate_4h_pct": 0.01}],
            )
            db_store.save_resonance_state(
                db_path,
                {
                    "initialized": True,
                    "active_resonance_symbols": [],
                    "history": {
                        "YBUSDT": {
                            "symbol": "YBUSDT",
                            "first_detected": cfg.get_beijing_time_str(),
                            "last_notified_time": int(time.time()),
                        }
                    },
                },
            )
            store = tad.ResonanceStateStore(db_path)
            notifier = MagicMock()
            notifier.send_alert.return_value = True
            alerts = tad.check_feature_1_resonance(
                notifier, store, database_path=db_path, debounce_hours=24.0
            )
            self.assertEqual(alerts, [])
            notifier.send_alert.assert_not_called()

    def test_daemon_f2_retry_respects_cooldown_and_cap(self):
        import trade_alert_daemon as tad

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.sqlite3"
            d = tad.TradeAlertDaemon(
                {"database_path": str(db_path)},
                dry_run=True,
            )
            f2_calls = []

            def failing_f2(*args, **kwargs):
                f2_calls.append(1)
                if kwargs.get("status") is not None:
                    kwargs["status"]["had_error"] = True
                return []

            base_bucket = 1800000000
            with patch.object(tad, "check_feature_1_resonance", return_value=[]) as mock_f1, patch.object(
                tad, "check_feature_2_breakout", side_effect=failing_f2
            ), patch.object(tad, "check_feature_3_stop_loss", return_value=[]), patch.object(
                tad, "check_feature_4_trailing_stops", return_value=[]
            ):
                d.run_iteration(now=base_bucket + 15)   # 1st attempt
                d.run_iteration(now=base_bucket + 25)   # within 60s cooldown -> skipped
                d.run_iteration(now=base_bucket + 80)   # 2nd attempt
                d.run_iteration(now=base_bucket + 145)  # 3rd attempt -> hits cap
                d.run_iteration(now=base_bucket + 210)  # bucket closed -> skipped

            self.assertEqual(mock_f1.call_count, 1)
            self.assertEqual(len(f2_calls), 3)
            self.assertEqual(d.last_evaluated_15m_bucket, base_bucket)

    def test_db_mode_sharpe_uses_recording_initial_equity(self):
        client = bpc.BinanceFuturesClient("demo", "demo")
        engine = aae.AccountAnalyticsEngine(client=client)  # default 10000.0
        start = 1795200000000
        previous = {
            "recording": {"start_ms": start, "started_at": "2026-11-21 00:00:00", "initial_equity": 100.0},
            "equity_curve": [{"timestamp": start, "equity": 100.0, "drawdown_usd": 0.0, "drawdown_pct": 0.0}],
        }
        income = [
            {"incomeType": "REALIZED_PNL", "tranId": 1, "tradeId": "1", "time": start + 1000, "income": "20", "asset": "USDT", "symbol": "BTCUSDT"},
            {"incomeType": "REALIZED_PNL", "tranId": 2, "tradeId": "2", "time": start + 86400 * 1000 + 1000, "income": "-5", "asset": "USDT", "symbol": "BTCUSDT"},
        ]
        status = {"connected": True, "mode": "BINANCE_REALTIME", "positions_available": True, "income_available": True}
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "account.sqlite3"
            db_store.publish_report(db_path, previous)
            with patch.object(
                engine,
                "fetch_binance_data",
                return_value=({"totalWalletBalance": "115", "totalMarginBalance": "115"}, [], [], status),
            ), patch.object(engine, "load_local_positions_store", return_value=([], [])), patch.object(
                client,
                "_send_signed_request",
                side_effect=lambda endpoint, params=None: income if endpoint == "/fapi/v1/income" else [],
            ):
                report = engine.run(database=db_path)

        # Verify daily returns were computed against 100.0 (+20%, -4.17%), not 10000.0
        expected_sharpe, expected_sortino, _ = engine._compute_risk_ratios(
            report["daily_pnl"], report["trade_history"], 100.0
        )
        self.assertEqual(report["summary"]["sharpe_ratio"], expected_sharpe)
        self.assertEqual(report["summary"]["sortino_ratio"], expected_sortino)

    def test_scanner_helpers_and_connection_pool_reset(self):
        import scan_second_leg_pool as ssp

        self.assertEqual(ssp.extract_base_and_multiplier("1000PEPEUSDT"), ("PEPE", 1000.0))
        self.assertEqual(ssp.extract_base_and_multiplier("1MBABYDOGEUSDT"), ("BABYDOGE", 1_000_000.0))
        self.assertEqual(ssp.extract_base_and_multiplier("BTCUSDT"), ("BTC", 1.0))

        self.assertIsNone(ssp.calc_ema([1.0, 2.0], 3))
        self.assertAlmostEqual(ssp.calc_ema([1.0, 2.0, 3.0, 4.0, 5.0], 3), 4.0)

        # CMC HTTP 400 bisection skips invalid symbol while keeping valid symbols
        client = http_client.HttpClient(max_per_host=2)
        try:
            def fake_request(url, **kwargs):
                if "BAD" in url:
                    raise http_client.HttpError("HTTP 400", status_code=400, url=url)
                return {"data": {"GOOD": {"symbol": "GOOD"}}}

            with patch.object(client, "request_json", side_effect=fake_request):
                res = ssp._fetch_cmc_batch(["GOOD", "BAD"], "demo-key", client)
            self.assertIn("GOOD", res)
            self.assertNotIn("BAD", res)

            # HttpClient pool release and close clears tracked connections cleanly
            key = ("https", "example.com", 443)
            conn, from_pool = client._acquire_conn(key, timeout=5.0)
            self.assertFalse(from_pool)
            client._release_conn(key, conn)
            self.assertEqual(len(client._pools[key]), 1)
            client.close()
            self.assertEqual(client._pools, {})
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
