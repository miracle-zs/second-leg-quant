#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for P2 shared utilities, styles, tailwind cleanup, and playbook fixes."""

from pathlib import Path
import re
import shutil
import subprocess
import textwrap
import unittest


ROOT_DIR = Path(__file__).resolve().parent.parent


class TestP2SharedAndPlaybook(unittest.TestCase):
    def test_common_js_p2_utilities_format_and_tv_links_in_node_vm(self):
        node_bin = shutil.which("node")
        self.assertIsNotNone(node_bin, "node is required to test web/common.js")

        script = textwrap.dedent(
            """
            const assert = require('assert');
            const fs = require('fs');
            const path = require('path');
            const vm = require('vm');

            const commonCode = fs.readFileSync(path.join(process.cwd(), 'web/common.js'), 'utf8');

            (async () => {
              // 1. EXT_ARROW_SVG, getExtArrowSvg, debounce, formatSignedPct
              {
                const win = {
                  setTimeout,
                  clearTimeout,
                  console: { error() {}, warn() {} },
                };
                win.window = win;
                vm.runInNewContext(commonCode, win);
                const C = win.SecondLegCommon;

                assert.ok(C.EXT_ARROW_SVG.includes('class="w-2.5 h-2.5 shrink-0"'));
                assert.strictEqual(C.getExtArrowSvg(), C.EXT_ARROW_SVG);
                assert.ok(
                  C.getExtArrowSvg('text-[#7f91a8]').includes('class="w-2.5 h-2.5 text-[#7f91a8] shrink-0"')
                );
                assert.ok(C.getExtArrowSvg('a"b<c').includes('a&quot;b&lt;c'));

                // formatSignedPct zero-sign consistency
                assert.strictEqual(C.formatSignedPct(0), '0.00%');
                assert.strictEqual(C.formatSignedPct(-0), '0.00%');
                assert.strictEqual(C.formatSignedPct('0'), '0.00%');
                assert.strictEqual(C.formatSignedPct(0, 4), '0.0000%');
                assert.strictEqual(C.formatSignedPct(-0.001, 2), '0.00%');
                assert.strictEqual(C.formatSignedPct(0.001, 2), '0.00%');
                assert.strictEqual(C.formatSignedPct(0, { plusOnZero: true }), '+0.00%');
                assert.strictEqual(C.formatSignedPct(1.5), '+1.50%');
                assert.strictEqual(C.formatSignedPct(-1.5), '-1.50%');
                assert.strictEqual(C.formatSignedPct(50, { signed: false, decimals: 1 }), '50.0%');
                assert.strictEqual(C.formatSignedPct(null), '--');

                // debounce + cancel
                assert.throws(() => C.debounce('not-fn', 10), { name: 'TypeError', message: /debounce requires a function/ });
                const calls = [];
                const deb = C.debounce(function(x, y) {
                  calls.push({ self: this && this.tag, x, y });
                }, 15);
                const ctx = { tag: 'ctx' };
                deb.call(ctx, 1, 2);
                deb.call(ctx, 3, 4);
                await new Promise((r) => setTimeout(r, 35));
                assert.deepStrictEqual(calls, [{ self: 'ctx', x: 3, y: 4 }]);

                deb.call(ctx, 5, 6);
                deb.cancel();
                await new Promise((r) => setTimeout(r, 35));
                assert.strictEqual(calls.length, 1);
              }

              // 2. bindSortButtons: Invoker Commands path + aria-sort sync
              {
                function FakeButton() {}
                FakeButton.prototype.commandForElement = null;
                const tableListeners = {};
                const makeTh = (col) => {
                  const attrs = { 'data-sort': col };
                  return {
                    getAttribute(k) { return Object.prototype.hasOwnProperty.call(attrs, k) ? attrs[k] : null; },
                    setAttribute(k, v) { attrs[k] = String(v); },
                    removeAttribute(k) { delete attrs[k]; },
                  };
                };
                const thPrice = makeTh('price');
                const thChg = makeTh('chg');
                const table = {
                  dataset: {},
                  addEventListener(ev, fn) { (tableListeners[ev] ||= []).push(fn); },
                  querySelectorAll(sel) { return sel === 'th.sortable' ? [thPrice, thChg] : []; },
                };
                const win = {
                  HTMLButtonElement: FakeButton,
                  setTimeout,
                  clearTimeout,
                  console: { error() {}, warn() {} },
                };
                win.window = win;
                vm.runInNewContext(commonCode, win);

                assert.throws(
                  () => win.SecondLegCommon.bindSortButtons(table, null),
                  { name: 'TypeError', message: /bindSortButtons requires an onSort callback/ }
                );
                const sorted = [];
                const binder = win.SecondLegCommon.bindSortButtons(table, (th) => sorted.push(th.getAttribute('data-sort')));
                // Duplicate call should not attach duplicate command listener
                win.SecondLegCommon.bindSortButtons(table, (th) => sorted.push(th.getAttribute('data-sort')));
                assert.strictEqual((tableListeners.command || []).length, 1);

                tableListeners.command[0]({ command: '--other', source: { closest: () => thPrice } });
                tableListeners.command[0]({ command: '--sort', source: { closest: () => thPrice } });
                assert.deepStrictEqual(sorted, ['price']);

                binder.syncAria('price', false);
                assert.strictEqual(thPrice.getAttribute('aria-sort'), 'descending');
                assert.strictEqual(thChg.getAttribute('aria-sort'), null);
                win.SecondLegCommon.syncAriaSort([thPrice, thChg], 'chg', true);
                assert.strictEqual(thPrice.getAttribute('aria-sort'), null);
                assert.strictEqual(thChg.getAttribute('aria-sort'), 'ascending');
              }

              // 3. bindSortButtons: click fallback when Invoker Commands unsupported
              {
                const makeSortTh = (col) => {
                  const attrs = { 'data-sort': col };
                  const btnListeners = {};
                  const btn = {
                    addEventListener(ev, fn) { (btnListeners[ev] ||= []).push(fn); },
                    click() { (btnListeners.click || []).forEach((fn) => fn()); },
                    _listeners: btnListeners,
                  };
                  return {
                    dataset: {},
                    getAttribute(k) { return Object.prototype.hasOwnProperty.call(attrs, k) ? attrs[k] : null; },
                    setAttribute(k, v) { attrs[k] = String(v); },
                    removeAttribute(k) { delete attrs[k]; },
                    querySelector(sel) { return sel === 'button[command="--sort"]' ? btn : null; },
                    _btn: btn,
                  };
                };
                const thA = makeSortTh('a');
                const thB = makeSortTh('b');
                const table = {
                  querySelectorAll(sel) { return sel === 'th.sortable' ? [thA, thB] : []; },
                };
                const win = { setTimeout, clearTimeout, console: { error() {}, warn() {} } };
                win.window = win;
                vm.runInNewContext(commonCode, win);

                const clicked = [];
                win.SecondLegCommon.bindSortButtons(table, (th) => clicked.push(th.getAttribute('data-sort')));
                win.SecondLegCommon.bindSortButtons(table, (th) => clicked.push(th.getAttribute('data-sort')));
                assert.strictEqual(thA._btn._listeners.click.length, 1);
                thA._btn.click();
                thB._btn.click();
                assert.deepStrictEqual(clicked, ['a', 'b']);
              }

              // 4. [data-tv-layout-link] loading -> unconfigured -> error -> retry success
              {
                const makeTvLink = () => {
                  const attrs = { 'data-tv-layout-title': 'true' };
                  const listeners = {};
                  return {
                    href: 'https://cn.tradingview.com/',
                    title: '打开 TradingView 专属画线布局',
                    getAttribute(k) { return Object.prototype.hasOwnProperty.call(attrs, k) ? attrs[k] : null; },
                    setAttribute(k, v) { attrs[k] = String(v); },
                    removeAttribute(k) { delete attrs[k]; },
                    addEventListener(ev, fn) { (listeners[ev] ||= []).push(fn); },
                    click() {
                      let prevented = false;
                      const ev = { preventDefault() { prevented = true; } };
                      (listeners.click || []).forEach((fn) => fn(ev));
                      return prevented;
                    },
                  };
                };
                const link = makeTvLink();
                let mode = 'empty';
                let resolvePending = null;
                const win = {
                  document: {
                    readyState: 'loading',
                    addEventListener() {},
                    querySelector() { return null; },
                    querySelectorAll(sel) { return sel === 'a[data-tv-layout-link]' ? [link] : []; },
                  },
                  fetch: async () => {
                    if (mode === 'pending') {
                      return await new Promise((r) => { resolvePending = r; });
                    }
                    if (mode === 'error') {
                      return { ok: false, status: 503, headers: { get: () => null }, text: async () => 'down' };
                    }
                    if (mode === 'empty') {
                      return { ok: true, status: 200, headers: { get: () => null }, text: async () => JSON.stringify({ tradingview_layout_id: '' }) };
                    }
                    return { ok: true, status: 200, headers: { get: () => null }, text: async () => JSON.stringify({ tradingview_layout_id: 'LayoutP2' }) };
                  },
                  AbortController,
                  DOMException,
                  setTimeout,
                  clearTimeout,
                  console: { error() {}, warn() {} },
                };
                win.window = win;
                vm.runInNewContext(commonCode, win);

                // Loading state blocks click & sets data-tv-pending="loading"
                mode = 'pending';
                const pPending = win.SecondLegCommon.ensureConfig();
                assert.strictEqual(link.getAttribute('data-tv-pending'), 'loading');
                assert.strictEqual(link.getAttribute('aria-disabled'), 'true');
                assert.ok(link.title.includes('正在加载'));
                assert.strictEqual(link.click(), true);
                resolvePending({ ok: true, status: 200, headers: { get: () => null }, text: async () => JSON.stringify({ tradingview_layout_id: '' }) });
                await assert.rejects(pPending, /missing tradingview_layout_id/);
                assert.strictEqual(link.getAttribute('data-tv-pending'), 'unconfigured');
                assert.ok(link.title.includes('未配置'));
                assert.strictEqual(link.href, 'https://cn.tradingview.com/');

                // Error state blocks click & triggers retry on click
                mode = 'error';
                await assert.rejects(win.SecondLegCommon.ensureConfig(), /HTTP 503/);
                assert.strictEqual(link.getAttribute('data-tv-pending'), 'error');
                assert.ok(link.title.includes('加载失败'));

                // Clicking retries fetch when mode switches to ready
                mode = 'ready';
                assert.strictEqual(link.click(), true);
                await new Promise((r) => setTimeout(r, 20));
                assert.strictEqual(link.getAttribute('data-tv-pending'), null);
                assert.strictEqual(link.getAttribute('aria-disabled'), null);
                assert.strictEqual(link.href, 'https://cn.tradingview.com/chart/LayoutP2/');
                assert.ok(link.title.includes('LayoutP2'));
                assert.strictEqual(link.click(), false);
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

    def test_playbook_html_head_headers_steps_and_tokens(self):
        html = (ROOT_DIR / "web/playbook.html").read_text(encoding="utf-8")

        # Title aligned with nav label, favicon + api/config preload present
        self.assertIn("<title>突破追多与移动保本系统 — 二段爆发战法</title>", html)
        self.assertRegex(html, r'<link\b[^>]*rel="icon"[^>]*href="data:image/svg\+xml,')
        self.assertRegex(
            html,
            r'<link\b[^>]*rel="preload"[^>]*as="fetch"[^>]*crossorigin="anonymous"[^>]*href="api/config"',
        )

        # TV link includes data-tv-layout-title
        tv_cta = re.search(r'<a\b[^>]*data-tv-layout-link[^>]*>', html)
        self.assertIsNotNone(tv_cta)
        self.assertIn('data-tv-layout-title="true"', tv_cta.group(0))

        # All #121822 section headers unified to text-xs font-semibold
        header_bars = re.findall(
            r'<div\b[^>]*bg-\[#121822\][^>]*>\s*<h2\b[^>]*class="([^"]+)"',
            html,
        )
        self.assertEqual(len(header_bars), 3)
        for cls in header_bars:
            self.assertIn("text-xs", cls)
            self.assertIn("font-semibold", cls)
            self.assertNotIn("text-sm", cls)
            self.assertNotIn("font-bold", cls)

        # STEP 01-04 all share the aligned header + right-side label structure
        step_headers = re.findall(
            r'<div class="flex flex-wrap items-center justify-between gap-2">\s*<h3\b[^>]*>[^<]+</h3>\s*<span class="font-sans text-\[11px\] text-\[#9fb0c5\]">[^<]+</span>\s*</div>',
            html,
        )
        self.assertEqual(len(step_headers), 4)

        # Drifted colors converged to DESIGN.md tokens
        for drifted in ("#141d2a", "#161f2c"):
            self.assertNotIn(drifted, html)
        self.assertIn("bg-[#151c27]", html)
        self.assertIn('stroke="#1c2532"', html)
        self.assertIn("border-[#1c2532]", html)

    def test_styles_css_and_tailwind_css_cleanup(self):
        styles = (ROOT_DIR / "web/styles.css").read_text(encoding="utf-8")
        tailwind = (ROOT_DIR / "web/tailwind.css").read_text(encoding="utf-8")

        # Hardcoded row colors and drifted #2b384b removed; resonance first-child rules preserved
        for removed_hex in ("#16141c", "#16181d", "#2b384b"):
            self.assertNotIn(removed_hex, styles)
        self.assertIn("color-mix(", styles)
        self.assertIn("tr.resonance-neg-row > td:first-child", styles)
        self.assertIn("tr.resonance-gold-row > td:first-child", styles)

        # Only [hidden] uses !important
        important_matches = re.findall(r"[^\n{}]*!important", styles)
        self.assertEqual(len(important_matches), 1, f"Unexpected !important rules: {important_matches}")
        self.assertIn("display: none !important", important_matches[0])

        # .row-micro-btn touch target improved via min-height + ::after hit area
        micro_btn_match = re.search(r"\.row-micro-btn\s*\{([^}]*)\}", styles)
        self.assertIsNotNone(micro_btn_match)
        self.assertIn("min-height:", micro_btn_match.group(1))
        self.assertIn(".row-micro-btn::after", styles)

        # Unused 9 utility classes removed from tailwind.css; required classes present
        for unused_cls in (
            ".text-emerald-400{",
            ".text-rose-400{",
            ".text-slate-300{",
            ".text-slate-400{",
            ".blur{",
            ",.filter{",
            ".resize{",
            ".table{",
            ",.transform{",
        ):
            self.assertNotIn(unused_cls, tailwind)

        for kept_cls in (
            ".-translate-y-1\\/2{",
            ".backdrop-blur-sm{",
            ".max-w-\\[calc\\(100vw-2\\.5rem\\)\\]{",
            ".sm\\:grid-cols-4{",
        ):
            self.assertIn(kept_cls, tailwind)


if __name__ == "__main__":
    unittest.main()
