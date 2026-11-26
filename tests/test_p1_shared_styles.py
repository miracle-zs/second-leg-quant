#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for shared styles, common config consumption, and playbook P1 fixes."""

from pathlib import Path
import re
import shutil
import subprocess
import textwrap
import unittest


ROOT_DIR = Path(__file__).resolve().parent.parent


class TestP1SharedStylesAndCommon(unittest.TestCase):
    def test_common_js_config_and_chinese_tv_map_in_node_vm(self):
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
              // 1. Server /api/config overrides & extends CHINESE_TV_MAP while keeping built-in fallback
              const tvLinks = [{
                href: 'https://cn.tradingview.com/',
                attributes: { 'data-tv-layout-title': 'true' },
                getAttribute(k) { return this.attributes[k] || null; },
              }];
              let configRequests = 0;

              const win = {
                document: {
                  readyState: 'loading',
                  addEventListener() {},
                  querySelector() { return null; },
                  querySelectorAll(sel) {
                    return sel === 'a[data-tv-layout-link]' ? tvLinks : [];
                  },
                },
                fetch: async (url) => {
                  if (url === 'api/config') {
                    configRequests += 1;
                    return {
                      ok: true,
                      status: 200,
                      headers: { get: () => null },
                      text: async () => JSON.stringify({
                        tradingview_layout_id: 'LayoutP1',
                        high_turnover_threshold: 1.8,
                        chinese_tv_map: {
                          '龙虾USDT': 'BINANCE:OVERRIDE_LONGXIA.P',
                          '新中文USDT': 'BINANCE:XINZHONGWENUSDT.P',
                          '__proto__': 'BINANCE:EVIL.P',
                          'badNumber': 123,
                          'emptyVal': '   ',
                        },
                      }),
                    };
                  }
                  throw new Error('Unexpected fetch: ' + url);
                },
                AbortController,
                DOMException,
                setTimeout,
                clearTimeout,
                console: { error() {}, warn() {} },
              };
              win.window = win;
              vm.runInNewContext(commonCode, win);

              // Before ensureConfig: built-in fallback works and getConfig is frozen
              const initialCfg = win.SecondLegCommon.getConfig();
              assert.strictEqual(Object.isFrozen(initialCfg), true);
              assert.strictEqual(Object.isFrozen(initialCfg.chinese_tv_map), true);
              assert.strictEqual(initialCfg.tradingview_layout_id, '');
              assert.strictEqual(
                win.SecondLegCommon.getTvSymbol('龙虾USDT'),
                'BINANCE:LONGXIAUSDT.P'
              );

              const loadedId = await win.SecondLegCommon.ensureConfig();
              assert.strictEqual(loadedId, 'LayoutP1');
              assert.strictEqual(configRequests, 1);

              // Merged chinese_tv_map prioritizes /api/config while keeping fallback keys
              assert.strictEqual(
                win.SecondLegCommon.getTvSymbol('龙虾USDT'),
                'BINANCE:OVERRIDE_LONGXIA.P'
              );
              assert.strictEqual(
                win.SecondLegCommon.getTvSymbol('新中文USDT'),
                'BINANCE:XINZHONGWENUSDT.P'
              );
              assert.strictEqual(
                win.SecondLegCommon.getTvSymbol('哈基米USDT'),
                'BINANCE:HAJIMIUSDT.P'
              );
              assert.strictEqual(
                win.SecondLegCommon.getTvUrl('新中文USDT', null, '15m'),
                'https://cn.tradingview.com/chart/LayoutP1/?symbol=BINANCE%3AXINZHONGWENUSDT.P&interval=15'
              );

              // Safe getConfig & getConfigValue accessors resist mutation and prototype keys
              const cfg = win.SecondLegCommon.getConfig();
              assert.strictEqual(Object.isFrozen(cfg), true);
              assert.strictEqual(Object.isFrozen(cfg.chinese_tv_map), true);
              assert.strictEqual(cfg.tradingview_layout_id, 'LayoutP1');
              assert.strictEqual(cfg.high_turnover_threshold, 1.8);
              assert.strictEqual(win.SecondLegCommon.getConfigValue('high_turnover_threshold', 1.5), 1.8);
              assert.strictEqual(win.SecondLegCommon.getConfigValue('missing_field', 'fallback'), 'fallback');
              assert.strictEqual(win.SecondLegCommon.getConfigValue('__proto__', 'safe'), 'safe');
              assert.strictEqual(win.SecondLegCommon.getConfigValue('constructor', 'safe'), 'safe');
              assert.strictEqual(Object.prototype.hasOwnProperty.call(cfg.chinese_tv_map, 'badNumber'), false);
              assert.strictEqual(Object.prototype.hasOwnProperty.call(cfg.chinese_tv_map, 'emptyVal'), false);

              cfg.tradingview_layout_id = 'Tampered';
              cfg.chinese_tv_map['龙虾USDT'] = 'TAMPERED';
              assert.strictEqual(win.SecondLegCommon.getConfig().tradingview_layout_id, 'LayoutP1');
              assert.strictEqual(win.SecondLegCommon.getTvSymbol('龙虾USDT'), 'BINANCE:OVERRIDE_LONGXIA.P');
              assert.strictEqual(tvLinks[0].href, 'https://cn.tradingview.com/chart/LayoutP1/');

              // 2. Fallback when /api/config omits chinese_tv_map
              const winFallback = {
                document: {
                  readyState: 'loading',
                  addEventListener() {},
                  querySelector() { return null; },
                  querySelectorAll() { return []; },
                },
                fetch: async () => ({
                  ok: true,
                  status: 200,
                  headers: { get: () => null },
                  text: async () => JSON.stringify({ tradingview_layout_id: 'ig0LuDL8' }),
                }),
                AbortController,
                DOMException,
                setTimeout,
                clearTimeout,
                console: { error() {}, warn() {} },
              };
              winFallback.window = winFallback;
              vm.runInNewContext(commonCode, winFallback);
              await winFallback.SecondLegCommon.ensureConfig();
              assert.strictEqual(
                winFallback.SecondLegCommon.getTvUrl('哈基米USDT'),
                'https://cn.tradingview.com/chart/ig0LuDL8/?symbol=BINANCE%3AHAJIMIUSDT.P'
              );
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

    def test_styles_css_design_tokens_shared_classes_and_scrollbar(self):
        css = (ROOT_DIR / "web/styles.css").read_text(encoding="utf-8")

        # Typography decoupling: .font-sans, .tabular-nums (no monospace), .font-mono
        font_sans_match = re.search(r"\.font-sans\s*\{([^}]*)\}", css)
        self.assertIsNotNone(font_sans_match)
        self.assertIn("font-family: var(--font-sans)", font_sans_match.group(1))

        tabular_match = re.search(r"(?:^|\n)\.tabular-nums\s*\{([^}]*)\}", css)
        self.assertIsNotNone(tabular_match, ".tabular-nums must be defined independently")
        self.assertIn("font-variant-numeric: tabular-nums", tabular_match.group(1))
        self.assertIn('font-feature-settings: "tnum" 1, "zero" 1', tabular_match.group(1))
        self.assertNotIn("var(--font-mono)", tabular_match.group(1))

        font_mono_match = re.search(r"(?:^|\n)\.font-mono\s*\{([^}]*)\}", css)
        self.assertIsNotNone(font_mono_match)
        self.assertIn("font-family: var(--font-mono)", font_mono_match.group(1))
        self.assertIn("font-variant-numeric: tabular-nums", font_mono_match.group(1))

        # Modern browser scrollbar fix: no @supports not (scrollbar-color: auto) guard
        self.assertNotIn("@supports not (scrollbar-color: auto)", css)
        self.assertIn("@supports selector(::-webkit-scrollbar)", css)
        self.assertIn("html::-webkit-scrollbar,", css)
        self.assertIn(".custom-scrollbar::-webkit-scrollbar", css)
        self.assertIn("width: 10px;", css)

        # Shared cross-page classes
        self.assertIn(".min-h-0", css)
        min_h_match = re.search(r"\.min-h-0\s*\{([^}]*)\}", css)
        self.assertIsNotNone(min_h_match)
        self.assertIn("min-height: 0", min_h_match.group(1))

        cobalt_match = re.search(r"\.btn-primary-cobalt\s*\{([^}]*)\}", css)
        self.assertIsNotNone(cobalt_match)
        self.assertIn("#2563eb", cobalt_match.group(1))
        self.assertIn("#ffffff", cobalt_match.group(1))
        self.assertIn("font-weight: 600", cobalt_match.group(1))
        cobalt_hover = re.search(r"\.btn-primary-cobalt:hover\s*\{([^}]*)\}", css)
        self.assertIsNotNone(cobalt_hover)
        self.assertIn("#1d4ed8", cobalt_hover.group(1))

        self.assertIn(".kpi-zone-title", css)
        active_zone_title = re.search(r"\.kpi-card\.active\s+\.kpi-zone-title\s*\{([^}]*)\}", css)
        self.assertIsNotNone(active_zone_title)
        self.assertIn("var(--sig-amber)", active_zone_title.group(1))
        self.assertIn("font-weight: 600", active_zone_title.group(1))

        track_match = re.search(r"\.zone-progress-track\s*\{([^}]*)\}", css)
        self.assertIsNotNone(track_match)
        self.assertIn("height: 2px", track_match.group(1))
        self.assertIn("#1a2230", track_match.group(1))

        fill_match = re.search(r"\.zone-progress-fill\s*\{([^}]*)\}", css)
        self.assertIsNotNone(fill_match)
        self.assertIn("var(--sig-amber)", fill_match.group(1))
        self.assertIn("transition:", fill_match.group(1))

        self.assertIn(".tier2-split-row", css)
        self.assertIn("@media (min-width: 1280px)", css)
        self.assertIn("flex-direction: column", css)
        self.assertIn("flex-direction: row", css)

        self.assertIn("@keyframes price-flash-up", css)
        self.assertIn("@keyframes price-flash-down", css)
        self.assertIn(".price-flash-up", css)
        self.assertIn(".price-flash-down", css)
        self.assertIn("prefers-reduced-motion: reduce", css)

        # Preserve P0 contract
        self.assertIn("tr.resonance-neg-row > td:first-child", css)
        self.assertIn("tr.resonance-gold-row > td:first-child", css)

    def test_playbook_html_cta_and_typography(self):
        html = (ROOT_DIR / "web/playbook.html").read_text(encoding="utf-8")

        # Primary CTA uses .btn-primary-cobalt
        tv_cta_match = re.search(r'<a\b[^>]*data-tv-layout-link[^>]*>', html)
        self.assertIsNotNone(tv_cta_match)
        self.assertIn("btn-primary-cobalt", tv_cta_match.group(0))
        self.assertNotIn("bg-[#3b82f6]/[0.12]", tv_cta_match.group(0))

        # Chinese prose labels no longer wrapped in font-mono / ui-monospace
        self.assertNotIn("ui-monospace", html)
        self.assertNotIn('font-mono text-[11px] text-[#9fb0c5]">Server酱入圈推送', html)
        self.assertNotIn('font-mono text-[10px] text-[#7f91a8]">仅匹配画线多单受控', html)
        self.assertNotIn('font-mono text-[#7f91a8] text-right">← 左右滑动', html)
        self.assertIn('font-sans text-[11px] text-[#9fb0c5]">Server酱入圈推送', html)
        self.assertIn('font-sans text-[10px] text-[#7f91a8]">仅匹配画线多单受控', html)
        self.assertIn("看板展示 · 手动止损", html)
        dl_match = re.search(r'<dl\b[^>]*class="([^"]*)"', html)
        self.assertIsNotNone(dl_match)
        self.assertIn("font-sans", dl_match.group(1))
        self.assertNotIn("font-mono", dl_match.group(1))


if __name__ == "__main__":
    unittest.main()
