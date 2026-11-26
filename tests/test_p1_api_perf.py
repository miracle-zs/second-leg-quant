#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1 backend API performance, caching, Nginx static delivery, and schema contract tests."""

from __future__ import annotations

import hashlib
import http.client
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
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
import quant_metrics as qm  # noqa: E402


class TestAPIPerfAndContractsP1(unittest.TestCase):
    def _invoke(
        self,
        handler_cls,
        method: str,
        raw_path: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        handler = handler_cls.__new__(handler_cls)
        handler.path = raw_path
        handler.headers = headers or {}
        handler.wfile = io.BytesIO()
        status_box: dict[str, int] = {}
        resp_headers: dict[str, str] = {}

        handler.send_response = lambda code, message=None: status_box.__setitem__("code", code)
        handler.send_header = lambda k, v: resp_headers.__setitem__(k.lower(), v)
        handler.end_headers = lambda: None
        handler.send_error = lambda code, message=None, explain=None: status_box.__setitem__("code", code)
        handler.log_error = lambda *args, **kwargs: None

        if method == "HEAD":
            handler.do_HEAD()
        else:
            handler.do_GET()
        return status_box.get("code", 0), resp_headers, handler.wfile.getvalue()

    def test_proxy_cache_hit_checks_scalar_version_without_loading_full_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "cand_ver.sqlite3"
            db_store.publish_candidates(
                db_path,
                [{"symbol": "YBUSDT"}],
                updated_at="2026-11-26T10:00:00+08:00",
            )
            handler_cls = api.create_handler(db_path)

            raw_tickers = json.dumps(
                [
                    {"symbol": "BTCUSDT", "lastPrice": "95000"},
                    {"symbol": "YBUSDT", "lastPrice": "0.5"},
                ]
            ).encode("utf-8")
            raw_premiums = json.dumps(
                [
                    {"symbol": "BTCUSDT", "lastFundingRate": "0.0001"},
                    {"symbol": "YBUSDT", "lastFundingRate": "-0.0002"},
                ]
            ).encode("utf-8")

            def fake_upstream(url: str, **_kwargs) -> bytes:
                if "ticker/24hr" in url:
                    return raw_tickers
                if "premiumIndex" in url:
                    return raw_premiums
                raise AssertionError(f"Unexpected URL: {url}")

            with (
                patch.object(http_client, "request_bytes", side_effect=fake_upstream),
                patch.object(
                    db_store, "load_candidate_version", wraps=db_store.load_candidate_version
                ) as spy_ver,
                patch.object(
                    db_store, "load_candidates", wraps=db_store.load_candidates
                ) as spy_full,
            ):
                # 1. Cold miss on ticker24hr loads candidate symbols once
                c1, _, b1 = self._invoke(handler_cls, "GET", "/api/binance/ticker24hr")
                self.assertEqual(c1, 200)
                self.assertEqual(json.loads(b1), [{"symbol": "YBUSDT", "lastPrice": "0.5"}])
                self.assertEqual(spy_ver.call_count, 1)
                self.assertEqual(spy_full.call_count, 1)

                # 2. Proxy cache hit with unchanged version checks scalar version only
                c2, _, b2 = self._invoke(handler_cls, "GET", "/api/binance/ticker24hr")
                self.assertEqual(c2, 200)
                self.assertEqual(b2, b1)
                self.assertEqual(spy_ver.call_count, 2)
                self.assertEqual(spy_full.call_count, 1)

                # 3. Another endpoint (premiumIndex) with same candidate version reuses cached symbols
                c3, _, b3 = self._invoke(handler_cls, "GET", "/api/binance/premiumIndex")
                self.assertEqual(c3, 200)
                self.assertEqual(json.loads(b3), [{"symbol": "YBUSDT", "lastFundingRate": "-0.0002"}])
                self.assertEqual(spy_ver.call_count, 3)
                self.assertEqual(spy_full.call_count, 1)

                # 4. Candidate version bump triggers one full load and re-filters cached payload
                db_store.publish_candidates(
                    db_path,
                    [{"symbol": "BTCUSDT"}, {"symbol": "YBUSDT"}],
                    updated_at="2026-11-26T10:05:00+08:00",
                )
                c4, _, b4 = self._invoke(handler_cls, "GET", "/api/binance/ticker24hr")
                self.assertEqual(c4, 200)
                self.assertEqual(len(json.loads(b4)), 2)
                self.assertEqual(spy_ver.call_count, 4)
                self.assertEqual(spy_full.call_count, 2)

                # 5. Subsequent hit on updated version skips full load again
                c5, _, b5 = self._invoke(handler_cls, "GET", "/api/binance/ticker24hr")
                self.assertEqual(c5, 200)
                self.assertEqual(b5, b4)
                self.assertEqual(spy_ver.call_count, 5)
                self.assertEqual(spy_full.call_count, 2)

    def test_singleflight_wait_timeout_returns_504_gateway_timeout(self):
        self.assertGreater(api._BINANCE_SINGLEFLIGHT_WAIT_TIMEOUT_SEC, 0)
        self.assertLess(api._BINANCE_SINGLEFLIGHT_WAIT_TIMEOUT_SEC, 30.0)

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sf_timeout.sqlite3"
            handler_cls = api.create_handler(db_path, singleflight_timeout_sec=0.05)

            entered = threading.Event()
            release = threading.Event()
            upstream_calls = 0

            def slow_upstream(_url: str, **_kwargs) -> bytes:
                nonlocal upstream_calls
                upstream_calls += 1
                entered.set()
                release.wait(timeout=5.0)
                return b'[{"symbol":"BTCUSDT"}]'

            leader_box: list[tuple[int, dict[str, str], bytes]] = []
            follower_get_box: list[tuple[int, dict[str, str], bytes]] = []
            follower_head_box: list[tuple[int, dict[str, str], bytes]] = []

            with patch.object(http_client, "request_bytes", side_effect=slow_upstream):
                t_leader = threading.Thread(
                    target=lambda: leader_box.append(
                        self._invoke(handler_cls, "GET", "/api/binance/ticker24hr")
                    )
                )
                t_leader.start()
                self.assertTrue(entered.wait(timeout=2.0))

                t_get = threading.Thread(
                    target=lambda: follower_get_box.append(
                        self._invoke(handler_cls, "GET", "/api/binance/ticker24hr")
                    )
                )
                t_head = threading.Thread(
                    target=lambda: follower_head_box.append(
                        self._invoke(handler_cls, "HEAD", "/api/binance/ticker24hr")
                    )
                )
                t_get.start()
                t_head.start()
                t_get.join(timeout=2.0)
                t_head.join(timeout=2.0)

                self.assertFalse(t_get.is_alive(), "Follower GET must not hang past timeout")
                self.assertFalse(t_head.is_alive(), "Follower HEAD must not hang past timeout")

                code_504, hdr_504, body_504 = follower_get_box[0]
                self.assertEqual(code_504, 504)
                self.assertEqual(hdr_504.get("content-type"), "application/json; charset=utf-8")
                self.assertEqual(hdr_504.get("cache-control"), "no-store")
                self.assertEqual(hdr_504.get("x-content-type-options"), "nosniff")
                self.assertEqual(
                    json.loads(body_504),
                    {"error": "Gateway timeout waiting for upstream response"},
                )

                head_504, hdr_head_504, body_head_504 = follower_head_box[0]
                self.assertEqual(head_504, 504)
                self.assertEqual(hdr_head_504.get("content-type"), "application/json; charset=utf-8")
                self.assertEqual(body_head_504, b"")

                release.set()
                t_leader.join(timeout=2.0)

            self.assertEqual(upstream_calls, 1)
            self.assertEqual(leader_box[0][0], 200)

            # Inflight slot is cleaned up and cached result serves subsequent callers cleanly
            with patch.object(http_client, "request_bytes", side_effect=AssertionError("Should hit cache")):
                code_after, _, body_after = self._invoke(handler_cls, "GET", "/api/binance/ticker24hr")
            self.assertEqual(code_after, 200)
            self.assertEqual(body_after, b'[{"symbol":"BTCUSDT"}]')

    def test_static_cache_control_and_html_asset_version_injection(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "static_cache.sqlite3"
            handler_cls = api.create_handler(db_path)

            # 1. Unversioned / empty-version static assets keep no-cache; ?v=<hash> gets immutable long cache
            c_unver, h_unver, _ = self._invoke(handler_cls, "GET", "/static/common.js")
            self.assertEqual(c_unver, 200)
            self.assertEqual(h_unver.get("cache-control"), "no-cache")

            c_empty_v, h_empty_v, _ = self._invoke(handler_cls, "GET", "/static/common.js?v=")
            self.assertEqual(c_empty_v, 200)
            self.assertEqual(h_empty_v.get("cache-control"), "no-cache")

            c_ver, h_ver, _ = self._invoke(handler_cls, "GET", "/static/common.js?v=test123")
            self.assertEqual(c_ver, 200)
            self.assertEqual(h_ver.get("cache-control"), "public, max-age=31536000, immutable")
            self.assertEqual(h_ver.get("x-content-type-options"), "nosniff")

            c_ver_304, h_ver_304, b_ver_304 = self._invoke(
                handler_cls,
                "GET",
                "/static/common.js?v=test123",
                headers={"If-None-Match": h_ver["etag"]},
            )
            self.assertEqual(c_ver_304, 304)
            self.assertEqual(b_ver_304, b"")
            self.assertEqual(h_ver_304.get("cache-control"), "public, max-age=31536000, immutable")
            self.assertEqual(h_ver_304.get("x-content-type-options"), "nosniff")

            # 2. Live HTML routes inject ?v=<content_hash> for all static/... assets while keeping HTML no-cache
            for route, expected_assets in (
                ("/", ("static/tailwind.css", "static/styles.css", "static/common.js", "static/candidates.js")),
                ("/index.html", ("static/kline-modal.js",)),
                ("/account.html", ("static/vendor/chart.js", "static/account.js")),
                ("/playbook.html", ("static/styles.css", "static/common.js")),
            ):
                code_html, hdrs_html, body_html = self._invoke(handler_cls, "GET", route)
                self.assertEqual(code_html, 200)
                self.assertEqual(hdrs_html.get("cache-control"), "no-cache")
                text_html = body_html.decode("utf-8")
                for asset_rel in expected_assets:
                    disk_bytes = (ROOT_DIR / "web" / asset_rel[len("static/") :]).read_bytes()
                    expected_hash = hashlib.sha256(disk_bytes).hexdigest()[:10]
                    expected_url = f"{asset_rel}?v={expected_hash}"
                    self.assertIn(expected_url, text_html, f"{route} missing injected {expected_url}")

                    # Fetching the injected URL yields immutable caching
                    c_asset, h_asset, _ = self._invoke(handler_cls, "GET", f"/{expected_url}")
                    self.assertEqual(c_asset, 200)
                    self.assertEqual(h_asset.get("cache-control"), "public, max-age=31536000, immutable")

            # 3. Pre-existing ?v= is preserved and changing a static file updates the injected HTML hash
            web_dir = Path(tmp) / "web"
            web_dir.mkdir()
            app_js = web_dir / "app.js"
            pinned_css = web_dir / "pinned.css"
            app_js.write_text("console.log('v1');", encoding="utf-8")
            pinned_css.write_text("body { color: #fff; }", encoding="utf-8")
            (web_dir / "index.html").write_text(
                '<html><head>'
                '<link rel="stylesheet" href="static/pinned.css?v=manual99">'
                '<script src="static/app.js" defer></script>'
                '<script src="static/missing.js" defer></script>'
                "</head></html>",
                encoding="utf-8",
            )
            with patch.object(api, "WEB_DIR", web_dir):
                tmp_handler = api.create_handler(db_path)
                _, h1, html1 = self._invoke(tmp_handler, "GET", "/")
                text1 = html1.decode("utf-8")
                hash1 = hashlib.sha256(b"console.log('v1');").hexdigest()[:10]
                self.assertIn('href="static/pinned.css?v=manual99"', text1)
                self.assertIn(f'src="static/app.js?v={hash1}"', text1)
                self.assertIn('src="static/missing.js"', text1)

                time.sleep(0.01)
                app_js.write_text("console.log('v2-updated');", encoding="utf-8")
                _, h2, html2 = self._invoke(tmp_handler, "GET", "/")
                text2 = html2.decode("utf-8")
                hash2 = hashlib.sha256(b"console.log('v2-updated');").hexdigest()[:10]
                self.assertNotEqual(hash1, hash2)
                self.assertIn(f'src="static/app.js?v={hash2}"', text2)
                self.assertNotEqual(h1["etag"], h2["etag"])

    def test_nginx_static_location_config_and_security_invariants(self):
        conf = (ROOT_DIR / "deploy" / "nginx" / "ai-coin-selector.locations.conf").read_text(encoding="utf-8")
        service = (ROOT_DIR / "deploy" / "systemd" / "ai-coin-selector-account-api.service").read_text(
            encoding="utf-8"
        )
        self.assertIn("WorkingDirectory=/opt/ai-coin-selector", service)
        self.assertIn("location /ai-coin-selector/static/", conf)
        self.assertIn("alias /opt/ai-coin-selector/web/;", conf)
        self.assertIn("gzip on;", conf)
        self.assertIn("gzip_vary on;", conf)
        self.assertIn("expires -1;", conf)
        self.assertIn('if ($arg_v != "")', conf)
        self.assertIn("expires 365d;", conf)

        active_lines = [
            line.strip()
            for line in conf.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        for line in active_lines:
            self.assertFalse(line.startswith("add_header"), f"Unexpected add_header override: {line}")
            self.assertFalse(
                line.startswith("proxy_hide_header"), f"Unexpected proxy_hide_header override: {line}"
            )

    def test_api_config_exposes_high_turnover_threshold_and_chinese_tv_map(self):
        common_js = (ROOT_DIR / "web" / "common.js").read_text(encoding="utf-8")
        scanner_py = (ROOT_DIR / "scripts" / "scan_second_leg_pool.py").read_text(encoding="utf-8")
        self.assertIn("turnover_ratio >= 1.5", scanner_py)

        # Parse CHINESE_TV_MAP from web/common.js to guarantee 1:1 parity
        map_match = re.search(r"const\s+CHINESE_TV_MAP\s*=\s*Object\.freeze\(\{([\s\S]*?)\}\);", common_js)
        self.assertIsNotNone(map_match)
        expected_map = dict(re.findall(r"'([^']+)'\s*:\s*'([^']+)'", map_match.group(1)))
        self.assertGreaterEqual(len(expected_map), 5)

        with tempfile.TemporaryDirectory() as tmp:
            handler_cls = api.create_handler(Path(tmp) / "cfg.sqlite3")
            code, hdrs, body = self._invoke(handler_cls, "GET", "/api/config")
            self.assertEqual(code, 200)
            self.assertEqual(hdrs.get("cache-control"), "no-store")
            payload = json.loads(body.decode("utf-8"))
            self.assertIn("tradingview_layout_id", payload)
            self.assertEqual(payload["high_turnover_threshold"], 1.5)
            self.assertEqual(payload["chinese_tv_map"], expected_map)

    def test_current_account_analytics_schema_contract_compatible_with_account_js(self):
        trades = [
            {
                "trade_id": "TRD-001-BTCUSDT",
                "symbol": "BTCUSDT",
                "side": "LONG",
                "entry_price": 90000.0,
                "exit_price": 92000.0,
                "amount": 0.01,
                "entry_notional": 900.0,
                "exit_notional": 920.0,
                "realized_pnl": 20.0,
                "fee": 0.9,
                "net_pnl": 19.1,
                "pnl_pct": 2.12,
                "roe_pct": 10.6,
                "leverage": 5,
                "entry_time": "2026-11-25 10:00:00",
                "closed_at": "2026-11-25 12:00:00",
                "entry_ts": 1795572000000,
                "closed_ts": 1795579200000,
                "duration_seconds": 7200,
                "duration_str": "2h 0m",
                "outcome": "WIN",
                "status": "BINANCE_REALTIME_TRADE",
                "close_reason": "移动保本止盈",
                "close_reason_code": "TRAILING_STOP",
                "trendline_id": None,
                "trendline_label": "--",
                "source": "BINANCE_REALTIME",
            },
            {
                "trade_id": "TRD-002-ETHUSDT",
                "symbol": "ETHUSDT",
                "side": "LONG",
                "entry_price": 3200.0,
                "exit_price": 3150.0,
                "amount": 0.2,
                "entry_notional": 640.0,
                "exit_notional": 630.0,
                "realized_pnl": -10.0,
                "fee": 0.6,
                "net_pnl": -10.6,
                "pnl_pct": -1.66,
                "roe_pct": -8.3,
                "leverage": 5,
                "entry_time": "2026-11-25 14:00:00",
                "closed_at": "2026-11-25 15:00:00",
                "entry_ts": 1795586400000,
                "closed_ts": 1795590000000,
                "duration_seconds": 3600,
                "duration_str": "1h 0m",
                "outcome": "LOSS",
                "status": "BINANCE_REALTIME_TRADE",
                "close_reason": "15m 破线止损",
                "close_reason_code": "TRENDLINE_BREAK",
                "trendline_id": None,
                "trendline_label": "--",
                "source": "BINANCE_REALTIME",
            },
        ]
        summary, equity_curve, daily_pnl = qm.calculate_quant_risk_metrics(
            trades,
            [],
            None,
            initial_capital=1000.0,
            total_funding_fee=-0.25,
        )
        pnl_distribution = qm.build_pnl_distribution(trades)
        report = {
            "version": 1,
            "generated_at": "2026-11-25 16:00:00",
            "data_source": "BINANCE_API",
            "positions_source": "BINANCE_API",
            "api_status": {
                "configured": True,
                "connected": True,
                "positions_available": True,
                "income_available": True,
                "status_code": 200,
                "message": "Connected",
                "mode": "BINANCE_REALTIME",
                "last_sync": "2026-11-25 16:00:00",
            },
            "summary": summary,
            "active_positions": [],
            "trade_history": trades,
            "equity_curve": equity_curve,
            "daily_pnl": daily_pnl,
            "pnl_distribution": pnl_distribution,
        }

        for required_key in (
            "win_count",
            "loss_count",
            "breakeven_count",
            "avg_win",
            "avg_loss",
            "total_funding_fee",
            "net_realized_pnl",
            "open_trades",
            "closed_trades",
        ):
            self.assertIn(required_key, summary)
        for legacy_key in (
            "winning_trades",
            "losing_trades",
            "breakeven_trades",
            "avg_winning_trade",
            "avg_losing_trade",
        ):
            self.assertNotIn(legacy_key, summary)

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "schema_ok.sqlite3"
            db_store.publish_report(db_path, report)
            handler_cls = api.create_handler(db_path)
            code, _, raw_body = self._invoke(handler_cls, "GET", "/api/account")
            self.assertEqual(code, 200)
            served_json = raw_body.decode("utf-8")

        node_bin = shutil.which("node")
        self.assertIsNotNone(node_bin, "node is required to validate account.js schema compatibility")
        js_check = textwrap.dedent(
            """
            const assert = require('assert');
            const fs = require('fs');
            const path = require('path');
            const vm = require('vm');

            const root = process.cwd();
            const commonCode = fs.readFileSync(path.join(root, 'web/common.js'), 'utf8');
            const accountCode = fs.readFileSync(path.join(root, 'web/account.js'), 'utf8');
            const accountJson = process.env.ACCOUNT_PAYLOAD_JSON;

            const els = {};
            const makeEl = (id) => ({
              id,
              textContent: '',
              innerHTML: '',
              value: '',
              hidden: false,
              disabled: false,
              className: '',
              dataset: {},
              style: {},
              classList: { add() {}, remove() {}, toggle() {} },
              setAttribute() {},
              getAttribute() { return null; },
              addEventListener() {},
              querySelector() { return null; },
              querySelectorAll() { return []; },
            });
            const doc = {
              hidden: false,
              readyState: 'complete',
              getElementById: (id) => (els[id] ||= makeEl(id)),
              querySelector: () => makeEl('q'),
              querySelectorAll: () => [],
              addEventListener() {},
            };
            const win = {
              document: doc,
              fetch: async (url) => {
                if (url === 'api/config') {
                  return { ok: true, status: 200, headers: { get: () => null }, text: async () => '{"tradingview_layout_id":"ig0LuDL8"}' };
                }
                if (url === 'api/account') {
                  return { ok: true, status: 200, headers: { get: () => '"e1"' }, text: async () => accountJson };
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
              console: { warn() {}, error(e) { throw e; } },
            };
            win.window = win;

            (async () => {
              vm.runInNewContext(commonCode, win);
              vm.runInNewContext(accountCode, win);
              await new Promise((r) => setTimeout(r, 30));
              assert.strictEqual(els['kpi-wl-record'].textContent, '1W / 1L / 0BE');
              assert.strictEqual(els['win-loss-summary-badge'].textContent, '2 笔');
              assert.strictEqual(els['account-report'].hidden, false);
            })().catch((err) => {
              console.error(err);
              process.exit(1);
            });
            """
        )
        proc = subprocess.run(
            [node_bin, "-e", js_check],
            cwd=str(ROOT_DIR),
            env={"PATH": "", "ACCOUNT_PAYLOAD_JSON": served_json},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, f"account.js failed on current schema:\n{proc.stderr}")


if __name__ == "__main__":
    unittest.main()
