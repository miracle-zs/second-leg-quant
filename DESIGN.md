---
name: "突破追多与移动保本系统"
description: "面向币安 USDⓈ-M 低市值永续合约的瑞士时刻表级二段突破监控、实盘风控与绩效账本终端。"
colors:
  sig-cobalt: "#3b82f6"
  action-cobalt: "#2563eb"
  action-cobalt-hover: "#1d4ed8"
  sky-turnover: "#60a5fa"
  ink-cobalt: "#8ab8ff"
  sig-amber: "#f0b429"
  ink-amber: "#f5c451"
  sig-emerald: "#22c58b"
  ink-emerald: "#38d99f"
  sig-crimson: "#f25565"
  ink-crimson: "#f8717e"
  bg-canvas: "#0b0e13"
  bg-header: "#0d1219"
  bg-masthead: "#0e131b"
  bg-plate: "#10151d"
  bg-toolbar: "#121822"
  bg-plate-elevated: "#151c27"
  bg-row-hover: "#17202e"
  bg-plate-active: "#192231"
  bg-control-hover: "#1d2736"
  border-subtle: "#1c2532"
  border-hairline: "#222b38"
  border-control: "#283446"
  border-strong: "#324055"
  scrollbar-thumb: "#4b5c75"
  ink-primary: "#f1f5f9"
  ink-secondary: "#9fb0c5"
  ink-muted: "#7f91a8"
typography:
  display:
    fontFamily: "\"JetBrains Mono\", \"SF Mono\", \"Menlo\", \"Monaco\", \"Consolas\", ui-monospace, monospace"
    fontSize: "24px"
    fontWeight: 700
    lineHeight: 1
    letterSpacing: "-0.025em"
    fontFeature: "\"tnum\" 1, \"zero\" 1"
  headline:
    fontFamily: "\"Inter\", -apple-system, BlinkMacSystemFont, \"PingFang SC\", \"Hiragino Sans GB\", \"Microsoft YaHei\", \"Segoe UI\", sans-serif"
    fontSize: "20px"
    fontWeight: 700
    lineHeight: 1.4
    letterSpacing: "-0.025em"
  title:
    fontFamily: "\"Inter\", -apple-system, BlinkMacSystemFont, \"PingFang SC\", \"Hiragino Sans GB\", \"Microsoft YaHei\", \"Segoe UI\", sans-serif"
    fontSize: "14px"
    fontWeight: 700
    lineHeight: 1.25
    letterSpacing: "-0.025em"
  body:
    fontFamily: "\"Inter\", -apple-system, BlinkMacSystemFont, \"PingFang SC\", \"Hiragino Sans GB\", \"Microsoft YaHei\", \"Segoe UI\", sans-serif"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.625
  label:
    fontFamily: "\"JetBrains Mono\", \"SF Mono\", \"Menlo\", \"Monaco\", \"Consolas\", ui-monospace, monospace"
    fontSize: "11px"
    fontWeight: 500
    lineHeight: 1.4
    fontFeature: "\"tnum\" 1, \"zero\" 1"
rounded:
  hairline: "1px"
  xs: "3px"
  sm: "4px"
  md: "6px"
  full: "9999px"
spacing:
  px: "1px"
  xs: "4px"
  sm: "6px"
  md: "10px"
  lg: "12px"
  xl: "16px"
  2xl: "20px"
