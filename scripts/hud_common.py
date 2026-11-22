#!/usr/bin/env python3
"""
突破追多与移动保本系统 · 双端 HUD 公共渲染模块 (v3.5 Pro)
供 scripts/build_index.py 与 scripts/build_second_leg_playbook.py 共享：
- 候选池加载与分区统计
- 自适应小数格式化
- 暗黑终端公共样式、战法纪律横幅、表头与候选行渲染
- 双引擎站内 K 线弹窗（币安直连极速 Canvas K 线 + TradingView 内嵌切换）
- Toast 与前端交互 / 币安 5s 实时轮询脚本
"""

import html
import json
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POOL_FILE = ROOT / "runs" / "second_leg_candidates_pool.json"
TV_LAYOUT_ID = "ig0LuDL8"  # TradingView 专属画线图表布局 ID (统一用于行直达与弹窗画线跳转)


def format_crypto_val(val, ref_diff=None) -> str:
    if val is None or not isinstance(val, (int, float)):
        return "--"
    abs_v = abs(val)
    if abs_v >= 100:
        base_dec = 2
    elif abs_v >= 0.01:
        base_dec = 4
    elif abs_v >= 0.0001:
        base_dec = 6
    else:
        base_dec = 8

    if ref_diff is not None and 0 < abs(ref_diff) < 10 ** (-base_dec):
        base_dec = min(8, base_dec + 2)

    return f"{val:.{base_dec}f}"


