(function () {
  const Common = window.SecondLegCommon;
  const KlineModal = window.SecondLegKlineModal;
  const {
    TV_LAYOUT_ID,
    escapeHtml,
    isFiniteNum,
    formatCryptoPrice,
    formatSignedPct,
    getTvSymbol,
    getTvUrl,
    fetchJsonWithTimeout,
    toBeijingDate,
  } = Common;

  const ZONE_NAMES = {
    resonance: '顺势高换手共振区',
    neg_fr: '负费率贴水区',
    high_tr: '巨量高换手区',
    ema_bull: '1h EMA 顺势区',
    all: '全量候选底池',
  };
  const UI_STATE_KEY = 'slq_candidates_ui_state';
  const VALID_SORT_COLS = new Set(['price', 'chg', 'mc', 'vol', 'tr', 'fr', 'ema', 'duration']);
  const VALID_BAR_COUNTS = KlineModal ? KlineModal.VALID_BAR_COUNTS : new Set([60, 120, 168]);

  const EXT_ARROW_SVG = '<svg class="w-2.5 h-2.5 shrink-0" viewBox="0 0 12 12" fill="none" aria-hidden="true"><path d="M3.5 8.5L8.5 3.5M8.5 3.5H4.5M8.5 3.5V7.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  const CHECK_SVG = '<svg class="w-3 h-3" viewBox="0 0 12 12" fill="none" aria-hidden="true"><path d="M2.5 6.2L5 8.7L9.5 3.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';

  let candidatesState = [];
  const rowRefsBySym = new Map();
  const prevPriceBySym = new Map();
  const copyResetTimers = new WeakMap();

  let currentVersion = null;
  let lastEtag = null;
  let currentZone = 'resonance';
  let searchQuery = '';
  let sortColumn = null;
  let sortAsc = true;
  let modalVisibleBars = 120;

  let pollController = null;
  let snapshotController = null;
  let pollTimer = null;
  let versionTimer = null;
  let countdownTimer = null;
  let filterDebounceTimer = null;
  let activeFetchId = 0;
  let activeSnapshotFetchId = 0;
  let toastTimer = null;
  let prefersReducedMotion = false;
  if (typeof window.matchMedia === 'function') {
    const reducedMotionQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
    if (reducedMotionQuery) {
      prefersReducedMotion = Boolean(reducedMotionQuery.matches);
      if (typeof reducedMotionQuery.addEventListener === 'function') {
        reducedMotionQuery.addEventListener('change', (e) => {
          prefersReducedMotion = Boolean(e.matches);
        });
      } else if (typeof reducedMotionQuery.addListener === 'function') {
        reducedMotionQuery.addListener((e) => {
          prefersReducedMotion = Boolean(e.matches);
        });
      }
    }
  }

  function saveUiState() {
    try {
      sessionStorage.setItem(
        UI_STATE_KEY,
        JSON.stringify({
          currentZone,
          searchQuery,
          sortColumn,
          sortAsc,
          modalVisibleBars,
        })
      );
    } catch (err) {
      console.warn('Failed to save UI state to sessionStorage:', err);
    }
  }

  function restoreUiState() {
    let raw = null;
    try {
      raw = sessionStorage.getItem(UI_STATE_KEY);
    } catch (err) {
      console.warn('Failed to read UI state from sessionStorage:', err);
      return;
    }
    if (!raw) return;
    try {
      const saved = JSON.parse(raw);
      if (!saved || typeof saved !== 'object' || Array.isArray(saved)) {
        throw new Error('Unexpected UI state structure');
      }
      if (typeof saved.currentZone === 'string' && Object.hasOwn(ZONE_NAMES, saved.currentZone)) {
        currentZone = saved.currentZone;
      }
      if (typeof saved.searchQuery === 'string') {
        searchQuery = saved.searchQuery;
      }
      if (typeof saved.sortColumn === 'string' && VALID_SORT_COLS.has(saved.sortColumn)) {
        sortColumn = saved.sortColumn;
        sortAsc = Boolean(saved.sortAsc);
      }
      if (VALID_BAR_COUNTS.has(Number(saved.modalVisibleBars))) {
        modalVisibleBars = Number(saved.modalVisibleBars);
      }
    } catch (err) {
      console.warn('Corrupt UI state in sessionStorage; clearing:', err);
      try {
        sessionStorage.removeItem(UI_STATE_KEY);
      } catch (removeErr) {
        console.warn('Failed to clear corrupt UI state from sessionStorage:', removeErr);
      }
    }
  }

  const syncBtn = document.getElementById('btn-manual-sync');
  const syncStatusBadge = document.getElementById('sync-status-badge');
  const poolUpdatedEl = document.getElementById('pool-updated-time');
  const countBadge = document.getElementById('pool-count-badge');
  const searchInput = document.getElementById('symbol-search-input');
  const resetBtn = document.getElementById('btn-reset-filter');
  const kpiCards = document.querySelectorAll('.kpi-card');
  const candidatesTable = document.getElementById('candidates-table');
  const tableBody = document.getElementById('candidate-table-body');
  const sortHeaders = document.querySelectorAll('th.sortable');
  const sortStatusEl = document.getElementById('candidates-sort-status');
  const batchCopyBtn = document.getElementById('btn-batch-copy-tv');
  const batchExportBtn = document.getElementById('btn-batch-export-tv');
  const toast = document.getElementById('toast');
  const toastText = document.getElementById('toast-text');
  const tvDrawer = document.getElementById('tv-drawer');
  const toggleDrawerBtn = document.getElementById('btn-toggle-drawer');
  const closeDrawerBtn = document.getElementById('btn-close-drawer');

  const kpiEls = {
    resCount: document.getElementById('kpi-resonance-count'),
    resSub: document.getElementById('kpi-resonance-sub'),
    resSample: document.getElementById('kpi-resonance-sample'),
    resPct: document.getElementById('kpi-resonance-pct'),
    negCount: document.getElementById('kpi-neg-fr-count'),
    negSample: document.getElementById('kpi-neg-fr-sample'),
    negPct: document.getElementById('kpi-neg-fr-pct'),
    highTrCount: document.getElementById('kpi-high-tr-count'),
    highTrSample: document.getElementById('kpi-high-tr-sample'),
    highTrPct: document.getElementById('kpi-high-tr-pct'),
    emaBullCount: document.getElementById('kpi-ema-bull-count'),
    emaBullSample: document.getElementById('kpi-ema-bull-sample'),
    emaBullPct: document.getElementById('kpi-ema-bull-pct'),
    allCount: document.getElementById('kpi-all-count'),
    allSub: document.getElementById('kpi-all-sub'),
  };

  function formatMillions(val) {
    if (!isFiniteNum(val)) return '--';
    return '$' + (val / 1e6).toFixed(1) + 'M';
  }

  function formatUpdatedAtBeijing(updatedAt) {
    if (!updatedAt || typeof updatedAt !== 'string') return '--';
    const trimmed = updatedAt.trim();
    if (!trimmed) return '--';
    const parsed = new Date(trimmed);
    if (Number.isNaN(parsed.getTime())) return trimmed;
    const bj = toBeijingDate(parsed.getTime());
    const pad = (n) => String(n).padStart(2, '0');
    return (
      `${bj.getUTCFullYear()}-${pad(bj.getUTCMonth() + 1)}-${pad(bj.getUTCDate())} ` +
      `${pad(bj.getUTCHours())}:${pad(bj.getUTCMinutes())}:${pad(bj.getUTCSeconds())} (北京时间)`
    );
  }

  function getEffectiveSupply(c) {
    if (isFiniteNum(c.circulating_supply) && c.circulating_supply > 0 && isFiniteNum(c.multiplier) && c.multiplier > 0) {
      return c.circulating_supply / c.multiplier;
    }
    return 0;
  }

  function classifyCandidate(c) {
    const isEmaBull = Boolean(c.is_ema_bull);
    const isHighTr = isFiniteNum(c.turnover_ratio) && c.turnover_ratio >= 1.5;
    const isNegFr = isFiniteNum(c.funding_rate_4h_pct) && c.funding_rate_4h_pct < 0;
    const isResonance = isEmaBull && isHighTr;
    c.is_ema_bull = isEmaBull;
    c.is_high_turnover = isHighTr;
    c.is_neg_funding = isNegFr;
    c.is_resonance = isResonance;
  }

  function getFundingClass(fr4h) {
    if (!isFiniteNum(fr4h)) return 'sym-fr-4h font-mono tabular-nums text-[#7f91a8]';
    if (fr4h <= -0.025) return 'sym-fr-4h font-mono tabular-nums text-[#f8717e] font-bold';
    if (fr4h < 0) return 'sym-fr-4h font-mono tabular-nums text-[#f8717e]';
    return 'sym-fr-4h font-mono tabular-nums text-[#9fb0c5]';
  }

  function getTurnoverBadgeClass(tr) {
    if (!isFiniteNum(tr)) return 'text-[#7f91a8]';
    if (tr >= 5.0) return 'text-[#f1f5f9] font-bold';
    if (tr >= 2.0) return 'text-[#9bc0ff] font-semibold';
    if (tr >= 1.5) return 'text-[#8ab8ff] font-medium';
    if (tr >= 0.8) return 'text-[#9fb0c5]';
    return 'text-[#7f91a8]';
  }

  function getPosBarColor(posPct) {
    if (!isFiniteNum(posPct)) return 'bg-[#3b82f6]';
    if (posPct <= 25) return 'bg-[#22c58b]';
    if (posPct >= 75) return 'bg-[#f0b429]';
    return 'bg-[#3b82f6]';
  }

  function getRowClass(c) {
    if (c.is_resonance && c.is_neg_funding) {
      return 'resonance-neg-row candidate-row';
    }
    if (c.is_resonance) {
      return 'resonance-gold-row candidate-row';
    }
    return 'candidate-row';
  }

  function formatTickerDisplayHtml(sym) {
    if (sym.endsWith('USDT') && sym.length > 4) {
      const base = escapeHtml(sym.slice(0, -4));
      return `<span class="text-[#f1f5f9] font-bold font-sans text-[13px] tracking-tight">${base}</span><span class="text-[#7f91a8] font-mono text-[10px]">USDT</span>`;
    }
    return `<span class="text-[#f1f5f9] font-bold font-sans text-[13px] tracking-tight">${escapeHtml(sym)}</span>`;
  }

  function buildIntervalBadge(interval) {
    if (interval === 1) {
      return '<span class="text-[10px] font-mono font-semibold text-[#f5c451]" title="每 1 小时结算资金费率">1h</span>';
    }
    if (interval === 4) {
      return '<span class="text-[10px] font-mono text-[#7f91a8]">4h</span>';
    }
    if (interval === 8) {
      return '<span class="text-[10px] font-mono text-[#7f91a8]">8h</span>';
    }
    if (isFiniteNum(interval) && interval > 0) {
      return `<span class="text-[10px] font-mono text-[#7f91a8]">${escapeHtml(interval)}h</span>`;
    }
    return '<span class="text-[10px] font-mono text-[#7f91a8]">--</span>';
  }

  function buildEmaHtml(c) {
    if (!isFiniteNum(c.ema5) || !isFiniteNum(c.ema10)) {
      return '<span class="text-[#7f91a8] text-[11px]">--</span>';
    }
    const diffEma = c.ema5 - c.ema10;
    const ema5Str = escapeHtml(formatCryptoPrice(c.ema5, { refDiff: diffEma }));
    const ema10Str = escapeHtml(formatCryptoPrice(c.ema10, { refDiff: diffEma }));
    const spread = isFiniteNum(c.ema_spread_pct) ? c.ema_spread_pct : null;
    const spreadText = spread !== null ? escapeHtml(formatSignedPct(spread, 2)) : '--';

    let dotClass = 'bg-[#4b5c75]';
    let textClass = 'text-[#7f91a8]';
    if (spread !== null && Math.abs(spread) <= 0.35) {
      dotClass = 'bg-[#f0b429]';
      textClass = 'text-[#f5c451] font-semibold';
    } else if (c.is_ema_bull) {
      dotClass = 'bg-[#22c58b]';
      textClass = 'text-[#38d99f] font-semibold';
    }

    return `<div class="inline-flex items-center gap-1.5 whitespace-nowrap" title="EMA5 ${ema5Str} / EMA10 ${ema10Str}">
      <span class="w-1.5 h-1.5 rounded-full ${dotClass} shrink-0" aria-hidden="true"></span>
      <span class="text-[11px] font-mono tabular-nums ${textClass}">${spreadText}</span>
    </div>`;
  }

  function buildDurationHtml(c) {
    if (!c.is_resonance) {
      return '<span class="text-[#7f91a8] font-mono text-xs">--</span>';
    }
    const entryStr = c.resonance_entry_time ? `<div class="text-[10px] font-mono tabular-nums text-[#7f91a8] mt-0.5">${escapeHtml(c.resonance_entry_time)}</div>` : '';
    if (!isFiniteNum(c.resonance_duration_hours) || Number(c.resonance_duration_hours) < 0) {
      return `<div><span class="text-[#7f91a8] font-mono text-xs">--</span>${entryStr}</div>`;
    }
    const resHours = Math.floor(Number(c.resonance_duration_hours));
    let badge;
    if (resHours > 0) {
      if (resHours <= 3) {
        badge = `<span class="text-[11px] font-mono tabular-nums font-semibold text-[#38d99f] inline-flex items-center gap-1.5"><span class="w-1.5 h-1.5 rounded-full bg-[#22c58b]" aria-hidden="true"></span>新进 ${resHours}h</span>`;
      } else if (resHours <= 12) {
        badge = `<span class="text-[11px] font-mono tabular-nums font-semibold text-[#f5c451] inline-flex items-center gap-1.5"><span class="w-1.5 h-1.5 rounded-full bg-[#f0b429]" aria-hidden="true"></span>活跃 ${resHours}h</span>`;
      } else {
        const days = Math.floor(resHours / 24);
        const remH = resHours % 24;
        const dStr = days > 0 ? `${days}d ${remH}h` : `${resHours}h`;
        badge = `<span class="text-[11px] font-mono tabular-nums font-medium text-[#9fb0c5] inline-flex items-center gap-1.5">驻留 ${escapeHtml(dStr)}</span>`;
      }
    } else {
      badge = '<span class="text-[11px] font-mono font-semibold text-[#38d99f] inline-flex items-center gap-1.5"><span class="w-1.5 h-1.5 rounded-full bg-[#22c58b]" aria-hidden="true"></span>刚入圈</span>';
    }
    return `<div>${badge}${entryStr}</div>`;
  }

  function computePositionPct(p, low, high) {
    if (!isFiniteNum(p) || !isFiniteNum(low) || !isFiniteNum(high) || high <= low) return null;
    return Math.max(0, Math.min(100, ((p - low) / (high - low)) * 100));
  }

  function getSortMetric(c, col) {
    switch (col) {
      case 'price':
        return isFiniteNum(c.price) ? c.price : NaN;
      case 'chg':
        return isFiniteNum(c.price_chg_24h) ? c.price_chg_24h : NaN;
      case 'mc':
        return isFiniteNum(c.market_cap_usd) ? c.market_cap_usd : NaN;
      case 'vol':
        return isFiniteNum(c.volume_24h_usd) ? c.volume_24h_usd : NaN;
      case 'tr':
        return isFiniteNum(c.turnover_ratio) ? c.turnover_ratio : NaN;
      case 'fr':
        return isFiniteNum(c.funding_rate_4h_pct) ? c.funding_rate_4h_pct : NaN;
      case 'ema':
        return isFiniteNum(c.ema_spread_pct) ? c.ema_spread_pct : NaN;
      case 'duration':
        return c.is_resonance && isFiniteNum(c.resonance_duration_hours) ? c.resonance_duration_hours : NaN;
      default:
        return NaN;
    }
  }

  function syncSortAria() {
    sortHeaders.forEach((h) => {
      if (sortColumn && h.getAttribute('data-sort') === sortColumn) {
        h.setAttribute('aria-sort', sortAsc ? 'ascending' : 'descending');
      } else {
        h.removeAttribute('aria-sort');
      }
    });
  }

  function showToast(msg) {
    if (!toast || !toastText) return;
    if (toastTimer) {
      clearTimeout(toastTimer);
      toastTimer = null;
    }
    toastText.textContent = msg;
    toast.classList.remove('hidden');
    toastTimer = setTimeout(() => {
      toast.classList.add('hidden');
      toastTimer = null;
    }, 2000);
  }

  function updateKpiCards(list) {
    const total = list.length;
    const resonance = list.filter((c) => c.is_resonance);
    const negRes = resonance.filter((c) => c.is_neg_funding);
    const pureResCount = resonance.length - negRes.length;
    const negFr = list.filter((c) => c.is_neg_funding);
    const highTr = list.filter((c) => c.is_high_turnover);
    const emaBull = list.filter((c) => c.is_ema_bull);
    const overflow = list.filter((c) => Boolean(c.is_overflow_7d));

    const sampleRes = resonance.slice(0, 4).map((c) => c.symbol.replace(/USDT$/, '')).join(' · ') || '--';
    const sampleNeg = negFr
      .filter((c) => isFiniteNum(c.funding_rate_4h_pct))
      .sort((a, b) => Number(a.funding_rate_4h_pct) - Number(b.funding_rate_4h_pct))
      .slice(0, 3)
      .map((c) => `${c.symbol.replace(/USDT$/, '')}(${formatSignedPct(c.funding_rate_4h_pct, 2)})`)
      .join('、') || '--';
    const sampleTr = highTr
      .filter((c) => isFiniteNum(c.turnover_ratio))
      .sort((a, b) => Number(b.turnover_ratio) - Number(a.turnover_ratio))
      .slice(0, 3)
      .map((c) => `${c.symbol.replace(/USDT$/, '')}(${Number(c.turnover_ratio).toFixed(1)}x)`)
      .join('、') || '--';
    const sampleEma = emaBull
      .filter((c) => isFiniteNum(c.ema_spread_pct))
      .sort((a, b) => Number(b.ema_spread_pct) - Number(a.ema_spread_pct))
      .slice(0, 3)
      .map((c) => `${c.symbol.replace(/USDT$/, '')}(${formatSignedPct(c.ema_spread_pct, 1)})`)
      .join('、') || '--';

    const pctLabel = (cnt) => (total > 0 ? `${((cnt / total) * 100).toFixed(1)}%` : '--');

    if (kpiEls.resCount) kpiEls.resCount.textContent = String(resonance.length);
    if (kpiEls.resSub) kpiEls.resSub.textContent = `${pureResCount} 黄金 / ${negRes.length} 贴水`;
    if (kpiEls.resPct) kpiEls.resPct.textContent = pctLabel(resonance.length);
    if (kpiEls.resSample) {
      kpiEls.resSample.textContent = `代表: ${sampleRes}`;
      kpiEls.resSample.title = sampleRes;
    }

    if (kpiEls.negCount) kpiEls.negCount.textContent = String(negFr.length);
    if (kpiEls.negPct) kpiEls.negPct.textContent = pctLabel(negFr.length);
    if (kpiEls.negSample) {
      kpiEls.negSample.textContent = `贴水深: ${sampleNeg}`;
      kpiEls.negSample.title = sampleNeg;
    }

    if (kpiEls.highTrCount) kpiEls.highTrCount.textContent = String(highTr.length);
    if (kpiEls.highTrPct) kpiEls.highTrPct.textContent = pctLabel(highTr.length);
    if (kpiEls.highTrSample) {
      kpiEls.highTrSample.textContent = `最高: ${sampleTr}`;
      kpiEls.highTrSample.title = sampleTr;
    }

    if (kpiEls.emaBullCount) kpiEls.emaBullCount.textContent = String(emaBull.length);
    if (kpiEls.emaBullPct) kpiEls.emaBullPct.textContent = pctLabel(emaBull.length);
    if (kpiEls.emaBullSample) {
      kpiEls.emaBullSample.textContent = `多头: ${sampleEma}`;
      kpiEls.emaBullSample.title = sampleEma;
    }

    if (kpiEls.allCount) kpiEls.allCount.textContent = String(total);
    if (kpiEls.allSub) kpiEls.allSub.textContent = `含 ${overflow.length} 溢出跟踪`;
  }

  function renderTableRows(list) {
    if (!tableBody) return;
    rowRefsBySym.clear();

    const emptyRowHtml = `
      <tr id="empty-search-row" class="hidden">
        <td colspan="10" class="py-14 text-center text-[#9fb0c5] font-sans">
          <div class="text-sm font-semibold text-[#f1f5f9]">当前筛选下无匹配合约</div>
          <div class="text-xs text-[#7f91a8] mt-1">按 <kbd class="px-1.5 py-0.5 bg-[#151c27] rounded-[3px] border border-[#283446] font-mono text-[#9fb0c5]">Esc</kbd> 清空搜索词或点击重置返回全池</div>
        </td>
      </tr>`;

    const rowsHtml = list.map((c, idx) => {
      const sym = c.symbol;
      const safeSym = escapeHtml(sym);
      const tvSym = getTvSymbol(sym);
      const safeTvSym = escapeHtml(tvSym);
      const tvUrl = getTvUrl(tvSym, TV_LAYOUT_ID, '15m');
      const binanceUrl = `https://www.binance.com/zh-CN/futures/${encodeURIComponent(sym)}`;

      const pStr = escapeHtml(formatCryptoPrice(c.price));
      const lowStr = escapeHtml(formatCryptoPrice(c.low_24h));
      const highStr = escapeHtml(formatCryptoPrice(c.high_24h));
      const posPct = computePositionPct(c.price, c.low_24h, c.high_24h);
      const posWidth = posPct !== null ? posPct.toFixed(1) : '0.0';
      const posLabel = posPct !== null ? `${Math.round(posPct)}%` : '--';
      const posBarCol = getPosBarColor(posPct);
      const posTitle = posPct !== null
        ? escapeHtml(`24h振幅区间: ${lowStr} ~ ${highStr} (现价分位: ${Math.round(posPct)}%)`)
        : '24h振幅区间缺失';

      const chgClass = isFiniteNum(c.price_chg_24h)
        ? (c.price_chg_24h > 0 ? 'text-[#38d99f] font-semibold' : (c.price_chg_24h < 0 ? 'text-[#f8717e] font-semibold' : 'text-[#f1f5f9] font-medium'))
        : 'text-[#7f91a8]';
      const chgText = escapeHtml(formatSignedPct(c.price_chg_24h, 2));

      const mcStr = escapeHtml(formatMillions(c.market_cap_usd));
      let mcHtml;
      if (c.is_overflow_7d) {
        const daysLeft = isFiniteNum(c.overflow_days_left) ? c.overflow_days_left.toFixed(1) : '--';
        mcHtml = `<div class="inline-flex flex-col items-end">
          <div class="text-[#f5c451] font-mono tabular-nums font-semibold sym-mc-val">${mcStr}</div>
          <span class="text-[10px] font-mono tabular-nums text-[#f5c451]/80 mt-0.5" title="曾落入 $30M 以下底池，市值溢出后保持 7 天跟踪">7d溢出 · ${escapeHtml(daysLeft)}d</span>
        </div>`;
      } else {
        mcHtml = `<div class="text-[#f1f5f9] font-mono tabular-nums sym-mc-val">${mcStr}</div>`;
      }

      const volStr = escapeHtml(formatMillions(c.volume_24h_usd));
      const trStr = isFiniteNum(c.turnover_ratio) ? c.turnover_ratio.toFixed(2) + 'x' : '--';
      const trClass = getTurnoverBadgeClass(c.turnover_ratio);

      const fr4hStr = escapeHtml(formatSignedPct(c.funding_rate_4h_pct, 4));
      const frSettleStr = escapeHtml(formatSignedPct(c.funding_rate_pct, 4));
      const frColor = getFundingClass(c.funding_rate_4h_pct);
      const cycleBadge = buildIntervalBadge(c.funding_interval_hours);
      const showSettleNote = c.funding_interval_hours !== 4 && isFiniteNum(c.funding_rate_pct);

      return `<tr class="${getRowClass(c)}" data-sym="${safeSym}" data-tv-sym="${safeTvSym}">
        <td class="py-2.5 px-2 text-center text-[#7f91a8] font-mono tabular-nums text-[11px] sym-rank">${idx + 1}</td>
        <th scope="row" class="py-2 px-3 font-normal whitespace-nowrap sticky-col">
          <div class="flex items-baseline gap-0.5">${formatTickerDisplayHtml(sym)}</div>
          <div class="flex items-center gap-1 mt-1 -ml-1 font-sans">
            <a href="${escapeHtml(tvUrl)}" target="_blank" rel="noopener noreferrer" class="row-micro-btn" title="在 TradingView 打开 15m 专属画线布局"><span>TV</span>${EXT_ARROW_SVG}</a>
            <a href="${escapeHtml(binanceUrl)}" target="_blank" rel="noopener noreferrer" class="row-micro-btn" title="打开币安合约盘口"><span>币安</span>${EXT_ARROW_SVG}</a>
            <button type="button" class="btn-copy-sym row-micro-btn" data-copy="${safeTvSym}" data-label="复制" title="复制 TV 代码 (${safeTvSym})">复制</button>
          </div>
        </th>
        <td class="py-2.5 px-2.5 whitespace-nowrap text-right">
          <div class="text-[#f1f5f9] font-mono tabular-nums font-semibold sym-price">${pStr}</div>
          <div class="sym-pos-box w-[76px] ml-auto mt-1 flex items-center justify-end gap-1.5 text-[10px]" title="${posTitle}">
            <div class="w-12 h-1 bg-[#1b2432] rounded-[1px] relative overflow-hidden">
              <div class="sym-pos-bar h-full ${posBarCol} transition-all duration-300" style="width: ${posWidth}%"></div>
            </div>
            <span class="sym-pos-text font-mono tabular-nums text-[10px] text-[#9fb0c5] w-7 text-right">${posLabel}</span>
          </div>
        </td>
        <td class="py-2.5 px-2.5 font-mono tabular-nums sym-chg whitespace-nowrap text-right ${chgClass}">${chgText}</td>
        <td class="py-2.5 px-2.5 whitespace-nowrap text-right border-l border-[#1c2532]">${mcHtml}</td>
        <td class="py-2.5 px-2.5 text-[#9fb0c5] font-mono tabular-nums sym-vol whitespace-nowrap text-right">${volStr}</td>
        <td class="py-2.5 px-2.5 sym-tr font-mono tabular-nums whitespace-nowrap text-right"><span class="${trClass}">${escapeHtml(trStr)}</span></td>
        <td class="py-2.5 px-2.5 sym-fr whitespace-nowrap text-right border-l border-[#1c2532]">
          <div class="inline-flex items-center justify-end gap-1">
            <span class="${frColor}">${fr4hStr}</span>
            ${cycleBadge}
          </div>
          <div class="text-[10px] text-[#7f91a8] font-mono tabular-nums sym-fr-settle mt-0.5${showSettleNote ? '' : ' hidden'}">${showSettleNote ? `(${frSettleStr})` : ''}</div>
        </td>
        <td class="py-2.5 px-3 font-sans whitespace-nowrap">${buildEmaHtml(c)}</td>
        <td class="py-2.5 px-3 font-sans whitespace-nowrap sym-duration border-l border-[#1c2532]">${buildDurationHtml(c)}</td>
      </tr>`;
    }).join('');

    tableBody.innerHTML = emptyRowHtml + rowsHtml;

    tableBody.querySelectorAll('tr.candidate-row').forEach((rowEl) => {
      const sym = rowEl.getAttribute('data-sym');
      if (!sym) return;
      rowRefsBySym.set(sym, {
        rowEl,
        dirty: false,
        rankEl: rowEl.querySelector('.sym-rank'),
        priceEl: rowEl.querySelector('.sym-price'),
        posBoxEl: rowEl.querySelector('.sym-pos-box'),
        posBarEl: rowEl.querySelector('.sym-pos-bar'),
        posTextEl: rowEl.querySelector('.sym-pos-text'),
        chgEl: rowEl.querySelector('.sym-chg'),
        mcValEl: rowEl.querySelector('.sym-mc-val'),
        volEl: rowEl.querySelector('.sym-vol'),
        trBadgeEl: rowEl.querySelector('.sym-tr span'),
        fr4hEl: rowEl.querySelector('.sym-fr-4h'),
        frSettleEl: rowEl.querySelector('.sym-fr-settle'),
        durationEl: rowEl.querySelector('.sym-duration'),
      });
    });
  }

  function applySortOrder() {
    if (!tableBody) return;
    if (sortColumn) {
      candidatesState.sort((a, b) => {
        const valA = getSortMetric(a, sortColumn);
        const valB = getSortMetric(b, sortColumn);
        const okA = Number.isFinite(valA);
        const okB = Number.isFinite(valB);
        if (!okA && !okB) return a.originalOrder - b.originalOrder;
        if (!okA) return 1;
        if (!okB) return -1;
        const diff = sortAsc ? valA - valB : valB - valA;
        return diff !== 0 ? diff : a.originalOrder - b.originalOrder;
      });
    } else {
      candidatesState.sort((a, b) => a.originalOrder - b.originalOrder);
    }

    const domRows = tableBody.querySelectorAll('tr.candidate-row');
    let orderChanged = domRows.length !== candidatesState.length;
    if (!orderChanged) {
      for (let i = 0; i < candidatesState.length; i++) {
        const refs = rowRefsBySym.get(candidatesState[i].symbol);
        if (!refs || domRows[i] !== refs.rowEl) {
          orderChanged = true;
          break;
        }
      }
    }

    if (orderChanged) {
      if (typeof tableBody.moveBefore === 'function') {
        candidatesState.forEach((c) => {
          const refs = rowRefsBySym.get(c.symbol);
          if (refs && refs.rowEl) tableBody.moveBefore(refs.rowEl, null);
        });
      } else {
        const frag = document.createDocumentFragment();
        candidatesState.forEach((c) => {
          const refs = rowRefsBySym.get(c.symbol);
          if (refs && refs.rowEl) frag.appendChild(refs.rowEl);
        });
        tableBody.appendChild(frag);
      }
    }

    let visibleRank = 0;
    candidatesState.forEach((c) => {
      const refs = rowRefsBySym.get(c.symbol);
      if (!refs || !refs.rowEl || refs.rowEl.style.display === 'none') return;
      visibleRank += 1;
      if (refs.rankEl) {
        const nextRank = String(visibleRank);
        if (refs.rankEl.textContent !== nextRank) {
          refs.rankEl.textContent = nextRank;
        }
      }
    });
  }

  function matchesCurrentFilter(c) {
    const q = searchQuery.trim().toUpperCase();
    if (q && !c.symbol.toUpperCase().includes(q)) return false;
    if (currentZone === 'resonance') return c.is_resonance;
    if (currentZone === 'neg_fr') return c.is_neg_funding;
    if (currentZone === 'high_tr') return c.is_high_turnover;
    if (currentZone === 'ema_bull') return c.is_ema_bull;
    return true;
  }

  function syncRowDom(c, refs, { animatePrice = false, prevPrice = undefined, prevRes = c.is_resonance, prevNegFr = c.is_neg_funding } = {}) {
    if (!refs) return;
    if (isFiniteNum(c.price) && refs.priceEl) {
      if (animatePrice && prevPrice !== undefined && prevPrice !== c.price && !prefersReducedMotion) {
        refs.priceEl.animate(
          [
            { color: c.price > prevPrice ? '#38d99f' : '#f8717e' },
            { color: '#f1f5f9' },
          ],
          { duration: 480, easing: 'cubic-bezier(0.16, 1, 0.3, 1)' }
        );
      }
      refs.priceEl.textContent = formatCryptoPrice(c.price);
    }

    const posPct = computePositionPct(c.price, c.low_24h, c.high_24h);
    if (refs.posBarEl && posPct !== null) {
      refs.posBarEl.style.width = posPct.toFixed(1) + '%';
      refs.posBarEl.className = `sym-pos-bar h-full ${getPosBarColor(posPct)} transition-all duration-300`;
    }
    if (refs.posTextEl) {
      refs.posTextEl.textContent = posPct !== null ? `${Math.round(posPct)}%` : '--';
    }
    if (refs.posBoxEl && isFiniteNum(c.low_24h) && isFiniteNum(c.high_24h) && posPct !== null) {
      refs.posBoxEl.setAttribute('title', `24h振幅区间: ${formatCryptoPrice(c.low_24h)} ~ ${formatCryptoPrice(c.high_24h)} (现价分位: ${Math.round(posPct)}%)`);
    }

    if (isFiniteNum(c.price_chg_24h) && refs.chgEl) {
      refs.chgEl.textContent = formatSignedPct(c.price_chg_24h, 2);
      const nextChgCls = c.price_chg_24h > 0
        ? 'text-[#38d99f] font-semibold'
        : (c.price_chg_24h < 0 ? 'text-[#f8717e] font-semibold' : 'text-[#f1f5f9] font-medium');
      refs.chgEl.className = `py-2.5 px-2.5 font-mono tabular-nums sym-chg whitespace-nowrap text-right ${nextChgCls}`;
    }

    if (isFiniteNum(c.volume_24h_usd) && refs.volEl) {
      refs.volEl.textContent = formatMillions(c.volume_24h_usd);
    }
    if (isFiniteNum(c.market_cap_usd) && refs.mcValEl) {
      refs.mcValEl.textContent = formatMillions(c.market_cap_usd);
    }
    if (isFiniteNum(c.turnover_ratio) && refs.trBadgeEl) {
      refs.trBadgeEl.textContent = c.turnover_ratio.toFixed(2) + 'x';
      refs.trBadgeEl.className = getTurnoverBadgeClass(c.turnover_ratio);
    }

    if (refs.frSettleEl) {
      if (c.funding_interval_hours !== 4 && isFiniteNum(c.funding_rate_pct)) {
        refs.frSettleEl.textContent = `(${formatSignedPct(c.funding_rate_pct, 4)})`;
        refs.frSettleEl.classList.remove('hidden');
      } else {
        refs.frSettleEl.textContent = '';
        refs.frSettleEl.classList.add('hidden');
      }
    }
    if (refs.fr4hEl && isFiniteNum(c.funding_rate_4h_pct)) {
      refs.fr4hEl.textContent = formatSignedPct(c.funding_rate_4h_pct, 4);
      refs.fr4hEl.className = getFundingClass(c.funding_rate_4h_pct);
    }

    if (refs.rowEl && (prevRes !== c.is_resonance || prevNegFr !== c.is_neg_funding)) {
      refs.rowEl.className = getRowClass(c);
    }
    if (refs.durationEl && prevRes !== c.is_resonance) {
      refs.durationEl.innerHTML = buildDurationHtml(c);
    }
    refs.dirty = false;
  }

  function applyFilter() {
    let visibleCount = 0;
    candidatesState.forEach((c) => {
      const refs = rowRefsBySym.get(c.symbol);
      if (!refs || !refs.rowEl) return;
      const shouldShow = matchesCurrentFilter(c);
      const nextDisplay = shouldShow ? '' : 'none';
      if (refs.rowEl.style.display !== nextDisplay) {
        refs.rowEl.style.display = nextDisplay;
      }
      if (shouldShow) {
        if (refs.dirty) {
          syncRowDom(c, refs, { prevRes: !c.is_resonance, prevNegFr: !c.is_neg_funding });
        }
        visibleCount += 1;
        if (refs.rankEl) {
          const nextRank = String(visibleCount);
          if (refs.rankEl.textContent !== nextRank) {
            refs.rankEl.textContent = nextRank;
          }
        }
      }
    });

    if (countBadge) {
      const nextCountText = `${visibleCount} / ${candidatesState.length} 标的`;
      if (countBadge.textContent !== nextCountText) {
        countBadge.textContent = nextCountText;
      }
    }
    const emptyRow = document.getElementById('empty-search-row');
    if (emptyRow) {
      emptyRow.classList.toggle('hidden', visibleCount > 0);
    }
  }

  function switchZone(zone) {
    kpiCards.forEach((card) => {
      const isSelected = card.getAttribute('data-zone') === zone;
      card.classList.toggle('active', isSelected);
      card.setAttribute('aria-pressed', isSelected ? 'true' : 'false');
    });
    currentZone = zone;
    saveUiState();
    applyFilter();
  }

  function getVisibleTvSymbols() {
    return candidatesState.filter(matchesCurrentFilter).map((c) => getTvSymbol(c.symbol));
  }

  async function copyText(text, successMsg) {
    if (!text) return;
    let ok = false;
    try {
      if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
        await navigator.clipboard.writeText(text);
        ok = true;
      }
    } catch (err) {
      console.warn('Clipboard write failed:', err);
    }
    showToast(ok ? successMsg : '复制失败，请手动选择复制');
  }

  function updateSettleCountdown() {
    const timerEl = document.getElementById('settle-countdown');
    if (!timerEl) return;
    const now = new Date();
    const nextHour = new Date(now);
    nextHour.setUTCHours(now.getUTCHours() + 1, 0, 0, 0);
    const diffMs = nextHour - now;
    const mins = Math.floor(diffMs / 60000);
    const secs = Math.floor((diffMs % 60000) / 1000);
    timerEl.textContent = `${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
  }

  async function syncBinancePool() {
    if (!candidatesState.length) return;
    if (pollController) pollController.abort();
    pollController = new AbortController();
    const fetchId = ++activeFetchId;
    const signal = pollController.signal;

    if (syncBtn) {
      syncBtn.setAttribute('aria-busy', 'true');
      syncBtn.classList.add('opacity-75');
    }

    try {
      const [tickerRes, premiumRes] = await Promise.all([
        fetchJsonWithTimeout('api/binance/ticker24hr', { signal }, 12000),
        fetchJsonWithTimeout('api/binance/premiumIndex', { signal }, 12000),
      ]);
      if (fetchId !== activeFetchId) return;

      const tickers = tickerRes.data;
      const premiums = premiumRes.data;
      if (!Array.isArray(tickers) || !Array.isArray(premiums)) {
        throw new Error('Invalid Binance ticker24hr/premiumIndex payload');
      }

      const tickMap = new Map();
      tickers.forEach((t) => {
        if (!t || typeof t.symbol !== 'string' || !t.symbol) {
          throw new Error('Malformed ticker24hr item missing symbol');
        }
        tickMap.set(t.symbol, t);
      });
      const premMap = new Map();
      premiums.forEach((p) => {
        if (!p || typeof p.symbol !== 'string' || !p.symbol) {
          throw new Error('Malformed premiumIndex item missing symbol');
        }
        premMap.set(p.symbol, p);
      });

      let sortMetricChanged = false;
      let anyClassChanged = false;

      candidatesState.forEach((c) => {
        const sym = c.symbol;
        const refs = rowRefsBySym.get(sym);
        const t = tickMap.get(sym);
        const pr = premMap.get(sym);
        const prevSortVal = sortColumn ? getSortMetric(c, sortColumn) : NaN;
        const prevRes = c.is_resonance;
        const prevHighTr = c.is_high_turnover;
        const prevNegFr = c.is_neg_funding;
        const prevTrText = isFiniteNum(c.turnover_ratio) ? c.turnover_ratio.toFixed(1) : '';
        void prevTrText;
        let oldPrice;

        if (t) {
          const pVal = parseFloat(t.lastPrice);
          const chgVal = parseFloat(t.priceChangePercent);
          const highVal = parseFloat(t.highPrice);
          const lowVal = parseFloat(t.lowPrice);
          const volVal = parseFloat(t.quoteVolume);
          if (!isFiniteNum(pVal) || pVal <= 0 || !isFiniteNum(chgVal) || !isFiniteNum(highVal) || !isFiniteNum(lowVal) || !isFiniteNum(volVal)) {
            console.warn(`Malformed Binance ticker24hr fields for ${sym}:`, t);
          }

          if (isFiniteNum(pVal)) {
            oldPrice = prevPriceBySym.get(sym);
            prevPriceBySym.set(sym, pVal);
            c.price = pVal;
          }

          if (isFiniteNum(highVal)) c.high_24h = highVal;
          if (isFiniteNum(lowVal)) c.low_24h = lowVal;
          if (isFiniteNum(chgVal)) c.price_chg_24h = chgVal;
          if (isFiniteNum(volVal)) c.volume_24h_usd = volVal;

          const cs = getEffectiveSupply(c);
          if (cs > 0 && isFiniteNum(c.price)) {
            const currentMcap = c.price * cs;
            c.market_cap_usd = currentMcap;
            if (isFiniteNum(c.volume_24h_usd) && currentMcap > 0) {
              const trVal = c.volume_24h_usd / currentMcap;
              c.turnover_ratio = trVal;
            }
          }
        }

        if (pr) {
          const settleVal = parseFloat(pr.lastFundingRate) * 100;
          const interval = c.funding_interval_hours;
          if (!isFiniteNum(settleVal)) {
            console.warn(`Malformed Binance premiumIndex lastFundingRate for ${sym}:`, pr);
          } else {
            c.funding_rate_pct = settleVal;
            if (isFiniteNum(interval) && interval > 0) {
              c.funding_rate_4h_pct = settleVal * (4.0 / interval);
            }
          }
        }

        classifyCandidate(c);
        if (sortColumn && !sortMetricChanged) {
          const nextSortVal = getSortMetric(c, sortColumn);
          const prevOk = Number.isFinite(prevSortVal);
          const nextOk = Number.isFinite(nextSortVal);
          if (prevOk !== nextOk || (prevOk && prevSortVal !== nextSortVal)) {
            sortMetricChanged = true;
          }
        }
        const classChanged = (
          prevRes !== c.is_resonance ||
          prevHighTr !== c.is_high_turnover ||
          prevNegFr !== c.is_neg_funding
        );
        if (classChanged) anyClassChanged = true;

        const isRowHidden = Boolean(refs && refs.rowEl && refs.rowEl.style.display === 'none');
        if (refs) {
          if (isRowHidden) {
            refs.dirty = true;
          } else {
            syncRowDom(c, refs, { animatePrice: true, prevPrice: oldPrice, prevRes, prevNegFr });
          }
        }
      });

      if (syncStatusBadge) {
        syncStatusBadge.className = 'px-2 py-0.5 font-medium bg-[#22c58b]/10 text-[#38d99f] inline-flex items-center gap-1.5';
        syncStatusBadge.innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-[#22c58b]" aria-hidden="true"></span><span>币安 60s 轮询</span>';
      }

      updateKpiCards(candidatesState);
      if (sortColumn && sortMetricChanged) applySortOrder();
      if (anyClassChanged) applyFilter();
    } catch (e) {
      if (e.name !== 'AbortError') {
        console.warn('Binance pool sync error:', e);
        if (syncStatusBadge) {
          syncStatusBadge.className = 'px-2 py-0.5 font-medium bg-[#f0b429]/12 text-[#f5c451] inline-flex items-center gap-1.5';
          syncStatusBadge.innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-[#f0b429]" aria-hidden="true"></span><span>币安轮询异常</span>';
        }
      }
    } finally {
      if (fetchId === activeFetchId && syncBtn) {
        syncBtn.removeAttribute('aria-busy');
        syncBtn.classList.remove('opacity-75');
      }
    }
  }

  async function loadCandidateSnapshot({ force = false } = {}) {
    if (snapshotController) snapshotController.abort();
    snapshotController = new AbortController();
    const fetchId = ++activeSnapshotFetchId;
    const headers = {};
    if (!force && lastEtag) headers['If-None-Match'] = lastEtag;

    try {
      const { notModified, etag, data: payload } = await fetchJsonWithTimeout(
        'api/candidates',
        { cache: 'no-store', headers, signal: snapshotController.signal },
        12000
      );
      if (fetchId !== activeSnapshotFetchId) return;
      if (notModified) return;
      if (etag) lastEtag = etag;
      if (!payload || !Array.isArray(payload.candidates) || typeof payload.updated_at !== 'string' || !payload.updated_at.trim()) {
        throw new Error('Invalid /api/candidates payload');
      }

      currentVersion = payload.updated_at;
      if (poolUpdatedEl) {
        const shortUpdated = formatUpdatedAtBeijing(currentVersion)
          .replace(/^\d{4}-/, '')
          .replace(/\s*\(北京时间\)$/, '');
        poolUpdatedEl.textContent = `更新: ${shortUpdated}`;
        poolUpdatedEl.className = 'hidden sm:inline-flex px-2 py-0.5 font-mono tabular-nums text-[#9fb0c5] bg-[#10151d]';
      }

      const optionalNumericFields = [
        'price',
        'high_24h',
        'low_24h',
        'price_chg_24h',
        'market_cap_usd',
        'volume_24h_usd',
        'turnover_ratio',
        'circulating_supply',
        'multiplier',
        'funding_interval_hours',
        'funding_rate_pct',
        'funding_rate_4h_pct',
        'ema5',
        'ema10',
        'ema_spread_pct',
        'resonance_duration_hours',
        'overflow_days_left',
      ];
      candidatesState = payload.candidates.map((raw, idx) => {
        if (!raw || typeof raw !== 'object' || typeof raw.symbol !== 'string' || !raw.symbol.trim()) {
          throw new Error(`Invalid candidate entry at index ${idx}: missing symbol`);
        }
        for (const field of optionalNumericFields) {
          const val = raw[field];
          if (val !== undefined && val !== null && !isFiniteNum(val)) {
            throw new Error(`Invalid numeric field ${field} for ${raw.symbol}`);
          }
        }
        const copy = { ...raw, symbol: raw.symbol.trim(), originalOrder: idx };
        classifyCandidate(copy);
        if (isFiniteNum(copy.price)) prevPriceBySym.set(copy.symbol, copy.price);
        return copy;
      });

      renderTableRows(candidatesState);
      updateKpiCards(candidatesState);
      if (sortColumn) applySortOrder();
      applyFilter();
      syncBinancePool();
    } catch (err) {
      if (err && err.name === 'AbortError') return;
      if (fetchId !== activeSnapshotFetchId) return;
      console.error('Failed to load candidate pool:', err);
      if (poolUpdatedEl) {
        poolUpdatedEl.textContent = '候选池加载失败';
        poolUpdatedEl.className = 'px-2 py-0.5 font-mono text-[#f8717e] bg-[#f25565]/10 inline-flex';
      }
      if (!candidatesState.length && tableBody) {
        tableBody.innerHTML = `<tr><td colspan="10" class="py-14 text-center text-[#f8717e] font-sans text-xs">候选币池读取失败 (${escapeHtml(err.message)})，请检查后台 API 服务是否在线。</td></tr>`;
      }
    }
  }

  async function checkSnapshotVersion() {
    if (document.hidden) return false;
    try {
      const { data } = await fetchJsonWithTimeout('api/candidate-version', { cache: 'no-store' }, 8000);
      if (!data || typeof data.updated_at !== 'string' || !data.updated_at.trim()) {
        throw new Error('Invalid /api/candidate-version payload');
      }
      if (data.updated_at !== currentVersion) {
        await loadCandidateSnapshot({ force: true });
        return true;
      }
    } catch (err) {
      console.warn('Candidate version check failed:', err);
    }
    return false;
  }

  function startPolling() {
    stopPolling();
    pollTimer = setInterval(syncBinancePool, 60000);
    versionTimer = setInterval(checkSnapshotVersion, 30000);
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
    if (versionTimer) {
      clearInterval(versionTimer);
      versionTimer = null;
    }
  }

  function triggerHeaderSort(th) {
    if (!th) return;
    const col = th.getAttribute('data-sort');
    if (!col) return;
    if (sortColumn === col) {
      sortAsc = !sortAsc;
    } else {
      sortColumn = col;
      sortAsc = col === 'fr';
    }
    syncSortAria();
    saveUiState();
    applySortOrder();
    if (sortStatusEl) {
      const label = th.textContent ? th.textContent.trim() : col;
      sortStatusEl.textContent = `已按 ${label} ${sortAsc ? '升序' : '降序'}排列，共 ${candidatesState.length} 个标的`;
    }
  }

  kpiCards.forEach((card) => {
    card.addEventListener('click', () => {
      switchZone(card.getAttribute('data-zone') || 'all');
    });
    card.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        switchZone(card.getAttribute('data-zone') || 'all');
      }
    });
  });

  if (resetBtn) {
    resetBtn.addEventListener('click', () => {
      if (filterDebounceTimer) {
        clearTimeout(filterDebounceTimer);
        filterDebounceTimer = null;
      }
      if (searchInput) searchInput.value = '';
      searchQuery = '';
      sortColumn = null;
      sortAsc = true;
      syncSortAria();
      applySortOrder();
      switchZone('all');
    });
  }

  if (searchInput) {
    searchInput.addEventListener('input', (e) => {
      searchQuery = e.target.value;
      saveUiState();
      if (filterDebounceTimer) clearTimeout(filterDebounceTimer);
      filterDebounceTimer = setTimeout(() => {
        filterDebounceTimer = null;
        applyFilter();
      }, 120);
    });
  }

  if (batchCopyBtn) {
    batchCopyBtn.addEventListener('click', async () => {
      const symbols = getVisibleTvSymbols();
      if (!symbols.length) {
        showToast('当前筛选下无可见标的');
        return;
      }
      await copyText(symbols.join(', '), `已复制 ${symbols.length} 个标的为 TV 自选格式`);
    });
  }

  if (batchExportBtn) {
    batchExportBtn.addEventListener('click', () => {
      const symbols = getVisibleTvSymbols();
      if (!symbols.length) {
        showToast('当前筛选下无可见标的');
        return;
      }
      const blob = new Blob([symbols.join('\n')], { type: 'text/plain;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `TV_Watchlist_${currentZone}_${symbols.length}.txt`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      showToast(`已导出 ${symbols.length} 个标的 .txt 自选文件`);
    });
  }

  const supportsInvokerCommands = typeof HTMLButtonElement !== 'undefined' && 'commandForElement' in HTMLButtonElement.prototype;
  if (candidatesTable && supportsInvokerCommands) {
    candidatesTable.addEventListener('command', (e) => {
      if (e.command === '--sort' && e.source && typeof e.source.closest === 'function') {
        triggerHeaderSort(e.source.closest('th.sortable'));
      }
    });
  } else {
    sortHeaders.forEach((th) => {
      const btn = th.querySelector('button[command="--sort"]');
      const target = btn || th;
      target.addEventListener('click', () => triggerHeaderSort(th));
    });
  }

  function openCandidateChartModal(sym, tvSym) {
    if (!KlineModal) return;
    const visible = candidatesState.filter(matchesCurrentFilter);
    const idx = visible.findIndex((c) => c.symbol === sym);
    if (typeof KlineModal.setNavMeta === 'function') {
      KlineModal.setNavMeta(idx, visible.length);
    }
    KlineModal.open(sym, tvSym);
  }

  function navigateModalSymbol(delta) {
    if (!KlineModal || typeof KlineModal.getCurrentSymbol !== 'function') return;
    const visible = candidatesState.filter(matchesCurrentFilter);
    if (visible.length <= 1) return;
    const currentSym = KlineModal.getCurrentSymbol();
    const curIdx = visible.findIndex((c) => c.symbol === currentSym);
    const nextIdx = curIdx === -1 ? 0 : (curIdx + delta + visible.length) % visible.length;
    const nextItem = visible[nextIdx];
    openCandidateChartModal(nextItem.symbol, getTvSymbol(nextItem.symbol));
  }

  if (tableBody) {
    tableBody.addEventListener('click', async (e) => {
      const copyBtn = e.target.closest('.btn-copy-sym');
      if (copyBtn) {
        e.stopPropagation();
        const tvSym = copyBtn.getAttribute('data-copy');
        if (!tvSym) return;
        const label = copyBtn.getAttribute('data-label') || '复制';
        const prevTimer = copyResetTimers.get(copyBtn);
        if (prevTimer) clearTimeout(prevTimer);
        await copyText(tvSym, `已复制: ${tvSym}`);
        copyBtn.innerHTML = CHECK_SVG;
        copyBtn.classList.add('text-[#38d99f]');
        const timerId = setTimeout(() => {
          copyBtn.textContent = label;
          copyBtn.classList.remove('text-[#38d99f]');
          copyResetTimers.delete(copyBtn);
        }, 1200);
        copyResetTimers.set(copyBtn, timerId);
        return;
      }

      if (e.target.closest('a, button')) return;
      const row = e.target.closest('tr.candidate-row');
      if (row) {
        const sym = row.getAttribute('data-sym');
        const tvSym = row.getAttribute('data-tv-sym');
        if (sym && tvSym) {
          openCandidateChartModal(sym, tvSym);
        }
      }
    });
  }

  function setDrawerOpen(open) {
    if (!tvDrawer) return;
    tvDrawer.classList.toggle('hidden', !open);
    if (toggleDrawerBtn) {
      toggleDrawerBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    }
  }

  if (toggleDrawerBtn && tvDrawer) {
    toggleDrawerBtn.addEventListener('click', () => {
      setDrawerOpen(tvDrawer.classList.contains('hidden'));
    });
  }
  if (closeDrawerBtn && tvDrawer) {
    closeDrawerBtn.addEventListener('click', () => setDrawerOpen(false));
  }

  document.addEventListener('keydown', (e) => {
    const activeTag = document.activeElement ? document.activeElement.tagName : '';
    const isInputActive = activeTag === 'INPUT' || activeTag === 'TEXTAREA';

    if (KlineModal && KlineModal.isOpen()) {
      return;
    }

    if (e.key === 'Escape') {
      if (tvDrawer && !tvDrawer.classList.contains('hidden')) {
        setDrawerOpen(false);
      } else if (searchInput && (searchInput.value || document.activeElement === searchInput)) {
        if (filterDebounceTimer) {
          clearTimeout(filterDebounceTimer);
          filterDebounceTimer = null;
        }
        searchInput.value = '';
        searchQuery = '';
        searchInput.blur();
        saveUiState();
        applyFilter();
      } else if (currentZone !== 'all') {
        switchZone('all');
      }
    } else if (e.key === '/' && !isInputActive) {
      e.preventDefault();
      if (searchInput) searchInput.focus();
    } else if (!isInputActive) {
      if (e.key === '1') switchZone('resonance');
      else if (e.key === '2') switchZone('neg_fr');
      else if (e.key === '3') switchZone('high_tr');
      else if (e.key === '4') switchZone('ema_bull');
      else if (e.key === '5') switchZone('all');
    }
  });

  function startCountdown() {
    if (countdownTimer !== null) return;
    updateSettleCountdown();
    countdownTimer = setInterval(updateSettleCountdown, 1000);
  }

  function stopCountdown() {
    if (countdownTimer !== null) {
      clearInterval(countdownTimer);
      countdownTimer = null;
    }
  }

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      stopPolling();
      stopCountdown();
      activeFetchId += 1;
      if (pollController) pollController.abort();
    } else {
      startCountdown();
      checkSnapshotVersion().then((reloaded) => {
        if (!reloaded && !document.hidden) syncBinancePool();
      });
      startPolling();
    }
  });

  if (syncBtn) {
    syncBtn.addEventListener('click', () => {
      syncBinancePool();
    });
  }

  restoreUiState();
  kpiCards.forEach((card) => {
    const isSelected = card.getAttribute('data-zone') === currentZone;
    card.classList.toggle('active', isSelected);
    card.setAttribute('aria-pressed', isSelected ? 'true' : 'false');
  });
  if (searchInput && searchQuery) searchInput.value = searchQuery;
  syncSortAria();
  if (KlineModal) {
    KlineModal.init({
      visibleBars: modalVisibleBars,
      onVisibleBarsChange: (bars) => {
        modalVisibleBars = bars;
        saveUiState();
      },
      onNavigateSymbol: navigateModalSymbol,
    });
  }

  if (!document.hidden) {
    startCountdown();
  } else {
    updateSettleCountdown();
  }
  loadCandidateSnapshot({ force: true });
  startPolling();
})();