components:
  button-primary:
    backgroundColor: "{colors.action-cobalt}"
    textColor: "#ffffff"
    typography: "{typography.body}"
    rounded: "{rounded.sm}"
    padding: "4px 10px"
  button-primary-hover:
    backgroundColor: "{colors.action-cobalt-hover}"
    textColor: "#ffffff"
  button-secondary:
    backgroundColor: "{colors.bg-plate-elevated}"
    textColor: "{colors.ink-primary}"
    typography: "{typography.body}"
    rounded: "{rounded.sm}"
    padding: "4px 10px"
  button-secondary-hover:
    backgroundColor: "{colors.bg-control-hover}"
    textColor: "{colors.ink-primary}"
  row-action-primary:
    backgroundColor: "rgba(59, 130, 246, 0.1)"
    textColor: "{colors.ink-cobalt}"
    typography: "{typography.label}"
    rounded: "{rounded.xs}"
    padding: "1px 6px"
  row-action-ghost:
    backgroundColor: "{colors.bg-plate-elevated}"
    textColor: "{colors.ink-secondary}"
    typography: "{typography.label}"
    rounded: "{rounded.xs}"
    padding: "1px 6px"
  nav-segment-active:
    backgroundColor: "{colors.bg-plate-active}"
    textColor: "{colors.ink-primary}"
    typography: "{typography.body}"
    padding: "4px 12px"
  nav-segment:
    backgroundColor: "{colors.bg-canvas}"
    textColor: "{colors.ink-secondary}"
    typography: "{typography.body}"
    padding: "4px 12px"
  input-search:
    backgroundColor: "{colors.bg-canvas}"
    textColor: "{colors.ink-primary}"
    typography: "{typography.body}"
    rounded: "{rounded.sm}"
    padding: "4px 10px 4px 32px"
  badge-resonance-gold:
    backgroundColor: "rgba(240, 180, 41, 0.15)"
    textColor: "{colors.ink-amber}"
    typography: "{typography.label}"
    rounded: "{rounded.xs}"
    padding: "2px 6px"
  badge-resonance-squeeze:
    backgroundColor: "rgba(242, 85, 101, 0.15)"
    textColor: "{colors.ink-crimson}"
    typography: "{typography.label}"
    rounded: "{rounded.xs}"
    padding: "2px 6px"
  ledger-frame:
    backgroundColor: "{colors.bg-plate}"
    textColor: "{colors.ink-primary}"
    rounded: "{rounded.md}"
    padding: "14px 16px"
  zone-schedule-cell:
    backgroundColor: "{colors.bg-plate}"
    textColor: "{colors.ink-primary}"
    padding: "8px 14px"
  zone-schedule-cell-active:
    backgroundColor: "{colors.bg-plate-active}"
    textColor: "{colors.ink-primary}"
    padding: "8px 14px"
---

# Design System: 突破追多与移动保本系统

## Overview

**Creative North Star: "The Swiss Schedule Ledger"**

This visual system governs a high-frequency discretionary-quant trading terminal built for scanning sub-$30M Binance USDⓈ-M perpetual contracts, previewing 15m/1h EMA structures, launching TradingView logarithmic trendline layouts (`ig0LuDL8`), and auditing live stop-loss and trailing-breakeven discipline. It fuses the typographic rigor of a Swiss railway timetable with the density of an institutional order-book ledger: every funding countdown, keyboard-indexed opportunity zone, and contract row locks into a continuous `1px`-ruled slate matrix designed for multi-hour dark-room screen sessions.

Hierarchy is architectural rather than decorative. Stepped obsidian-to-slate plates (`#0b0e13` through `#192231`) and crisp `1px` structural hairlines (`#222b38`) replace floating card grids, while tabular monospace numerals (`tabular-nums`) guarantee zero horizontal jitter during 1-second settlement countdowns and 60-second market polls. The system explicitly rejects generic SaaS floating rounded cards, rainbow pill soup, and soft decorative glow shadows—chromatic ink appears only when signaling dual-core conviction, funding-rate squeeze polarity, EMA alignment, PnL outcome, or direct TradingView execution.

Motion and browser chrome follow the same instrument-grade restraint. Top-layer candlestick dialogs enter and exit via crisp `180ms` exponential ease-out (`cubic-bezier(0.16, 1, 0.3, 1)`) transitions with `@starting-style`, live price ticks flash briefly (`480ms`) in directional emerald or crimson before settling back to cold white, and native scrollbars, focus outlines, text selection, and input carets are all explicitly themed from the ledger palette while honoring `prefers-reduced-motion`.

**Key Characteristics:**
- **Connected 1px Hairline Matrices:** Multi-cell KPI schedules, trading rules, and two-tier account metrics sit inside shared `1px`-gap background grids (`#222b38`) rather than detached cards.
- **Rationed Quad-Signal Inks:** Neutral obsidian-slate plates occupy ≥85% of the viewport; Bullion Amber, Squeeze Crimson, Bullish Emerald, and Execution Cobalt appear strictly as semantic market and execution signals.
- **Zero-Jitter Tabular Numerals:** Every price, percentage, funding rate, countdown, rank, and ratio enforces `JetBrains Mono` with `"tnum" 1, "zero" 1` and right-aligned numeric columns.
- **Single-Keystroke Operational Cadence:** Visible `<kbd>` keycaps (`1`–`5`, `/`, `Enter`, `←`/`→`, `Esc`), sticky rank & symbol columns, and inline clickable ticker K-line triggers plus `TV` / `币安` / `复制` micro-actions keep scanning-to-charting to one action.
- **Themed Native Browser Surfaces:** Dark `color-scheme`, high-contrast slate scrollbars (`#4b5c75` over `#0e131b`), `2px` cobalt `:focus-visible` rings, cobalt selection highlights, and a bullion-amber caret leave zero unstyled browser defaults.

## Colors

A dark-room graphite-obsidian palette built on cool steel-tinted slate surfaces and four strictly rationed signal hues, each split into a saturated stroke/fill token and a higher-lightness ink token to preserve small-text contrast on dark plates.