def load_candidates(pool_file: Path = POOL_FILE) -> list:
    if pool_file.exists():
        with open(pool_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def compute_pool_stats(candidates: list) -> dict:
    multi_resonance_coins = [
        c for c in candidates
        if (
            c.get("is_resonance")
            or (
                c.get("is_ema_bull")
                and (c.get("is_high_turnover") or c.get("turnover_ratio", 0) >= 1.5)
            )
        )
    ]
    triple_resonance_coins = [
        c for c in multi_resonance_coins
        if (c.get("is_neg_funding") or c.get("funding_rate_4h_pct", 0) < -0.00005)
    ]
    double_resonance_coins = [c for c in multi_resonance_coins if c not in triple_resonance_coins]
    neg_fr_coins = [
        c for c in candidates
        if c.get("is_neg_funding") or c.get("funding_rate_4h_pct", 0) < -0.00005
    ]
    high_tr_coins = [
        c for c in candidates
        if c.get("is_high_turnover") or c.get("turnover_ratio", 0) >= 1.5
    ]
    ema_bull_coins = [c for c in candidates if c.get("is_ema_bull")]
    overflow_coins = [c for c in candidates if c.get("is_overflow_7d", False)]

    return {
        "n_total": len(candidates),
        "n_resonance": len(multi_resonance_coins),
        "n_triple": len(triple_resonance_coins),
        "n_double": len(double_resonance_coins),
        "n_neg_fr": len(neg_fr_coins),
        "n_high_tr": len(high_tr_coins),
        "n_ema_bull": len(ema_bull_coins),
        "n_overflow": len(overflow_coins),
        "sample_resonance": "、".join([c["symbol"].removesuffix("USDT") for c in multi_resonance_coins[:4]]),
        "sample_neg_fr": "、".join(
            [f"{c['symbol'].removesuffix('USDT')}({c['funding_rate_4h_pct']:+.2f}%)"
             for c in sorted(neg_fr_coins, key=lambda x: x["funding_rate_4h_pct"])[:3]]
        ),
        "sample_high_tr": "、".join(
            [f"{c['symbol'].removesuffix('USDT')}({c['turnover_ratio']:.1f}x)"
             for c in sorted(high_tr_coins, key=lambda x: -x["turnover_ratio"])[:3]]
        ),
        "sample_ema_bull": "、".join(
            [f"{c['symbol'].removesuffix('USDT')}({c.get('ema_spread_pct', 0):+.1f}%)"
             for c in sorted(ema_bull_coins, key=lambda x: -x.get("ema_spread_pct", 0))[:3]]
        ),
    }


EXECUTION_DISCIPLINE_RIBBON_HTML = """      <!-- Execution Discipline Ribbon (Compact) -->
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
      </div>"""


TABLE_THEAD_HTML = """          <thead class="sticky-header select-none text-[11px]">
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
          </thead>"""


EMPTY_SEARCH_ROW_HTML = """            <!-- Empty Search State -->
            <tr id="empty-search-row" class="hidden">
              <td colspan="11" class="py-12 text-center text-slate-500 font-sans">
                <div class="text-2xl mb-2">🔍</div>
                <div class="text-sm font-semibold text-slate-300">未找到匹配标的</div>
                <div class="text-xs text-slate-500 mt-1">按 <kbd class="px-1.5 py-0.5 bg-slate-800 rounded border border-slate-700 font-mono text-slate-400">Esc</kbd> 清空搜索或重置筛选条件</div>
              </td>
            </tr>"""


CHART_MODAL_AND_TOAST_HTML = """  <!-- In-Page Interactive 15m/1h Candlestick & TV Chart Modal -->
  <div id="chart-modal" class="fixed inset-0 z-50 hidden bg-black/80 backdrop-blur-sm flex items-center justify-center p-2 md:p-5 transition-all">
    <div class="bg-slate-900 border border-slate-700/80 rounded-2xl w-full max-w-5xl h-[85vh] max-h-[720px] shadow-2xl flex flex-col overflow-hidden relative animate-scaleUp">
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
  <div id="toast" class="fixed bottom-6 right-6 z-50 hidden px-4 py-2.5 rounded-xl bg-slate-800 border border-slate-700 text-slate-200 text-xs shadow-2xl flex items-center gap-2">
    <span class="text-emerald-400 font-bold">✓</span>
    <span id="toast-text">已复制</span>
  </div>"""


def render_candidate_rows(candidates: list, row_class_name: str = "candidate-row") -> str:
    chunks = []
    for idx, c in enumerate(candidates, 1):
        sym = c["symbol"]
        p = c["price"]
        chg = c["price_chg_24h"]
        mc_usd = c["market_cap_usd"]
        mc = mc_usd / 1e6
        effective_cs = mc_usd / p if p > 0 else c.get("circulating_supply", 0)
        vol_usd = c["volume_24h_usd"]
        vol = vol_usd / 1e6
        tr = c["turnover_ratio"]
        fr_4h = c.get("funding_rate_4h_pct", c["funding_rate_pct"])
        fr_settle = c["funding_rate_pct"]
        interval = c.get("funding_interval_hours", 8)

        high_24h = c.get("high_24h", p * 1.05)
        low_24h = c.get("low_24h", p * 0.95)
        range_span = max(high_24h - low_24h, 1e-8)
        pos_pct = max(0.0, min(100.0, ((p - low_24h) / range_span) * 100))

        ema5 = c.get("ema5")
        ema10 = c.get("ema10")
        is_ema_bull = c.get("is_ema_bull", False)
        ema_spread = c.get("ema_spread_pct", 0.0)
        is_neg_fr = c.get("is_neg_funding", fr_4h < -0.00005)
        is_high_tr = c.get("is_high_turnover", tr >= 1.5)

        safe_sym = html.escape(str(sym))
        is_overflow = c.get("is_overflow_7d", False)
        days_left = c.get("overflow_days_left", 7.0)
        if is_overflow:
            mc_html = f"""<div>
          <div class="text-amber-300 font-mono tabular-nums font-semibold sym-mc-val" data-sym="{safe_sym}">${mc:.1f}M</div>
          <span class="px-1.5 py-0.2 rounded text-[9px] bg-amber-500/15 text-amber-300 border border-amber-500/30 inline-flex items-center gap-0.5" title="曾落入3000wu以下，突破后保持7天持续跟踪">★ 剩{days_left:.1f}天</span>
        </div>"""
        else:
            mc_html = f'<div class="text-slate-200 font-mono tabular-nums sym-mc-val" data-sym="{safe_sym}">${mc:.1f}M</div>'

        CHINESE_TV_MAP = {
            "龙虾USDT": "BINANCE:LONGXIAUSDT.P",
            "哈基米USDT": "BINANCE:HAJIMIUSDT.P",
            "我踏马来了USDT": "BINANCE:WOTAMALAILIAOUSDT.P",
            "币安人生USDT": "BINANCE:BIANRENSHENGUSDT.P",
            "牛来USDT": "BINANCE:NIULAIUSDT.P",
        }
        tv_copy_symbol = CHINESE_TV_MAP.get(sym, f"BINANCE:{sym}.P")
        tv_url = f"https://cn.tradingview.com/chart/{TV_LAYOUT_ID}/?symbol={urllib.parse.quote(tv_copy_symbol)}&interval=15"
        binance_url = f"https://www.binance.com/zh-CN/futures/{urllib.parse.quote(sym)}"

        is_res = is_ema_bull and is_high_tr
        res_hours = int(c.get("resonance_duration_hours", 0) or 0)
        res_entry_time = c.get("resonance_entry_time", "")

        if is_res and res_hours > 0:
            if res_hours <= 3:
                time_badge = f'<span class="px-1.5 py-0.5 rounded text-[10px] font-mono font-bold bg-emerald-500/20 text-emerald-300 border border-emerald-500/30 inline-flex items-center gap-1 w-fit"><span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse"></span>⚡ 新进 {res_hours}h</span>'
            elif res_hours <= 12:
                time_badge = f'<span class="px-1.5 py-0.5 rounded text-[10px] font-mono font-bold bg-amber-500/20 text-amber-300 border border-amber-500/30 inline-flex items-center gap-1 w-fit"><span class="w-1.5 h-1.5 rounded-full bg-amber-400"></span>⏳ 活跃 {res_hours}h</span>'
            else:
                days = res_hours // 24
                rem_h = res_hours % 24
                d_str = f"{days}天{rem_h}h" if days > 0 else f"{res_hours}h"
                time_badge = f'<span class="px-1.5 py-0.5 rounded text-[10px] font-mono font-bold bg-sky-500/15 text-sky-300 border border-sky-500/30 inline-flex items-center gap-1 w-fit">⏱️ 驻留 {d_str}</span>'

            entry_sub = f'<div class="text-[9px] font-mono text-slate-400 mt-0.5">入圈: {res_entry_time}</div>' if res_entry_time else ''
            duration_cell_html = f'<div>{time_badge}{entry_sub}</div>'
        elif is_res:
            entry_sub = f'<div class="text-[9px] font-mono text-slate-400 mt-0.5">入圈: {res_entry_time}</div>' if res_entry_time else ''
            duration_cell_html = f'<div><span class="px-1.5 py-0.5 rounded text-[10px] font-mono font-bold bg-emerald-500/20 text-emerald-300 border border-emerald-500/30 inline-flex items-center gap-1 w-fit"><span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse"></span>⚡ 刚刚进入</span>{entry_sub}</div>'
        else:
            duration_cell_html = '<span class="text-slate-600 font-mono text-xs">--</span>'
        if is_res and is_neg_fr:
            row_class = f"hover:bg-slate-800/40 bg-rose-950/15 border-l-2 border-rose-500 {row_class_name}"
        elif is_res:
            row_class = f"hover:bg-slate-800/40 bg-amber-950/10 border-l-2 border-amber-500/70 {row_class_name}"
        else:
            row_class = f"hover:bg-slate-800/30 {row_class_name}"

        resonance_badge = ""
        if is_res and is_neg_fr:
            resonance_badge = '<span class="px-1.5 py-0.5 rounded text-[10px] font-bold bg-rose-500/20 text-rose-300 border border-rose-500/30">🔥 双核共振+逼空</span>'
        elif is_res:
            resonance_badge = '<span class="px-1.5 py-0.5 rounded text-[10px] font-bold bg-amber-500/20 text-amber-300 border border-amber-500/30">👑 双核黄金共振</span>'

        zone_tags = []
        if is_ema_bull:
            zone_tags.append('<span class="px-1 py-0.2 rounded text-[9px] bg-emerald-950/60 text-emerald-300 border border-emerald-500/20">1h顺势</span>')
        if is_high_tr:
            zone_tags.append(f'<span class="px-1 py-0.2 rounded text-[9px] bg-purple-950/60 text-purple-300 border border-purple-500/20">换手{tr:.1f}x</span>')
        if is_neg_fr:
            zone_tags.append('<span class="px-1 py-0.2 rounded text-[9px] bg-rose-950/60 text-rose-300 border border-rose-500/20">负费率</span>')

        status_html = f"""<div class="flex items-center gap-1.5 flex-wrap">
      {resonance_badge}
      {''.join(zone_tags) if zone_tags else '<span class="text-slate-500 text-[10px]">基础底池</span>'}
    </div>"""

        cycle_badge = (
            '<span class="px-1 py-0.2 text-[9px] font-sans font-bold rounded border bg-amber-500/20 text-amber-300 border-amber-500/30">1h期</span>'
            if interval == 1
            else (
                '<span class="px-1 py-0.2 text-[9px] font-sans rounded border bg-sky-500/20 text-sky-300 border-sky-500/30">4h期</span>'
                if interval == 4
                else '<span class="px-1 py-0.2 text-[9px] font-sans rounded border bg-slate-800 text-slate-400 border-slate-700">8h期</span>'
            )
        )

        fr_color = "text-rose-400 font-bold" if fr_4h <= -0.025 else ("text-rose-300" if fr_4h < 0 else "text-slate-400")
        tr_class = "text-purple-300 font-bold bg-purple-500/10 px-1 py-0.5 rounded border border-purple-500/20" if tr >= 1.5 else "text-sky-400"

        p_str = format_crypto_val(p)
        high_str = format_crypto_val(high_24h)
        low_str = format_crypto_val(low_24h)

        if pos_pct <= 25:
            pos_bar_color = "bg-emerald-400"
        elif pos_pct >= 75:
            pos_bar_color = "bg-amber-400"
        else:
            pos_bar_color = "bg-sky-400"

        if ema5 is not None and ema10 is not None:
            diff_ema = ema5 - ema10
            ema5_str = format_crypto_val(ema5, diff_ema)
            ema10_str = format_crypto_val(ema10, diff_ema)

            if abs(ema_spread) <= 0.35:
                ema_html = f"""<div class="whitespace-nowrap">
              <div class="flex items-center gap-1.5">
                <span class="w-1.5 h-1.5 rounded-full bg-amber-400" aria-hidden="true"></span>
                <span class="text-amber-300 font-bold px-1.5 py-0.2 rounded bg-amber-500/15 border border-amber-500/30 text-[10px]">★ 粘合走平</span>
                <span class="text-[10px] text-amber-300/80 font-mono">({ema_spread:+.2f}%)</span>
              </div>
              <div class="text-[10px] text-slate-500 font-mono mt-0.5">5:{ema5_str} / 10:{ema10_str}</div>
            </div>"""
            elif is_ema_bull:
                ema_html = f"""<div class="whitespace-nowrap">
              <div class="flex items-center gap-1.5">
                <span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" aria-hidden="true"></span>
                <span class="text-emerald-400 font-bold">金叉多头</span>
                <span class="text-[10px] text-emerald-300 font-mono">({ema_spread:+.2f}%)</span>
              </div>
              <div class="text-[10px] text-slate-500 font-mono mt-0.5">5:{ema5_str} / 10:{ema10_str}</div>
            </div>"""
            else:
                ema_html = f"""<div class="whitespace-nowrap">
              <div class="flex items-center gap-1.5">
                <span class="w-1.5 h-1.5 rounded-full bg-slate-600" aria-hidden="true"></span>
                <span class="text-slate-400">死叉整理</span>
                <span class="text-[10px] text-slate-500 font-mono">({ema_spread:+.2f}%)</span>
              </div>
              <div class="text-[10px] text-slate-600 font-mono mt-0.5">5:{ema5_str} / 10:{ema10_str}</div>
            </div>"""
        else:
            ema_html = '<span class="text-slate-500 text-[10px]">--</span>'

        chunks.append(f"""
            <tr class="{row_class}"
                data-sym="{safe_sym}"
                data-tv-sym="{tv_copy_symbol}"
                data-resonance="{'1' if is_res else '0'}"
                data-neg-fr="{'1' if is_neg_fr else '0'}"
                data-high-tr="{'1' if is_high_tr else '0'}"
                data-ema-bull="{'1' if is_ema_bull else '0'}"
                data-cs="{effective_cs}"
                data-val-price="{p}"
                data-val-pos="{pos_pct:.1f}"
                data-val-chg="{chg}"
                data-val-mc="{mc_usd}"
                data-val-vol="{vol_usd}"
                data-val-tr="{tr}"
                data-val-fr="{fr_4h}"
                data-val-ema="{ema_spread}"
                data-val-duration="{res_hours}">
              <td class="py-1.5 px-1.5 text-center text-slate-500 font-sans text-xs">{idx}</td>
              <th scope="row" class="py-1.5 px-2 font-normal whitespace-nowrap">
                <div class="flex items-center gap-1">
                  <span class="text-white font-bold font-sans text-sm tracking-tight">{safe_sym}</span>
                  <button type="button" class="btn-copy-sym px-1 py-0.2 rounded bg-slate-800 hover:bg-slate-700 text-slate-400 hover:text-slate-200 border border-slate-700 text-[10px] transition-all" data-copy="{tv_copy_symbol}" title="复制 TV 代码 ({tv_copy_symbol})">📋</button>
                </div>
                <div class="flex items-center gap-1 mt-0.5 text-[10px] font-sans">
                  <button type="button" class="btn-preview-chart px-1.5 py-0.2 rounded bg-sky-500/10 hover:bg-sky-500/20 text-sky-300 border border-sky-500/30 transition-all font-semibold" data-sym="{safe_sym}" data-tv="{tv_copy_symbol}" title="站内秒级弹窗预览 15m 实时 K 线">👁️15m</button>
                  <a href="{tv_url}" target="_blank" rel="noopener noreferrer" class="px-1.5 py-0.2 rounded bg-[#2962FF]/15 hover:bg-[#2962FF]/25 text-[#38bdf8] hover:text-white border border-[#2962FF]/30 transition-all font-semibold" title="在 TradingView 打开 15m 专属画线布局 (ig0LuDL8)">
                    <span>TV↗</span>
                  </a>
                  <a href="{binance_url}" target="_blank" rel="noopener noreferrer" class="px-1.5 py-0.2 rounded bg-amber-500/10 hover:bg-amber-500/20 text-amber-300 hover:text-white border border-amber-500/30 transition-all" title="直达币安合约盘口深度">
                    <span>币安↗</span>
                  </a>
                </div>
              </th>
              <td class="py-1.5 px-2 whitespace-nowrap">
                <div class="text-white font-mono tabular-nums font-semibold sym-price" data-sym="{safe_sym}">{p_str}</div>
                <div class="sym-pos-box w-16 mt-0.5 flex items-center gap-1 text-[9px] text-slate-500" data-sym="{safe_sym}" title="24h振幅区间: {low_str} ~ {high_str} (现价分位: {pos_pct:.0f}%)">
                  <div class="w-full h-1 bg-slate-800 rounded-full relative overflow-hidden">
                    <div class="sym-pos-bar h-full {pos_bar_color} rounded-full transition-all duration-300" style="width: {pos_pct:.1f}%"></div>
                  </div>
                  <span class="sym-pos-text font-mono text-[9px] text-slate-400">{pos_pct:.0f}%</span>
                </div>
              </td>
              <td class="py-1.5 px-2 font-mono tabular-nums sym-chg whitespace-nowrap {'text-emerald-400 font-semibold' if chg >= 0 else 'text-rose-400 font-semibold'}" data-sym="{safe_sym}">{chg:+.2f}%</td>
              <td class="py-1.5 px-2 whitespace-nowrap">{mc_html}</td>
              <td class="py-1.5 px-2 text-slate-300 font-mono tabular-nums sym-vol whitespace-nowrap" data-sym="{safe_sym}">${vol:.1f}M</td>
              <td class="py-1.5 px-2 sym-tr font-mono tabular-nums whitespace-nowrap" data-sym="{safe_sym}">
                <span class="{tr_class}">{tr:.2f}x</span>
              </td>
              <td class="py-1.5 px-2 sym-fr whitespace-nowrap" data-sym="{safe_sym}">
                <div class="flex items-center gap-1">
                  <span class="sym-fr-4h font-mono tabular-nums {fr_color}">{fr_4h:+.4f}%</span>
                  {cycle_badge}
                </div>
                <div class="text-[10px] text-slate-500 font-mono sym-fr-settle">单期: {fr_settle:+.4f}%</div>
              </td>
              <td class="py-1.5 px-2 font-sans whitespace-nowrap">
                {ema_html}
              </td>
              <td class="py-1.5 px-2 font-sans whitespace-nowrap">
                {duration_cell_html}
              </td>
              <td class="py-1.5 px-2 font-sans whitespace-nowrap">
                {status_html}
              </td>
            </tr>""")
    return "".join(chunks)


def render_hud_script(
    candidates: list,
    *,
    row_class_name: str,
    sync_btn_id: str,
    count_badge_id: str,
    active_zone_pill_id: str,
    search_input_id: str,
    reset_btn_id: str,
    table_body_id: str,
    batch_copy_btn_id: str,
    batch_export_btn_id: str,
    settle_countdown_id: str,
    drawer_id: str = "",
    toggle_drawer_btn_id: str = "",
    close_drawer_btn_id: str = "",
) -> str:
    n_total = len(candidates)
    pool_symbols_json = json.dumps([c["symbol"] for c in candidates])
    pool_interval_json = json.dumps({c["symbol"]: c.get("funding_interval_hours", 8) for c in candidates})

    cfg_json = json.dumps(
        {
            "rowSelector": f".{row_class_name}",
            "syncBtnId": sync_btn_id,
            "countBadgeId": count_badge_id,
            "activeZonePillId": active_zone_pill_id,
            "searchInputId": search_input_id,
            "resetBtnId": reset_btn_id,
            "tableBodyId": table_body_id,
            "batchCopyBtnId": batch_copy_btn_id,
            "batchExportBtnId": batch_export_btn_id,
            "settleCountdownId": settle_countdown_id,
            "drawerId": drawer_id,
            "toggleDrawerBtnId": toggle_drawer_btn_id,
            "closeDrawerBtnId": close_drawer_btn_id,
            "nTotal": n_total,
        },
        ensure_ascii=False,
    )

    return f"""  <script>
  (function() {{
    const CFG = {cfg_json};
    const poolSymbols = {pool_symbols_json};
    const poolIntervalMap = {pool_interval_json};
    const symPriceMap = new Map();
    let pollController = null;
    let pollTimer = null;
    let isFetching = false;

    function formatPrice(p) {{
      if (typeof p !== 'number' || isNaN(p)) return '--';
      const absP = Math.abs(p);
      if (absP >= 100) return p.toFixed(2);
      if (absP >= 0.01) return p.toFixed(4);
      if (absP >= 0.0001) return p.toFixed(6);
      return p.toFixed(8);
    }}

    const rowElementMap = new Map();
    poolSymbols.forEach(sym => {{
      rowElementMap.set(sym, {{
        rowEl: document.querySelector(`tr[data-sym="${{sym}}"]`),
        priceEl: document.querySelector(`.sym-price[data-sym="${{sym}}"]`),
        posBoxEl: document.querySelector(`.sym-pos-box[data-sym="${{sym}}"]`),
        posBarEl: document.querySelector(`.sym-pos-box[data-sym="${{sym}}"] .sym-pos-bar`),
        posTextEl: document.querySelector(`.sym-pos-box[data-sym="${{sym}}"] .sym-pos-text`),
        chgEl: document.querySelector(`.sym-chg[data-sym="${{sym}}"]`),
        volEl: document.querySelector(`.sym-vol[data-sym="${{sym}}"]`),
        trEl: document.querySelector(`.sym-tr[data-sym="${{sym}}"]`),
        mcValEl: document.querySelector(`.sym-mc-val[data-sym="${{sym}}"]`),
        fr4hEl: document.querySelector(`.sym-fr[data-sym="${{sym}}"] .sym-fr-4h`),
        frSettleEl: document.querySelector(`.sym-fr[data-sym="${{sym}}"] .sym-fr-settle`)
      }});
    }});

    const syncBtn = document.getElementById(CFG.syncBtnId);
    const countBadge = document.getElementById(CFG.countBadgeId);
    const activeZonePill = document.getElementById(CFG.activeZonePillId);
    const searchInput = document.getElementById(CFG.searchInputId);
    const resetBtn = document.getElementById(CFG.resetBtnId);
    const kpiCards = document.querySelectorAll('.kpi-card');
    const tableBody = document.getElementById(CFG.tableBodyId);
    const sortHeaders = document.querySelectorAll('th.sortable');
    const batchCopyBtn = document.getElementById(CFG.batchCopyBtnId);
    const batchExportBtn = document.getElementById(CFG.batchExportBtnId);
    const toast = document.getElementById('toast');
    const toastText = document.getElementById('toast-text');

    const tvDrawer = CFG.drawerId ? document.getElementById(CFG.drawerId) : null;
    const toggleDrawerBtn = CFG.toggleDrawerBtnId ? document.getElementById(CFG.toggleDrawerBtnId) : null;
    const closeDrawerBtn = CFG.closeDrawerBtnId ? document.getElementById(CFG.closeDrawerBtnId) : null;

    let currentZone = 'resonance';
    let searchQuery = '';
    let sortColumn = null;
    let sortAsc = true;

    const zoneNames = {{
      'resonance': '👑 顺势高换手共振区',
      'neg_fr': '🔥 负费率逼空区',
      'high_tr': '⚡ 巨量高换手区',
      'ema_bull': '📈 1h EMA顺势区',
      'all': '🌐 全量候选底池'
    }};

    function showToast(msg) {{
      if (!toast) return;
      toastText.textContent = msg;
      toast.classList.remove('hidden');
      setTimeout(() => {{
        toast.classList.add('hidden');
      }}, 2000);
    }}

    function applyFilter() {{
      let visibleCount = 0;
      const q = searchQuery.trim().toUpperCase();
      const rows = document.querySelectorAll(CFG.rowSelector);

      rows.forEach(row => {{
        const sym = row.getAttribute('data-sym') || '';
        const matchSearch = !q || sym.toUpperCase().includes(q);

        let matchZone = true;
        if (currentZone === 'resonance') {{
          matchZone = row.getAttribute('data-resonance') === '1';
        }} else if (currentZone === 'neg_fr') {{
          matchZone = row.getAttribute('data-neg-fr') === '1';
        }} else if (currentZone === 'high_tr') {{
          matchZone = row.getAttribute('data-high-tr') === '1';
        }} else if (currentZone === 'ema_bull') {{
          matchZone = row.getAttribute('data-ema-bull') === '1';
        }}

        if (matchSearch && matchZone) {{
          row.style.display = '';
          visibleCount++;
        }} else {{
          row.style.display = 'none';
        }}
      }});

      if (countBadge) {{
        countBadge.textContent = `${{visibleCount}} / ${{CFG.nTotal}} 标的`;
      }}
      if (activeZonePill) {{
        activeZonePill.textContent = `当前筛选: ${{zoneNames[currentZone] || '自定义'}}`;
      }}
      const emptyRow = document.getElementById('empty-search-row');
      if (emptyRow) {{
        emptyRow.classList.toggle('hidden', visibleCount > 0);
      }}
    }}

    function switchZone(zone) {{
      kpiCards.forEach(c => {{
        c.classList.toggle('active', c.getAttribute('data-zone') === zone);
      }});
      currentZone = zone;
      applyFilter();
    }}

    kpiCards.forEach(card => {{
      card.addEventListener('click', () => {{
        switchZone(card.getAttribute('data-zone') || 'all');
      }});
    }});

    if (resetBtn) {{
      resetBtn.addEventListener('click', () => {{
        switchZone('all');
        if (searchInput) searchInput.value = '';
        searchQuery = '';
        applyFilter();
      }});
    }}

    if (searchInput) {{
      searchInput.addEventListener('input', (e) => {{
        searchQuery = e.target.value;
        applyFilter();
      }});
    }}

    function getVisibleTvSymbols() {{
      const rows = Array.from(document.querySelectorAll(CFG.rowSelector)).filter(r => r.style.display !== 'none');
      return rows.map(r => r.getAttribute('data-tv-sym')).filter(Boolean);
    }}

    async function copyText(text, successMsg) {{
      if (!text) return;
      let ok = false;
      try {{
        if (navigator.clipboard && window.isSecureContext) {{
          await navigator.clipboard.writeText(text);
          ok = true;
        }}
      }} catch (e) {{}}
      if (!ok) {{
        try {{
          const ta = document.createElement('textarea');
          ta.value = text;
          ta.style.position = 'fixed';
          ta.style.left = '-9999px';
          ta.style.top = '-9999px';
          document.body.appendChild(ta);
          ta.focus();
          ta.select();
          ok = document.execCommand('copy');
          document.body.removeChild(ta);
        }} catch (err) {{}}
      }}
      showToast(ok ? successMsg : '复制失败，请手动选择复制');
    }}

    if (batchCopyBtn) {{
      batchCopyBtn.addEventListener('click', async () => {{
        const symbols = getVisibleTvSymbols();
        if (symbols.length === 0) {{
          showToast('当前筛选下无可见标的');
          return;
        }}
        await copyText(symbols.join(', '), `已复制 ${{symbols.length}} 个标的为 TV 自选格式！`);
      }});
    }}

    if (batchExportBtn) {{
      batchExportBtn.addEventListener('click', () => {{
        const symbols = getVisibleTvSymbols();
        if (symbols.length === 0) return;
        const text = symbols.join('\\n');
        const blob = new Blob([text], {{ type: 'text/plain;charset=utf-8' }});
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `TV_Watchlist_${{currentZone}}_${{symbols.length}}.txt`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        showToast(`已导出 ${{symbols.length}} 个标的 .txt 自选文件！`);
      }});
    }}

    sortHeaders.forEach(th => {{
      th.addEventListener('click', () => {{
        const col = th.getAttribute('data-sort');
        if (sortColumn === col) {{
          sortAsc = !sortAsc;
        }} else {{
          sortColumn = col;
          sortAsc = (col === 'fr');
        }}

        sortHeaders.forEach(h => {{
          const icon = h.querySelector('.sort-icon');
          if (icon) icon.textContent = '↕';
        }});
        const activeIcon = th.querySelector('.sort-icon');
        if (activeIcon) {{
          activeIcon.textContent = sortAsc ? '▲' : '▼';
        }}

        const rows = Array.from(tableBody.querySelectorAll(CFG.rowSelector));
        rows.sort((a, b) => {{
          const valA = parseFloat(a.getAttribute(`data-val-${{col}}`)) || 0;
          const valB = parseFloat(b.getAttribute(`data-val-${{col}}`)) || 0;
          return sortAsc ? valA - valB : valB - valA;
        }});

        rows.forEach(r => tableBody.appendChild(r));
      }});
    }});

    document.querySelectorAll('.btn-copy-sym').forEach(btn => {{
      btn.addEventListener('click', async (e) => {{
        e.stopPropagation();
        const sym = btn.getAttribute('data-copy');
        if (!sym) return;
        const originalText = btn.textContent;
        await copyText(sym, `已复制: ${{sym}}`);
        btn.textContent = '✓';
        btn.classList.add('bg-emerald-600', 'text-white');
        setTimeout(() => {{
          btn.textContent = originalText;
          btn.classList.remove('bg-emerald-600', 'text-white');
        }}, 1200);
      }});
    }});

    // Dual-Engine In-Page K-Line Chart Modal (Native Canvas & TradingView Iframe)
    const chartModal = document.getElementById('chart-modal');
    const modalCloseBtn = document.getElementById('modal-close-btn');
    const modalChartTitle = document.getElementById('modal-chart-title');
    const modalChartPrice = document.getElementById('modal-chart-price');
    const modalExternalLink = document.getElementById('modal-external-link');
    const modalNativeView = document.getElementById('modal-native-chart-view');
    const modalTvView = document.getElementById('modal-tv-view');
    const modalTvIframe = document.getElementById('modal-tv-iframe');
    const modalCanvas = document.getElementById('modal-kline-canvas');
    const modalTooltip = document.getElementById('modal-chart-tooltip');
    const modalLoading = document.getElementById('modal-chart-loading');
    const modalError = document.getElementById('modal-chart-error');
    const modalRetryBtn = document.getElementById('modal-chart-retry-btn');
    const btnEngineNative = document.getElementById('btn-chart-engine-native');
    const btnEngineTv = document.getElementById('btn-chart-engine-tv');
    const modalHudEma5 = document.getElementById('modal-hud-ema5');
    const modalHudEma10 = document.getElementById('modal-hud-ema10');

    let modalCurrentSymbol = '';
    let modalCurrentTvSymbol = '';
    let modalCurrentInterval = '15m';
    let modalActiveEngine = 'native';
    let modalCandles = [];
    let modalHoverIndex = -1;
    let modalFetchController = null;

    const TV_LAYOUT_ID = 'ig0LuDL8';
    const TV_INTERVAL_MAP = {{
      '15m': '15',
      '1h': '60',
      '4h': '240',
      '1d': '1D'
    }};

    function getTvUrl(tvSym, interval) {{
      const tvInterval = TV_INTERVAL_MAP[interval] || '15';
      return `https://cn.tradingview.com/chart/${{TV_LAYOUT_ID}}/?symbol=${{encodeURIComponent(tvSym)}}&interval=${{tvInterval}}`;
    }}

    function calcEMAArray(candlesList, period) {{
      const k = 2 / (period + 1);
      const res = [];
      let prev = null;
      for (let i = 0; i < candlesList.length; i++) {{
        const c = candlesList[i].close;
        if (prev === null) prev = c;
        else prev = c * k + prev * (1 - k);
        res.push(prev);
      }}
      return res;
    }}

    async function fetchModalKlines(sym, interval) {{
      if (!modalNativeView || !modalLoading) return;
      if (modalFetchController) modalFetchController.abort();
      modalFetchController = new AbortController();

      modalLoading.classList.remove('hidden');
      if (modalError) modalError.classList.add('hidden');

      try {{
        const cleanSym = encodeURIComponent(sym);
        const url = `https://www.binance.com/fapi/v1/klines?symbol=${{cleanSym}}&interval=${{interval}}&limit=90`;
        const res = await fetch(url, {{ signal: modalFetchController.signal }});
        if (!res.ok) throw new Error(`HTTP ${{res.status}}`);
        const raw = await res.json();
        if (!Array.isArray(raw) || raw.length === 0) throw new Error('No data');

        modalCandles = raw.map(d => ({{
          time: d[0],
          open: parseFloat(d[1]),
          high: parseFloat(d[2]),
          low: parseFloat(d[3]),
          close: parseFloat(d[4]),
          volume: parseFloat(d[5])
        }}));

        const ema5Vals = calcEMAArray(modalCandles, 5);
        const ema10Vals = calcEMAArray(modalCandles, 10);
        modalCandles.forEach((c, idx) => {{
          c.ema5 = ema5Vals[idx];
          c.ema10 = ema10Vals[idx];
        }});

        const last = modalCandles[modalCandles.length - 1];
        if (last && modalChartPrice) {{
          const isUp = last.close >= last.open;
          modalChartPrice.textContent = formatPrice(last.close);
          modalChartPrice.className = `font-mono font-bold text-sm ${{isUp ? 'text-emerald-400' : 'text-rose-400'}}`;
          if (modalHudEma5) modalHudEma5.textContent = formatPrice(last.ema5);
          if (modalHudEma10) modalHudEma10.textContent = formatPrice(last.ema10);
        }}

        renderModalChart();
      }} catch (err) {{
        if (err.name === 'AbortError') return;
        console.error('Failed to load klines:', err);
        if (modalError) modalError.classList.remove('hidden');
      }} finally {{
        if (modalLoading) modalLoading.classList.add('hidden');
      }}
    }}

    function renderModalChart() {{
      if (!modalCanvas || !modalNativeView || !modalCandles.length) return;
      const ctx = modalCanvas.getContext('2d');
      if (!ctx) return;

      const dpr = window.devicePixelRatio || 1;
      const w = modalNativeView.clientWidth;
      const h = modalNativeView.clientHeight;
      if (w <= 0 || h <= 0) return;

      modalCanvas.width = w * dpr;
      modalCanvas.height = h * dpr;
      ctx.resetTransform();
      ctx.scale(dpr, dpr);

      const priceH = h * 0.78;
      const volH = h * 0.18;
      const rightPadding = 68;
      const chartW = w - rightPadding;

      let minP = Infinity, maxP = -Infinity, maxVol = 0;
      modalCandles.forEach(c => {{
        if (c.low < minP) minP = c.low;
        if (c.high > maxP) maxP = c.high;
        if (c.volume > maxVol) maxVol = c.volume;
      }});
      const span = (maxP - minP) || 1;
      minP -= span * 0.05;
      maxP += span * 0.05;
      const fullSpan = maxP - minP;

      const getY = (p) => priceH - ((p - minP) / fullSpan) * priceH;
      const getVolY = (v) => h - 4 - (v / (maxVol || 1)) * volH;

      ctx.fillStyle = '#0b0f19';
      ctx.fillRect(0, 0, w, h);

      ctx.strokeStyle = '#1e293b';
      ctx.lineWidth = 0.8;
      ctx.fillStyle = '#64748b';
      ctx.font = '10px monospace';
      ctx.textAlign = 'left';

      const steps = 6;
      for (let i = 0; i <= steps; i++) {{
        const p = minP + (fullSpan / steps) * i;
        const y = getY(p);
        ctx.beginPath();
        ctx.moveTo(0, y);
        ctx.lineTo(chartW, y);
        ctx.stroke();
        ctx.fillText(formatPrice(p), chartW + 6, y + 3);
      }}

      const n = modalCandles.length;
      const slot = chartW / n;
      const barW = Math.max(slot * 0.68, 2);

      modalCandles.forEach((c, i) => {{
        const x = i * slot + slot / 2;
        const isBull = c.close >= c.open;
        const col = isBull ? '#10b981' : '#f43f5e';
        ctx.strokeStyle = col;
        ctx.fillStyle = col;

        ctx.beginPath();
        ctx.moveTo(x, getY(c.high));
        ctx.lineTo(x, getY(c.low));
        ctx.stroke();

        const yO = getY(c.open);
        const yC = getY(c.close);
        const topY = Math.min(yO, yC);
        const bodyH = Math.max(Math.abs(yC - yO), 1);
        ctx.fillRect(x - barW / 2, topY, barW, bodyH);

        const vY = getVolY(c.volume);
        ctx.fillStyle = isBull ? 'rgba(16, 185, 129, 0.35)' : 'rgba(244, 63, 94, 0.35)';
        ctx.fillRect(x - barW / 2, vY, barW, h - 4 - vY);
      }});

      ctx.beginPath();
      ctx.strokeStyle = '#f59e0b';
      ctx.lineWidth = 1.6;
      modalCandles.forEach((c, i) => {{
        const x = i * slot + slot / 2;
        const y = getY(c.ema5);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }});
      ctx.stroke();

      ctx.beginPath();
      ctx.strokeStyle = '#a855f7';
      ctx.lineWidth = 1.6;
      modalCandles.forEach((c, i) => {{
        const x = i * slot + slot / 2;
        const y = getY(c.ema10);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }});
      ctx.stroke();

      const last = modalCandles[modalCandles.length - 1];
      if (last) {{
        const lastY = getY(last.close);
        const isBull = last.close >= last.open;
        ctx.beginPath();
        ctx.setLineDash([4, 4]);
        ctx.strokeStyle = isBull ? '#10b981' : '#f43f5e';
        ctx.moveTo(0, lastY);
        ctx.lineTo(chartW, lastY);
        ctx.stroke();
        ctx.setLineDash([]);

        ctx.fillStyle = isBull ? '#059669' : '#e11d48';
        ctx.fillRect(chartW + 2, lastY - 9, rightPadding - 4, 18);
        ctx.fillStyle = '#ffffff';
        ctx.font = 'bold 10px monospace';
        ctx.fillText(formatPrice(last.close), chartW + 6, lastY + 4);
      }}

      if (modalHoverIndex >= 0 && modalHoverIndex < n && modalTooltip) {{
        const c = modalCandles[modalHoverIndex];
        const hx = modalHoverIndex * slot + slot / 2;
        const hy = getY(c.close);

        ctx.strokeStyle = '#94a3b8';
        ctx.setLineDash([2, 2]);
        ctx.beginPath();
        ctx.moveTo(hx, 0);
        ctx.lineTo(hx, h);
        ctx.moveTo(0, hy);
        ctx.lineTo(chartW, hy);
        ctx.stroke();
        ctx.setLineDash([]);

        const d = new Date(c.time);
        const timeStr = `${{d.getMonth()+1}}/${{d.getDate()}} ${{String(d.getHours()).padStart(2,'0')}}:${{String(d.getMinutes()).padStart(2,'0')}}`;
        const chg = (((c.close - c.open) / c.open) * 100).toFixed(2);
        const isBull = c.close >= c.open;

        modalTooltip.innerHTML = `
          <div class="flex items-center gap-2 mb-1">
            <span class="text-white font-bold">${{timeStr}}</span>
            <span class="${{isBull ? 'text-emerald-400' : 'text-rose-400'}} font-bold">${{chg > 0 ? '+' : ''}}${{chg}}%</span>
          </div>
          <div class="grid grid-cols-4 gap-x-2.5 gap-y-0.5 text-slate-400">
            <div>开: <span class="text-white">${{c.open}}</span></div>
            <div>高: <span class="text-white">${{c.high}}</span></div>
            <div>低: <span class="text-white">${{c.low}}</span></div>
            <div>收: <span class="text-white">${{c.close}}</span></div>
          </div>
          <div class="flex items-center gap-2.5 mt-1 pt-1 border-t border-slate-800 text-[10px]">
            <span class="text-amber-400">EMA5: ${{formatPrice(c.ema5)}}</span>
            <span class="text-purple-400">EMA10: ${{formatPrice(c.ema10)}}</span>
            <span class="text-slate-400">量: ${{(c.volume / 1000).toFixed(1)}}k</span>
          </div>
        `;
        modalTooltip.classList.remove('hidden');
      }} else if (modalTooltip) {{
        modalTooltip.classList.add('hidden');
      }}
    }}

    function openTvModal(sym, tvSym) {{
      if (!chartModal) return;
      modalCurrentSymbol = sym;
      modalCurrentTvSymbol = tvSym;
      modalHoverIndex = -1;

      if (modalChartTitle) modalChartTitle.textContent = `${{sym}}`;
      if (modalExternalLink) modalExternalLink.href = getTvUrl(tvSym, modalCurrentInterval);

      chartModal.classList.remove('hidden');

      if (modalActiveEngine === 'native') {{
        if (modalNativeView) modalNativeView.classList.remove('hidden');
        if (modalTvView) modalTvView.classList.add('hidden');
        fetchModalKlines(modalCurrentSymbol, modalCurrentInterval);
      }} else {{
        if (modalNativeView) modalNativeView.classList.add('hidden');
        if (modalTvView) modalTvView.classList.remove('hidden');
        if (modalTvIframe) {{
          const tvInterval = TV_INTERVAL_MAP[modalCurrentInterval] || '15';
          modalTvIframe.src = `https://s.tradingview.com/widgetembed/?symbol=${{encodeURIComponent(tvSym)}}&interval=${{tvInterval}}&theme=dark&style=1&timezone=Asia%2FShanghai&locale=zh_CN`;
        }}
      }}
    }}

    function closeTvModal() {{
      if (!chartModal) return;
      chartModal.classList.add('hidden');
      if (modalFetchController) modalFetchController.abort();
      if (modalTvIframe) modalTvIframe.src = 'about:blank';
      modalHoverIndex = -1;
      if (modalTooltip) modalTooltip.classList.add('hidden');
    }}

    if (modalNativeView) {{
      modalNativeView.addEventListener('mousemove', (e) => {{
        const rect = modalNativeView.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const chartW = modalNativeView.clientWidth - 68;
        if (x >= 0 && x <= chartW && modalCandles.length) {{
          const slot = chartW / modalCandles.length;
          modalHoverIndex = Math.min(Math.floor(x / slot), modalCandles.length - 1);
          renderModalChart();
        }}
      }});

      modalNativeView.addEventListener('mouseleave', () => {{
        modalHoverIndex = -1;
        renderModalChart();
      }});
    }}

    document.querySelectorAll('.modal-interval-btn').forEach(btn => {{
      btn.addEventListener('click', () => {{
        document.querySelectorAll('.modal-interval-btn').forEach(b => {{
          b.classList.remove('bg-sky-500/20', 'text-sky-300', 'font-bold');
          b.classList.add('hover:bg-slate-800', 'text-slate-400');
        }});
        btn.classList.add('bg-sky-500/20', 'text-sky-300', 'font-bold');
        btn.classList.remove('hover:bg-slate-800', 'text-slate-400');

        modalCurrentInterval = btn.getAttribute('data-interval') || '15m';
        if (modalExternalLink && modalCurrentTvSymbol) {{
          modalExternalLink.href = getTvUrl(modalCurrentTvSymbol, modalCurrentInterval);
        }}

        if (modalActiveEngine === 'native') {{
          fetchModalKlines(modalCurrentSymbol, modalCurrentInterval);
        }} else if (modalTvIframe && modalCurrentTvSymbol) {{
          const tvInterval = TV_INTERVAL_MAP[modalCurrentInterval] || '15';
          modalTvIframe.src = `https://s.tradingview.com/widgetembed/?symbol=${{encodeURIComponent(modalCurrentTvSymbol)}}&interval=${{tvInterval}}&theme=dark&style=1&timezone=Asia%2FShanghai&locale=zh_CN`;
        }}
      }});
    }});

    if (btnEngineNative && btnEngineTv) {{
      btnEngineNative.addEventListener('click', () => {{
        modalActiveEngine = 'native';
        btnEngineNative.className = 'px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-300 font-semibold';
        btnEngineTv.className = 'px-2 py-0.5 rounded hover:bg-slate-800 text-slate-400';
        if (modalTvView) modalTvView.classList.add('hidden');
        if (modalNativeView) modalNativeView.classList.remove('hidden');
        fetchModalKlines(modalCurrentSymbol, modalCurrentInterval);
      }});

      btnEngineTv.addEventListener('click', () => {{
        modalActiveEngine = 'tv';
        btnEngineTv.className = 'px-2 py-0.5 rounded bg-[#2962FF]/20 text-[#38bdf8] font-semibold';
        btnEngineNative.className = 'px-2 py-0.5 rounded hover:bg-slate-800 text-slate-400';
        if (modalNativeView) modalNativeView.classList.add('hidden');
        if (modalTvView) modalTvView.classList.remove('hidden');
        if (modalTvIframe && modalCurrentTvSymbol) {{
          const tvInterval = TV_INTERVAL_MAP[modalCurrentInterval] || '15';
          modalTvIframe.src = `https://s.tradingview.com/widgetembed/?symbol=${{encodeURIComponent(modalCurrentTvSymbol)}}&interval=${{tvInterval}}&theme=dark&style=1&timezone=Asia%2FShanghai&locale=zh_CN`;
        }}
      }});
    }}

    if (modalRetryBtn) {{
      modalRetryBtn.addEventListener('click', () => {{
        fetchModalKlines(modalCurrentSymbol, modalCurrentInterval);
      }});
    }}

    if (modalCloseBtn) modalCloseBtn.addEventListener('click', closeTvModal);
    if (chartModal) {{
      chartModal.addEventListener('click', (e) => {{
        if (e.target === chartModal) closeTvModal();
      }});
    }}

    window.addEventListener('resize', () => {{
      if (chartModal && !chartModal.classList.contains('hidden') && modalActiveEngine === 'native') {{
        renderModalChart();
      }}
    }});

    document.addEventListener('click', (e) => {{
      const btn = e.target.closest('.btn-preview-chart');
      if (btn) {{
        e.stopPropagation();
        openTvModal(btn.getAttribute('data-sym'), btn.getAttribute('data-tv'));
      }}
    }});

    if (toggleDrawerBtn && tvDrawer) {{
      toggleDrawerBtn.addEventListener('click', () => {{
        tvDrawer.classList.toggle('hidden');
      }});
    }}
    if (closeDrawerBtn && tvDrawer) {{
      closeDrawerBtn.addEventListener('click', () => {{
        tvDrawer.classList.add('hidden');
      }});
    }}

    document.addEventListener('keydown', (e) => {{
      const isInputActive = ['INPUT', 'TEXTAREA'].includes(document.activeElement?.tagName);

      if (e.key === 'Escape') {{
        if (chartModal && !chartModal.classList.contains('hidden')) {{
          closeTvModal();
        }} else if (tvDrawer && !tvDrawer.classList.contains('hidden')) {{
          tvDrawer.classList.add('hidden');
        }} else if (searchInput && (searchInput.value || document.activeElement === searchInput)) {{
          searchInput.value = '';
          searchQuery = '';
          searchInput.blur();
          applyFilter();
        }} else if (currentZone !== 'all') {{
          switchZone('all');
          applyFilter();
        }}
      }} else if (e.key === '/' && !isInputActive) {{
        e.preventDefault();
        if (searchInput) searchInput.focus();
      }} else if (!isInputActive) {{
        if (e.key === '1') switchZone('resonance');
        else if (e.key === '2') switchZone('neg_fr');
        else if (e.key === '3') switchZone('high_tr');
        else if (e.key === '4') switchZone('ema_bull');
        else if (e.key === '5') switchZone('all');
      }}
    }});

    function updateSettleCountdown() {{
      const timerEl = document.getElementById(CFG.settleCountdownId);
      if (!timerEl) return;
      const now = new Date();
      const nextHour = new Date(now);
      nextHour.setHours(now.getHours() + 1, 0, 0, 0);
      const diffMs = nextHour - now;
      const mins = Math.floor(diffMs / 60000);
      const secs = Math.floor((diffMs % 60000) / 1000);
      timerEl.textContent = `${{String(mins).padStart(2, '0')}}:${{String(secs).padStart(2, '0')}}`;
    }}
    setInterval(updateSettleCountdown, 1000);
    updateSettleCountdown();

    async function syncBinancePool() {{
      if (isFetching) return;
      isFetching = true;

      if (syncBtn) {{
        syncBtn.setAttribute('aria-busy', 'true');
        syncBtn.classList.add('opacity-75');
      }}

      if (pollController) {{
        pollController.abort();
      }}
      pollController = new AbortController();
      let signal = pollController.signal;
      if (typeof AbortSignal !== 'undefined' && typeof AbortSignal.timeout === 'function' && typeof AbortSignal.any === 'function') {{
        signal = AbortSignal.any([pollController.signal, AbortSignal.timeout(4500)]);
      }}

      try {{
        const [tickerRes, premiumRes] = await Promise.all([
          fetch('https://www.binance.com/fapi/v1/ticker/24hr', {{ signal }}),
          fetch('https://www.binance.com/fapi/v1/premiumIndex', {{ signal }})
        ]);

        if (!tickerRes.ok || !premiumRes.ok) return;

        const tickers = await tickerRes.json();
        const premiums = await premiumRes.json();

        const tickMap = new Map();
        tickers.forEach(t => tickMap.set(t.symbol, t));

        const premMap = new Map();
        premiums.forEach(p => premMap.set(p.symbol, p));

        const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

        poolSymbols.forEach(sym => {{
          const t = tickMap.get(sym);
          const pr = premMap.get(sym);
          const elements = rowElementMap.get(sym);
          if (!elements) return;

          if (t && elements.priceEl) {{
            const pVal = parseFloat(t.lastPrice);
            const chgVal = parseFloat(t.priceChangePercent);
            const highVal = parseFloat(t.highPrice);
            const lowVal = parseFloat(t.lowPrice);
            const volVal = parseFloat(t.quoteVolume);
            const span = Math.max(highVal - lowVal, 0.00000001);
            const posPct = Math.max(0, Math.min(100, ((pVal - lowVal) / span) * 100));
            const oldP = symPriceMap.get(sym);

            if (oldP !== undefined && oldP !== pVal && !prefersReducedMotion) {{
              elements.priceEl.animate([
                {{ color: pVal > oldP ? '#34d399' : '#f87171', transform: 'scale(1.05)' }},
                {{ color: '#ffffff', transform: 'scale(1)' }}
              ], {{
                duration: 600,
                easing: 'ease-out'
              }});
            }}

            elements.priceEl.textContent = formatPrice(pVal);
            symPriceMap.set(sym, pVal);

            if (elements.posBarEl && !isNaN(posPct)) {{
              elements.posBarEl.style.width = posPct.toFixed(1) + '%';
              elements.posBarEl.classList.remove('bg-emerald-400', 'bg-amber-400', 'bg-sky-400');
              if (posPct <= 25) {{
                elements.posBarEl.classList.add('bg-emerald-400');
              }} else if (posPct >= 75) {{
                elements.posBarEl.classList.add('bg-amber-400');
              }} else {{
                elements.posBarEl.classList.add('bg-sky-400');
              }}
            }}
            if (elements.posTextEl && !isNaN(posPct)) {{
              elements.posTextEl.textContent = Math.round(posPct) + '%';
            }}
            if (elements.posBoxEl && !isNaN(lowVal) && !isNaN(highVal)) {{
              elements.posBoxEl.setAttribute('title', `24h振幅区间: ${{formatPrice(lowVal)}} ~ ${{formatPrice(highVal)}} (现价分位: ${{Math.round(posPct)}}%)`);
            }}

            if (elements.chgEl && !isNaN(chgVal)) {{
              elements.chgEl.textContent = (chgVal >= 0 ? '+' : '') + chgVal.toFixed(2) + '%';
              elements.chgEl.classList.toggle('text-emerald-400', chgVal >= 0);
              elements.chgEl.classList.toggle('text-rose-400', chgVal < 0);
            }}

            if (elements.volEl && !isNaN(volVal)) {{
              elements.volEl.textContent = '$' + (volVal / 1e6).toFixed(1) + 'M';
            }}

            if (elements.rowEl) {{
              const cs = parseFloat(elements.rowEl.getAttribute('data-cs')) || 0;
              if (cs > 0) {{
                const currentMcap = pVal * cs;
                if (elements.mcValEl) {{
                  elements.mcValEl.textContent = '$' + (currentMcap / 1e6).toFixed(1) + 'M';
                }}
                if (elements.trEl && !isNaN(volVal)) {{
                  const trVal = currentMcap > 0 ? (volVal / currentMcap) : 0;
                  const trBadge = elements.trEl.querySelector('span') || elements.trEl;
                  trBadge.textContent = trVal.toFixed(2) + 'x';
                  trBadge.className = trVal >= 2.0
                    ? "text-purple-300 font-bold bg-purple-500/10 px-1 py-0.5 rounded border border-purple-500/20"
                    : (trVal >= 1.5
                      ? "text-purple-400 font-semibold bg-purple-500/5 px-1 py-0.5 rounded border border-purple-500/15"
                      : (trVal >= 0.8 ? "text-slate-300" : "text-slate-500"));
                  elements.rowEl.setAttribute('data-val-tr', trVal);
                  elements.rowEl.setAttribute('data-high-tr', trVal >= 1.5 ? '1' : '0');
                }}
                elements.rowEl.setAttribute('data-val-mc', currentMcap);
              }}
              elements.rowEl.setAttribute('data-val-price', pVal);
              elements.rowEl.setAttribute('data-val-pos', posPct);
              elements.rowEl.setAttribute('data-val-chg', chgVal);
              elements.rowEl.setAttribute('data-val-vol', volVal);
            }}
          }}

          if (pr) {{
            const settleVal = parseFloat(pr.lastFundingRate) * 100;
            const interval = poolIntervalMap[sym] || 8;
            const fr4hVal = settleVal * (4.0 / interval);

            if (elements.fr4hEl && !isNaN(fr4hVal)) {{
              elements.fr4hEl.textContent = (fr4hVal >= 0 ? '+' : '') + fr4hVal.toFixed(4) + '%';
              elements.fr4hEl.classList.toggle('text-rose-400', fr4hVal <= -0.025);
              elements.fr4hEl.classList.toggle('font-bold', fr4hVal <= -0.025);
              elements.fr4hEl.classList.toggle('text-slate-300', fr4hVal > -0.025);
            }}
            if (elements.frSettleEl && !isNaN(settleVal)) {{
              elements.frSettleEl.textContent = `单期: ${{settleVal >= 0 ? '+' : ''}}${{settleVal.toFixed(4)}}%`;
            }}
            if (elements.rowEl && !isNaN(fr4hVal)) {{
              elements.rowEl.setAttribute('data-val-fr', fr4hVal);
              const isNeg = fr4hVal < -0.00005;
              elements.rowEl.setAttribute('data-neg-fr', isNeg ? '1' : '0');
              const isHighTr = elements.rowEl.getAttribute('data-high-tr') === '1';
              const isEmaBull = elements.rowEl.getAttribute('data-ema-bull') === '1';
              elements.rowEl.setAttribute('data-resonance', (isEmaBull && isHighTr) ? '1' : '0');
            }}
          }}
        }});

      }} catch (e) {{
        if (e.name !== 'AbortError') {{
          console.warn('Binance pool sync error:', e);
        }}
      }} finally {{
        isFetching = false;
        if (syncBtn) {{
          syncBtn.removeAttribute('aria-busy');
          syncBtn.classList.remove('opacity-75');
        }}
      }}
    }}

    function startPolling() {{
      stopPolling();
      pollTimer = setInterval(syncBinancePool, 5000);
    }}

    function stopPolling() {{
      if (pollTimer) {{
        clearInterval(pollTimer);
        pollTimer = null;
      }}
    }}

    document.addEventListener('visibilitychange', () => {{
      if (document.hidden) {{
        stopPolling();
        if (pollController) pollController.abort();
      }} else {{
        syncBinancePool();
        startPolling();
      }}
    }});

    if (syncBtn) {{
      syncBtn.addEventListener('click', () => {{
        syncBinancePool();
      }});
    }}

    applyFilter();
    syncBinancePool();
    startPolling();
  }})();
  </script>"""


def render_index_body(candidates: list, stats: dict) -> str:
    rows_html = render_candidate_rows(candidates, row_class_name="candidate-row")
    script_html = render_hud_script(
        candidates,
        row_class_name="candidate-row",
        sync_btn_id="btn-manual-sync",
        count_badge_id="pool-count-badge",
        active_zone_pill_id="active-zone-pill",
        search_input_id="symbol-search-input",
        reset_btn_id="btn-reset-filter",
        table_body_id="candidate-table-body",
        batch_copy_btn_id="btn-batch-copy-tv",
        batch_export_btn_id="btn-batch-export-tv",
        settle_countdown_id="settle-countdown",
        drawer_id="tv-drawer",
        toggle_drawer_btn_id="btn-toggle-drawer",
        close_drawer_btn_id="btn-close-drawer",
    )

    return f"""<body class="p-2 md:p-3.5 antialiased custom-scrollbar">
  <main id="main-content" class="w-full max-w-[1600px] mx-auto space-y-2">

    <!-- Compact Command HUD & Navigation Bar -->
    <header class="bg-slate-900/95 border border-slate-800 rounded-xl px-3.5 py-2.5 shadow-lg backdrop-blur-sm">
      <div class="flex flex-col lg:flex-row lg:items-center justify-between gap-2">
        <div class="flex flex-wrap items-center gap-2">
          <h1 class="text-lg md:text-xl font-black tracking-tight text-white flex items-center gap-1.5 mr-1">
            <span>突破追多与移动保本系统</span>
            <span class="text-[10px] px-1.5 py-0.5 rounded font-mono font-normal bg-slate-800 text-slate-300 border border-slate-700">v3.5 Pro</span>
          </h1>
          <span class="px-2 py-0.5 text-[11px] font-bold rounded-md bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
            CMC Pro &lt;$30M
          </span>
          <span id="sync-status-badge" class="px-2 py-0.5 text-[11px] font-semibold rounded-md bg-emerald-500/10 text-emerald-300 border border-emerald-500/20 flex items-center gap-1.5" role="status" aria-live="polite">
            <span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-ping" aria-hidden="true"></span>
            <span>币安 5s 轮询</span>
          </span>
          <span id="settle-timer-badge" class="px-2 py-0.5 text-[11px] font-mono font-semibold rounded-md bg-rose-500/10 text-rose-300 border border-rose-500/20 flex items-center gap-1" title="距离下个整点资费结算窗口倒计时">
            <span>⏱️ 结算:</span>
            <span id="settle-countdown">--:--</span>
          </span>
        </div>

        <nav aria-label="快捷实战与复盘入口" class="flex flex-wrap items-center gap-1.5 shrink-0">
          <button id="btn-manual-sync" type="button" class="px-2.5 py-1 text-xs font-bold rounded-lg bg-slate-800 hover:bg-slate-700 text-sky-400 border border-slate-700 transition-all flex items-center gap-1 focus-visible:outline-2 focus-visible:outline-sky-400" title="立即手动刷新全池价格与资金费率">
            <span aria-hidden="true">🔄</span>
            <span>刷新</span>
          </button>
          <a href="reports/strategy_second_leg_playbook.html" class="px-2.5 py-1 text-xs font-bold rounded-lg bg-amber-500/15 hover:bg-amber-500/25 text-amber-300 border border-amber-500/30 transition-all flex items-center gap-1">
            <span>📖 战法手册</span>
          </a>
          <button id="btn-toggle-drawer" type="button" class="px-2.5 py-1 text-xs font-bold rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 transition-all flex items-center gap-1">
            <span>📐 TV 画线速查</span>
          </button>
        </nav>
      </div>
    </header>

    <!-- Collapsible TV Trendline & Risk Management Quick Reference -->
    <div id="tv-drawer" class="hidden bg-slate-900/90 border border-amber-500/30 rounded-xl p-3 text-xs space-y-2.5 transition-all animate-fadeIn">
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
          <span class="text-slate-400 block">15m 收盘跌回趋势线下方无条件清仓止损；涨幅 ≥ +6% 自动提升止损至 +1% 保本锁利，盈利随时可手动止盈。</span>
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
          <div class="text-xl font-black font-mono text-white tabular-nums">{stats['n_resonance']} <span class="text-[11px] font-normal text-slate-400 font-sans">标的</span></div>
          <div class="text-[10px] text-amber-400/90 font-mono truncate">含{stats['n_triple']}逼空加成</div>
        </div>
        <div class="text-[10px] text-slate-400 border-t border-slate-800/80 pt-1 truncate" title="{stats['sample_resonance']}">
          代表: {stats['sample_resonance']}
        </div>
      </div>

      <div data-zone="neg_fr" class="kpi-card card-neg-fr px-3 py-2 rounded-xl bg-slate-900 border border-slate-800 hover:border-rose-500/40 flex flex-col justify-between space-y-1">
        <div class="flex items-center justify-between gap-1">
          <span class="font-semibold text-rose-400 text-xs truncate">🔥 负费率逼空区</span>
          <span class="px-1.5 py-0.2 rounded text-[9px] font-mono text-rose-300/80 bg-rose-500/10 border border-rose-500/20 shrink-0">4h&lt;0</span>
        </div>
        <div class="flex items-baseline justify-between gap-2">
          <div class="text-xl font-black font-mono text-white tabular-nums">{stats['n_neg_fr']} <span class="text-[11px] font-normal text-slate-400 font-sans">标的</span></div>
          <div class="text-[10px] text-rose-400/90 font-mono truncate">空头贴水倒挂</div>
        </div>
        <div class="text-[10px] text-slate-400 border-t border-slate-800/80 pt-1 truncate" title="{stats['sample_neg_fr']}">
          贴水深: {stats['sample_neg_fr']}
        </div>
      </div>

      <div data-zone="high_tr" class="kpi-card card-high-tr px-3 py-2 rounded-xl bg-slate-900 border border-slate-800 hover:border-purple-500/40 flex flex-col justify-between space-y-1">
        <div class="flex items-center justify-between gap-1">
          <span class="font-semibold text-purple-400 text-xs truncate">⚡ 巨量高换手区</span>
          <span class="px-1.5 py-0.2 rounded text-[9px] font-mono text-purple-300/80 bg-purple-500/10 border border-purple-500/20 shrink-0">≥1.5x</span>
        </div>
        <div class="flex items-baseline justify-between gap-2">
          <div class="text-xl font-black font-mono text-white tabular-nums">{stats['n_high_tr']} <span class="text-[11px] font-normal text-slate-400 font-sans">标的</span></div>
          <div class="text-[10px] text-purple-400/90 font-mono truncate">筹码充分洗刷</div>
        </div>
        <div class="text-[10px] text-slate-400 border-t border-slate-800/80 pt-1 truncate" title="{stats['sample_high_tr']}">
          最高: {stats['sample_high_tr']}
        </div>
      </div>

      <div data-zone="ema_bull" class="kpi-card card-ema-bull px-3 py-2 rounded-xl bg-slate-900 border border-slate-800 hover:border-emerald-500/40 flex flex-col justify-between space-y-1">
        <div class="flex items-center justify-between gap-1">
          <span class="font-semibold text-emerald-400 text-xs truncate">📈 1h EMA顺势区</span>
          <span class="px-1.5 py-0.2 rounded text-[9px] font-mono text-emerald-300/80 bg-emerald-500/10 border border-emerald-500/20 shrink-0">5&gt;10</span>
        </div>
        <div class="flex items-baseline justify-between gap-2">
          <div class="text-xl font-black font-mono text-white tabular-nums">{stats['n_ema_bull']} <span class="text-[11px] font-normal text-slate-400 font-sans">标的</span></div>
          <div class="text-[10px] text-emerald-400/90 font-mono truncate">右侧动能转强</div>
        </div>
        <div class="text-[10px] text-slate-400 border-t border-slate-800/80 pt-1 truncate" title="{stats['sample_ema_bull']}">
          多头: {stats['sample_ema_bull']}
        </div>
      </div>

      <div data-zone="all" class="kpi-card card-all px-3 py-2 rounded-xl bg-slate-900 border border-slate-800 hover:border-sky-500/40 flex flex-col justify-between space-y-1 col-span-2 md:col-span-1">
        <div class="flex items-center justify-between gap-1">
          <span class="font-semibold text-sky-400 text-xs truncate">🌐 全量候选底池</span>
          <span class="px-1.5 py-0.2 rounded text-[9px] font-mono text-sky-300/80 bg-sky-500/10 border border-sky-500/20 shrink-0">&lt;$30M</span>
        </div>
        <div class="flex items-baseline justify-between gap-2">
          <div class="text-xl font-black font-mono text-white tabular-nums">{stats['n_total']} <span class="text-[11px] font-normal text-slate-400 font-sans">标的</span></div>
          <div class="text-[10px] text-sky-400/90 font-mono truncate">含{stats['n_overflow']}溢出跟踪</div>
        </div>
        <div class="text-[10px] text-slate-400 border-t border-slate-800/80 pt-1 truncate">
          &lt;3000wu 底池 + 7d 溢出跟踪
        </div>
      </div>
    </section>

    <!-- Candidate Pool & TV Launchpad Table Section -->
    <section aria-labelledby="pool-table-heading" class="bg-slate-900 border border-slate-800 rounded-xl p-2.5 md:p-3 shadow-sm space-y-2">
{EXECUTION_DISCIPLINE_RIBBON_HTML}

      <!-- Search, TV Batch Export & Filter Controls -->
      <div class="flex flex-col lg:flex-row items-stretch lg:items-center justify-between gap-2 bg-slate-950/70 px-2.5 py-1.5 rounded-xl border border-slate-800">
        <div class="flex items-center gap-1.5 flex-wrap">
          <span id="active-zone-pill" class="px-2 py-0.5 rounded-md text-xs font-bold bg-amber-500/20 text-amber-300 border border-amber-500/30 flex items-center gap-1">
            <span>当前筛选: 👑 顺势高换手共振区</span>
          </span>
          <span id="pool-count-badge" class="text-xs font-mono px-2 py-0.5 rounded-md bg-slate-900 text-slate-300 border border-slate-800">
            {stats['n_resonance']} / {stats['n_total']} 标的
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
{TABLE_THEAD_HTML}
          <tbody id="candidate-table-body" class="divide-y divide-slate-800/60 text-slate-300">
{EMPTY_SEARCH_ROW_HTML}{rows_html}
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

{CHART_MODAL_AND_TOAST_HTML}

{script_html}
</body>
</html>
"""


def render_playbook_body(candidates: list, stats: dict) -> str:
    rows_html = render_candidate_rows(candidates, row_class_name="playbook-row")
    script_html = render_hud_script(
        candidates,
        row_class_name="playbook-row",
        sync_btn_id="btn-manual-sync-playbook",
        count_badge_id="playbook-count-badge",
        active_zone_pill_id="playbook-active-zone-pill",
        search_input_id="symbol-search-playbook",
        reset_btn_id="btn-reset-playbook",
        table_body_id="playbook-table-body",
        batch_copy_btn_id="btn-batch-copy-playbook",
        batch_export_btn_id="btn-batch-export-playbook",
        settle_countdown_id="settle-countdown-pb",
    )

    return f"""<body class="p-2 md:p-3.5 antialiased custom-scrollbar">
  <div class="w-full max-w-[1600px] mx-auto space-y-2.5">

    <!-- Compact Header -->
    <header class="bg-slate-900/90 border border-slate-800 rounded-xl px-3.5 py-2.5 shadow-lg flex flex-col lg:flex-row lg:items-center justify-between gap-2">
      <div class="flex flex-wrap items-center gap-2">
        <h1 class="text-lg md:text-xl font-black tracking-tight text-white mr-1">
          突破追多与移动保本战法手册
        </h1>
        <span class="px-2 py-0.5 text-[11px] font-semibold rounded-md bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
          CMC Pro &lt;$30M
        </span>
        <span class="px-2 py-0.5 text-[11px] font-semibold rounded-md bg-purple-500/10 text-purple-400 border border-purple-500/20">
          4 步战法 + 移动锁利风控
        </span>
        <span id="sync-status-badge" class="px-2 py-0.5 text-[11px] font-semibold rounded-md bg-emerald-500/10 text-emerald-300 border border-emerald-500/20 flex items-center gap-1.5" role="status" aria-live="polite">
          <span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" aria-hidden="true"></span>
          <span>5s 同步</span>
        </span>
        <span id="settle-timer-badge" class="px-2 py-0.5 text-[11px] font-mono font-semibold rounded-md bg-rose-500/10 text-rose-300 border border-rose-500/20 flex items-center gap-1" title="距离下个整点资费结算窗口倒计时">
          <span>⏱️ 结算:</span>
          <span id="settle-countdown-pb">--:--</span>
        </span>
      </div>

      <nav aria-label="战法终端快捷导航" class="flex flex-wrap items-center gap-1.5 shrink-0">
        <button id="btn-manual-sync-playbook" type="button" class="px-2.5 py-1 text-xs font-bold rounded-lg bg-slate-800 hover:bg-slate-700 text-sky-400 border border-slate-700 transition-all flex items-center gap-1 focus-visible:outline-2 focus-visible:outline-sky-400" title="立即手动刷新全池价格与资金费率">
          <span aria-hidden="true">🔄</span>
          <span>刷新</span>
        </button>
        <a href="../index.html" class="px-3 py-1 text-xs font-bold rounded-lg bg-sky-600 hover:bg-sky-500 text-white transition-all shadow-md shadow-sky-600/20">
          <span>➔ 返回实战工作台</span>
        </a>
      </nav>
    </header>

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

    <!-- Step 1: Candidate Pool & Opportunity Zones -->
    <div class="bg-slate-900 border border-slate-800 rounded-xl p-2.5 md:p-3 shadow-sm space-y-2">
      <!-- Compact 5 KPI Cards -->
      <div class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-2 text-xs">
        <div data-zone="resonance" class="kpi-card card-resonance active px-3 py-2 rounded-xl bg-slate-950/60 border border-amber-500/40 flex flex-col justify-between space-y-1">
          <div class="flex items-center justify-between gap-1">
            <span class="font-bold text-amber-300 text-xs truncate">👑 顺势高换手共振区</span>
            <span class="px-1.5 py-0.2 rounded text-[9px] font-mono font-bold bg-amber-500/20 text-amber-300 shrink-0">破线开多</span>
          </div>
          <div class="flex items-baseline justify-between gap-2">
            <div class="text-xl font-black font-mono text-white tabular-nums">{stats['n_resonance']} <span class="text-[11px] font-normal text-slate-400">标的</span></div>
            <div class="text-[10px] text-amber-400/90 font-mono truncate">含{stats['n_triple']}逼空</div>
          </div>
          <div class="text-[10px] text-slate-400 border-t border-slate-800/80 pt-1 truncate">{stats['sample_resonance']}</div>
        </div>

        <div data-zone="neg_fr" class="kpi-card card-neg-fr px-3 py-2 rounded-xl bg-slate-950/60 border border-slate-800 hover:border-rose-500/40 flex flex-col justify-between space-y-1">
          <div class="flex items-center justify-between gap-1">
            <span class="font-semibold text-rose-400 text-xs truncate">🔥 负费率逼空区</span>
            <span class="text-[9px] font-mono text-rose-300/80 shrink-0">&lt;0</span>
          </div>
          <div class="text-xl font-black font-mono text-white tabular-nums">{stats['n_neg_fr']} <span class="text-[11px] font-normal text-slate-400">标的</span></div>
          <div class="text-[10px] text-slate-400 border-t border-slate-800/80 pt-1 truncate">{stats['sample_neg_fr']}</div>
        </div>

        <div data-zone="high_tr" class="kpi-card card-high-tr px-3 py-2 rounded-xl bg-slate-950/60 border border-slate-800 hover:border-purple-500/40 flex flex-col justify-between space-y-1">
          <div class="flex items-center justify-between gap-1">
            <span class="font-semibold text-purple-400 text-xs truncate">⚡ 巨量高换手区</span>
            <span class="text-[9px] font-mono text-purple-300/80 shrink-0">&ge;1.5x</span>
          </div>
          <div class="text-xl font-black font-mono text-white tabular-nums">{stats['n_high_tr']} <span class="text-[11px] font-normal text-slate-400">标的</span></div>
          <div class="text-[10px] text-slate-400 border-t border-slate-800/80 pt-1 truncate">{stats['sample_high_tr']}</div>
        </div>

        <div data-zone="ema_bull" class="kpi-card card-ema-bull px-3 py-2 rounded-xl bg-slate-950/60 border border-slate-800 hover:border-emerald-500/40 flex flex-col justify-between space-y-1">
          <div class="flex items-center justify-between gap-1">
            <span class="font-semibold text-emerald-400 text-xs truncate">📈 1h EMA顺势区</span>
            <span class="text-[9px] font-mono text-emerald-300/80 shrink-0">5&gt;10</span>
          </div>
          <div class="text-xl font-black font-mono text-white tabular-nums">{stats['n_ema_bull']} <span class="text-[11px] font-normal text-slate-400">标的</span></div>
          <div class="text-[10px] text-slate-400 border-t border-slate-800/80 pt-1 truncate">{stats['sample_ema_bull']}</div>
        </div>

        <div data-zone="all" class="kpi-card card-all px-3 py-2 rounded-xl bg-slate-950/60 border border-slate-800 hover:border-sky-500/40 flex flex-col justify-between space-y-1 col-span-2 md:col-span-1">
          <div class="flex items-center justify-between gap-1">
            <span class="font-semibold text-sky-400 text-xs truncate">🌐 全量底池</span>
            <span class="text-[9px] font-mono text-sky-300/80 shrink-0">&lt;3000w</span>
          </div>
          <div class="flex items-baseline justify-between gap-2">
            <div class="text-xl font-black font-mono text-white tabular-nums">{stats['n_total']} <span class="text-[11px] font-normal text-slate-400">标的</span></div>
            <div class="text-[10px] text-sky-400/90 font-mono truncate">含{stats['n_overflow']}溢出</div>
          </div>
          <div class="text-[10px] text-slate-400 border-t border-slate-800/80 pt-1 truncate">&lt;3000wu 底池 + 7d 溢出跟踪</div>
        </div>
      </div>

{EXECUTION_DISCIPLINE_RIBBON_HTML}

      <!-- Quick Search & TV Watchlist Controls -->
      <div class="flex flex-col lg:flex-row items-stretch lg:items-center justify-between gap-2 bg-slate-950/70 px-2.5 py-1.5 rounded-xl border border-slate-800">
        <div class="flex items-center gap-1.5 flex-wrap">
          <div id="playbook-active-zone-pill" class="text-xs font-bold text-amber-300 px-2 py-0.5 rounded-md bg-amber-500/10 border border-amber-500/20">
            当前筛选: 👑 顺势高换手共振区
          </div>
          <span id="playbook-count-badge" class="text-xs font-mono px-2 py-0.5 rounded-md bg-slate-900 text-slate-300 border border-slate-800">
            {stats['n_resonance']} / {stats['n_total']} 标的
          </span>

          <div class="flex items-center gap-1.5 border-l border-slate-800 pl-2">
            <div class="relative group">
              <button id="btn-batch-copy-playbook" type="button" class="px-2 py-0.5 text-xs font-semibold rounded-md bg-sky-500/15 hover:bg-sky-500/25 text-sky-300 border border-sky-500/30 transition-all flex items-center gap-1 shadow-sm">
                <span>📋 复制 TV 自选</span>
              </button>
              <div class="absolute bottom-full left-1/2 -translate-x-1/2 mb-1.5 hidden group-hover:block z-30 w-56 p-2 bg-slate-800 text-[11px] text-slate-200 rounded-lg shadow-xl border border-slate-700 pointer-events-none text-center">
                💡 复制后在 TradingView 自选列表直接按 Ctrl+V (或 Cmd+V) 即可批量添加！
              </div>
            </div>
            <div class="relative group">
              <button id="btn-batch-export-playbook" type="button" class="px-2 py-0.5 text-xs font-semibold rounded-md bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 transition-all flex items-center gap-1">
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
            <input id="symbol-search-playbook" type="search" placeholder="搜代号 [/] (如 PIPPIN, ZKC)..." class="w-full px-2.5 py-1 text-xs bg-slate-900 border border-slate-700 rounded-lg text-slate-200 placeholder-slate-500 focus:outline-none focus:border-sky-500 focus:ring-1 focus:ring-sky-500">
            <span class="absolute right-2 top-1.5 text-xs text-slate-500 pointer-events-none">🔍</span>
          </div>
          <button id="btn-reset-playbook" type="button" class="px-2.5 py-1 text-xs text-slate-400 hover:text-white bg-slate-800 hover:bg-slate-700 rounded-lg border border-slate-700 transition-all shrink-0">
            重置
          </button>
        </div>
      </div>

      <!-- Live Candidates Table -->
      <div tabindex="0" role="region" aria-label="第二段候选池实时数据表" class="overflow-x-auto max-h-[calc(100vh-280px)] min-h-[380px] rounded-xl border border-slate-800 focus-visible:outline-2 focus-visible:outline-sky-400 custom-scrollbar">
        <table class="w-full text-left text-xs font-mono relative">
          <caption class="sr-only">突破追多与移动保本候选池标的列表</caption>
{TABLE_THEAD_HTML}
          <tbody id="playbook-table-body" class="divide-y divide-slate-800/60 text-slate-300">
{EMPTY_SEARCH_ROW_HTML}{rows_html}
          </tbody>
        </table>
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
              点击直达 TV 专属云端图表，开启对数坐标 (Alt+L)，在 15m 周期连接回踩高点绘制下降趋势线。云端守护进程自动秒级感知并提取对数几何坐标。
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
              入场后若 15m 收盘跌回趋势线下方无条件清仓止损；当浮盈突破 ≥ +6% 后，自动将止损线拉升至开仓价 +1% 保本锁利，盈利随时可自由止盈。
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
              当标的涨幅相比开仓均价拉开 ≥ +6% 安全垫时，系统自动锁定保本止损线至 +1%，确保该笔交易绝不会转盈为亏，从容博弈第二段翻倍主升。
            </p>
          </div>
        </div>
      </div>
    </div>

  </div>

{CHART_MODAL_AND_TOAST_HTML}

{script_html}
</body>
</html>
"""

