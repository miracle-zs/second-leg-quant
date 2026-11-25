#!/usr/bin/env python3
"""
突破追多与移动保本系统 · 双端纯静态 HTML Shell 与全局导航渲染模块
================================================================
仅负责：
- 全局顶部三视图导航栏 (`render_global_nav_tabs`, `render_global_header`)
- 策略监控看板纯静态 HTML Shell (`render_index_shell_html`)
- 二段爆发战法手册纯静态 HTML (`render_playbook_html`)

候选池业务渲染、过滤排序、币安 60s 轮询与双引擎 K 线弹窗统一由 `web/candidates.js` 实现，
公共暗色主题样式统一由 `web/styles.css` 提供。
"""

from __future__ import annotations

import html
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CANONICAL_PAGES = ("index", "account", "playbook")


def render_global_nav_tabs(current_page: str = "index") -> str:
    """渲染全局顶部导航栏三核心视图标签组（index / account / playbook）。"""
    page_norm = current_page.lower().strip()
    if page_norm not in CANONICAL_PAGES:
        raise ValueError(f"Unsupported current_page {current_page!r}; expected one of {CANONICAL_PAGES}")

    is_in_reports = page_norm == "playbook"
    index_url = "../index.html" if is_in_reports else "index.html"
    account_url = "../account.html" if is_in_reports else "account.html"
    playbook_url = "strategy_second_leg_playbook.html" if is_in_reports else "reports/strategy_second_leg_playbook.html"

    tab_configs = (
        (
            "index",
            index_url,
            "📊",
            "策略监控看板",
            "二段突破与趋势线监控",
            "bg-sky-500/20 text-sky-400 border-sky-500/40 shadow-sky-500/10",
            "bg-sky-400",
        ),
        (
            "account",
            account_url,
            "💼",
            "账户数据分析",
            "资金走势、风控指标、开平仓流水",
            "bg-emerald-500/20 text-emerald-400 border-emerald-500/40 shadow-emerald-500/10",
            "bg-emerald-400",
        ),
        (
            "playbook",
            playbook_url,
            "📖",
            "二段爆发战法",
            "战法解析与实战规则手册",
            "bg-amber-500/20 text-amber-400 border-amber-500/40 shadow-amber-500/10",
            "bg-amber-400",
        ),
    )

    items: list[str] = []
    for key, url, icon, title, hint, active_classes, dot_color in tab_configs:
        if key == page_norm:
            items.append(
                f'<a href="{url}" title="{hint}" aria-current="page" '
                f'class="px-2.5 sm:px-3 py-1.5 text-xs font-bold rounded-lg border shadow-sm {active_classes} flex items-center gap-1.5 transition-all">'
                f'<span class="w-1.5 h-1.5 rounded-full {dot_color} animate-pulse" aria-hidden="true"></span>'
                f'<span>{icon} {title}</span>'
                f'</a>'
            )
        else:
            items.append(
                f'<a href="{url}" title="{hint}" '
                f'class="px-2.5 sm:px-3 py-1.5 text-xs font-semibold rounded-lg text-slate-300 hover:text-white hover:bg-slate-800/80 border border-transparent hover:border-slate-700/60 flex items-center gap-1.5 transition-all">'
                f'<span class="w-1.5 h-1.5 rounded-full bg-slate-600" aria-hidden="true"></span>'
                f'<span>{icon} {title}</span>'
                f'</a>'
            )

    return (
        '<nav aria-label="全系统核心视图导航" class="flex flex-wrap items-center p-1 bg-slate-950/80 border border-slate-800 rounded-xl gap-1 shrink-0">'
        + "".join(items)
        + "</nav>"
    )


