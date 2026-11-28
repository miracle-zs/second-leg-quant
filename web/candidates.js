(function () {
  const Common = window.SecondLegCommon;
  const Render = window.SecondLegCandidatesRender;
  const KlineModal = window.SecondLegKlineModal;
  const {
    debounce,
    bindSortButtons,
    escapeHtml,
    isFiniteNum,
    getTvSymbol,
    getConfig,
    ensureConfig,
    fetchJsonWithTimeout,
    formatBeijingTime,
    setIfChanged,
    focusSafely,
    setButtonBusy,
    updateStatusBadge,
    createPollScheduler,
  } = Common;

  const {
    DEFAULT_HIGH_TURNOVER_THRESHOLD,
    isValidFundingIntervalHours,
    getEffectiveSupply,
    classifyCandidate,
    getSortMetric,
    updateKpiCards: renderKpiCards,
    renderTableRows,
    syncRowDom,
    applySortOrder: renderSortOrder,
  } = Render;

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
  const POLL_INTERVAL_MS = 60000;
  const VERSION_CHECK_INTERVAL_MS = 30000;
  const VISIBILITY_COOLDOWN_MS = 5000;

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
  let highTurnoverThreshold = DEFAULT_HIGH_TURNOVER_THRESHOLD;
  let hiddenOrderDirty = false;

  let pollController = null;
  let snapshotController = null;
  let versionController = null;
  let versionCheckInFlight = null;
  let countdownTimer = null;
  let pollingActive = false;
  let activeFetchId = 0;
  let activeSnapshotFetchId = 0;
  let lastSyncActivityAt = 0;
  const syncBadgeCache = { className: '', html: '' };
  let toastTimer = null;
  let resizeRafId = null;
  let prefersReducedMotion = false;
  if (typeof window.matchMedia === 'function') {
    const reducedMotionQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
    if (reducedMotionQuery) {
      prefersReducedMotion = Boolean(reducedMotionQuery.matches);
      if (typeof reducedMotionQuery.addEventListener === 'function') {
        reducedMotionQuery.addEventListener('change', (e) => {
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
  const tickerSyncedEl = document.getElementById('ticker-synced-time');
  const countBadge = document.getElementById('pool-count-badge');
  const searchInput = document.getElementById('symbol-search-input');
  const resetBtn = document.getElementById('btn-reset-filter');
  const kpiCards = document.querySelectorAll('.kpi-card');
  const tableScrollEl = document.getElementById('candidates-table-scroll');
  const candidatesTable = document.getElementById('candidates-table');
  const tableBody = document.getElementById('candidate-table-body');
  const sortStatusEl = document.getElementById('candidates-sort-status');
  const batchCopyBtn = document.getElementById('btn-batch-copy-tv');
  const batchCopyLabelEl = document.getElementById('btn-batch-copy-tv-label');
  const batchExportBtn = document.getElementById('btn-batch-export-tv');
  const batchExportLabelEl = document.getElementById('btn-batch-export-tv-label');
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
    resBar: document.getElementById('kpi-resonance-bar'),
    negCount: document.getElementById('kpi-neg-fr-count'),
    negSample: document.getElementById('kpi-neg-fr-sample'),
    negPct: document.getElementById('kpi-neg-fr-pct'),
    negBar: document.getElementById('kpi-neg-fr-bar'),
    highTrCount: document.getElementById('kpi-high-tr-count'),
    highTrSub: document.getElementById('kpi-high-tr-sub'),
    highTrSample: document.getElementById('kpi-high-tr-sample'),
    highTrPct: document.getElementById('kpi-high-tr-pct'),
    highTrBar: document.getElementById('kpi-high-tr-bar'),
    emaBullCount: document.getElementById('kpi-ema-bull-count'),
    emaBullSample: document.getElementById('kpi-ema-bull-sample'),
    emaBullPct: document.getElementById('kpi-ema-bull-pct'),
    emaBullBar: document.getElementById('kpi-ema-bull-bar'),
    allCount: document.getElementById('kpi-all-count'),
    allSub: document.getElementById('kpi-all-sub'),
    allSample: document.getElementById('kpi-all-sample'),
    allBar: document.getElementById('kpi-all-bar'),
  };

  function formatUpdatedAtBeijing(updatedAt) {
    return formatBeijingTime(updatedAt, { suffix: ' (北京时间)' });
  }

  function resolveHighTurnoverThreshold() {
    const val = (getConfig() || {}).high_turnover_threshold;
    highTurnoverThreshold = (isFiniteNum(val) && Number(val) > 0)
      ? Number(val)
      : DEFAULT_HIGH_TURNOVER_THRESHOLD;
    return highTurnoverThreshold;
  }

  let sortBinding = null;

  function syncSortAria() {
    if (sortBinding) {
      sortBinding.syncAria(sortColumn, sortAsc);
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
    return renderKpiCards(kpiEls, list, highTurnoverThreshold);
  }

  function applySortOrder() {
    hiddenOrderDirty = renderSortOrder({
      tableBody,
      candidatesState,
      rowRefsBySym,
      sortColumn,
      sortAsc,
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

  function getVisibleCandidates() {
    return candidatesState.filter(matchesCurrentFilter);
  }

  function applyFilter() {
    let visibleCount = 0;
    let anyShownFromHidden = false;
    for (let i = 0; i < candidatesState.length; i += 1) {
      const c = candidatesState[i];
      const refs = rowRefsBySym.get(c.symbol);
      if (!refs || !refs.rowEl) continue;
      const shouldShow = matchesCurrentFilter(c);
      const wasHidden = refs.rowEl.style.display === 'none';
      const nextDisplay = shouldShow ? '' : 'none';
      if (refs.rowEl.style.display !== nextDisplay) {
        refs.rowEl.style.display = nextDisplay;
      }
      if (shouldShow) {
        if (wasHidden) anyShownFromHidden = true;
        if (refs.dirty) {
          syncRowDom(c, refs, {
            prevRes: !c.is_resonance,
            prevNegFr: !c.is_neg_funding,
            prefersReducedMotion,
            highTurnoverThreshold,
          });
        }
        visibleCount += 1;
        setIfChanged(refs.rankEl, 'textContent', String(visibleCount));
      }
    }

    if (anyShownFromHidden && (sortColumn || hiddenOrderDirty)) {
      applySortOrder();
    }

    setIfChanged(countBadge, 'textContent', `${visibleCount} / ${candidatesState.length} 标的`);
    setIfChanged(batchCopyLabelEl, 'textContent', `复制 TV 自选 (${visibleCount})`);
    setIfChanged(batchCopyBtn, 'title', `复制当前筛选生效的 ${visibleCount} 个标的代码，在 TradingView 自选列表粘贴即可批量添加`);
    setIfChanged(batchExportLabelEl, 'textContent', `导出 .txt (${visibleCount})`);
    setIfChanged(batchExportBtn, 'title', `导出当前筛选生效的 ${visibleCount} 个标的 .txt 文件以导入 TradingView 自选列表`);

    const emptyRow = document.getElementById('empty-search-row');
    if (emptyRow) {
      emptyRow.classList.toggle('hidden', visibleCount > 0);
      if (visibleCount === 0) {
        const emptyTitleEl = document.getElementById('empty-state-title');
        const emptyHintEl = document.getElementById('empty-state-hint');
        const emptyResetBtn = document.getElementById('btn-empty-reset-filter');
        const isBackendEmpty = candidatesState.length === 0;
        if (emptyTitleEl) {
          setIfChanged(emptyTitleEl, 'textContent', isBackendEmpty ? '当前候选底池暂无符合策略标的' : '当前筛选下无匹配合约');
        }
        if (emptyHintEl) {
          emptyHintEl.innerHTML = isBackendEmpty
            ? '后台扫描每小时更新，可点击右上角「刷新」手动同步'
            : '按 <kbd class="px-1.5 py-0.5 bg-[#151c27] rounded-[3px] border border-[#283446] font-mono text-[#9fb0c5]">Esc</kbd> 清空搜索词或点击下方按钮重置筛选';
        }
        if (emptyResetBtn && emptyResetBtn.classList) {
          emptyResetBtn.classList.toggle('hidden', isBackendEmpty);
        }
      }
    }
    return visibleCount;
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
    return getVisibleCandidates().map((c) => getTvSymbol(c.symbol));
  }

  async function copyText(text, successMsg) {
    if (!text) return false;
    let ok = false;
    try {
      if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
        await navigator.clipboard.writeText(text);
        ok = true;
      }
    } catch (err) {
      console.warn('Clipboard write failed:', err);
    }
    if (!ok && typeof document !== 'undefined' && typeof document.createElement === 'function' && typeof document.execCommand === 'function') {
      let textarea = null;
      const prevActive = document.activeElement;
      try {
        textarea = document.createElement('textarea');
        textarea.value = text;
        textarea.setAttribute('readonly', '');
        textarea.style.position = 'fixed';
        textarea.style.top = '-9999px';
        textarea.style.left = '-9999px';
        textarea.style.opacity = '0';
        (document.body || document.documentElement).appendChild(textarea);
        textarea.focus();
        textarea.select();
        if (typeof textarea.setSelectionRange === 'function') {
          textarea.setSelectionRange(0, text.length);
        }
        ok = Boolean(document.execCommand('copy'));
      } catch (fallbackErr) {
        console.warn('Fallback execCommand copy failed:', fallbackErr);
      } finally {
        if (textarea && textarea.parentNode) {
          textarea.parentNode.removeChild(textarea);
        }
        focusSafely(prevActive);
      }
    }
    showToast(ok ? successMsg : '复制失败，请手动选择复制');
    return ok;
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
    setIfChanged(timerEl, 'textContent', `${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}`);
  }

  function updateSyncStatusBadge(level, label) {
    if (level === 'warn') {
      updateStatusBadge(syncStatusBadge, syncBadgeCache, {
        className: 'w-full px-2 py-0.5 font-sans font-medium bg-[#f0b429]/[0.12] text-[#f5c451] inline-flex items-center gap-1.5',
        dotClass: 'bg-[#f0b429]',
        label,
      });
    } else {
      updateStatusBadge(syncStatusBadge, syncBadgeCache, {
        className: 'w-full px-2 py-0.5 font-sans font-medium bg-[#22c58b]/10 text-[#38d99f] inline-flex items-center gap-1.5',
        dotClass: 'bg-[#22c58b]',
        label,
      });
    }
  }

  const pollScheduler = createPollScheduler(() => {
    if (pollingActive) syncBinancePool();
  }, POLL_INTERVAL_MS);

  const versionScheduler = createPollScheduler(async () => {
    if (!pollingActive) return;
    await checkSnapshotVersion();
    if (pollingActive && !document.hidden && !versionScheduler.isScheduled()) {
      versionScheduler.schedule();
    }
  }, VERSION_CHECK_INTERVAL_MS);

  async function syncBinancePool({ manual = false } = {}) {
    pollScheduler.clear();
    if (!candidatesState.length) {
      return loadCandidateSnapshot({ force: true, manual });
    }
    if (pollController) pollController.abort();
    pollController = new AbortController();
    const fetchId = ++activeFetchId;
    const signal = pollController.signal;

    setButtonBusy(syncBtn, true);

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
        if (rowRefsBySym.has(t.symbol)) tickMap.set(t.symbol, t);
      });
      const premMap = new Map();
      premiums.forEach((p) => {
        if (!p || typeof p.symbol !== 'string' || !p.symbol) {
          throw new Error('Malformed premiumIndex item missing symbol');
        }
        if (rowRefsBySym.has(p.symbol)) premMap.set(p.symbol, p);
      });

      let sortMetricChanged = false;
      let anyClassChanged = false;
      const flashBudget = { count: 0 };

      candidatesState.forEach((c) => {
        const sym = c.symbol;
        const refs = rowRefsBySym.get(sym);
        const t = tickMap.get(sym);
        const pr = premMap.get(sym);
        const prevSortVal = sortColumn ? getSortMetric(c, sortColumn) : NaN;
        const prevRes = c.is_resonance;
        const prevHighTr = c.is_high_turnover;
        const prevNegFr = c.is_neg_funding;
        let oldPrice;

        if (t) {
          const pVal = parseFloat(t.lastPrice);
          const chgVal = parseFloat(t.priceChangePercent);
          const highVal = parseFloat(t.highPrice);
          const lowVal = parseFloat(t.lowPrice);
          const volVal = parseFloat(t.quoteVolume);
          if (!isFiniteNum(pVal) || pVal <= 0 || !isFiniteNum(chgVal) || !isFiniteNum(highVal) || highVal <= 0 || !isFiniteNum(lowVal) || lowVal <= 0 || !isFiniteNum(volVal) || volVal < 0) {
            console.warn(`Malformed Binance ticker24hr fields for ${sym}:`, t);
          }

          if (isFiniteNum(pVal) && pVal > 0) {
            oldPrice = prevPriceBySym.get(sym);
            prevPriceBySym.set(sym, pVal);
            c.price = pVal;
          }

          if (isFiniteNum(highVal) && highVal > 0) c.high_24h = highVal;
          if (isFiniteNum(lowVal) && lowVal > 0) c.low_24h = lowVal;
          if (isFiniteNum(chgVal)) c.price_chg_24h = chgVal;
          if (isFiniteNum(volVal) && volVal >= 0) c.volume_24h_usd = volVal;

          const cs = getEffectiveSupply(c);
          if (cs > 0 && isFiniteNum(c.price) && c.price > 0) {
            const currentMcap = c.price * cs;
            c.market_cap_usd = currentMcap;
            if (isFiniteNum(c.volume_24h_usd) && c.volume_24h_usd >= 0 && currentMcap > 0) {
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
            if (isValidFundingIntervalHours(interval)) {
              c.funding_rate_4h_pct = settleVal * (4.0 / Number(interval));
            } else {
              c.funding_rate_4h_pct = null;
            }
          }
        }

        classifyCandidate(c, highTurnoverThreshold);
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
            syncRowDom(c, refs, {
              animatePrice: true,
              prevPrice: oldPrice,
              prevRes,
              prevNegFr,
              flashBudget,
              prefersReducedMotion,
              highTurnoverThreshold,
            });
          }
        }
      });

      lastSyncActivityAt = Date.now();
      if (tickerSyncedEl) {
        const syncedStr = formatBeijingTime(lastSyncActivityAt, { format: 'time' });
        setIfChanged(tickerSyncedEl, 'textContent', `行情: ${syncedStr}`);
        setIfChanged(tickerSyncedEl, 'title', `币安行情最后同步时间: ${syncedStr} (北京时间)`);
      }
      updateSyncStatusBadge('ok', '币安 60s 轮询');

      updateKpiCards(candidatesState);
      if (sortColumn && sortMetricChanged) applySortOrder();
      if (anyClassChanged) applyFilter();
      if (manual) {
        showToast(`已同步最新行情 (${candidatesState.length} 标的)`);
      }
    } catch (e) {
      if (e.name !== 'AbortError') {
        console.warn('Binance pool sync error:', e);
        updateSyncStatusBadge('warn', '币安轮询异常');
        if (manual) {
          showToast(`行情刷新失败: ${e.message || '网络异常'}`);
        }
      }
    } finally {
      if (fetchId === activeFetchId) {
        setButtonBusy(syncBtn, false);
        if (pollingActive) pollScheduler.schedule();
      }
    }
  }

  async function loadCandidateSnapshot({ force = false, manual = false } = {}) {
    if (snapshotController) snapshotController.abort();
    const controller = new AbortController();
    snapshotController = controller;
    const fetchId = ++activeSnapshotFetchId;
    const headers = {};
    if (!force && lastEtag) headers['If-None-Match'] = lastEtag;

    const existingRetryBtn = typeof document !== 'undefined' && typeof document.getElementById === 'function'
      ? document.getElementById('btn-retry-candidates')
      : null;
    const retryHadFocus = Boolean(existingRetryBtn && typeof document !== 'undefined' && document.activeElement === existingRetryBtn);
    if (existingRetryBtn) {
      existingRetryBtn.disabled = true;
      existingRetryBtn.setAttribute('aria-busy', 'true');
      existingRetryBtn.textContent = '正在重试...';
    }
    setButtonBusy(syncBtn, true);
    let handedOffToSync = false;

    try {
      const [, { notModified, etag, data: payload }] = await Promise.all([
        ensureConfig(),
        fetchJsonWithTimeout(
          'api/candidates',
          { cache: 'no-store', headers, signal: controller.signal },
          12000
        ),
      ]);
      if (fetchId !== activeSnapshotFetchId) return false;
      resolveHighTurnoverThreshold();
      if (notModified) return false;
      if (etag) lastEtag = etag;
      if (!payload || !Array.isArray(payload.candidates) || typeof payload.updated_at !== 'string' || !payload.updated_at.trim()) {
        throw new Error('Invalid /api/candidates payload');
      }

      currentVersion = payload.updated_at;
      lastSyncActivityAt = Date.now();
      if (poolUpdatedEl) {
        const fullBeijing = formatUpdatedAtBeijing(currentVersion);
        const shortUpdated = fullBeijing.replace(/^\d{4}-/, '');
        setIfChanged(poolUpdatedEl, 'textContent', `底池: ${shortUpdated}`);
        setIfChanged(poolUpdatedEl, 'title', `底池扫描时间: ${fullBeijing}`);
        setIfChanged(poolUpdatedEl, 'className', 'px-2 py-0.5 font-sans tabular-nums text-[#9fb0c5] bg-[#10151d] inline-flex items-center flex-1 sm:flex-none');
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
        if (copy.funding_interval_hours !== undefined && copy.funding_interval_hours !== null && !isValidFundingIntervalHours(copy.funding_interval_hours)) {
          console.warn(`Invalid funding_interval_hours for ${copy.symbol}:`, copy.funding_interval_hours);
          copy.funding_interval_hours = null;
          copy.funding_rate_4h_pct = null;
        }
        classifyCandidate(copy, highTurnoverThreshold);
        if (isFiniteNum(copy.price)) prevPriceBySym.set(copy.symbol, copy.price);
        return copy;
      });

      renderTableRows(tableBody, candidatesState, rowRefsBySym, highTurnoverThreshold);
      updateKpiCards(candidatesState);
      applyFilter();
      if (sortColumn) applySortOrder();
      updateTableScrollMaxHeight();
      if (pollingActive) versionScheduler.schedule();
      if (candidatesState.length > 0) {
        handedOffToSync = true;
        syncBinancePool({ manual });
      } else {
        updateSyncStatusBadge('ok', '币安 60s 轮询');
        if (pollingActive) pollScheduler.schedule();
        if (manual) {
          showToast('当前候选底池暂无符合策略标的');
        }
      }
      return true;
    } catch (err) {
      if (err && err.name === 'AbortError') return false;
      if (fetchId !== activeSnapshotFetchId) return false;
      console.error('Failed to load candidate pool:', err);
      if (candidatesState.length > 0) {
        updateSyncStatusBadge('warn', '底池更新异常');
        showToast('底池快照更新失败，已保留当前数据');
      } else {
        if (poolUpdatedEl) {
          poolUpdatedEl.textContent = '候选池加载失败';
          poolUpdatedEl.className = 'px-2 py-0.5 font-sans text-[#f8717e] bg-[#f25565]/10 inline-flex items-center flex-1 sm:flex-none';
        }
        if (tableBody) {
          tableBody.innerHTML = `<tr><td colspan="11" class="py-14 text-center font-sans text-xs"><div class="text-[#f8717e]">候选币池读取失败 (${escapeHtml(err.message)})，请检查后台 API 服务是否在线。</div><button type="button" id="btn-retry-candidates" class="retry-candidates-btn px-3 py-1.5 bg-[#151c27] hover:bg-[#1d2736] text-[#f1f5f9] rounded-[4px] border border-[#324055] text-xs font-medium inline-flex items-center transition-colors">立即重试</button></td></tr>`;
          if (retryHadFocus) {
            const nextRetryBtn = typeof document !== 'undefined' && typeof document.getElementById === 'function'
              ? document.getElementById('btn-retry-candidates')
              : null;
            focusSafely(nextRetryBtn);
          }
        }
      }
      return false;
    } finally {
      if (snapshotController === controller) {
        snapshotController = null;
      }
      if (fetchId === activeSnapshotFetchId && !handedOffToSync) {
        setButtonBusy(syncBtn, false);
      }
    }
  }

  function checkSnapshotVersion() {
    if (document.hidden) return Promise.resolve(false);
    if (versionCheckInFlight) return versionCheckInFlight;
    const controller = new AbortController();
    versionController = controller;

    versionCheckInFlight = (async () => {
      try {
        const { data } = await fetchJsonWithTimeout(
          'api/candidate-version',
          { cache: 'no-store', signal: controller.signal },
          8000
        );
        if (!data || typeof data.updated_at !== 'string' || !data.updated_at.trim()) {
          throw new Error('Invalid /api/candidate-version payload');
        }
        if (data.updated_at !== currentVersion) {
          return await loadCandidateSnapshot({ force: true });
        }
        return false;
      } catch (err) {
        if (err && err.name === 'AbortError') return false;
        console.warn('Candidate version check failed:', err);
        if (candidatesState.length > 0) {
          updateSyncStatusBadge('warn', '版本检查异常');
        }
        return false;
      } finally {
        if (versionController === controller) {
          versionController = null;
        }
        versionCheckInFlight = null;
      }
    })();
    return versionCheckInFlight;
  }

  function startPolling() {
    stopPolling();
    pollingActive = true;
    pollScheduler.schedule();
    versionScheduler.schedule();
  }

  function stopPolling() {
    pollingActive = false;
    pollScheduler.clear();
    versionScheduler.clear();
  }

  function triggerHeaderSort(th) {
    if (!th) return;
    const col = th.getAttribute('data-sort');
    if (!col) return;
    const defaultAsc = col === 'fr';
    if (sortColumn !== col) {
      sortColumn = col;
      sortAsc = defaultAsc;
    } else if (sortAsc === defaultAsc) {
      sortAsc = !defaultAsc;
    } else {
      sortColumn = null;
      sortAsc = true;
    }
    syncSortAria();
    saveUiState();
    applySortOrder();
    if (sortStatusEl) {
      const label = th.textContent ? th.textContent.trim() : col;
      const visibleCount = getVisibleCandidates().length;
      if (sortColumn === null) {
        sortStatusEl.textContent = `已恢复 ${label} 默认排序，当前显示 ${visibleCount} 个标的`;
      } else {
        sortStatusEl.textContent = `已按 ${label} ${sortAsc ? '升序' : '降序'}排列，当前显示 ${visibleCount} 个标的`;
      }
    }
  }

  const debouncedApplyFilter = debounce(() => {
    applyFilter();
  }, 120);

  function resetFiltersToDefault() {
    debouncedApplyFilter.cancel();
    if (searchInput) searchInput.value = '';
    searchQuery = '';
    sortColumn = null;
    sortAsc = true;
    syncSortAria();
    applySortOrder();
    switchZone('resonance');
    if (sortStatusEl) {
      const visibleCount = getVisibleCandidates().length;
      sortStatusEl.textContent = `已重置为默认共振区筛选，当前显示 ${visibleCount} 个标的`;
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
    resetBtn.addEventListener('click', resetFiltersToDefault);
  }

  if (searchInput) {
    searchInput.addEventListener('input', (e) => {
      searchQuery = e.target.value;
      saveUiState();
      debouncedApplyFilter();
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
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      showToast(`已导出 ${symbols.length} 个标的 .txt 自选文件`);
    });
  }

  const supportsInvokerCommands = typeof HTMLButtonElement !== 'undefined' && 'commandForElement' in HTMLButtonElement.prototype;
  const sortRoot = (
    candidatesTable &&
    typeof candidatesTable.querySelectorAll === 'function' &&
    (supportsInvokerCommands || candidatesTable.querySelectorAll('th.sortable').length > 0)
  )
    ? candidatesTable
    : document;
  sortBinding = bindSortButtons(sortRoot, triggerHeaderSort);

  function openCandidateChartModal(sym, tvSym, triggerEl) {
    if (!KlineModal) return;
    const visible = getVisibleCandidates();
    const idx = visible.findIndex((c) => c.symbol === sym);
    if (typeof KlineModal.setNavMeta === 'function') {
      KlineModal.setNavMeta(idx, visible.length);
    }
    KlineModal.open(sym, tvSym, triggerEl);
  }

  function navigateModalSymbol(delta) {
    if (!KlineModal || typeof KlineModal.getCurrentSymbol !== 'function') return;
    const visible = getVisibleCandidates();
    if (visible.length <= 1) return;
    const currentSym = KlineModal.getCurrentSymbol();
    const curIdx = visible.findIndex((c) => c.symbol === currentSym);
    const nextIdx = curIdx === -1 ? 0 : (curIdx + delta + visible.length) % visible.length;
    const nextItem = visible[nextIdx];
    openCandidateChartModal(nextItem.symbol, getTvSymbol(nextItem.symbol));
  }

  if (tableBody) {
    tableBody.addEventListener('click', async (e) => {
      const retryBtn = e.target.closest('#btn-retry-candidates');
      if (retryBtn) {
        e.stopPropagation();
        loadCandidateSnapshot({ force: true, manual: true });
        return;
      }

      const emptyResetBtn = e.target.closest('#btn-empty-reset-filter');
      if (emptyResetBtn) {
        e.stopPropagation();
        resetFiltersToDefault();
        return;
      }

      const previewBtn = e.target.closest('.btn-preview-kline');
      if (previewBtn) {
        e.stopPropagation();
        const sym = previewBtn.getAttribute('data-sym');
        const tvSym = previewBtn.getAttribute('data-tv-sym');
        if (sym && tvSym) {
          openCandidateChartModal(sym, tvSym, previewBtn);
        }
        return;
      }

      const copyBtn = e.target.closest('.btn-copy-sym');
      if (copyBtn) {
        e.stopPropagation();
        const tvSym = copyBtn.getAttribute('data-copy');
        if (!tvSym) return;
        const label = copyBtn.getAttribute('data-label') || '复制';
        const ok = await copyText(tvSym, `已复制: ${tvSym}`);
        if (ok) {
          const prevTimer = copyResetTimers.get(copyBtn);
          if (prevTimer) clearTimeout(prevTimer);
          copyBtn.innerHTML = CHECK_SVG;
          copyBtn.classList.add('text-[#38d99f]');
          const timerId = setTimeout(() => {
            copyBtn.textContent = label;
            copyBtn.classList.remove('text-[#38d99f]');
            copyResetTimers.delete(copyBtn);
          }, 1200);
          copyResetTimers.set(copyBtn, timerId);
        }
        return;
      }

      if (e.target.closest('a, button')) return;
      const row = e.target.closest('tr.candidate-row');
      if (row) {
        const sym = row.getAttribute('data-sym');
        const tvSym = row.getAttribute('data-tv-sym');
        if (sym && tvSym) {
          const triggerEl = row.querySelector('.btn-preview-kline');
          openCandidateChartModal(sym, tvSym, triggerEl);
        }
      }
    });
  }

  function updateTableScrollMaxHeight() {
    if (!tableScrollEl || !tableScrollEl.style) return;
    const vw = typeof window.innerWidth === 'number' ? window.innerWidth : 0;
    const vh = typeof window.innerHeight === 'number' ? window.innerHeight : 0;
    if (vw < 1024 || vh <= 0 || typeof tableScrollEl.getBoundingClientRect !== 'function') {
      if (tableScrollEl.style.maxHeight !== '') tableScrollEl.style.maxHeight = '';
      return;
    }
    const rect = tableScrollEl.getBoundingClientRect();
    const available = Math.floor(vh - rect.top - 44);
    const nextMax = `${Math.max(320, available)}px`;
    if (tableScrollEl.style.maxHeight !== nextMax) {
      tableScrollEl.style.maxHeight = nextMax;
    }
  }

  function scheduleTableScrollResize() {
    if (typeof requestAnimationFrame !== 'function') {
      updateTableScrollMaxHeight();
      return;
    }
    if (resizeRafId !== null) return;
    resizeRafId = requestAnimationFrame(() => {
      resizeRafId = null;
      updateTableScrollMaxHeight();
    });
  }

  function setDrawerOpen(open, { restoreFocus = false } = {}) {
    if (!tvDrawer) return;
    const wasOpen = !tvDrawer.classList.contains('hidden');
    tvDrawer.classList.toggle('hidden', !open);
    if (toggleDrawerBtn) {
      toggleDrawerBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    }
    if (!open && wasOpen && restoreFocus) {
      focusSafely(toggleDrawerBtn);
    }
    updateTableScrollMaxHeight();
  }

  if (toggleDrawerBtn && tvDrawer) {
    toggleDrawerBtn.addEventListener('click', () => {
      setDrawerOpen(tvDrawer.classList.contains('hidden'));
    });
  }
  if (closeDrawerBtn && tvDrawer) {
    closeDrawerBtn.addEventListener('click', () => setDrawerOpen(false, { restoreFocus: true }));
  }

  document.addEventListener('keydown', (e) => {
    const activeEl = document.activeElement;
    const activeTag = activeEl ? activeEl.tagName : '';
    const isEditableActive = Boolean(
      activeEl && (
        activeTag === 'INPUT' ||
        activeTag === 'TEXTAREA' ||
        activeTag === 'SELECT' ||
        activeEl.isContentEditable
      )
    );
    const hasModifier = Boolean(e.ctrlKey || e.metaKey || e.altKey);

    if (KlineModal && KlineModal.isOpen()) {
      return;
    }

    if (e.key === 'Escape') {
      if (tvDrawer && !tvDrawer.classList.contains('hidden')) {
        setDrawerOpen(false, { restoreFocus: true });
      } else if (searchInput && (searchInput.value || document.activeElement === searchInput)) {
        debouncedApplyFilter.cancel();
        searchInput.value = '';
        searchQuery = '';
        searchInput.blur();
        saveUiState();
        applyFilter();
      } else if (currentZone !== 'resonance' || sortColumn !== null) {
        resetFiltersToDefault();
      }
    } else if (e.key === '/' && !isEditableActive && !hasModifier) {
      e.preventDefault();
      if (searchInput) searchInput.focus();
    } else if (!isEditableActive && !hasModifier) {
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

  function pauseBackgroundWork() {
    stopPolling();
    stopCountdown();
    debouncedApplyFilter.cancel();
    if (toastTimer !== null) {
      clearTimeout(toastTimer);
      toastTimer = null;
    }
    if (resizeRafId !== null && typeof cancelAnimationFrame === 'function') {
      cancelAnimationFrame(resizeRafId);
      resizeRafId = null;
    }
    activeFetchId += 1;
    if (pollController) {
      pollController.abort();
      pollController = null;
    }
    if (snapshotController) {
      snapshotController.abort();
      snapshotController = null;
    }
    if (versionController) {
      versionController.abort();
      versionController = null;
    }
    setButtonBusy(syncBtn, false);
  }

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      pauseBackgroundWork();
    } else {
      startCountdown();
      pollingActive = true;
      const now = Date.now();
      if (now - lastSyncActivityAt >= VISIBILITY_COOLDOWN_MS) {
        lastSyncActivityAt = now;
        checkSnapshotVersion().then((reloaded) => {
          if (!reloaded && !document.hidden) syncBinancePool();
        });
        versionScheduler.schedule();
      } else {
        pollScheduler.schedule();
        versionScheduler.schedule();
      }
    }
  });

  if (typeof window.addEventListener === 'function') {
    window.addEventListener('pagehide', pauseBackgroundWork);
    window.addEventListener('pageshow', (event) => {
      if (event && event.persisted && !document.hidden) {
        startCountdown();
        startPolling();
      }
    });
  }

  if (syncBtn) {
    syncBtn.addEventListener('click', () => {
      syncBinancePool({ manual: true });
    });
  }

  window.addEventListener('resize', scheduleTableScrollResize);

  restoreUiState();
  kpiCards.forEach((card) => {
    const isSelected = card.getAttribute('data-zone') === currentZone;
    card.classList.toggle('active', isSelected);
    card.setAttribute('aria-pressed', isSelected ? 'true' : 'false');
  });
  if (searchInput && searchQuery) searchInput.value = searchQuery;
  syncSortAria();
  updateTableScrollMaxHeight();
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
    startPolling();
  } else {
    updateSettleCountdown();
  }
  loadCandidateSnapshot({ force: true });
})();
