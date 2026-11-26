#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for account page & data alignment P2 fixes (#22, #23, #25, #26, #27, #28, #29)."""

import io
from contextlib import redirect_stdout
from pathlib import Path
import re
import shutil
import subprocess
import sys
import textwrap
import unittest


ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import account_analytics_engine as aae  # noqa: E402
import trade_enrichment as te  # noqa: E402


class TestAccountP2BackendAlignment(unittest.TestCase):
    """#26: Backend active position trendline_label & trendline_id == 0 alignment."""

    def test_enrich_active_position_outputs_stable_trendline_label_including_zero(self):
        pos_with_tl_dict = te.enrich_active_position(
            {
                "symbol": "SOLUSDT",
                "entry_price": 150.0,
                "mark_price": 160.0,
                "amount": 5.0,
                "trendline": {"id": 0, "p1": 155.0, "p2": 148.0},
            },
            0,
        )
        self.assertEqual(pos_with_tl_dict["trendline_id"], 0)
        self.assertEqual(pos_with_tl_dict["trendline_label"], "TL#0 (155.0000 -> 148.0000)")

        pos_with_id_zero = te.enrich_active_position(
            {
                "symbol": "BTCUSDT",
                "entry_price": 90000.0,
                "mark_price": 91000.0,
                "amount": 0.1,
                "trendline_id": 0,
            },
            1,
        )
        self.assertEqual(pos_with_id_zero["trendline_id"], 0)
        self.assertEqual(pos_with_id_zero["trendline_label"], "TL#0")

        pos_without_tl = te.enrich_active_position(
            {
                "symbol": "ETHUSDT",
                "entry_price": 3000.0,
                "mark_price": 3050.0,
                "amount": 1.0,
            },
            2,
        )
        self.assertIsNone(pos_without_tl["trendline_id"])
        self.assertEqual(pos_without_tl["trendline_label"], "--")

    def test_enrich_trade_record_and_merge_support_trendline_id_zero(self):
        enriched_trade = te.enrich_trade_record(
            {
                "symbol": "BTCUSDT",
                "entry_price": 90000.0,
                "exit_price": 91000.0,
                "amount": 0.2,
                "entry_time": "2026-11-25 10:00:00",
                "closed_at": "2026-11-25 11:00:00",
                "trendline": {"id": 0, "p1": 90500.0, "p2": 89900.0},
            },
            0,
        )
        self.assertEqual(enriched_trade["trendline_id"], 0)
        self.assertEqual(enriched_trade["trendline_label"], "TL#0 (90500.0000 -> 89900.0000)")

        merged = te.merge_active_positions(
            [
                {
                    "symbol": "ADAUSDT",
                    "side": "LONG",
                    "amount": 100.0,
                    "signed_amount": 100.0,
                    "entry_price": 0.5,
                    "mark_price": 0.55,
                    "position_side": "BOTH",
                }
            ],
            [
                {
                    "symbol": "ADAUSDT",
                    "side": "LONG",
                    "amount": 100.0,
                    "trendline_id": 0,
                    "trendline_label": "TL#0 (0.5200 -> 0.4900)",
                }
            ],
            positions_available=True,
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["trendline_id"], 0)
        self.assertEqual(merged[0]["trendline_label"], "TL#0 (0.5200 -> 0.4900)")

    def test_print_cli_summary_handles_trendline_id_zero(self):
        report = {
            "generated_at": "2026-11-26 12:00:00",
            "api_status": {"connected": True},
            "summary": {
                "total_equity": 1000.0,
                "initial_capital": 1000.0,
                "total_return_pct": 0.0,
                "available_margin": 900.0,
                "cumulative_realized_pnl": 0.0,
                "net_realized_pnl": 0.0,
                "total_fee": 0.0,
                "win_rate_pct": 0.0,
                "profit_factor": None,
                "win_count": 0,
                "loss_count": 0,
                "breakeven_count": 0,
                "max_drawdown_pct": 0.0,
                "max_drawdown_usd": 0.0,
                "sharpe_ratio": None,
                "sortino_ratio": None,
                "payoff_ratio": None,
                "avg_hold_duration_str": "--",
                "max_consecutive_wins": 0,
            },
            "active_positions": [
                {
                    "symbol": "BTCUSDT",
                    "leverage": 5,
                    "amount": 0.1,
                    "entry_price": 90000.0,
                    "mark_price": 91000.0,
                    "unrealized_pnl": 100.0,
                    "roe_pct": 5.56,
                    "holding_str": "10m",
                    "trendline_id": 0,
                    "trendline_label": "TL#0",
                }
            ],
            "trade_history": [
                {
                    "closed_at": "2026-11-26 11:00:00",
                    "symbol": "ETHUSDT",
                    "entry_price": 3000.0,
                    "exit_price": 3100.0,
                    "net_pnl": 50.0,
                    "roe_pct": 10.0,
                    "close_reason": "手动平仓",
                    "trendline_id": 0,
                    "trendline_label": "TL#0",
                }
            ],
        }
        buf = io.StringIO()
        with redirect_stdout(buf):
            aae.print_cli_summary(report)
        out = buf.getvalue()
        self.assertIn("[TL#0]", out)