### Primary
- **Execution Cobalt** (`#3b82f6`): Global `:focus-visible` `2px` outline, native `accent-color`, search input focus border, Zone 5 full-pool bar, and the account equity curve trace.
- **Action Cobalt** (`#2563eb`): Solid background fill for primary TradingView workflow triggers (`复制 TV 自选` batch copy and `TV 画线` layout launchers across all three views).
- **Action Cobalt Hover** (`#1d4ed8`): Deepened hover state for solid primary cobalt CTA buttons.
- **Sky Turnover** (`#60a5fa`): Zone 3 high-turnover (`≥1.5x`) active top bar and pool-share fill bar, plus the EMA10 chart overlay line and HUD swatch.
- **Ice Cobalt Ink** (`#8ab8ff`): High-contrast text ink for inline `15m图` primary row triggers, `≥1.5x` turnover badges, EMA10 readouts, steady Sharpe/Sortino ratings, and symbol link hovers.

### Secondary
- **Bullion Amber** (`#f0b429`): Signature conviction accent driving the masthead trendline-break emblem, Zone 1 (`顺势高换手共振区`) active top bar and fill bar, active table sort arrows, text input caret, EMA5 chart line, and degraded/partial sync indicator dots.
- **Warm Amber Ink** (`#f5c451`): Lifted amber text ink for Zone 1 resonance titles, `双核黄金共振` tags, ticking UTC hour settlement countdowns, 1h funding-interval badges, 7-day market-cap overflow tags, and unmonitored short/manual stop-loss warnings.

### Tertiary
- **Bullish Emerald** (`#22c58b`): Zone 4 (`1h EMA 顺势区`) active top bar and fill bar, healthy polling/API status dots, bullish candlestick bodies/wicks, and profitable daily/distribution chart bars.
- **Mint Emerald Ink** (`#38d99f`): High-contrast emerald text ink for positive 24h changes, positive realized/unrealized PnL and ROE, `LONG` side badges, `1h顺势` / `新进` resonance duration tags, and active trailing-breakeven status.
- **Squeeze Crimson** (`#f25565`): Zone 2 (`负费率贴水区`) active top bar and fill bar, leading `1px` inset hairline on negative-funding resonance rows, bearish candlesticks, loss chart bars, and API error dots.
- **Coral Crimson Ink** (`#f8717e`): High-contrast crimson text ink for negative 4h funding rates, negative PnL, max drawdown figures, liquidation estimates, `SHORT` side badges, `贴水双核共振` tags, and 15m trendline-break stop-loss alerts.

### Neutral
- **Obsidian Canvas** (`#0b0e13`): Root `html`/`body` background, recessed search inputs and `<select>` fields, segmented control tracks, and K-line `<canvas>` background.
- **Abyssal Header** (`#0d1219`): Sticky table header row (`thead.sticky-header`) and K-line `<dialog>` top control bar.
- **Masthead Slate** (`#0e131b`): Full-bleed top `<header>`, candidate table keyboard-legend `<footer>`, Tier-2 secondary risk KPI strip on `account.html`, and custom scrollbar track.
- **Ledger Plate** (`#10151d`): Primary surface for `.ledger-frame` containers, `.ledger-cell` grid items, resting `.kpi-card` cells, and `<dialog>` frames.
- **Toolbar Slate** (`#121822`): Unified section headers and filter toolbars sitting directly above tables and chart canvases.
- **Elevated Plate** (`#151c27`): Secondary buttons, resting `.row-action-link` chips, neutral status tags, quick-reference drawer header, `.kpi-card` hover state, and floating `#toast` surface.
- **Row Hover Slate** (`#17202e`): Interactive hover background across candidate, active position, and historical trade `tbody` rows (including sticky symbol cells on hover).
- **Active Zone Plate** (`#192231`): Selected `.kpi-card.active` cell, `aria-current="page"` masthead nav tab, active modal interval/bar/engine segment, and `#active-zone-pill`.
- **Control Hover Slate** (`#1d2736`): Hover fill on secondary slate buttons, copy buttons, and `.row-action-link:hover`.
- **Subtle Divider** (`#1c2532`): Low-contrast `1px` internal row dividers (`divide-y`) and vertical column group dividers (`border-l`) inside data tables and KPI sub-footers.
- **Structural Hairline** (`#222b38`): Primary `1px` architectural border on `.ledger-frame`, `.ledger-grid` background gap matrix, masthead bottom rule, and sticky table seams.
- **Control Border** (`#283446`): `1px` stroke around segmented nav/telemetry strips, secondary buttons, search inputs, `<select>` controls, and `<kbd>` keycaps.
- **Strong Frame Border** (`#324055`): High-emphasis `1px` border reserved for `<dialog.chart-modal-dialog>`, `#modal-chart-tooltip`, `#toast`, and active filter summary pills.
- **Scrollbar Slate** (`#4b5c75`): Custom scrollbar thumb (`≥3:1` contrast over `#0e131b`), breakeven chart segments, and bearish EMA indicator dots.
- **Cold White Ink** (`#f1f5f9`): Primary foreground ink for headings, contract base tickers, live prices, primary KPI readouts, and active tab labels.
- **Slate Secondary Ink** (`#9fb0c5`): Cool steel-tinted ink for table column headers, metric titles, inactive navigation tabs, secondary button icons/labels, and playbook body copy.
- **Muted Telemetry Ink** (`#7f91a8`): Tertiary cool-slate ink for row rank indices, `USDT` quote suffixes, input placeholders, timestamps, axis captions, and keyboard hint labels.

