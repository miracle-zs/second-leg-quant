#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for candidates & kline-modal P1 fixes."""

from pathlib import Path
import re
import shutil
import subprocess
import textwrap
import unittest


ROOT_DIR = Path(__file__).resolve().parent.parent


class TestCandidatesAndModalP1Fixes(unittest.TestCase):
    def test_static_html_and_js_p1_contracts(self):
        index_html = (ROOT_DIR / "web/index.html").read_text(encoding="utf-8")
        cand_js = (
            (ROOT_DIR / "web/candidates-render.js").read_text(encoding="utf-8")
            + "\n"
            + (ROOT_DIR / "web/candidates.js").read_text(encoding="utf-8")
        )
        modal_js = (ROOT_DIR / "web/kline-modal.js").read_text(encoding="utf-8")

        # #18: Remove focus:outline-none from search input
        search_match = re.search(r'<input\b[^>]*id="symbol-search-input"[^>]*>', index_html)
        self.assertIsNotNone(search_match)
        self.assertNotIn("focus:outline-none", search_match.group(0))

        # #19: Modal stage uses flex-1 + min-h-0 (not h-full); table scroll has no fixed max-h double-scroll trap
        stage_match = re.search(r'<div\b[^>]*id="modal-chart-stage"[^>]*>', index_html)
        self.assertIsNotNone(stage_match)
        self.assertIn("flex-1", stage_match.group(0))
        self.assertIn("min-h-0", stage_match.group(0))
        self.assertNotIn("h-full", stage_match.group(0))
        self.assertNotIn("max-h-[65vh]", index_html)
        self.assertNotIn("lg:max-h-[calc(100vh-225px)]", index_html)

        # #15: #modal-chart-error has flex class; #modal-tv-view has loading & fallback controls
        err_match = re.search(r'<div\b[^>]*id="modal-chart-error"[^>]*>', index_html)
        self.assertIsNotNone(err_match)
        self.assertIn("flex", err_match.group(0).split('class="')[1].split('"')[0].split())
        self.assertIn('id="modal-tv-loading"', index_html)
        self.assertIn('id="modal-tv-fallback"', index_html)
        self.assertIn('id="modal-tv-switch-native-btn"', index_html)
        self.assertIn('id="modal-tv-open-ext-btn"', index_html)

        # #19: Sticky rank column (#) at left 0 and symbol column offset past #
        self.assertIn('aria-label="序号"', index_html)
        self.assertIn("inset-inline-start: 2.25rem", index_html)
        self.assertIn("inset-inline-start: 2.25rem", cand_js)

        # #20: Primary cobalt CTA, 5 zone progress bars, kpi-zone-title, and font-sans for Chinese text
        copy_tv_match = re.search(r'<button\b[^>]*id="btn-batch-copy-tv"[^>]*>', index_html)
        ext_tv_match = re.search(r'<a\b[^>]*id="modal-external-link"[^>]*>', index_html)
        self.assertIsNotNone(copy_tv_match)
        self.assertIsNotNone(ext_tv_match)
        self.assertIn("btn-primary-cobalt", copy_tv_match.group(0))
        self.assertIn("btn-primary-cobalt", ext_tv_match.group(0))

        self.assertEqual(index_html.count("zone-progress-track"), 5)
        self.assertEqual(index_html.count("zone-progress-fill"), 5)
        self.assertEqual(index_html.count("kpi-zone-title"), 5)
        zone1_match = re.search(r'data-zone="resonance"[\s\S]*?<span class="([^"]+)">顺势高换手共振区</span>', index_html)
        self.assertIsNotNone(zone1_match)
        self.assertNotIn("text-[#f5c451]", zone1_match.group(1))

        # #17: Canvas ARIA + button aria-pressed + separated pool/ticker timestamps
        canvas_match = re.search(r'<canvas\b[^>]*id="modal-kline-canvas"[^>]*>', index_html)
        self.assertIsNotNone(canvas_match)
        self.assertIn('role="img"', canvas_match.group(0))
        self.assertIn('aria-label="', canvas_match.group(0))
        self.assertIn('id="ticker-synced-time"', index_html)
        self.assertIn('aria-pressed="true"', index_html)

        # #9/#10/#15: CSS flash classes and touchend in JS
        self.assertIn("price-flash-up", cand_js)
        self.assertIn("price-flash-down", cand_js)
        self.assertIn("touchend", modal_js)

        # Ensure no uncompiled Tailwind utility classes are introduced
        for bad_cls in ("pt-2.5", "pb-2", "bg-[#0b0e13]/85", "sm:py-2", "sm:gap-1.5", "sm:px-1.5"):
            self.assertNotIn(bad_cls, index_html)
        for bad_cls in ("min-w-0", "mt-3"):
            self.assertNotIn(bad_cls, cand_js)

    def test_runtime_p1_fixes_in_node_vm(self):
        node_bin = shutil.which("node")
        self.assertIsNotNone(node_bin, "node is required to verify frontend P1 runtime behavior")

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

            function makeClassList(initial = []) {
              const set = new Set(initial);
              return {
                _set: set,
                add(...cls) { cls.forEach((c) => set.add(c)); },
                remove(...cls) { cls.forEach((c) => set.delete(c)); },
                toggle(c, force) {
                  if (force === undefined) {
                    if (set.has(c)) { set.delete(c); return false; }
                    set.add(c); return true;
                  }
                  if (force) set.add(c);
                  else set.delete(c);
                  return Boolean(force);
                },
                contains(c) { return set.has(c); },
              };
            }

            function makeEl(id, { tagName = 'DIV', attrs = {}, classes = [], isContentEditable = false } = {}) {
              const attrMap = { ...attrs };
              const listeners = {};
              const el = {
                id,
                tagName,
                textContent: '',
                innerHTML: '',
                title: '',
                value: '',
                href: '',
                src: '',
                referrerPolicy: '',
                hidden: false,
                open: false,
                disabled: false,
                isConnected: true,
                isContentEditable,
                clientWidth: 600,
                clientHeight: 400,
                dataset: {},
                style: {},
                classList: makeClassList(classes),
                get className() { return [...el.classList._set].join(' '); },
                set className(v) {
                  el.classList._set.clear();
                  String(v || '').split(/\\s+/).filter(Boolean).forEach((c) => el.classList._set.add(c));
                },
                setAttribute(k, v) { attrMap[k] = String(v); },
                getAttribute(k) { return Object.prototype.hasOwnProperty.call(attrMap, k) ? attrMap[k] : null; },
                removeAttribute(k) { delete attrMap[k]; },
                hasAttribute(k) { return Object.prototype.hasOwnProperty.call(attrMap, k); },
                addEventListener(ev, fn) { (listeners[ev] ||= []).push(fn); },
                removeEventListener(ev, fn) {
                  if (!listeners[ev]) return;
                  listeners[ev] = listeners[ev].filter((f) => f !== fn);
                },
                dispatch(ev, eventObj = {}) {
                  (listeners[ev] || []).forEach((fn) => fn(eventObj));
                },
                getBoundingClientRect() { return { left: 0, top: 0, width: 600, height: 400 }; },
                closest() { return null; },
                querySelector() { return null; },
                querySelectorAll() { return []; },
                focus() {},
                blur() {},
                select() {},
                setSelectionRange() {},
                click() {},
                append() {},
                appendChild() {},
                removeChild() {},
                replaceChildren() {},
              };
              return el;
            }

            function makeCandidate(sym, overrides = {}) {
              return {
                symbol: sym,
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
                ...overrides,
              };
            }

            (async () => {
              // --------------------------------------------------------------
              // Test 1: kline-modal.js P1 stability, touch cleanup, TV fallback & a11y
              // --------------------------------------------------------------
              {
                const els = {};
                const getEl = (id) => (els[id] ||= makeEl(id));
                const docListeners = {};
                let activeEl = null;

                const chartModal = makeEl('chart-modal', { tagName: 'DIALOG' });
                chartModal.showModal = () => { chartModal.open = true; };
                chartModal.close = () => { chartModal.open = false; chartModal.dispatch('close', {}); };
                els['chart-modal'] = chartModal;

                const nativeView = getEl('modal-native-chart-view');
                const tvView = getEl('modal-tv-view');
                const tvIframe = getEl('modal-tv-iframe');
                const tvLoading = getEl('modal-tv-loading');
                const tvFallback = getEl('modal-tv-fallback');
                const tvSwitchNativeBtn = getEl('modal-tv-switch-native-btn');
                const tvOpenExtBtn = getEl('modal-tv-open-ext-btn');
                const modalError = getEl('modal-chart-error');
                const modalErrorText = getEl('modal-chart-error-text');
                modalError.classList.add('hidden');
                const modalCanvas = getEl('modal-kline-canvas');
                let ctxDraws = 0;
                modalCanvas.getContext = () => ({
                  save() {}, restore() {}, setTransform() {}, clearRect() {},
                  fillRect() {}, strokeRect() {}, beginPath() {}, moveTo() {}, lineTo() {},
                  stroke() {}, fillText() {}, setLineDash() {}, drawImage() { ctxDraws += 1; },
                  measureText: (t) => ({ width: String(t).length * 6 }),
                });

                const interval15 = makeEl('int-15m', { tagName: 'BUTTON', attrs: { 'data-interval': '15m', 'aria-pressed': 'true' } });
                const interval1h = makeEl('int-1h', { tagName: 'BUTTON', attrs: { 'data-interval': '1h', 'aria-pressed': 'false' } });
                const intervalBad = makeEl('int-bad', { tagName: 'BUTTON', attrs: { 'data-interval': '99m', 'aria-pressed': 'false' } });
                const bars60 = makeEl('bars-60', { tagName: 'BUTTON', attrs: { 'data-bars': '60', 'aria-pressed': 'false' } });
                const bars120 = makeEl('bars-120', { tagName: 'BUTTON', attrs: { 'data-bars': '120', 'aria-pressed': 'true' } });
                const btnNative = getEl('btn-chart-engine-native');
                const btnTv = getEl('btn-chart-engine-tv');
                const toolbarEl = getEl('modal-header-toolbar');

                let klineFetchCount = 0;
                const abortedFetches = [];
                let failKlines = false;
                let holdPendingKline = false;
                let pendingKlineResolve = null;

                const timers = [];
                const win = {
                  document: {
                    hidden: false,
                    readyState: 'complete',
                    body: makeEl('body'),
                    get activeElement() { return activeEl; },
                    getElementById: getEl,
                    querySelectorAll: (sel) => {
                      if (sel === '.modal-interval-btn') return [interval15, interval1h, intervalBad];
                      if (sel === '.modal-bars-btn') return [bars60, bars120];
                      return [];
                    },
                    createElement: (tag) => {
                      const c = makeEl(tag, { tagName: tag.toUpperCase() });
                      c.getContext = () => ({
                        setTransform() {}, fillRect() {}, strokeRect() {}, beginPath() {},
                        moveTo() {}, lineTo() {}, stroke() {}, fillText() {}, setLineDash() {},
                        measureText: () => ({ width: 30 }),
                      });
                      c.append = () => {};
                      c.replaceChildren = () => {};
                      return c;
                    },
                    addEventListener(ev, fn) { (docListeners[ev] ||= []).push(fn); },
                  },
                  fetch: (url, opts = {}) => {
                    if (url.startsWith('api/binance/klines')) {
                      klineFetchCount += 1;
                      if (failKlines) {
                        return Promise.resolve({ ok: false, status: 429, headers: { get: () => null }, text: async () => '' });
                      }
                      if (holdPendingKline) {
                        return new Promise((resolve, reject) => {
                          pendingKlineResolve = resolve;
                          if (opts.signal) {
                            opts.signal.addEventListener('abort', () => {
                              abortedFetches.push(url);
                              reject(new DOMException('Aborted', 'AbortError'));
                            }, { once: true });
                          }
                        });
                      }
                      return Promise.resolve({
                        ok: true,
                        status: 200,
                        headers: { get: () => null },
                        text: async () => JSON.stringify([
                          [1795200000000, '10', '12', '9', '11', '100'],
                          [1795200900000, '11', '13', '10', '12', '120'],
                        ]),
                      });
                    }
                    return Promise.resolve({ ok: true, status: 200, headers: { get: () => null }, text: async () => '{}' });
                  },
                  AbortController,
                  DOMException,
                  setTimeout: (fn, ms) => {
                    const id = setTimeout(fn, ms);
                    timers.push({ fn, ms, id });
                    return id;
                  },
                  clearTimeout,
                  requestAnimationFrame: (fn) => { fn(); return 1; },
                  cancelAnimationFrame() {},
                  addEventListener() {},
                  console: { warn() {}, error() {} },
                };
                win.window = win;
                vm.runInNewContext(commonCode, win);
                win.SecondLegCommon.setTvLayoutId('ig0LuDL8');
                vm.runInNewContext(klineModalCode, win);

                let navCalls = 0;
                win.SecondLegKlineModal.init({
                  visibleBars: 120,
                  onNavigateSymbol: () => { navCalls += 1; },
                });

                // Open modal with 1 symbol -> ArrowRight must NOT preventDefault or navigate
                win.SecondLegKlineModal.setNavMeta(0, 1);
                win.SecondLegKlineModal.open('BTCUSDT', 'BINANCE:BTCUSDT.P');
                await new Promise((r) => setTimeout(r, 10));
                assert.strictEqual(klineFetchCount, 1);
                assert.ok(modalCanvas.getAttribute('aria-label').includes('BTCUSDT 15m'));

                let prevented = false;
                chartModal.dispatch('keydown', { key: 'ArrowRight', preventDefault() { prevented = true; } });
                assert.strictEqual(prevented, false);
                assert.strictEqual(navCalls, 0);

                // With 3 symbols, focus inside toolbar group -> ArrowRight must NOT preventDefault
                win.SecondLegKlineModal.setNavMeta(0, 3);
                activeEl = interval15;
                interval15.closest = (sel) => (sel === '[role="group"]' ? {} : null);
                chartModal.dispatch('keydown', { key: 'ArrowRight', preventDefault() { prevented = true; } });
                assert.strictEqual(prevented, false);
                assert.strictEqual(navCalls, 0);

                // Focus outside toolbar -> ArrowRight navigates
                activeEl = win.document.body;
                chartModal.dispatch('keydown', { key: 'ArrowRight', preventDefault() { prevented = true; } });
                assert.strictEqual(prevented, true);
                assert.strictEqual(navCalls, 1);

                // Invalid interval shows real error in #modal-chart-error-text and keeps 15m active
                intervalBad.dispatch('click', {});
                assert.strictEqual(modalError.classList.contains('hidden'), false);
                assert.ok(modalErrorText.textContent.includes('99m'));
                assert.strictEqual(interval15.getAttribute('aria-pressed'), 'true');
                assert.strictEqual(intervalBad.getAttribute('aria-pressed'), 'false');

                // Clicking already-active 15m / 120 / native is a no-op
                const beforeNoop = klineFetchCount;
                interval15.dispatch('click', {});
                bars120.dispatch('click', {});
                btnNative.dispatch('click', {});
                assert.strictEqual(klineFetchCount, beforeNoop);

                // Touchstart sets hover, touchend clears hover & hides tooltip
                const tooltipEl = getEl('modal-chart-tooltip');
                nativeView.dispatch('touchstart', { touches: [{ clientX: 100 }] });
                assert.strictEqual(tooltipEl.classList.contains('hidden'), false);
                nativeView.dispatch('touchend', {});
                assert.strictEqual(tooltipEl.classList.contains('hidden'), true);

                // Failed klines shows actual HTTP 429 message
                failKlines = true;
                interval1h.dispatch('click', {});
                await new Promise((r) => setTimeout(r, 10));
                assert.strictEqual(modalError.classList.contains('hidden'), false);
                assert.ok(modalErrorText.textContent.includes('HTTP 429'));
                assert.strictEqual(interval1h.getAttribute('aria-pressed'), 'true');
                assert.strictEqual(interval15.getAttribute('aria-pressed'), 'false');

                // Switching to TV aborts in-flight kline fetch, shows loading, and handles error -> switch back to native
                failKlines = false;
                holdPendingKline = true;
                win.SecondLegKlineModal.open('ETHUSDT', 'BINANCE:ETHUSDT.P');
                btnTv.dispatch('click', {});
                assert.strictEqual(abortedFetches.length, 1);
                assert.strictEqual(btnTv.getAttribute('aria-pressed'), 'true');
                assert.strictEqual(tvLoading.classList.contains('hidden'), false);
                assert.ok(tvOpenExtBtn.href.includes('ETHUSDT'));

                // Clicking already-active TV does not reload iframe src
                const prevSrc = tvIframe.src;
                tvIframe.src = 'https://s.tradingview.com/widgetembed/?kept=1';
                btnTv.dispatch('click', {});
                assert.strictEqual(tvIframe.src, 'https://s.tradingview.com/widgetembed/?kept=1');
                tvIframe.src = prevSrc;

                // TV error reveals fallback, switch native button returns to native engine
                tvIframe.dispatch('error', {});
                assert.strictEqual(tvFallback.classList.contains('hidden'), false);
                holdPendingKline = false;
                tvSwitchNativeBtn.dispatch('click', {});
                await new Promise((r) => setTimeout(r, 10));
                assert.strictEqual(btnNative.getAttribute('aria-pressed'), 'true');
                assert.strictEqual(tvView.classList.contains('hidden'), true);
                assert.strictEqual(nativeView.classList.contains('hidden'), false);

                win.SecondLegKlineModal.close();
              }

              // --------------------------------------------------------------
              // Test 2: candidates.js P1 perf, polling races, config, fallback copy, UX & a11y
              // --------------------------------------------------------------
              {
                let fakeNow = 100000;
                class FakeDate extends Date {
                  static now() { return fakeNow; }
                }

                const els = {};
                const docListeners = {};
                let activeElementRef = null;
                const rowEls = new Map();
                const createdTempEls = [];
                let execCommandCalls = 0;
                let appendChildMoves = 0;
                let revokedUrls = [];
                let setIntervalCalls = 0;
                let versionRequestCount = 0;
                let versionShouldFail = false;
                let candidatesShouldFail = false;
                let snapshotVersion = '2026-11-26T10:00:00+08:00';
                let holdVersion = false;
                let resolvePendingVer = null;

                const makeCandEl = (id, opts = {}) => {
                  const e = makeEl(id, opts);
                  return e;
                };

                const tableBody = makeCandEl('candidate-table-body');
                Object.defineProperty(tableBody, 'innerHTML', {
                  get() { return this._html || ''; },
                  set(html) {
                    this._html = html;
                    rowEls.clear();
                    const matches = [...html.matchAll(/data-sym="([^"]+)"/g)];
                    const seen = new Set();
                    for (const m of matches) {
                      const sym = m[1];
                      if (seen.has(sym)) continue;
                      seen.add(sym);
                      const rankEl = makeCandEl(`rank-${sym}`);
                      const priceEl = makeCandEl(`price-${sym}`);
                      priceEl.animate = () => { throw new Error('WAAPI animate() should not be called when classList exists'); };
                      const resBadgeEl = makeCandEl(`res-${sym}`);
                      const fr4hEl = makeCandEl(`fr4h-${sym}`);
                      const frSettleEl = makeCandEl(`frsettle-${sym}`);
                      const subFallback = makeCandEl(`sub-${sym}`);
                      const row = makeCandEl(`row-${sym}`, { tagName: 'TR' });
                      row.setAttribute('data-sym', sym);
                      row.setAttribute('data-tv-sym', `BINANCE:${sym}.P`);
                      row.querySelector = (sel) => {
                        if (sel === '.sym-rank') return rankEl;
                        if (sel === '.sym-price') return priceEl;
                        if (sel === '.sym-res-badge') return resBadgeEl;
                        if (sel === '.sym-fr-4h') return fr4hEl;
                        if (sel === '.sym-fr-settle') return frSettleEl;
                        return subFallback;
                      };
                      rowEls.set(sym, { row, rankEl, priceEl, resBadgeEl, fr4hEl, frSettleEl });
                    }
                  },
                });
                tableBody.querySelectorAll = (sel) => (sel === 'tr.candidate-row' ? [...rowEls.values()].map((r) => r.row) : []);
                tableBody.appendChild = () => { appendChildMoves += 1; };
                els['candidate-table-body'] = tableBody;

                const sortThPrice = makeCandEl('th-price', { tagName: 'TH', attrs: { 'data-sort': 'price' } });
                sortThPrice.textContent = '最新价';
                const sortBtnPrice = makeCandEl('btn-sort-price', { tagName: 'BUTTON', attrs: { command: '--sort' } });
                sortThPrice.querySelector = (sel) => (sel === 'button[command="--sort"]' ? sortBtnPrice : null);

                const kpiRes = makeCandEl('kpi-card-res', { attrs: { 'data-zone': 'resonance' } });
                const kpiAll = makeCandEl('kpi-card-all', { attrs: { 'data-zone': 'all' } });

                const toggleDrawerBtn = getElFn('btn-toggle-drawer');
                let drawerFocusCount = 0;
                toggleDrawerBtn.focus = () => { drawerFocusCount += 1; activeElementRef = toggleDrawerBtn; };
                const tvDrawer = getElFn('tv-drawer');
                tvDrawer.classList.add('hidden');

                function getElFn(id) {
                  if (!els[id]) els[id] = makeCandEl(id);
                  return els[id];
                }

                // Track aria-live #sync-status-badge DOM writes
                const syncBadge = getElFn('sync-status-badge');
                let syncBadgeWrites = 0;
                let syncBadgeHtml = '';
                Object.defineProperty(syncBadge, 'innerHTML', {
                  get() { return syncBadgeHtml; },
                  set(v) { syncBadgeWrites += 1; syncBadgeHtml = v; },
                });

                const snapshotList = [
                  makeCandidate('AAAUSDT', { price: 1.0, turnover_ratio: 1.8, is_high_turnover: true, is_resonance: true, funding_interval_hours: 4 }),
                  makeCandidate('BBBUSDT', { price: 2.0, turnover_ratio: 2.5, is_high_turnover: true, is_resonance: true, funding_interval_hours: null, funding_rate_4h_pct: 0.05 }),
                  makeCandidate('CCCUSDT', { price: 3.0, turnover_ratio: 2.2, is_ema_bull: false, is_high_turnover: true, is_resonance: false, ema_spread_pct: 0.2 }),
                  makeCandidate('DDDUSDT', { price: 4.0, turnover_ratio: 3.0, is_high_turnover: true, is_resonance: true, funding_interval_hours: 4 }),
                ];

                let tickerStep = 0;
                let candidatesPayload = { updated_at: snapshotVersion, candidates: snapshotList };

                const win = {
                  Date: FakeDate,
                  innerWidth: 1280,
                  innerHeight: 800,
                  navigator: {},
                  document: {
                    hidden: false,
                    readyState: 'complete',
                    body: makeCandEl('body'),
                    get activeElement() { return activeElementRef; },
                    getElementById: (id) => {
                      if (id === 'empty-state-title' || id === 'empty-state-hint' || id === 'btn-empty-reset-filter') {
                        return getElFn(id);
                      }
                      return getElFn(id);
                    },
                    querySelector: () => null,
                    querySelectorAll: (sel) => {
                      if (sel === '.kpi-card') return [kpiRes, kpiAll];
                      if (sel === 'th.sortable') return [sortThPrice];
                      return [];
                    },
                    createElement: (tag) => {
                      const el = makeCandEl(`tmp-${tag}`, { tagName: tag.toUpperCase() });
                      el.parentNode = { removeChild() {} };
                      createdTempEls.push(el);
                      return el;
                    },
                    createDocumentFragment: () => ({ appendChild() {} }),
                    execCommand: (cmd) => {
                      if (cmd === 'copy') { execCommandCalls += 1; return true; }
                      return false;
                    },
                    addEventListener(ev, fn) { (docListeners[ev] ||= []).push(fn); },
                  },
                  sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
                  URL: {
                    createObjectURL: () => 'blob:mock-1',
                    revokeObjectURL: (u) => { revokedUrls.push(u); },
                  },
                  Blob: function(parts) { this.parts = parts; },
                  fetch: (url) => {
                    if (url === 'api/config') {
                      return Promise.resolve({
                        ok: true,
                        status: 200,
                        headers: { get: () => null },
                        text: async () => JSON.stringify({ tradingview_layout_id: 'ig0LuDL8', high_turnover_threshold: 2.0 }),
                      });
                    }
                    if (url === 'api/candidates') {
                      if (candidatesShouldFail) {
                        return Promise.resolve({ ok: false, status: 500, headers: { get: () => null }, text: async () => '' });
                      }
                      return Promise.resolve({
                        ok: true,
                        status: 200,
                        headers: { get: () => '"etag-1"' },
                        text: async () => JSON.stringify(candidatesPayload),
                      });
                    }
                    if (url === 'api/candidate-version') {
                      versionRequestCount += 1;
                      if (versionShouldFail) {
                        return Promise.resolve({ ok: false, status: 502, headers: { get: () => null }, text: async () => '' });
                      }
                      if (holdVersion) {
                        return new Promise((res) => { resolvePendingVer = res; });
                      }
                      return Promise.resolve({
                        ok: true,
                        status: 200,
                        headers: { get: () => null },
                        text: async () => JSON.stringify({ updated_at: snapshotVersion }),
                      });
                    }
                    if (url === 'api/binance/ticker24hr') {
                      const pA = tickerStep === 0 ? '1.0' : '1.5';
                      return Promise.resolve({
                        ok: true,
                        status: 200,
                        headers: { get: () => null },
                        text: async () => JSON.stringify([
                          { symbol: 'AAAUSDT', lastPrice: pA, priceChangePercent: '2.0', highPrice: '1.2', lowPrice: '0.8', quoteVolume: '18000000' },
                          { symbol: 'BBBUSDT', lastPrice: '2.2', priceChangePercent: '3.0', highPrice: '2.4', lowPrice: '1.8', quoteVolume: '50000000' },
                          { symbol: 'CCCUSDT', lastPrice: '3.1', priceChangePercent: '1.0', highPrice: '3.2', lowPrice: '2.8', quoteVolume: '22000000' },
                          { symbol: 'DDDUSDT', lastPrice: '4.0', priceChangePercent: '5.0', highPrice: '4.2', lowPrice: '3.8', quoteVolume: '120000000' },
                        ]),
                      });
                    }
                    if (url === 'api/binance/premiumIndex') {
                      return Promise.resolve({
                        ok: true,
                        status: 200,
                        headers: { get: () => null },
                        text: async () => JSON.stringify([
                          { symbol: 'AAAUSDT', lastFundingRate: '0.0002' },
                          { symbol: 'BBBUSDT', lastFundingRate: '0.0003' },
                          { symbol: 'CCCUSDT', lastFundingRate: '-0.0001' },
                          { symbol: 'DDDUSDT', lastFundingRate: '0.0001' },
                        ]),
                      });
                    }
                    return Promise.resolve({ ok: true, status: 200, headers: { get: () => null }, text: async () => '{}' });
                  },
                  AbortController,
                  DOMException,
                  setTimeout,
                  clearTimeout,
                  setInterval: () => { setIntervalCalls += 1; return setIntervalCalls; },
                  clearInterval() {},
                  addEventListener() {},
                  matchMedia: () => ({ matches: false, addEventListener() {} }),
                  console: { warn() {}, error() {} },
                };
                win.window = win;

                // Prepare tableScrollEl bounding box for desktop max-height test
                const tableScroll = getElFn('candidates-table-scroll');
                tableScroll.getBoundingClientRect = () => ({ top: 200 });

                vm.runInNewContext(commonCode, win);
                if (typeof win.SecondLegCommon.getConfig !== 'function') {
                  const baseCommon = win.SecondLegCommon;
                  win.SecondLegCommon = Object.freeze({
                    ...baseCommon,
                    getConfig: () => ({ tradingview_layout_id: 'ig0LuDL8', high_turnover_threshold: 2.0 }),
                  });
                }
                vm.runInNewContext(klineModalCode, win);
                vm.runInNewContext(candRenderCode, win);
                const instrumentedCand = candCode.replace(
                  /\\}\\)\\(\\);\\s*$/,
                  'window.__candTest = { checkSnapshotVersion, loadCandidateSnapshot, syncBinancePool, applySortOrder, applyFilter, resetFiltersToDefault, getState: () => candidatesState };})();'
                );
                vm.runInNewContext(instrumentedCand, win);
                await new Promise((r) => setTimeout(r, 30));

                // Polling should NOT use setInterval for 60s/30s loops (only 1 setInterval for 1s countdown)
                assert.strictEqual(setIntervalCalls, 1);

                // Config high_turnover_threshold = 2.0 -> AAAUSDT (1.8x) is no longer high_turnover or resonance!
                const state = win.__candTest.getState();
                assert.strictEqual(state.find((c) => c.symbol === 'AAAUSDT').is_high_turnover, false);
                assert.strictEqual(state.find((c) => c.symbol === 'AAAUSDT').is_resonance, false);
                assert.strictEqual(rowEls.get('AAAUSDT').row.style.display, 'none');
                assert.strictEqual(rowEls.get('BBBUSDT').row.style.display, '');

                // applySortOrder skips hidden rows (only visible rows moved when sort changes DOM order)
                let movedSymbols = [];
                win.document.createDocumentFragment = () => ({
                  appendChild(node) {
                    if (node && typeof node.getAttribute === 'function') movedSymbols.push(node.getAttribute('data-sym'));
                  },
                });
                sortBtnPrice.dispatch('click', {}); // sort desc in 'resonance' (BBBUSDT=2.2, DDDUSDT=4.0 visible; AAA/CCC hidden)
                assert.deepStrictEqual(movedSymbols, ['DDDUSDT', 'BBBUSDT']);
                sortBtnPrice.dispatch('click', {});
                sortBtnPrice.dispatch('click', {}); // back to default

                // BBBUSDT has null funding_interval_hours -> poll clears stale funding_rate_4h_pct and renders '--'
                assert.strictEqual(state.find((c) => c.symbol === 'BBBUSDT').funding_rate_4h_pct, null);
                assert.strictEqual(rowEls.get('BBBUSDT').fr4hEl.textContent, '--');

                // Separated pool & ticker timestamps
                assert.ok(getElFn('pool-updated-time').textContent.startsWith('底池:'));
                assert.ok(getElFn('ticker-synced-time').textContent.startsWith('行情:'));

                // Price flash uses .price-flash-up CSS class (NOT element.animate)
                tickerStep = 1;
                const writesBeforeOkPoll = syncBadgeWrites;
                await win.__candTest.syncBinancePool();
                assert.ok(rowEls.get('BBBUSDT').priceEl.classList.contains('price-flash-up'));
                // aria-live #sync-status-badge did NOT rewrite when status stayed healthy
                assert.strictEqual(syncBadgeWrites, writesBeforeOkPoll);

                // Resonance badge + EMA critical state text labels (WCAG 1.4.1)
                assert.ok(tableBody.innerHTML.includes('共振'));
                assert.ok(tableBody.innerHTML.includes('粘合'));

                // Transient version/snapshot failure with existing data does NOT clobber #pool-updated-time
                const savedPoolText = getElFn('pool-updated-time').textContent;
                versionShouldFail = true;
                await win.__candTest.checkSnapshotVersion();
                assert.strictEqual(getElFn('pool-updated-time').textContent, savedPoolText);
                versionShouldFail = false;

                candidatesShouldFail = true;
                await win.__candTest.loadCandidateSnapshot({ force: true });
                assert.strictEqual(getElFn('pool-updated-time').textContent, savedPoolText);
                candidatesShouldFail = false;

                // Reentrancy lock coalesces concurrent checkSnapshotVersion calls
                holdVersion = true;
                const reqsBefore = versionRequestCount;
                const pA = win.__candTest.checkSnapshotVersion();
                const pB = win.__candTest.checkSnapshotVersion();
                assert.strictEqual(pA, pB);
                assert.strictEqual(versionRequestCount, reqsBefore + 1);
                resolvePendingVer({ ok: true, status: 200, headers: { get: () => null }, text: async () => JSON.stringify({ updated_at: snapshotVersion }) });
                holdVersion = false;
                resolvePendingVer = null;
                await pA;

                // visibilitychange cooldown: within 5s does NOT check version; after 5s DOES
                const reqsBeforeVis = versionRequestCount;
                docListeners.visibilitychange.forEach((fn) => fn());
                await new Promise((r) => setTimeout(r, 10));
                assert.strictEqual(versionRequestCount, reqsBeforeVis);
                fakeNow += 5001;
                docListeners.visibilitychange.forEach((fn) => fn());
                await new Promise((r) => setTimeout(r, 10));
                assert.strictEqual(versionRequestCount, reqsBeforeVis + 1);

                // Tri-state sorting (desc -> asc -> default) + visible count announcement
                kpiAll.dispatch('click', {}); // switch to 'all' (4 visible)
                sortBtnPrice.dispatch('click', {});
                assert.strictEqual(sortThPrice.getAttribute('aria-sort'), 'descending');
                assert.ok(getElFn('candidates-sort-status').textContent.includes('4 个标的'));
                sortBtnPrice.dispatch('click', {});
                assert.strictEqual(sortThPrice.getAttribute('aria-sort'), 'ascending');
                sortBtnPrice.dispatch('click', {});
                assert.strictEqual(sortThPrice.getAttribute('aria-sort'), null);

                // Global shortcuts ignore modifiers and SELECT/contentEditable
                docListeners.keydown.forEach((fn) => fn({ key: '2', metaKey: true, preventDefault() {} }));
                assert.strictEqual(kpiAll.getAttribute('aria-pressed'), 'true');
                activeElementRef = makeCandEl('sel', { tagName: 'SELECT' });
                docListeners.keydown.forEach((fn) => fn({ key: '1', preventDefault() {} }));
                assert.strictEqual(kpiAll.getAttribute('aria-pressed'), 'true');
                activeElementRef = makeCandEl('editable', { isContentEditable: true });
                docListeners.keydown.forEach((fn) => fn({ key: '1', preventDefault() {} }));
                assert.strictEqual(kpiAll.getAttribute('aria-pressed'), 'true');

                // #btn-reset-filter restores default 'resonance'
                activeElementRef = win.document.body;
                getElFn('btn-reset-filter').dispatch('click', {});
                assert.strictEqual(kpiRes.getAttribute('aria-pressed'), 'true');

                // Empty filtered state shows reset button; empty backend pool hides reset button
                getElFn('symbol-search-input').dispatch('input', { target: { value: 'ZZZNOTFOUND' } });
                await new Promise((r) => setTimeout(r, 140));
                assert.strictEqual(getElFn('empty-search-row').classList.contains('hidden'), false);
                assert.strictEqual(getElFn('empty-state-title').textContent, '当前筛选下无匹配合约');
                assert.strictEqual(getElFn('btn-empty-reset-filter').classList.contains('hidden'), false);

                // Clicking inline empty reset restores default
                tableBody.dispatch('click', {
                  stopPropagation() {},
                  target: { closest: (sel) => (sel === '#btn-empty-reset-filter' ? getElFn('btn-empty-reset-filter') : null) },
                });
                assert.strictEqual(getElFn('empty-search-row').classList.contains('hidden'), true);

                // Drawer close restores focus to #btn-toggle-drawer
                toggleDrawerBtn.dispatch('click', {});
                assert.strictEqual(tvDrawer.classList.contains('hidden'), false);
                getElFn('btn-close-drawer').dispatch('click', {});
                assert.strictEqual(tvDrawer.classList.contains('hidden'), true);
                assert.strictEqual(drawerFocusCount, 1);

                // Non-HTTPS clipboard fallback via execCommand('copy')
                getElFn('btn-batch-copy-tv').dispatch('click', {});
                await new Promise((r) => setTimeout(r, 10));
                assert.strictEqual(execCommandCalls, 1);

                // Export delays URL.revokeObjectURL by 1000ms (not synchronous)
                getElFn('btn-batch-export-tv').dispatch('click', {});
                assert.strictEqual(revokedUrls.length, 0);

                // Backend empty candidates pool distinguishes copy
                candidatesPayload = { updated_at: '2026-11-26T11:00:00+08:00', candidates: [] };
                await win.__candTest.loadCandidateSnapshot({ force: true });
                assert.strictEqual(getElFn('empty-state-title').textContent, '当前候选底池暂无符合策略标的');
                assert.strictEqual(getElFn('btn-empty-reset-filter').classList.contains('hidden'), true);
              }
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
