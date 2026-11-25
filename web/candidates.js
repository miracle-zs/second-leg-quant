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
    ema_bull: '1h EMA顺势区',
    all: '全量候选底池',
  };
  const UI_STATE_KEY = 'slq_candidates_ui_state';
  const VALID_SORT_COLS = new Set(['price', 'chg', 'mc', 'vol', 'tr', 'fr', 'ema', 'duration']);
  const VALID_BAR_COUNTS = KlineModal ? KlineModal.VALID_BAR_COUNTS : new Set([60, 120, 168]);

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
  const activeZonePill = document.getElementById('active-zone-pill');
  const searchInput = document.getElementById('symbol-search-input');
  const resetBtn = document.getElementById('btn-reset-filter');
  const kpiCards = document.querySelectorAll('.kpi-card');
  const tableBody = document.getElementById('candidate-table-body');
  const sortHeaders = document.querySelectorAll('th.sortable');
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
    negCount: document.getElementById('kpi-neg-fr-count'),
    negSample: document.getElementById('kpi-neg-fr-sample'),
    highTrCount: document.getElementById('kpi-high-tr-count'),
    highTrSample: document.getElementById('kpi-high-tr-sample'),
    emaBullCount: document.getElementById('kpi-ema-bull-count'),
    emaBullSample: document.getElementById('kpi-ema-bull-sample'),
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
    if (!isFiniteNum(fr4h)) return 'sym-fr-4h font-mono tabular-nums text-slate-500';
    if (fr4h <= -0.025) return 'sym-fr-4h font-mono tabular-nums text-rose-400 font-bold';
    if (fr4h < 0) return 'sym-fr-4h font-mono tabular-nums text-rose-300';
    return 'sym-fr-4h font-mono tabular-nums text-slate-400';
  }

  function getTurnoverBadgeClass(tr) {
    if (!isFiniteNum(tr)) return 'text-slate-500';
    if (tr >= 2.0) return 'text-purple-300 font-bold bg-purple-500/10 px-1 py-0.5 rounded border border-purple-500/20';
    if (tr >= 1.5) return 'text-purple-400 font-semibold bg-purple-500/5 px-1 py-0.5 rounded border border-purple-500/15';
    if (tr >= 0.8) return 'text-slate-300';
    return 'text-slate-500';
  }

  function getPosBarColor(posPct) {
    if (!isFiniteNum(posPct)) return 'bg-sky-400';
    if (posPct <= 25) return 'bg-emerald-400';
    if (posPct >= 75) return 'bg-amber-400';
    return 'bg-sky-400';
  }

  function getRowClass(c) {
    if (c.is_resonance && c.is_neg_funding) {
      return 'hover:bg-slate-800/40 bg-rose-950/15 resonance-neg-row candidate-row';
    }
    if (c.is_resonance) {
      return 'hover:bg-slate-800/40 bg-amber-950/10 resonance-gold-row candidate-row';
    }
    return 'hover:bg-slate-800/30 candidate-row';
  }

  function buildZonesHtml(c) {
    let resonanceBadge = '';
    if (c.is_resonance && c.is_neg_funding) {
      resonanceBadge = '<span class="px-1.5 py-0.5 rounded text-[10px] font-bold bg-rose-500/20 text-rose-300 border border-rose-500/30" title="1h顺势+高换手，但4h负费率，合约端做空情绪浓厚">贴水双核(空头浓厚)</span>';
    } else if (c.is_resonance) {
      resonanceBadge = '<span class="px-1.5 py-0.5 rounded text-[10px] font-bold bg-amber-500/20 text-amber-300 border border-amber-500/30" title="1h顺势+高换手，资金面健康">双核黄金共振</span>';
    }

    const tags = [];
    if (c.is_ema_bull) {
      tags.push('<span class="px-1 py-0.5 rounded text-[9px] bg-emerald-950/60 text-emerald-300 border border-emerald-500/20">1h顺势</span>');
    }
    if (c.is_high_turnover) {
      const trLabel = isFiniteNum(c.turnover_ratio) ? c.turnover_ratio.toFixed(1) + 'x' : '--';
      tags.push(`<span class="px-1 py-0.5 rounded text-[9px] bg-purple-950/60 text-purple-300 border border-purple-500/20">换手${escapeHtml(trLabel)}</span>`);
    }
    if (c.is_neg_funding) {
      tags.push('<span class="px-1 py-0.5 rounded text-[9px] bg-rose-950/60 text-rose-300 border border-rose-500/20" title="合约相对现货深度贴水，空头情绪极强">负费率贴水</span>');
    }

    return `<div class="flex items-center gap-1.5 flex-wrap">${resonanceBadge}${tags.length ? tags.join('') : '<span class="text-slate-500 text-[10px]">基础底池</span>'}</div>`;
  }

  function buildIntervalBadge(interval) {
    if (interval === 1) {
      return '<span class="px-1 py-0.5 text-[9px] font-sans font-bold rounded border bg-amber-500/20 text-amber-300 border-amber-500/30">1h期</span>';
    }
    if (interval === 4) {
      return '<span class="px-1 py-0.5 text-[9px] font-sans rounded border bg-sky-500/20 text-sky-300 border-sky-500/30">4h期</span>';
    }
    if (interval === 8) {
      return '<span class="px-1 py-0.5 text-[9px] font-sans rounded border bg-slate-800 text-slate-400 border-slate-700">8h期</span>';
    }
    if (isFiniteNum(interval) && interval > 0) {
      return `<span class="px-1 py-0.5 text-[9px] font-sans rounded border bg-slate-800 text-slate-400 border-slate-700">${escapeHtml(interval)}h期</span>`;
    }
    return '<span class="px-1 py-0.5 text-[9px] font-sans rounded border bg-slate-800 text-slate-500 border-slate-700">--期</span>';
  }

  function buildEmaHtml(c) {
    if (!isFiniteNum(c.ema5) || !isFiniteNum(c.ema10)) {
      return '<span class="text-slate-500 text-[10px]">--</span>';
    }
    const diffEma = c.ema5 - c.ema10;
    const ema5Str = escapeHtml(formatCryptoPrice(c.ema5, { refDiff: diffEma }));
    const ema10Str = escapeHtml(formatCryptoPrice(c.ema10, { refDiff: diffEma }));
    const spread = isFiniteNum(c.ema_spread_pct) ? c.ema_spread_pct : null;
    const spreadText = spread !== null ? escapeHtml(formatSignedPct(spread, 2)) : '--';

    if (spread !== null && Math.abs(spread) <= 0.35) {
      return `<div class="whitespace-nowrap">
        <div class="flex items-center gap-1.5">
          <span class="w-1.5 h-1.5 rounded-full bg-amber-400" aria-hidden="true"></span>
          <span class="text-amber-300 font-bold px-1.5 py-0.5 rounded bg-amber-500/15 border border-amber-500/30 text-[10px]">粘合走平</span>
          <span class="text-[10px] text-amber-300/80 font-mono">(${spreadText})</span>
        </div>
        <div class="text-[10px] text-slate-500 font-mono mt-0.5">5:${ema5Str} / 10:${ema10Str}</div>
      </div>`;
    }
    if (c.is_ema_bull) {
      return `<div class="whitespace-nowrap">
        <div class="flex items-center gap-1.5">
          <span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" aria-hidden="true"></span>
          <span class="text-emerald-400 font-bold">金叉多头</span>
          <span class="text-[10px] text-emerald-300 font-mono">(${spreadText})</span>
        </div>
        <div class="text-[10px] text-slate-500 font-mono mt-0.5">5:${ema5Str} / 10:${ema10Str}</div>
      </div>`;
    }
    return `<div class="whitespace-nowrap">
      <div class="flex items-center gap-1.5">
        <span class="w-1.5 h-1.5 rounded-full bg-slate-600" aria-hidden="true"></span>
        <span class="text-slate-400">死叉整理</span>
        <span class="text-[10px] text-slate-500 font-mono">(${spreadText})</span>
      </div>
      <div class="text-[10px] text-slate-600 font-mono mt-0.5">5:${ema5Str} / 10:${ema10Str}</div>
    </div>`;
  }

  function buildDurationHtml(c) {
    if (!c.is_resonance) {
      return '<span class="text-slate-600 font-mono text-xs">--</span>';
    }
    const entryStr = c.resonance_entry_time ? `<div class="text-[9px] font-mono text-slate-400 mt-0.5">入圈: ${escapeHtml(c.resonance_entry_time)}</div>` : '';
    if (!isFiniteNum(c.resonance_duration_hours) || Number(c.resonance_duration_hours) < 0) {
      return `<div><span class="text-slate-500 font-mono text-xs">--</span>${entryStr}</div>`;
    }
    const resHours = Math.floor(Number(c.resonance_duration_hours));
    let badge;
    if (resHours > 0) {
      if (resHours <= 3) {
        badge = `<span class="px-1.5 py-0.5 rounded text-[10px] font-mono font-bold bg-emerald-500/20 text-emerald-300 border border-emerald-500/30 inline-flex items-center gap-1 w-fit"><span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" aria-hidden="true"></span>新进 ${resHours}h</span>`;
      } else if (resHours <= 12) {
        badge = `<span class="px-1.5 py-0.5 rounded text-[10px] font-mono font-bold bg-amber-500/20 text-amber-300 border border-amber-500/30 inline-flex items-center gap-1 w-fit"><span class="w-1.5 h-1.5 rounded-full bg-amber-400" aria-hidden="true"></span>活跃 ${resHours}h</span>`;
      } else {
        const days = Math.floor(resHours / 24);
        const remH = resHours % 24;
        const dStr = days > 0 ? `${days}天${remH}h` : `${resHours}h`;
        badge = `<span class="px-1.5 py-0.5 rounded text-[10px] font-mono font-bold bg-sky-500/15 text-sky-300 border border-sky-500/30 inline-flex items-center gap-1 w-fit"><span class="w-1.5 h-1.5 rounded-full bg-sky-400" aria-hidden="true"></span>驻留 ${escapeHtml(dStr)}</span>`;
      }
    } else {
      badge = '<span class="px-1.5 py-0.5 rounded text-[10px] font-mono font-bold bg-emerald-500/20 text-emerald-300 border border-emerald-500/30 inline-flex items-center gap-1 w-fit"><span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" aria-hidden="true"></span>刚刚进入</span>';
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
    const resonance = list.filter((c) => c.is_resonance);
    const negRes = resonance.filter((c) => c.is_neg_funding);
    const pureResCount = resonance.length - negRes.length;
    const negFr = list.filter((c) => c.is_neg_funding);
    const highTr = list.filter((c) => c.is_high_turnover);
    const emaBull = list.filter((c) => c.is_ema_bull);
    const overflow = list.filter((c) => Boolean(c.is_overflow_7d));

    const sampleRes = resonance.slice(0, 4).map((c) => c.symbol.replace(/USDT$/, '')).join('、') || '--';
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

    if (kpiEls.resCount) kpiEls.resCount.textContent = String(resonance.length);
    if (kpiEls.resSub) kpiEls.resSub.textContent = `${pureResCount}纯黄金 / ${negRes.length}贴水`;
    if (kpiEls.resSample) {
      kpiEls.resSample.textContent = `代表: ${sampleRes}`;
      kpiEls.resSample.title = sampleRes;
    }

    if (kpiEls.negCount) kpiEls.negCount.textContent = String(negFr.length);
    if (kpiEls.negSample) {
      kpiEls.negSample.textContent = `贴水深: ${sampleNeg}`;
      kpiEls.negSample.title = sampleNeg;
    }

    if (kpiEls.highTrCount) kpiEls.highTrCount.textContent = String(highTr.length);
    if (kpiEls.highTrSample) {
      kpiEls.highTrSample.textContent = `最高: ${sampleTr}`;
      kpiEls.highTrSample.title = sampleTr;
    }

    if (kpiEls.emaBullCount) kpiEls.emaBullCount.textContent = String(emaBull.length);
    if (kpiEls.emaBullSample) {
      kpiEls.emaBullSample.textContent = `多头: ${sampleEma}`;
      kpiEls.emaBullSample.title = sampleEma;
    }

    if (kpiEls.allCount) kpiEls.allCount.textContent = String(list.length);
    if (kpiEls.allSub) kpiEls.allSub.textContent = `含${overflow.length}溢出跟踪`;
  }

  function renderTableRows(list) {
    if (!tableBody) return;
    rowRefsBySym.clear();

    const emptyRowHtml = `
      <tr id="empty-search-row" class="hidden">
        <td colspan="11" class="py-12 text-center text-slate-500 font-sans">
          <div class="text-sm font-semibold text-slate-300">未找到匹配标的</div>
          <div class="text-xs text-slate-500 mt-1">按 <kbd class="px-1.5 py-0.5 bg-slate-800 rounded border border-slate-700 font-mono text-slate-400">Esc</kbd> 清空搜索或重置筛选条件</div>
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
        ? (c.price_chg_24h >= 0 ? 'text-emerald-400 font-semibold' : 'text-rose-400 font-semibold')
        : 'text-slate-500';
      const chgText = escapeHtml(formatSignedPct(c.price_chg_24h, 2));

      const mcStr = escapeHtml(formatMillions(c.market_cap_usd));
      let mcHtml;
      if (c.is_overflow_7d) {
        const daysLeft = isFiniteNum(c.overflow_days_left) ? c.overflow_days_left.toFixed(1) : '--';
        mcHtml = `<div>
          <div class="text-amber-300 font-mono tabular-nums font-semibold sym-mc-val">${mcStr}</div>
          <span class="px-1.5 py-0.5 rounded text-[9px] bg-amber-500/15 text-amber-300 border border-amber-500/30 inline-flex items-center gap-0.5" title="曾落入3000wu以下，突破后保持7天持续跟踪">溢出剩${escapeHtml(daysLeft)}天</span>
        </div>`;
      } else {
        mcHtml = `<div class="text-slate-200 font-mono tabular-nums sym-mc-val">${mcStr}</div>`;
      }

      const volStr = escapeHtml(formatMillions(c.volume_24h_usd));
      const trStr = isFiniteNum(c.turnover_ratio) ? c.turnover_ratio.toFixed(2) + 'x' : '--';
      const trClass = getTurnoverBadgeClass(c.turnover_ratio);

      const fr4hStr = escapeHtml(formatSignedPct(c.funding_rate_4h_pct, 4));
      const frSettleStr = escapeHtml(formatSignedPct(c.funding_rate_pct, 4));
      const frColor = getFundingClass(c.funding_rate_4h_pct);
      const cycleBadge = buildIntervalBadge(c.funding_interval_hours);

      return `<tr class="${getRowClass(c)}" data-sym="${safeSym}" data-tv-sym="${safeTvSym}">
        <td class="py-1.5 px-1.5 text-center text-slate-500 font-sans text-xs sym-rank">${idx + 1}</td>
        <th scope="row" class="py-1.5 px-2 font-normal whitespace-nowrap sticky-col">
          <div class="flex items-center gap-1">
            <span class="text-white font-bold font-sans text-sm tracking-tight">${safeSym}</span>
            <button type="button" class="btn-copy-sym px-1 py-0.5 rounded bg-slate-800 hover:bg-slate-700 text-slate-400 hover:text-slate-200 border border-slate-700 text-[10px] transition-all" data-copy="${safeTvSym}" data-label="复制" title="复制 TV 代码 (${safeTvSym})">复制</button>
          </div>
          <div class="flex items-center gap-1 mt-0.5 text-[10px] font-sans">
            <button type="button" class="btn-preview-chart px-1.5 py-0.5 rounded bg-sky-500/10 hover:bg-sky-500/20 text-sky-300 border border-sky-500/30 transition-all font-semibold" data-sym="${safeSym}" data-tv="${safeTvSym}" title="站内弹窗预览 15m 实时 K 线">15m图</button>
            <a href="${escapeHtml(tvUrl)}" target="_blank" rel="noopener noreferrer" class="px-1.5 py-0.5 rounded bg-[#2962FF]/15 hover:bg-[#2962FF]/25 text-[#38bdf8] hover:text-white border border-[#2962FF]/30 transition-all font-semibold" title="在 TradingView 打开 15m 专属画线布局 (ig0LuDL8)"><span>TV↗</span></a>
            <a href="${escapeHtml(binanceUrl)}" target="_blank" rel="noopener noreferrer" class="px-1.5 py-0.5 rounded bg-amber-500/10 hover:bg-amber-500/20 text-amber-300 hover:text-white border border-amber-500/30 transition-all" title="直达币安合约盘口深度"><span>币安↗</span></a>
          </div>
        </th>
        <td class="py-1.5 px-2 whitespace-nowrap">
          <div class="text-white font-mono tabular-nums font-semibold sym-price">${pStr}</div>
          <div class="sym-pos-box w-16 mt-0.5 flex items-center gap-1 text-[9px] text-slate-500" title="${posTitle}">
            <div class="w-full h-1 bg-slate-800 rounded-full relative overflow-hidden">
              <div class="sym-pos-bar h-full ${posBarCol} rounded-full transition-all duration-300" style="width: ${posWidth}%"></div>
            </div>
            <span class="sym-pos-text font-mono text-[9px] text-slate-400">${posLabel}</span>
          </div>
        </td>
        <td class="py-1.5 px-2 font-mono tabular-nums sym-chg whitespace-nowrap ${chgClass}">${chgText}</td>
        <td class="py-1.5 px-2 whitespace-nowrap">${mcHtml}</td>
        <td class="py-1.5 px-2 text-slate-300 font-mono tabular-nums sym-vol whitespace-nowrap">${volStr}</td>
        <td class="py-1.5 px-2 sym-tr font-mono tabular-nums whitespace-nowrap"><span class="${trClass}">${escapeHtml(trStr)}</span></td>
        <td class="py-1.5 px-2 sym-fr whitespace-nowrap">
          <div class="flex items-center gap-1">
            <span class="${frColor}">${fr4hStr}</span>
            ${cycleBadge}
          </div>
          <div class="text-[10px] text-slate-500 font-mono sym-fr-settle">单期: ${frSettleStr}</div>
        </td>
        <td class="py-1.5 px-2 font-sans whitespace-nowrap">${buildEmaHtml(c)}</td>
        <td class="py-1.5 px-2 font-sans whitespace-nowrap sym-duration">${buildDurationHtml(c)}</td>
        <td class="py-1.5 px-2 font-sans whitespace-nowrap sym-zones">${buildZonesHtml(c)}</td>
      </tr>`;
    }).join('');

    tableBody.innerHTML = emptyRowHtml + rowsHtml;

    tableBody.querySelectorAll('tr.candidate-row').forEach((rowEl) => {
      const sym = rowEl.getAttribute('data-sym');
      if (!sym) return;
      rowRefsBySym.set(sym, {
        rowEl,
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
        zonesEl: rowEl.querySelector('.sym-zones'),
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
      const frag = document.createDocumentFragment();
      candidatesState.forEach((c) => {
        const refs = rowRefsBySym.get(c.symbol);
        if (refs && refs.rowEl) frag.appendChild(refs.rowEl);
      });
      tableBody.appendChild(frag);
    }

    candidatesState.forEach((c, idx) => {
      const refs = rowRefsBySym.get(c.symbol);
      if (refs && refs.rankEl) {
        const nextRank = String(idx + 1);
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

  function applyFilter() {
    let visibleCount = 0;
    candidatesState.forEach((c) => {
      const refs = rowRefsBySym.get(c.symbol);
      if (!refs || !refs.rowEl) return;
      if (matchesCurrentFilter(c)) {
        refs.rowEl.style.display = '';
        visibleCount += 1;
      } else {
        refs.rowEl.style.display = 'none';
      }
    });

    if (countBadge) {
      countBadge.textContent = `${visibleCount} / ${candidatesState.length} 标的`;
    }
    if (activeZonePill) {
      activeZonePill.textContent = `当前筛选: ${ZONE_NAMES[currentZone] || '自定义'}`;
    }
    const emptyRow = document.getElementById('empty-search-row');
    if (emptyRow) {
      emptyRow.classList.toggle('hidden', visibleCount > 0);
    }
  }

  function switchZone(zone) {
    kpiCards.forEach((card) => {
      card.classList.toggle('active', card.getAttribute('data-zone') === zone);
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

      const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

      candidatesState.forEach((c) => {
        const sym = c.symbol;
        const refs = rowRefsBySym.get(sym);
        const t = tickMap.get(sym);
        const pr = premMap.get(sym);
        const prevRes = c.is_resonance;
        const prevHighTr = c.is_high_turnover;
        const prevNegFr = c.is_neg_funding;
        const prevTrText = isFiniteNum(c.turnover_ratio) ? c.turnover_ratio.toFixed(1) : '';

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
            const oldP = prevPriceBySym.get(sym);
            if (refs && refs.priceEl) {
              if (oldP !== undefined && oldP !== pVal && !prefersReducedMotion) {
                refs.priceEl.animate(
                  [
                    { color: pVal > oldP ? '#34d399' : '#f87171', transform: 'scale(1.05)' },
                    { color: '#ffffff', transform: 'scale(1)' },
                  ],
                  { duration: 600, easing: 'ease-out' }
                );
              }
              refs.priceEl.textContent = formatCryptoPrice(pVal);
            }
            prevPriceBySym.set(sym, pVal);
            c.price = pVal;
          }

          if (isFiniteNum(highVal)) c.high_24h = highVal;
          if (isFiniteNum(lowVal)) c.low_24h = lowVal;
          const posPct = computePositionPct(c.price, c.low_24h, c.high_24h);
          if (refs && refs.posBarEl && posPct !== null) {
            refs.posBarEl.style.width = posPct.toFixed(1) + '%';
            refs.posBarEl.className = `sym-pos-bar h-full ${getPosBarColor(posPct)} rounded-full transition-all duration-300`;
          }
          if (refs && refs.posTextEl) {
            refs.posTextEl.textContent = posPct !== null ? `${Math.round(posPct)}%` : '--';
          }
          if (refs && refs.posBoxEl && isFiniteNum(c.low_24h) && isFiniteNum(c.high_24h) && posPct !== null) {
            refs.posBoxEl.setAttribute('title', `24h振幅区间: ${formatCryptoPrice(c.low_24h)} ~ ${formatCryptoPrice(c.high_24h)} (现价分位: ${Math.round(posPct)}%)`);
          }

          if (isFiniteNum(chgVal)) {
            c.price_chg_24h = chgVal;
            if (refs && refs.chgEl) {
              refs.chgEl.textContent = formatSignedPct(chgVal, 2);
              refs.chgEl.className = `py-1.5 px-2 font-mono tabular-nums sym-chg whitespace-nowrap ${chgVal >= 0 ? 'text-emerald-400 font-semibold' : 'text-rose-400 font-semibold'}`;
            }
          }

          if (isFiniteNum(volVal)) {
            c.volume_24h_usd = volVal;
            if (refs && refs.volEl) refs.volEl.textContent = formatMillions(volVal);
          }

          const cs = getEffectiveSupply(c);
          if (cs > 0 && isFiniteNum(c.price)) {
            const currentMcap = c.price * cs;
            c.market_cap_usd = currentMcap;
            if (refs && refs.mcValEl) refs.mcValEl.textContent = formatMillions(currentMcap);
            if (isFiniteNum(c.volume_24h_usd) && currentMcap > 0) {
              const trVal = c.volume_24h_usd / currentMcap;
              c.turnover_ratio = trVal;
              if (refs && refs.trBadgeEl) {
                refs.trBadgeEl.textContent = trVal.toFixed(2) + 'x';
                refs.trBadgeEl.className = getTurnoverBadgeClass(trVal);
              }
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
            if (refs && refs.frSettleEl) {
              refs.frSettleEl.textContent = `单期: ${formatSignedPct(settleVal, 4)}`;
            }
            if (isFiniteNum(interval) && interval > 0) {
              const fr4hVal = settleVal * (4.0 / interval);
              c.funding_rate_4h_pct = fr4hVal;
              if (refs && refs.fr4hEl) {
                refs.fr4hEl.textContent = formatSignedPct(fr4hVal, 4);
                refs.fr4hEl.className = getFundingClass(fr4hVal);
              }
            }
          }
        }

        classifyCandidate(c);
        const currTrText = isFiniteNum(c.turnover_ratio) ? c.turnover_ratio.toFixed(1) : '';
        const classChanged = (
          prevRes !== c.is_resonance ||
          prevHighTr !== c.is_high_turnover ||
          prevNegFr !== c.is_neg_funding
        );
        if (refs && refs.rowEl && (prevRes !== c.is_resonance || prevNegFr !== c.is_neg_funding)) {
          refs.rowEl.className = getRowClass(c);
        }
        if (refs && refs.zonesEl && (classChanged || (c.is_high_turnover && prevTrText !== currTrText))) {
          refs.zonesEl.innerHTML = buildZonesHtml(c);
        }
        if (refs && refs.durationEl && prevRes !== c.is_resonance) {
          refs.durationEl.innerHTML = buildDurationHtml(c);
        }
      });

      if (syncStatusBadge) {
        syncStatusBadge.className = 'px-2 py-0.5 text-[11px] font-semibold rounded-md bg-emerald-500/10 text-emerald-300 border border-emerald-500/20 flex items-center gap-1.5';
        syncStatusBadge.innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-ping" aria-hidden="true"></span><span>币安 60 秒轮询</span>';
      }

      updateKpiCards(candidatesState);
      if (sortColumn) applySortOrder();
      applyFilter();
    } catch (e) {
      if (e.name !== 'AbortError') {
        console.warn('Binance pool sync error:', e);
        if (syncStatusBadge) {
          syncStatusBadge.className = 'px-2 py-0.5 text-[11px] font-semibold rounded-md bg-amber-500/10 text-amber-300 border border-amber-500/30 flex items-center gap-1.5';
          syncStatusBadge.innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-amber-400" aria-hidden="true"></span><span>币安轮询异常</span>';
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
        poolUpdatedEl.textContent = `更新: ${formatUpdatedAtBeijing(currentVersion)}`;
        poolUpdatedEl.className = 'px-2 py-0.5 text-[11px] font-mono text-slate-400 bg-slate-800/80 rounded-md border border-slate-700/60';
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
        poolUpdatedEl.className = 'px-2 py-0.5 text-[11px] font-mono text-rose-300 bg-rose-500/10 rounded-md border border-rose-500/30';
      }
      if (!candidatesState.length && tableBody) {
        tableBody.innerHTML = `<tr><td colspan="11" class="py-12 text-center text-rose-400 font-sans">候选币池加载失败 (${escapeHtml(err.message)})，请检查后台 API 服务。</td></tr>`;
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

  kpiCards.forEach((card) => {
    card.addEventListener('click', () => {
      switchZone(card.getAttribute('data-zone') || 'all');
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
      sortHeaders.forEach((h) => {
        const icon = h.querySelector('.sort-icon');
        if (icon) icon.textContent = '↕';
      });
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
      await copyText(symbols.join(', '), `已复制 ${symbols.length} 个标的为 TV 自选格式！`);
    });
  }

  if (batchExportBtn) {
    batchExportBtn.addEventListener('click', () => {
      const symbols = getVisibleTvSymbols();
      if (!symbols.length) return;
      const blob = new Blob([symbols.join('\n')], { type: 'text/plain;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `TV_Watchlist_${currentZone}_${symbols.length}.txt`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      showToast(`已导出 ${symbols.length} 个标的 .txt 自选文件！`);
    });
  }

  sortHeaders.forEach((th) => {
    th.addEventListener('click', () => {
      const col = th.getAttribute('data-sort');
      if (sortColumn === col) {
        sortAsc = !sortAsc;
      } else {
        sortColumn = col;
        sortAsc = col === 'fr';
      }
      sortHeaders.forEach((h) => {
        const icon = h.querySelector('.sort-icon');
        if (icon) icon.textContent = '↕';
      });
      const activeIcon = th.querySelector('.sort-icon');
      if (activeIcon) activeIcon.textContent = sortAsc ? '▲' : '▼';
      saveUiState();
      applySortOrder();
    });
  });

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
        copyBtn.textContent = '✓';
        copyBtn.classList.add('bg-emerald-600', 'text-white');
        const timerId = setTimeout(() => {
          copyBtn.textContent = label;
          copyBtn.classList.remove('bg-emerald-600', 'text-white');
          copyResetTimers.delete(copyBtn);
        }, 1200);
        copyResetTimers.set(copyBtn, timerId);
        return;
      }

      const previewBtn = e.target.closest('.btn-preview-chart');
      if (previewBtn) {
        e.stopPropagation();
        const sym = previewBtn.getAttribute('data-sym');
        const tvSym = previewBtn.getAttribute('data-tv');
        if (!sym || !tvSym) {
          throw new Error('Preview button missing data-sym or data-tv');
        }
        if (KlineModal) KlineModal.open(sym, tvSym);
      }
    });
  }

  if (toggleDrawerBtn && tvDrawer) {
    toggleDrawerBtn.addEventListener('click', () => tvDrawer.classList.toggle('hidden'));
  }
  if (closeDrawerBtn && tvDrawer) {
    closeDrawerBtn.addEventListener('click', () => tvDrawer.classList.add('hidden'));
  }

  document.addEventListener('keydown', (e) => {
    const activeTag = document.activeElement ? document.activeElement.tagName : '';
    const isInputActive = activeTag === 'INPUT' || activeTag === 'TEXTAREA';

    if (KlineModal && KlineModal.isOpen()) {
      return;
    }

    if (e.key === 'Escape') {
      if (tvDrawer && !tvDrawer.classList.contains('hidden')) {
        tvDrawer.classList.add('hidden');
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
    card.classList.toggle('active', card.getAttribute('data-zone') === currentZone);
  });
  if (searchInput && searchQuery) searchInput.value = searchQuery;
  if (sortColumn) {
    sortHeaders.forEach((h) => {
      const icon = h.querySelector('.sort-icon');
      if (!icon) return;
      icon.textContent = h.getAttribute('data-sort') === sortColumn ? (sortAsc ? '▲' : '▼') : '↕';
    });
  }
  if (KlineModal) {
    KlineModal.init({
      visibleBars: modalVisibleBars,
      onVisibleBarsChange: (bars) => {
        modalVisibleBars = bars;
        saveUiState();
      },
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
