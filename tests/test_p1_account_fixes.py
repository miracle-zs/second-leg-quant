#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for account page P1 fixes (#7, #8, #10, #12, #17, #18, #19, #20)."""

from pathlib import Path
import re
import shutil
import subprocess
import textwrap
import unittest


ROOT_DIR = Path(__file__).resolve().parent.parent


class TestAccountP1Fixes(unittest.TestCase):
    def test_static_account_html_and_js_p1_contracts(self):
        html = (ROOT_DIR / "web/account.html").read_text(encoding="utf-8")
        js = (
            (ROOT_DIR / "web/account-tables.js").read_text(encoding="utf-8")
            + "\n"
            + (ROOT_DIR / "web/account-charts.js").read_text(encoding="utf-8")
            + "\n"
            + (ROOT_DIR / "web/account.js").read_text(encoding="utf-8")
        )

        # #20: Primary CTA uses .btn-primary-cobalt instead of ghost styling
        tv_cta_match = re.search(r'<a\b[^>]*data-tv-layout-link[^>]*>', html)
        self.assertIsNotNone(tv_cta_match)
        tv_cta_tag = tv_cta_match.group(0)
        self.assertIn("btn-primary-cobalt", tv_cta_tag)
        self.assertNotIn("bg-[#3b82f6]/[0.12]", tv_cta_tag)

        # #20: Chinese labels/legends must not inherit font-mono
        self.assertIn('<span class="font-sans">快照:</span>', html)
        self.assertIn('<span id="sync-time-text" class="font-mono tabular-nums">--</span>', html)
        self.assertIn('<span id="kpi-sharpe-rating" class="text-[#9fb0c5] font-medium">--</span>', html)
        self.assertNotIn('font-mono tabular-nums bg-[#151c27] text-[#9fb0c5] border border-[#283446]">--</span>', html)
        for badge_id in (
            "daily-days-badge",
            "win-loss-summary-badge",
            "dist-summary-badge",
            "active-pos-count-badge",
            "history-count-badge",
        ):
            m = re.search(rf'<span\b[^>]*id="{badge_id}"[^>]*>', html)
            self.assertIsNotNone(m, f"Missing badge #{badge_id}")
            self.assertIn("font-sans", m.group(0), f"#{badge_id} should use font-sans")
            self.assertNotIn("font-mono", m.group(0), f"#{badge_id} should not use font-mono")

        # #18: Remove focus:outline-none on history search & outcome filter
        search_match = re.search(r'<input\b[^>]*id="history-search-input"[^>]*>', html)
        filter_match = re.search(r'<select\b[^>]*id="history-outcome-filter"[^>]*>', html)
        self.assertIsNotNone(search_match)
        self.assertIsNotNone(filter_match)
        self.assertNotIn("focus:outline-none", search_match.group(0))
        self.assertNotIn("focus:outline-none", filter_match.group(0))

        # #19: Tier-2 profit factor uses tier2-split-row instead of sm:flex-row
        self.assertIn("tier2-split-row", html)
        self.assertNotIn("sm:flex-row", html)

        # #19: Account tables have max-h-[65vh] scroll boundary so sticky-header works
        pos_wrap = re.search(r'<div\b[^>]*id="active-positions-table-wrap"[^>]*>', html)
        hist_wrap = re.search(r'<div\b[^>]*id="history-table-wrap"[^>]*>', html)
        self.assertIsNotNone(pos_wrap)
        self.assertIsNotNone(hist_wrap)
        self.assertIn("max-h-[65vh]", pos_wrap.group(0))
        self.assertIn("max-h-[65vh]", hist_wrap.group(0))

        # #19: Active position <td> cells have whitespace-nowrap
        pos_fn_body = js.split("function renderActivePositions")[1].split("function buildHistorySearchKey")[0]
        td_tags = re.findall(r"<td\b[^>]*>", pos_fn_body)
        self.assertEqual(len(td_tags), 9)
        for td in td_tags:
            self.assertIn("whitespace-nowrap", td)

        # #10 & #8: Chart fallback containers & pagination bar present
        for cid in ("chart-equity-curve", "chart-daily-pnl", "chart-win-loss", "chart-pnl-dist"):
            self.assertIn(f'id="{cid}-fallback"', html)
        for pid in ("history-pagination-bar", "history-page-range", "history-page-info", "btn-history-prev-page", "btn-history-next-page"):
            self.assertIn(f'id="{pid}"', html)

        # #12: Uses setTimeout polling instead of setInterval
        self.assertNotIn("setInterval(", js)
        self.assertIn("pagehide", js)

    def test_runtime_p1_account_fixes_in_node_vm(self):
        node_bin = shutil.which("node")
        self.assertIsNotNone(node_bin, "node is required to run account.js runtime tests")

        script = textwrap.dedent(
            """
            const assert = require('assert');
            const fs = require('fs');
            const path = require('path');
            const vm = require('vm');

            const root = process.cwd();
            const commonCode = fs.readFileSync(path.join(root, 'web/common.js'), 'utf8');
            const acctTablesCode = fs.readFileSync(path.join(root, 'web/account-tables.js'), 'utf8');
            const acctChartsCode = fs.readFileSync(path.join(root, 'web/account-charts.js'), 'utf8');
            const acctCode = fs.readFileSync(path.join(root, 'web/account.js'), 'utf8');

            function makeEl(id) {
              const attrs = {};
              const listeners = {};
              const classSet = new Set();
              let innerHtmlVal = '';
              let innerHtmlWrites = 0;
              return {
                id,
                textContent: '',
                title: '',
                className: '',
                value: '',
                hidden: false,
                disabled: false,
                dataset: {},
                style: {},
                parentElement: null,
                get innerHTML() { return innerHtmlVal; },
                set innerHTML(v) { innerHtmlVal = String(v); innerHtmlWrites += 1; },
                get _innerHtmlWrites() { return innerHtmlWrites; },
                classList: {
                  add(...cls) { cls.forEach((c) => classSet.add(c)); },
                  remove(...cls) { cls.forEach((c) => classSet.delete(c)); },
                  toggle(c, force) {
                    if (force === undefined) {
                      if (classSet.has(c)) classSet.delete(c);
                      else classSet.add(c);
                    } else if (force) {
                      classSet.add(c);
                    } else {
                      classSet.delete(c);
                    }
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
              };
            }

            (async () => {
              // --------------------------------------------------------------
              // A. #7 Chart lifecycle, options sync, canvas change, pagehide,
              //    decimation (>500 points), #8 search key + pagination,
              //    #12 activeLoadId + setTimeout polling, #17 aria-live + retry
              // --------------------------------------------------------------
              const els = {};
              const docListeners = {};
              const winListeners = {};
              const getEl = (id) => (els[id] ||= makeEl(id));

              const chartInstancesByCanvas = new Map();
              let destroyCalls = 0;
              function FakeChart(canvas, config) {
                this.canvas = canvas;
                this.data = config.data;
                this.options = config.options;
                this.update = () => {};
                this.destroy = () => { destroyCalls += 1; };
                chartInstancesByCanvas.set(canvas.id, this);
              }
              FakeChart.defaults = { color: '', font: {}, borderColor: '' };

              let scheduledTimers = [];
              let nextTimerId = 100;
              function fakeSetTimeout(fn, ms) {
                const id = ++nextTimerId;
                scheduledTimers.push({ id, fn, ms, cleared: false });
                return id;
              }
              function fakeClearTimeout(id) {
                const t = scheduledTimers.find((x) => x.id === id);
                if (t) t.cleared = true;
              }

              let accountMode = 'FAIL';
              let pendingAccountRequests = [];
              let accountFetchCount = 0;
              let currentPayload = null;

              const win = {
                document: {
                  hidden: false,
                  readyState: 'complete',
                  activeElement: null,
                  getElementById: getEl,
                  querySelector: () => null,
                  querySelectorAll: () => [],
                  createElement: (tag) => makeEl(tag),
                  addEventListener(ev, fn) { (docListeners[ev] ||= []).push(fn); },
                },
                Chart: FakeChart,
                fetch: (url, opts) => {
                  if (url === 'api/config') {
                    return Promise.resolve({
                      ok: true,
                      status: 200,
                      headers: { get: () => null },
                      text: async () => JSON.stringify({ tradingview_layout_id: 'ig0LuDL8' }),
                    });
                  }
                  accountFetchCount += 1;
                  const idx = accountFetchCount;
                  if (accountMode === 'FAIL') {
                    return Promise.resolve({ ok: false, status: 503, headers: { get: () => null }, text: async () => 'err' });
                  }
                  if (accountMode === 'PENDING') {
                    return new Promise((resolve, reject) => {
                      if (opts && opts.signal) {
                        opts.signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true });
                      }
                      pendingAccountRequests.push({ idx, resolve });
                    });
                  }
                  return Promise.resolve({
                    ok: true,
                    status: 200,
                    headers: { get: () => '"etag-1"' },
                    text: async () => JSON.stringify(currentPayload),
                  });
                },
                AbortController,
                DOMException,
                setTimeout: fakeSetTimeout,
                clearTimeout: fakeClearTimeout,
                addEventListener(ev, fn) { (winListeners[ev] ||= []).push(fn); },
                console: { error() {}, warn() {} },
              };
              win.window = win;

              vm.runInNewContext(commonCode, win);
              vm.runInNewContext(acctTablesCode, win);
              vm.runInNewContext(acctChartsCode, win);
              vm.runInNewContext(
                acctCode.replace(/\\}\\)\\(\\);\\s*$/, 'window.__acctTest = { renderDashboard, tryLoadData, upsertChart, downsampleEquityCurve, getCurrentAccountData: () => currentAccountData };})();'),
                win
              );

              // Wait for initial failed load
              await new Promise((r) => setTimeout(r, 20));
              const statusEl = getEl('account-load-status');
              assert.strictEqual(statusEl.hidden, false);
              assert.ok(statusEl.innerHTML.includes('id="btn-retry-account"'));
              assert.ok(statusEl.innerHTML.includes('立即重试'));
              // Poll timer should be scheduled after failure
              assert.strictEqual(scheduledTimers.filter((t) => !t.cleared && t.ms === 30000).length, 1);

              // Build 125 trade_history items and 1200 equity_curve points
              const largeEqCurve = [];
              for (let i = 0; i < 1200; i += 1) {
                const day = String(1 + Math.floor(i / 48)).padStart(2, '0');
                const hr = String(i % 24).padStart(2, '0');
                largeEqCurve.push({ datetime: `2026-11-${day} ${hr}:00:00`, equity: 1000 + i });
              }
              const largeHistory = [];
              for (let i = 0; i < 125; i += 1) {
                largeHistory.push({
                  symbol: i === 0 ? 'SPECIALCOINUSDT' : `COIN${i}USDT`,
                  side: 'LONG',
                  closed_ts: 2000000 - i * 1000,
                  closed_at: '2026-11-25 12:00:00',
                  entry_time: '2026-11-25 11:00:00',
                  entry_price: 10,
                  exit_price: 11,
                  amount: 5,
                  entry_notional: 50,
                  net_pnl: i % 2 === 0 ? 5 : -2,
                  pnl_pct: i % 2 === 0 ? 10 : -4,
                  outcome: i % 2 === 0 ? 'WIN' : 'LOSS',
                  close_reason: i === 0 ? '移动保本锁利' : '止损',
                  trendline_label: `TL#${i}`,
                  duration_str: '1h',
                });
              }
              currentPayload = {
                api_status: { mode: 'BINANCE_REALTIME' },
                generated_at: '2026-11-25 12:00:00',
                summary: {
                  initial_capital: 1000,
                  total_equity: 2199,
                  win_count: 63,
                  loss_count: 62,
                  breakeven_count: 0,
                  win_rate_pct: 50.4,
                },
                active_positions: [],
                trade_history: largeHistory,
                equity_curve: largeEqCurve,
                daily_pnl: [{ date: '2026-11-25', net_pnl: 100, trades_count: 2 }],
                pnl_distribution: { bins: [{ label: '0~5%', min: 0, max: 5, count: 63, trades: ['SPECIALCOINUSDT'] }] },
              };

              // #17: Click #btn-retry-account to recover
              accountMode = 'OK';
              const retryBtn = makeEl('btn-retry-account');
              win.document.activeElement = retryBtn;
              statusEl.dispatch('click', {
                target: { closest: (sel) => (sel === '#btn-retry-account' ? retryBtn : null) },
              });
              await new Promise((r) => setTimeout(r, 20));
              assert.strictEqual(statusEl.hidden, true);
              assert.strictEqual(getEl('account-report').hidden, false);

              // #7: Decimation caps chart points to <= 500 while preserving endpoints & true peak label
              const eqChart = chartInstancesByCanvas.get('chart-equity-curve');
              assert.ok(eqChart.data.labels.length <= 500);
              assert.strictEqual(eqChart.data.datasets[0].data[0], 1000);
              assert.strictEqual(eqChart.data.datasets[0].data[eqChart.data.datasets[0].data.length - 1], 2199);
              assert.strictEqual(getEl('equity-peak-label').textContent, '峰值: $2,199.00');

              // #7: upsertChart updates options in-place on same canvas, destroys on new canvas
              const origOptsRef = eqChart.options;
              const newCanvas = makeEl('chart-equity-curve');
              win.__acctTest.upsertChart('eq', eqChart.canvas, {
                type: 'line',
                data: eqChart.data,
                options: { ...origOptsRef, customTestFlag: 42 },
              });
              assert.strictEqual(eqChart.options, origOptsRef);
              assert.strictEqual(eqChart.options.customTestFlag, 42);
              win.__acctTest.upsertChart('eq', newCanvas, {
                type: 'line',
                data: eqChart.data,
                options: origOptsRef,
              });
              assert.strictEqual(destroyCalls, 1);

              // #8: Precomputed _searchKey + pagination (50 per page -> 3 pages for 125 items)
              const loadedHistory = win.__acctTest.getCurrentAccountData().trade_history;
              assert.ok(typeof loadedHistory[0]._searchKey === 'string' && loadedHistory[0]._searchKey.includes('specialcoinusdt'));
              assert.strictEqual(getEl('history-pagination-bar').hidden, false);
              assert.strictEqual(getEl('history-page-range').textContent, '显示第 1 - 50 笔 · 共 125 笔');
              assert.strictEqual(getEl('history-page-info').textContent, '1 / 3');
              assert.strictEqual(getEl('btn-history-prev-page').disabled, true);
              assert.strictEqual(getEl('btn-history-next-page').disabled, false);

              getEl('btn-history-next-page').dispatch('click', {});
              assert.strictEqual(getEl('history-page-range').textContent, '显示第 51 - 100 笔 · 共 125 笔');
              assert.strictEqual(getEl('history-page-info').textContent, '2 / 3');
              getEl('btn-history-next-page').dispatch('click', {});
              assert.strictEqual(getEl('history-page-range').textContent, '显示第 101 - 125 笔 · 共 125 笔');
              assert.strictEqual(getEl('history-page-info').textContent, '3 / 3');
              assert.strictEqual(getEl('btn-history-next-page').disabled, true);

              // Search filter resets to page 1 and hides pagination bar when <= 50 matches
              getEl('history-search-input').value = 'specialcoinusdt';
              getEl('history-outcome-filter').dispatch('change', {});
              assert.strictEqual(getEl('history-count-badge').textContent, '显示 1 / 共 125 笔');
              assert.strictEqual(getEl('history-pagination-bar').hidden, true);
              getEl('btn-reset-history-filter').dispatch('click', {});
              assert.strictEqual(getEl('history-count-badge').textContent, '共 125 笔');
              assert.strictEqual(getEl('history-pagination-bar').hidden, false);

              // #17: aria-live status badge deduplication across identical polls
              const badgeWritesAfterFirstOk = getEl('api-status-badge')._innerHtmlWrites;
              await win.__acctTest.tryLoadData();
              await win.__acctTest.tryLoadData();
              assert.strictEqual(getEl('api-status-badge')._innerHtmlWrites, badgeWritesAfterFirstOk);

              // #12: Manual refresh button protection + activeLoadId guard + poll timer reset
              const refreshBtn = getEl('btn-manual-refresh');
              const spinner = getEl('refresh-spinner');
              accountMode = 'PENDING';
              refreshBtn.dispatch('click', {});
              assert.strictEqual(refreshBtn.disabled, true);
              assert.strictEqual(refreshBtn.getAttribute('aria-busy'), 'true');
              assert.strictEqual(spinner.classList.contains('animate-spin'), true);

              // Supersede manual refresh with a newer load (simulating visibility refresh)
              const pNew = win.__acctTest.tryLoadData();
              await new Promise((r) => setTimeout(r, 0));
              // Aborted first load must NOT stop spinner while second load is in-flight
              assert.strictEqual(spinner.classList.contains('animate-spin'), true);
              assert.strictEqual(refreshBtn.disabled, true);

              // Complete newer load -> spinner stops, button re-enabled, fresh 30s timer scheduled
              const activeTimersBefore = scheduledTimers.filter((t) => !t.cleared && t.ms === 30000).length;
              pendingAccountRequests[pendingAccountRequests.length - 1].resolve({
                ok: true,
                status: 304,
                headers: { get: () => '"etag-1"' },
                text: async () => '',
              });
              await pNew;
              assert.strictEqual(spinner.classList.contains('animate-spin'), false);
              assert.strictEqual(refreshBtn.disabled, false);
              assert.strictEqual(refreshBtn.hasAttribute('aria-busy'), false);
              assert.strictEqual(scheduledTimers.filter((t) => !t.cleared && t.ms === 30000).length, 1);
              assert.ok(activeTimersBefore <= 1);

              // #7: pagehide destroys remaining chart instances & clears timers
              const destroyBeforePagehide = destroyCalls;
              (winListeners.pagehide || []).forEach((fn) => fn({}));
              assert.strictEqual(destroyCalls, destroyBeforePagehide + 4);
              assert.strictEqual(scheduledTimers.filter((t) => !t.cleared && t.ms === 30000).length, 0);

              // --------------------------------------------------------------
              // B. #10: Missing Chart.js fallback & missing summary fields error
              // --------------------------------------------------------------
              const els2 = {};
              const getEl2 = (id) => (els2[id] ||= makeEl(id));
              let payload2 = {
                api_status: { mode: 'BINANCE_REALTIME' },
                generated_at: '2026-11-25 12:00:00',
                summary: { win_count: 1, loss_count: 0, breakeven_count: 0 },
                active_positions: [],
                trade_history: [],
                equity_curve: [],
                daily_pnl: [],
                pnl_distribution: { bins: [] },
              };
              const winNoChart = {
                document: {
                  hidden: false,
                  readyState: 'loading',
                  getElementById: getEl2,
                  querySelector: () => null,
                  querySelectorAll: () => [],
                  createElement: (tag) => makeEl(tag),
                  addEventListener() {},
                },
                // Chart deliberately omitted (undefined)
                fetch: async (url) => ({
                  ok: true,
                  status: 200,
                  headers: { get: () => null },
                  text: async () => JSON.stringify(url === 'api/config' ? { tradingview_layout_id: 'ig0LuDL8' } : payload2),
                }),
                AbortController,
                DOMException,
                setTimeout: fakeSetTimeout,
                clearTimeout: fakeClearTimeout,
                addEventListener() {},
                console: { error() {}, warn() {} },
              };
              winNoChart.window = winNoChart;
              vm.runInNewContext(commonCode, winNoChart);
              vm.runInNewContext(acctTablesCode, winNoChart);
              vm.runInNewContext(acctChartsCode, winNoChart);
              vm.runInNewContext(
                acctCode.replace(/\\}\\)\\(\\);\\s*$/, 'window.__acctTest = { tryLoadData };})();'),
                winNoChart
              );
              await winNoChart.__acctTest.tryLoadData();
              for (const cid of ['chart-equity-curve', 'chart-daily-pnl', 'chart-win-loss', 'chart-pnl-dist']) {
                assert.strictEqual(getEl2(cid).hidden, true);
                assert.strictEqual(getEl2(cid + '-fallback').hidden, false);
                assert.ok(getEl2(cid + '-fallback').textContent.includes('Chart.js'));
              }

              // Legacy/incomplete summary missing win_count -> friendly error banner + retry button
              payload2 = {
                api_status: { mode: 'OFFLINE_FALLBACK' },
                summary: { winning_trades: 3 },
                active_positions: [],
                trade_history: [],
                equity_curve: [],
                daily_pnl: [],
                pnl_distribution: { bins: [] },
              };
              await winNoChart.__acctTest.tryLoadData();
              const status2 = getEl2('account-load-status');
              assert.strictEqual(status2.hidden, false);
              assert.ok(status2.innerHTML.includes('摘要字段缺失'));
              assert.ok(status2.innerHTML.includes('id="btn-retry-account"'));
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
