#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0-3 / P0-4 security tests for account_dashboard_api.py and Nginx locations config."""

from __future__ import annotations

import http.client
import io
import json
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import account_dashboard_api as api  # noqa: E402
import db_store  # noqa: E402
import http_client  # noqa: E402


class TestAPISecurityP0(unittest.TestCase):
    def _invoke(
        self,
        handler_cls,
        method: str,
        raw_path: str,
        *,
        headers: dict[str, str] | None = None,
        client_address: tuple[str, int] | None = None,
    ) -> tuple[int, dict[str, str], bytes, list[tuple]]:
        handler = handler_cls.__new__(handler_cls)
        handler.path = raw_path
        handler.headers = headers or {}
        if client_address is not None:
            handler.client_address = client_address
        handler.wfile = io.BytesIO()
        status_box: dict[str, int] = {}
        resp_headers: dict[str, str] = {}
        error_logs: list[tuple] = []

        handler.send_response = lambda code, message=None: status_box.__setitem__("code", code)
        handler.send_header = lambda k, v: resp_headers.__setitem__(k.lower(), v)
        handler.end_headers = lambda: None
        handler.send_error = lambda code, message=None, explain=None: status_box.__setitem__("code", code)
        handler.log_error = lambda *args, **kwargs: error_logs.append(args)

        if method == "HEAD":
            handler.do_HEAD()
        else:
            handler.do_GET()
        return status_box.get("code", 0), resp_headers, handler.wfile.getvalue(), error_logs

    def test_klines_symbol_interval_and_limit_validation(self):
        valid_symbols = (
            "BTCUSDT",
            "YBUSDT",
            "1000PEPEUSDT",
            "1MBABYDOGEUSDT",
            "4USDT",
            "ETHUSDC",
            "BTCBUSD",
            "龙虾USDT",
            "哈基米USDT",
            "我踏马来了USDT",
            "币安人生USDT",
            "牛来USDT",
        )
        for sym in valid_symbols:
            self.assertTrue(api._is_valid_symbol(sym), f"Expected valid symbol: {sym!r}")

        invalid_symbols = (
            "",
            "A",
            "USDT",
            "USDC",
            "BTC",
            "123456",
            "btcusdt",
            "YB/USDT",
            "BTC-USDT",
            "BTC_USDT",
            "BTC USDT",
            "../etc/passwd",
            "BTCUSDT?foo=1",
            "BTCUSDT#frag",
            "BTCUSDT%00",
            "BTCUSDT\n",
            "²USDT",
            "ＢＴＣUSDT",
            "A" * 29 + "USDT",
        )
        for sym in invalid_symbols:
            self.assertFalse(api._is_valid_symbol(sym), f"Expected invalid symbol: {sym!r}")

        with tempfile.TemporaryDirectory() as tmp:
            handler_cls = api.create_handler(Path(tmp) / "sec.sqlite3")
            with patch.object(http_client, "request_bytes", return_value=b"[[1,2,3]]") as mock_req:
                for route in (
                    "/api/binance/klines?symbol=1000pepeusdt&interval=15m&limit=120",
                    "/api/binance/klines?symbol=%E6%88%91%E8%B8%8F%E9%A9%AC%E6%9D%A5%E4%BA%86USDT&interval=1h&limit=60",
                    "/api/binance/klines?symbol=ETHUSDC&interval=4h&limit=1",
                    "/api/binance/klines?symbol=BTCUSDT&interval=1d&limit=500",
                ):
                    code, _, body, _ = self._invoke(handler_cls, "GET", route)
                    self.assertEqual(code, 200, f"Expected 200 for {route}: {body!r}")
                self.assertEqual(mock_req.call_count, 4)

                bad_routes = (
                    "/api/binance/klines?symbol=&interval=15m&limit=168",
                    "/api/binance/klines?symbol=BTC&interval=15m&limit=168",
                    "/api/binance/klines?symbol=USDT&interval=15m&limit=168",
                    "/api/binance/klines?symbol=BTC_USDT&interval=15m&limit=168",
                    "/api/binance/klines?symbol=BTC-USDT&interval=15m&limit=168",
                    "/api/binance/klines?symbol=BTCUSDT&interval=1m&limit=168",
                    "/api/binance/klines?symbol=BTCUSDT&interval=15M&limit=168",
                    "/api/binance/klines?symbol=BTCUSDT&interval=15m&limit=0",
                    "/api/binance/klines?symbol=BTCUSDT&interval=15m&limit=-5",
                    "/api/binance/klines?symbol=BTCUSDT&interval=15m&limit=501",
                    "/api/binance/klines?symbol=BTCUSDT&interval=15m&limit=1_0",
                    "/api/binance/klines?symbol=BTCUSDT&interval=15m&limit=%2B10",
                    "/api/binance/klines?symbol=BTCUSDT&interval=15m&limit=",
                    "/api/binance/klines?symbol=BTCUSDT&symbol=ETHUSDT&interval=15m&limit=10",
                    "/api/binance/klines?symbol=BTCUSDT&interval=15m&interval=1h&limit=10",
                    "/api/binance/klines?symbol=BTCUSDT&interval=15m&limit=10&limit=20",
                )
                for bad_route in bad_routes:
                    code, hdrs, body, _ = self._invoke(handler_cls, "GET", bad_route)
                    self.assertEqual(code, 400, f"Expected 400 for {bad_route}")
                    self.assertEqual(json.loads(body), {"error": "Invalid klines parameters"})
                    self.assertIn("content-security-policy", hdrs)

                self.assertEqual(mock_req.call_count, 4)

    def test_502_and_empty_4xx_upstream_errors_do_not_leak_internal_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler_cls = api.create_handler(Path(tmp) / "err.sqlite3")

            secret_url = "https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=15m&limit=168"
            internal_exc = http_client.HttpError(
                f"HTTP 503 from {secret_url}: internal-stack-trace-secret",
                status_code=503,
                body="internal-stack-trace-secret",
                url=secret_url,
            )
            with patch.object(http_client, "request_bytes", side_effect=internal_exc):
                code, hdrs, body, logs = self._invoke(
                    handler_cls, "GET", "/api/binance/klines?symbol=BTCUSDT&interval=15m&limit=168"
                )
            self.assertEqual(code, 502)
            payload = json.loads(body)
            self.assertEqual(payload, {"error": "Binance upstream error"})
            self.assertNotIn("fapi.binance.com", body.decode("utf-8"))
            self.assertNotIn("internal-stack-trace-secret", body.decode("utf-8"))
            self.assertIn("x-content-type-options", hdrs)
            self.assertEqual(len(logs), 1)

            with patch.object(
                http_client,
                "request_bytes",
                side_effect=OSError("dial tcp 10.0.0.8:443: i/o timeout"),
            ):
                code_net, _, body_net, logs_net = self._invoke(
                    handler_cls, "GET", "/api/binance/ticker24hr"
                )
            self.assertEqual(code_net, 502)
            self.assertEqual(json.loads(body_net), {"error": "Binance upstream error"})
            self.assertNotIn("10.0.0.8", body_net.decode("utf-8"))
            self.assertEqual(len(logs_net), 1)

            # Empty-body 4xx must not fall back to str(exc) containing upstream URL
            with patch.object(
                http_client,
                "request_bytes",
                side_effect=http_client.HttpError(
                    f"HTTP 404 from {secret_url}: ",
                    status_code=404,
                    body="",
                    url=secret_url,
                ),
            ):
                code_404, _, body_404, _ = self._invoke(
                    handler_cls, "GET", "/api/binance/klines?symbol=ETHUSDT&interval=15m&limit=10"
                )
            self.assertEqual(code_404, 404)
            self.assertEqual(json.loads(body_404), {"error": "Upstream HTTP 404"})
            self.assertNotIn("fapi.binance.com", body_404.decode("utf-8"))

    def test_binance_rate_limiting_per_client_ip_and_spoof_protection(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "rl.sqlite3"
            db_store.publish_candidates(db_path, [{"symbol": "BTCUSDT"}], updated_at="2026-11-26T10:00:00+08:00")
            handler_cls = api.create_handler(db_path, rate_limit_max=3, rate_limit_window_sec=30.0)

            clock = [1000.0]
            with (
                patch.object(api.time, "monotonic", side_effect=lambda: clock[0]),
                patch.object(http_client, "request_bytes", return_value=b"[[1,2,3]]"),
            ):
                # 3 requests from proxied IP 203.0.113.10 succeed
                for i in range(3):
                    code, _, _, _ = self._invoke(
                        handler_cls,
                        "GET",
                        f"/api/binance/klines?symbol=BTCUSDT&interval=15m&limit={10 + i}",
                        headers={"X-Real-IP": "203.0.113.10"},
                        client_address=("127.0.0.1", 50000),
                    )
                    self.assertEqual(code, 200)

                # 4th request from same IP -> 429 with Retry-After and security headers
                code_429, hdr_429, body_429, _ = self._invoke(
                    handler_cls,
                    "GET",
                    "/api/binance/ticker24hr",
                    headers={"X-Real-IP": "203.0.113.10"},
                    client_address=("127.0.0.1", 50001),
                )
                self.assertEqual(code_429, 429)
                self.assertIn("retry-after", hdr_429)
                self.assertGreaterEqual(int(hdr_429["retry-after"]), 1)
                self.assertEqual(hdr_429.get("cache-control"), "no-store")
                self.assertEqual(hdr_429.get("x-content-type-options"), "nosniff")
                self.assertIn("Rate limit", json.loads(body_429)["error"])

                # HEAD also gets 429 with empty body
                code_head, hdr_head, body_head, _ = self._invoke(
                    handler_cls,
                    "HEAD",
                    "/api/binance/premiumIndex",
                    headers={"X-Real-IP": "203.0.113.10"},
                    client_address=("127.0.0.1", 50002),
                )
                self.assertEqual(code_head, 429)
                self.assertIn("retry-after", hdr_head)
                self.assertEqual(body_head, b"")

                # Different proxied IP still succeeds independently
                code_other, _, _, _ = self._invoke(
                    handler_cls,
                    "GET",
                    "/api/binance/klines?symbol=BTCUSDT&interval=15m&limit=10",
                    headers={"X-Forwarded-For": "198.51.100.2, 203.0.113.20"},
                    client_address=("127.0.0.1", 50003),
                )
                self.assertEqual(code_other, 200)

                # Non-Binance API & static routes remain unaffected by Binance rate limit
                code_cfg, _, _, _ = self._invoke(
                    handler_cls,
                    "GET",
                    "/api/config",
                    headers={"X-Real-IP": "203.0.113.10"},
                    client_address=("127.0.0.1", 50004),
                )
                self.assertEqual(code_cfg, 200)

                # Untrusted non-loopback peer cannot spoof X-Real-IP to bypass limit
                for _ in range(3):
                    c_ok, _, _, _ = self._invoke(
                        handler_cls,
                        "GET",
                        "/api/binance/klines?symbol=BTCUSDT&interval=15m&limit=10",
                        headers={"X-Real-IP": "192.0.2.1"},
                        client_address=("198.51.100.77", 50005),
                    )
                    self.assertEqual(c_ok, 200)
                c_spoof_blocked, _, _, _ = self._invoke(
                    handler_cls,
                    "GET",
                    "/api/binance/klines?symbol=BTCUSDT&interval=15m&limit=10",
                    headers={"X-Real-IP": "192.0.2.99"},
                    client_address=("198.51.100.77", 50006),
                )
                self.assertEqual(c_spoof_blocked, 429)

                # After window expires, 203.0.113.10 is allowed again
                clock[0] += 31.0
                code_reset, _, _, _ = self._invoke(
                    handler_cls,
                    "GET",
                    "/api/binance/klines?symbol=BTCUSDT&interval=15m&limit=10",
                    headers={"X-Real-IP": "203.0.113.10"},
                    client_address=("127.0.0.1", 50007),
                )
                self.assertEqual(code_reset, 200)

    def test_security_headers_across_responses_and_csp_compatibility(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "headers.sqlite3"
            db_store.publish_report(
                db_path,
                {
                    "summary": {"total_equity": 100.0},
                    "active_positions": [],
                    "trade_history": [],
                    "equity_curve": [],
                    "daily_pnl": [],
                },
            )
            db_store.publish_candidates(db_path, [{"symbol": "BTCUSDT"}], updated_at="2026-11-26T10:00:00+08:00")
            handler_cls = api.create_handler(db_path)

            def assert_sec_headers(hdrs: dict[str, str]) -> None:
                self.assertEqual(hdrs.get("x-content-type-options"), "nosniff")
                self.assertEqual(hdrs.get("x-frame-options"), "SAMEORIGIN")
                self.assertEqual(hdrs.get("referrer-policy"), "strict-origin-when-cross-origin")
                csp = hdrs.get("content-security-policy", "")
                self.assertIn("default-src 'self'", csp)
                self.assertIn("script-src 'self'", csp)
                self.assertNotIn("script-src 'self' 'unsafe-inline'", csp)
                self.assertNotIn("'unsafe-eval'", csp)
                self.assertIn("style-src 'self' 'unsafe-inline'", csp)
                self.assertIn("img-src 'self' data:", csp)
                self.assertIn("connect-src 'self'", csp)
                self.assertIn("frame-src https://s.tradingview.com", csp)
                self.assertIn("object-src 'none'", csp)
                self.assertIn("frame-ancestors 'self'", csp)

            for route in (
                "/",
                "/index.html",
                "/account.html",
                "/playbook.html",
                "/static/common.js",
                "/static/styles.css",
                "/api/config",
                "/api/account",
                "/api/candidates",
                "/api/candidate-version",
                "/api/unknown",
            ):
                _, hdrs, _, _ = self._invoke(handler_cls, "GET", route)
                assert_sec_headers(hdrs)

            # 304 Not Modified also carries security headers
            _, hdrs_acc, _, _ = self._invoke(handler_cls, "GET", "/api/account")
            c304, hdrs_304, b304, _ = self._invoke(
                handler_cls, "GET", "/api/account", headers={"If-None-Match": hdrs_acc["etag"]}
            )
            self.assertEqual(c304, 304)
            self.assertEqual(b304, b"")
            assert_sec_headers(hdrs_304)

            # Live HTTPServer roundtrip including 404 send_error path
            with api.DashboardHTTPServer(("127.0.0.1", 0), handler_cls) as server:
                port = server.server_address[1]
                t = threading.Thread(target=server.serve_forever, daemon=True)
                t.start()
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                try:
                    for path in ("/", "/api/config", "/does-not-exist"):
                        conn.request("GET", path)
                        resp = conn.getresponse()
                        resp.read()
                        live_hdrs = {k.lower(): v for k, v in resp.getheaders()}
                        assert_sec_headers(live_hdrs)
                finally:
                    conn.close()
                    server.shutdown()

            # Verify HTML pages have no inline scripts or inline event handlers incompatible with script-src 'self'
            for html_name in ("index.html", "account.html", "playbook.html"):
                html_text = (ROOT_DIR / "web" / html_name).read_text(encoding="utf-8")
                self.assertIsNone(
                    re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html_text, re.IGNORECASE),
                    f"Inline <script> found in {html_name}",
                )
                self.assertIsNone(
                    re.search(r"\son[a-z]+\s*=", html_text, re.IGNORECASE),
                    f"Inline event handler found in {html_name}",
                )

    def test_nginx_locations_conf_syntax_and_security_invariants(self):
        conf = (ROOT_DIR / "deploy" / "nginx" / "ai-coin-selector.locations.conf").read_text(encoding="utf-8")
        self.assertIn("location /ai-coin-selector/api/binance/", conf)
        self.assertIn("location /ai-coin-selector/", conf)
        self.assertIn("proxy_set_header X-Real-IP $remote_addr;", conf)
        self.assertIn("proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;", conf)
        # Ensure no active (uncommented) limit_req_zone / proxy_hide_header / add_header overrides
        active_lines = [
            line.strip()
            for line in conf.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        for line in active_lines:
            self.assertFalse(
                line.startswith("limit_req_zone"),
                f"Active limit_req_zone is illegal inside server/location context: {line}",
            )
            self.assertFalse(
                line.startswith("proxy_hide_header"),
                f"Unexpected proxy_hide_header could strip security headers: {line}",
            )
            self.assertFalse(
                line.startswith("add_header"),
                f"Unexpected add_header could override parent headers: {line}",
            )


if __name__ == "__main__":
    unittest.main()