class TestAccountP2FrontendStatic(unittest.TestCase):
    """Static checks for web/account.html and web/account.js P2 contracts."""

    def test_account_html_p2_landmarks_preload_icon_and_clean_markup(self):
        html = (ROOT_DIR / "web/account.html").read_text(encoding="utf-8")

        # #28: Unified page title with nav label
        self.assertIn("<title>突破追多与移动保本系统 — 账户数据分析</title>", html)

        # #27: Inline SVG favicon & api/config preload
        self.assertRegex(html, r'<link[^>]+rel="icon"[^>]+href="data:image/svg\+xml,')
        self.assertIn(
            '<link rel="preload" as="fetch" crossorigin="anonymous" href="api/config">',
            html,
        )

        # #23: #account-report accessible name & non-duplicate table wrap landmarks
        report_tag = re.search(r'<section\b[^>]*id="account-report"[^>]*>', html)
        self.assertIsNotNone(report_tag)
        self.assertRegex(report_tag.group(0), r'aria-label(?:ledby)?="[^"]+"')

        pos_wrap = re.search(r'<div\b[^>]*id="active-positions-table-wrap"[^>]*>', html)
        hist_wrap = re.search(r'<div\b[^>]*id="history-table-wrap"[^>]*>', html)
        self.assertIsNotNone(pos_wrap)
        self.assertIsNotNone(hist_wrap)
        self.assertNotIn('aria-labelledby="active-positions-heading"', pos_wrap.group(0))
        self.assertNotIn('aria-labelledby="history-ledger-heading"', hist_wrap.group(0))
        self.assertIn('aria-label="', pos_wrap.group(0))
        self.assertIn('aria-label="', hist_wrap.group(0))

        # #29: Deduplicated short warning & narrow-screen status bar without divide-x
        self.assertEqual(html.count("仅多头仓位纳入 15m 破线止损与移动保本风控"), 1)
        self.assertIn('id="short-position-warning"', html)
        status_strip = html.split('id="api-status-badge"')[0].splitlines()[-1]
        self.assertNotIn("divide-x", status_strip)

        # #28: W / L / BE header aligned with 1W / 1L / 0BE cell
        self.assertNotIn("<span>胜/负/平</span>", html)
        self.assertIn("W / L / BE", html)

        # #29: No drifted colors in account.html
        self.assertNotIn("#6b7f9a", html)

    def test_account_js_p2_clean_tokens_and_shared_helpers(self):
        js = (ROOT_DIR / "web/account.js").read_text(encoding="utf-8")

        # #22 & #29: Contrast fix (#4b5c75 -> #647894 in charts) and drifted colors converged
        self.assertNotIn("#8b9bb0", js)
        self.assertNotIn("#c9d5e3", js)
        self.assertNotIn("#5c6f89", js)
        self.assertNotIn("#3d4e66", js)
        self.assertIn("#647894", js)

        # #29: Dead class .trade-row removed
        self.assertNotIn("trade-row", js)

        # #28: dec4Threshold: 1 override removed, 空仓在岸 replaced with 当前空仓
        self.assertNotIn("dec4Threshold: 1", js)
        self.assertNotIn("空仓在岸", js)
        self.assertIn("当前空仓", js)

        # #25: Consumes Common.EXT_ARROW_SVG / getExtArrowSvg, Common.debounce, Common.bindSortButtons
        self.assertIn("Common.EXT_ARROW_SVG", js)
        self.assertIn("Common.debounce", js)
        self.assertIn("Common.bindSortButtons", js)

    def test_no_uncompiled_tailwind_classes_added_in_account_html(self):
        html = (ROOT_DIR / "web/account.html").read_text(encoding="utf-8")
        tailwind_css = (ROOT_DIR / "web/tailwind.css").read_text(encoding="utf-8")
        styles_css = (ROOT_DIR / "web/styles.css").read_text(encoding="utf-8")
        combined_css = tailwind_css + "\n" + styles_css

        # Verify custom classes used in modified account.html snippets exist in CSS
        for cls in (
            "whitespace-nowrap",
            "sm:border-l",
            "sm:border-[#222b38]",
            "border-dashed",
            "border-t",
            "font-mono",
        ):
            css_escaped = (
                cls.replace(":", r"\:")
                .replace("[", r"\[")
                .replace("]", r"\]")
                .replace("#", r"\#")
            )
            self.assertIn(f".{css_escaped}", combined_css, f"Missing CSS class: {cls}")


