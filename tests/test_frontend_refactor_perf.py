#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for refactored frontend modules, shared Common utilities, and DOM/Canvas perf optimizations."""

from pathlib import Path
import shutil
import subprocess
import textwrap
import unittest


ROOT_DIR = Path(__file__).resolve().parent.parent


class TestFrontendRefactorAndPerf(unittest.TestCase):
    def test_html_script_order_and_static_module_boundaries(self):
        index_html = (ROOT_DIR / "web/index.html").read_text(encoding="utf-8")
        account_html = (ROOT_DIR / "web/account.html").read_text(encoding="utf-8")

        self.assertLess(index_html.index("static/common.js"), index_html.index("static/kline-modal.js"))
        self.assertLess(index_html.index("static/kline-modal.js"), index_html.index("static/candidates-render.js"))
        self.assertLess(index_html.index("static/candidates-render.js"), index_html.index("static/candidates.js"))

        self.assertLess(account_html.index("static/vendor/chart.js"), account_html.index("static/common.js"))
        self.assertLess(account_html.index("static/common.js"), account_html.index("static/kline-modal.js"))
        self.assertLess(account_html.index("static/kline-modal.js"), account_html.index("static/account-tables.js"))
        self.assertLess(account_html.index("static/account-tables.js"), account_html.index("static/account-charts.js"))
        self.assertLess(account_html.index("static/account-charts.js"), account_html.index("static/account.js"))

        index_html = (ROOT_DIR / "web/index.html").read_text(encoding="utf-8")
        for cid in ("modal-copy-btn", "modal-context-hud", "btn-filter-fresh", "btn-filter-healthy-fr"):
            self.assertIn(f'id="{cid}"', index_html)
        for aid in ("history-side-filter", "history-filtered-summary"):
            self.assertIn(f'id="{aid}"', account_html)

        # Ensure monolithic files were genuinely decomposed
        self.assertLess(len((ROOT_DIR / "web/candidates.js").read_text(encoding="utf-8").splitlines()), 1200)
        self.assertLess(len((ROOT_DIR / "web/account.js").read_text(encoding="utf-8").splitlines()), 500)

    def test_shared_common_helpers_perf_memoization_and_canvas_batching_in_node_vm(self):
        node_bin = shutil.which("node")
        self.assertIsNotNone(node_bin, "node is required to test refactored frontend modules")

        script = textwrap.dedent(
            """
            const assert = require('assert');
            const fs = require('fs');
            const path = require('path');
            const vm = require('vm');

            const root = process.cwd();
            const commonCode = fs.readFileSync(path.join(root, 'web/common.js'), 'utf8');
            const candRenderCode = fs.readFileSync(path.join(root, 'web/candidates-render.js'), 'utf8');
            const acctTablesCode = fs.readFileSync(path.join(root, 'web/account-tables.js'), 'utf8');
            const acctChartsCode = fs.readFileSync(path.join(root, 'web/account-charts.js'), 'utf8');
            const modalCode = fs.readFileSync(path.join(root, 'web/kline-modal.js'), 'utf8');

            // 1. Common helpers: formatBeijingTime, setIfChanged, setButtonBusy, updateStatusBadge, createPollScheduler
            {
              const timers = [];
              let nextId = 1;
              const doc = {
                hidden: false,
                readyState: 'complete',
                querySelector: () => null,
                querySelectorAll: () => [],
              };
              const win = {
                document: doc,
                setTimeout(fn, ms) {
                  const id = nextId++;
                  const obj = { id, fn, ms, cleared: false, unrefCalled: false, unref() { this.unrefCalled = true; } };
                  timers.push(obj);
                  return obj;
                },
                clearTimeout(t) { if (t) t.cleared = true; },
                console: { error() {}, warn() {} },
              };
              win.window = win;
              vm.runInNewContext(commonCode, win);
              const C = win.SecondLegCommon;

              const ts = Date.UTC(2026, 0, 5, 1, 5, 9); // 09:05:09 Beijing
              assert.strictEqual(C.formatBeijingTime(ts), '2026-01-05 09:05:09');
              assert.strictEqual(C.formatBeijingTime(ts, { format: 'time' }), '09:05:09');
              assert.strictEqual(C.formatBeijingTime(ts, { format: 'mdhm' }), '01/05 09:05');
              assert.strictEqual(C.formatBeijingTime(ts, { format: 'md' }), '01/05');
              assert.strictEqual(C.formatBeijingTime('2026-11-28 08:30:00', { suffix: ' (北京时间)' }), '2026-11-28 08:30:00 (北京时间)');

              let textWrites = 0;
              let _txt = 'abc';
              const el = {
                get textContent() { return _txt; },
                set textContent(v) { textWrites += 1; _txt = v; },
              };
              C.setIfChanged(el, 'textContent', 'abc');
              assert.strictEqual(textWrites, 0);
              C.setIfChanged(el, 'textContent', 'def');
              assert.strictEqual(textWrites, 1);

              const badgeCache = { className: '', html: '' };
              let htmlWrites = 0;
              let _html = '';
              const badge = {
                title: '',
                className: '',
                get innerHTML() { return _html; },
                set innerHTML(v) { htmlWrites += 1; _html = v; },
              };
              assert.strictEqual(C.updateStatusBadge(badge, badgeCache, { className: 'ok', dotClass: 'd', label: '在线' }), true);
              assert.strictEqual(C.updateStatusBadge(badge, badgeCache, { className: 'ok', dotClass: 'd', label: '在线' }), false);
              assert.strictEqual(htmlWrites, 1);

              let fired = 0;
              const poller = C.createPollScheduler(() => { fired += 1; }, 5000);
              poller.schedule();
              assert.strictEqual(poller.isScheduled(), true);
              assert.strictEqual(timers[0].unrefCalled, true);
              timers[0].fn();
              assert.strictEqual(fired, 1);
              assert.strictEqual(poller.isScheduled(), false);
              doc.hidden = true;
              poller.schedule();
              assert.strictEqual(poller.isScheduled(), false);
            }

            // 2. AccountTables memoization & active positions signature skip + AccountCharts signature skip
            {
              const win = {
                document: {
                  hidden: false,
                  readyState: 'complete',
                  getElementById: (id) => (els[id] ||= makeEl(id)),
                  querySelector: () => null,
                  querySelectorAll: () => [],
                },
                console: { error() {}, warn() {} },
              };
              const els = {};
              function makeEl(id) {
                let htmlVal = '';
                let htmlWrites = 0;
                const attrs = {};
                const cls = new Set();
                return {
                  id,
                  value: '',
                  hidden: false,
                  disabled: false,
                  textContent: '',
                  title: '',
                  isConnected: true,
                  classList: {
                    add(...c) { c.forEach((x) => cls.add(x)); },
                    remove(...c) { c.forEach((x) => cls.delete(x)); },
                    toggle(c, f) { if (f === undefined) { if (cls.has(c)) cls.delete(c); else cls.add(c); } else if (f) cls.add(c); else cls.delete(c); },
                  },
                  setAttribute(k, v) { attrs[k] = String(v); },
                  getAttribute(k) { return attrs[k] || null; },
                  get innerHTML() { return htmlVal; },
                  set innerHTML(v) { htmlWrites += 1; htmlVal = v; },
                  get _htmlWrites() { return htmlWrites; },
                };
              }
              let chartUpdates = 0;
              function FakeChart(canvas, config) {
                this.canvas = canvas;
                this.data = config.data;
                this.options = config.options;
                this.update = () => { chartUpdates += 1; };
                this.destroy = () => {};
              }
              FakeChart.defaults = { color: '', font: {}, borderColor: '' };
              win.Chart = FakeChart;
              win.window = win;
              vm.runInNewContext(commonCode, win);
              win.SecondLegCommon.setTvLayoutId('ig0LuDL8');
              vm.runInNewContext(acctTablesCode, win);
              vm.runInNewContext(acctChartsCode, win);

              const T = win.SecondLegAccountTables;
              const Ch = win.SecondLegAccountCharts;

              // Active positions unchanged -> skips rewriting tbody.innerHTML
              const pos = [{ symbol: 'BTCUSDT', side: 'LONG', risk_monitored: true, entry_price: 100, mark_price: 102, unrealized_pnl: 20, roe_pct: 2, leverage: 5, notional: 1000, amount: 10, liquidation_price: 80 }];
              T.renderActivePositions(pos, (id) => (els[id] ||= makeEl(id)));
              T.renderActivePositions(pos, (id) => (els[id] ||= makeEl(id)));
              assert.strictEqual(els['active-positions-tbody']._htmlWrites, 1);

              // History memoization across page turns
              const history = Array.from({ length: 120 }, (_, i) => ({
                symbol: `COIN${i}USDT`,
                side: 'LONG',
                closed_ts: 100000 - i,
                net_pnl: i,
                pnl_pct: 1,
                outcome: 'WIN',
              }));
              let filterCalls = 0;
              const trackedHistory = new Proxy(history, {
                get(target, prop) {
                  if (prop === 'filter') {
                    return function (...args) {
                      filterCalls += 1;
                      return Array.prototype.filter.apply(target, args);
                    };
                  }
                  return Reflect.get(target, prop);
                },
              });
              const sortState = { column: 'closed_ts', asc: false };
              const r1 = T.getFilteredAndSortedHistory(trackedHistory, sortState);
              const r2 = T.getFilteredAndSortedHistory(trackedHistory, sortState);
              assert.strictEqual(filterCalls, 1);
              assert.strictEqual(r1, r2);

              // AccountCharts skips Chart.update('none') when chart data signatures are unchanged
              const payload = {
                summary: { initial_capital: 1000, win_count: 1, loss_count: 0, breakeven_count: 0, win_rate_pct: 100 },
                equity_curve: [{ datetime: '2026-11-28 08:00:00', equity: 1050 }],
                daily_pnl: [{ date: '2026-11-28', net_pnl: 50, trades_count: 1 }],
                pnl_distribution: { bins: [{ label: '0~5%', min: 0, max: 5, count: 1, trades: ['BTCUSDT'] }] },
              };
              const deps = {
                setText: (id, t) => T.setText(id, t, (x) => (els[x] ||= makeEl(x))),
                setSignedColorClass: T.setSignedColorClass,
                getAccountData: () => payload,
              };
              Ch.updateCharts(payload, deps);
              assert.strictEqual(chartUpdates, 0);
              Ch.updateCharts(payload, deps);
              assert.strictEqual(chartUpdates, 0, 'Unchanged chart signatures must skip redundant chart.update calls');

              const payloadDailyChanged = {
                ...payload,
                daily_pnl: [{ date: '2026-11-28', net_pnl: 80, trades_count: 2 }],
              };
              Ch.updateCharts(payloadDailyChanged, deps);
              assert.strictEqual(chartUpdates, 1, 'Only daily chart updates when only daily_pnl changes');
            }

            // 3. CandidatesRender zero-selector rowRefs & syncRowDom scalar skip + kline-modal batched strokes
            (async () => {
              // 3a. CandidatesRender cells fast path & scalar skip
              {
                const candEls = {};
                const makeCandEl = (id) => ({
                  id,
                  textContent: '',
                  innerHTML: '',
                  title: '',
                  style: {},
                  className: '',
                  classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
                  setAttribute() {},
                  getAttribute() { return null; },
                });
                const makeSub = (clsList = []) => {
                  let txt = '';
                  let txtWrites = 0;
                  return {
                    style: {},
                    className: clsList.join(' '),
                    classList: {
                      contains: (c) => clsList.includes(c),
                      add() {},
                      remove() {},
                    },
                    get textContent() { return txt; },
                    set textContent(v) { txtWrites += 1; txt = v; },
                    get _txtWrites() { return txtWrites; },
                    setAttribute() {},
                    getAttribute() { return null; },
                  };
                };
                const priceEl = makeSub(['sym-price']);
                const mcValEl = makeSub(['sym-mc-val']);
                const trBadgeEl = makeSub();
                const fr4hEl = makeSub(['sym-fr-4h']);
                const posBox = makeSub(['sym-pos-box']);
                const posBar = makeSub(['sym-pos-bar']);
                const posText = makeSub(['sym-pos-text']);
                posBox.firstElementChild = { firstElementChild: posBar };
                posBox.children = [posBox.firstElementChild, posText];
                const cells = [
                  makeSub(['sym-rank']),
                  { firstElementChild: { children: [makeSub(), makeSub(['sym-res-badge'])] } },
                  makeSub(),
                  { firstElementChild: priceEl, children: [priceEl, posBox] },
                  makeSub(['sym-chg']),
                  { firstElementChild: mcValEl },
                  makeSub(['sym-vol']),
                  { firstElementChild: trBadgeEl },
                  { firstElementChild: { firstElementChild: fr4hEl }, children: [{}, makeSub(['sym-fr-settle'])] },
                  makeSub(),
                  makeSub(['sym-duration']),
                ];
                let rowQuerySelectorCalls = 0;
                const rowEl = {
                  id: 'row-AAAUSDT',
                  style: {},
                  cells,
                  getAttribute: (k) => (k === 'data-sym' ? 'AAAUSDT' : null),
                  querySelector() { rowQuerySelectorCalls += 1; return null; },
                };
                const emptyRow = makeCandEl('empty-search-row');
                const tbody = {
                  innerHTML: '',
                  children: [emptyRow, rowEl],
                  querySelectorAll() { throw new Error('Should not call querySelectorAll when children present'); },
                };
                const winCand = {
                  document: {
                    hidden: false,
                    readyState: 'complete',
                    getElementById: (id) => (candEls[id] ||= makeCandEl(id)),
                    querySelector: () => null,
                    querySelectorAll: () => [],
                  },
                  console: { error() {}, warn() {} },
                };
                winCand.window = winCand;
                vm.runInNewContext(commonCode, winCand);
                winCand.SecondLegCommon.setTvLayoutId('ig0LuDL8');
                vm.runInNewContext(candRenderCode, winCand);
                const R = winCand.SecondLegCandidatesRender;
                const rowRefs = new Map();
                const cand = {
                  symbol: 'AAAUSDT',
                  price: 1.5,
                  high_24h: 1.8,
                  low_24h: 1.2,
                  price_chg_24h: 2.0,
                  market_cap_usd: 15000000,
                  volume_24h_usd: 30000000,
                  turnover_ratio: 2.0,
                  funding_interval_hours: 4,
                  funding_rate_pct: 0.01,
                  funding_rate_4h_pct: 0.01,
                  ema5: 1.52,
                  ema10: 1.48,
                  ema_spread_pct: 2.7,
                  is_ema_bull: true,
                  is_high_turnover: true,
                  is_neg_funding: false,
                  is_resonance: true,
                };
                R.renderTableRows(tbody, [cand], rowRefs, 1.5);
                assert.strictEqual(rowQuerySelectorCalls, 0);
                const refs = rowRefs.get('AAAUSDT');
                assert.strictEqual(refs.priceEl, priceEl);
                assert.strictEqual(refs.mcValEl, mcValEl);
                R.syncRowDom(cand, refs, {});
                assert.strictEqual(priceEl._txtWrites, 0, 'Unchanged price should skip DOM text write');
              }

              // 3b. kline-modal batched Canvas strokes
              let strokeCalls = 0;
              let fillRectCalls = 0;
              const baseCtx = {
                save() {},
                restore() {},
                clearRect() {},
                drawImage() {},
                setTransform() {},
                fillRect() { fillRectCalls += 1; },
                strokeRect() {},
                beginPath() {},
                moveTo() {},
                lineTo() {},
                stroke() { strokeCalls += 1; },
                fillText() {},
                setLineDash() {},
                measureText: () => ({ width: 30 }),
                strokeStyle: '',
                fillStyle: '',
                lineWidth: 1,
                font: '',
                textAlign: '',
              };
              const els = {};
              const makeSimpleEl = (id) => ({
                id,
                textContent: '',
                innerHTML: '',
                title: '',
                style: {},
                className: '',
                classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
                setAttribute() {},
                removeAttribute() {},
                getAttribute() { return null; },
                showModal() { this.open = true; },
                close() { this.open = false; },
                addEventListener() {},
                querySelector() { return null; },
                querySelectorAll() { return []; },
                clientWidth: 800,
                clientHeight: 500,
              });
              const modalCanvas = makeSimpleEl('modal-kline-canvas');
              modalCanvas.getContext = () => baseCtx;
              els['modal-kline-canvas'] = modalCanvas;
              els['modal-native-chart-view'] = makeSimpleEl('modal-native-chart-view');
              const win = {
                devicePixelRatio: 1,
                document: {
                  hidden: false,
                  readyState: 'complete',
                  getElementById: (id) => (els[id] ||= makeSimpleEl(id)),
                  querySelector: () => null,
                  querySelectorAll: () => [],
                  createElement: (tag) => {
                    const el = makeSimpleEl(tag);
                    el.getContext = () => baseCtx;
                    return el;
                  },
                  addEventListener() {},
                },
                fetch: async (url) => {
                  if (url.startsWith('api/binance/klines')) {
                    const candles = Array.from({ length: 120 }, (_, i) => [
                      1795200000000 + i * 900000,
                      String(10 + (i % 2)),
                      String(12),
                      String(9),
                      String(11 - (i % 2)),
                      '1000',
                    ]);
                    return { ok: true, status: 200, headers: { get: () => null }, text: async () => JSON.stringify(candles) };
                  }
                  return { ok: true, status: 200, headers: { get: () => null }, text: async () => JSON.stringify({ tradingview_layout_id: 'ig0LuDL8' }) };
                },
                AbortController,
                DOMException,
                requestAnimationFrame: (fn) => { fn(); return 1; },
                cancelAnimationFrame() {},
                setTimeout,
                clearTimeout,
                addEventListener() {},
                console: { error() {}, warn() {} },
              };
              win.window = win;
              vm.runInNewContext(commonCode, win);
              win.SecondLegCommon.setTvLayoutId('ig0LuDL8');
              vm.runInNewContext(modalCode, win);
              win.SecondLegKlineModal.open('BTCUSDT', 'BINANCE:BTCUSDT.P');
              await new Promise((r) => setTimeout(r, 30));
              // 120 candles: 1 grid/axis stroke + 2 batched wick strokes + 2 EMA strokes + 1 last-price stroke = 6 strokes total (down from 120+)
              assert.ok(strokeCalls <= 8, `Expected batched strokes <= 8 for 120 candles, got ${strokeCalls}`);
              assert.strictEqual(fillRectCalls, 120 * 2 + 2);
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

    def test_direction_a_workflow_interactions_and_overlays_in_node_vm(self):
        node_bin = shutil.which("node")
        self.assertIsNotNone(node_bin, "node is required to test Direction A interactions")

        script = textwrap.dedent(
            """
            const assert = require('assert');
            const fs = require('fs');
            const path = require('path');
            const vm = require('vm');

            const root = process.cwd();
            const commonCode = fs.readFileSync(path.join(root, 'web/common.js'), 'utf8');
            const candRenderCode = fs.readFileSync(path.join(root, 'web/candidates-render.js'), 'utf8');
            const acctTablesCode = fs.readFileSync(path.join(root, 'web/account-tables.js'), 'utf8');
            const modalCode = fs.readFileSync(path.join(root, 'web/kline-modal.js'), 'utf8');

            // 1. candidates-render filter chips, newly entered resonance detection, and row selection sync
            {
              const win = { document: { querySelector: () => null, querySelectorAll: () => [] }, console: { error() {}, warn() {} } };
              win.window = win;
              vm.runInNewContext(commonCode, win);
              win.SecondLegCommon.setTvLayoutId('ig0LuDL8');
              vm.runInNewContext(candRenderCode, win);
              const R = win.SecondLegCandidatesRender;

              const freshRes = { symbol: 'FRESHUSDT', is_resonance: true, is_neg_funding: false, is_high_turnover: true, is_ema_bull: true, resonance_duration_hours: 1.5, funding_rate_4h_pct: 0.01 };
              const staleNeg = { symbol: 'STALEUSDT', is_resonance: true, is_neg_funding: true, is_high_turnover: true, is_ema_bull: true, resonance_duration_hours: 6.2, funding_rate_4h_pct: -0.05 };
              assert.strictEqual(R.matchesCandidateFilter(freshRes, { currentZone: 'resonance', onlyFreshResonance: true, onlyHealthyFunding: true, searchQuery: '' }), true);
              assert.strictEqual(R.matchesCandidateFilter(staleNeg, { currentZone: 'resonance', onlyFreshResonance: true, onlyHealthyFunding: false, searchQuery: '' }), false);
              assert.strictEqual(R.matchesCandidateFilter(staleNeg, { currentZone: 'resonance', onlyFreshResonance: false, onlyHealthyFunding: true, searchQuery: '' }), false);

              const first = R.collectNewlyEnteredResonance([staleNeg], new Set(), false);
              assert.strictEqual(first.entered.length, 0);
              const second = R.collectNewlyEnteredResonance([staleNeg, freshRes], first.currentSet, true);
              assert.deepStrictEqual(Array.from(second.entered), ['FRESHUSDT']);

              const attrs = new Map();
              let scrolled = false;
              const makeRow = (sym, hidden = false) => ({
                style: { display: hidden ? 'none' : '' },
                setAttribute(k, v) { attrs.set(`${sym}:${k}`, v); },
                removeAttribute(k) { attrs.delete(`${sym}:${k}`); },
                hasAttribute(k) { return attrs.has(`${sym}:${k}`); },
                scrollIntoView() { scrolled = true; },
              });
              const refsMap = new Map([
                ['FRESHUSDT', { rowEl: makeRow('FRESHUSDT') }],
                ['STALEUSDT', { rowEl: makeRow('STALEUSDT') }],
              ]);
              R.syncRowSelection(refsMap, 'FRESHUSDT', { scroll: true });
              assert.strictEqual(attrs.get('FRESHUSDT:data-selected'), 'true');
              assert.strictEqual(scrolled, true);
              R.syncRowSelection(refsMap, 'STALEUSDT', {});
              assert.strictEqual(attrs.has('FRESHUSDT:data-selected'), false);
              assert.strictEqual(attrs.get('STALEUSDT:aria-selected'), 'true');
            }

            // 2. account-tables side filter, filtered summary stats, active position kline button, and modal context forwarding
            {
              const els = {
                'history-search-input': { value: '' },
                'history-side-filter': { value: 'LONG' },
                'history-outcome-filter': { value: '' },
                'history-count-badge': { textContent: '' },
                'history-filtered-summary': { hidden: true, textContent: '', className: '', classList: { add() {}, remove() {}, toggle() {} } },
                'history-tbody': { innerHTML: '' },
                'active-positions-tbody': { innerHTML: '' },
                'active-pos-count-badge': { textContent: '' },
              };
              const win = { document: { getElementById: (id) => els[id] || null, querySelector: () => null, querySelectorAll: () => [] }, console: { error() {}, warn() {} } };
              win.window = win;
              vm.runInNewContext(commonCode, win);
              win.SecondLegCommon.setTvLayoutId('ig0LuDL8');
              vm.runInNewContext(acctTablesCode, win);
              const T = win.SecondLegAccountTables;

              const accountData = {
                active_positions: [{ symbol: 'AAAUSDT', side: 'LONG', risk_monitored: true, entry_price: 1.0, mark_price: 1.08, leverage: 5, notional: 500, amount: 500, unrealized_pnl: 40, roe_pct: 40, liquidation_price: 0.8 }],
                trade_history: [
                  { symbol: 'AAAUSDT', side: 'LONG', outcome: 'WIN', net_pnl: 120, pnl_pct: 12, closed_ts: 200 },
                  { symbol: 'BBBUSDT', side: 'LONG', outcome: 'LOSS', net_pnl: -20, pnl_pct: -4, closed_ts: 150 },
                  { symbol: 'CCCUSDT', side: 'SHORT', outcome: 'WIN', net_pnl: 300, pnl_pct: 20, closed_ts: 100 },
                ],
              };
              T.prepareHistorySearchKeys(accountData.trade_history);
              T.renderActivePositions(accountData.active_positions, (id) => els[id] || null);
              assert.ok(els['active-positions-tbody'].innerHTML.includes('data-kline-kind="position"'));

              T.renderHistoryTable(accountData, { column: 'closed_ts', asc: false }, 1, { getEl: (id) => els[id] || null });
              assert.strictEqual(T.getLastHistoryPageRows().length, 2);
              assert.strictEqual(els['history-filtered-summary'].hidden, false);
              assert.ok(els['history-filtered-summary'].textContent.includes('+$100.00'));
              assert.ok(els['history-filtered-summary'].textContent.includes('50.0%'));

              els['history-side-filter'].value = '';
              T.renderHistoryTable(accountData, { column: 'closed_ts', asc: false }, 1, { getEl: (id) => els[id] || null });
              assert.strictEqual(els['history-filtered-summary'].hidden, true);

              let modalCall = null;
              T.openAccountKlineModal({ open: (sym, tv, trig, ctx) => { modalCall = { sym, tv, ctx }; } }, accountData, 'position', 0, null);
              assert.strictEqual(modalCall.sym, 'AAAUSDT');
              assert.strictEqual(modalCall.ctx.type, 'position');
            }

            // 3. kline-modal lazy dialog injection, context HUD, trendline/entry overlays, and hotkeys
            (async () => {
              const els = {};
              const makeEl = (tag) => ({
                id: '',
                tagName: (tag || 'DIV').toUpperCase(),
                textContent: '',
                innerHTML: '',
                title: '',
                style: {},
                dataset: {},
                className: '',
                _listeners: {},
                classList: {
                  _set: new Set(),
                  add(...cs) { cs.forEach((c) => this._set.add(c)); },
                  remove(...cs) { cs.forEach((c) => this._set.delete(c)); },
                  toggle(c, f) { if (f === undefined) { this._set.has(c) ? this._set.delete(c) : this._set.add(c); } else if (f) { this._set.add(c); } else { this._set.delete(c); } },
                  contains(c) { return this._set.has(c); },
                },
                setAttribute() {},
                removeAttribute() {},
                getAttribute() { return null; },
                showModal() { this.open = true; },
                close() { this.open = false; },
                focus() {},
                addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); },
                querySelector() { return null; },
                querySelectorAll() { return []; },
                clientWidth: 800,
                clientHeight: 500,
              });
              let dashedCalls = 0;
              const texts = [];
              const ctx = {
                save() {},
                restore() {},
                clearRect() {},
                drawImage() {},
                setTransform() {},
                fillRect() {},
                strokeRect() {},
                beginPath() {},
                moveTo() {},
                lineTo() {},
                stroke() {},
                fillText(t) { texts.push(t); },
                setLineDash(arr) { if (Array.isArray(arr) && arr.length > 0) dashedCalls += 1; },
                measureText: () => ({ width: 30 }),
              };
              const idsToCreate = [
                ['chart-modal', 'dialog'], ['modal-header-toolbar', 'div'], ['modal-close-btn', 'button'],
                ['modal-copy-btn', 'button'], ['modal-nav-group', 'div'], ['modal-prev-btn', 'button'],
                ['modal-next-btn', 'button'], ['modal-nav-pos', 'span'], ['modal-chart-title', 'h3'],
                ['modal-chart-price', 'span'], ['modal-external-link', 'a'], ['modal-hud-ema5', 'span'],
                ['modal-hud-ema10', 'span'], ['modal-hud-ema-spread', 'span'], ['modal-hud-context', 'div'],
                ['modal-native-chart-view', 'div'], ['modal-tv-view', 'div'], ['modal-tv-iframe', 'iframe'],
                ['modal-tv-loading', 'div'], ['modal-tv-fallback', 'div'], ['modal-tv-fallback-text', 'span'],
                ['modal-tv-switch-native-btn', 'button'], ['modal-tv-open-ext-btn', 'a'], ['modal-kline-canvas', 'canvas'],
                ['modal-chart-tooltip', 'div'], ['modal-chart-loading', 'div'], ['modal-chart-error', 'div'],
                ['modal-chart-error-text', 'span'], ['modal-chart-retry-btn', 'button'],
                ['btn-chart-engine-native', 'button'], ['btn-chart-engine-tv', 'button'], ['modal-bars-group', 'div'],
              ];
              const bodyChildren = [];
              const win = {
                devicePixelRatio: 1,
                document: {
                  hidden: false,
                  readyState: 'complete',
                  activeElement: null,
                  body: {
                    appendChild(node) { bodyChildren.push(node); },
                  },
                  getElementById: (id) => els[id] || null,
                  querySelector: () => null,
                  querySelectorAll: () => [],
                  createElement: (tag) => {
                    const el = makeEl(tag);
                    el.getContext = () => ctx;
                    let _html = '';
                    Object.defineProperty(el, 'innerHTML', {
                      get() { return _html; },
                      set(v) {
                        _html = String(v);
                        if (_html.includes('id="chart-modal"')) {
                          idsToCreate.forEach(([id, t]) => {
                            const node = makeEl(t);
                            node.id = id;
                            if (t === 'canvas') node.getContext = () => ctx;
                            if (id === 'modal-tv-view') node.classList.add('hidden');
                            els[id] = node;
                          });
                          el.firstElementChild = els['chart-modal'];
                        }
                      },
                    });
                    return el;
                  },
                  addEventListener() {},
                },
                fetch: async (url) => {
                  if (url.startsWith('api/binance/klines')) {
                    const baseMs = 1795200000000;
                    const candles = Array.from({ length: 80 }, (_, i) => [
                      baseMs + i * 900000,
                      String(10 + (i % 2)),
                      String(12),
                      String(9),
                      String(11 - (i % 2)),
                      '1000',
                    ]);
                    return { ok: true, status: 200, headers: { get: () => null }, text: async () => JSON.stringify(candles) };
                  }
                  return { ok: true, status: 200, headers: { get: () => null }, text: async () => JSON.stringify({ tradingview_layout_id: 'ig0LuDL8' }) };
                },
                AbortController,
                DOMException,
                requestAnimationFrame: (fn) => { fn(); return 1; },
                cancelAnimationFrame() {},
                setTimeout,
                clearTimeout,
                addEventListener() {},
                console: { error() {}, warn() {} },
              };
              win.window = win;
              vm.runInNewContext(commonCode, win);
              win.SecondLegCommon.setTvLayoutId('ig0LuDL8');
              vm.runInNewContext(modalCode, win);

              win.SecondLegKlineModal.open('AAAUSDT', 'BINANCE:AAAUSDT.P', null, {
                type: 'position',
                position: {
                  side: 'LONG',
                  entry_price: 10.2,
                  roe_pct: 6.5,
                  trailing_stop_triggered: true,
                  trailing_stop_price: 10.35,
                  trendline: { points: [{ timestamp: 1795200000 + 20 * 900, price: 11.8 }, { timestamp: 1795200000 + 60 * 900, price: 10.8 }] },
                },
              });
              await new Promise((r) => setTimeout(r, 40));
              assert.ok(bodyChildren.length === 1 && els['chart-modal'] && els['chart-modal'].open === true);
              assert.ok(els['modal-hud-context'].innerHTML.includes('LONG'));
              assert.ok(els['modal-hud-context'].innerHTML.includes('开仓'));
              assert.ok(els['modal-hud-context'].innerHTML.includes('保本线'));
              assert.ok(els['modal-hud-ema5'].textContent.startsWith('EMA5 '));
              const hiLbl = win.SecondLegCommon.formatCryptoPrice(12);
              const loLbl = win.SecondLegCommon.formatCryptoPrice(9);
              assert.ok(texts.includes(hiLbl) && texts.includes(loLbl), `Expected visible-window High/Low micro-labels (${hiLbl}, ${loLbl}) in ${JSON.stringify(texts)}`);
              assert.ok(texts.some((t) => typeof t === 'string' && t.startsWith('开仓 ')));
              assert.ok(texts.some((t) => typeof t === 'string' && t.startsWith('保本 ')));
              assert.ok(dashedCalls >= 2, 'Expected grid + entry/trailing reference dashed lines');

              // Hotkey 't' toggles TV iframe engine view
              const modalKeyListeners = els['chart-modal']._listeners.keydown || [];
              assert.ok(modalKeyListeners.length >= 1);
              let prevented = false;
              modalKeyListeners.forEach((fn) => fn({ key: 't', ctrlKey: false, metaKey: false, altKey: false, preventDefault() { prevented = true; } }));
              assert.strictEqual(prevented, true);
              assert.strictEqual(els['modal-native-chart-view'].classList.contains('hidden'), true);
              assert.strictEqual(els['modal-tv-view'].classList.contains('hidden'), false);
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
