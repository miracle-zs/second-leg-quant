#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for frontend P0 fixes (P0-1, P0-2, P0-4)."""

from pathlib import Path
import re
import shutil
import subprocess
import textwrap
import unittest


ROOT_DIR = Path(__file__).resolve().parent.parent


class TestFrontendP0Fixes(unittest.TestCase):
    def test_static_html_and_css_p0_contracts(self):
        index_html = (ROOT_DIR / "web/index.html").read_text(encoding="utf-8")
        styles_css = (ROOT_DIR / "web/styles.css").read_text(encoding="utf-8")

        # P0-4: #modal-tv-iframe has referrerpolicy="no-referrer" and drops deprecated attributes
        iframe_match = re.search(r'<iframe\b[^>]*id="modal-tv-iframe"[^>]*>', index_html)
        self.assertIsNotNone(iframe_match, "web/index.html must contain #modal-tv-iframe")
        iframe_tag = iframe_match.group(0)
        self.assertIn('referrerpolicy="no-referrer"', iframe_tag)
        self.assertNotIn("allowtransparency", iframe_tag)
        self.assertNotIn("scrolling", iframe_tag)

        # P0-4: CSS controls transparency and overflow instead of deprecated HTML attributes
        self.assertIn("#modal-tv-iframe", styles_css)
        self.assertIn("background-color: transparent", styles_css)
        self.assertIn("overflow: hidden", styles_css)

        # P0-2: table header has 11 columns including 快捷操作 and keeps resonance td:first-child rule
        table_head = index_html.split('<thead')[1].split('</thead>')[0]
        self.assertEqual(len(re.findall(r"<th\b", table_head)), 11)
        self.assertIn("快捷操作", table_head)
        self.assertIn('colspan="11"', index_html)
        self.assertIn("tr.resonance-neg-row > td:first-child", styles_css)
        self.assertIn("tr.resonance-gold-row > td:first-child", styles_css)
        self.assertIn(".row-symbol-btn", styles_css)

    def test_runtime_p0_1_p0_2_p0_4_behavior_in_node_vm(self):
        node_bin = shutil.which("node")
        self.assertIsNotNone(node_bin, "node is required to verify frontend P0 runtime behavior")

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

            function makeEl(id) {
              const attrs = {};
              const listeners = {};
              return {
                id,
                tagName: 'DIV',
                textContent: '',
                innerHTML: '',
                title: '',
                className: '',
                value: '',
                href: '',
                src: '',
                referrerPolicy: '',
                hidden: false,
                open: false,
                disabled: false,
                isConnected: true,
                dataset: {},
                style: {},
                classList: {
                  _set: new Set(),
                  add(...cls) { cls.forEach((c) => this._set.add(c)); },
                  remove(...cls) { cls.forEach((c) => this._set.delete(c)); },
                  toggle(c, force) {
                    if (force === undefined) {
                      if (this._set.has(c)) this._set.delete(c);
                      else this._set.add(c);
                    } else if (force) {
                      this._set.add(c);
                    } else {
                      this._set.delete(c);
                    }
                  },
                  contains(c) { return this._set.has(c); },
                },
                setAttribute(k, v) { attrs[k] = String(v); },
                getAttribute(k) { return Object.prototype.hasOwnProperty.call(attrs, k) ? attrs[k] : null; },
                removeAttribute(k) { delete attrs[k]; },
                hasAttribute(k) { return Object.prototype.hasOwnProperty.call(attrs, k); },
                addEventListener(ev, fn) { (listeners[ev] ||= []).push(fn); },
                removeEventListener(ev, fn) {
                  if (!listeners[ev]) return;
                  listeners[ev] = listeners[ev].filter((f) => f !== fn);
                },
                dispatch(ev, eventObj = {}) {
                  (listeners[ev] || []).forEach((fn) => fn(eventObj));
                },
                querySelector() { return null; },
                querySelectorAll() { return []; },
                focus() {},
              };
            }

            (async () => {
              // ------------------------------------------------------------
              // 1. P0-1: Initial load failure -> retry button + header refresh fallback
              // ------------------------------------------------------------
              const els = {};
              const bodyEl = makeEl('body');
              let activeElementRef = bodyEl;
              let candidateRequests = 0;
              let failCandidates = true;

              const sampleCandidate = {
                symbol: 'BTCUSDT',
                price: 95000,
                high_24h: 96000,
                low_24h: 94000,
                price_chg_24h: 2.5,
                market_cap_usd: 20000000,
                volume_24h_usd: 50000000,
                turnover_ratio: 2.5,
                circulating_supply: 210,
                multiplier: 1,
                funding_interval_hours: 4,
                funding_rate_pct: 0.01,
                funding_rate_4h_pct: 0.01,
                ema5: 95100,
                ema10: 94900,
                ema_spread_pct: 0.21,
                is_ema_bull: true,
                is_high_turnover: true,
                is_neg_funding: false,
                is_resonance: true,
              };

              const tableBodyEl = makeEl('candidate-table-body');
              const rowEls = [];
              let retryBtnRef = null;
              let retryFocusCount = 0;
              Object.defineProperty(tableBodyEl, 'innerHTML', {
                get() { return this._html || ''; },
                set(html) {
                  this._html = html;
                  rowEls.length = 0;
                  if (html.includes('id="btn-retry-candidates"')) {
                    retryBtnRef = makeEl('btn-retry-candidates');
                    retryBtnRef.tagName = 'BUTTON';
                    retryBtnRef.focus = () => {
                      retryFocusCount += 1;
                      activeElementRef = retryBtnRef;
                    };
                    els['btn-retry-candidates'] = retryBtnRef;
                  } else {
                    retryBtnRef = null;
                    delete els['btn-retry-candidates'];
                  }
                  const matches = [...html.matchAll(/data-sym="([^"]+)"/g)];
                  const seen = new Set();
                  for (const m of matches) {
                    const sym = m[1];
                    if (seen.has(sym)) continue;
                    seen.add(sym);
                    const row = makeEl(`row-${sym}`);
                    row.setAttribute('data-sym', sym);
                    row.setAttribute('data-tv-sym', `BINANCE:${sym}.P`);
                    row.querySelector = () => makeEl('sub');
                    rowEls.push(row);
                  }
                },
              });
              tableBodyEl.querySelectorAll = (sel) => (sel === 'tr.candidate-row' ? rowEls : []);
              els['candidate-table-body'] = tableBodyEl;

              // Setup chart modal + TV iframe mocks for P0-2 and P0-4
              const chartModalEl = makeEl('chart-modal');
              chartModalEl.showModal = () => { chartModalEl.open = true; };
              chartModalEl.close = () => {
                chartModalEl.open = false;
                chartModalEl.dispatch('close', {});
              };
              els['chart-modal'] = chartModalEl;

              const modalTvIframeEl = makeEl('modal-tv-iframe');
              modalTvIframeEl.setAttribute('allowtransparency', 'true');
              modalTvIframeEl.setAttribute('scrolling', 'no');
              els['modal-tv-iframe'] = modalTvIframeEl;

              const btnEngineTvEl = makeEl('btn-chart-engine-tv');
              const btnEngineNativeEl = makeEl('btn-chart-engine-native');
              els['btn-chart-engine-tv'] = btnEngineTvEl;
              els['btn-chart-engine-native'] = btnEngineNativeEl;

              const modalCanvasEl = makeEl('modal-kline-canvas');
              modalCanvasEl.getContext = () => null;
              els['modal-kline-canvas'] = modalCanvasEl;

              const syncBtnEl = makeEl('btn-manual-sync');
              els['btn-manual-sync'] = syncBtnEl;

              const doc = {
                hidden: false,
                readyState: 'complete',
                body: bodyEl,
                get activeElement() { return activeElementRef; },
                getElementById: (id) => (els[id] ||= makeEl(id)),
                querySelector: (sel) => (sel ? makeEl(sel) : null),
                querySelectorAll: () => [],
                createElement: (tag) => makeEl(tag),
                createDocumentFragment: () => ({ appendChild() {} }),
                addEventListener() {},
              };

              const win = {
                document: doc,
                sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
                fetch: async (url) => {
                  if (url === 'api/config') {
                    return {
                      ok: true,
                      status: 200,
                      headers: { get: () => null },
                      text: async () => JSON.stringify({ tradingview_layout_id: 'ig0LuDL8' }),
                    };
                  }
                  if (url === 'api/candidates') {
                    candidateRequests += 1;
                    if (failCandidates) {
                      return { ok: false, status: 503, headers: { get: () => null }, text: async () => 'Service Unavailable' };
                    }
                    return {
                      ok: true,
                      status: 200,
                      headers: { get: () => '"etag-ok"' },
                      text: async () => JSON.stringify({
                        updated_at: '2026-11-26T12:00:00+08:00',
                        candidates: [sampleCandidate],
                      }),
                    };
                  }
                  if (url === 'api/binance/ticker24hr' || url === 'api/binance/premiumIndex') {
                    return { ok: true, status: 200, headers: { get: () => null }, text: async () => '[]' };
                  }
                  if (url.startsWith('api/binance/klines')) {
                    return {
                      ok: true,
                      status: 200,
                      headers: { get: () => null },
                      text: async () => JSON.stringify([[1795200000000, '94000', '95500', '93800', '95000', '100']]),
                    };
                  }
                  return { ok: true, status: 200, headers: { get: () => null }, text: async () => '{}' };
                },
                AbortController,
                DOMException,
                setTimeout,
                clearTimeout,
                setInterval: () => 1,
                clearInterval() {},
                requestAnimationFrame: (fn) => { fn(); return 1; },
                addEventListener() {},
                matchMedia: () => ({ matches: false, addEventListener() {} }),
                console: { warn() {}, error() {} },
              };
              win.window = win;

              vm.runInNewContext(commonCode, win);
              vm.runInNewContext(klineModalCode, win);
              vm.runInNewContext(candRenderCode, win);
              vm.runInNewContext(candCode, win);

              // Wait for initial loadCandidateSnapshot({ force: true }) to fail
              await new Promise((r) => setTimeout(r, 20));
              assert.strictEqual(candidateRequests, 1);
              assert.strictEqual(els['pool-updated-time'].textContent, '候选池加载失败');
              assert.ok(tableBodyEl.innerHTML.includes('id="btn-retry-candidates"'));
              assert.ok(tableBodyEl.innerHTML.includes('立即重试'));
              assert.ok(tableBodyEl.innerHTML.includes('colspan="11"'));
              assert.ok(retryBtnRef, 'retry button mock should be instantiated');

              // Focus retry button and click it while API is still failing -> refocuses retry button
              activeElementRef = retryBtnRef;
              tableBodyEl.dispatch('click', {
                stopPropagation() {},
                target: {
                  closest: (sel) => (sel === '#btn-retry-candidates' ? retryBtnRef : null),
                },
              });
              await new Promise((r) => setTimeout(r, 20));
              assert.strictEqual(candidateRequests, 2);
              assert.strictEqual(retryFocusCount, 1);

              // Header refresh button (#btn-manual-sync) when candidatesState.length === 0 must NOT silently return
              failCandidates = false;
              syncBtnEl.dispatch('click', {});
              await new Promise((r) => setTimeout(r, 20));
              assert.strictEqual(candidateRequests, 3);
              assert.strictEqual(rowEls.length, 1);
              assert.ok(!tableBodyEl.innerHTML.includes('id="btn-retry-candidates"'));

              // ------------------------------------------------------------
              // 2. P0-2: <th scope="row"> isolation & keyboard trigger + focus restore
              // ------------------------------------------------------------
              const thMatch = tableBodyEl.innerHTML.match(/<th scope="row"[^>]*>([\\s\\S]*?)<\\/th>/);
              assert.ok(thMatch, 'rendered row must include <th scope="row">');
              const thInner = thMatch[1];
              assert.ok(thInner.includes('class="btn-preview-kline row-symbol-btn"'));
              assert.ok(!thInner.includes('TV'), '<th scope="row"> must not contain TV button');
              assert.ok(!thInner.includes('币安'), '<th scope="row"> must not contain 币安 button');
              assert.ok(!thInner.includes('btn-copy-sym'), '<th scope="row"> must not contain copy button');
              // td:first-child remains rank cell before <th scope="row">
              assert.ok(/<tr[^>]*>\\s*<td[^>]*sym-rank[^>]*>1<\\/td>\\s*<th scope="row"/.test(tableBodyEl.innerHTML));
              // sibling <td> holds TV, 币安, 复制
              assert.ok(/<\\/th>\\s*<td[^>]*>[\\s\\S]*TV[\\s\\S]*币安[\\s\\S]*btn-copy-sym[\\s\\S]*<\\/td>/.test(tableBodyEl.innerHTML));

              // Simulate keyboard activation of .btn-preview-kline
              let triggerFocusedCount = 0;
              const previewBtn = makeEl('preview-BTCUSDT');
              previewBtn.tagName = 'BUTTON';
              previewBtn.setAttribute('data-sym', 'BTCUSDT');
              previewBtn.setAttribute('data-tv-sym', 'BINANCE:BTCUSDT.P');
              previewBtn.focus = (opts) => {
                triggerFocusedCount += 1;
                previewBtn._lastFocusOpts = opts;
                activeElementRef = previewBtn;
              };
              activeElementRef = previewBtn;

              tableBodyEl.dispatch('click', {
                stopPropagation() {},
                target: {
                  closest: (sel) => (sel === '.btn-preview-kline' ? previewBtn : null),
                },
              });
              await new Promise((r) => setTimeout(r, 10));
              assert.strictEqual(win.SecondLegKlineModal.isOpen(), true);
              assert.strictEqual(win.SecondLegKlineModal.getCurrentSymbol(), 'BTCUSDT');

              // Navigate symbol while modal stays open -> must NOT clobber lastFocusedElement
              const nextBtn = makeEl('next-nav');
              activeElementRef = nextBtn;
              win.SecondLegKlineModal.open('ETHUSDT', 'BINANCE:ETHUSDT.P', nextBtn);
              await new Promise((r) => setTimeout(r, 10));
              assert.strictEqual(win.SecondLegKlineModal.getCurrentSymbol(), 'ETHUSDT');

              // ------------------------------------------------------------
              // 3. P0-4: Switching to TV engine configures referrerpolicy & cleans deprecated attrs
              // ------------------------------------------------------------
              btnEngineTvEl.dispatch('click', {});
              assert.strictEqual(modalTvIframeEl.getAttribute('referrerpolicy'), 'no-referrer');
              assert.strictEqual(modalTvIframeEl.referrerPolicy, 'no-referrer');
              assert.strictEqual(modalTvIframeEl.hasAttribute('allowtransparency'), false);
              assert.strictEqual(modalTvIframeEl.hasAttribute('scrolling'), false);
              assert.ok(modalTvIframeEl.src.includes('https://s.tradingview.com/widgetembed/'));

              // Close modal -> restores focus to original previewBtn
              win.SecondLegKlineModal.close();
              assert.strictEqual(win.SecondLegKlineModal.isOpen(), false);
              assert.strictEqual(triggerFocusedCount, 1);
              assert.strictEqual(activeElementRef, previewBtn);
              assert.strictEqual(modalTvIframeEl.src, 'about:blank');
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
