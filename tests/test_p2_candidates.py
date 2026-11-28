#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for candidates, kline-modal, and DESIGN.md P2 fixes (#23-#29)."""

from pathlib import Path
import re
import shutil
import subprocess
import textwrap
import unittest


ROOT_DIR = Path(__file__).resolve().parent.parent


class TestCandidatesAndModalP2Fixes(unittest.TestCase):
    def test_static_html_js_and_design_doc_p2_contracts(self):
        index_html = (ROOT_DIR / "web/index.html").read_text(encoding="utf-8")
        cand_js = (
            (ROOT_DIR / "web/candidates-render.js").read_text(encoding="utf-8")
            + "\n"
            + (ROOT_DIR / "web/candidates.js").read_text(encoding="utf-8")
        )
        modal_js = (ROOT_DIR / "web/kline-modal.js").read_text(encoding="utf-8")
        design_md = (ROOT_DIR / "DESIGN.md").read_text(encoding="utf-8")
        tailwind_css = (ROOT_DIR / "web/tailwind.css").read_text(encoding="utf-8")
        styles_css = (ROOT_DIR / "web/styles.css").read_text(encoding="utf-8")

        # #23: Eliminate duplicate aria-labelledby landmark and ensure # column has aria-label="序号"
        self.assertIn('<section aria-labelledby="pool-table-heading"', index_html)
        scroll_match = re.search(r'<div\b[^>]*id="candidates-table-scroll"[^>]*>', index_html)
        self.assertIsNotNone(scroll_match)
        self.assertNotIn('aria-labelledby="pool-table-heading"', scroll_match.group(0))
        self.assertIn('aria-label="候选币池数据表格滚动区"', scroll_match.group(0))
        self.assertRegex(index_html, r'<th\b[^>]*aria-label="序号"[^>]*>#</th>')

        # #24: Modal nav & close buttons must be at least 32px (w-8 h-8) on sm+ as well
        for btn_id in ("modal-prev-btn", "modal-next-btn", "modal-close-btn", "btn-close-drawer"):
            m = re.search(rf'<button\b[^>]*id="{btn_id}"[^>]*>', index_html)
            self.assertIsNotNone(m, f"Missing button #{btn_id}")
            cls_list = m.group(0).split('class="')[1].split('"')[0].split()
            self.assertIn("w-8", cls_list)
            self.assertIn("h-8", cls_list)
            self.assertNotIn("sm:w-6", cls_list)
            self.assertNotIn("sm:h-6", cls_list)

        # #27: Inline SVG favicon & preload for api/config
        self.assertRegex(index_html, r'<link\b[^>]*rel="icon"[^>]*href="data:image/svg\+xml,')
        preload_match = re.search(r'<link\b[^>]*rel="preload"[^>]*>', index_html)
        self.assertIsNotNone(preload_match)
        self.assertIn('as="fetch"', preload_match.group(0))
        self.assertIn('crossorigin="anonymous"', preload_match.group(0))
        self.assertIn('href="api/config"', preload_match.group(0))

        # #28: Title, spacing, right-aligned numeric columns, footer shortcuts
        self.assertIn("<title>突破追多与移动保本系统 — 策略监控看板</title>", index_html)
        self.assertNotIn("7d溢出", cand_js)
        self.assertIn("7d 溢出", cand_js)
        self.assertNotIn("TV画线", index_html)
        self.assertNotIn("TV画线", design_md)
        self.assertIn("TV 画线", index_html)
        self.assertIn("整点结算", index_html)

        for sort_col in ("ema", "duration"):
            th_match = re.search(rf'<th\b[^>]*data-sort="{sort_col}"[^>]*>', index_html)
            self.assertIsNotNone(th_match)
            cls_tokens = th_match.group(0).split('class="')[1].split('"')[0].split()
            self.assertIn("num-col", cls_tokens)
            self.assertIn("text-right", cls_tokens)

        footer_match = re.search(r'<footer\b[\s\S]*?</footer>', index_html)
        self.assertIsNotNone(footer_match)
        footer_text = footer_match.group(0)
        for key_label in ("Enter", "/", ">1<", ">5<", ">←<", ">→<", "Esc"):
            self.assertIn(key_label, footer_text)

        # #29: Dead class removed, toast max-w, tooltip responsive grid, symmetric zone cards, converged hex colors
        self.assertNotIn("table-scroll-region", index_html)
        toast_match = re.search(r'<div\b[^>]*id="toast"[^>]*>', index_html)
        self.assertIsNotNone(toast_match)
        self.assertIn("max-w-[calc(100vw-2.5rem)]", toast_match.group(0))
        self.assertIn("grid-cols-2 sm:grid-cols-4", modal_js)
        self.assertNotIn("grid grid-cols-4 gap-x-3", modal_js)

        self.assertIn('id="kpi-high-tr-sub"', index_html)
        self.assertIn('id="kpi-all-sample"', index_html)
        self.assertEqual(index_html.count("hidden sm:block text-[11px]"), 5)

        for drifted_hex in ("#9bc0ff", "#1b2432", "#8b9bb0", "#159668", "#d13b4b", "#647894", "#243247"):
            self.assertNotIn(drifted_hex, cand_js.lower())
            self.assertNotIn(drifted_hex, modal_js.lower())
            self.assertNotIn(drifted_hex, index_html.lower())

        # DESIGN.md synchronization
        self.assertIn("min-w-[920px]", design_md)
        self.assertIn("11-column", design_md)
        self.assertIn("updateTableScrollMaxHeight()", design_md)
        self.assertIn("inset-inline-start: 2.25rem", design_md)
        self.assertNotIn("min-w-[1060px]", design_md)
        self.assertNotIn("min-h-[420px]", design_md)
        self.assertNotIn("container-type: inline-size", design_md)

        # Verify no uncompiled Tailwind classes introduced (excluding the two added by shared-p2-fixer)
        defined = set()
        for css in (tailwind_css, styles_css):
            for blk in re.findall(r"([^{}]+)\{", css):
                for m in re.finditer(r"\.((?:\\.|[A-Za-z0-9_-])+)", blk):
                    defined.add(re.sub(r"\\(.)", r"\1", m.group(1)))

        allowed = {
            "modal-interval-btn", "modal-bars-btn", "btn-preview-kline", "btn-copy-sym",
            "sym-rank", "sym-price", "sym-pos-box", "sym-pos-bar", "sym-pos-text",
            "sym-chg", "sym-mc-val", "sym-vol", "sym-tr", "sym-fr", "sym-fr-4h",
            "sym-fr-settle", "sym-duration", "sym-res-badge", "candidate-row",
            "max-w-[calc(100vw-2.5rem)]", "sm:grid-cols-4",
        }
        for txt in (index_html, cand_js, modal_js):
            for m in re.finditer(r'class(?:Name)?\s*=\s*(?:"([^"]+)"|\'([^\']+)\'|`([^`]+)`)', txt):
                raw_val = m.group(1) or m.group(2) or m.group(3) or ""
                if "${" in raw_val:
                    raw_val = re.sub(r"\$\{[^}]+\}", " ", raw_val)
                for cls in raw_val.split():
                    if cls in ("?", ":"):
                        continue
                    self.assertTrue(
                        cls in defined or cls in allowed,
                        f"Uncompiled Tailwind/CSS class detected: {cls}",
                    )

    def test_runtime_p2_common_reuse_lifecycle_and_formatting_in_node_vm(self):
        node_bin = shutil.which("node")
        self.assertIsNotNone(node_bin, "node is required to verify frontend P2 runtime behavior")

        script = textwrap.dedent(
            """
            const assert = require('assert');
            const fs = require('fs');
            const path = require('path');
            const vm = require('vm');

            const root = process.cwd();
            const commonCode = fs.readFileSync(path.join(root, 'web/common.js'), 'utf8');
            const modalCode = fs.readFileSync(path.join(root, 'web/kline-modal.js'), 'utf8');
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

            function makeEl(id, { tagName = 'DIV', attrs = {} } = {}) {
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
                hidden: false,
                open: false,
                disabled: false,
                isConnected: true,
                clientWidth: 640,
                clientHeight: 400,
                dataset: {},
                style: {},
                children: [],
                classList: makeClassList(),
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
                getBoundingClientRect() { return { left: 0, top: 0, width: 640, height: 400 }; },
                closest() { return null; },
                querySelector() { return null; },
                querySelectorAll() { return []; },
                focus() {},
                blur() {},
                append(...nodes) { this.children.push(...nodes); },
                appendChild(node) { this.children.push(node); },
                removeChild() {},
                replaceChildren(...nodes) { this.children = [...nodes]; },
              };
              return el;
            }

            (async () => {
              // --------------------------------------------------------------
              // 1. kline-modal.js: rAF cleanup on close, ResizeObserver + fallback, tooltip format & colors
              // --------------------------------------------------------------
              {
                const els = {};
                const getEl = (id) => (els[id] ||= makeEl(id));
                const chartModal = makeEl('chart-modal', { tagName: 'DIALOG' });
                chartModal.showModal = () => { chartModal.open = true; };
                chartModal.close = () => { chartModal.open = false; chartModal.dispatch('close', {}); };
                els['chart-modal'] = chartModal;

                const nativeView = getEl('modal-native-chart-view');
                const modalTooltip = getEl('modal-chart-tooltip');
                const modalCanvas = getEl('modal-kline-canvas');
                const strokeColors = [];
                const fillColors = [];
                modalCanvas.getContext = () => ({
                  save() {}, restore() {}, setTransform() {}, clearRect() {},
                  fillRect() {}, strokeRect() {}, beginPath() {}, moveTo() {}, lineTo() {},
                  stroke() {}, fillText() {}, setLineDash() {}, drawImage() {},
                  measureText: (t) => ({ width: String(t).length * 6 }),
                });

                let nextRafId = 100;
                const pendingRafs = new Map();
                const cancelledRafs = [];
                let observedTarget = null;
                let resizeObserverCallback = null;
                let windowResizeCount = 0;

                class MockResizeObserver {
                  constructor(cb) { resizeObserverCallback = cb; }
                  observe(target) { observedTarget = target; }
                  disconnect() {}
                }

                // Candle at 2026-01-05 09:05:00 Beijing Time (01:05:00 UTC)
                const tsSingleDigits = Date.UTC(2026, 0, 5, 1, 5, 0);

                const win = {
                  ResizeObserver: MockResizeObserver,
                  document: {
                    hidden: false,
                    readyState: 'complete',
                    body: makeEl('body'),
                    activeElement: null,
                    getElementById: getEl,
                    querySelectorAll: () => [],
                    createElement: (tag) => {
                      const el = makeEl(tag, { tagName: tag.toUpperCase() });
                      el.getContext = () => ({
                        setTransform() {}, fillRect() {}, strokeRect() {}, beginPath() {},
                        moveTo() {}, lineTo() {}, stroke() {}, fillText() {}, setLineDash() {},
                        measureText: () => ({ width: 30 }),
                        set strokeStyle(v) { strokeColors.push(v); },
                        set fillStyle(v) { fillColors.push(v); },
                      });
                      return el;
                    },
                    addEventListener() {},
                  },
                  fetch: async (url) => {
                    if (url.startsWith('api/binance/klines')) {
                      return {
                        ok: true,
                        status: 200,
                        headers: { get: () => null },
                        text: async () => JSON.stringify([
                          [tsSingleDigits, '10', '12', '9', '11', '1500'],
                        ]),
                      };
                    }
                    return { ok: true, status: 200, headers: { get: () => null }, text: async () => '{}' };
                  },
                  AbortController,
                  DOMException,
                  setTimeout,
                  clearTimeout,
                  requestAnimationFrame: (fn) => {
                    const id = ++nextRafId;
                    pendingRafs.set(id, fn);
                    return id;
                  },
                  cancelAnimationFrame: (id) => {
                    cancelledRafs.push(id);
                    pendingRafs.delete(id);
                  },
                  addEventListener(ev) {
                    if (ev === 'resize') windowResizeCount += 1;
                  },
                  console: { warn() {}, error() {} },
                };
                win.window = win;
                vm.runInNewContext(commonCode, win);
                win.SecondLegCommon.setTvLayoutId('ig0LuDL8');
                vm.runInNewContext(modalCode, win);

                assert.strictEqual(observedTarget, nativeView, 'ResizeObserver should observe modalNativeView');
                assert.strictEqual(windowResizeCount, 0, 'window.resize should not be used when ResizeObserver exists');

                win.SecondLegKlineModal.open('BTCUSDT', 'BINANCE:BTCUSDT.P');
                await new Promise((r) => setTimeout(r, 10));

                // Trigger hover via mousemove -> schedules rAF (pending)
                nativeView.dispatch('mousemove', { clientX: 100 });
                assert.strictEqual(pendingRafs.size, 1);
                const rafToCancel = [...pendingRafs.keys()][0];

                // Closing modal cancels pending rAF and clears modalRenderRafId
                win.SecondLegKlineModal.close();
                assert.deepStrictEqual(cancelledRafs, [rafToCancel]);
                assert.strictEqual(pendingRafs.size, 0);

                // Reopen and flush ResizeObserver callback + hover render
                win.SecondLegKlineModal.open('BTCUSDT', 'BINANCE:BTCUSDT.P');
                await new Promise((r) => setTimeout(r, 10));
                resizeObserverCallback();
                assert.strictEqual(pendingRafs.size, 1);
                for (const fn of [...pendingRafs.values()]) fn();
                pendingRafs.clear();

                // Hover and flush rAF to populate tooltip
                nativeView.dispatch('mousemove', { clientX: 100 });
                for (const fn of [...pendingRafs.values()]) fn();
                pendingRafs.clear();

                const headerDiv = modalTooltip.children[0];
                const ohlcDiv = modalTooltip.children[1];
                const footerDiv = modalTooltip.children[2];
                assert.strictEqual(headerDiv.children[0].textContent, '01/05 09:05');
                assert.ok(ohlcDiv.className.includes('grid-cols-2 sm:grid-cols-4'));
                assert.strictEqual(footerDiv.children[2].textContent, '成交量 1.5K BTC');

                for (const bad of ['#1b2432', '#8b9bb0', '#159668', '#d13b4b', '#647894', '#243247']) {
                  assert.strictEqual(strokeColors.includes(bad), false, `Drifted stroke color ${bad}`);
                  assert.strictEqual(fillColors.includes(bad), false, `Drifted fill color ${bad}`);
                }

                // Fallback to window.resize when ResizeObserver is unavailable
                const fallbackWin = {
                  ...win,
                  ResizeObserver: undefined,
                  addEventListener(ev) {
                    if (ev === 'resize') windowResizeCount += 1;
                  },
                };
                fallbackWin.window = fallbackWin;
                vm.runInNewContext(commonCode, fallbackWin);
                vm.runInNewContext(modalCode, fallbackWin);
                assert.strictEqual(windowResizeCount, 1);
              }

              // --------------------------------------------------------------
              // 2. candidates.js: SecondLegCommon reuse + fallback, Beijing time, count labels, alignment
              // --------------------------------------------------------------
              {
                const els = {};
                const getEl = (id) => (els[id] ||= makeEl(id));
                const docListeners = {};
                let bindSortCalledWith = null;
                let debounceCalls = 0;
                let cancelCalls = 0;

                const sortThEma = makeEl('th-ema', { tagName: 'TH', attrs: { 'data-sort': 'ema' } });
                sortThEma.textContent = '1h EMA 趋势';
                const sortBtnEma = makeEl('btn-sort-ema', { tagName: 'BUTTON', attrs: { command: '--sort' } });
                sortThEma.querySelector = (sel) => (sel === 'button[command="--sort"]' ? sortBtnEma : null);

                const kpiRes = makeEl('kpi-res', { attrs: { 'data-zone': 'resonance' } });
                const kpiAll = makeEl('kpi-all', { attrs: { 'data-zone': 'all' } });
                const tableBody = getEl('candidate-table-body');
                const rowEls = [];
                Object.defineProperty(tableBody, 'innerHTML', {
                  get() { return this._html || ''; },
                  set(html) {
                    this._html = html;
                    rowEls.length = 0;
                    const seen = new Set();
                    for (const m of html.matchAll(/data-sym="([^"]+)"/g)) {
                      const sym = m[1];
                      if (seen.has(sym)) continue;
                      seen.add(sym);
                      const row = makeEl(`row-${sym}`, { tagName: 'TR' });
                      row.setAttribute('data-sym', sym);
                      row.setAttribute('data-tv-sym', `BINANCE:${sym}.P`);
                      row.querySelector = () => makeEl('sub');
                      rowEls.push(row);
                    }
                  },
                });
                tableBody.querySelectorAll = (sel) => (sel === 'tr.candidate-row' ? rowEls : []);

                const customArrow = '<svg data-custom-common-arrow="1"></svg>';
                const sampleList = [
                  {
                    symbol: 'AAAUSDT',
                    price: 1.5,
                    high_24h: 1.6,
                    low_24h: 1.2,
                    price_chg_24h: 3.2,
                    market_cap_usd: 35000000,
                    volume_24h_usd: 80000000,
                    turnover_ratio: 2.3,
                    circulating_supply: 23333333,
                    multiplier: 1,
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
                    is_overflow_7d: true,
                    overflow_days_left: 4.2,
                  },
                  {
                    symbol: 'BBBUSDT',
                    price: 2.0,
                    high_24h: 2.1,
                    low_24h: 1.9,
                    price_chg_24h: -1.0,
                    market_cap_usd: 15000000,
                    volume_24h_usd: 9000000,
                    turnover_ratio: 0.6,
                    circulating_supply: 7500000,
                    multiplier: 1,
                    funding_interval_hours: 4,
                    funding_rate_pct: -0.02,
                    funding_rate_4h_pct: -0.02,
                    ema5: 1.98,
                    ema10: 2.02,
                    ema_spread_pct: -1.98,
                    is_ema_bull: false,
                    is_high_turnover: false,
                    is_neg_funding: true,
                    is_resonance: false,
                  },
                ];

                const win = {
                  document: {
                    hidden: false,
                    readyState: 'complete',
                    body: makeEl('body'),
                    activeElement: null,
                    getElementById: getEl,
                    querySelector: () => null,
                    querySelectorAll: (sel) => {
                      if (sel === '.kpi-card') return [kpiRes, kpiAll];
                      if (sel === 'th.sortable') return [sortThEma];
                      return [];
                    },
                    createElement: (tag) => makeEl(tag, { tagName: tag.toUpperCase() }),
                    createDocumentFragment: () => ({ appendChild() {} }),
                    addEventListener(ev, fn) { (docListeners[ev] ||= []).push(fn); },
                  },
                  sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
                  fetch: async (url) => {
                    if (url === 'api/config') {
                      return {
                        ok: true,
                        status: 200,
                        headers: { get: () => null },
                        text: async () => JSON.stringify({ tradingview_layout_id: 'ig0LuDL8', high_turnover_threshold: 1.5 }),
                      };
                    }
                    if (url === 'api/candidates') {
                      return {
                        ok: true,
                        status: 200,
                        headers: { get: () => '"v1"' },
                        text: async () => JSON.stringify({
                          updated_at: '2026-11-26T12:00:00+08:00',
                          candidates: sampleList,
                        }),
                      };
                    }
                    return { ok: true, status: 200, headers: { get: () => null }, text: async () => '[]' };
                  },
                  AbortController,
                  DOMException,
                  setTimeout,
                  clearTimeout,
                  setInterval: () => 1,
                  clearInterval() {},
                  addEventListener() {},
                  matchMedia: () => ({ matches: false, addEventListener() {} }),
                  console: { warn() {}, error() {} },
                };
                win.window = win;
                vm.runInNewContext(commonCode, win);

                // Inject SecondLegCommon P2 helpers (as provided by shared-p2-fixer)
                const baseCommon = win.SecondLegCommon;
                win.SecondLegCommon = Object.freeze({
                  ...baseCommon,
                  EXT_ARROW_SVG: customArrow,
                  debounce(fn, delayMs) {
                    debounceCalls += 1;
                    let t = null;
                    const d = function (...args) {
                      if (t) clearTimeout(t);
                      t = setTimeout(() => { t = null; fn.apply(this, args); }, delayMs);
                    };
                    d.cancel = () => {
                      cancelCalls += 1;
                      if (t) { clearTimeout(t); t = null; }
                    };
                    return d;
                  },
                  bindSortButtons(rootEl, onSort) {
                    bindSortCalledWith = rootEl;
                    rootEl.querySelectorAll('th.sortable').forEach((th) => {
                      const btn = th.querySelector('button[command="--sort"]') || th;
                      btn.addEventListener('click', () => onSort(th));
                    });
                    return {
                      syncAria(col, asc) {
                        rootEl.querySelectorAll('th.sortable').forEach((th) => {
                          if (col && th.getAttribute('data-sort') === col) th.setAttribute('aria-sort', asc ? 'ascending' : 'descending');
                          else th.removeAttribute('aria-sort');
                        });
                      },
                    };
                  },
                });

                vm.runInNewContext(candRenderCode, win);
                vm.runInNewContext(candCode, win);
                await new Promise((r) => setTimeout(r, 30));

                assert.ok(bindSortCalledWith, 'candidates.js should call SecondLegCommon.bindSortButtons');
                assert.strictEqual(debounceCalls, 1, 'candidates.js should wrap search input via SecondLegCommon.debounce');
                assert.ok(tableBody.innerHTML.includes(customArrow), 'candidates.js should reuse SecondLegCommon.EXT_ARROW_SVG');
                assert.ok(tableBody.innerHTML.includes('7d 溢出'), '7d 溢出 spacing fixed');
                assert.ok(tableBody.innerHTML.includes('在新标签页打开 AAAUSDT TradingView 15m 专属画线布局'));
                assert.ok(tableBody.innerHTML.includes('在新标签页打开 AAAUSDT 币安永续合约盘口'));
                assert.ok(!tableBody.innerHTML.includes('#9bc0ff'));
                assert.ok(!tableBody.innerHTML.includes('#1b2432'));

                // Beijing time retained on #pool-updated-time
                assert.strictEqual(getEl('pool-updated-time').textContent, '底池: 11-26 12:00:00 (北京时间)');

                // Batch copy/export labels reflect filtered count (1 in resonance, 2 in all)
                assert.strictEqual(getEl('btn-batch-copy-tv-label').textContent, '复制 TV 自选 (1)');
                assert.strictEqual(getEl('btn-batch-export-tv-label').textContent, '导出 .txt (1)');
                assert.ok(getEl('btn-batch-copy-tv').title.includes('1 个标的'));
                assert.ok(getEl('btn-batch-export-tv').title.includes('1 个标的'));

                kpiAll.dispatch('click', {});
                assert.strictEqual(getEl('btn-batch-copy-tv-label').textContent, '复制 TV 自选 (2)');
                assert.strictEqual(getEl('btn-batch-export-tv-label').textContent, '导出 .txt (2)');

                // Reset invokes debouncedApplyFilter.cancel()
                getEl('btn-reset-filter').dispatch('click', {});
                assert.strictEqual(cancelCalls, 1);

                // Sort binding delegates via SecondLegCommon.bindSortButtons
                sortBtnEma.dispatch('click', {});
                assert.strictEqual(sortThEma.getAttribute('aria-sort'), 'descending');
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
