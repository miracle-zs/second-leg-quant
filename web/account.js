(function() {
  const Common = window.SecondLegCommon;
  const {
    escapeHtml,
    isFiniteNum: isFiniteNumber,
    formatCryptoPrice: commonFormatCryptoPrice,
    formatSignedPct,
    getTvUrl,
    ensureConfig,
    fetchJsonWithTimeout,
  } = Common;

  const EXT_ARROW_SVG = '<svg class="w-2.5 h-2.5 text-[#7f91a8] shrink-0" viewBox="0 0 12 12" fill="none" aria-hidden="true"><path d="M3.5 8.5L8.5 3.5M8.5 3.5H4.5M8.5 3.5V7.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  const symbolCollator = typeof Intl !== 'undefined' && typeof Intl.Collator === 'function'
    ? new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' })
    : null;

  const chartInstances = {};
  const HISTORY_SORT = { column: 'closed_ts', asc: false };
  const VISIBILITY_COOLDOWN_MS = 5000;
  let currentAccountData = null;
  let latestSnapshot = '';
  let lastEtag = null;
  let accountController = null;
  let activeLoadId = 0;
  let lastSuccessfulLoadAt = 0;
  let chartDefaultsConfigured = false;
  let historySearchTimer = null;
  let cachedEqGradient = null;
  let cachedEqGradientTop = null;
  let cachedEqGradientHeight = null;
  let cachedEqGradientCtx = null;

  function formatMoney(val, options) {
    const opts = options || {};
    if (!isFiniteNumber(val)) return '--';
    const num = Number(val);
    const decimals = opts.decimals !== undefined ? opts.decimals : 2;
    const absStr = Math.abs(num).toLocaleString('en-US', {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals
    });
    if (num < 0) return '-$' + absStr;
    if (opts.signed && num > 0) return '+$' + absStr;
    return '$' + absStr;
  }

  function formatAxisUsd(v) {
    const num = Number(v);
    if (!Number.isFinite(num)) return '';
    return (num < 0 ? '-$' : '$') + Math.abs(num).toLocaleString('en-US');
  }

  function formatCryptoPrice(val) {
    return commonFormatCryptoPrice(val, {
      positiveOnly: true,
      useGrouping: true,
      dec4Threshold: 1,
    });
  }

  function formatDollarPrice(val) {
    const formatted = formatCryptoPrice(val);
    return formatted === '--' ? '--' : ('$' + formatted);
  }

  function formatRatio(val, decimals) {
    if (!isFiniteNumber(val)) return '--';
    return Number(val).toFixed(decimals !== undefined ? decimals : 2);
  }

  function setSignedColorClass(el, val) {
    if (!el) return;
    el.classList.remove(
      'text-[#38d99f]',
      'text-[#f8717e]',
      'text-[#9fb0c5]',
      'text-[#f1f5f9]',
      'text-emerald-400',
      'text-rose-400',
      'text-slate-400',
      'text-slate-300',
      'text-white'
    );
    if (!isFiniteNumber(val)) {
      el.classList.add('text-[#9fb0c5]');
    } else if (Number(val) > 0) {
      el.classList.add('text-[#38d99f]');
    } else if (Number(val) < 0) {
      el.classList.add('text-[#f8717e]');
    } else {
      el.classList.add('text-[#f1f5f9]');
    }
  }

  function setText(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
    return el;
  }

  function updateApiStatusBadge(accountData, fetchError) {
    const badge = document.getElementById('api-status-badge');
    if (!badge) return;

    if (fetchError) {
      badge.className = 'px-2 py-0.5 font-medium bg-[#f25565]/10 text-[#f8717e] inline-flex items-center gap-1.5';
      badge.innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-[#f25565]" aria-hidden="true"></span><span>数据拉取失败</span>';
      return;
    }

    const mode = (accountData && accountData.api_status && accountData.api_status.mode) || '';
    if (mode === 'BINANCE_REALTIME') {
      badge.className = 'px-2 py-0.5 font-medium bg-[#22c58b]/10 text-[#38d99f] inline-flex items-center gap-1.5';
      badge.innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-[#22c58b]" aria-hidden="true"></span><span>币安实盘在线</span>';
    } else if (mode === 'BINANCE_PARTIAL') {
      badge.className = 'px-2 py-0.5 font-medium bg-[#f0b429]/[0.12] text-[#f5c451] inline-flex items-center gap-1.5';
      badge.innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-[#f0b429]" aria-hidden="true"></span><span>币安部分数据可用</span>';
    } else if (mode === 'BINANCE_SNAPSHOT_PENDING_INCOME') {
      badge.className = 'px-2 py-0.5 font-medium bg-[#f0b429]/[0.12] text-[#f5c451] inline-flex items-center gap-1.5';
      badge.innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-[#f0b429]" aria-hidden="true"></span><span>币安快照同步中</span>';
    } else if (mode === 'OFFLINE_FALLBACK') {
      badge.className = 'px-2 py-0.5 font-medium bg-[#151c27] text-[#f5c451] inline-flex items-center gap-1.5';
      badge.innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-[#f0b429]" aria-hidden="true"></span><span>离线风控同步</span>';
    } else {
      badge.className = 'px-2 py-0.5 font-medium bg-[#f25565]/10 text-[#f8717e] inline-flex items-center gap-1.5';
      const label = mode ? ('接口状态异常 (' + mode + ')') : '接口状态缺失';
      badge.innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-[#f25565]" aria-hidden="true"></span><span>' + escapeHtml(label) + '</span>';
    }
  }

  function updateKpis(summary) {
    setText('kpi-total-equity', formatMoney(summary.total_equity));
    setText(
      'kpi-initial-capital',
      '本金: ' + (isFiniteNumber(summary.initial_capital) ? formatMoney(summary.initial_capital, { decimals: 0 }) : '--')
    );

    const retEl = setText('kpi-total-return', formatSignedPct(summary.total_return_pct));
    setSignedColorClass(retEl, summary.total_return_pct);

    setText('kpi-available-margin', formatMoney(summary.available_margin));
    setText('kpi-position-margin', formatMoney(summary.position_margin));
    const marginBadge = document.getElementById('kpi-margin-badge');
    if (marginBadge) {
      if (isFiniteNumber(summary.total_equity) && Number(summary.total_equity) > 0 && isFiniteNumber(summary.position_margin)) {
        const utilPct = (Number(summary.position_margin) / Number(summary.total_equity)) * 100;
        marginBadge.textContent = utilPct > 0 ? ('占用 ' + utilPct.toFixed(1) + '%') : '空仓在岸';
      } else {
        marginBadge.textContent = '--';
      }
    }

    const realizedEl = setText('kpi-net-realized-pnl', formatMoney(summary.net_realized_pnl, { signed: true }));
    setSignedColorClass(realizedEl, summary.net_realized_pnl);
    setText(
      'kpi-total-fee',
      '手续费: ' + (isFiniteNumber(summary.total_fee) ? formatMoney(-Math.abs(Number(summary.total_fee))) : '--')
    );
    setText('kpi-funding-fee', '资费: ' + formatMoney(summary.total_funding_fee, { signed: true }));

    const unrealizedEl = setText('kpi-unrealized-pnl', formatMoney(summary.total_unrealized_pnl, { signed: true }));
    setSignedColorClass(unrealizedEl, summary.total_unrealized_pnl);
    const openCountText = isFiniteNumber(summary.open_trades) ? (Number(summary.open_trades) + ' 笔') : '--';
    setText('kpi-open-trades', openCountText);

    setText('kpi-win-rate', formatSignedPct(summary.win_rate_pct, { signed: false, decimals: 1 }));
    const winCount = isFiniteNumber(summary.win_count) ? Number(summary.win_count) : null;
    const lossCount = isFiniteNumber(summary.loss_count) ? Number(summary.loss_count) : null;
    const beCount = isFiniteNumber(summary.breakeven_count) ? Number(summary.breakeven_count) : null;
    if (winCount === null && lossCount === null && beCount === null) {
      setText('kpi-wl-record', '--');
    } else {
      setText(
        'kpi-wl-record',
        (winCount !== null ? winCount + 'W' : '--') + ' / ' +
        (lossCount !== null ? lossCount + 'L' : '--') + ' / ' +
        (beCount !== null ? beCount + 'BE' : '--')
      );
    }
    setText('kpi-total-trades', isFiniteNumber(summary.closed_trades) ? Number(summary.closed_trades) + ' 笔' : '--');

    let pfText = '--';
    if (isFiniteNumber(summary.profit_factor)) {
      pfText = Number(summary.profit_factor).toFixed(2);
    } else if (winCount !== null && lossCount !== null && winCount > 0 && lossCount === 0) {
      pfText = '全胜';
    }
    setText('kpi-profit-factor', pfText);
    setText('kpi-avg-win', '均盈: ' + formatMoney(summary.avg_win, { signed: true }));
    setText(
      'kpi-avg-loss',
      '均亏: ' + (isFiniteNumber(summary.avg_loss) ? formatMoney(-Math.abs(Number(summary.avg_loss))) : '--')
    );

    const ddPctVal = isFiniteNumber(summary.max_drawdown_pct) ? Math.abs(Number(summary.max_drawdown_pct)) : null;
    const ddPctFixed = ddPctVal === null ? null : ddPctVal.toFixed(2);
    const ddPctEl = setText(
      'kpi-max-dd-pct',
      ddPctFixed === null ? '--' : (ddPctFixed === '0.00' ? '0.00%' : ('-' + ddPctFixed + '%'))
    );
    const ddUsdEl = setText(
      'kpi-max-dd-usd',
      isFiniteNumber(summary.max_drawdown_usd) ? formatMoney(-Math.abs(Number(summary.max_drawdown_usd))) : '--'
    );
    if (ddPctEl) {
      const hasDd = isFiniteNumber(summary.max_drawdown_pct) && Math.abs(Number(summary.max_drawdown_pct)) > 0;
      ddPctEl.classList.toggle('text-[#f8717e]', hasDd);
      ddPctEl.classList.toggle('text-[#f1f5f9]', !hasDd);
    }
    if (ddUsdEl) {
      const hasDdUsd = isFiniteNumber(summary.max_drawdown_usd) && Math.abs(Number(summary.max_drawdown_usd)) > 0;
      ddUsdEl.classList.toggle('text-[#f8717e]', hasDdUsd);
      ddUsdEl.classList.toggle('text-[#9fb0c5]', !hasDdUsd);
    }

    setText('kpi-sharpe', formatRatio(summary.sharpe_ratio, 2));
    const sharpeRating = document.getElementById('kpi-sharpe-rating');
    if (sharpeRating) {
      sharpeRating.classList.remove('text-[#38d99f]', 'text-[#8ab8ff]', 'text-[#f5c451]', 'text-[#f8717e]', 'text-[#9fb0c5]');
      if (!isFiniteNumber(summary.sharpe_ratio)) {
        sharpeRating.textContent = '样本不足';
        sharpeRating.classList.add('text-[#9fb0c5]');
      } else {
        const sr = Number(summary.sharpe_ratio);
        if (sr >= 2.0) {
          sharpeRating.textContent = '优异稳健';
          sharpeRating.classList.add('text-[#38d99f]');
        } else if (sr >= 1.0) {
          sharpeRating.textContent = '表现稳健';
          sharpeRating.classList.add('text-[#8ab8ff]');
        } else if (sr >= 0) {
          sharpeRating.textContent = '收益一般';
          sharpeRating.classList.add('text-[#f5c451]');
        } else {
          sharpeRating.textContent = '负收益警告';
          sharpeRating.classList.add('text-[#f8717e]');
        }
      }
    }

    setText('kpi-sortino', formatRatio(summary.sortino_ratio, 2));
    const sortinoRating = document.getElementById('kpi-sortino-rating');
    if (sortinoRating) {
      sortinoRating.classList.remove('text-[#38d99f]', 'text-[#8ab8ff]', 'text-[#f5c451]', 'text-[#f8717e]', 'text-[#9fb0c5]');
      if (!isFiniteNumber(summary.sortino_ratio)) {
        if (summary.sharpe_method !== 'NONE' && winCount !== null && lossCount !== null && winCount > 0 && lossCount === 0) {
          sortinoRating.textContent = '零下行波动';
          sortinoRating.classList.add('text-[#38d99f]');
        } else {
          sortinoRating.textContent = '样本不足';
          sortinoRating.classList.add('text-[#9fb0c5]');
        }
      } else {
        const so = Number(summary.sortino_ratio);
        if (so >= 2.5) {
          sortinoRating.textContent = '极低尾部风险';
          sortinoRating.classList.add('text-[#38d99f]');
        } else if (so >= 1.0) {
          sortinoRating.textContent = '下行风险可控';
          sortinoRating.classList.add('text-[#8ab8ff]');
        } else if (so >= 0) {
          sortinoRating.textContent = '下行收益一般';
          sortinoRating.classList.add('text-[#f5c451]');
        } else {
          sortinoRating.textContent = '下行风险偏高';
          sortinoRating.classList.add('text-[#f8717e]');
        }
      }
    }

    const maxWins = isFiniteNumber(summary.max_consecutive_wins) ? summary.max_consecutive_wins : '--';
    const maxLosses = isFiniteNumber(summary.max_consecutive_losses) ? summary.max_consecutive_losses : '--';
    setText('kpi-max-streak', maxWins + 'W / ' + maxLosses + 'L');
    setText('kpi-avg-hold', summary.avg_hold_duration_str || '--');
  }

  function renderActivePositions(positions) {
    const posTbody = document.getElementById('active-positions-tbody');
    setText('active-pos-count-badge', positions.length + ' 活跃仓位');

    const posEmpty = document.getElementById('active-positions-empty');
    if (posEmpty) {
      posEmpty.classList.toggle('hidden', positions.length > 0);
      posEmpty.classList.toggle('inline-flex', positions.length === 0);
    }
    const posWrap = document.getElementById('active-positions-table-wrap');
    if (posWrap) posWrap.classList.toggle('hidden', positions.length === 0);

    const shortWarn = document.getElementById('short-position-warning');
    if (shortWarn) {
      const hasUnmonitored = positions.some(p => {
        if (!p || typeof p !== 'object' || typeof p.side !== 'string' || !p.side.trim()) {
          throw new Error('Invalid active position: missing side');
        }
        const side = p.side.trim().toUpperCase();
        return side === 'SHORT' || p.risk_monitored !== true;
      });
      shortWarn.classList.toggle('hidden', !hasUnmonitored);
    }

    if (!posTbody) return;
    if (positions.length === 0) {
      posTbody.innerHTML = '';
      return;
    }

    posTbody.innerHTML = positions.map(p => {
      if (!p || typeof p !== 'object' || typeof p.symbol !== 'string' || !p.symbol.trim() || typeof p.side !== 'string' || !p.side.trim()) {
        throw new Error('Invalid active position: missing symbol or side');
      }
      const sym = p.symbol.trim();
      const safeSym = escapeHtml(sym);
      const side = p.side.trim().toUpperCase();
      const safeSide = escapeHtml(side);
      const riskMonitored = p.risk_monitored === true && side !== 'SHORT';
      const sideBadge = side === 'SHORT'
        ? '<span class="px-1.5 py-0.5 rounded-[3px] text-[10px] font-mono font-semibold bg-[#f25565]/15 text-[#f8717e] border border-[#f25565]/30">SHORT</span>'
        : (side === 'LONG'
          ? '<span class="px-1.5 py-0.5 rounded-[3px] text-[10px] font-mono font-semibold bg-[#22c58b]/[0.12] text-[#38d99f] border border-[#22c58b]/30">LONG</span>'
          : '<span class="px-1.5 py-0.5 rounded-[3px] text-[10px] font-mono font-semibold bg-[#151c27] text-[#9fb0c5] border border-[#283446]">' + safeSide + '</span>');

      const pnlVal = isFiniteNumber(p.unrealized_pnl) ? Number(p.unrealized_pnl) : null;
      const roeVal = isFiniteNumber(p.roe_pct) ? Number(p.roe_pct) : null;
      const pnlCls = pnlVal === null ? 'text-[#9fb0c5]' : (pnlVal > 0 ? 'text-[#38d99f]' : (pnlVal < 0 ? 'text-[#f8717e]' : 'text-[#f1f5f9]'));
      const pnlStr = formatMoney(pnlVal, { signed: true });
      const roeStr = roeVal === null ? '--' : (formatSignedPct(roeVal) + ' ROE');

      const levStr = isFiniteNumber(p.leverage) ? (Number(p.leverage) + 'x') : '--';
      const notionalStr = formatMoney(p.notional);
      const amtStr = isFiniteNumber(p.amount) ? Number(p.amount).toLocaleString('en-US') : '--';

      const tvUrl = getTvUrl(sym);
      const safeTlId = p.trendline_id ? escapeHtml(p.trendline_id) : '--';
      const unknownMonitored = p.risk_monitored === undefined && side !== 'SHORT';
      const trailingHtml = riskMonitored
        ? (p.trailing_stop_triggered ? '<span class="text-[#38d99f] font-semibold">已激活锁利</span>' : '<span class="text-[#9fb0c5]">待触发</span>')
        : (unknownMonitored
          ? '<span class="text-[#f5c451]">风控状态缺失</span>'
          : '<span class="text-[#7f91a8]">--</span>');
      const slHtml = riskMonitored
        ? (p.stop_loss_notified ? '<span class="text-[#f8717e] font-semibold">已推送止损</span>' : '<span class="text-[#38d99f]">巡检监控中</span>')
        : (side === 'SHORT'
          ? '<span class="px-1.5 py-0.5 rounded-[3px] text-[10px] font-sans font-medium bg-[#151c27] text-[#f5c451] border border-[#f0b429]/30">未受风控监控（空单）</span>'
          : (unknownMonitored
            ? '<span class="px-1.5 py-0.5 rounded-[3px] text-[10px] font-sans font-medium bg-[#f25565]/10 text-[#f8717e] border border-[#f25565]/30">风控状态未知</span>'
            : '<span class="px-1.5 py-0.5 rounded-[3px] text-[10px] font-sans font-medium bg-[#151c27] text-[#f5c451] border border-[#f0b429]/30">未纳入风控监控</span>'));

      return (
        '<tr class="hover:bg-[#17202e] transition-colors font-mono tabular-nums text-xs">' +
          '<td class="px-3.5 py-2.5 font-sans font-bold text-[#f1f5f9]">' +
            '<div class="flex items-center gap-1.5">' +
              '<a href="' + escapeHtml(tvUrl) + '" target="_blank" rel="noopener noreferrer" class="text-[#f1f5f9] hover:text-[#8ab8ff] hover:underline inline-flex items-center gap-1">' +
                '<span>' + safeSym + '</span>' + EXT_ARROW_SVG +
              '</a>' +
              sideBadge +
            '</div>' +
          '</td>' +
          '<td class="px-3.5 py-2.5 text-right text-[#f1f5f9]"><div>' + escapeHtml(levStr) + ' · ' + escapeHtml(notionalStr) + '</div><div class="text-[10px] text-[#7f91a8]">' + escapeHtml(amtStr) + '</div></td>' +
          '<td class="px-3.5 py-2.5 text-right text-[#9fb0c5]">' + escapeHtml(formatDollarPrice(p.entry_price)) + '</td>' +
          '<td class="px-3.5 py-2.5 text-right text-[#f1f5f9] font-semibold">' + escapeHtml(formatDollarPrice(p.mark_price)) + '</td>' +
          '<td class="px-3.5 py-2.5 text-right font-bold ' + pnlCls + '"><div>' + escapeHtml(pnlStr) + '</div><div class="text-[10px] font-medium">' + escapeHtml(roeStr) + '</div></td>' +
          '<td class="px-3.5 py-2.5 text-right text-[#f8717e]">' + escapeHtml(formatDollarPrice(p.liquidation_price)) + '</td>' +
          '<td class="px-3.5 py-2.5 font-sans text-xs">' + trailingHtml + '</td>' +
          '<td class="px-3.5 py-2.5 font-sans text-xs">' + slHtml + '</td>' +
          '<td class="px-3.5 py-2.5 text-[#9fb0c5]">' + (riskMonitored ? safeTlId : '--') + '</td>' +
        '</tr>'
      );
    }).join('');
  }

  function getFilteredAndSortedHistory(history) {
    const searchInput = document.getElementById('history-search-input');
    const outcomeSelect = document.getElementById('history-outcome-filter');
    const query = (searchInput ? searchInput.value : '').trim().toLowerCase();
    const outcomeFilter = outcomeSelect ? outcomeSelect.value : '';

    const filtered = history.filter(h => {
      if (outcomeFilter && String(h.outcome || '').toUpperCase() !== outcomeFilter) {
        return false;
      }
      if (!query) return true;
      const haystack = [
        h.symbol || '',
        h.close_reason || '',
        h.trendline_label || '',
        h.trendline_id || '',
        h.side || '',
        h.closed_at || ''
      ].join(' ').toLowerCase();
      return haystack.includes(query);
    });

    const col = HISTORY_SORT.column;
    const asc = HISTORY_SORT.asc;
    filtered.sort((a, b) => {
      if (col === 'net_pnl' || col === 'pnl_pct' || col === 'closed_ts') {
        const aOk = isFiniteNumber(a[col]);
        const bOk = isFiniteNumber(b[col]);
        if (!aOk && !bOk) return 0;
        if (!aOk) return 1;
        if (!bOk) return -1;
        const diff = Number(a[col]) - Number(b[col]);
        return asc ? diff : -diff;
      }
      if (col === 'symbol') {
        const valA = String(a.symbol || '');
        const valB = String(b.symbol || '');
        const cmp = symbolCollator ? symbolCollator.compare(valA, valB) : (valA < valB ? -1 : (valA > valB ? 1 : 0));
        return asc ? cmp : -cmp;
      }
      return 0;
    });

    return filtered;
  }

  function updateSortIndicators() {
    document.querySelectorAll('#trade-history-table th.sortable').forEach(th => {
      if (th.dataset.sort === HISTORY_SORT.column) {
        th.setAttribute('aria-sort', HISTORY_SORT.asc ? 'ascending' : 'descending');
      } else {
        th.removeAttribute('aria-sort');
      }
    });
  }

  function renderCloseReasonBadge(reason) {
    if (reason === null || reason === undefined || String(reason).trim() === '') {
      return '<span class="text-[#7f91a8]">--</span>';
    }
    const text = String(reason);
    const safeReason = escapeHtml(text);
    if (text.includes('保本') || text.includes('锁利')) {
      return '<span class="px-2 py-0.5 rounded-[3px] text-[10px] font-sans font-medium bg-[#22c58b]/[0.12] text-[#38d99f] border border-[#22c58b]/30">' + safeReason + '</span>';
    }
    if (text.includes('止损') || text.includes('跌破')) {
      return '<span class="px-2 py-0.5 rounded-[3px] text-[10px] font-sans font-medium bg-[#f25565]/[0.12] text-[#f8717e] border border-[#f25565]/30">' + safeReason + '</span>';
    }
    if (text.includes('实盘') || text.includes('BINANCE')) {
      return '<span class="text-[11px] font-sans text-[#9fb0c5]">' + safeReason + '</span>';
    }
    return '<span class="text-[11px] font-sans text-[#7f91a8]">' + safeReason + '</span>';
  }

  function renderHistoryTable() {
    const histTbody = document.getElementById('history-tbody');
    if (!histTbody || !currentAccountData) return;

    const fullHistory = currentAccountData.trade_history;
    const rows = getFilteredAndSortedHistory(fullHistory);
    setText(
      'history-count-badge',
      rows.length === fullHistory.length
        ? ('共 ' + fullHistory.length + ' 笔')
        : ('显示 ' + rows.length + ' / 共 ' + fullHistory.length + ' 笔')
    );
    updateSortIndicators();

    if (rows.length === 0) {
      histTbody.innerHTML = '<tr><td colspan="10" class="py-10 text-center text-[#9fb0c5] text-xs font-sans">' +
        (fullHistory.length === 0 ? '暂无历史开平仓流水记录' : '当前筛选条件下无匹配交易记录') + '</td></tr>';
      return;
    }

    histTbody.innerHTML = rows.map(h => {
      if (!h || typeof h !== 'object' || typeof h.symbol !== 'string' || !h.symbol.trim() || typeof h.side !== 'string' || !h.side.trim()) {
        throw new Error('Invalid trade_history entry: missing symbol or side');
      }
      const sym = h.symbol.trim();
      const safeSym = escapeHtml(sym);
      const side = h.side.trim().toUpperCase();
      const sideCls = side === 'SHORT'
        ? 'text-[#f8717e]'
        : (side === 'LONG' ? 'text-[#38d99f]' : 'text-[#9fb0c5]');

      const pnlVal = isFiniteNumber(h.net_pnl) ? Number(h.net_pnl) : null;
      const pnlCls = pnlVal === null ? 'text-[#7f91a8] font-normal' : (pnlVal > 0 ? 'text-[#38d99f] font-bold' : (pnlVal < 0 ? 'text-[#f8717e] font-bold' : 'text-[#f1f5f9] font-medium'));
      const pctVal = isFiniteNumber(h.pnl_pct) ? Number(h.pnl_pct) : null;
      const pctCls = pctVal === null ? 'text-[#7f91a8] font-normal' : (pctVal > 0 ? 'text-[#38d99f] font-bold' : (pctVal < 0 ? 'text-[#f8717e] font-bold' : 'text-[#f1f5f9] font-medium'));
      const pctText = formatSignedPct(h.pnl_pct);
      const cTime = escapeHtml(h.closed_at || '--');
      const hasEntryTime = typeof h.entry_time === 'string' && h.entry_time.trim() !== '' && h.entry_time.trim() !== '--';
      const entryTimeHtml = hasEntryTime
        ? '<div class="text-[10px] text-[#7f91a8]">开仓: ' + escapeHtml(h.entry_time.trim()) + '</div>'
        : '';
      const dur = escapeHtml(h.duration_str || '--');
      const tvUrl = getTvUrl(sym);

      const entryStr = isFiniteNumber(h.entry_price) && Number(h.entry_price) > 0 ? ('$' + formatCryptoPrice(h.entry_price)) : '--';
      const exitStr = isFiniteNumber(h.exit_price) && Number(h.exit_price) > 0 ? ('$' + formatCryptoPrice(h.exit_price)) : '--';
      const amtStr = isFiniteNumber(h.amount) && Number(h.amount) > 0 ? Number(h.amount).toLocaleString('en-US') : '--';
      const notionalStr = isFiniteNumber(h.entry_notional) && Number(h.entry_notional) > 0 ? formatMoney(h.entry_notional) : '--';
      const notionalCellHtml = (notionalStr === '--' && amtStr === '--')
        ? '<span class="text-[#7f91a8]">--</span>'
        : '<div>' + escapeHtml(notionalStr) + '</div><div class="text-[10px] text-[#7f91a8]">' + escapeHtml(amtStr) + '</div>';

      return (
        '<tr class="trade-row hover:bg-[#17202e] transition-colors font-mono tabular-nums text-[11px]">' +
          '<td class="px-2.5 sm:px-3.5 py-2.5 text-[#9fb0c5] whitespace-nowrap">' +
            '<div class="text-[#f1f5f9] font-medium">' + cTime + '</div>' +
            entryTimeHtml +
          '</td>' +
          '<td class="px-2.5 sm:px-3.5 py-2.5 font-sans font-bold text-[#f1f5f9] whitespace-nowrap">' +
            '<div class="flex items-center gap-1.5">' +
              '<a href="' + escapeHtml(tvUrl) + '" target="_blank" rel="noopener noreferrer" class="text-[#f1f5f9] hover:text-[#8ab8ff] hover:underline inline-flex items-center gap-1">' +
                '<span>' + safeSym + '</span>' + EXT_ARROW_SVG +
              '</a>' +
              '<span class="text-[10px] font-mono font-semibold ' + sideCls + '">' + escapeHtml(side) + '</span>' +
            '</div>' +
          '</td>' +
          '<td class="px-2.5 sm:px-3.5 py-2.5 text-right text-[#f1f5f9] whitespace-nowrap">' + notionalCellHtml + '</td>' +
          '<td class="px-2.5 sm:px-3.5 py-2.5 text-right ' + (entryStr === '--' ? 'text-[#7f91a8]' : 'text-[#9fb0c5]') + ' whitespace-nowrap">' + escapeHtml(entryStr) + '</td>' +
          '<td class="px-2.5 sm:px-3.5 py-2.5 text-right ' + (exitStr === '--' ? 'text-[#7f91a8]' : 'text-[#f1f5f9] font-semibold') + ' whitespace-nowrap">' + escapeHtml(exitStr) + '</td>' +
          '<td class="px-2.5 sm:px-3.5 py-2.5 text-right whitespace-nowrap ' + pnlCls + '">' + escapeHtml(formatMoney(pnlVal, { signed: true })) + '</td>' +
          '<td class="px-2.5 sm:px-3.5 py-2.5 text-right whitespace-nowrap ' + pctCls + '">' + escapeHtml(pctText) + '</td>' +
          '<td class="px-2.5 sm:px-3.5 py-2.5 text-right ' + (dur === '--' ? 'text-[#7f91a8]' : 'text-[#9fb0c5]') + ' whitespace-nowrap">' + dur + '</td>' +
          '<td class="px-2.5 sm:px-3.5 py-2.5 whitespace-nowrap">' + renderCloseReasonBadge(h.close_reason) + '</td>' +
          '<td class="px-2.5 sm:px-3.5 py-2.5 ' + (h.trendline_label ? 'text-[#9fb0c5]' : 'text-[#7f91a8]') + ' whitespace-nowrap">' + escapeHtml(h.trendline_label || '--') + '</td>' +
        '</tr>'
      );
    }).join('');
  }

  const barValueLabelsPlugin = {
    id: 'barValueLabels',
    afterDatasetsDraw(chart, _args, pluginOpts) {
      if (!pluginOpts || !pluginOpts.enabled) return;
      const ctx = chart.ctx;
      const meta = chart.getDatasetMeta(0);
      const data = chart.data && chart.data.datasets && chart.data.datasets[0] ? chart.data.datasets[0].data : [];
      if (!meta || !Array.isArray(meta.data) || !meta.data.length || !ctx) return;
      if (pluginOpts.signed) {
        const plotW = chart.chartArea ? (chart.chartArea.right - chart.chartArea.left) : 400;
        if (meta.data.length > 14 || (plotW / meta.data.length) < 38) return;
      }
      ctx.save();
      ctx.font = '600 10px "JetBrains Mono", "SF Mono", monospace';
      ctx.textAlign = 'center';
      meta.data.forEach((bar, i) => {
        const raw = Number(data[i]);
        if (!Number.isFinite(raw) || raw === 0) return;
        const label = typeof pluginOpts.formatter === 'function' ? pluginOpts.formatter(raw) : String(raw);
        if (!label) return;
        const isNeg = raw < 0;
        ctx.fillStyle = pluginOpts.signed
          ? (isNeg ? '#f8717e' : '#38d99f')
          : '#c9d5e3';
        ctx.textBaseline = isNeg ? 'top' : 'bottom';
        const yPos = isNeg ? (bar.y + 4) : (bar.y - 4);
        ctx.fillText(label, bar.x, yPos);
      });
      ctx.restore();
    }
  };

  const emptyStatePlugin = {
    id: 'emptyState',
    afterDraw(chart, _args, pluginOpts) {
      if (!pluginOpts || pluginOpts.enabled === false) return;
      const text = pluginOpts.text;
      if (!text || !chart.chartArea) return;
      const datasets = chart.data && Array.isArray(chart.data.datasets) ? chart.data.datasets : [];
      const hasData = datasets.some(ds => {
        if (!Array.isArray(ds.data) || ds.data.length === 0) return false;
        if (pluginOpts.emptyWhenNonPositive) {
          return ds.data.some(v => Number(v) > 0);
        }
        return true;
      });
      if (hasData) return;
      const ctx = chart.ctx;
      if (!ctx) return;
      const cx = (chart.chartArea.left + chart.chartArea.right) / 2;
      const cy = (chart.chartArea.top + chart.chartArea.bottom) / 2;
      ctx.save();
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillStyle = '#7f91a8';
      ctx.font = '500 12px "Inter", -apple-system, BlinkMacSystemFont, sans-serif';
      ctx.fillText(text, cx, cy);
      ctx.restore();
    }
  };

  const doughnutCenterPlugin = {
    id: 'doughnutCenter',
    afterDraw(chart) {
      if (!currentAccountData || !currentAccountData.summary || !chart.chartArea) return;
      const ctx = chart.ctx;
      if (!ctx) return;
      const s = currentAccountData.summary;
      const w = isFiniteNumber(s.win_count) ? Number(s.win_count) : 0;
      const l = isFiniteNumber(s.loss_count) ? Number(s.loss_count) : 0;
      const b = isFiniteNumber(s.breakeven_count) ? Number(s.breakeven_count) : 0;
      const meta = chart.getDatasetMeta(0);
      const arc = meta && Array.isArray(meta.data) ? meta.data[0] : null;
      const cx = (arc && isFiniteNumber(arc.x)) ? arc.x : (chart.chartArea.left + chart.chartArea.right) / 2;
      const cy = (arc && isFiniteNumber(arc.y)) ? arc.y : (chart.chartArea.top + chart.chartArea.bottom) / 2;

      ctx.save();
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      if (w + l + b === 0) {
        ctx.fillStyle = '#7f91a8';
        ctx.font = '500 12px "Inter", -apple-system, BlinkMacSystemFont, sans-serif';
        ctx.fillText('暂无交易样本', cx, cy);
        ctx.restore();
        return;
      }
      if (!arc) {
        ctx.restore();
        return;
      }
      const winRateStr = isFiniteNumber(s.win_rate_pct) ? (Number(s.win_rate_pct).toFixed(1) + '%') : '--';
      const subStr = b > 0 ? (w + 'W · ' + l + 'L · ' + b + 'BE') : (w + 'W · ' + l + 'L');
      ctx.fillStyle = '#f1f5f9';
      ctx.font = '700 22px "JetBrains Mono", "SF Mono", monospace';
      ctx.fillText(winRateStr, cx, cy - 8);
      ctx.fillStyle = '#8b9bb0';
      ctx.font = '500 11px "JetBrains Mono", "SF Mono", monospace';
      ctx.fillText(subStr, cx, cy + 14);
      ctx.restore();
    }
  };

  function ensureChartDefaults() {
    if (chartDefaultsConfigured || typeof Chart === 'undefined' || !Chart.defaults) return;
    Chart.defaults.color = '#8b9bb0';
    if (Chart.defaults.font) {
      Chart.defaults.font.family = '"JetBrains Mono", "Inter", -apple-system, BlinkMacSystemFont, sans-serif';
    }
    Chart.defaults.borderColor = 'rgba(34, 43, 56, 0.7)';
    chartDefaultsConfigured = true;
  }

  function upsertChart(key, canvas, config) {
    if (!canvas || typeof Chart === 'undefined') return;
    if (chartInstances[key]) {
      chartInstances[key].data = config.data;
      chartInstances[key].update('none');
      return;
    }
    chartInstances[key] = new Chart(canvas, config);
  }

  function updateCharts(accountData) {
    const summary = accountData.summary;
    const equityCurve = accountData.equity_curve;
    const dailyPnl = accountData.daily_pnl;
    const pnlDist = accountData.pnl_distribution;
    const bins = pnlDist.bins;

    if (equityCurve.length > 0) {
      const firstPt = equityCurve[0];
      const lastPt = equityCurve[equityCurve.length - 1];
      if (!firstPt || typeof firstPt.datetime !== 'string' || !firstPt.datetime || !lastPt || typeof lastPt.datetime !== 'string' || !lastPt.datetime) {
        throw new Error('Invalid equity_curve endpoint: missing datetime');
      }
      setText('equity-range-label', firstPt.datetime.slice(5, 16) + ' ~ ' + lastPt.datetime.slice(5, 16));
      let peakEq = null;
      equityCurve.forEach((pt, idx) => {
        if (!pt || typeof pt !== 'object' || typeof pt.datetime !== 'string' || !pt.datetime || !isFiniteNumber(pt.equity)) {
          throw new Error('Invalid equity_curve entry at index ' + idx);
        }
        peakEq = peakEq === null ? Number(pt.equity) : Math.max(peakEq, Number(pt.equity));
      });
      setText('equity-peak-label', '峰值: ' + formatMoney(peakEq));
      const currEl = setText('equity-current-label', '当前: ' + formatMoney(lastPt.equity));
      if (currEl && isFiniteNumber(summary.initial_capital)) {
        const netTransfers = isFiniteNumber(summary.net_external_transfers) ? Number(summary.net_external_transfers) : 0;
        const delta = Number(lastPt.equity) - Number(summary.initial_capital) - netTransfers;
        setSignedColorClass(currEl, delta);
      }
    } else {
      setText('equity-range-label', '--');
      setText('equity-peak-label', '峰值: --');
      setText('equity-current-label', '当前: --');
    }

    setText('daily-days-badge', dailyPnl.length + ' 天');

    if (!isFiniteNumber(summary.win_count) || !isFiniteNumber(summary.loss_count) || !isFiniteNumber(summary.breakeven_count)) {
      throw new Error('Invalid summary: missing win/loss/breakeven counts');
    }
    const winCount = Number(summary.win_count);
    const lossCount = Number(summary.loss_count);
    const beCount = Number(summary.breakeven_count);
    setText('win-loss-summary-badge', (winCount + lossCount + beCount) + ' 笔');

    const totalBinned = bins.reduce((sum, b, idx) => {
      if (!b || typeof b !== 'object' || typeof b.label !== 'string' || !isFiniteNumber(b.count)) {
        throw new Error('Invalid pnl_distribution bin at index ' + idx);
      }
      return sum + Number(b.count);
    }, 0);
    setText('dist-summary-badge', totalBinned + ' 笔样本');

    if (typeof Chart === 'undefined') return;

    ensureChartDefaults();

    const tooltipConfig = {
      backgroundColor: 'rgba(16, 21, 29, 0.96)',
      titleColor: '#f1f5f9',
      bodyColor: '#c9d5e3',
      borderColor: '#324055',
      borderWidth: 1,
      cornerRadius: 4,
      padding: 10,
      boxPadding: 4,
      usePointStyle: true
    };

    const eqCanvas = document.getElementById('chart-equity-curve');
    if (eqCanvas) {
      const eqLabels = equityCurve.map(p => p.datetime.slice(5, 16));
      const eqValues = equityCurve.map(p => Number(p.equity));
      const hasInitialCapital = isFiniteNumber(summary.initial_capital);
      const datasets = [{
        label: '账户总资产',
        data: eqValues,
        borderColor: '#3b82f6',
        backgroundColor: function(c) {
          const chart = c.chart;
          const ctx = chart.ctx;
          const chartArea = chart.chartArea;
          if (!chartArea) return null;
          const height = chartArea.bottom - chartArea.top;
          if (
            !cachedEqGradient ||
            cachedEqGradientCtx !== ctx ||
            cachedEqGradientTop !== chartArea.top ||
            cachedEqGradientHeight !== height
          ) {
            cachedEqGradientCtx = ctx;
            cachedEqGradientTop = chartArea.top;
            cachedEqGradientHeight = height;
            const g = ctx.createLinearGradient(0, chartArea.top, 0, chartArea.bottom);
            g.addColorStop(0, 'rgba(59, 130, 246, 0.20)');
            g.addColorStop(1, 'rgba(59, 130, 246, 0.0)');
            cachedEqGradient = g;
          }
          return cachedEqGradient;
        },
        fill: true,
        tension: 0.15,
        pointRadius: eqValues.length > 15 ? 0 : 2.5,
        pointHoverRadius: 4,
        pointHitRadius: 12,
        pointBackgroundColor: '#3b82f6',
        borderWidth: 1.75
      }];
      if (hasInitialCapital) {
        const cap = Number(summary.initial_capital);
        const hasTransfers = isFiniteNumber(summary.net_external_transfers);
        const netTransfers = hasTransfers ? Number(summary.net_external_transfers) : 0;
        const baseline = cap + netTransfers;
        datasets.push({
          label: hasTransfers && netTransfers !== 0 ? '净投入基准线' : '本金基准线',
          data: equityCurve.map(() => baseline),
          borderColor: '#5c6f89',
          borderDash: [4, 4],
          borderWidth: 1.2,
          pointRadius: 0,
          fill: false
        });
      }
      upsertChart('eq', eqCanvas, {
        type: 'line',
        plugins: [emptyStatePlugin],
        data: { labels: eqLabels, datasets: datasets },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: { mode: 'index', intersect: false },
          plugins: {
            emptyState: { text: '暂无净值数据' },
            tooltip: {
              ...tooltipConfig,
              callbacks: {
                label: c => c.dataset.label + ': ' + formatMoney(c.raw)
              }
            },
            legend: { display: false }
          },
          scales: {
            x: { grid: { display: false }, ticks: { maxTicksLimit: 7, font: { size: 10 } } },
            y: {
              grid: { color: 'rgba(34, 43, 56, 0.6)' },
              ticks: {
                font: { size: 10 },
                callback: formatAxisUsd
              }
            }
          }
        }
      });
    }

    const dailyCanvas = document.getElementById('chart-daily-pnl');
    if (dailyCanvas) {
      const dLabels = dailyPnl.map((d, idx) => {
        if (!d || typeof d !== 'object' || typeof d.date !== 'string' || !d.date) {
          throw new Error('Invalid daily_pnl entry missing date at index ' + idx);
        }
        return d.date.length >= 10 && d.date[4] === '-' ? d.date.slice(5, 10) : d.date;
      });
      const dValues = dailyPnl.map((d, idx) => {
        if (!isFiniteNumber(d.net_pnl)) {
          throw new Error('Invalid daily_pnl entry non-finite net_pnl at index ' + idx);
        }
        return Number(d.net_pnl);
      });
      upsertChart('daily', dailyCanvas, {
        type: 'bar',
        plugins: [barValueLabelsPlugin, emptyStatePlugin],
        data: {
          labels: dLabels,
          datasets: [{
            label: '当日净盈亏 ($)',
            data: dValues,
            backgroundColor: dValues.map(v => (v >= 0 ? 'rgba(34, 197, 139, 0.85)' : 'rgba(242, 85, 101, 0.85)')),
            hoverBackgroundColor: dValues.map(v => (v >= 0 ? '#22c58b' : '#f25565')),
            borderRadius: 3,
            borderSkipped: false,
            maxBarThickness: 38,
            barPercentage: 0.55,
            categoryPercentage: 0.65
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          layout: { padding: { top: 20, bottom: 18 } },
          plugins: {
            emptyState: { text: '暂无每日盈亏' },
            barValueLabels: {
              enabled: true,
              signed: true,
              formatter: (v) => {
                const abs = Math.abs(v);
                const s = abs >= 100 ? Math.round(abs).toLocaleString('en-US') : abs.toFixed(1);
                return (v < 0 ? '-$' : '+$') + s;
              }
            },
            tooltip: {
              ...tooltipConfig,
              callbacks: {
                label: c => {
                  const list = currentAccountData.daily_pnl;
                  const item = list[c.dataIndex];
                  const cnt = item && isFiniteNumber(item.trades_count) ? item.trades_count : '--';
                  return ['当日净盈亏: ' + formatMoney(c.raw, { signed: true }), '成交笔数: ' + cnt + ' 笔'];
                }
              }
            },
            legend: { display: false }
          },
          scales: {
            x: { grid: { display: false }, ticks: { maxTicksLimit: 10, font: { size: 10 } } },
            y: {
              grid: {
                color: (ctx) => (ctx && ctx.tick && ctx.tick.value === 0 ? '#3d4e66' : 'rgba(34, 43, 56, 0.55)'),
                lineWidth: (ctx) => (ctx && ctx.tick && ctx.tick.value === 0 ? 1.5 : 1)
              },
              ticks: {
                font: { size: 10 },
                callback: formatAxisUsd
              }
            }
          }
        }
      });
    }

    const winLossCanvas = document.getElementById('chart-win-loss');
    if (winLossCanvas) {
      upsertChart('winLoss', winLossCanvas, {
        type: 'doughnut',
        plugins: [doughnutCenterPlugin],
        data: {
          labels: ['盈利', '亏损', '保本'],
          datasets: [{
            data: [winCount, lossCount, beCount],
            backgroundColor: ['#22c58b', '#f25565', '#4b5c75'],
            borderColor: '#10151d',
            borderWidth: 2,
            hoverOffset: 4
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          cutout: '76%',
          plugins: {
            tooltip: tooltipConfig,
            legend: { position: 'bottom', labels: { boxWidth: 10, padding: 14, font: { size: 11 } } }
          }
        }
      });
    }

    const distCanvas = document.getElementById('chart-pnl-dist');
    if (distCanvas) {
      const binLabels = bins.map(b => b.label);
      const binCounts = bins.map(b => Number(b.count));
      const binColors = bins.map(b => {
        if (isFiniteNumber(b.max) && Number(b.max) <= -0.0001) return 'rgba(242, 85, 101, 0.85)';
        if (isFiniteNumber(b.min) && Number(b.min) >= 0.0001) return 'rgba(34, 197, 139, 0.85)';
        return '#4b5c75';
      });
      upsertChart('dist', distCanvas, {
        type: 'bar',
        plugins: [barValueLabelsPlugin, emptyStatePlugin],
        data: {
          labels: binLabels,
          datasets: [{
            label: '交易笔数',
            data: binCounts,
            backgroundColor: binColors,
            borderRadius: 3,
            borderSkipped: false,
            maxBarThickness: 34,
            barPercentage: 0.6
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          layout: { padding: { top: 20 } },
          plugins: {
            emptyState: { enabled: true, text: '暂无交易样本', emptyWhenNonPositive: true },
            barValueLabels: {
              enabled: true,
              signed: false,
              formatter: (v) => (v > 0 ? String(v) : '')
            },
            tooltip: {
              ...tooltipConfig,
              callbacks: {
                label: c => {
                  const curBins = currentAccountData.pnl_distribution.bins;
                  const bin = curBins[c.dataIndex];
                  const syms = bin && Array.isArray(bin.trades) && bin.trades.length > 0
                    ? ' (' + bin.trades.slice(0, 5).join(', ') + (bin.trades.length > 5 ? '…' : '') + ')'
                    : '';
                  return '笔数: ' + c.raw + ' 笔' + syms;
                }
              }
            },
            legend: { display: false }
          },
          scales: {
            x: { grid: { display: false }, ticks: { font: { size: 10 } } },
            y: {
              beginAtZero: true,
              grid: { color: 'rgba(34, 43, 56, 0.55)' },
              ticks: { precision: 0, font: { size: 10 } }
            }
          }
        }
      });
    }
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
    currentAccountData = accountData;

    const summary = accountData.summary;
    const positions = accountData.active_positions;

    setText('sync-time-text', accountData.generated_at || '--');
    updateApiStatusBadge(accountData, false);
    updateKpis(summary);
    renderActivePositions(positions);
    renderHistoryTable();
    updateCharts(accountData);
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
    renderHistoryTable();
    const sortStatus = document.getElementById('history-sort-status');
    if (sortStatus && currentAccountData) {
      const label = th.textContent ? th.textContent.trim() : col;
      sortStatus.textContent = '历史流水已按 ' + label + (HISTORY_SORT.asc ? ' 升序' : ' 降序') + '排列';
    }
  }

  function bindControlsOnce() {
    const searchInput = document.getElementById('history-search-input');
    if (searchInput && !searchInput.dataset.bound) {
      searchInput.dataset.bound = 'true';
      searchInput.addEventListener('input', function() {
        if (historySearchTimer) clearTimeout(historySearchTimer);
        historySearchTimer = setTimeout(() => {
          historySearchTimer = null;
          renderHistoryTable();
        }, 120);
      });
    }

    const outcomeSelect = document.getElementById('history-outcome-filter');
    if (outcomeSelect && !outcomeSelect.dataset.bound) {
      outcomeSelect.dataset.bound = 'true';
      outcomeSelect.addEventListener('change', renderHistoryTable);
    }

    const resetBtn = document.getElementById('btn-reset-history-filter');
    if (resetBtn && !resetBtn.dataset.bound) {
      resetBtn.dataset.bound = 'true';
      resetBtn.addEventListener('click', function() {
        if (historySearchTimer) {
          clearTimeout(historySearchTimer);
          historySearchTimer = null;
        }
        if (searchInput) searchInput.value = '';
        if (outcomeSelect) outcomeSelect.value = '';
        renderHistoryTable();
      });
    }

    const histTable = document.getElementById('trade-history-table');
    const supportsInvokerCommands = typeof HTMLButtonElement !== 'undefined' && 'commandForElement' in HTMLButtonElement.prototype;
    if (histTable && supportsInvokerCommands && !histTable.dataset.bound) {
      histTable.dataset.bound = 'true';
      histTable.addEventListener('command', function(e) {
        if (e.command === '--sort' && e.source && typeof e.source.closest === 'function') {
          triggerHistorySort(e.source.closest('th.sortable'));
        }
      });
    } else {
      document.querySelectorAll('#trade-history-table th.sortable').forEach(th => {
        if (th.dataset.bound) return;
        th.dataset.bound = 'true';
        const btn = th.querySelector ? th.querySelector('button[command="--sort"]') : null;
        const target = btn || th;
        target.addEventListener('click', function() {
          triggerHistorySort(th);
        });
      });
    }

    const refreshBtn = document.getElementById('btn-manual-refresh');
    if (refreshBtn && !refreshBtn.dataset.bound) {
      refreshBtn.dataset.bound = 'true';
      refreshBtn.addEventListener('click', function() {
        const spinner = document.getElementById('refresh-spinner');
        if (spinner) spinner.classList.add('animate-spin');
        tryLoadData().finally(() => {
          if (spinner) spinner.classList.remove('animate-spin');
        });
      });
    }
  }

  async function tryLoadData() {
    if (accountController) accountController.abort();
    const controller = new AbortController();
    accountController = controller;
    const loadId = ++activeLoadId;
    const status = document.getElementById('account-load-status');
    const report = document.getElementById('account-report');
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
        if (status) status.hidden = true;
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
      if (status) status.hidden = true;
    } catch (error) {
      if (error && error.name === 'AbortError') return;
      if (loadId !== activeLoadId) return;
      console.error('Failed to load account snapshot:', error);
      if (report) report.hidden = !latestSnapshot;
      updateApiStatusBadge(currentAccountData, true);
      if (status) {
        status.hidden = false;
        status.className = 'ledger-frame px-4 py-3 text-xs bg-[#f25565]/10 border-[#f25565]/30 text-[#f8717e]';
        status.textContent = latestSnapshot
          ? ('账户数据刷新失败 (' + error.message + ')，当前展示上次快照。')
          : ('暂时无法加载账户数据 (' + error.message + ')，请检查后台 API 服务，系统将自动重试。');
      }
    } finally {
      if (accountController === controller) {
        accountController = null;
      }
    }
  }

  function init() {
    ensureChartDefaults();
    bindControlsOnce();
    tryLoadData();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  setInterval(() => {
    if (!document.hidden) tryLoadData();
  }, 30000);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      if (accountController) {
        accountController.abort();
        accountController = null;
      }
      activeLoadId += 1;
      return;
    }
    if (Date.now() - lastSuccessfulLoadAt < VISIBILITY_COOLDOWN_MS) return;
    tryLoadData();
  });
})();
