(function (global) {
  'use strict';

  const Common = global.SecondLegCommon;
  const {
    TV_INTERVAL_MAP,
    escapeHtml,
    isFiniteNum,
    formatCryptoPrice,
    formatSignedPct,
    getTvUrl,
    fetchJsonWithTimeout,
    formatBeijingTime,
    focusSafely,
    copyText,
  } = Common;

  const VALID_BAR_COUNTS = new Set([60, 120, 168]);
  const RIGHT_AXIS_WIDTH = 78;
  const TV_IFRAME_TIMEOUT_MS = 10000;
  const INTERVAL_HOTKEY_MAP = Object.freeze({
    '1': '15m',
    '2': '1h',
    '3': '4h',
    '4': '1d',
  });

  let modalCurrentSymbol = '';
  let modalCurrentTvSymbol = '';
  let modalCurrentInterval = '15m';
  let modalVisibleBars = 120;
  let modalActiveEngine = 'native';
  let modalCandles = [];
  let modalHoverIndex = -1;
  let modalNavTotal = 0;
  let modalContext = null;
  let modalContextKey = '';
  let modalFetchController = null;
  let modalRenderRafId = null;
  let tvLoadTimer = null;
  let tvLoadSeq = 0;
  let onVisibleBarsChange = null;
  let onNavigateSymbol = null;
  let supportsDisplayTransition;
  let lastFocusedElement = null;

  let chartModal = null;
  let modalHeaderToolbar = null;
  let modalCloseBtn = null;
  let modalCopyBtn = null;
  let modalNavGroup = null;
  let modalPrevBtn = null;
  let modalNextBtn = null;
  let modalNavPos = null;
  let modalChartTitle = null;
  let modalChartPrice = null;
  let modalExternalLink = null;
  let modalHudEma5 = null;
  let modalHudEma10 = null;
  let modalHudEmaSpread = null;
  let modalHudContext = null;
  let modalNativeView = null;
  let modalTvView = null;
  let modalTvIframe = null;
  let modalTvLoading = null;
  let modalTvFallback = null;
  let modalTvFallbackText = null;
  let modalTvSwitchNativeBtn = null;
  let modalTvOpenExtBtn = null;
  let modalCanvas = null;
  let modalTooltip = null;
  let modalLoading = null;
  let modalError = null;
  let modalErrorText = null;
  let modalRetryBtn = null;
  let btnEngineNative = null;
  let btnEngineTv = null;
  let modalBarsGroup = null;
  let modalBarsBtns = [];
  let modalIntervalBtns = [];

  let baseChartCanvas = null;
  let baseChartKey = '';
  let modalChartMetrics = null;
  let tooltipRefs = null;
  let modalDomBound = false;
  let modalResizeObserver = null;

  function buildModalShellHtml() {
    return `<dialog id="chart-modal" tabindex="-1" closedby="any" aria-labelledby="modal-chart-title" class="chart-modal-dialog bg-[#10151d] border border-[#324055] rounded-[6px] w-[calc(100vw-1rem)] max-w-5xl h-[85vh] max-h-[740px] flex-col overflow-hidden">
      <div id="modal-header-toolbar" class="px-2.5 sm:px-3 py-1.5 bg-[#0d1219] border-b border-[#222b38] flex flex-wrap items-center justify-between gap-1 sm:gap-2 shrink-0">
        <div class="flex items-center gap-1 sm:gap-2 flex-wrap">
          <span id="modal-nav-group" hidden class="inline-flex items-center divide-x divide-[#283446] border border-[#283446] rounded-[4px] overflow-hidden bg-[#0b0e13]">
            <button type="button" id="modal-prev-btn" class="w-8 h-8 hover:bg-[#192231] text-[#9fb0c5] hover:text-[#f1f5f9] inline-flex items-center justify-center transition-colors" title="上一个标的 (←)" aria-label="上一个标的">
              <svg class="w-3 h-3" viewBox="0 0 12 12" fill="none" aria-hidden="true"><path d="M7.5 2.5L4 6L7.5 9.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
            </button>
            <span id="modal-nav-pos" class="px-1.5 text-[10px] font-mono tabular-nums text-[#7f91a8]">--</span>
            <button type="button" id="modal-next-btn" class="w-8 h-8 hover:bg-[#192231] text-[#9fb0c5] hover:text-[#f1f5f9] inline-flex items-center justify-center transition-colors" title="下一个标的 (→)" aria-label="下一个标的">
              <svg class="w-3 h-3" viewBox="0 0 12 12" fill="none" aria-hidden="true"><path d="M4.5 2.5L8 6L4.5 9.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>
            </button>
          </span>
          <h2 id="modal-chart-title" class="font-bold text-[#f1f5f9] text-sm sm:text-[15px] font-sans tracking-tight"></h2>
          <span id="modal-chart-price" class="font-mono tabular-nums text-[#38d99f] font-bold text-xs sm:text-[13px]">--</span>
          <span role="group" aria-label="K线时间周期" class="inline-flex items-center divide-x divide-[#283446] border border-[#283446] rounded-[4px] overflow-hidden bg-[#0b0e13] text-[11px] font-mono">
            <button type="button" aria-pressed="true" class="modal-interval-btn px-1.5 py-0.5 bg-[#192231] text-[#f1f5f9] font-semibold" data-interval="15m">15m</button>
            <button type="button" aria-pressed="false" class="modal-interval-btn px-1.5 py-0.5 hover:bg-[#151c27] text-[#9fb0c5]" data-interval="1h">1h</button>
            <button type="button" aria-pressed="false" class="modal-interval-btn px-1.5 py-0.5 hover:bg-[#151c27] text-[#9fb0c5]" data-interval="4h">4h</button>
            <button type="button" aria-pressed="false" class="modal-interval-btn px-1.5 py-0.5 hover:bg-[#151c27] text-[#9fb0c5]" data-interval="1d">1D</button>
          </span>
          <span id="modal-bars-group" role="group" aria-label="显示K线根数" class="inline-flex items-center divide-x divide-[#283446] border border-[#283446] rounded-[4px] overflow-hidden bg-[#0b0e13] text-[11px] font-sans tabular-nums" title="显示 K 线根数">
            <button type="button" aria-pressed="false" class="modal-bars-btn px-1.5 py-0.5 hover:bg-[#151c27] text-[#9fb0c5]" data-bars="60">60根</button>
            <button type="button" aria-pressed="true" class="modal-bars-btn px-1.5 py-0.5 bg-[#192231] text-[#f1f5f9] font-semibold" data-bars="120">120根</button>
            <button type="button" aria-pressed="false" class="modal-bars-btn px-1.5 py-0.5 hover:bg-[#151c27] text-[#9fb0c5]" data-bars="168">168根</button>
          </span>
        </div>
        <div class="flex items-center gap-1 sm:gap-2 ml-auto">
          <span role="group" aria-label="图表引擎切换" class="inline-flex items-center divide-x divide-[#283446] border border-[#283446] rounded-[4px] overflow-hidden bg-[#0b0e13] text-[11px] font-sans">
            <button type="button" id="btn-chart-engine-native" aria-pressed="true" class="px-1.5 py-0.5 bg-[#192231] text-[#f1f5f9] font-semibold">极速K线</button>
            <button type="button" id="btn-chart-engine-tv" aria-pressed="false" class="px-1.5 py-0.5 hover:bg-[#151c27] text-[#9fb0c5]">TV内嵌</button>
          </span>
          <button id="modal-copy-btn" type="button" class="px-2 py-0.5 rounded-[4px] bg-[#151c27] hover:bg-[#1d2736] text-[#9fb0c5] hover:text-[#f1f5f9] border border-[#283446] text-[11px] font-sans inline-flex items-center gap-1 transition-colors" title="复制 TV 代码 (c)">
            <span>复制代码</span>
          </button>
          <a id="modal-external-link" href="#" target="_blank" rel="noopener noreferrer" class="btn-primary-cobalt px-2 py-0.5 rounded-[4px] text-[11px] inline-flex items-center gap-1 transition-colors" title="在 TradingView 打开专属画线布局">
            <span>TV 画线</span>
            <svg class="w-3 h-3" viewBox="0 0 12 12" fill="none" aria-hidden="true">
              <path d="M3.5 8.5L8.5 3.5M8.5 3.5H4.5M8.5 3.5V7.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
          </a>
          <button id="modal-close-btn" type="button" class="w-8 h-8 rounded-[4px] bg-[#151c27] hover:bg-[#222b38] text-[#9fb0c5] hover:text-[#f1f5f9] border border-[#283446] inline-flex items-center justify-center transition-colors" title="关闭 (Esc)" aria-label="关闭弹窗">
            <svg class="w-3.5 h-3.5" viewBox="0 0 16 16" fill="none" aria-hidden="true">
              <path d="M4 4L12 12M12 4L4 12" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
            </svg>
          </button>
        </div>
      </div>
      <div id="modal-context-hud" class="modal-hud-strip shrink-0">
        <div class="flex items-center gap-3 flex-wrap">
          <span class="inline-flex items-center gap-1.5 font-mono tabular-nums"><span class="w-2 h-2 rounded-full bg-[#f0b429]" aria-hidden="true"></span><span id="modal-hud-ema5" class="text-[#f5c451]">EMA5 --</span></span>
          <span class="inline-flex items-center gap-1.5 font-mono tabular-nums"><span class="w-2 h-2 rounded-full" style="background-color: #60a5fa" aria-hidden="true"></span><span id="modal-hud-ema10" class="text-[#8ab8ff]">EMA10 --</span></span>
          <span id="modal-hud-ema-spread" class="font-mono tabular-nums text-[#9fb0c5]">--</span>
        </div>
        <div id="modal-hud-context" class="hidden flex items-center gap-1.5 flex-wrap"></div>
      </div>
      <div id="modal-chart-stage" tabindex="-1" autofocus class="flex-1 w-full min-h-0 bg-[#0b0e13] relative overflow-hidden">
        <div id="modal-native-chart-view" class="w-full h-full relative">
          <canvas id="modal-kline-canvas" role="img" aria-label="K线走势图" class="w-full h-full block select-none"></canvas>
          <div id="modal-chart-tooltip" class="absolute top-2.5 left-3 bg-[#10151d]/95 border border-[#324055] px-3 py-2 rounded-[4px] text-[11px] font-mono tabular-nums text-[#f1f5f9] hidden pointer-events-none z-20"></div>
          <div id="modal-chart-loading" class="absolute inset-0 bg-[#0b0e13]/80 flex flex-col items-center justify-center gap-2 text-[#9fb0c5] text-xs font-sans font-medium z-30">
            <span>正在拉取实时 K 线序列...</span>
          </div>
          <div id="modal-chart-error" class="hidden absolute inset-0 bg-[#0b0e13]/90 flex flex-col items-center justify-center gap-3 text-[#f8717e] text-xs font-sans font-medium z-30 px-4 text-center">
            <span id="modal-chart-error-text">K 线序列读取失败，请检查网络连接后重试</span>
            <button type="button" id="modal-chart-retry-btn" class="px-3 py-1.5 bg-[#151c27] hover:bg-[#1d2736] text-[#f1f5f9] rounded-[4px] border border-[#324055] text-xs">重新加载</button>
          </div>
        </div>
        <div id="modal-tv-view" class="w-full h-full hidden relative bg-[#0b0e13]">
          <iframe id="modal-tv-iframe" class="w-full h-full border-0" title="TradingView 实时图表" referrerpolicy="no-referrer" allowfullscreen></iframe>
          <div id="modal-tv-loading" class="hidden absolute inset-0 bg-[#0b0e13]/80 flex flex-col items-center justify-center gap-2 text-[#9fb0c5] text-xs font-sans font-medium z-20">
            <span>正在加载 TradingView 图表...</span>
          </div>
          <div id="modal-tv-fallback" class="hidden absolute inset-0 bg-[#0b0e13]/90 flex flex-col items-center justify-center gap-3 text-xs font-sans z-30 px-4 text-center">
            <span id="modal-tv-fallback-text" class="text-[#f5c451]">TradingView 内嵌图表加载超时或受网络限制</span>
            <div class="flex items-center justify-center gap-2 flex-wrap">
              <button type="button" id="modal-tv-switch-native-btn" class="px-3 py-1.5 bg-[#151c27] hover:bg-[#1d2736] text-[#f1f5f9] rounded-[4px] border border-[#324055] text-xs font-medium transition-colors">切回极速K线</button>
              <a id="modal-tv-open-ext-btn" href="#" target="_blank" rel="noopener noreferrer" class="btn-primary-cobalt px-3 py-1.5 rounded-[4px] text-xs inline-flex items-center gap-1 transition-colors">
                <span>新窗口打开</span>
                <svg class="w-3 h-3" viewBox="0 0 12 12" fill="none" aria-hidden="true">
                  <path d="M3.5 8.5L8.5 3.5M8.5 3.5H4.5M8.5 3.5V7.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
                </svg>
              </a>
            </div>
          </div>
        </div>
      </div>
    </dialog>`;
  }

  function resolveModalDomRefs() {
    if (typeof global.document === 'undefined' || typeof global.document.getElementById !== 'function') {
      return;
    }
    chartModal = global.document.getElementById('chart-modal');
    if (!chartModal && global.document.body && typeof global.document.createElement === 'function') {
      const wrap = global.document.createElement('div');
      wrap.innerHTML = buildModalShellHtml();
      const created = wrap.firstElementChild;
      if (created && typeof global.document.body.appendChild === 'function') {
        global.document.body.appendChild(created);
        chartModal = created;
      }
    }
    modalHeaderToolbar = global.document.getElementById('modal-header-toolbar');
    modalCloseBtn = global.document.getElementById('modal-close-btn');
    modalCopyBtn = global.document.getElementById('modal-copy-btn');
    modalNavGroup = global.document.getElementById('modal-nav-group');
    modalPrevBtn = global.document.getElementById('modal-prev-btn');
    modalNextBtn = global.document.getElementById('modal-next-btn');
    modalNavPos = global.document.getElementById('modal-nav-pos');
    modalChartTitle = global.document.getElementById('modal-chart-title');
    modalChartPrice = global.document.getElementById('modal-chart-price');
    modalExternalLink = global.document.getElementById('modal-external-link');
    modalHudEma5 = global.document.getElementById('modal-hud-ema5');
    modalHudEma10 = global.document.getElementById('modal-hud-ema10');
    modalHudEmaSpread = global.document.getElementById('modal-hud-ema-spread');
    modalHudContext = global.document.getElementById('modal-hud-context');
    modalNativeView = global.document.getElementById('modal-native-chart-view');
    modalTvView = global.document.getElementById('modal-tv-view');
    modalTvIframe = global.document.getElementById('modal-tv-iframe');
    modalTvLoading = global.document.getElementById('modal-tv-loading');
    modalTvFallback = global.document.getElementById('modal-tv-fallback');
    modalTvFallbackText = global.document.getElementById('modal-tv-fallback-text');
    modalTvSwitchNativeBtn = global.document.getElementById('modal-tv-switch-native-btn');
    modalTvOpenExtBtn = global.document.getElementById('modal-tv-open-ext-btn');
    modalCanvas = global.document.getElementById('modal-kline-canvas');
    modalTooltip = global.document.getElementById('modal-chart-tooltip');
    modalLoading = global.document.getElementById('modal-chart-loading');
    modalError = global.document.getElementById('modal-chart-error');
    modalErrorText = global.document.getElementById('modal-chart-error-text');
    modalRetryBtn = global.document.getElementById('modal-chart-retry-btn');
    btnEngineNative = global.document.getElementById('btn-chart-engine-native');
    btnEngineTv = global.document.getElementById('btn-chart-engine-tv');
    modalBarsGroup = global.document.getElementById('modal-bars-group');
    modalBarsBtns = typeof global.document.querySelectorAll === 'function' ? global.document.querySelectorAll('.modal-bars-btn') : [];
    modalIntervalBtns = typeof global.document.querySelectorAll === 'function' ? global.document.querySelectorAll('.modal-interval-btn') : [];
  }

  function canTransitionDisplay() {
    if (supportsDisplayTransition !== undefined) return supportsDisplayTransition;
    if (
      !global.CSS ||
      typeof global.CSS.supports !== 'function' ||
      !global.CSS.supports('transition-behavior', 'allow-discrete') ||
      !global.document ||
      !global.document.body ||
      typeof global.document.createElement !== 'function' ||
      typeof global.getComputedStyle !== 'function'
    ) {
      supportsDisplayTransition = false;
      return false;
    }
    const probe = global.document.createElement('div');
    probe.style.cssText = 'transition: display 1s allow-discrete !important; display: block;';
    global.document.body.appendChild(probe);
    global.getComputedStyle(probe).display;
    probe.style.display = 'none';
    supportsDisplayTransition = global.getComputedStyle(probe).display === 'block';
    probe.remove();
    return supportsDisplayTransition;
  }

  function supportsTopLayerExit() {
    return Boolean(
      global.CSS &&
      typeof global.CSS.supports === 'function' &&
      global.CSS.supports('overlay', 'auto') &&
      canTransitionDisplay()
    );
  }

  async function closeTopLayer(dialogEl) {
    if (!dialogEl || !dialogEl.open) return;
    if (!supportsTopLayerExit() && typeof dialogEl.getAnimations === 'function') {
      dialogEl.setAttribute('data-closing', '');
      const animations = dialogEl.getAnimations({ subtree: true });
      if (animations.length > 0) {
        await Promise.race([
          Promise.allSettled(animations.map((a) => a.finished)),
          new Promise((r) => setTimeout(r, 400)),
        ]);
      }
      if (!dialogEl.hasAttribute('data-closing')) return;
      dialogEl.removeAttribute('data-closing');
    }
    if (dialogEl.open) {
      dialogEl.close();
    }
  }

  // SMA-seeded EMA matching scripts/scan_second_leg_pool.py calc_ema
  function calcEMAArray(candlesList, period) {
    const n = candlesList.length;
    const res = new Array(n).fill(null);
    if (n < period) return res;
    let sum = 0;
    for (let i = 0; i < period; i++) {
      sum += candlesList[i].close;
    }
    let ema = sum / period;
    res[period - 1] = ema;
    const alpha = 2.0 / (period + 1);
    for (let i = period; i < n; i++) {
      ema = alpha * candlesList[i].close + (1 - alpha) * ema;
      res[i] = ema;
    }
    return res;
  }

  function formatCompactVolume(vol) {
    if (!isFiniteNum(vol)) return '--';
    const v = Math.abs(Number(vol));
    if (v >= 1e9) return (v / 1e9).toFixed(2) + 'B';
    if (v >= 1e6) return (v / 1e6).toFixed(2) + 'M';
    if (v >= 1e3) return (v / 1e3).toFixed(1) + 'K';
    return v.toFixed(0);
  }

  function formatAxisTime(tsMs, interval) {
    return formatBeijingTime(tsMs, { format: interval === '1d' ? 'md' : 'mdhm' });
  }

  function buildContextKey(ctx) {
    if (!ctx || typeof ctx !== 'object') return '';
    if (ctx.type === 'candidate' && ctx.candidate) {
      const c = ctx.candidate;
      return `cand:${c.symbol || ''}:${c.turnover_ratio || ''}:${c.funding_rate_4h_pct || ''}:${c.is_resonance ? 1 : 0}`;
    }
    if (ctx.type === 'position' && ctx.position) {
      const p = ctx.position;
      return `pos:${p.symbol || ''}:${p.entry_price || ''}:${p.trailing_stop_price || ''}:${p.trailing_stop_triggered ? 1 : 0}:${p.trendline_label || ''}`;
    }
    if (ctx.type === 'trade' && ctx.trade) {
      const t = ctx.trade;
      return `trade:${t.symbol || ''}:${t.entry_price || ''}:${t.exit_price || ''}:${t.net_pnl || ''}`;
    }
    return '';
  }

  function updateModalEmaHud(candle) {
    if (!modalHudEma5 && !modalHudEma10 && !modalHudEmaSpread) return;
    if (!candle) {
      if (modalHudEma5) modalHudEma5.textContent = 'EMA5 --';
      if (modalHudEma10) modalHudEma10.textContent = 'EMA10 --';
      if (modalHudEmaSpread) {
        modalHudEmaSpread.textContent = '--';
        modalHudEmaSpread.className = 'font-mono tabular-nums text-[#9fb0c5]';
      }
      return;
    }
    const diff = isFiniteNum(candle.ema5) && isFiniteNum(candle.ema10) ? candle.ema5 - candle.ema10 : undefined;
    if (modalHudEma5) {
      modalHudEma5.textContent = `EMA5 ${formatCryptoPrice(candle.ema5, { refDiff: diff })}`;
    }
    if (modalHudEma10) {
      modalHudEma10.textContent = `EMA10 ${formatCryptoPrice(candle.ema10, { refDiff: diff })}`;
    }
    if (modalHudEmaSpread) {
      if (isFiniteNum(candle.ema5) && isFiniteNum(candle.ema10) && candle.ema10 > 0) {
        const spreadPct = ((candle.ema5 - candle.ema10) / candle.ema10) * 100;
        modalHudEmaSpread.textContent = `均线差 ${formatSignedPct(spreadPct, 2)}`;
        modalHudEmaSpread.className = `font-mono tabular-nums ${spreadPct >= 0 ? 'text-[#38d99f]' : 'text-[#f8717e]'}`;
      } else {
        modalHudEmaSpread.textContent = '--';
        modalHudEmaSpread.className = 'font-mono tabular-nums text-[#9fb0c5]';
      }
    }
  }

  function renderModalContextHud() {
    if (!modalHudContext) return;
    const ctx = modalContext;
    if (!ctx || typeof ctx !== 'object') {
      modalHudContext.innerHTML = '';
      modalHudContext.classList.add('hidden');
      return;
    }
    const badges = [];
    if (ctx.type === 'candidate' && ctx.candidate) {
      const c = ctx.candidate;
      if (c.is_resonance && c.is_neg_funding) {
        badges.push('<span class="modal-hud-badge text-[#f8717e]">贴水双核共振</span>');
      } else if (c.is_resonance) {
        badges.push('<span class="modal-hud-badge text-[#f5c451]">顺势高换手共振</span>');
      } else if (c.is_ema_bull) {
        badges.push('<span class="modal-hud-badge text-[#38d99f]">1h EMA 顺势</span>');
      }
      if (isFiniteNum(c.turnover_ratio)) {
        badges.push(`<span class="modal-hud-badge font-mono tabular-nums text-[#8ab8ff]">换手 ${Number(c.turnover_ratio).toFixed(2)}x</span>`);
      }
      if (isFiniteNum(c.funding_rate_4h_pct)) {
        const frCls = Number(c.funding_rate_4h_pct) < 0 ? 'text-[#f8717e]' : 'text-[#9fb0c5]';
        badges.push(`<span class="modal-hud-badge font-mono tabular-nums ${frCls}">4h资费 ${escapeHtml(formatSignedPct(c.funding_rate_4h_pct, 4))}</span>`);
      }
      if (isFiniteNum(c.market_cap_usd)) {
        badges.push(`<span class="modal-hud-badge font-mono tabular-nums">市值 $${(Number(c.market_cap_usd) / 1e6).toFixed(1)}M</span>`);
      }
    } else if (ctx.type === 'position' && ctx.position) {
      const p = ctx.position;
      const side = String(p.side || '').toUpperCase();
      if (side) {
        const sCls = side === 'SHORT' ? 'text-[#f8717e]' : 'text-[#38d99f]';
        badges.push(`<span class="modal-hud-badge font-mono font-semibold ${sCls}">${escapeHtml(side)}</span>`);
      }
      if (isFiniteNum(p.entry_price) && Number(p.entry_price) > 0) {
        badges.push(`<span class="modal-hud-badge font-mono tabular-nums text-[#8ab8ff]">开仓 $${escapeHtml(formatCryptoPrice(p.entry_price))}</span>`);
      }
      if (p.trailing_stop_triggered && isFiniteNum(p.trailing_stop_price) && Number(p.trailing_stop_price) > 0) {
        badges.push(`<span class="modal-hud-badge font-mono tabular-nums text-[#38d99f]">保本线 $${escapeHtml(formatCryptoPrice(p.trailing_stop_price))}</span>`);
      }
      if (isFiniteNum(p.roe_pct)) {
        const rCls = Number(p.roe_pct) >= 0 ? 'text-[#38d99f]' : 'text-[#f8717e]';
        badges.push(`<span class="modal-hud-badge font-mono tabular-nums ${rCls}">ROE ${escapeHtml(formatSignedPct(p.roe_pct, 2))}</span>`);
      }
      if (typeof p.trendline_label === 'string' && p.trendline_label.trim() && p.trendline_label.trim() !== '--') {
        badges.push(`<span class="modal-hud-badge font-mono text-[#f5c451]">${escapeHtml(p.trendline_label.trim())}</span>`);
      }
    } else if (ctx.type === 'trade' && ctx.trade) {
      const t = ctx.trade;
      const side = String(t.side || '').toUpperCase();
      if (side) {
        const sCls = side === 'SHORT' ? 'text-[#f8717e]' : 'text-[#38d99f]';
        badges.push(`<span class="modal-hud-badge font-mono font-semibold ${sCls}">${escapeHtml(side)}</span>`);
      }
      if (isFiniteNum(t.entry_price) && Number(t.entry_price) > 0) {
        badges.push(`<span class="modal-hud-badge font-mono tabular-nums text-[#8ab8ff]">开 $${escapeHtml(formatCryptoPrice(t.entry_price))}</span>`);
      }
      if (isFiniteNum(t.exit_price) && Number(t.exit_price) > 0) {
        badges.push(`<span class="modal-hud-badge font-mono tabular-nums text-[#f5c451]">平 $${escapeHtml(formatCryptoPrice(t.exit_price))}</span>`);
      }
      if (isFiniteNum(t.pnl_pct)) {
        const pCls = Number(t.pnl_pct) >= 0 ? 'text-[#38d99f]' : 'text-[#f8717e]';
        badges.push(`<span class="modal-hud-badge font-mono tabular-nums ${pCls}">${escapeHtml(formatSignedPct(t.pnl_pct, 2))}</span>`);
      }
      if (typeof t.close_reason === 'string' && t.close_reason.trim()) {
        badges.push(`<span class="modal-hud-badge">${escapeHtml(t.close_reason.trim())}</span>`);
      }
    }

    if (badges.length === 0) {
      modalHudContext.innerHTML = '';
      modalHudContext.classList.add('hidden');
      return;
    }
    modalHudContext.innerHTML = badges.join('');
    modalHudContext.classList.remove('hidden');
  }

  function ensureTooltipNodes() {
    if (!modalTooltip) return null;
    if (tooltipRefs) return tooltipRefs;
    if (typeof global.document === 'undefined' || typeof global.document.createElement !== 'function') {
      return null;
    }
    const header = global.document.createElement('div');
    header.className = 'flex items-center gap-2 mb-1';
    const timeEl = global.document.createElement('span');
    timeEl.className = 'text-[#f1f5f9] font-bold';
    const chgEl = global.document.createElement('span');
    header.append(timeEl, chgEl);

    const ohlc = global.document.createElement('div');
    ohlc.className = 'grid grid-cols-2 sm:grid-cols-4 gap-x-3 gap-y-0.5 text-[#9fb0c5]';
    const makeCell = (label) => {
      const wrap = global.document.createElement('div');
      wrap.append(label + ' ');
      const val = global.document.createElement('span');
      val.className = 'text-[#f1f5f9]';
      wrap.append(val);
      ohlc.append(wrap);
      return val;
    };
    const oEl = makeCell('开');
    const hEl = makeCell('高');
    const lEl = makeCell('低');
    const cEl = makeCell('收');

    const footer = global.document.createElement('div');
    footer.className = 'flex items-center gap-3 mt-1 pt-1 border-t border-[#222b38] text-[10px]';
    const ema5El = global.document.createElement('span');
    ema5El.className = 'text-[#f5c451]';
    const ema10El = global.document.createElement('span');
    ema10El.className = 'text-[#8ab8ff]';
    const volEl = global.document.createElement('span');
    volEl.className = 'text-[#9fb0c5]';
    footer.append(ema5El, ema10El, volEl);

    modalTooltip.replaceChildren(header, ohlc, footer);
    tooltipRefs = { timeEl, chgEl, oEl, hEl, lEl, cEl, ema5El, ema10El, volEl };
    return tooltipRefs;
  }

  function extractTrendlineOverlay(ctxData) {
    if (!ctxData || ctxData.type !== 'position' || !ctxData.position) return null;
    const tl = ctxData.position.trendline;
    if (!tl || typeof tl !== 'object' || !Array.isArray(tl.points) || tl.points.length < 2) return null;
    const pA = tl.points[0];
    const pB = tl.points[1];
    const readPt = (pt) => {
      if (Array.isArray(pt) && pt.length >= 2) {
        return { t: Number(pt[0]), p: Number(pt[1]) };
      }
      if (pt && typeof pt === 'object') {
        return { t: Number(pt.time ?? pt.timestamp ?? pt.t), p: Number(pt.price ?? pt.p) };
      }
      return null;
    };
    const a = readPt(pA);
    const b = readPt(pB);
    if (!a || !b || !Number.isFinite(a.t) || !Number.isFinite(a.p) || !Number.isFinite(b.t) || !Number.isFinite(b.p)) {
      return null;
    }
    if (a.p <= 0 || b.p <= 0 || a.t === b.t) return null;
    const normT = (t) => (t < 1e11 ? t * 1000 : t);
    const t1 = normT(a.t);
    const t2 = normT(b.t);
    return t1 <= t2 ? { t1, p1: a.p, t2, p2: b.p } : { t1: t2, p1: b.p, t2: t1, p2: a.p };
  }

  function renderModalBaseChart(ctx, visibleCandles, w, h) {
    const topPadding = 16;
    const bottomAxisH = 22;
    const plotH = Math.max(96, h - bottomAxisH);
    const priceH = plotH * 0.78;
    const priceSpanH = Math.max(40, priceH - topPadding);
    const volH = plotH * 0.17;
    const rightPadding = RIGHT_AXIS_WIDTH;
    const chartW = w - rightPadding;

    let minP = Infinity;
    let maxP = -Infinity;
    let maxHighIdx = 0;
    let minLowIdx = 0;
    let maxVol = 0;
    visibleCandles.forEach((c, idx) => {
      if (c.low < minP) {
        minP = c.low;
        minLowIdx = idx;
      }
      if (c.high > maxP) {
        maxP = c.high;
        maxHighIdx = idx;
      }
      if (c.volume > maxVol) maxVol = c.volume;
    });
    const rawSpan = maxP - minP;
    const span = rawSpan > 0 ? rawSpan : Math.max(Math.abs(maxP) * 0.02, 1e-8);
    minP -= span * 0.06;
    maxP += span * 0.06;
    const fullSpan = maxP - minP;

    const getY = (p) => topPadding + priceSpanH - ((p - minP) / fullSpan) * priceSpanH;
    const getVolY = (v) => plotH - 3 - (v / (maxVol || 1)) * volH;
    const lastCandle = visibleCandles[visibleCandles.length - 1];
    const lastCloseY = lastCandle ? getY(lastCandle.close) : null;

    ctx.fillStyle = '#0b0e13';
    ctx.fillRect(0, 0, w, h);

    ctx.strokeStyle = '#1c2532';
    ctx.lineWidth = 1;
    ctx.fillStyle = '#7f91a8';
    ctx.font = '10px "JetBrains Mono", "SF Mono", monospace';
    ctx.textAlign = 'left';

    const steps = 6;
    ctx.beginPath();
    for (let i = 0; i <= steps; i++) {
      const p = minP + (fullSpan / steps) * i;
      const y = getY(p);
      ctx.moveTo(0, y);
      ctx.lineTo(chartW, y);
      if (lastCloseY === null || Math.abs(y - lastCloseY) >= 13) {
        ctx.fillText(formatCryptoPrice(p), chartW + 7, y + 3);
      }
    }

    const n = visibleCandles.length;
    const slot = chartW / n;
    const barW = Math.max(slot * 0.68, 2);

    ctx.moveTo(0, plotH);
    ctx.lineTo(chartW, plotH);

    const tickTarget = Math.max(2, Math.min(6, Math.floor(chartW / 120)));
    const tickStep = Math.max(1, Math.floor((n - 1) / tickTarget));
    ctx.textAlign = 'center';
    for (let i = 0; i < n; i += tickStep) {
      const x = i * slot + slot / 2;
      if (x < 36 || x > chartW - 36) continue;
      ctx.moveTo(x, plotH);
      ctx.lineTo(x, plotH + 4);
      ctx.fillText(formatAxisTime(visibleCandles[i].time, modalCurrentInterval), x, plotH + 15);
    }
    ctx.stroke();
    ctx.textAlign = 'left';

    const halfBarW = barW / 2;
    const drawCandlePolarityBatch = (isBullBatch) => {
      const col = isBullBatch ? '#22c58b' : '#f25565';
      const volCol = isBullBatch ? 'rgba(34, 197, 139, 0.32)' : 'rgba(242, 85, 101, 0.32)';
      ctx.strokeStyle = col;
      ctx.beginPath();
      for (let i = 0; i < n; i++) {
        const c = visibleCandles[i];
        if ((c.close >= c.open) !== isBullBatch) continue;
        const x = i * slot + slot / 2;
        ctx.moveTo(x, getY(c.high));
        ctx.lineTo(x, getY(c.low));
      }
      ctx.stroke();

      ctx.fillStyle = col;
      for (let i = 0; i < n; i++) {
        const c = visibleCandles[i];
        if ((c.close >= c.open) !== isBullBatch) continue;
        const x = i * slot + slot / 2;
        const yO = getY(c.open);
        const yC = getY(c.close);
        ctx.fillRect(x - halfBarW, Math.min(yO, yC), barW, Math.max(Math.abs(yC - yO), 1));
      }

      ctx.fillStyle = volCol;
      for (let i = 0; i < n; i++) {
        const c = visibleCandles[i];
        if ((c.close >= c.open) !== isBullBatch) continue;
        const x = i * slot + slot / 2;
        const vY = getVolY(c.volume);
        ctx.fillRect(x - halfBarW, vY, barW, plotH - 3 - vY);
      }
    };

    drawCandlePolarityBatch(true);
    drawCandlePolarityBatch(false);

    const drawEmaLine = (field, color) => {
      ctx.beginPath();
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.5;
      let started = false;
      visibleCandles.forEach((c, i) => {
        if (!isFiniteNum(c[field])) return;
        const x = i * slot + slot / 2;
        const y = getY(c[field]);
        if (!started) {
          ctx.moveTo(x, y);
          started = true;
        } else {
          ctx.lineTo(x, y);
        }
      });
      if (started) ctx.stroke();
    };

    drawEmaLine('ema5', '#f0b429');
    drawEmaLine('ema10', '#60a5fa');

    if (n >= 2 && rawSpan > 0) {
      ctx.font = '10px "JetBrains Mono", "SF Mono", monospace';
      ctx.fillStyle = '#9fb0c5';
      const hiC = visibleCandles[maxHighIdx];
      const hiX = Math.max(28, Math.min(chartW - 36, maxHighIdx * slot + slot / 2));
      const hiY = Math.max(11, getY(hiC.high) - 4);
      ctx.textAlign = 'center';
      ctx.fillText(formatCryptoPrice(hiC.high), hiX, hiY);

      const loC = visibleCandles[minLowIdx];
      const loX = Math.max(28, Math.min(chartW - 36, minLowIdx * slot + slot / 2));
      const loY = Math.min(priceH - 3, getY(loC.low) + 11);
      ctx.fillText(formatCryptoPrice(loC.low), loX, loY);
      ctx.textAlign = 'left';
    }

    if (modalContext && (modalContext.type === 'position' || modalContext.type === 'trade')) {
      const refSrc = modalContext.type === 'position' ? modalContext.position : modalContext.trade;
      const drawRefLine = (priceVal, label, strokeCol) => {
        if (!isFiniteNum(priceVal) || Number(priceVal) <= 0) return;
        const num = Number(priceVal);
        if (num < minP || num > maxP) return;
        const ry = getY(num);
        ctx.save();
        ctx.setLineDash([4, 3]);
        ctx.strokeStyle = strokeCol;
        ctx.lineWidth = 1.1;
        ctx.beginPath();
        ctx.moveTo(0, ry);
        ctx.lineTo(chartW, ry);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = strokeCol;
        ctx.font = 'bold 10px "JetBrains Mono", "SF Mono", monospace';
        ctx.fillText(`${label} ${formatCryptoPrice(num)}`, 8, Math.max(11, ry - 4));
        ctx.restore();
      };
      if (refSrc) {
        drawRefLine(refSrc.entry_price, '开仓', '#8ab8ff');
        if (modalContext.type === 'position' && refSrc.trailing_stop_triggered) {
          drawRefLine(refSrc.trailing_stop_price, '保本', '#38d99f');
        }
        if (modalContext.type === 'trade') {
          drawRefLine(refSrc.exit_price, '平仓', '#f5c451');
        }
      }
      if (modalCurrentInterval === '15m') {
        const tl = extractTrendlineOverlay(modalContext);
        if (tl) {
          const lnP1 = Math.log(tl.p1);
          const lnP2 = Math.log(tl.p2);
          const dt = tl.t2 - tl.t1;
          ctx.save();
          ctx.setLineDash([5, 3]);
          ctx.strokeStyle = '#f0b429';
          ctx.lineWidth = 1.4;
          ctx.beginPath();
          let started = false;
          for (let i = 0; i < n; i++) {
            const ct = visibleCandles[i].time;
            const proj = Math.exp(lnP1 + ((ct - tl.t1) / dt) * (lnP2 - lnP1));
            if (proj < minP * 0.85 || proj > maxP * 1.15) continue;
            const x = i * slot + slot / 2;
            const y = getY(proj);
            if (!started) {
              ctx.moveTo(x, y);
              started = true;
            } else {
              ctx.lineTo(x, y);
            }
          }
          if (started) ctx.stroke();
          ctx.restore();
        }
      }
    }

    if (lastCandle) {
      const lastY = getY(lastCandle.close);
      const isBull = lastCandle.close >= lastCandle.open;
      ctx.beginPath();
      ctx.setLineDash([3, 3]);
      ctx.strokeStyle = isBull ? '#22c58b' : '#f25565';
      ctx.moveTo(0, lastY);
      ctx.lineTo(chartW, lastY);
      ctx.stroke();
      ctx.setLineDash([]);

      ctx.fillStyle = isBull ? '#22c58b' : '#f25565';
      ctx.fillRect(chartW + 2, lastY - 9, rightPadding - 4, 18);
      ctx.fillStyle = '#0b0e13';
      ctx.font = 'bold 10px "JetBrains Mono", "SF Mono", monospace';
      ctx.fillText(formatCryptoPrice(lastCandle.close), chartW + 6, lastY + 4);
    }

    return { n, slot, plotH, chartW, rightPadding, getY };
  }

  function renderModalChart() {
    if (!modalCanvas || !modalNativeView || !modalCandles.length) return;
    const ctx = modalCanvas.getContext('2d');
    if (!ctx) return;

    const visibleCandles = modalCandles.slice(-modalVisibleBars);
    if (!visibleCandles.length) return;

    const dpr = global.devicePixelRatio || 1;
    const w = modalNativeView.clientWidth;
    const h = modalNativeView.clientHeight;
    if (w <= 0 || h <= 0) return;

    const targetW = Math.round(w * dpr);
    const targetH = Math.round(h * dpr);
    if (modalCanvas.width !== targetW || modalCanvas.height !== targetH) {
      modalCanvas.width = targetW;
      modalCanvas.height = targetH;
    }

    const lastCandle = visibleCandles[visibleCandles.length - 1];
    const nextBaseKey = `${modalCurrentSymbol}:${modalCurrentInterval}:${modalVisibleBars}:${visibleCandles.length}:${lastCandle ? lastCandle.time : 0}:${targetW}x${targetH}:${dpr}:${modalContextKey}`;

    if (typeof global.document !== 'undefined' && typeof global.document.createElement === 'function') {
      if (!baseChartCanvas) {
        baseChartCanvas = global.document.createElement('canvas');
      }
      if (baseChartCanvas.width !== targetW || baseChartCanvas.height !== targetH) {
        baseChartCanvas.width = targetW;
        baseChartCanvas.height = targetH;
        baseChartKey = '';
      }
      if (baseChartKey !== nextBaseKey || !modalChartMetrics) {
        const baseCtx = baseChartCanvas.getContext('2d');
        if (baseCtx) {
          baseCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
          modalChartMetrics = renderModalBaseChart(baseCtx, visibleCandles, w, h);
          baseChartKey = nextBaseKey;
        }
      }
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.drawImage(baseChartCanvas, 0, 0);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    } else {
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      modalChartMetrics = renderModalBaseChart(ctx, visibleCandles, w, h);
    }

    const { n, slot, plotH, chartW, rightPadding, getY } = modalChartMetrics;
    const activeCandle = modalHoverIndex >= 0 && modalHoverIndex < n ? visibleCandles[modalHoverIndex] : lastCandle;
    updateModalEmaHud(activeCandle);

    if (modalHoverIndex >= 0 && modalHoverIndex < n && modalTooltip) {
      const c = visibleCandles[modalHoverIndex];
      const hx = modalHoverIndex * slot + slot / 2;
      const hy = getY(c.close);

      ctx.strokeStyle = '#7f91a8';
      ctx.setLineDash([2, 2]);
      ctx.beginPath();
      ctx.moveTo(hx, 0);
      ctx.lineTo(hx, plotH);
      ctx.moveTo(0, hy);
      ctx.lineTo(chartW, hy);
      ctx.stroke();
      ctx.setLineDash([]);

      const hoverPriceText = formatCryptoPrice(c.close);
      ctx.fillStyle = '#1d2736';
      ctx.fillRect(chartW + 2, hy - 9, rightPadding - 4, 18);
      ctx.strokeStyle = '#4b5c75';
      ctx.lineWidth = 1;
      ctx.strokeRect(chartW + 2.5, hy - 8.5, rightPadding - 5, 17);
      ctx.fillStyle = '#f1f5f9';
      ctx.font = 'bold 10px "JetBrains Mono", "SF Mono", monospace';
      ctx.textAlign = 'left';
      ctx.fillText(hoverPriceText, chartW + 6, hy + 4);

      const axisTimeText = formatAxisTime(c.time, modalCurrentInterval);
      ctx.font = 'bold 10px "JetBrains Mono", "SF Mono", monospace';
      const timeBoxW = Math.max(54, Math.ceil(ctx.measureText(axisTimeText).width) + 12);
      const timeBoxX = Math.max(2, Math.min(chartW - timeBoxW - 2, hx - timeBoxW / 2));
      ctx.fillStyle = '#1d2736';
      ctx.fillRect(timeBoxX, plotH + 2, timeBoxW, 18);
      ctx.strokeStyle = '#4b5c75';
      ctx.strokeRect(timeBoxX + 0.5, plotH + 2.5, timeBoxW - 1, 17);
      ctx.fillStyle = '#f1f5f9';
      ctx.textAlign = 'center';
      ctx.fillText(axisTimeText, timeBoxX + timeBoxW / 2, plotH + 14);
      ctx.textAlign = 'left';

      const timeStr = formatBeijingTime(c.time, { format: 'mdhm' });
      const chg = c.open > 0 ? (((c.close - c.open) / c.open) * 100).toFixed(2) : null;
      const isBull = c.close >= c.open;
      const chgLabel = chg !== null ? `${Number(chg) > 0 ? '+' : ''}${chg}%` : '--';

      if (modalHoverIndex < n * 0.45) {
        modalTooltip.style.left = 'auto';
        modalTooltip.style.right = `${RIGHT_AXIS_WIDTH + 10}px`;
      } else {
        modalTooltip.style.left = '12px';
        modalTooltip.style.right = 'auto';
      }

      const refs = ensureTooltipNodes();
      if (refs) {
        refs.timeEl.textContent = timeStr;
        refs.chgEl.textContent = chgLabel;
        refs.chgEl.className = `${isBull ? 'text-[#38d99f]' : 'text-[#f8717e]'} font-bold`;
        refs.oEl.textContent = formatCryptoPrice(c.open);
        refs.hEl.textContent = formatCryptoPrice(c.high);
        refs.lEl.textContent = formatCryptoPrice(c.low);
        refs.cEl.textContent = formatCryptoPrice(c.close);
        refs.ema5El.textContent = `EMA5 ${formatCryptoPrice(c.ema5)}`;
        refs.ema10El.textContent = `EMA10 ${formatCryptoPrice(c.ema10)}`;
        const volText = formatCompactVolume(c.volume);
        const volUnit = modalCurrentSymbol && modalCurrentSymbol.endsWith('USDT') && modalCurrentSymbol.length > 4
          ? modalCurrentSymbol.slice(0, -4)
          : '币';
        refs.volEl.textContent = volText === '--' ? '成交量 --' : `成交量 ${volText} ${volUnit}`;
      }
      modalTooltip.classList.remove('hidden');
    } else if (modalTooltip) {
      modalTooltip.classList.add('hidden');
    }
  }

  function clearModalChartDisplay() {
    modalCandles = [];
    modalHoverIndex = -1;
    baseChartKey = '';
    modalChartMetrics = null;
    updateModalEmaHud(null);
    if (modalTooltip) modalTooltip.classList.add('hidden');
    if (modalChartPrice) {
      modalChartPrice.textContent = '--';
      modalChartPrice.className = 'font-mono tabular-nums text-[#38d99f] font-bold text-xs sm:text-[13px]';
    }
    if (modalCanvas && typeof modalCanvas.getContext === 'function') {
      const ctx = modalCanvas.getContext('2d');
      if (ctx) {
        ctx.save();
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.clearRect(0, 0, modalCanvas.width, modalCanvas.height);
        ctx.restore();
      }
    }
  }

  function updateCanvasAriaLabel(visibleCount) {
    if (!modalCanvas || typeof modalCanvas.setAttribute !== 'function') return;
    if (!modalCurrentSymbol) {
      modalCanvas.setAttribute('aria-label', 'K线走势图');
      return;
    }
    modalCanvas.setAttribute(
      'aria-label',
      `${modalCurrentSymbol} ${modalCurrentInterval} K线与 EMA5/EMA10 走势图（显示 ${visibleCount} 根）`
    );
  }

  function showModalChartError(message) {
    if (modalLoading) modalLoading.classList.add('hidden');
    if (!modalError) return;
    if (modalErrorText) {
      modalErrorText.textContent = message || 'K 线序列读取失败，请检查网络连接后重试';
    }
    modalError.classList.remove('hidden');
  }

  function hideModalChartError() {
    if (modalError) modalError.classList.add('hidden');
  }

  function clearTvLoadTimer() {
    if (tvLoadTimer !== null) {
      clearTimeout(tvLoadTimer);
      tvLoadTimer = null;
    }
  }

  function hideTvOverlays() {
    tvLoadSeq += 1;
    clearTvLoadTimer();
    if (modalTvLoading) modalTvLoading.classList.add('hidden');
    if (modalTvFallback) modalTvFallback.classList.add('hidden');
  }

  function showTvFallback(message) {
    clearTvLoadTimer();
    if (modalTvLoading) modalTvLoading.classList.add('hidden');
    if (modalTvFallbackText && message) {
      modalTvFallbackText.textContent = message;
    }
    if (modalTvFallback) modalTvFallback.classList.remove('hidden');
  }

  function loadTvIframe() {
    if (!modalTvIframe || !modalCurrentTvSymbol) return;
    const tvInterval = TV_INTERVAL_MAP[modalCurrentInterval];
    if (!tvInterval) {
      showTvFallback(`不支持的 K 线周期: ${modalCurrentInterval}`);
      return;
    }
    if (modalFetchController) {
      modalFetchController.abort();
      modalFetchController = null;
    }
    if (modalTvOpenExtBtn) {
      modalTvOpenExtBtn.href = getTvUrl(modalCurrentTvSymbol, null, modalCurrentInterval);
    }
    modalTvIframe.setAttribute('referrerpolicy', 'no-referrer');
    modalTvIframe.referrerPolicy = 'no-referrer';
    modalTvIframe.removeAttribute('allowtransparency');
    modalTvIframe.removeAttribute('scrolling');

    clearTvLoadTimer();
    const seq = ++tvLoadSeq;
    if (modalTvFallback) modalTvFallback.classList.add('hidden');
    if (modalTvLoading) modalTvLoading.classList.remove('hidden');
    tvLoadTimer = setTimeout(() => {
      tvLoadTimer = null;
      if (seq !== tvLoadSeq || modalActiveEngine !== 'tv') return;
      showTvFallback('TradingView 内嵌图表加载超时或受网络限制');
    }, TV_IFRAME_TIMEOUT_MS);
    if (tvLoadTimer && typeof tvLoadTimer.unref === 'function') tvLoadTimer.unref();

    modalTvIframe.src = `https://s.tradingview.com/widgetembed/?symbol=${encodeURIComponent(modalCurrentTvSymbol)}&interval=${tvInterval}&theme=dark&style=1&timezone=Asia%2FShanghai&locale=zh_CN`;
  }

  async function fetchModalKlines(sym, interval) {
    if (!modalNativeView || !modalLoading) return;
    if (!interval || !Object.hasOwn(TV_INTERVAL_MAP, interval)) {
      showModalChartError(`不支持的时间周期: ${interval || '未知'}`);
      return;
    }
    if (modalFetchController) modalFetchController.abort();
    const controller = new AbortController();
    modalFetchController = controller;
    const signal = controller.signal;

    clearModalChartDisplay();
    modalLoading.classList.remove('hidden');
    hideModalChartError();

    try {
      const url = `api/binance/klines?symbol=${encodeURIComponent(sym)}&interval=${encodeURIComponent(interval)}&limit=168`;
      const { data: raw } = await fetchJsonWithTimeout(url, { signal }, 12000);
      if (signal.aborted || modalActiveEngine !== 'native') return;
      if (!Array.isArray(raw) || raw.length === 0) throw new Error('No kline data');

      modalCandles = raw.map((d, idx) => {
        if (!Array.isArray(d) || d.length < 6) {
          throw new Error(`Invalid kline bar at index ${idx}`);
        }
        const bar = {
          time: Number(d[0]),
          open: parseFloat(d[1]),
          high: parseFloat(d[2]),
          low: parseFloat(d[3]),
          close: parseFloat(d[4]),
          volume: parseFloat(d[5]),
        };
        if (
          !Number.isFinite(bar.time) ||
          !Number.isFinite(bar.open) ||
          !Number.isFinite(bar.high) ||
          !Number.isFinite(bar.low) ||
          !Number.isFinite(bar.close) ||
          !Number.isFinite(bar.volume)
        ) {
          throw new Error(`Non-finite kline values at index ${idx}`);
        }
        return bar;
      });

      const ema5Vals = calcEMAArray(modalCandles, 5);
      const ema10Vals = calcEMAArray(modalCandles, 10);
      modalCandles.forEach((c, idx) => {
        c.ema5 = ema5Vals[idx];
        c.ema10 = ema10Vals[idx];
      });

      const last = modalCandles[modalCandles.length - 1];
      if (last && modalChartPrice) {
        const isUp = last.close >= last.open;
        modalChartPrice.textContent = formatCryptoPrice(last.close);
        modalChartPrice.className = `font-mono tabular-nums font-bold text-xs sm:text-[13px] ${isUp ? 'text-[#38d99f]' : 'text-[#f8717e]'}`;
      }
      updateCanvasAriaLabel(Math.min(modalVisibleBars, modalCandles.length));
      renderModalChart();
    } catch (err) {
      if (err && err.name === 'AbortError') return;
      if (modalActiveEngine !== 'native') return;
      console.error('Failed to load klines:', err);
      const errMsg = err && err.message ? err.message : '未知网络错误';
      showModalChartError(`K 线序列读取失败: ${errMsg}`);
    } finally {
      if (modalFetchController === controller) {
        modalFetchController = null;
      }
      if (modalLoading && !signal.aborted) modalLoading.classList.add('hidden');
    }
  }

  function openTvModal(sym, tvSym, triggerEl, context) {
    ensureModalInitialized();
    if (!chartModal) return;
    if (!chartModal.open) {
      const activeEl = typeof document !== 'undefined' ? document.activeElement : null;
      const bodyEl = typeof document !== 'undefined' ? document.body : null;
      lastFocusedElement = (activeEl && activeEl !== bodyEl) ? activeEl : (triggerEl || activeEl);
    }
    if (modalFetchController) {
      modalFetchController.abort();
      modalFetchController = null;
    }
    chartModal.removeAttribute('data-closing');
    modalCurrentSymbol = sym;
    modalCurrentTvSymbol = tvSym;
    modalContext = context || null;
    modalContextKey = buildContextKey(modalContext);
    renderModalContextHud();
    clearModalChartDisplay();
    hideModalChartError();
    hideTvOverlays();
    updateCanvasAriaLabel(0);

    if (modalChartTitle) modalChartTitle.textContent = sym;
    if (modalExternalLink) modalExternalLink.href = getTvUrl(tvSym, null, modalCurrentInterval);
    if (!chartModal.open) {
      chartModal.showModal();
    }

    if (modalActiveEngine === 'native') {
      if (modalNativeView) modalNativeView.classList.remove('hidden');
      if (modalTvView) modalTvView.classList.add('hidden');
      fetchModalKlines(modalCurrentSymbol, modalCurrentInterval);
    } else {
      if (modalLoading) modalLoading.classList.add('hidden');
      if (modalNativeView) modalNativeView.classList.add('hidden');
      if (modalTvView) modalTvView.classList.remove('hidden');
      loadTvIframe();
    }
  }

  function restoreFocusAfterClose() {
    const target = lastFocusedElement;
    lastFocusedElement = null;
    focusSafely(target);
  }

  function resetModalStateOnClose() {
    if (chartModal) chartModal.removeAttribute('data-closing');
    if (modalRenderRafId !== null) {
      if (typeof cancelAnimationFrame === 'function') {
        cancelAnimationFrame(modalRenderRafId);
      }
      modalRenderRafId = null;
    }
    if (modalFetchController) {
      modalFetchController.abort();
      modalFetchController = null;
    }
    modalContext = null;
    modalContextKey = '';
    renderModalContextHud();
    hideTvOverlays();
    hideModalChartError();
    if (modalTvIframe) modalTvIframe.src = 'about:blank';
    clearModalChartDisplay();
    restoreFocusAfterClose();
  }

  function closeTvModal() {
    if (!chartModal || !chartModal.open) return;
    closeTopLayer(chartModal);
  }

  function scheduleModalRender() {
    if (modalRenderRafId !== null) return;
    let ranSync = false;
    modalRenderRafId = requestAnimationFrame(() => {
      ranSync = true;
      modalRenderRafId = null;
      renderModalChart();
    });
    if (ranSync) {
      modalRenderRafId = null;
    }
  }

  function updateModalIntervalUi() {
    modalIntervalBtns.forEach((b) => {
      const active = b.getAttribute('data-interval') === modalCurrentInterval;
      b.setAttribute('aria-pressed', active ? 'true' : 'false');
      b.classList.toggle('bg-[#192231]', active);
      b.classList.toggle('text-[#f1f5f9]', active);
      b.classList.toggle('font-semibold', active);
      b.classList.toggle('hover:bg-[#151c27]', !active);
      b.classList.toggle('text-[#9fb0c5]', !active);
    });
  }

  function updateModalBarsUi() {
    modalBarsBtns.forEach((b) => {
      const active = Number(b.getAttribute('data-bars')) === modalVisibleBars;
      b.setAttribute('aria-pressed', active ? 'true' : 'false');
      b.classList.toggle('bg-[#192231]', active);
      b.classList.toggle('text-[#f1f5f9]', active);
      b.classList.toggle('font-semibold', active);
      b.classList.toggle('hover:bg-[#151c27]', !active);
      b.classList.toggle('text-[#9fb0c5]', !active);
    });
  }

  function updateModalEngineUi() {
    const isNative = modalActiveEngine === 'native';
    if (btnEngineNative) {
      btnEngineNative.setAttribute('aria-pressed', isNative ? 'true' : 'false');
      btnEngineNative.className = isNative
        ? 'px-1.5 py-0.5 bg-[#192231] text-[#f1f5f9] font-semibold'
        : 'px-1.5 py-0.5 hover:bg-[#151c27] text-[#9fb0c5]';
    }
    if (btnEngineTv) {
      btnEngineTv.setAttribute('aria-pressed', !isNative ? 'true' : 'false');
      btnEngineTv.className = !isNative
        ? 'px-1.5 py-0.5 bg-[#192231] text-[#f1f5f9] font-semibold'
        : 'px-1.5 py-0.5 hover:bg-[#151c27] text-[#9fb0c5]';
    }
  }

  function selectInterval(nextInterval) {
    if (!nextInterval || !Object.hasOwn(TV_INTERVAL_MAP, nextInterval)) {
      showModalChartError(`不支持的时间周期: ${nextInterval || '未知'}`);
      return;
    }
    if (nextInterval === modalCurrentInterval) return;
    modalCurrentInterval = nextInterval;
    updateModalIntervalUi();
    if (modalExternalLink && modalCurrentTvSymbol) {
      modalExternalLink.href = getTvUrl(modalCurrentTvSymbol, null, modalCurrentInterval);
    }
    updateCanvasAriaLabel(Math.min(modalVisibleBars, modalCandles.length));
    if (!modalCurrentSymbol) return;
    if (modalActiveEngine === 'native') {
      fetchModalKlines(modalCurrentSymbol, modalCurrentInterval);
    } else {
      loadTvIframe();
    }
  }

  function switchChartEngine(targetEngine) {
    if (targetEngine !== 'native' && targetEngine !== 'tv') return;
    if (modalActiveEngine === targetEngine) return;
    modalActiveEngine = targetEngine;
    updateModalEngineUi();
    if (targetEngine === 'native') {
      hideTvOverlays();
      if (modalBarsGroup) modalBarsGroup.hidden = false;
      if (modalTvView) modalTvView.classList.add('hidden');
      if (modalNativeView) modalNativeView.classList.remove('hidden');
      if (modalCurrentSymbol) {
        fetchModalKlines(modalCurrentSymbol, modalCurrentInterval);
      }
    } else {
      if (modalFetchController) {
        modalFetchController.abort();
        modalFetchController = null;
      }
      if (modalLoading) modalLoading.classList.add('hidden');
      hideModalChartError();
      if (modalBarsGroup) modalBarsGroup.hidden = true;
      if (modalNativeView) modalNativeView.classList.add('hidden');
      if (modalTvView) modalTvView.classList.remove('hidden');
      if (modalCurrentTvSymbol) {
        loadTvIframe();
      }
    }
  }

  function bindModalDomEvents() {
    if (modalDomBound) return;
    modalDomBound = true;

    if (modalNativeView) {
      const updateModalHoverAtClientX = (clientX) => {
        const rect = modalNativeView.getBoundingClientRect();
        const x = clientX - rect.left;
        const chartW = modalNativeView.clientWidth - RIGHT_AXIS_WIDTH;
        const visibleCount = Math.min(modalVisibleBars, modalCandles.length);
        if (x >= 0 && x <= chartW && visibleCount > 0) {
          const slot = chartW / visibleCount;
          const nextIdx = Math.min(Math.floor(x / slot), visibleCount - 1);
          if (nextIdx !== modalHoverIndex) {
            modalHoverIndex = nextIdx;
            scheduleModalRender();
          }
        } else if (modalHoverIndex !== -1) {
          modalHoverIndex = -1;
          scheduleModalRender();
        }
      };
      const clearModalHover = () => {
        if (modalTooltip) modalTooltip.classList.add('hidden');
        if (modalHoverIndex !== -1) {
          modalHoverIndex = -1;
          scheduleModalRender();
        }
      };

      modalNativeView.addEventListener('mousemove', (e) => {
        updateModalHoverAtClientX(e.clientX);
      });
      modalNativeView.addEventListener('mouseleave', clearModalHover);
      modalNativeView.addEventListener('touchstart', (e) => {
        if (!e.touches || !e.touches.length) return;
        updateModalHoverAtClientX(e.touches[0].clientX);
      }, { passive: true });
      modalNativeView.addEventListener('touchmove', (e) => {
        if (!e.touches || !e.touches.length) return;
        if (e.cancelable) e.preventDefault();
        updateModalHoverAtClientX(e.touches[0].clientX);
      }, { passive: false });
      modalNativeView.addEventListener('touchend', clearModalHover, { passive: true });
      modalNativeView.addEventListener('touchcancel', clearModalHover, { passive: true });
    }

    modalBarsBtns.forEach((btn) => {
      btn.addEventListener('click', () => {
        const bars = Number(btn.getAttribute('data-bars'));
        if (!VALID_BAR_COUNTS.has(bars) || bars === modalVisibleBars) return;
        modalVisibleBars = bars;
        modalHoverIndex = -1;
        updateModalBarsUi();
        if (typeof onVisibleBarsChange === 'function') {
          onVisibleBarsChange(bars);
        }
        updateCanvasAriaLabel(Math.min(modalVisibleBars, modalCandles.length));
        renderModalChart();
      });
    });

    modalIntervalBtns.forEach((btn) => {
      btn.addEventListener('click', () => {
        selectInterval(btn.getAttribute('data-interval'));
      });
    });

    if (btnEngineNative && btnEngineTv) {
      btnEngineNative.addEventListener('click', () => switchChartEngine('native'));
      btnEngineTv.addEventListener('click', () => switchChartEngine('tv'));
    }

    if (modalTvSwitchNativeBtn) {
      modalTvSwitchNativeBtn.addEventListener('click', () => switchChartEngine('native'));
    }

    if (modalCopyBtn) {
      modalCopyBtn.addEventListener('click', () => {
        if (!modalCurrentTvSymbol) return;
        copyText(modalCurrentTvSymbol, `已复制: ${modalCurrentTvSymbol}`);
      });
    }

    if (modalTvIframe) {
      modalTvIframe.addEventListener('load', () => {
        if (modalActiveEngine !== 'tv') return;
        if (!modalTvIframe.src || modalTvIframe.src === 'about:blank') return;
        hideTvOverlays();
      });
      modalTvIframe.addEventListener('error', () => {
        if (modalActiveEngine !== 'tv') return;
        showTvFallback('TradingView 内嵌图表加载失败，请切回极速 K 线或新窗口打开');
      });
    }

    if (modalPrevBtn) {
      modalPrevBtn.addEventListener('click', () => {
        if (typeof onNavigateSymbol === 'function') onNavigateSymbol(-1);
      });
    }
    if (modalNextBtn) {
      modalNextBtn.addEventListener('click', () => {
        if (typeof onNavigateSymbol === 'function') onNavigateSymbol(1);
      });
    }
    if (modalRetryBtn) {
      modalRetryBtn.addEventListener('click', () => {
        if (!modalCurrentSymbol) return;
        fetchModalKlines(modalCurrentSymbol, modalCurrentInterval);
      });
    }
    if (modalCloseBtn) modalCloseBtn.addEventListener('click', closeTvModal);
    if (chartModal) {
      chartModal.addEventListener('close', resetModalStateOnClose);
      chartModal.addEventListener('cancel', (event) => {
        event.preventDefault();
        closeTopLayer(chartModal);
      });
      chartModal.addEventListener('keydown', (event) => {
        if (!chartModal.open) return;
        const activeEl = typeof document !== 'undefined' ? document.activeElement : null;
        const tag = activeEl ? activeEl.tagName : '';
        if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || (activeEl && activeEl.isContentEditable)) return;

        const hasModifier = Boolean(event.ctrlKey || event.metaKey || event.altKey);
        if (!hasModifier) {
          if (Object.hasOwn(INTERVAL_HOTKEY_MAP, event.key)) {
            event.preventDefault();
            selectInterval(INTERVAL_HOTKEY_MAP[event.key]);
            return;
          }
          if (event.key === 't' || event.key === 'T') {
            event.preventDefault();
            switchChartEngine(modalActiveEngine === 'native' ? 'tv' : 'native');
            return;
          }
          if (event.key === 'c' || event.key === 'C') {
            if (modalCurrentTvSymbol) {
              event.preventDefault();
              copyText(modalCurrentTvSymbol, `已复制: ${modalCurrentTvSymbol}`);
            }
            return;
          }
        }

        if (typeof onNavigateSymbol !== 'function' || modalNavTotal <= 1) return;
        if (
          activeEl &&
          typeof activeEl.closest === 'function' &&
          (activeEl.closest('[role="group"]') || (modalHeaderToolbar && activeEl.closest('#modal-header-toolbar')))
        ) {
          return;
        }
        if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
          event.preventDefault();
          onNavigateSymbol(-1);
        } else if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
          event.preventDefault();
          onNavigateSymbol(1);
        }
      });
      if (typeof global.HTMLDialogElement === 'undefined' || !('closedBy' in global.HTMLDialogElement.prototype)) {
        chartModal.addEventListener('click', (event) => {
          if (event.target !== chartModal) return;
          const rect = chartModal.getBoundingClientRect();
          const isDialogContent = (
            rect.top <= event.clientY &&
            event.clientY <= rect.top + rect.height &&
            rect.left <= event.clientX &&
            event.clientX <= rect.left + rect.width
          );
          if (!isDialogContent) {
            closeTvModal();
          }
        });
      }
    }

    const handleNativeContainerResize = () => {
      if (chartModal && chartModal.open && modalActiveEngine === 'native') {
        scheduleModalRender();
      }
    };
    if (modalNativeView && typeof global.ResizeObserver === 'function') {
      modalResizeObserver = new global.ResizeObserver(() => {
        handleNativeContainerResize();
      });
      modalResizeObserver.observe(modalNativeView);
    } else if (typeof global.addEventListener === 'function') {
      global.addEventListener('resize', handleNativeContainerResize);
    }
  }

  function ensureModalInitialized() {
    if (!chartModal) {
      resolveModalDomRefs();
    }
    if (chartModal && !modalDomBound) {
      bindModalDomEvents();
    }
  }

  ensureModalInitialized();

  if (typeof global.addEventListener === 'function') {
    global.addEventListener('pagehide', () => {
      clearTvLoadTimer();
      if (modalRenderRafId !== null && typeof cancelAnimationFrame === 'function') {
        cancelAnimationFrame(modalRenderRafId);
        modalRenderRafId = null;
      }
      if (modalFetchController) {
        modalFetchController.abort();
        modalFetchController = null;
      }
    });
  }

  function setNavMeta(index, total) {
    ensureModalInitialized();
    modalNavTotal = Number.isInteger(total) ? total : 0;
    if (!modalNavGroup || !modalNavPos) return;
    if (Number.isInteger(index) && Number.isInteger(total) && total > 1 && index >= 0 && index < total) {
      modalNavPos.textContent = `${index + 1}/${total}`;
      modalNavGroup.hidden = false;
    } else {
      modalNavGroup.hidden = true;
    }
  }

  function init(opts) {
    ensureModalInitialized();
    const options = opts || {};
    if (VALID_BAR_COUNTS.has(Number(options.visibleBars))) {
      modalVisibleBars = Number(options.visibleBars);
    }
    onVisibleBarsChange = typeof options.onVisibleBarsChange === 'function' ? options.onVisibleBarsChange : null;
    onNavigateSymbol = typeof options.onNavigateSymbol === 'function' ? options.onNavigateSymbol : null;
    updateModalIntervalUi();
    updateModalBarsUi();
    updateModalEngineUi();
  }

  global.SecondLegKlineModal = Object.freeze({
    VALID_BAR_COUNTS,
    init,
    open: openTvModal,
    close: closeTvModal,
    setNavMeta,
    isOpen: () => Boolean(chartModal && chartModal.open),
    getVisibleBars: () => modalVisibleBars,
    getCurrentSymbol: () => modalCurrentSymbol,
  });
})(window);
