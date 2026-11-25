#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
账户数据可视化静态外壳构建脚本 (Account Analytics Static Shell Builder)
==============================================================================
交付路径: account.html
职责:
- 生成纯静态 HTML 外壳 (链接 static/styles.css、Chart.js 与 static/account.js)
- 不内联 CSS/JS 或私有账户 JSON 数据，所有动态数据由浏览器从同源 api/account 拉取渲染
"""

import os
import sys
import time
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import DEFAULT_TV_LAYOUT_ID  # noqa: E402
from hud_common import render_global_header  # noqa: E402

OUTPUT_HTML = PROJECT_ROOT / "account.html"
TV_LAYOUT_ID = DEFAULT_TV_LAYOUT_ID


def render_global_nav_header() -> str:
    """渲染与全站统一的全局 HUD Header 导航栏。"""
    badges = (
        '<span class="px-2 py-0.5 text-[11px] font-bold rounded-md bg-sky-500/10 text-sky-400 '
        'border border-sky-500/20 flex items-center gap-1">'
        '<span>📊</span><span>账户数据与量化风控看板</span>'
        '</span>'
        '<span id="sync-time-badge" class="px-2 py-0.5 text-[11px] font-mono text-slate-400 '
        'bg-slate-800/80 rounded-md border border-slate-700/60 flex items-center gap-1">'
        '<span>⏱️ 同步:</span><span id="sync-time-text">--</span>'
        '</span>'
    )
    extra_actions = (
        '<button id="btn-manual-refresh" type="button" '
        'class="px-2.5 py-1.5 text-xs font-bold rounded-lg bg-slate-800 hover:bg-slate-700 text-sky-400 '
        'border border-slate-700 transition-all flex items-center gap-1.5 '
        'focus-visible:outline-2 focus-visible:outline-sky-400" title="重新获取账户最新资产与流水数据">'
        '<span id="refresh-spinner" class="inline-block transition-transform duration-300">🔄</span>'
        '<span>刷新</span>'
        '</button>'
        f'<a href="https://www.tradingview.com/chart/{TV_LAYOUT_ID}/" target="_blank" rel="noopener noreferrer" '
        'class="px-2.5 py-1.5 text-xs font-bold rounded-lg bg-indigo-500/15 hover:bg-indigo-500/25 '
        'text-indigo-300 border border-indigo-500/30 transition-all flex items-center gap-1" '
        'title="跳转 TradingView 专属画线图表">'
        '<span>📐 TV 云端布局</span>'
        '</a>'
    )
    return render_global_header(
        current_page="account",
        title="突破追多与移动保本系统",
        title_badge="v3.5 Pro",
        badges_html=badges,
        extra_actions_html=extra_actions,
        settle_countdown_id="",
        sync_badge_text="等待账户数据",
        sync_badge_id="api-status-badge",
    )


def render_discipline_ribbon() -> str:
    """渲染量化风控纪律说明横幅。"""
    return """    <!-- Risk Control & Execution Ribbon -->
    <div class="bg-gradient-to-r from-slate-900 via-indigo-950/40 to-slate-900 px-3.5 py-1.5 rounded-xl border border-indigo-500/30 flex flex-col md:flex-row md:items-center justify-between gap-2 text-[11px]">
      <div class="flex items-center gap-2 flex-wrap">
        <span class="px-1.5 py-0.5 rounded bg-indigo-500/20 text-indigo-300 font-bold border border-indigo-500/30">
          🛡️ 铁律风控体系
        </span>
        <span class="text-slate-300">
          <strong>单笔风险:</strong> 严格控制 ≤ 1.5% 本金
        </span>
        <span class="text-slate-600">|</span>
        <span class="text-slate-300">
          <strong>保本锁利:</strong> 浮盈触及 +6% 后设立 +1% 锁利提醒线
        </span>
        <span class="text-slate-600">|</span>
        <span class="text-slate-300">
          <strong>坚决斩仓:</strong> 15m 收盘跌破趋势线支撑时发送止损提醒
        </span>
      </div>
      <div class="flex items-center gap-2 shrink-0 font-mono text-[11px]">
        <span class="text-rose-400 font-bold bg-rose-950/40 px-2 py-0.5 rounded border border-rose-500/30">
          回撤硬红线: -5.0%
        </span>
      </div>
    </div>"""


def render_kpi_shell() -> str:
    """渲染 10 联排 KPI 矩阵静态外壳（由 static/account.js 填充真实数值）。"""
    return """    <!-- Top KPI Quantitative Performance Matrix -->
    <section id="kpi-matrix-section" aria-label="核心资产与风控绩效矩阵" class="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-5 gap-2 text-xs">

      <!-- Card 1: 账户总资产 (Equity) -->
      <div class="kpi-card px-3.5 py-2.5 rounded-xl bg-slate-900/90 border border-slate-800 hover:border-sky-500/40 transition-all flex flex-col justify-between">
        <div class="flex items-center justify-between text-slate-400">
          <span class="font-medium text-[11px]">账户总资产 (Equity)</span>
          <span class="px-1.5 py-0.2 rounded text-[10px] font-mono bg-sky-500/10 text-sky-400 border border-sky-500/20">USDT</span>
        </div>
        <div class="my-1">
          <div id="kpi-total-equity" class="text-xl md:text-2xl font-black font-mono text-white tabular-nums tracking-tight">--</div>
        </div>
        <div class="text-[10px] font-mono flex items-center justify-between text-slate-400 border-t border-slate-800/80 pt-1">
          <span id="kpi-initial-capital">本金: --</span>
          <span id="kpi-total-return" class="text-slate-300 font-bold">--</span>
        </div>
      </div>

      <!-- Card 2: 可用保证金 (Available Margin) -->
      <div class="kpi-card px-3.5 py-2.5 rounded-xl bg-slate-900/90 border border-slate-800 hover:border-sky-500/40 transition-all flex flex-col justify-between">
        <div class="flex items-center justify-between text-slate-400">
          <span class="font-medium text-[11px]">可用保证金 (Margin)</span>
          <span id="kpi-margin-badge" class="px-1.5 py-0.2 rounded text-[10px] font-mono bg-slate-800 text-slate-300 border border-slate-700">--</span>
        </div>
        <div class="my-1">
          <div id="kpi-available-margin" class="text-xl md:text-2xl font-black font-mono text-white tabular-nums tracking-tight">--</div>
        </div>
        <div class="text-[10px] font-mono flex items-center justify-between text-slate-400 border-t border-slate-800/80 pt-1">
          <span>占用保证金</span>
          <span id="kpi-position-margin" class="text-slate-300">--</span>
        </div>
      </div>

      <!-- Card 3: 累计净实现盈亏 (Net Realized PnL) -->
      <div class="kpi-card px-3.5 py-2.5 rounded-xl bg-slate-900/90 border border-slate-800 hover:border-emerald-500/40 transition-all flex flex-col justify-between">
        <div class="flex items-center justify-between text-slate-400">
          <span class="font-medium text-[11px]">累计净盈亏 (Realized)</span>
          <span class="px-1.5 py-0.2 rounded text-[10px] font-mono bg-slate-800 text-slate-300">扣费净值</span>
        </div>
        <div class="my-1">
          <div id="kpi-net-realized-pnl" class="text-xl md:text-2xl font-black font-mono text-white tabular-nums tracking-tight">--</div>
        </div>
        <div class="text-[10px] font-mono flex items-center justify-between text-slate-400 border-t border-slate-800/80 pt-1 gap-2">
          <span id="kpi-total-fee">手续费: --</span>
          <span id="kpi-funding-fee">资费: --</span>
        </div>
      </div>

      <!-- Card 4: 未结浮动盈亏 (Unrealized PnL) -->
      <div class="kpi-card px-3.5 py-2.5 rounded-xl bg-slate-900/90 border border-slate-800 hover:border-amber-500/40 transition-all flex flex-col justify-between">
        <div class="flex items-center justify-between text-slate-400">
          <span class="font-medium text-[11px]">未结浮盈 (Unrealized)</span>
          <span class="px-1.5 py-0.2 rounded text-[10px] font-mono bg-indigo-500/10 text-indigo-300">实时敞口</span>
        </div>
        <div class="my-1">
          <div id="kpi-unrealized-pnl" class="text-xl md:text-2xl font-black font-mono text-white tabular-nums tracking-tight">--</div>
        </div>
        <div class="text-[10px] font-mono flex items-center justify-between text-slate-400 border-t border-slate-800/80 pt-1">
          <span>活跃持仓</span>
          <span id="kpi-open-trades" class="text-slate-300 font-bold">--</span>
        </div>
      </div>

      <!-- Card 5: 战法胜率 (Win Rate %) -->
      <div class="kpi-card px-3.5 py-2.5 rounded-xl bg-slate-900/90 border border-slate-800 hover:border-amber-500/40 transition-all flex flex-col justify-between">
        <div class="flex items-center justify-between text-slate-400">
          <span class="font-medium text-[11px]">战法胜率 (Win Rate)</span>
          <span id="kpi-total-trades" class="px-1.5 py-0.2 rounded text-[10px] font-mono bg-amber-500/10 text-amber-300 border border-amber-500/20">--</span>
        </div>
        <div class="my-1">
          <div id="kpi-win-rate" class="text-xl md:text-2xl font-black font-mono text-amber-300 tabular-nums tracking-tight">--</div>
        </div>
        <div class="text-[10px] font-mono flex items-center justify-between text-slate-400 border-t border-slate-800/80 pt-1">
          <span>胜 / 负 / 平</span>
          <span id="kpi-wl-record" class="text-slate-300">--</span>
        </div>
      </div>

      <!-- Card 6: 盈亏比 (Profit Factor) -->
      <div class="kpi-card px-3.5 py-2.5 rounded-xl bg-slate-900/90 border border-slate-800 hover:border-sky-500/40 transition-all flex flex-col justify-between">
        <div class="flex items-center justify-between text-slate-400">
          <span class="font-medium text-[11px]">盈亏比 (Profit Factor)</span>
          <span class="px-1.5 py-0.2 rounded text-[10px] font-mono bg-sky-500/10 text-sky-400">净盈亏口径</span>
        </div>
        <div class="my-1">
          <div id="kpi-profit-factor" class="text-xl md:text-2xl font-black font-mono text-sky-300 tabular-nums tracking-tight">--</div>
        </div>
        <div class="text-[10px] font-mono flex items-center justify-between text-slate-400 border-t border-slate-800/80 pt-1 gap-2">
          <span id="kpi-avg-win" class="text-emerald-400">均盈: --</span>
          <span id="kpi-avg-loss" class="text-rose-400">均亏: --</span>
        </div>
      </div>

      <!-- Card 7: 最大回撤 (Max Drawdown) -->
      <div class="kpi-card px-3.5 py-2.5 rounded-xl bg-slate-900/90 border border-slate-800 hover:border-rose-500/40 transition-all flex flex-col justify-between">
        <div class="flex items-center justify-between text-slate-400">
          <span class="font-medium text-[11px]">最大回撤 (Max Drawdown)</span>
          <span class="px-1.5 py-0.2 rounded text-[10px] font-mono bg-rose-500/10 text-rose-400">极值约束</span>
        </div>
        <div class="my-1">
          <div id="kpi-max-dd-pct" class="text-xl md:text-2xl font-black font-mono text-rose-400 tabular-nums tracking-tight">--</div>
        </div>
        <div class="text-[10px] font-mono flex items-center justify-between text-slate-400 border-t border-slate-800/80 pt-1">
          <span>峰值回撤金额</span>
          <span id="kpi-max-dd-usd" class="text-rose-400 font-bold">--</span>
        </div>
      </div>

      <!-- Card 8: 夏普比率 (Sharpe Ratio) -->
      <div class="kpi-card px-3.5 py-2.5 rounded-xl bg-slate-900/90 border border-slate-800 hover:border-indigo-500/40 transition-all flex flex-col justify-between">
        <div class="flex items-center justify-between text-slate-400">
          <span class="font-medium text-[11px]">夏普比率 (Sharpe)</span>
          <span class="px-1.5 py-0.2 rounded text-[10px] font-mono bg-indigo-500/10 text-indigo-300">年化风险收益</span>
        </div>
        <div class="my-1">
          <div id="kpi-sharpe" class="text-xl md:text-2xl font-black font-mono text-indigo-300 tabular-nums tracking-tight">--</div>
        </div>
        <div class="text-[10px] font-mono flex items-center justify-between text-slate-400 border-t border-slate-800/80 pt-1">
          <span>评级</span>
          <span id="kpi-sharpe-rating" class="text-slate-300 font-bold">--</span>
        </div>
      </div>

      <!-- Card 9: 索提诺比率 (Sortino Ratio) -->
      <div class="kpi-card px-3.5 py-2.5 rounded-xl bg-slate-900/90 border border-slate-800 hover:border-purple-500/40 transition-all flex flex-col justify-between">
        <div class="flex items-center justify-between text-slate-400">
          <span class="font-medium text-[11px]">索提诺比率 (Sortino)</span>
          <span class="px-1.5 py-0.2 rounded text-[10px] font-mono bg-purple-500/10 text-purple-300">下行惩罚</span>
        </div>
        <div class="my-1">
          <div id="kpi-sortino" class="text-xl md:text-2xl font-black font-mono text-purple-300 tabular-nums tracking-tight">--</div>
        </div>
        <div class="text-[10px] font-mono flex items-center justify-between text-slate-400 border-t border-slate-800/80 pt-1">
          <span>下行控制</span>
          <span id="kpi-sortino-rating" class="text-slate-300 font-bold">--</span>
        </div>
      </div>

      <!-- Card 10: 连胜连亏与平均持仓 -->
      <div class="kpi-card px-3.5 py-2.5 rounded-xl bg-slate-900/90 border border-slate-800 hover:border-slate-700 transition-all flex flex-col justify-between">
        <div class="flex items-center justify-between text-slate-400">
          <span class="font-medium text-[11px]">连胜 / 连亏 (Streak)</span>
          <span class="px-1.5 py-0.2 rounded text-[10px] font-mono bg-slate-800 text-slate-300">交易节奏</span>
        </div>
        <div class="my-1">
          <div id="kpi-max-streak" class="text-xl md:text-2xl font-black font-mono text-white tabular-nums tracking-tight">--</div>
        </div>
        <div class="text-[10px] font-mono flex items-center justify-between text-slate-400 border-t border-slate-800/80 pt-1">
          <span>平均持仓周期</span>
          <span id="kpi-avg-hold" class="text-slate-300">--</span>
        </div>
      </div>

    </section>"""


def render_charts_shell() -> str:
    """渲染 4 大图表 canvas 静态容器。"""
    return """    <!-- Interactive Visual Analytics Charts (2x2 Grid) -->
    <section aria-label="核心量化分析可视化图表" class="grid grid-cols-1 lg:grid-cols-2 gap-2.5">

      <!-- Chart 1: 资金净值走势曲线 (Equity Curve) -->
      <div class="bg-slate-900/90 border border-slate-800 rounded-xl p-3.5 shadow-lg flex flex-col justify-between">
        <div class="flex flex-wrap items-center justify-between gap-2 border-b border-slate-800/80 pb-2 mb-2">
          <div class="flex items-center gap-2">
            <span class="text-sky-400 text-sm font-bold flex items-center gap-1.5">
              <span>📈</span>
              <span>资金净值走势 (Equity Curve)</span>
            </span>
            <span id="equity-range-label" class="text-[10px] text-slate-400 font-mono">--</span>
          </div>
          <div class="flex items-center gap-2 text-[10px] font-mono">
            <span id="equity-peak-label" class="px-2 py-0.5 rounded bg-slate-800 text-slate-300 border border-slate-700">峰值: --</span>
            <span id="equity-current-label" class="px-2 py-0.5 rounded bg-sky-500/10 text-sky-300 border border-sky-500/20">当前: --</span>
          </div>
        </div>
        <div class="relative w-full h-[260px] sm:h-[300px]">
          <canvas id="chart-equity-curve" role="img" aria-label="资金净值曲线图"></canvas>
        </div>
      </div>

      <!-- Chart 2: 每日盈亏柱状图 (Daily PnL Bar Chart) -->
      <div class="bg-slate-900/90 border border-slate-800 rounded-xl p-3.5 shadow-lg flex flex-col justify-between">
        <div class="flex items-center justify-between border-b border-slate-800/80 pb-2 mb-2">
          <div class="flex items-center gap-2">
            <span class="text-amber-400 text-sm font-bold flex items-center gap-1.5">
              <span>📊</span>
              <span>每日净盈亏对比 (Daily PnL)</span>
            </span>
            <span class="text-[10px] text-slate-400 font-mono hidden sm:inline">含手续费与资金费率</span>
          </div>
          <span id="daily-days-badge" class="px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-amber-500/10 text-amber-300 border border-amber-500/20">--</span>
        </div>
        <div class="relative w-full h-[260px] sm:h-[300px]">
          <canvas id="chart-daily-pnl" role="img" aria-label="每日净盈亏柱状图"></canvas>
        </div>
      </div>

      <!-- Chart 3: 胜负结构分布 (Win / Loss / Breakeven) -->
      <div class="bg-slate-900/90 border border-slate-800 rounded-xl p-3.5 shadow-lg flex flex-col justify-between">
        <div class="flex items-center justify-between border-b border-slate-800/80 pb-2 mb-2">
          <div class="flex items-center gap-2">
            <span class="text-emerald-400 text-sm font-bold flex items-center gap-1.5">
              <span>🥧</span>
              <span>胜负笔数结构 (Win / Loss)</span>
            </span>
          </div>
          <span id="win-loss-summary-badge" class="px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-emerald-500/10 text-emerald-300 border border-emerald-500/20">--</span>
        </div>
        <div class="relative w-full h-[240px] sm:h-[280px]">
          <canvas id="chart-win-loss" role="img" aria-label="胜负结构分布图"></canvas>
        </div>
      </div>

      <!-- Chart 4: 单笔盈亏率分布直方图 (Trade PnL Distribution) -->
      <div class="bg-slate-900/90 border border-slate-800 rounded-xl p-3.5 shadow-lg flex flex-col justify-between">
        <div class="flex items-center justify-between border-b border-slate-800/80 pb-2 mb-2">
          <div class="flex items-center gap-2">
            <span class="text-indigo-400 text-sm font-bold flex items-center gap-1.5">
              <span>🎯</span>
              <span>单笔收益率分布 (PnL Distribution)</span>
            </span>
          </div>
          <span id="dist-summary-badge" class="px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-indigo-500/10 text-indigo-300 border border-indigo-500/20">--</span>
        </div>
        <div class="relative w-full h-[240px] sm:h-[280px]">
          <canvas id="chart-pnl-dist" role="img" aria-label="单笔收益率分布直方图"></canvas>
        </div>
      </div>

    </section>"""


def render_active_positions_shell() -> str:
    """渲染活跃持仓面板静态外壳（9 列）。"""
    return """    <!-- Active Positions Panel -->
    <section id="active-positions-panel" aria-label="活跃在途持仓监控面板" class="bg-slate-900/90 border border-slate-800 rounded-xl p-3.5 shadow-lg space-y-2.5">
      <div class="flex items-center justify-between border-b border-slate-800 pb-2">
        <div class="flex items-center gap-2">
          <span class="text-white text-sm font-bold flex items-center gap-1.5">
            <span>⚡</span>
            <span>活跃在途持仓监控 (Active Positions)</span>
          </span>
          <span id="active-pos-count-badge" class="px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-sky-500/10 text-sky-300 border border-sky-500/20">--</span>
        </div>
        <div class="flex items-center gap-2 text-slate-400 text-xs">
          <span class="hidden sm:inline">仅多头仓位纳入 15m 破线止损与移动保本风控；空头仅展示敞口，请手动管理止损</span>
        </div>
      </div>

      <div id="short-position-warning" class="hidden px-3 py-2 rounded-lg bg-amber-500/10 border border-amber-500/30 text-xs text-amber-300">
        ⚠️ 检测到空头仓位：当前风控守护进程仅监控多头仓位，空头仓位不会触发 15m 破线止损或移动保本提醒，请手动管理止损。
      </div>

      <div id="active-positions-empty" class="hidden py-8 text-center text-slate-400 space-y-1.5">
        <div class="text-2xl">🛡️</div>
        <div class="text-sm font-semibold text-slate-300">当前账户无在途敞口 · 资金安全在岸</div>
        <div class="text-xs text-slate-500">守护进程正高频巡检共振标的，等待 15m 实体收盘突破对数趋势线确立入场机会</div>
      </div>

      <div id="active-positions-table-wrap" class="overflow-x-auto custom-scrollbar">
        <table class="w-full text-left border-collapse">
          <thead class="sticky-header select-none text-[11px] text-slate-400 border-b border-slate-800">
            <tr>
              <th class="px-3 py-2 font-semibold">标的 / 杠杆</th>
              <th class="px-3 py-2 font-semibold">仓位规模</th>
              <th class="px-3 py-2 font-semibold">开仓均价</th>
              <th class="px-3 py-2 font-semibold">标记现价</th>
              <th class="px-3 py-2 font-semibold">未实现盈亏</th>
              <th class="px-3 py-2 font-semibold">强平估值</th>
              <th class="px-3 py-2 font-semibold">移动保本 / 锁利状态</th>
              <th class="px-3 py-2 font-semibold">破线止损状态</th>
              <th class="px-3 py-2 font-semibold">关联画线</th>
            </tr>
          </thead>
          <tbody id="active-positions-tbody"></tbody>
        </table>
      </div>
    </section>"""


def render_trade_history_shell() -> str:
    """渲染历史开平仓全量流水静态外壳。"""
    return """    <!-- Historical Trade History Section -->
    <section aria-label="历史开平仓全量流水" class="bg-slate-900/90 border border-slate-800 rounded-xl p-3.5 shadow-lg space-y-3">
      <div class="flex flex-col lg:flex-row lg:items-center justify-between gap-2.5 border-b border-slate-800 pb-2.5">
        <div class="flex items-center gap-2 flex-wrap">
          <span class="text-white text-sm font-bold flex items-center gap-1.5">
            <span>📜</span>
            <span>历史开平仓全量流水 (Trade History)</span>
          </span>
          <span id="history-count-badge" class="px-2 py-0.5 rounded text-[10px] font-mono font-bold bg-slate-800 text-slate-300 border border-slate-700">--</span>
        </div>

        <div class="flex flex-wrap items-center gap-1.5 text-xs">
          <input id="history-search-input" type="text" placeholder="🔍 搜索标的 / 画线ID / 平仓原因..."
                 class="bg-slate-950/80 border border-slate-700/80 rounded-lg px-2.5 py-1 text-xs text-white placeholder-slate-500 focus:outline-none focus:border-sky-500 focus:ring-1 focus:ring-sky-500 w-44 sm:w-56 transition-all" />
          <select id="history-outcome-filter" aria-label="按交易结果筛选"
                  class="bg-slate-950/80 border border-slate-700/80 rounded-lg px-2.5 py-1 text-xs text-slate-200 focus:outline-none focus:border-sky-500">
            <option value="">全部结果</option>
            <option value="WIN">🟢 盈利 (WIN)</option>
            <option value="LOSS">🔴 亏损 (LOSS)</option>
            <option value="BREAKEVEN">⚪ 保本 (BREAKEVEN)</option>
          </select>
          <button id="btn-reset-history-filter" type="button"
                  class="px-2.5 py-1 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 transition-all">
            重置
          </button>
        </div>
      </div>

      <div class="overflow-x-auto custom-scrollbar">
        <table id="trade-history-table" class="w-full text-left border-collapse">
          <thead class="sticky-header select-none text-[11px] text-slate-400 border-b border-slate-800">
            <tr>
              <th class="px-3 py-2 font-semibold sortable" data-sort="closed_ts">平仓时间 / 开仓 <span class="sort-icon">↕</span></th>
              <th class="px-3 py-2 font-semibold sortable" data-sort="symbol">交易标的 <span class="sort-icon">↕</span></th>
              <th class="px-3 py-2 font-semibold">名义仓位</th>
              <th class="px-3 py-2 font-semibold">开仓均价</th>
              <th class="px-3 py-2 font-semibold">平仓均价</th>
              <th class="px-3 py-2 font-semibold sortable" data-sort="net_pnl">净盈亏 ($) <span class="sort-icon">↕</span></th>
              <th class="px-3 py-2 font-semibold sortable" data-sort="pnl_pct">盈亏比例 (%) <span class="sort-icon">↕</span></th>
              <th class="px-3 py-2 font-semibold">持仓时长</th>
              <th class="px-3 py-2 font-semibold">平仓原因 / 战法触发</th>
              <th class="px-3 py-2 font-semibold">关联 TV 画线</th>
            </tr>
          </thead>
          <tbody id="history-tbody"></tbody>
        </table>
      </div>
    </section>"""


def render_account_html() -> str:
    """组装纯静态 account.html 外壳，不包含内联样式、内联脚本或私有数据。"""
    doc_parts = [
        "<!DOCTYPE html>",
        '<html lang="zh-CN">',
        "<head>",
        '  <meta charset="UTF-8">',
        '  <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">',
        '  <meta name="color-scheme" content="dark">',
        "  <title>账户数据分析与量化风控看板 —— 实战工作台 v3.5</title>",
        '  <script src="https://www.gstatic.com/antigravity/web/dev/tailwindcss.min.js"></script>',
        '  <link rel="stylesheet" href="static/styles.css">',
        '  <script src="https://cdn.jsdelivr.net/npm/chart.js" defer></script>',
        '  <script src="static/account.js" defer></script>',
        "</head>",
        '<body class="p-2 md:p-3.5 antialiased custom-scrollbar">',
        '  <main id="main-content" class="w-full max-w-[1600px] mx-auto space-y-2.5">',
        render_global_nav_header(),
        render_discipline_ribbon(),
        '    <p id="account-load-status" role="status" class="px-3 py-2 text-sm text-slate-300">正在读取账户数据…</p>',
        '    <section id="account-report" class="space-y-3.5" hidden>',
        render_kpi_shell(),
        render_charts_shell(),
        render_active_positions_shell(),
        render_trade_history_shell(),
        "    </section>",
        '    <footer class="text-center py-4 text-slate-500 text-xs font-mono space-y-1">',
        "      <div>突破追多与移动保本系统 · 账户数据分析引擎 v3.5 Pro · 顶级量化执行终端</div>",
        f'      <div class="text-[10px] text-slate-600">TradingView 专属对数画线云端追踪 ({TV_LAYOUT_ID}) · 币安 USDⓈ-M 永续实盘直连</div>',
        "    </footer>",
        "  </main>",
        "</body>",
        "</html>",
    ]
    raw_html = "\n".join(doc_parts)
    # 压缩多余行首缩进以保持纯静态外壳精简 (<25KB)
    return "\n".join(line.lstrip() if line.startswith("      ") else line for line in raw_html.splitlines()) + "\n"


def build_account_analytics(output_path: Optional[Path] = None) -> Path:
    """原子化写入纯静态账户看板外壳 HTML。"""
    out = Path(output_path) if output_path is not None else OUTPUT_HTML
    out.parent.mkdir(parents=True, exist_ok=True)
    html_content = render_account_html()
    tmp_path = out.with_name(f".{out.name}.tmp.{os.getpid()}.{time.time_ns()}")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(html_content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, out)
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise
    print(f"Successfully compiled {out.name}")
    return out


def main() -> None:
    build_account_analytics()


if __name__ == "__main__":
    main()
