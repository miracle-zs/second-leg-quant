(function () {
  const TV_LAYOUT_ID = 'ig0LuDL8';
  const TV_INTERVAL_MAP = {
    '15m': '15',
    '1h': '60',
    '4h': '240',
    '1d': '1D',
  };
  const CHINESE_TV_MAP = {
    '龙虾USDT': 'BINANCE:LONGXIAUSDT.P',
    '哈基米USDT': 'BINANCE:HAJIMIUSDT.P',
    '我踏马来了USDT': 'BINANCE:WOTAMALAILIAOUSDT.P',
    '币安人生USDT': 'BINANCE:BIANRENSHENGUSDT.P',
    '牛来USDT': 'BINANCE:NIULAIUSDT.P',
  };
  const ZONE_NAMES = {
    resonance: '顺势高换手共振区',
    neg_fr: '负费率贴水区',
    high_tr: '巨量高换手区',
    ema_bull: '1h EMA顺势区',
    all: '全量候选底池',
  };
  const UI_STATE_KEY = 'slq_candidates_ui_state';
  const VALID_SORT_COLS = new Set(['price', 'chg', 'mc', 'vol', 'tr', 'fr', 'ema', 'duration']);
  const VALID_BAR_COUNTS = new Set([60, 120, 168]);

  let candidatesState = [];
  const candidateBySym = new Map();
  const rowRefsBySym = new Map();
  const prevPriceBySym = new Map();

  let currentVersion = null;
  let lastEtag = null;
  let currentZone = 'resonance';
  let searchQuery = '';
  let sortColumn = null;
  let sortAsc = true;

  let pollController = null;
  let pollTimer = null;
  let versionTimer = null;
  let activeFetchId = 0;
  let toastTimer = null;

  let modalCurrentSymbol = '';
  let modalCurrentTvSymbol = '';
  let modalCurrentInterval = '15m';
  let modalVisibleBars = 120;
  let modalActiveEngine = 'native';
  let modalCandles = [];
  let modalHoverIndex = -1;
  let modalFetchController = null;
  let modalRenderRafId = null;

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
    } catch (_) {}
  }

  function restoreUiState() {
    try {
      const raw = sessionStorage.getItem(UI_STATE_KEY);
      if (!raw) return;
      const saved = JSON.parse(raw);
      if (saved && typeof saved === 'object') {
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
      }
    } catch (_) {}
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
  const modalBarsGroup = document.getElementById('modal-bars-group');
  const modalBarsBtns = document.querySelectorAll('.modal-bars-btn');
  const modalHudEma5 = document.getElementById('modal-hud-ema5');
  const modalHudEma10 = document.getElementById('modal-hud-ema10');

  function escapeHtml(val) {
    if (val === null || val === undefined) return '';
    return String(val)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function isFiniteNum(v) {
    return typeof v === 'number' && Number.isFinite(v);
  }

  function formatCryptoVal(val, refDiff = null) {
    if (!isFiniteNum(val)) return '--';
    const absV = Math.abs(val);
    let baseDec = 8;
    if (absV >= 100) baseDec = 2;
    else if (absV >= 0.01) baseDec = 4;
    else if (absV >= 0.0001) baseDec = 6;
    if (isFiniteNum(refDiff) && Math.abs(refDiff) > 0 && Math.abs(refDiff) < Math.pow(10, -baseDec)) {
      baseDec = Math.min(8, baseDec + 2);
    }
    return val.toFixed(baseDec);
  }

  function formatPrice(p) {
    return formatCryptoVal(p);
  }

  function formatSignedPct(val, decimals = 2) {
    if (!isFiniteNum(val)) return '--';
    return (val >= 0 ? '+' : '') + val.toFixed(decimals) + '%';
  }

  function formatMillions(val) {
    if (!isFiniteNum(val)) return '--';
    return '$' + (val / 1e6).toFixed(1) + 'M';
  }

  function formatUpdatedAtBeijing(updatedAt) {
    if (!updatedAt || typeof updatedAt !== 'string') return '--';
    const trimmed = updatedAt.trim();
    if (!trimmed) return '--';
    const iso = trimmed.includes('T') ? trimmed : trimmed.replace(' ', 'T') + 'Z';
    const parsed = new Date(iso);
    if (Number.isNaN(parsed.getTime())) return trimmed;
    const bj = new Date(parsed.getTime() + 8 * 3600 * 1000);
    const pad = (n) => String(n).padStart(2, '0');
    return (
      `${bj.getUTCFullYear()}-${pad(bj.getUTCMonth() + 1)}-${pad(bj.getUTCDate())} ` +
      `${pad(bj.getUTCHours())}:${pad(bj.getUTCMinutes())}:${pad(bj.getUTCSeconds())} (北京时间)`
    );
  }

  function getTvSymbol(sym) {
    return CHINESE_TV_MAP[sym] || `BINANCE:${sym}.P`;
  }

  function getTvUrl(tvSym, interval) {
    const tvInterval = TV_INTERVAL_MAP[interval] || '15';
    return `https://cn.tradingview.com/chart/${TV_LAYOUT_ID}/?symbol=${encodeURIComponent(tvSym)}&interval=${tvInterval}`;
  }

  function getEffectiveSupply(c) {
    if (isFiniteNum(c.circulating_supply) && c.circulating_supply > 0 && isFiniteNum(c.multiplier) && c.multiplier > 0) {
      return c.circulating_supply / c.multiplier;
    }
    if (isFiniteNum(c.market_cap_usd) && c.market_cap_usd > 0 && isFiniteNum(c.price) && c.price > 0) {
      return c.market_cap_usd / c.price;
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
      return 'hover:bg-slate-800/40 bg-rose-950/15 border-l-2 border-rose-500 candidate-row';
    }
    if (c.is_resonance) {
      return 'hover:bg-slate-800/40 bg-amber-950/10 border-l-2 border-amber-500/70 candidate-row';
    }
    return 'hover:bg-slate-800/30 candidate-row';
  }

  function buildZonesHtml(c) {
    let resonanceBadge = '';
    if (c.is_resonance && c.is_neg_funding) {
      resonanceBadge = '<span class="px-1.5 py-0.5 rounded text-[10px] font-bold bg-rose-500/20 text-rose-300 border border-rose-500/30" title="1h顺势+高换手，但4h负费率，合约端做空情绪浓厚">⚠️ 贴水双核(空头浓厚)</span>';
    } else if (c.is_resonance) {
      resonanceBadge = '<span class="px-1.5 py-0.5 rounded text-[10px] font-bold bg-amber-500/20 text-amber-300 border border-amber-500/30" title="1h顺势+高换手，资金面健康">👑 双核黄金共振</span>';
    }

    const tags = [];
    if (c.is_ema_bull) {
      tags.push('<span class="px-1 py-0.2 rounded text-[9px] bg-emerald-950/60 text-emerald-300 border border-emerald-500/20">1h顺势</span>');
    }
    if (c.is_high_turnover) {
      const trLabel = isFiniteNum(c.turnover_ratio) ? c.turnover_ratio.toFixed(1) + 'x' : '--';
      tags.push(`<span class="px-1 py-0.2 rounded text-[9px] bg-purple-950/60 text-purple-300 border border-purple-500/20">换手${escapeHtml(trLabel)}</span>`);
    }
    if (c.is_neg_funding) {
      tags.push('<span class="px-1 py-0.2 rounded text-[9px] bg-rose-950/60 text-rose-300 border border-rose-500/20" title="合约相对现货深度贴水，空头情绪极强">⚠️负费率贴水</span>');
    }

    return `<div class="flex items-center gap-1.5 flex-wrap">${resonanceBadge}${tags.length ? tags.join('') : '<span class="text-slate-500 text-[10px]">基础底池</span>'}</div>`;
  }

  function buildIntervalBadge(interval) {
    if (interval === 1) {
      return '<span class="px-1 py-0.2 text-[9px] font-sans font-bold rounded border bg-amber-500/20 text-amber-300 border-amber-500/30">1h期</span>';
    }
    if (interval === 4) {
      return '<span class="px-1 py-0.2 text-[9px] font-sans rounded border bg-sky-500/20 text-sky-300 border-sky-500/30">4h期</span>';
    }
    if (interval === 8) {
      return '<span class="px-1 py-0.2 text-[9px] font-sans rounded border bg-slate-800 text-slate-400 border-slate-700">8h期</span>';
    }
    if (isFiniteNum(interval) && interval > 0) {
      return `<span class="px-1 py-0.2 text-[9px] font-sans rounded border bg-slate-800 text-slate-400 border-slate-700">${escapeHtml(interval)}h期</span>`;
    }
    return '<span class="px-1 py-0.2 text-[9px] font-sans rounded border bg-slate-800 text-slate-500 border-slate-700">--期</span>';
  }

  function buildEmaHtml(c) {
    if (!isFiniteNum(c.ema5) || !isFiniteNum(c.ema10)) {
      return '<span class="text-slate-500 text-[10px]">--</span>';
    }
    const diffEma = c.ema5 - c.ema10;
    const ema5Str = escapeHtml(formatCryptoVal(c.ema5, diffEma));
    const ema10Str = escapeHtml(formatCryptoVal(c.ema10, diffEma));
    const spread = isFiniteNum(c.ema_spread_pct) ? c.ema_spread_pct : null;
    const spreadText = spread !== null ? escapeHtml(formatSignedPct(spread, 2)) : '--';

    if (spread !== null && Math.abs(spread) <= 0.35) {
      return `<div class="whitespace-nowrap">
        <div class="flex items-center gap-1.5">
          <span class="w-1.5 h-1.5 rounded-full bg-amber-400" aria-hidden="true"></span>
          <span class="text-amber-300 font-bold px-1.5 py-0.2 rounded bg-amber-500/15 border border-amber-500/30 text-[10px]">★ 粘合走平</span>
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
    const resHours = isFiniteNum(c.resonance_duration_hours) ? Math.floor(c.resonance_duration_hours) : 0;
    const entryStr = c.resonance_entry_time ? `<div class="text-[9px] font-mono text-slate-400 mt-0.5">入圈: ${escapeHtml(c.resonance_entry_time)}</div>` : '';
    let badge;
    if (resHours > 0) {
      if (resHours <= 3) {
        badge = `<span class="px-1.5 py-0.5 rounded text-[10px] font-mono font-bold bg-emerald-500/20 text-emerald-300 border border-emerald-500/30 inline-flex items-center gap-1 w-fit"><span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse"></span>⚡ 新进 ${resHours}h</span>`;
      } else if (resHours <= 12) {
        badge = `<span class="px-1.5 py-0.5 rounded text-[10px] font-mono font-bold bg-amber-500/20 text-amber-300 border border-amber-500/30 inline-flex items-center gap-1 w-fit"><span class="w-1.5 h-1.5 rounded-full bg-amber-400"></span>⏳ 活跃 ${resHours}h</span>`;
      } else {
        const days = Math.floor(resHours / 24);
        const remH = resHours % 24;
        const dStr = days > 0 ? `${days}天${remH}h` : `${resHours}h`;
        badge = `<span class="px-1.5 py-0.5 rounded text-[10px] font-mono font-bold bg-sky-500/15 text-sky-300 border border-sky-500/30 inline-flex items-center gap-1 w-fit">⏱️ 驻留 ${escapeHtml(dStr)}</span>`;
      }
    } else {
      badge = '<span class="px-1.5 py-0.5 rounded text-[10px] font-mono font-bold bg-emerald-500/20 text-emerald-300 border border-emerald-500/30 inline-flex items-center gap-1 w-fit"><span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse"></span>⚡ 刚刚进入</span>';
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
    const sampleNeg = [...negFr]
      .sort((a, b) => (a.funding_rate_4h_pct ?? 0) - (b.funding_rate_4h_pct ?? 0))
      .slice(0, 3)
      .map((c) => `${c.symbol.replace(/USDT$/, '')}(${formatSignedPct(c.funding_rate_4h_pct, 2)})`)
      .join('、') || '--';
    const sampleTr = [...highTr]
      .sort((a, b) => (b.turnover_ratio ?? 0) - (a.turnover_ratio ?? 0))
      .slice(0, 3)
      .map((c) => `${c.symbol.replace(/USDT$/, '')}(${isFiniteNum(c.turnover_ratio) ? c.turnover_ratio.toFixed(1) + 'x' : '--'})`)
      .join('、') || '--';
    const sampleEma = [...emaBull]
      .sort((a, b) => (b.ema_spread_pct ?? -Infinity) - (a.ema_spread_pct ?? -Infinity))
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
          <div class="text-2xl mb-2">🔍</div>
          <div class="text-sm font-semibold text-slate-300">未找到匹配标的</div>
          <div class="text-xs text-slate-500 mt-1">按 <kbd class="px-1.5 py-0.5 bg-slate-800 rounded border border-slate-700 font-mono text-slate-400">Esc</kbd> 清空搜索或重置筛选条件</div>
        </td>
      </tr>`;

    const rowsHtml = list.map((c, idx) => {
      const sym = String(c.symbol || '');
      const safeSym = escapeHtml(sym);
      const tvSym = getTvSymbol(sym);
      const safeTvSym = escapeHtml(tvSym);
      const tvUrl = getTvUrl(tvSym, '15m');
      const binanceUrl = `https://www.binance.com/zh-CN/futures/${encodeURIComponent(sym)}`;

      const pStr = escapeHtml(formatPrice(c.price));
      const lowStr = escapeHtml(formatPrice(c.low_24h));
      const highStr = escapeHtml(formatPrice(c.high_24h));
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
          <span class="px-1.5 py-0.2 rounded text-[9px] bg-amber-500/15 text-amber-300 border border-amber-500/30 inline-flex items-center gap-0.5" title="曾落入3000wu以下，突破后保持7天持续跟踪">★ 剩${escapeHtml(daysLeft)}天</span>
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
            <button type="button" class="btn-copy-sym px-1 py-0.2 rounded bg-slate-800 hover:bg-slate-700 text-slate-400 hover:text-slate-200 border border-slate-700 text-[10px] transition-all" data-copy="${safeTvSym}" title="复制 TV 代码 (${safeTvSym})">📋</button>
          </div>
          <div class="flex items-center gap-1 mt-0.5 text-[10px] font-sans">
            <button type="button" class="btn-preview-chart px-1.5 py-0.2 rounded bg-sky-500/10 hover:bg-sky-500/20 text-sky-300 border border-sky-500/30 transition-all font-semibold" data-sym="${safeSym}" data-tv="${safeTvSym}" title="站内秒级弹窗预览 15m 实时 K 线">👁️15m</button>
            <a href="${escapeHtml(tvUrl)}" target="_blank" rel="noopener noreferrer" class="px-1.5 py-0.2 rounded bg-[#2962FF]/15 hover:bg-[#2962FF]/25 text-[#38bdf8] hover:text-white border border-[#2962FF]/30 transition-all font-semibold" title="在 TradingView 打开 15m 专属画线布局 (ig0LuDL8)"><span>TV↗</span></a>
            <a href="${escapeHtml(binanceUrl)}" target="_blank" rel="noopener noreferrer" class="px-1.5 py-0.2 rounded bg-amber-500/10 hover:bg-amber-500/20 text-amber-300 hover:text-white border border-amber-500/30 transition-all" title="直达币安合约盘口深度"><span>币安↗</span></a>
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
    if (!tableBody || !sortColumn) return;
    candidatesState.sort((a, b) => {
      const valA = getSortMetric(a, sortColumn);
      const valB = getSortMetric(b, sortColumn);
      const okA = Number.isFinite(valA);
      const okB = Number.isFinite(valB);
      if (!okA && !okB) return 0;
      if (!okA) return 1;
      if (!okB) return -1;
      return sortAsc ? valA - valB : valB - valA;
    });
    const frag = document.createDocumentFragment();
    candidatesState.forEach((c) => {
      const refs = rowRefsBySym.get(c.symbol);
      if (refs && refs.rowEl) frag.appendChild(refs.rowEl);
    });
    tableBody.appendChild(frag);
  }

  function matchesCurrentFilter(c) {
    const q = searchQuery.trim().toUpperCase();
    if (q && !String(c.symbol || '').toUpperCase().includes(q)) return false;
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
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        ok = true;
      }
    } catch (_) {}
    if (!ok) {
      try {
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
      } catch (_) {}
    }
    showToast(ok ? successMsg : '复制失败，请手动选择复制');
  }

  // SMA-seeded EMA matching scripts/scan_second_leg_pool.py calc_ema_series
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

  function formatAxisTime(tsMs, interval) {
    const d = new Date(tsMs);
    const mm = String(d.getMonth() + 1).padStart(2, '0');
    const dd = String(d.getDate()).padStart(2, '0');
    if (interval === '1d') return `${mm}/${dd}`;
    const hh = String(d.getHours()).padStart(2, '0');
    const mi = String(d.getMinutes()).padStart(2, '0');
    return `${mm}/${dd} ${hh}:${mi}`;
  }

  function renderModalChart() {
    if (!modalCanvas || !modalNativeView || !modalCandles.length) return;
    const ctx = modalCanvas.getContext('2d');
    if (!ctx) return;

    const visibleCandles = modalCandles.slice(-modalVisibleBars);
    if (!visibleCandles.length) return;

    const dpr = window.devicePixelRatio || 1;
    const w = modalNativeView.clientWidth;
    const h = modalNativeView.clientHeight;
    if (w <= 0 || h <= 0) return;

    const targetW = Math.round(w * dpr);
    const targetH = Math.round(h * dpr);
    if (modalCanvas.width !== targetW || modalCanvas.height !== targetH) {
      modalCanvas.width = targetW;
      modalCanvas.height = targetH;
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    const bottomAxisH = 20;
    const plotH = Math.max(80, h - bottomAxisH);
    const priceH = plotH * 0.78;
    const volH = plotH * 0.18;
    const rightPadding = 68;
    const chartW = w - rightPadding;

    let minP = Infinity;
    let maxP = -Infinity;
    let maxVol = 0;
    visibleCandles.forEach((c) => {
      if (c.low < minP) minP = c.low;
      if (c.high > maxP) maxP = c.high;
      if (c.volume > maxVol) maxVol = c.volume;
    });
    const span = maxP - minP || 1;
    minP -= span * 0.05;
    maxP += span * 0.05;
    const fullSpan = maxP - minP;

    const getY = (p) => priceH - ((p - minP) / fullSpan) * priceH;
    const getVolY = (v) => plotH - 3 - (v / (maxVol || 1)) * volH;

    ctx.fillStyle = '#0b0f19';
    ctx.fillRect(0, 0, w, h);

    ctx.strokeStyle = '#1e293b';
    ctx.lineWidth = 0.8;
    ctx.fillStyle = '#64748b';
    ctx.font = '10px monospace';
    ctx.textAlign = 'left';

    const steps = 6;
    for (let i = 0; i <= steps; i++) {
      const p = minP + (fullSpan / steps) * i;
      const y = getY(p);
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(chartW, y);
      ctx.stroke();
      ctx.fillText(formatPrice(p), chartW + 6, y + 3);
    }

    const n = visibleCandles.length;
    const slot = chartW / n;
    const barW = Math.max(slot * 0.68, 2);

    // Bottom X-axis separator and time ticks
    ctx.beginPath();
    ctx.moveTo(0, plotH);
    ctx.lineTo(chartW, plotH);
    ctx.stroke();

    const tickTarget = Math.max(2, Math.min(6, Math.floor(chartW / 120)));
    const tickStep = Math.max(1, Math.floor((n - 1) / tickTarget));
    ctx.fillStyle = '#64748b';
    ctx.font = '10px monospace';
    ctx.textAlign = 'center';
    for (let i = 0; i < n; i += tickStep) {
      const x = i * slot + slot / 2;
      if (x < 36 || x > chartW - 36) continue;
      ctx.beginPath();
      ctx.moveTo(x, plotH);
      ctx.lineTo(x, plotH + 4);
      ctx.stroke();
      ctx.fillText(formatAxisTime(visibleCandles[i].time, modalCurrentInterval), x, plotH + 14);
    }
    ctx.textAlign = 'left';

    visibleCandles.forEach((c, i) => {
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
      ctx.fillRect(x - barW / 2, Math.min(yO, yC), barW, Math.max(Math.abs(yC - yO), 1));

      const vY = getVolY(c.volume);
      ctx.fillStyle = isBull ? 'rgba(16, 185, 129, 0.35)' : 'rgba(244, 63, 94, 0.35)';
      ctx.fillRect(x - barW / 2, vY, barW, plotH - 3 - vY);
    });

    const drawEmaLine = (field, color) => {
      ctx.beginPath();
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.6;
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

    drawEmaLine('ema5', '#f59e0b');
    drawEmaLine('ema10', '#a855f7');

    const last = visibleCandles[visibleCandles.length - 1];
    if (last) {
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
    }

    if (modalHoverIndex >= 0 && modalHoverIndex < n && modalTooltip) {
      const c = visibleCandles[modalHoverIndex];
      const hx = modalHoverIndex * slot + slot / 2;
      const hy = getY(c.close);

      ctx.strokeStyle = '#94a3b8';
      ctx.setLineDash([2, 2]);
      ctx.beginPath();
      ctx.moveTo(hx, 0);
      ctx.lineTo(hx, plotH);
      ctx.moveTo(0, hy);
      ctx.lineTo(chartW, hy);
      ctx.stroke();
      ctx.setLineDash([]);

      const d = new Date(c.time);
      const timeStr = `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
      const chg = c.open > 0 ? (((c.close - c.open) / c.open) * 100).toFixed(2) : null;
      const isBull = c.close >= c.open;
      const chgLabel = chg !== null ? `${Number(chg) > 0 ? '+' : ''}${chg}%` : '--';

      modalTooltip.innerHTML = `
        <div class="flex items-center gap-2 mb-1">
          <span class="text-white font-bold">${escapeHtml(timeStr)}</span>
          <span class="${isBull ? 'text-emerald-400' : 'text-rose-400'} font-bold">${escapeHtml(chgLabel)}</span>
        </div>
        <div class="grid grid-cols-4 gap-x-2.5 gap-y-0.5 text-slate-400">
          <div>开: <span class="text-white">${escapeHtml(formatPrice(c.open))}</span></div>
          <div>高: <span class="text-white">${escapeHtml(formatPrice(c.high))}</span></div>
          <div>低: <span class="text-white">${escapeHtml(formatPrice(c.low))}</span></div>
          <div>收: <span class="text-white">${escapeHtml(formatPrice(c.close))}</span></div>
        </div>
        <div class="flex items-center gap-2.5 mt-1 pt-1 border-t border-slate-800 text-[10px]">
          <span class="text-amber-400">EMA5: ${escapeHtml(formatPrice(c.ema5))}</span>
          <span class="text-purple-400">EMA10: ${escapeHtml(formatPrice(c.ema10))}</span>
          <span class="text-slate-400">量: ${escapeHtml((c.volume / 1000).toFixed(1))}k</span>
        </div>
      `;
      modalTooltip.classList.remove('hidden');
    } else if (modalTooltip) {
      modalTooltip.classList.add('hidden');
    }
  }

  async function fetchModalKlines(sym, interval) {
    if (!modalNativeView || !modalLoading) return;
    if (modalFetchController) modalFetchController.abort();
    modalFetchController = new AbortController();

    modalLoading.classList.remove('hidden');
    if (modalError) modalError.classList.add('hidden');

    try {
      const url = `api/binance/klines?symbol=${encodeURIComponent(sym)}&interval=${encodeURIComponent(interval)}&limit=168`;
      const res = await fetch(url, { signal: modalFetchController.signal });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const raw = await res.json();
      if (!Array.isArray(raw) || raw.length === 0) throw new Error('No kline data');

      modalCandles = raw.map((d) => ({
        time: d[0],
        open: parseFloat(d[1]),
        high: parseFloat(d[2]),
        low: parseFloat(d[3]),
        close: parseFloat(d[4]),
        volume: parseFloat(d[5]),
      }));

      const ema5Vals = calcEMAArray(modalCandles, 5);
      const ema10Vals = calcEMAArray(modalCandles, 10);
      modalCandles.forEach((c, idx) => {
        c.ema5 = ema5Vals[idx];
        c.ema10 = ema10Vals[idx];
      });

      const last = modalCandles[modalCandles.length - 1];
      if (last && modalChartPrice) {
        const isUp = last.close >= last.open;
        modalChartPrice.textContent = formatPrice(last.close);
        modalChartPrice.className = `font-mono font-bold text-sm ${isUp ? 'text-emerald-400' : 'text-rose-400'}`;
        if (modalHudEma5) modalHudEma5.textContent = formatPrice(last.ema5);
        if (modalHudEma10) modalHudEma10.textContent = formatPrice(last.ema10);
      }
      renderModalChart();
    } catch (err) {
      if (err.name === 'AbortError') return;
      console.error('Failed to load klines:', err);
      if (modalError) modalError.classList.remove('hidden');
    } finally {
      if (modalLoading) modalLoading.classList.add('hidden');
    }
  }

  function openTvModal(sym, tvSym) {
    if (!chartModal) return;
    modalCurrentSymbol = sym;
    modalCurrentTvSymbol = tvSym;
    modalHoverIndex = -1;

    if (modalChartTitle) modalChartTitle.textContent = sym;
    if (modalExternalLink) modalExternalLink.href = getTvUrl(tvSym, modalCurrentInterval);
    chartModal.classList.remove('hidden');

    if (modalActiveEngine === 'native') {
      if (modalNativeView) modalNativeView.classList.remove('hidden');
      if (modalTvView) modalTvView.classList.add('hidden');
      fetchModalKlines(modalCurrentSymbol, modalCurrentInterval);
    } else {
      if (modalNativeView) modalNativeView.classList.add('hidden');
      if (modalTvView) modalTvView.classList.remove('hidden');
      if (modalTvIframe) {
        const tvInterval = TV_INTERVAL_MAP[modalCurrentInterval] || '15';
        modalTvIframe.src = `https://s.tradingview.com/widgetembed/?symbol=${encodeURIComponent(tvSym)}&interval=${tvInterval}&theme=dark&style=1&timezone=Asia%2FShanghai&locale=zh_CN`;
      }
    }
  }

  function closeTvModal() {
    if (!chartModal) return;
    chartModal.classList.add('hidden');
    if (modalFetchController) modalFetchController.abort();
    if (modalTvIframe) modalTvIframe.src = 'about:blank';
    modalHoverIndex = -1;
    if (modalTooltip) modalTooltip.classList.add('hidden');
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

    let signal = pollController.signal;
    if (typeof AbortSignal !== 'undefined' && typeof AbortSignal.timeout === 'function' && typeof AbortSignal.any === 'function') {
      signal = AbortSignal.any([pollController.signal, AbortSignal.timeout(4500)]);
    }

    if (syncBtn) {
      syncBtn.setAttribute('aria-busy', 'true');
      syncBtn.classList.add('opacity-75');
    }

    try {
      const [tickerRes, premiumRes] = await Promise.all([
        fetch('api/binance/ticker24hr', { signal }),
        fetch('api/binance/premiumIndex', { signal }),
      ]);
      if (!tickerRes.ok || !premiumRes.ok) {
        throw new Error(`Binance HTTP ${tickerRes.status}/${premiumRes.status}`);
      }

      const [tickers, premiums] = await Promise.all([tickerRes.json(), premiumRes.json()]);
      if (fetchId !== activeFetchId) return;

      const tickMap = new Map();
      tickers.forEach((t) => tickMap.set(t.symbol, t));
      const premMap = new Map();
      premiums.forEach((p) => premMap.set(p.symbol, p));

      const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

      candidatesState.forEach((c) => {
        const sym = c.symbol;
        const refs = rowRefsBySym.get(sym);
        const t = tickMap.get(sym);
        const pr = premMap.get(sym);

        if (t) {
          const pVal = parseFloat(t.lastPrice);
          const chgVal = parseFloat(t.priceChangePercent);
          const highVal = parseFloat(t.highPrice);
          const lowVal = parseFloat(t.lowPrice);
          const volVal = parseFloat(t.quoteVolume);

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
              refs.priceEl.textContent = formatPrice(pVal);
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
            refs.posBoxEl.setAttribute('title', `24h振幅区间: ${formatPrice(c.low_24h)} ~ ${formatPrice(c.high_24h)} (现价分位: ${Math.round(posPct)}%)`);
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
          if (isFiniteNum(settleVal)) {
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

        const prevRes = c.is_resonance;
        const prevHighTr = c.is_high_turnover;
        const prevNegFr = c.is_neg_funding;
        const prevTrText = isFiniteNum(c.turnover_ratio) ? c.turnover_ratio.toFixed(1) : '';
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
    const headers = {};
    if (!force && lastEtag) headers['If-None-Match'] = lastEtag;

    try {
      const res = await fetch('api/candidates', { cache: 'no-store', headers });
      if (res.status === 304) return;
      if (!res.ok) throw new Error(`HTTP ${res.status}`);

      const etag = res.headers.get('ETag');
      if (etag) lastEtag = etag;

      const payload = await res.json();
      if (!payload || !Array.isArray(payload.candidates)) {
        throw new Error('Invalid /api/candidates payload');
      }

      currentVersion = payload.updated_at || null;
      if (poolUpdatedEl) {
        poolUpdatedEl.textContent = `更新: ${formatUpdatedAtBeijing(currentVersion)}`;
        poolUpdatedEl.className = 'px-2 py-0.5 text-[11px] font-mono text-slate-400 bg-slate-800/80 rounded-md border border-slate-700/60';
      }

      candidatesState = payload.candidates.map((raw) => {
        const copy = { ...raw };
        classifyCandidate(copy);
        if (isFiniteNum(copy.price)) prevPriceBySym.set(copy.symbol, copy.price);
        return copy;
      });
      candidateBySym.clear();
      candidatesState.forEach((c) => candidateBySym.set(c.symbol, c));

      renderTableRows(candidatesState);
      updateKpiCards(candidatesState);
      if (sortColumn) applySortOrder();
      applyFilter();
      syncBinancePool();
    } catch (err) {
      console.error('Failed to load candidate pool:', err);
      if (poolUpdatedEl) {
        poolUpdatedEl.textContent = '候选池加载失败';
        poolUpdatedEl.className = 'px-2 py-0.5 text-[11px] font-mono text-rose-300 bg-rose-500/10 rounded-md border border-rose-500/30';
      }
      if (!candidatesState.length && tableBody) {
        tableBody.innerHTML = `<tr><td colspan="11" class="py-12 text-center text-rose-400 font-sans">⚠️ 候选币池加载失败 (${escapeHtml(err.message)})，请检查后台 API 服务。</td></tr>`;
      }
    }
  }

  async function checkSnapshotVersion() {
    if (document.hidden) return;
    try {
      const res = await fetch('api/candidate-version', { cache: 'no-store' });
      if (!res.ok) return;
      const data = await res.json();
      if (data && data.updated_at && data.updated_at !== currentVersion) {
        await loadCandidateSnapshot({ force: true });
      }
    } catch (err) {
      console.warn('Candidate version check failed:', err);
    }
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
      if (searchInput) searchInput.value = '';
      searchQuery = '';
      sortColumn = null;
      sortAsc = true;
      sortHeaders.forEach((h) => {
        const icon = h.querySelector('.sort-icon');
        if (icon) icon.textContent = '↕';
      });
      switchZone('all');
    });
  }

  if (searchInput) {
    searchInput.addEventListener('input', (e) => {
      searchQuery = e.target.value;
      saveUiState();
      applyFilter();
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
        const originalText = copyBtn.textContent;
        await copyText(tvSym, `已复制: ${tvSym}`);
        copyBtn.textContent = '✓';
        copyBtn.classList.add('bg-emerald-600', 'text-white');
        setTimeout(() => {
          copyBtn.textContent = originalText;
          copyBtn.classList.remove('bg-emerald-600', 'text-white');
        }, 1200);
        return;
      }

      const previewBtn = e.target.closest('.btn-preview-chart');
      if (previewBtn) {
        e.stopPropagation();
        openTvModal(previewBtn.getAttribute('data-sym') || '', previewBtn.getAttribute('data-tv') || '');
      }
    });
  }

  function scheduleModalRender() {
    if (modalRenderRafId !== null) return;
    modalRenderRafId = requestAnimationFrame(() => {
      modalRenderRafId = null;
      renderModalChart();
    });
  }

  if (modalNativeView) {
    modalNativeView.addEventListener('mousemove', (e) => {
      const rect = modalNativeView.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const chartW = modalNativeView.clientWidth - 68;
      const visibleCount = Math.min(modalVisibleBars, modalCandles.length);
      if (x >= 0 && x <= chartW && visibleCount > 0) {
        const slot = chartW / visibleCount;
        const nextIdx = Math.min(Math.floor(x / slot), visibleCount - 1);
        if (nextIdx !== modalHoverIndex) {
          modalHoverIndex = nextIdx;
          scheduleModalRender();
        }
      }
    });
    modalNativeView.addEventListener('mouseleave', () => {
      if (modalHoverIndex !== -1) {
        modalHoverIndex = -1;
        scheduleModalRender();
      }
    });
  }

  function updateModalBarsUi() {
    modalBarsBtns.forEach((b) => {
      const active = Number(b.getAttribute('data-bars')) === modalVisibleBars;
      b.classList.toggle('bg-emerald-500/20', active);
      b.classList.toggle('text-emerald-300', active);
      b.classList.toggle('font-bold', active);
      b.classList.toggle('hover:bg-slate-800', !active);
      b.classList.toggle('text-slate-400', !active);
    });
  }

  modalBarsBtns.forEach((btn) => {
    btn.addEventListener('click', () => {
      const bars = Number(btn.getAttribute('data-bars'));
      if (!VALID_BAR_COUNTS.has(bars)) return;
      modalVisibleBars = bars;
      modalHoverIndex = -1;
      updateModalBarsUi();
      saveUiState();
      renderModalChart();
    });
  });

  document.querySelectorAll('.modal-interval-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.modal-interval-btn').forEach((b) => {
        b.classList.remove('bg-sky-500/20', 'text-sky-300', 'font-bold');
        b.classList.add('hover:bg-slate-800', 'text-slate-400');
      });
      btn.classList.add('bg-sky-500/20', 'text-sky-300', 'font-bold');
      btn.classList.remove('hover:bg-slate-800', 'text-slate-400');

      modalCurrentInterval = btn.getAttribute('data-interval') || '15m';
      if (modalExternalLink && modalCurrentTvSymbol) {
        modalExternalLink.href = getTvUrl(modalCurrentTvSymbol, modalCurrentInterval);
      }
      if (modalActiveEngine === 'native') {
        fetchModalKlines(modalCurrentSymbol, modalCurrentInterval);
      } else if (modalTvIframe && modalCurrentTvSymbol) {
        const tvInterval = TV_INTERVAL_MAP[modalCurrentInterval] || '15';
        modalTvIframe.src = `https://s.tradingview.com/widgetembed/?symbol=${encodeURIComponent(modalCurrentTvSymbol)}&interval=${tvInterval}&theme=dark&style=1&timezone=Asia%2FShanghai&locale=zh_CN`;
      }
    });
  });

  if (btnEngineNative && btnEngineTv) {
    btnEngineNative.addEventListener('click', () => {
      modalActiveEngine = 'native';
      btnEngineNative.className = 'px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-300 font-semibold';
      btnEngineTv.className = 'px-2 py-0.5 rounded hover:bg-slate-800 text-slate-400';
      if (modalBarsGroup) modalBarsGroup.classList.remove('hidden');
      if (modalTvView) modalTvView.classList.add('hidden');
      if (modalNativeView) modalNativeView.classList.remove('hidden');
      fetchModalKlines(modalCurrentSymbol, modalCurrentInterval);
    });
    btnEngineTv.addEventListener('click', () => {
      modalActiveEngine = 'tv';
      btnEngineTv.className = 'px-2 py-0.5 rounded bg-[#2962FF]/20 text-[#38bdf8] font-semibold';
      btnEngineNative.className = 'px-2 py-0.5 rounded hover:bg-slate-800 text-slate-400';
      if (modalBarsGroup) modalBarsGroup.classList.add('hidden');
      if (modalNativeView) modalNativeView.classList.add('hidden');
      if (modalTvView) modalTvView.classList.remove('hidden');
      if (modalTvIframe && modalCurrentTvSymbol) {
        const tvInterval = TV_INTERVAL_MAP[modalCurrentInterval] || '15';
        modalTvIframe.src = `https://s.tradingview.com/widgetembed/?symbol=${encodeURIComponent(modalCurrentTvSymbol)}&interval=${tvInterval}&theme=dark&style=1&timezone=Asia%2FShanghai&locale=zh_CN`;
      }
    });
  }

  if (modalRetryBtn) {
    modalRetryBtn.addEventListener('click', () => fetchModalKlines(modalCurrentSymbol, modalCurrentInterval));
  }
  if (modalCloseBtn) modalCloseBtn.addEventListener('click', closeTvModal);
  if (chartModal) {
    chartModal.addEventListener('click', (e) => {
      if (e.target === chartModal) closeTvModal();
    });
  }
  window.addEventListener('resize', () => {
    if (chartModal && !chartModal.classList.contains('hidden') && modalActiveEngine === 'native') {
      renderModalChart();
    }
  });

  if (toggleDrawerBtn && tvDrawer) {
    toggleDrawerBtn.addEventListener('click', () => tvDrawer.classList.toggle('hidden'));
  }
  if (closeDrawerBtn && tvDrawer) {
    closeDrawerBtn.addEventListener('click', () => tvDrawer.classList.add('hidden'));
  }

  document.addEventListener('keydown', (e) => {
    const activeTag = document.activeElement ? document.activeElement.tagName : '';
    const isInputActive = activeTag === 'INPUT' || activeTag === 'TEXTAREA';

    if (e.key === 'Escape') {
      if (chartModal && !chartModal.classList.contains('hidden')) {
        closeTvModal();
      } else if (tvDrawer && !tvDrawer.classList.contains('hidden')) {
        tvDrawer.classList.add('hidden');
      } else if (searchInput && (searchInput.value || document.activeElement === searchInput)) {
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

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      stopPolling();
      activeFetchId += 1;
      if (pollController) pollController.abort();
    } else {
      checkSnapshotVersion();
      syncBinancePool();
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
  updateModalBarsUi();

  setInterval(updateSettleCountdown, 1000);
  updateSettleCountdown();
  loadCandidateSnapshot({ force: true });
  startPolling();
})();