### Named Rules

**The Signal Rationing Rule.** Chromatic tokens (`#f0b429`, `#f25565`, `#22c58b`, `#3b82f6`) are reserved strictly for live market state, PnL polarity, and TradingView execution triggers; ≥85% of every screen remains obsidian-slate neutral.

**The Dual-Tone Contrast Rule.** Pair saturated signal fills (`#f0b429`, `#22c58b`, `#f25565`, `#3b82f6`) on bars, dots, and `10%`–`15%` alpha badge backgrounds with their lifted ink counterparts (`#f5c451`, `#38d99f`, `#f8717e`, `#8ab8ff`) for `10px`–`12px` text so dense dark-mode telemetry always clears contrast floors.

**The Whisper-Wash Row Rule.** Full-row semantic highlights (`resonance-gold-row` at `rgba(240, 180, 41, 0.04)` and `resonance-neg-row` at `rgba(242, 85, 101, 0.045)`) combine an ultra-sheer row wash with an `inset 1px 0 0` leading signal hairline and opaque color-matched sticky symbol cells (`#16181d` / `#16141c`) so horizontally scrolled data never shows through the frozen column.

## Typography

**Display Font:** `JetBrains Mono` (with `SF Mono, Menlo, Monaco, Consolas, ui-monospace, monospace`)
**Body Font:** `Inter` (with `-apple-system, BlinkMacSystemFont, PingFang SC, Hiragino Sans GB, Microsoft YaHei, Segoe UI, sans-serif`)
**Label/Mono Font:** `JetBrains Mono` (with `SF Mono, Menlo, Monaco, Consolas, ui-monospace, monospace`)

**Character:** Crisp neo-grotesque Latin and CJK UI legibility (`Inter` + `PingFang SC`) paired with an engineered tabular monospace (`JetBrains Mono`) that locks every decimal point, sign, and countdown colon into vertical alignment.

### Hierarchy
- **Display** (`700` weight, `24px` / `1.5rem` on Zone Schedule cells and `24px`–`26px` on Account Tier-1 KPIs, `line-height: 1`, `letter-spacing: -0.025em`, `JetBrains Mono` with `"tnum" 1, "zero" 1`): Primary quantitative readouts—opportunity zone ticker counts, total equity, available margin, realized PnL, and unrealized PnL.
- **Headline** (`700` weight, `16px` mobile / `20px` `sm+`, `line-height: 1.4`, `letter-spacing: -0.025em`, `Inter` sans stack): Top masthead system identity (`突破追多与移动保本系统`); secondary `18px` (`1.125rem`, `line-height: 1.25`) `JetBrains Mono` `700` readouts serve Tier-2 risk ratios (Win Rate, Profit Factor, Max Drawdown, Sharpe, Sortino).
- **Title** (`700`–`600` weight, `14px` / `0.875rem` on Playbook rules & `16px` on K-line modal symbol title; `12px` / `0.75rem` `600` weight on compact operational ledger headers, `line-height: 1.25`–`1.4`): Section headers inside `#121822` toolbar strips, sequential playbook step titles, and modal symbol headers.
- **Body** (`400`–`500` weight, `12px` / `0.75rem`, `line-height: 1.625` in reading cells and `1.333` in toolbar controls, `Inter` sans stack): Playbook rule descriptions (capped at `65ch`–`68ch` measure), search inputs, segmented navigation tabs, and action buttons.
- **Label** (`500`–`600` weight, `10px`–`11px`, `line-height: 1.4`, `JetBrains Mono` with `"tnum" 1, "zero" 1` for metrics/badges/keycaps; `11px` sans for `<thead>` column headers): Telemetry strips, table headers, signal badges (`双核黄金共振`, `LONG`/`SHORT`), `<kbd>` shortcuts, and dual-line cell sub-metrics.

### Named Rules