class TestAccountP2RuntimeVM(unittest.TestCase):
    """Node VM runtime tests for web/account.js P2 behavior (#22, #25, #26, #28, #29)."""

    def test_account_js_p2_runtime_behavior_with_fallback_and_shared_common(self):
        node_bin = shutil.which("node")
        self.assertIsNotNone(node_bin, "node is required for account.js runtime tests")

        script = textwrap.dedent(
            """
            const assert = require('assert');
            const fs = require('fs');
            const path = require('path');
            const vm = require('vm');

            const root = process.cwd();
            const commonCode = fs.readFileSync(path.join(root, 'web/common.js'), 'utf8');
            const acctCode = fs.readFileSync(path.join(root, 'web/account.js'), 'utf8');

            function makeEl(id, extra = {}) {
              const attrs = {};
              const listeners = {};
              const classSet = new Set();
              return {
                id,
                textContent: '',
                innerHTML: '',
                title: '',
                className: '',
                value: '',
                hidden: false,
                disabled: false,
                dataset: {},
                style: {},
                classList: {
                  add(...cls) { cls.forEach((c) => classSet.add(c)); },
                  remove(...cls) { cls.forEach((c) => classSet.delete(c)); },
                  toggle(c, force) {
                    if (force === undefined) {
                      if (classSet.has(c)) classSet.delete(c);
                      else classSet.add(c);
                    } else if (force) classSet.add(c);
                    else classSet.delete(c);
                  },
                  contains(c) { return classSet.has(c); },
                },
                setAttribute(k, v) { attrs[k] = String(v); },
                getAttribute(k) { return Object.prototype.hasOwnProperty.call(attrs, k) ? attrs[k] : null; },
                removeAttribute(k) { delete attrs[k]; },
                hasAttribute(k) { return Object.prototype.hasOwnProperty.call(attrs, k); },
                addEventListener(ev, fn) { (listeners[ev] ||= []).push(fn); },
                dispatch(ev, eventObj = {}) { (listeners[ev] || []).forEach((fn) => fn(eventObj)); },
                querySelector() { return null; },
                querySelectorAll() { return []; },
                focus() {},
                ...extra,
              };
            }

            (async () => {
              // --------------------------------------------------------------
              // Case 1: With current common.js (fallback path for debounce/bindSortButtons/EXT_ARROW_SVG)
              // --------------------------------------------------------------
              const els = {};
              const sortableHeaders = ['closed_ts', 'symbol', 'net_pnl', 'pnl_pct'].map((col) => {
                const th = makeEl(`th-${col}`);
                th.dataset = { sort: col };
                const btn = makeEl(`btn-sort-${col}`);
                th.querySelector = (sel) => (sel === 'button[command="--sort"]' ? btn : null);
                th._btn = btn;
                if (col === 'closed_ts') th.setAttribute('aria-sort', 'descending');
                return th;
              });
              const histTable = makeEl('trade-history-table', {
                querySelectorAll(sel) {
                  return sel === 'th.sortable' ? sortableHeaders : [];
                },
              });
              els['trade-history-table'] = histTable;
              const getEl = (id) => (els[id] ||= makeEl(id));

              const chartsById = {};
              function FakeChart(canvas, config) {
                this.canvas = canvas;
                this.data = config.data;
                this.options = config.options;
                this.update = () => {};
                this.destroy = () => {};
                chartsById[canvas.id] = this;
              }
              FakeChart.defaults = { color: '', font: {}, borderColor: '' };

              const payload = {
                api_status: { mode: 'NOT_CONFIGURED' },
                generated_at: '2026-11-26T04:00:00Z',
                summary: {
                  initial_capital: 1000,
                  total_equity: 1000,
                  available_margin: 1000,
                  position_margin: 0,
                  net_realized_pnl: 0,
                  total_fee: 0,
                  total_funding_fee: 0,
                  total_unrealized_pnl: 0,
                  open_trades: 2,
                  closed_trades: 2,
                  win_count: 1,
                  loss_count: 0,
                  breakeven_count: 1,
                  win_rate_pct: 50,
                },
                active_positions: [
                  {
                    symbol: 'BTCUSDT',
                    side: 'LONG',
                    risk_monitored: true,
                    entry_price: 0.05,
                    mark_price: 0.06,
                    amount: 0.5,
                    notional: 50,
                    leverage: 5,
                    unrealized_pnl: 5,
                    roe_pct: 50,
                    trendline_id: 0,
                  },
                  {
                    symbol: 'SOLUSDT',
                    side: 'LONG',
                    risk_monitored: true,
                    entry_price: 150,
                    mark_price: 160,
                    amount: 10,
                    notional: 1600,
                    leverage: 5,
                    unrealized_pnl: 100,
                    roe_pct: 33.3,
                    trendline_id: 7,
                    trendline_label: 'TL#7 (155.0000 -> 148.0000)',
                  },
                ],
                trade_history: [
                  {
                    symbol: 'ETHUSDT',
                    side: 'LONG',
                    closed_ts: 1000,
                    closed_at: '2026-11-26 10:00:00',
                    entry_price: 0.05,
                    exit_price: 0.055,
                    amount: 2,
                    entry_notional: 100,
                    net_pnl: 10,
                    pnl_pct: 10,
                    outcome: 'WIN',
                    close_reason: '手动平仓',
                    trendline_id: 0,
                    duration_str: '10m',
                  },
                  {
                    symbol: 'XRPUSDT',
                    side: 'LONG',
                    closed_ts: 2000,
                    closed_at: '2026-11-26 11:00:00',
                    entry_price: 0.5,
                    exit_price: 0.5,
                    amount: 100,
                    entry_notional: 50,
                    net_pnl: 0,
                    pnl_pct: 0,
                    outcome: 'BREAKEVEN',
                    close_reason: '手动平仓',
                    trendline_id: 2,
                    trendline_label: 'TL#2 (0.5100 -> 0.4900)',
                    duration_str: '5m',
                  },
                ],
                equity_curve: [{ datetime: '2026-11-26 10:00:00', equity: 1000 }],
                daily_pnl: [{ date: '2026-11-26', net_pnl: 10, trades_count: 2 }],
                pnl_distribution: {
                  bins: [{ label: '0%', min: -0.00001, max: 0.00001, count: 1, trades: ['XRPUSDT'] }],
                },
              };

              const win = {
                document: {
                  hidden: false,
                  readyState: 'complete',
                  activeElement: null,
                  getElementById: getEl,
                  querySelector: () => null,
                  querySelectorAll: (sel) => (sel === '#trade-history-table th.sortable' ? sortableHeaders : []),
                  createElement: (tag) => makeEl(tag),
                  addEventListener() {},
                },
                Chart: FakeChart,
                fetch: async (url) => ({
                  ok: true,
                  status: 200,
                  headers: { get: () => null },
                  text: async () => JSON.stringify(
                    url === 'api/config' ? { tradingview_layout_id: 'ig0LuDL8' } : payload
                  ),
                }),
                AbortController,
                DOMException,
                setTimeout,
                clearTimeout,
                addEventListener() {},
                console: { error(e) { throw e; }, warn() {} },
              };
              win.window = win;

              vm.runInNewContext(commonCode, win);
              vm.runInNewContext(
                acctCode.replace(/\\}\\)\\(\\);\\s*$/, 'window.__acctTest = { HISTORY_SORT };})();'),
                win
              );
              await new Promise((r) => setTimeout(r, 30));

              // #26: NOT_CONFIGURED uses gentle neutral badge, not red API failure
              const badge = getEl('api-status-badge');
              assert.ok(badge.innerHTML.includes('未配置币安密钥'));
              assert.ok(!badge.className.includes('bg-[#f25565]'));
              assert.ok(!badge.innerHTML.includes('接口状态异常'));

              // #28: generated_at ISO formatted to YYYY-MM-DD HH:mm:ss Beijing time
              assert.strictEqual(getEl('sync-time-text').textContent, '2026-11-26 12:00:00');

              // #28: 空仓在岸 -> 当前空仓
              assert.strictEqual(getEl('kpi-margin-badge').textContent, '当前空仓');

              // #26 & #28: Active positions render trendline_id === 0, prioritize trendline_label, include units, title, and dec4Threshold=0.01
              const posHtml = getEl('active-positions-tbody').innerHTML;
              assert.ok(posHtml.includes('>0<'), 'trendline_id === 0 should render as 0');
              assert.ok(posHtml.includes('TL#7 (155.0000 -&gt; 148.0000)'));
              assert.ok(posHtml.includes('0.5 BTC'));
              assert.ok(posHtml.includes('10 SOL'));
              assert.ok(posHtml.includes('$0.0500<'), 'sub-1 price >= 0.01 should use 4 decimals');
              assert.ok(posHtml.includes('title="在新标签页打开 BTCUSDT TradingView 图表"'));

              // #26 & #28 & #29: History table removes .trade-row, renders unit, title, trendline_id===0 & trendline_label
              const histHtml = getEl('history-tbody').innerHTML;
              assert.ok(!histHtml.includes('trade-row'));
              assert.ok(histHtml.includes('>0<'));
              assert.ok(histHtml.includes('TL#2 (0.5100 -&gt; 0.4900)'));
              assert.ok(histHtml.includes('2 ETH'));
              assert.ok(histHtml.includes('100 XRP'));
              assert.ok(histHtml.includes('$0.0500<'));
              assert.ok(histHtml.includes('title="在新标签页打开 ETHUSDT TradingView 图表"'));

              // #22 & #29: Chart colors converged to DESIGN.md & #647894
              assert.strictEqual(FakeChart.defaults.color, '#9fb0c5');
              assert.strictEqual(chartsById['chart-win-loss'].data.datasets[0].backgroundColor[2], '#647894');
              assert.strictEqual(chartsById['chart-pnl-dist'].data.datasets[0].backgroundColor[0], '#647894');
              assert.strictEqual(chartsById['chart-equity-curve'].data.datasets[1].borderColor, '#7f91a8');

              // #28: Sorting and reset restores HISTORY_SORT to closed_ts desc
              sortableHeaders[1]._btn.dispatch('click', {});
              assert.strictEqual(win.__acctTest.HISTORY_SORT.column, 'symbol');
              assert.strictEqual(win.__acctTest.HISTORY_SORT.asc, true);
              assert.strictEqual(sortableHeaders[1].getAttribute('aria-sort'), 'ascending');
              assert.strictEqual(sortableHeaders[0].hasAttribute('aria-sort'), false);

              getEl('btn-reset-history-filter').dispatch('click', {});
              assert.strictEqual(win.__acctTest.HISTORY_SORT.column, 'closed_ts');
              assert.strictEqual(win.__acctTest.HISTORY_SORT.asc, false);
              assert.strictEqual(sortableHeaders[0].getAttribute('aria-sort'), 'descending');
              assert.strictEqual(sortableHeaders[1].hasAttribute('aria-sort'), false);

              // --------------------------------------------------------------
              // Case 2: When SecondLegCommon provides EXT_ARROW_SVG, debounce, bindSortButtons
              // --------------------------------------------------------------
              let debounceCalled = 0;
              let cancelCalled = 0;
              let bindSortCalled = 0;
              const els2 = {};
              const histTable2 = makeEl('trade-history-table', { querySelectorAll: () => [] });
              els2['trade-history-table'] = histTable2;
              const getEl2 = (id) => (els2[id] ||= makeEl(id));
              const winShared = {
                SecondLegCommon: {
                  EXT_ARROW_SVG: '<svg data-shared-arrow="1"></svg>',
                  escapeHtml: (v) => String(v ?? ''),
                  isFiniteNum: (v) => typeof v === 'number' ? Number.isFinite(v) : (typeof v === 'string' && v.trim() !== '' && Number.isFinite(Number(v))),
                  formatCryptoPrice: (v) => String(v),
                  formatSignedPct: (v) => String(v) + '%',
                  getTvUrl: (s) => 'https://cn.tradingview.com/?symbol=' + s,
                  ensureConfig: async () => 'ig0LuDL8',
                  fetchJsonWithTimeout: async () => ({ status: 200, notModified: false, etag: null, data: payload, text: JSON.stringify(payload) }),
                  toBeijingDate: (ms) => new Date(ms + 8 * 3600 * 1000),
                  debounce: (fn, ms) => {
                    debounceCalled += 1;
                    const wrapped = (...args) => fn(...args);
                    wrapped.cancel = () => { cancelCalled += 1; };
                    return wrapped;
                  },
                  bindSortButtons: (rootEl, onSort) => {
                    bindSortCalled += 1;
                    assert.strictEqual(rootEl, histTable2);
                    assert.strictEqual(typeof onSort, 'function');
                    return { syncAria() {} };
                  },
                },
                document: {
                  hidden: false,
                  readyState: 'complete',
                  getElementById: getEl2,
                  querySelector: () => null,
                  querySelectorAll: () => [],
                  createElement: (tag) => makeEl(tag),
                  addEventListener() {},
                },
                Chart: FakeChart,
                AbortController,
                DOMException,
                setTimeout,
                clearTimeout,
                addEventListener() {},
                console: { error(e) { throw e; }, warn() {} },
              };
              winShared.window = winShared;
              vm.runInNewContext(acctCode, winShared);
              await new Promise((r) => setTimeout(r, 20));
              assert.strictEqual(debounceCalled, 1);
              assert.strictEqual(bindSortCalled, 1);
              assert.ok(getEl2('active-positions-tbody').innerHTML.includes('data-shared-arrow="1"'));
              getEl2('btn-reset-history-filter').dispatch('click', {});
              assert.strictEqual(cancelCalled, 1);
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


if __name__ == "__main__":
    unittest.main()