def render_global_header(
    current_page: str = "index",
    title: str = "突破追多与移动保本系统",
    title_badge: str = "v3.5 Pro",
    badges_html: str = "",
    extra_actions_html: str = "",
    settle_countdown_id: str = "settle-countdown",
    sync_badge_text: str = "币安 60 秒轮询",
    sync_badge_id: str = "sync-status-badge",
) -> str:
    """生成标准化暗色 HUD 全局顶部导航栏。"""
    nav_tabs_html = render_global_nav_tabs(current_page=current_page)
    safe_title = html.escape(title)
    safe_title_badge = html.escape(title_badge)

    sync_badge = ""
    if sync_badge_text:
        safe_sync_id = html.escape(sync_badge_id, quote=True)
        safe_sync_text = html.escape(sync_badge_text)
        pulse_anim = "animate-ping" if "轮询" in sync_badge_text else "animate-pulse"
        sync_badge = (
            f'<span id="{safe_sync_id}" class="px-2 py-0.5 text-[11px] font-semibold rounded-md bg-emerald-500/10 text-emerald-300 border border-emerald-500/20 flex items-center gap-1.5" role="status" aria-live="polite">'
            f'<span class="w-1.5 h-1.5 rounded-full bg-emerald-400 {pulse_anim}" aria-hidden="true"></span>'
            f'<span>{safe_sync_text}</span>'
            f'</span>'
        )

    settle_badge = ""
    if settle_countdown_id:
        safe_settle_id = html.escape(settle_countdown_id, quote=True)
        settle_badge = (
            '<span id="settle-timer-badge" class="px-2 py-0.5 text-[11px] font-mono font-semibold rounded-md bg-rose-500/10 text-rose-300 border border-rose-500/20 flex items-center gap-1" title="距离下个整点资费结算窗口倒计时">'
            '<span>⏱️ 结算:</span>'
            f'<span id="{safe_settle_id}">--:--</span>'
            '</span>'
        )

    title_badge_html = ""
    if title_badge:
        title_badge_html = f'<span class="text-[10px] px-1.5 py-0.5 rounded font-mono font-normal bg-slate-800 text-slate-300 border border-slate-700">{safe_title_badge}</span>'

    actions_html = ""
    if extra_actions_html:
        actions_html = f'<div class="flex items-center gap-1.5 shrink-0">{extra_actions_html}</div>'

    return f"""    <!-- Global HUD Header Nav -->
    <header class="bg-slate-900/95 border border-slate-800 rounded-xl px-3.5 py-2.5 shadow-lg backdrop-blur-sm">
      <div class="flex flex-col lg:flex-row lg:items-center justify-between gap-2.5">
        <div class="flex flex-wrap items-center gap-2">
          <h1 class="text-lg md:text-xl font-black tracking-tight text-white flex items-center gap-1.5 mr-1">
            <span>{safe_title}</span>
            {title_badge_html}
          </h1>
          {badges_html}
          {sync_badge}
          {settle_badge}
        </div>

        <div class="flex flex-wrap items-center gap-2 shrink-0">
          {nav_tabs_html}
          {actions_html}
        </div>
      </div>
    </header>"""