**The Tabular-Everywhere Rule.** Every numeric element—prices, signed percentages, 4h/settle funding rates, market caps, countdowns, timestamps, and table ranks—must enforce `font-variant-numeric: tabular-nums` (`"tnum" 1, "zero" 1`) with right alignment (`text-right`) in numeric columns so live polling never shifts column rhythm.

**The Monospace-For-Measurement Rule.** Reserve `JetBrains Mono` strictly for financial figures, timestamps, keyboard keycaps (`<kbd>`), quote-currency suffixes (`USDT`), step codes (`STEP 01`), and TradingView layout/trendline IDs (`ig0LuDL8`); never set Chinese prose or section headings in monospace.

## Layout

All three surfaces share a unified full-bleed top masthead (`<header>`) over a centered `max-w-[1680px]` workspace container with compact `12px` (`px-3`) mobile and `20px` (`sm:px-5`) desktop horizontal gutters and `12px`–`14px` (`py-3` / `py-3.5`) vertical padding. Vertical section rhythm stays deliberately tight (`10px` `space-y-2.5` on `index.html`, `12px` `space-y-3` on `account.html` and `playbook.html`) so a desktop trader sees live sync telemetry, all five opportunity zones, the filter/TV-export toolbar, and the sticky candidate ledger simultaneously above the fold without scrolling.

Multi-cell strips use the `.ledger-grid` architectural pattern (`background-color: #222b38; gap: 1px; border: 1px solid #222b38; border-radius: 6px; overflow: hidden`), where child `.ledger-cell` or `.kpi-card` elements paint `#10151d` so the container background itself forms crisp `1px` interior rules:
- **Strategy Monitor (`index.html`):** Connected 5-column Zone Schedule Strip (`grid-cols-2 lg:grid-cols-5`, with Zone 5 spanning `col-span-2 lg:col-span-1` on narrow viewports), optional 4-column collapsible quick-reference drawer (`grid-cols-1 sm:grid-cols-2 lg:grid-cols-4`), and a horizontally scrollable 11-column candidate table (`min-w-[920px]`, including `#`, sticky `合约标的`, and `快捷操作` columns) inside `#candidates-table-scroll`. On viewports `≥ 1024px`, `updateTableScrollMaxHeight()` dynamically bounds scroll height (`max(320px, viewportHeight - rect.top - 44)`) to avoid nested scroll traps even when `#tv-drawer` is expanded, while `< 1024px` clears `maxHeight` to follow natural page scroll. Both `thead.sticky-header`, the sticky `#` column (`w-9`, `inset-inline-start: 0`), and the sticky symbol column (`inset-inline-start: 2.25rem`, `min-width: 112px; max-width: clamp(112px, 34vw, 190px)`) stay pinned during horizontal scrolling.
- **Account Risk & Analytics (`account.html`):** Connected two-tier KPI matrix combining a 4-column primary capital/PnL row (`grid-cols-1 sm:grid-cols-2 lg:grid-cols-4`) above a 6-column secondary risk strip (`grid-cols-2 sm:grid-cols-3 lg:grid-cols-6` on `#0e131b`), followed by a `2x2` chart plate grid (`grid-cols-1 lg:grid-cols-2 gap-3`), a 9-column active positions monitor (`min-w-[920px]`), and a 10-column historical trade ledger (`min-w-[980px]`).
- **Playbook Manual (`playbook.html`):** Connected 3-column trading boundary datum strip (`grid-cols-1 md:grid-cols-3`, with the third commitment cell elevated to `#141d2a`) over an asymmetric `7:5` specification grid (`lg:grid-cols-12` splitting `lg:col-span-7` 4-step timetable with `78px` mobile / `112px` desktop step-index rail and `lg:col-span-5` API & risk-daemon specification ledger).

Responsive breakpoints step cleanly at `640px` (`sm`), `768px` (`md`), `1024px` (`lg`), and `1280px` (`xl`), collapsing masthead clusters vertically below `1280px` while preserving full tabular column integrity inside touch-scrollable `overflow-x-auto` regions rather than truncating critical risk numbers.

## Elevation & Depth

Depth across the workspace is conveyed through tonal slate layering and `1px` structural hairlines—never drop shadows at rest. The z-axis steps monotonically from the recessed `#0b0e13` canvas and input wells, up to `#0e131b` masthead/footer chrome, `#10151d` ledger plates, `#121822` section toolbars, `#151c27` elevated interactive controls, and `#192231` active selection plates. True drop shadows appear exclusively when a top-layer element detaches above the document plane (`<dialog>` candlestick modal and transient clipboard `#toast`), while zero-blur `1px` box-shadows act purely as structural border pins on sticky table headers and columns.

