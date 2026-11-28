(function () {
  const Common = window.SecondLegCommon;
  const Tables = window.SecondLegAccountTables;
  const Charts = window.SecondLegAccountCharts;
  const KlineModal = window.SecondLegKlineModal || null;

  const {
    escapeHtml,
    ensureConfig,
    fetchJsonWithTimeout,
    formatBeijingTime,
    focusSafely,
    setButtonBusy,
    updateStatusBadge,
    createPollScheduler,
  } = Common;

  const debounce = Common.debounce;
  const bindSortButtons = Common.bindSortButtons;

  const {
    setSignedColorClass,
    setText: rawSetText,
    updateKpis: renderKpis,
    renderActivePositions: renderPositionsTable,
    prepareHistorySearchKeys,
    renderHistoryTable: renderHistoryRows,
    renderApiStatusBadge,
    formatLoadErrorMessage,
    getAccountModalCollection,
    openAccountKlineModal: openModalWithContext,
  } = Tables;

  const {
    ensureChartDefaults,
    destroyAllCharts,
    downsampleEquityCurve,
    upsertChart,
    updateCharts: renderCharts,
  } = Charts;

  const HISTORY_SORT = { column: 'closed_ts', asc: false };
  const VISIBILITY_COOLDOWN_MS = 5000;
  const POLL_INTERVAL_MS = 30000;

  const domCache = new Map();
  let currentAccountData = null;
  let latestSnapshot = '';
  let lastEtag = null;
  let accountController = null;
  let accountKeydownBound = false;
  let modalCollectionKind = null;
  let modalCollectionIdx = -1;
  let activeLoadId = 0;
  let lastSuccessfulLoadAt = 0;
  let historySortBinding = null;
  let historyCurrentPage = 1;
  const debouncedHistorySearch = debounce(() => {
    historyCurrentPage = 1;
    renderHistoryTable();
  }, 120);
  const apiBadgeCache = { className: '', html: '' };
  let lastLoadStatusSignature = '';

  function getCachedEl(id) {
    const cached = domCache.get(id);
    if (cached && cached.isConnected !== false) {
      return cached;
    }
    const el = document.getElementById(id);
    if (el && el.isConnected !== false) {
      domCache.set(id, el);
    }
    return el;
  }

  function setText(id, text) {
    return rawSetText(id, text, getCachedEl);
  }

  function updateKpis(summary) {
    return renderKpis(summary, getCachedEl);
  }

  function renderActivePositions(positions) {
    return renderPositionsTable(positions, getCachedEl);
  }

  function updateSortIndicators() {
    if (historySortBinding) {
      historySortBinding.syncAria(HISTORY_SORT.column, HISTORY_SORT.asc);
    }
  }

  function renderHistoryTable() {
    if (!currentAccountData) return;
    historyCurrentPage = renderHistoryRows(currentAccountData, HISTORY_SORT, historyCurrentPage, {
      getEl: getCachedEl,
      updateSortIndicators,
    });
  }

  function updateCharts(accountData) {
    return renderCharts(accountData, {
      setText,
      setSignedColorClass,
      getAccountData: () => currentAccountData,
    });
  }

  function updateApiStatusBadge(accountData, fetchError) {
    renderApiStatusBadge(getCachedEl('api-status-badge'), apiBadgeCache, accountData, fetchError);
  }

  function renderDashboard(accountData) {
    if (
      !accountData ||
      typeof accountData !== 'object' ||
      !accountData.api_status ||
      typeof accountData.api_status !== 'object' ||
      typeof accountData.api_status.mode !== 'string' ||
      !accountData.api_status.mode.trim() ||
      !accountData.summary ||
      typeof accountData.summary !== 'object' ||
      !Array.isArray(accountData.active_positions) ||
      !Array.isArray(accountData.trade_history) ||
      !Array.isArray(accountData.equity_curve) ||
      !Array.isArray(accountData.daily_pnl) ||
      !accountData.pnl_distribution ||
      typeof accountData.pnl_distribution !== 'object' ||
      !Array.isArray(accountData.pnl_distribution.bins)
    ) {
      throw new Error('Invalid /api/account payload');
    }
    const prevAccountData = currentAccountData;
    currentAccountData = accountData;

    try {
      const summary = accountData.summary;
      const positions = accountData.active_positions;
      prepareHistorySearchKeys(accountData.trade_history);

      const formattedSyncTime = formatBeijingTime(accountData.generated_at);
      const syncTimeEl = setText('sync-time-text', formattedSyncTime);
      if (syncTimeEl) {
        syncTimeEl.title = formattedSyncTime === '--' ? '' : ('快照生成时间: ' + formattedSyncTime + ' (北京时间)');
      }
      updateApiStatusBadge(accountData, false);
      updateKpis(summary);
      renderActivePositions(positions);
      renderHistoryTable();
      updateCharts(accountData);
    } catch (err) {
      currentAccountData = prevAccountData;
      throw err;
    }
  }

  function triggerHistorySort(th) {
    if (!th) return;
    const col = th.dataset ? th.dataset.sort : th.getAttribute('data-sort');
    if (!col) return;
    if (HISTORY_SORT.column === col) {
      HISTORY_SORT.asc = !HISTORY_SORT.asc;
    } else {
      HISTORY_SORT.column = col;
      HISTORY_SORT.asc = col === 'symbol';
    }
    historyCurrentPage = 1;
    renderHistoryTable();
    const sortStatus = getCachedEl('history-sort-status');
    if (sortStatus && currentAccountData) {
      const label = th.textContent ? th.textContent.trim() : col;
      const msg = '历史流水已按 ' + label + (HISTORY_SORT.asc ? ' 升序' : ' 降序') + '排列';
      if (sortStatus.textContent !== msg) {
        sortStatus.textContent = msg;
      }
    }
  }

  function setRefreshButtonBusy(busy) {
    setButtonBusy(getCachedEl('btn-manual-refresh'), busy, getCachedEl('refresh-spinner'));
  }

  function openAccountKlineModal(kind, idx, triggerEl) {
    const opened = openModalWithContext(KlineModal, currentAccountData, kind, idx, triggerEl);
    if (opened) {
      modalCollectionKind = opened.kind;
      modalCollectionIdx = opened.idx;
    }
  }

  function navigateAccountModalSymbol(delta) {
    const list = getAccountModalCollection(modalCollectionKind, currentAccountData);
    if (list.length <= 1) return;
    const curIdx = (modalCollectionIdx >= 0 && modalCollectionIdx < list.length) ? modalCollectionIdx : 0;
    openAccountKlineModal(modalCollectionKind, (curIdx + delta + list.length) % list.length, null);
  }

  function bindControlsOnce() {
    const searchInput = getCachedEl('history-search-input');
    if (searchInput && !searchInput.dataset.bound) {
      searchInput.dataset.bound = 'true';
      searchInput.addEventListener('input', debouncedHistorySearch);
    }

    const sideSelect = getCachedEl('history-side-filter');
    const outcomeSelect = getCachedEl('history-outcome-filter');
    [sideSelect, outcomeSelect].forEach((sel) => {
      if (sel && !sel.dataset.bound) {
        sel.dataset.bound = 'true';
        sel.addEventListener('change', () => {
          historyCurrentPage = 1;
          renderHistoryTable();
        });
      }
    });

    const resetBtn = getCachedEl('btn-reset-history-filter');
    if (resetBtn && !resetBtn.dataset.bound) {
      resetBtn.dataset.bound = 'true';
      resetBtn.addEventListener('click', () => {
        debouncedHistorySearch.cancel();
        if (searchInput) searchInput.value = '';
        if (sideSelect) sideSelect.value = '';
        if (outcomeSelect) outcomeSelect.value = '';
        HISTORY_SORT.column = 'closed_ts';
        HISTORY_SORT.asc = false;
        historyCurrentPage = 1;
        renderHistoryTable();
        const sortStatus = getCachedEl('history-sort-status');
        if (sortStatus && currentAccountData) sortStatus.textContent = '已重置筛选与排序，恢复按平仓时间降序排列';
      });
    }

    [['active-positions-tbody', 'position'], ['history-tbody', 'trade']].forEach(([tbodyId, kind]) => {
      const tbody = getCachedEl(tbodyId);
      if (tbody && !tbody.dataset.bound) {
        tbody.dataset.bound = 'true';
        tbody.addEventListener('click', (e) => {
          const btn = e && e.target && typeof e.target.closest === 'function' ? e.target.closest('button[data-kline-kind]') : null;
          const idx = btn ? Number.parseInt(btn.getAttribute('data-idx'), 10) : NaN;
          if (Number.isInteger(idx)) openAccountKlineModal(kind, idx, btn);
        });
      }
    });

    if (typeof document !== 'undefined' && typeof document.addEventListener === 'function' && !accountKeydownBound) {
      accountKeydownBound = true;
      document.addEventListener('keydown', (e) => {
        if (KlineModal && typeof KlineModal.isOpen === 'function' && KlineModal.isOpen()) return;
        const activeEl = document.activeElement;
        const activeTag = activeEl ? activeEl.tagName : '';
        const isEditable = Boolean(activeEl && (activeTag === 'INPUT' || activeTag === 'TEXTAREA' || activeTag === 'SELECT' || activeEl.isContentEditable));
        const input = getCachedEl('history-search-input');
        if (e.key === '/' && !isEditable && !e.ctrlKey && !e.metaKey && !e.altKey && input) {
          e.preventDefault();
          focusSafely(input);
        } else if (e.key === 'Escape' && input && (input.value || activeEl === input)) {
          debouncedHistorySearch.cancel();
          input.value = '';
          if (typeof input.blur === 'function') input.blur();
          historyCurrentPage = 1;
          renderHistoryTable();
        }
      });
    }

    const prevPageBtn = getCachedEl('btn-history-prev-page');
    if (prevPageBtn && !prevPageBtn.dataset.bound) {
      prevPageBtn.dataset.bound = 'true';
      prevPageBtn.addEventListener('click', function () {
        if (historyCurrentPage > 1) {
          historyCurrentPage -= 1;
          renderHistoryTable();
        }
      });
    }

    const nextPageBtn = getCachedEl('btn-history-next-page');
    if (nextPageBtn && !nextPageBtn.dataset.bound) {
      nextPageBtn.dataset.bound = 'true';
      nextPageBtn.addEventListener('click', function () {
        historyCurrentPage += 1;
        renderHistoryTable();
      });
    }

    const histTable = getCachedEl('trade-history-table');
    historySortBinding = bindSortButtons(histTable, triggerHistorySort);

    const refreshBtn = getCachedEl('btn-manual-refresh');
    if (refreshBtn && !refreshBtn.dataset.bound) {
      refreshBtn.dataset.bound = 'true';
      refreshBtn.addEventListener('click', function () {
        if (refreshBtn.disabled) return;
        setRefreshButtonBusy(true);
        const myLoadId = activeLoadId + 1;
        tryLoadData().finally(() => {
          if (myLoadId !== activeLoadId && accountController !== null) return;
          setRefreshButtonBusy(false);
        });
      });
    }

    const loadStatusEl = getCachedEl('account-load-status');
    if (loadStatusEl && !loadStatusEl.dataset.bound) {
      loadStatusEl.dataset.bound = 'true';
      loadStatusEl.addEventListener('click', function (e) {
        const retryBtn = e && e.target && typeof e.target.closest === 'function'
          ? e.target.closest('#btn-retry-account')
          : null;
        if (!retryBtn || retryBtn.disabled) return;
        retryBtn.disabled = true;
        retryBtn.setAttribute('aria-busy', 'true');
        retryBtn.textContent = '正在重试...';
        const hadFocus = typeof document !== 'undefined' && document.activeElement === retryBtn;
        tryLoadData().finally(() => {
          const currentRetryBtn = document.getElementById('btn-retry-account');
          if (currentRetryBtn && loadStatusEl && !loadStatusEl.hidden) {
            currentRetryBtn.disabled = false;
            if (typeof currentRetryBtn.removeAttribute === 'function') {
              currentRetryBtn.removeAttribute('aria-busy');
            }
            currentRetryBtn.textContent = '立即重试';
            if (hadFocus) {
              focusSafely(currentRetryBtn);
            }
          }
        });
      });
    }
  }

  const pollScheduler = createPollScheduler(() => {
    tryLoadData();
  }, POLL_INTERVAL_MS);

  function showLoadErrorStatus(error) {
    const status = getCachedEl('account-load-status');
    if (!status) return;
    const message = formatLoadErrorMessage(error, Boolean(latestSnapshot));
    const signature = (latestSnapshot ? 'stale' : 'initial') + '|' + message;
    if (lastLoadStatusSignature === signature && status.hidden === false) {
      return;
    }
    lastLoadStatusSignature = signature;
    status.hidden = false;
    status.className = 'ledger-frame px-4 py-3 text-xs bg-[#f25565]/10 border-[#f25565]/30 text-[#f8717e] flex flex-wrap items-center justify-between gap-2 font-sans';
    status.innerHTML = '<span>' + escapeHtml(message) + '</span>' +
      '<button id="btn-retry-account" type="button" class="px-2.5 py-1 text-xs font-medium rounded-[4px] bg-[#151c27] hover:bg-[#1d2736] text-[#f1f5f9] border border-[#283446] hover:border-[#3b4d66] transition-colors shrink-0">立即重试</button>';
  }

  function hideLoadStatus() {
    const status = getCachedEl('account-load-status');
    lastLoadStatusSignature = '';
    if (status) status.hidden = true;
  }

  async function tryLoadData() {
    pollScheduler.clear();
    if (accountController) accountController.abort();
    const controller = new AbortController();
    accountController = controller;
    const loadId = ++activeLoadId;
    const report = getCachedEl('account-report');
    try {
      const headers = {};
      if (lastEtag) headers['If-None-Match'] = lastEtag;
      const [, { notModified, etag, data, text: snapshot }] = await Promise.all([
        ensureConfig(),
        fetchJsonWithTimeout(
          'api/account',
          { cache: 'no-store', headers, signal: controller.signal },
          12000
        ),
      ]);
      if (loadId !== activeLoadId) return;
      lastSuccessfulLoadAt = Date.now();
      if (notModified) {
        if (currentAccountData) updateApiStatusBadge(currentAccountData, false);
        hideLoadStatus();
        return;
      }
      if (etag) lastEtag = etag;
      if (snapshot !== latestSnapshot) {
        renderDashboard(data);
        if (report) report.hidden = false;
        latestSnapshot = snapshot;
      } else if (currentAccountData) {
        updateApiStatusBadge(currentAccountData, false);
      }
      hideLoadStatus();
    } catch (error) {
      if (error && error.name === 'AbortError') return;
      if (loadId !== activeLoadId) return;
      console.error('Failed to load account snapshot:', error);
      if (report) report.hidden = !latestSnapshot;
      updateApiStatusBadge(currentAccountData, true);
      showLoadErrorStatus(error);
    } finally {
      if (accountController === controller) {
        accountController = null;
      }
      if (loadId === activeLoadId) {
        setRefreshButtonBusy(false);
        if (!document.hidden) {
          pollScheduler.schedule();
        }
      }
    }
  }

  function init() {
    ensureChartDefaults();
    if (KlineModal && typeof KlineModal.init === 'function') {
      KlineModal.init({
        onNavigateSymbol: navigateAccountModalSymbol,
      });
    }
    bindControlsOnce();
    tryLoadData();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      pollScheduler.clear();
      if (accountController) {
        accountController.abort();
        accountController = null;
      }
      activeLoadId += 1;
      setRefreshButtonBusy(false);
      return;
    }
    if (Date.now() - lastSuccessfulLoadAt < VISIBILITY_COOLDOWN_MS) {
      pollScheduler.schedule();
      return;
    }
    tryLoadData();
  });

  if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
    window.addEventListener('pagehide', () => {
      pollScheduler.clear();
      debouncedHistorySearch.cancel();
      if (accountController) {
        accountController.abort();
        accountController = null;
      }
      activeLoadId += 1;
      setRefreshButtonBusy(false);
      destroyAllCharts();
    });

    window.addEventListener('pageshow', (event) => {
      if (event && event.persisted) {
        if (currentAccountData) {
          updateCharts(currentAccountData);
        }
        if (!document.hidden) {
          pollScheduler.schedule();
        }
      }
    });
  }
})();