def render_index_shell_html() -> str:
    """生成纯静态策略监控看板 HTML Shell（数据由 static/candidates.js 异步拉取渲染）。"""
    header_html = render_global_header(
        current_page="index",
        title="突破追多与移动保本系统",
        title_badge="v3.5 Pro",
        badges_html=(
            '<span class="px-2 py-0.5 text-[11px] font-bold rounded-md bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">CMC Pro &lt;$30M</span>'
            '<span id="pool-updated-time" class="px-2 py-0.5 text-[11px] font-mono text-slate-400 bg-slate-800/80 rounded-md border border-slate-700/60">候选池更新: --</span>'
        ),
        extra_actions_html=(
            '<button id="btn-manual-sync" type="button" class="px-2.5 py-1.5 text-xs font-bold rounded-lg bg-slate-800 hover:bg-slate-700 text-sky-400 border border-slate-700 transition-all flex items-center gap-1 focus-visible:outline-2 focus-visible:outline-sky-400" title="立即手动刷新全池价格与资金费率">'
            '<span aria-hidden="true">🔄</span>'
            '<span class="hidden sm:inline">刷新</span>'
            '</button>'
            '<button id="btn-toggle-drawer" type="button" class="px-2.5 py-1.5 text-xs font-bold rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 transition-all flex items-center gap-1" title="展开/收起 TV 趋势线战法与风控速查抽屉">'
            '<span>📐 <span class="hidden sm:inline">TV </span>画线速查</span>'
            '</button>'
        ),
        settle_countdown_id="settle-countdown",
        sync_badge_text="币安 60 秒轮询",
        sync_badge_id="sync-status-badge",
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="color-scheme" content="dark">
  <title>突破追多与移动保本系统 —— 实战工作台 v3.5</title>
  <script src="https://www.gstatic.com/antigravity/web/dev/tailwindcss.min.js"></script>
  <link rel="stylesheet" href="static/styles.css">
  <script src="static/candidates.js" defer></script>
</head>
<body class="p-2 md:p-3.5 antialiased custom-scrollbar">
  <main id="main-content" class="w-full max-w-[1600px] mx-auto space-y-2">

{header_html}

    <!-- Collapsible TV Trendline & Risk Management Quick Reference -->
    <div id="tv-drawer" class="hidden bg-slate-900/90 border border-amber-500/30 rounded-xl p-3 text-xs space-y-2.5 transition-all">
      <div class="flex items-center justify-between border-b border-slate-800 pb-1.5">
        <div class="flex items-center gap-2 flex-wrap">
          <span class="text-amber-400 font-bold text-xs">📐 突破追多与移动保本实战四步法速查与执行纪律</span>
          <span class="text-slate-400 font-mono text-[11px]">(TV 快捷键: 对数坐标 <kbd class="px-1 py-0.5 bg-slate-800 rounded border border-slate-700">Alt + L</kbd> · 趋势线 <kbd class="px-1 py-0.5 bg-slate-800 rounded border border-slate-700">Alt + T</kbd> · 周期秒切 <kbd class="px-1 py-0.5 bg-slate-800 rounded border border-slate-700">15</kbd>/<kbd class="px-1 py-0.5 bg-slate-800 rounded border border-slate-700">60</kbd>)</span>
        </div>
        <button id="btn-close-drawer" type="button" class="text-slate-400 hover:text-white text-sm">✕</button>
      </div>
      <div class="grid grid-cols-1 md:grid-cols-4 gap-2 text-[11px] leading-relaxed">
        <div class="p-2.5 bg-slate-950/60 rounded-lg border border-slate-800 space-y-0.5">
          <span class="text-sky-400 font-bold block">1. 标的初选 (双核共振)</span>
          <span class="text-slate-400 block">CMC Pro 市值 &lt; $30M + 1h EMA 顺势 (5&gt;10) + 24h 换手 ≥ 1.5x，负费率逼空优先。新币进入共振区由 Server 酱通知画线。</span>
        </div>
        <div class="p-2.5 bg-slate-950/60 rounded-lg border border-slate-800 space-y-0.5">
          <span class="text-emerald-400 font-bold block">2. TV 画线 (对数下降线)</span>
          <span class="text-slate-400 block">在 TV 专属布局 ig0LuDL8 开启对数坐标，连接关键回踩高点画 15m 下降趋势线。系统自动云端读取并高频追踪。</span>
        </div>
        <div class="p-2.5 bg-slate-950/60 rounded-lg border border-slate-800 space-y-0.5">
          <span class="text-indigo-400 font-bold block">3. 突破开多 (15m 收盘确认)</span>
          <span class="text-slate-400 block">15m K 线实体收盘价格站上对数趋势线上方，Server 酱即时推送提醒直接开多下注入场。</span>
        </div>
        <div class="p-2.5 bg-slate-950/60 rounded-lg border border-slate-800 space-y-0.5">
          <span class="text-amber-400 font-bold block">4. 止损与锁利 (币安持仓自动风控)</span>
          <span class="text-slate-400 block">15m 收盘跌回趋势线下方无条件清仓止损；涨幅 ≥ +6% 后记录 +1% 锁利提醒线，触及时发送通知。</span>
        </div>
      </div>
    </div>

    <!-- Compact Interactive KPI Strip: 5 Clickable Filtering Cards -->
    <section aria-label="交易机会区交互式卡片仪表盘" class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-2 text-xs">
      <div data-zone="resonance" class="kpi-card card-resonance active px-3 py-2 rounded-xl bg-slate-900 border border-amber-500/40 flex flex-col justify-between space-y-1">
        <div class="flex items-center justify-between gap-1">
          <span class="font-bold text-amber-300 text-xs truncate">👑 顺势高换手共振区</span>
          <span class="px-1.5 py-0.2 rounded text-[9px] font-mono font-bold bg-amber-500/20 text-amber-300 border border-amber-500/30 shrink-0">破线开多</span>
        </div>
        <div class="flex items-baseline justify-between gap-2">
          <div class="text-xl font-black font-mono text-white tabular-nums"><span id="kpi-resonance-count">--</span> <span class="text-[11px] font-normal text-slate-400 font-sans">标的</span></div>
          <div id="kpi-resonance-sub" class="text-[10px] text-amber-400/90 font-mono truncate">--</div>
        </div>
        <div id="kpi-resonance-sample" class="text-[10px] text-slate-400 border-t border-slate-800/80 pt-1 truncate">
          代表: --
        </div>
      </div>

      <div data-zone="neg_fr" class="kpi-card card-neg-fr px-3 py-2 rounded-xl bg-slate-900 border border-slate-800 hover:border-rose-500/40 flex flex-col justify-between space-y-1">
        <div class="flex items-center justify-between gap-1">
          <span class="font-semibold text-rose-400 text-xs truncate">⚠️ 负费率贴水区</span>
          <span class="px-1.5 py-0.2 rounded text-[9px] font-mono text-rose-300/80 bg-rose-500/10 border border-rose-500/20 shrink-0">4h&lt;0</span>
        </div>
        <div class="flex items-baseline justify-between gap-2">
          <div class="text-xl font-black font-mono text-white tabular-nums"><span id="kpi-neg-fr-count">--</span> <span class="text-[11px] font-normal text-slate-400 font-sans">标的</span></div>
          <div id="kpi-neg-fr-sub" class="text-[10px] text-rose-400/90 font-mono truncate">空头过度拥挤</div>
        </div>
        <div id="kpi-neg-fr-sample" class="text-[10px] text-slate-400 border-t border-slate-800/80 pt-1 truncate">
          贴水深: --
        </div>
      </div>

      <div data-zone="high_tr" class="kpi-card card-high-tr px-3 py-2 rounded-xl bg-slate-900 border border-slate-800 hover:border-purple-500/40 flex flex-col justify-between space-y-1">
        <div class="flex items-center justify-between gap-1">
          <span class="font-semibold text-purple-400 text-xs truncate">⚡ 巨量高换手区</span>
          <span class="px-1.5 py-0.2 rounded text-[9px] font-mono text-purple-300/80 bg-purple-500/10 border border-purple-500/20 shrink-0">≥1.5x</span>
        </div>
        <div class="flex items-baseline justify-between gap-2">
          <div class="text-xl font-black font-mono text-white tabular-nums"><span id="kpi-high-tr-count">--</span> <span class="text-[11px] font-normal text-slate-400 font-sans">标的</span></div>
          <div id="kpi-high-tr-sub" class="text-[10px] text-purple-400/90 font-mono truncate">筹码充分洗刷</div>
        </div>
        <div id="kpi-high-tr-sample" class="text-[10px] text-slate-400 border-t border-slate-800/80 pt-1 truncate">
          最高: --
        </div>
      </div>

      <div data-zone="ema_bull" class="kpi-card card-ema-bull px-3 py-2 rounded-xl bg-slate-900 border border-slate-800 hover:border-emerald-500/40 flex flex-col justify-between space-y-1">
        <div class="flex items-center justify-between gap-1">
          <span class="font-semibold text-emerald-400 text-xs truncate">📈 1h EMA顺势区</span>
          <span class="px-1.5 py-0.2 rounded text-[9px] font-mono text-emerald-300/80 bg-emerald-500/10 border border-emerald-500/20 shrink-0">5&gt;10</span>
        </div>
        <div class="flex items-baseline justify-between gap-2">
          <div class="text-xl font-black font-mono text-white tabular-nums"><span id="kpi-ema-bull-count">--</span> <span class="text-[11px] font-normal text-slate-400 font-sans">标的</span></div>
          <div id="kpi-ema-bull-sub" class="text-[10px] text-emerald-400/90 font-mono truncate">右侧动能转强</div>
        </div>
        <div id="kpi-ema-bull-sample" class="text-[10px] text-slate-400 border-t border-slate-800/80 pt-1 truncate">
          多头: --
        </div>
      </div>

      <div data-zone="all" class="kpi-card card-all px-3 py-2 rounded-xl bg-slate-900 border border-slate-800 hover:border-sky-500/40 flex flex-col justify-between space-y-1 col-span-2 md:col-span-1">
        <div class="flex items-center justify-between gap-1">
          <span class="font-semibold text-sky-400 text-xs truncate">🌐 全量候选底池</span>
          <span class="px-1.5 py-0.2 rounded text-[9px] font-mono text-sky-300/80 bg-sky-500/10 border border-sky-500/20 shrink-0">&lt;$30M</span>
        </div>
        <div class="flex items-baseline justify-between gap-2">
          <div class="text-xl font-black font-mono text-white tabular-nums"><span id="kpi-all-count">--</span> <span class="text-[11px] font-normal text-slate-400 font-sans">标的</span></div>
          <div id="kpi-all-sub" class="text-[10px] text-sky-400/90 font-mono truncate">--</div>
        </div>
        <div id="kpi-all-sample" class="text-[10px] text-slate-400 border-t border-slate-800/80 pt-1 truncate">
          &lt;3000wu 底池 + 7d 溢出跟踪
        </div>
      </div>
    </section>

    <!-- Candidate Pool & TV Launchpad Table Section -->
    <section aria-labelledby="pool-table-heading" class="bg-slate-900 border border-slate-800 rounded-xl p-2.5 md:p-3 shadow-sm space-y-2">
      <h2 id="pool-table-heading" class="sr-only">候选币池实时行情表格</h2>
      <!-- Execution Discipline Ribbon (Compact) -->
      <div class="bg-gradient-to-r from-amber-950/40 via-slate-900 to-indigo-950/40 px-3 py-1.5 rounded-xl border border-amber-500/30 flex flex-col md:flex-row md:items-center justify-between gap-2 text-[11px]">
        <div class="flex items-center gap-2 flex-wrap">
          <span class="px-1.5 py-0.5 rounded bg-amber-500/20 text-amber-300 font-bold border border-amber-500/30">
            ⚡ 右侧破线下注
          </span>
          <span class="text-slate-300">
            <strong>内核:</strong> 1h EMA 顺势 + 换手 ≥ 1.5x
          </span>
          <span class="text-slate-600">|</span>
          <span class="text-slate-300">
            <strong>触发:</strong> TV 15m 突破下降趋势线（收盘确认）开多
          </span>
        </div>
        <div class="flex items-center gap-1.5 shrink-0 font-mono text-[11px]">
          <span class="text-rose-400 font-bold bg-rose-950/40 px-2 py-0.5 rounded border border-rose-500/30">
            🛡️ 跌回趋势线下收盘 = 止损
          </span>
          <span class="text-emerald-400 font-bold bg-emerald-950/40 px-2 py-0.5 rounded border border-emerald-500/30">
            🎯 盈利随时止盈 / 推保本
          </span>
        </div>
      </div>

      <!-- Search, TV Batch Export & Filter Controls -->
      <div class="flex flex-col lg:flex-row items-stretch lg:items-center justify-between gap-2 bg-slate-950/70 px-2.5 py-1.5 rounded-xl border border-slate-800">
        <div class="flex items-center gap-1.5 flex-wrap">
          <span id="active-zone-pill" class="px-2 py-0.5 rounded-md text-xs font-bold bg-amber-500/20 text-amber-300 border border-amber-500/30 flex items-center gap-1">
            <span>当前筛选: 👑 顺势高换手共振区</span>
          </span>
          <span id="pool-count-badge" class="text-xs font-mono px-2 py-0.5 rounded-md bg-slate-900 text-slate-300 border border-slate-800">
            -- / -- 标的
          </span>

          <div class="flex items-center gap-1.5 border-l border-slate-800 pl-2">
            <div class="relative group">
              <button id="btn-batch-copy-tv" type="button" class="px-2 py-0.5 text-xs font-semibold rounded-md bg-sky-500/15 hover:bg-sky-500/25 text-sky-300 border border-sky-500/30 transition-all flex items-center gap-1 shadow-sm">
                <span>📋 复制 TV 自选</span>
              </button>
              <div class="absolute bottom-full left-1/2 -translate-x-1/2 mb-1.5 hidden group-hover:block z-30 w-56 p-2 bg-slate-800 text-[11px] text-slate-200 rounded-lg shadow-xl border border-slate-700 pointer-events-none text-center">
                💡 复制后在 TradingView 自选列表直接按 Ctrl+V (或 Cmd+V) 即可批量添加！
              </div>
            </div>
            <div class="relative group">
              <button id="btn-batch-export-tv" type="button" class="px-2 py-0.5 text-xs font-semibold rounded-md bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 transition-all flex items-center gap-1">
                <span>💾 导出 .txt</span>
              </button>
              <div class="absolute bottom-full left-1/2 -translate-x-1/2 mb-1.5 hidden group-hover:block z-30 w-60 p-2 bg-slate-800 text-[11px] text-slate-200 rounded-lg shadow-xl border border-slate-700 pointer-events-none text-center">
                💡 在 TradingView 自选列表右上角点击「...」菜单 → 选择「导入列表」导入此 .txt 文件！
              </div>
            </div>
          </div>
        </div>

        <div class="flex items-center gap-1.5">
          <div class="relative shrink-0 w-full sm:w-56">
            <input id="symbol-search-input" type="search" placeholder="搜代号 [/] (如 PIPPIN, ZKC)..." class="w-full px-2.5 py-1 text-xs bg-slate-900 border border-slate-700 rounded-lg text-slate-200 placeholder-slate-500 focus:outline-none focus:border-sky-500 focus:ring-1 focus:ring-sky-500">
            <span class="absolute right-2 top-1.5 text-xs text-slate-500 pointer-events-none">🔍</span>
          </div>
          <button id="btn-reset-filter" type="button" class="px-2.5 py-1 text-xs text-slate-400 hover:text-white bg-slate-800 hover:bg-slate-700 rounded-lg border border-slate-700 transition-all shrink-0" title="重置为全量底池">
            重置
          </button>
        </div>
      </div>

      <!-- Main Candidates Table with Sticky Header & Interactive Sorter -->
      <div tabindex="0" role="region" aria-label="候选币池实时行情表格" class="overflow-x-auto max-h-[calc(100vh-230px)] min-h-[420px] rounded-xl border border-slate-800 focus-visible:outline-2 focus-visible:outline-sky-400 custom-scrollbar">
        <table id="candidates-table" class="w-full text-left text-xs font-mono relative">
          <thead class="sticky-header select-none text-[11px]">
            <tr>
              <th scope="col" class="py-1.5 px-1.5 w-7 text-center text-slate-500 font-sans">#</th>
              <th scope="col" class="py-1.5 px-2 min-w-[125px]">合约代号 &amp; 直达</th>
              <th scope="col" class="py-1.5 px-2 sortable min-w-[95px]" data-sort="price">最新价 &amp; 24h位置<span class="sort-icon">↕</span></th>
              <th scope="col" class="py-1.5 px-2 sortable min-w-[65px]" data-sort="chg">24h 涨跌<span class="sort-icon">↕</span></th>
              <th scope="col" class="py-1.5 px-2 sortable min-w-[70px]" data-sort="mc">流通市值<span class="sort-icon">↕</span></th>
              <th scope="col" class="py-1.5 px-2 sortable min-w-[70px]" data-sort="vol">24h 成交额<span class="sort-icon">↕</span></th>
              <th scope="col" class="py-1.5 px-2 sortable min-w-[60px]" data-sort="tr">换手倍数<span class="sort-icon">↕</span></th>
              <th scope="col" class="py-1.5 px-2 sortable min-w-[95px]" data-sort="fr">4h 换算资费<span class="sort-icon">↕</span></th>
              <th scope="col" class="py-1.5 px-2 font-sans sortable min-w-[110px]" data-sort="ema">1h EMA趋势<span class="sort-icon">↕</span></th>
              <th scope="col" class="py-1.5 px-2 font-sans sortable min-w-[105px]" data-sort="duration">共振时长<span class="sort-icon">↕</span></th>
              <th scope="col" class="py-1.5 px-2 font-sans min-w-[120px]">机会区共振与形态</th>
            </tr>
          </thead>
          <tbody id="candidate-table-body" class="divide-y divide-slate-800/60 text-slate-300">
            <tr id="pool-status-row">
              <td colspan="11" class="py-12 text-center text-slate-400 font-sans">正在加载候选币池数据...</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="flex flex-col sm:flex-row items-center justify-between text-[11px] text-slate-500 pt-1 border-t border-slate-800/60 gap-1">
        <div>
          <span>快捷键: <kbd class="px-1 bg-slate-800 rounded border border-slate-700 font-mono">/</kbd> 搜代号 · <kbd class="px-1 bg-slate-800 rounded border border-slate-700 font-mono">1-5</kbd> 切分区 · <kbd class="px-1 bg-slate-800 rounded border border-slate-700 font-mono">Esc</kbd> 清空/退出</span>
        </div>
        <div>
          <span>CoinMarketCap Pro 官方流通市值 &lt; 3000wu · 币安纯加密永续合约</span>
        </div>
      </div>
    </section>

  </main>

  <!-- In-Page Interactive 15m/1h Candlestick & TV Chart Modal -->
  <div id="chart-modal" class="fixed inset-0 z-50 hidden bg-black/80 backdrop-blur-sm flex items-center justify-center p-2 md:p-5 transition-all">
    <div class="bg-slate-900 border border-slate-700/80 rounded-2xl w-full max-w-5xl h-[85vh] max-h-[720px] shadow-2xl flex flex-col overflow-hidden relative">
      <div class="px-4 py-2.5 bg-slate-950 border-b border-slate-800 flex flex-wrap items-center justify-between gap-2 shrink-0">
        <div class="flex items-center gap-2.5">
          <span class="w-2.5 h-2.5 rounded-full bg-emerald-400 animate-pulse" aria-hidden="true"></span>
          <span id="modal-chart-title" class="font-bold text-white text-base font-sans"></span>
          <span id="modal-chart-price" class="font-mono text-emerald-400 font-bold text-sm">--</span>
          <div class="flex items-center bg-slate-900 p-0.5 rounded-lg border border-slate-800 text-xs font-mono ml-1">
            <button type="button" class="modal-interval-btn px-2 py-0.5 rounded bg-sky-500/20 text-sky-300 font-bold" data-interval="15m" title="15分钟周期 (下注破线战法核心)">15m</button>
            <button type="button" class="modal-interval-btn px-2 py-0.5 rounded hover:bg-slate-800 text-slate-400" data-interval="1h" title="1小时周期 (EMA趋势确认)">1h</button>
            <button type="button" class="modal-interval-btn px-2 py-0.5 rounded hover:bg-slate-800 text-slate-400" data-interval="4h" title="4小时周期 (宏观周期研判)">4h</button>
            <button type="button" class="modal-interval-btn px-2 py-0.5 rounded hover:bg-slate-800 text-slate-400" data-interval="1d" title="日线周期">日线</button>
          </div>
        </div>
        <div class="flex items-center gap-2">
          <div id="modal-ema-hud" class="hidden sm:flex items-center gap-2 text-xs font-mono">
            <span class="flex items-center gap-1 text-amber-400"><span class="w-2 h-0.5 bg-amber-400 inline-block"></span>EMA5: <span id="modal-hud-ema5">--</span></span>
            <span class="flex items-center gap-1 text-purple-400"><span class="w-2 h-0.5 bg-purple-400 inline-block"></span>EMA10: <span id="modal-hud-ema10">--</span></span>
          </div>
          <div class="flex items-center bg-slate-900 p-0.5 rounded-lg border border-slate-800 text-[11px] font-sans">
            <button type="button" id="btn-chart-engine-native" class="px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-300 font-semibold" title="币安直连极速渲染 (免翻墙/零卡顿)">⚡ 极速K线</button>
            <button type="button" id="btn-chart-engine-tv" class="px-2 py-0.5 rounded hover:bg-slate-800 text-slate-400" title="TradingView内嵌组件 (需自备海外网络代理)">🌐 TV内嵌</button>
          </div>
          <a id="modal-external-link" href="#" target="_blank" rel="noopener noreferrer" class="px-2.5 py-1 rounded-lg bg-[#2962FF] hover:bg-blue-600 text-white text-xs font-semibold flex items-center gap-1 transition-all" title="在 TradingView 打开专属画线布局 (ig0LuDL8)">
            <span>TV画线</span>
            <span aria-hidden="true">↗</span>
          </a>
          <button id="modal-close-btn" type="button" class="w-7 h-7 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-400 hover:text-white flex items-center justify-center text-xs transition-all" title="关闭 (Esc)">
            ✕
          </button>
        </div>
      </div>
      <div class="flex-1 w-full h-full bg-[#0b0f19] relative overflow-hidden">
        <div id="modal-native-chart-view" class="w-full h-full relative select-none">
          <canvas id="modal-kline-canvas" class="w-full h-full block"></canvas>
          <div id="modal-chart-tooltip" class="absolute top-2 left-3 bg-slate-900/90 border border-slate-700/80 px-2.5 py-1.5 rounded text-[11px] font-mono text-slate-300 hidden pointer-events-none shadow-xl z-20"></div>
          <div id="modal-chart-loading" class="absolute inset-0 bg-slate-950/70 flex flex-col items-center justify-center gap-2 text-sky-400 text-sm font-semibold z-30">
            <span class="animate-spin text-2xl">⏳</span>
            <span>正在直连币安获取实时 K 线...</span>
          </div>
          <div id="modal-chart-error" class="absolute inset-0 bg-slate-950/80 hidden flex-col items-center justify-center gap-3 text-rose-400 text-sm font-semibold z-30">
            <span>⚠️ K线数据加载失败，请检查网络连接</span>
            <button type="button" id="modal-chart-retry-btn" class="px-3 py-1 bg-slate-800 hover:bg-slate-700 text-white rounded border border-slate-700 text-xs">重新加载</button>
          </div>
        </div>
        <div id="modal-tv-view" class="w-full h-full hidden relative bg-black">
          <iframe id="modal-tv-iframe" class="w-full h-full border-0" allowtransparency="true" scrolling="no" allowfullscreen></iframe>
        </div>
      </div>
    </div>
  </div>

  <!-- Floating Toast Notification -->
  <div id="toast-container">
    <div id="toast" class="fixed bottom-6 right-6 z-50 hidden px-4 py-2.5 rounded-xl bg-slate-800 border border-slate-700 text-slate-200 text-xs shadow-2xl flex items-center gap-2">
      <span class="text-emerald-400 font-bold">✓</span>
      <span id="toast-text">已复制</span>
    </div>
  </div>
</body>
</html>
"""


def render_playbook_html() -> str:
    """生成纯静态战法解析与风控规则手册 HTML。"""
    playbook_header = render_global_header(
        current_page="playbook",
        title="突破追多与移动保本战法手册",
        title_badge="v3.5 Pro",
        badges_html=(
            '<span class="px-2 py-0.5 text-[11px] font-semibold rounded-md bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">CMC Pro &lt;$30M</span>'
            '<span class="px-2 py-0.5 text-[11px] font-semibold rounded-md bg-purple-500/10 text-purple-400 border border-purple-500/20">4 步战法 + 移动锁利风控</span>'
        ),
        settle_countdown_id="",
        sync_badge_text="",
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
  <meta name="color-scheme" content="dark">
  <title>突破追多与移动保本战法手册 v3.5</title>
  <script src="https://www.gstatic.com/antigravity/web/dev/tailwindcss.min.js"></script>
  <link rel="stylesheet" href="../static/styles.css">
</head>
<body class="p-2 md:p-3.5 antialiased custom-scrollbar">
  <div class="w-full max-w-[1600px] mx-auto space-y-2.5">

{playbook_header}

    <!-- Compact 3 Pillars -->
    <div class="grid grid-cols-1 md:grid-cols-3 gap-2 text-xs leading-snug">
      <div class="px-3 py-2 bg-slate-900 border border-slate-800 rounded-xl">
        <span class="font-bold text-rose-400">1. 放弃第一段：</span>
        <span class="text-slate-400">第一波拉升突发无序，散户追入多在高位见顶段，易遭断崖套牢。</span>
      </div>
      <div class="px-3 py-2 bg-slate-900 border border-slate-800 rounded-xl">
        <span class="font-bold text-amber-400">2. 不抄初跌：</span>
        <span class="text-slate-400">破位急跌属去杠杆踩踏，绝不接飞刀，须等量能萎缩进入收敛通道。</span>
      </div>
      <div class="px-3 py-2 bg-emerald-950/15 border border-emerald-500/40 rounded-xl">
        <span class="font-bold text-emerald-400">3. 突破追多：</span>
        <span class="text-slate-300">1h EMA顺势 + 巨量高换手共振，15m突破下降趋势线开多，极窄止损博弈翻倍主升。</span>
      </div>
    </div>

    <!-- Step 2 & 3: Modern 4-Step Execution & Automated Binance Risk Control Matrix -->
    <div class="grid grid-cols-1 md:grid-cols-2 gap-3 text-xs">
      <div class="bg-slate-900 border border-slate-800 rounded-xl p-4 space-y-2.5">
        <h3 class="text-sm font-bold text-white flex items-center justify-between border-b border-slate-800 pb-2">
          <span class="flex items-center gap-1.5">🎯 突破追多与移动保本实战四步法执行体系</span>
          <span class="text-[11px] font-mono text-emerald-400">TV 对数画线 · 突破开多</span>
        </h3>
        <div class="space-y-1.5">
          <div class="p-2 bg-slate-950/60 rounded-lg border border-slate-800/80">
            <div class="flex items-center justify-between font-semibold">
              <span class="text-sky-400">步骤 ①: 标的池初选 (双核共振)</span>
              <span class="text-slate-400 font-mono text-[10px]">Server酱通知画线</span>
            </div>
            <p class="text-slate-400 mt-0.5 text-[11px]">
              CMC Pro 官方流通市值 &lt; 3000wu，纯加密永续合约。1h EMA 顺势 (EMA5 &gt; EMA10) + 24h 换手率 ≥ 1.5x。新币进入共振区立即通过 Server 酱预警，提醒去 TV 上画下降趋势线。
            </p>
          </div>
          <div class="p-2 bg-slate-950/60 rounded-lg border border-slate-800/80">
            <div class="flex items-center justify-between font-semibold">
              <span class="text-emerald-400">步骤 ②: TV 对数画线</span>
              <span class="text-slate-400 font-mono text-[10px]">TV 布局 ig0LuDL8</span>
            </div>
            <p class="text-slate-400 mt-0.5 text-[11px]">
              点击直达 TV 专属云端图表，开启对数坐标 (Alt+L)，在 15m 周期连接回踩高点绘制下降趋势线。守护进程在收盘检查或新持仓匹配时读取画线。
            </p>
          </div>
          <div class="p-2 bg-slate-950/60 rounded-lg border border-slate-800/80">
            <div class="flex items-center justify-between font-semibold">
              <span class="text-indigo-400">步骤 ③: 15m 实体突破开多</span>
              <span class="text-emerald-400 font-mono text-[10px]">15m收盘站上趋势线</span>
            </div>
            <p class="text-slate-400 mt-0.5 text-[11px]">
              等待 15m K 线实体收盘价格严格站上对数趋势线上方。守护进程检测到闭合突破后立即通过 Server 酱推送买入信号，执行开多下注。
            </p>
          </div>
          <div class="p-2 bg-slate-950/60 rounded-lg border border-slate-800/80">
            <div class="flex items-center justify-between font-semibold">
              <span class="text-amber-400">步骤 ④: 严格止损与移动保本</span>
              <span class="text-amber-300 font-mono text-[10px]">破线止损 · +6%保本锁利</span>
            </div>
            <p class="text-slate-400 mt-0.5 text-[11px]">
              入场后若 15m 收盘跌回趋势线下方无条件清仓止损；当浮盈达到 +6% 后，系统记录开仓价 +1% 的锁利提醒线，触及时发送通知。
            </p>
          </div>
        </div>
      </div>

      <div class="bg-slate-900 border border-slate-800 rounded-xl p-4 space-y-2.5">
        <h3 class="text-sm font-bold text-white flex items-center justify-between border-b border-slate-800 pb-2">
          <span class="flex items-center gap-1.5">🛡️ 币安实盘持仓联动与智能风控守护</span>
          <span class="text-[11px] font-mono text-amber-300">API 自动感知 · 15m 收盘闭环</span>
        </h3>
        <div class="space-y-2">
          <div class="p-2.5 bg-slate-950/60 rounded-lg border border-slate-800/80 space-y-1">
            <div class="flex items-center justify-between text-xs font-semibold">
              <span class="text-sky-400">1. 实盘持仓自动对齐 (Binance Futures API)</span>
              <span class="text-emerald-400 font-mono">HMAC-SHA256</span>
            </div>
            <p class="text-slate-400 text-[11px] leading-relaxed">
              系统通过币安合约 API 自动读取最新持仓（代币符号、持仓张数、入场均价与未实现盈亏），无需手动登记维护持仓，实现零摩擦持仓监控。
            </p>
          </div>

          <div class="p-2.5 bg-slate-950/60 rounded-lg border border-rose-500/20 bg-rose-950/10 space-y-1">
            <div class="flex items-center justify-between text-xs font-semibold">
              <span class="text-rose-400">2. 破线清仓铁律 (硬止损 &lt; 1.5%)</span>
              <span class="text-rose-300 font-mono">15m 收盘价 &lt; 趋势线</span>
            </div>
            <p class="text-slate-400 text-[11px] leading-relaxed">
              每个 15m 柱收盘，守护进程比对当前收盘价与对数趋势线阻力价。一旦收盘跌破趋势线，立即触发 Server 酱高危预警提示清仓止损，杜绝深套。
            </p>
          </div>

          <div class="p-2.5 bg-slate-950/60 rounded-lg border border-emerald-500/20 bg-emerald-950/10 space-y-1">
            <div class="flex items-center justify-between text-xs font-semibold">
              <span class="text-emerald-400">3. 浮盈 +6% 移动保本锁利</span>
              <span class="text-emerald-300 font-mono">止损提至 +1.0%</span>
            </div>
            <p class="text-slate-400 text-[11px] leading-relaxed">
              当标的涨幅相比开仓均价拉开 ≥ +6% 安全垫时，系统记录 +1% 锁利提醒线，价格触及时发送离场通知。
            </p>
          </div>
        </div>
      </div>
    </div>

  </div>

</body>
</html>
"""