### Shadow Vocabulary
- **Modal Top-Layer Drop** (`box-shadow: 0 24px 48px -12px rgba(0, 0, 0, 0.72)`): Applied exclusively to `dialog.chart-modal-dialog` over a `rgba(5, 8, 13, 0.78)` backdrop with `backdrop-filter: blur(3px)`.
- **Toast Float** (`box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.1), 0 8px 10px -6px rgba(0, 0, 0, 0.1)`): Applied to the fixed bottom-right `#toast` confirmation banner (`z-50`).
- **Sticky Header Seam** (`box-shadow: 0 1px 0 #222b38`): Crisp `1px` bottom rule on `.sticky-header th` (`z-index: 10`) so scrolling rows clip cleanly beneath the `#0d1219` header.
- **Sticky Column Seam** (`box-shadow: inset -1px 0 0 #222b38`): Crisp `1px` right-hand divider on `#candidates-table .sticky-col` (`z-index: 5`, or `z-index: 20` on the top-left corner `th` combined with `0 1px 0 #222b38`).

### Named Rules

**The Flat-At-Rest Ledger Rule.** Workspace containers, KPI strips, charts, and tables are strictly flat at rest (`box-shadow: none`); never add ambient drop shadows or zero-offset colored neon glows to cards, badges, or buttons.

## Shapes

The form language is machined, rectilinear, and compact—echoing precision financial instruments and printed schedule sheets:
- **Outer Ledger Containers (`6px`):** `.ledger-frame`, `.ledger-grid`, and `<dialog.chart-modal-dialog>` use a tight `6px` border radius paired with `overflow: hidden` so internal header bars and `1px`-gap cells clip flush to the frame.
- **Interactive Controls & Strips (`4px`):** Primary/secondary buttons, segmented navigation and telemetry groups, search inputs, `<select>` dropdowns, modal chart tooltips, and inline warning banners use a `4px` radius (`5px` on the floating `#toast`).
- **Inline Micro-Tokens (`3px`):** `<kbd>` shortcut keycaps, resonance/side/interval badges, summary count pills, and `.row-action-link` table buttons use a crisp `3px` radius.
- **Telemetry Micro-Bars & Dots (`1px` / `9999px` / `0px`):** Inline 24h range tracks use `1px` corners; Zone Schedule pool-share bars (`2px` height) and active top indicator bars (`2px` height via `::before`) are sharp `0px` strips; `9999px` (`rounded-full`) is restricted exclusively to `6px`–`8px` live status/EMA dots—never pill-shaped buttons or tags.
- **Hairline Border Hierarchy (`1px`):** Outer frames use `#222b38`, interactive controls and divided strips use `#283446` (`divide-x divide-[#283446]`), internal table rows and metric sub-dividers use `#1c2532`, and top-layer modals/tooltips step up to `#324055`.

## Components

Every component is compact, crisp-edged, and engineered for rapid visual parsing and keyboard-first execution.

### Buttons
- **Shape:** Tight `4px` radius on toolbar/masthead actions; `3px` radius on dense inline table micro-actions.
- **Primary (`button-primary`):** Action Cobalt fill (`#2563eb`), pure white text (`#ffffff`), `12px` font size, `600` weight, `4px 10px` (`px-2.5 py-1`) padding, `6px` icon-text gap; used only for high-priority TradingView handoffs (`复制 TV 自选`, `TV 画线`).
- **Hover / Focus:** Primary shifts to `#1d4ed8` (`150ms` transition); secondary shifts background from `#151c27` to `#1d2736` and border from `#283446` to `#3b4d66`; keyboard focus renders a `2px solid #3b82f6` outline with `2px` offset.
- **Secondary & Row Micro-Actions:** Secondary toolbar controls (`刷新`, `速查`, `导出 .txt`, `重置`) pair `#151c27` background with `1px solid #283446` border and `#f1f5f9` / `#9fb0c5` text, alongside toolbar quick-filter toggle chips (`.filter-chip-btn`, `#btn-filter-fresh`, `#btn-filter-healthy-fr`) that illuminate to `#192231` fill with `rgba(59, 130, 246, 0.45)` border and `#8ab8ff` text when `aria-pressed="true"`. Inside candidate, active-position, and history rows, `.row-action-primary` / `.row-kline-chip` (`15m图`) use `rgba(59, 130, 246, 0.1)` fill, `rgba(59, 130, 246, 0.35)` border, and `#8ab8ff` text (`1px 6px` padding, `11px`), while `.row-action-link` (`TV↗`, `币安↗`) uses `#151c27` fill and `#263244` border (`#1d2736` / `#3d4f6a` on hover), and `.btn-copy-sym` locks `min-width: 34px` to eliminate width jitter when toggling to the check icon. Sortable table headers embed full-cell `<button class="sort-btn">` targets (`min-height: 34px`, `8px 10px` padding) with dual-chevron `.sort-icon` SVGs that highlight `#f0b429` when `aria-sort` is active.

### Chips
- **Style:** Rectilinear `3px`-radius micro-badges (`10px`–`11px`, `2px 6px` padding, `1px` border)—never rounded `9999px` pills.
- **State & Variants:** Conviction and polarity chips pair `10%`–`15%` signal alpha fills with `25%`–`35%` alpha borders and lifted signal ink: `双核黄金共振` (`rgba(240, 180, 41, 0.15)` bg, `#f5c451` text), `贴水双核共振` & `SHORT` (`rgba(242, 85, 101, 0.15)` bg, `#f8717e` text), `LONG` & `新进` (`rgba(34, 197, 139, 0.12)` bg, `#38d99f` text), and high turnover `≥2.0x` (`rgba(59, 130, 246, 0.15)` bg, `#f1f5f9` bold text). Neutral metadata tags (`换手`, `4h`, `驻留`) sit on `#151c27` with `#283446` borders, while `<kbd>` shortcut keycaps (`1`–`5`, `/`, `Esc`, `Alt+L`) render in `JetBrains Mono` on `#0b0e13` or `#151c27` with `1px solid #283446` strokes.

### Cards / Containers
- **Corner Style:** `6px` outer radius with sharp `0px` internal cell junctions.
- **Background:** `#10151d` plate body topped by a `#121822` header/toolbar strip (`1px solid #222b38` bottom divider); Tier-2 secondary KPI strips step down to `#0e131b`.
- **Shadow Strategy:** Strictly flat at rest (`box-shadow: none`), relying on `1px` hairline contrast against `#0b0e13` (see Elevation & Depth).
- **Border:** `1px solid #222b38` outer frame; `.ledger-grid` uses `#222b38` background with `gap: 1px` for seamless interior hairlines.
- **Internal Padding:** Compact `8px 14px` (`px-3.5 py-2`) on Zone Schedule cells, `10px 14px`–`10px 16px` on section toolbars, and `14px 16px` (`px-4 py-3.5` / `p-4`) on Account KPI and Playbook cells.

### Inputs / Fields
- **Style:** Recessed `#0b0e13` background, `1px solid #283446` stroke, `4px` radius, `12px` `#f1f5f9` text, `#7f91a8` placeholder, `#f0b429` caret, and a `14px` `#7f91a8` inline SVG search magnifier positioned at `left: 10px` (`padding: 4px 10px 4px 32px`). Native `<select>` filters (`#history-outcome-filter`) match the exact `#0b0e13` fill, `#283446` border, and `4px` radius.
- **Focus:** Border shifts crisply to `#3b82f6`; pressing `/` outside text fields focuses `#symbol-search-input` (on `index.html`) or `#history-search-input` (on `account.html`) immediately, and `Escape` clears and blurs it. Native `<select>` filters (`#history-side-filter`, `#history-outcome-filter`) share `#0b0e13` fill and `#283446` border, displaying a dynamic `#history-filtered-summary` net-PnL and win-rate pill when active filters narrow the history dataset.
- **Row Keyboard Selection & Urgency Signals:** Candidate rows support `↑`/`↓` (`j`/`k`) cursor stepping via `tr[data-selected="true"]` (`#192231` background + `inset 0 ±1px 0 rgba(59, 130, 246, 0.45)` top/bottom selection hairlines), opening the modal on `Enter` and copying the TV symbol on `c`. Within `<5 min` of hourly funding settlement, `#settle-countdown` shifts to `.settle-urgent` (`#f8717e`).
- **Error / Empty States:** Inline table empty/error rows render centered inside `tbody` (`py-14` / `py-8`) with actionable recovery hints (e.g., `<kbd>Esc</kbd>` reset instructions or retry buttons) rather than pop-up alerts.

### Navigation
- **Masthead & Divided Segmented Switcher:** Full-bleed `#0e131b` top bar (`border-b border-[#222b38]`) uniting three clusters: (1) brand SVG + `h1`, (2) divided `4px`-radius live telemetry strip (`border-[#283446] divide-x divide-[#283446]` housing live polling/API badge, UTC hour settlement countdown, and Beijing snapshot timestamp), and (3) divided 3-view `<nav>` switcher (`策略监控看板`, `账户数据分析`, `二段爆发战法`). Default tabs render `12px` `500`-weight `#9fb0c5` over `#0b0e13`, brighten to `#f1f5f9` on `#151c27` hover, and lock to `#192231` with `600`-weight `#f1f5f9` when `aria-current="page"`. On viewports below `1280px`, the left identity/telemetry cluster and right nav/action cluster wrap cleanly into two compact rows.

### Swiss Zone Schedule Strip & Dual-Engine K-Line Modal
- **Swiss Zone Schedule Strip (`.kpi-card[data-zone]`):** Signature 5-column connected matrix (`[1]` Resonance, `[2]` Negative Funding, `[3]` High Turnover, `[4]` 1h EMA Bull, `[5]` All Pool) acting simultaneously as live market telemetry and instant filter tabs. Resting cells sit on `#10151d`, lift to `#151c27` on hover, and switch to `#192231` when `.active` (`aria-pressed="true"`), illuminating a `2px` top edge bar (`::before`) in that zone's semantic signal color (`#f0b429`, `#f25565`, `#60a5fa`, `#22c58b`, or `#3b82f6`) matched by a `2px` bottom pool-share progress bar (`#1a2230` track) and representative ticker readout.
- **Dual-Engine K-Line Modal (`dialog.chart-modal-dialog`):** Shared across both `index.html` and `account.html` (lazy `<dialog>` shell injection when invoked from positions or trade history). Native top-layer `<dialog>` (`max-w-5xl`, `h-[85vh] max-h-[740px]`, `#10151d` plate, `1px solid #324055` border, `6px` radius) animated via `@starting-style` and `transition-behavior: allow-discrete` (`180ms` `cubic-bezier(0.16, 1, 0.3, 1)` from `translateY(6px) scale(0.985)`). Its `#0d1219` header combines live symbol price, divided interval (`15m`/`1h`/`4h`/`1D`), bar-count (`60根`/`120根`/`168根`), and engine (`极速K线`/`TV内嵌`) segmented groups, a direct `复制代码` trigger (`#modal-copy-btn`), and `TV 画线` button. Directly below sits the `#0b0e13` `#modal-context-hud` strip surfacing live/hovered `EMA5` (`#f0b429`), `EMA10` (`#60a5fa`), `EMA差值` spread, and caller context badges (`.modal-hud-badge` for candidate turnover/funding/resonance duration, active position entry/ROE/trailing-stop status, or trade entry/exit/net PnL/close reason). The HiDPI `<canvas>` overlays visible-window High/Low micro-labels, horizontal `entry_price` / `trailing_stop_price` / `exit_price` reference lines, and `15m` descending log `trendline.points` rays when previewed from `account.html`, plus modal hotkeys (`1`–`4` intervals, `t` engine toggle, `c` copy, `←`/`→` prev/next row, `Esc` close).

## Do's and Don'ts

### Do:
- **Do** house multi-metric KPIs and rule summaries inside connected `.ledger-grid` (`1px` `#222b38` hairline gap) or `.ledger-frame` plates so adjacent cells share crisp architectural rules.
- **Do** apply `tabular-nums` (`"tnum" 1, "zero" 1`) and `JetBrains Mono` to every price, percentage, funding rate, countdown, timestamp, and table rank, right-aligning numeric columns (`text-right`).
- **Do** pair `10%`–`15%` alpha signal badge fills (`#f0b429`, `#22c58b`, `#f25565`, `#3b82f6`) with their lifted ink tokens (`#f5c451`, `#38d99f`, `#f8717e`, `#8ab8ff`) so compact `10px`–`12px` labels maintain dark-mode readability.
- **Do** give sticky columns (`sticky-col`) an explicit opaque background matching each row state (`#10151d` default, `#16181d` gold resonance, `#16141c` squeeze resonance, `#17202e` hover) plus an `inset -1px 0 0 #222b38` seam shadow.
- **Do** expose keyboard shortcuts (`1`–`5`, `/`, `↑`/`↓`/`j`/`k`, `Enter`, `c`, `←`/`→`, `Esc`, modal `1`–`4`/`t`/`c`) via `3px`-radius `<kbd>` keycaps and preserve `2px solid #3b82f6` `:focus-visible` outlines and `prefers-reduced-motion` fallbacks across all interactive controls.

### Don't:
- **Don't** break KPI strips or specification matrices into detached, wide-gap rounded SaaS cards with drop shadows or hover lift transforms.
- **Don't** use `9999px` pill geometry on buttons, filter tabs, or signal tags; reserve `rounded-full` strictly for `6px`–`8px` status indicator dots.
- **Don't** introduce thick (`>1px`) colored `border-left` or `border-right` stripes on cards, rows, or callouts; use `inset 1px 0 0` hairlines on resonance table rows or `2px` top edge bars on active schedule cells.
- **Don't** paint secondary or muted text with flat neutral gray; always tint secondary (`#9fb0c5`) and muted (`#7f91a8`) inks from the cool steel-slate hue family.
- **Don't** add decorative neon `box-shadow` halos, gradient text, or unrequested modal interruptions; reserve top-layer `<dialog>` elevation strictly for on-demand candlestick inspection.
