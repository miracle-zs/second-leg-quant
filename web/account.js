(function() {
  const chartInstances = {};
  const HISTORY_SORT = { column: 'closed_ts', asc: false };
  let currentAccountData = null;
  let latestSnapshot = '';
  let lastEtag = null;
  let refreshInFlight = false;

  function isFiniteNumber(val) {
    if (val === null || val === undefined || val === '') return false;
    const num = Number(val);
    return Number.isFinite(num);
  }

  function escapeHtml(val) {
    if (val === null || val === undefined) return '';
    return String(val)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

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

  function formatPct(val, options) {
    const opts = options || {};
    if (!isFiniteNumber(val)) return '--';
    const num = Number(val);
    const decimals = opts.decimals !== undefined ? opts.decimals : 2;
    const signed = opts.signed !== undefined ? opts.signed : true;
    return (signed && num > 0 ? '+' : '') + num.toFixed(decimals) + '%';
  }

  function formatCryptoPrice(val) {
    if (!isFiniteNumber(val) || Number(val) <= 0) return '--';
    const num = Number(val);
    if (num >= 100) {
      return num.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }
    if (num >= 1) return num.toFixed(4);
    if (num >= 0.0001) return num.toFixed(6);
    return num.toFixed(8);
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
    el.classList.remove('text-emerald-400', 'text-rose-400', 'text-slate-400', 'text-slate-300', 'text-white');
    if (!isFiniteNumber(val)) {
      el.classList.add('text-slate-400');
    } else if (Number(val) >= 0) {
      el.classList.add('text-emerald-400');
    } else {
      el.classList.add('text-rose-400');
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
      badge.className = 'px-2 py-0.5 text-[11px] font-semibold rounded-md bg-rose-500/10 text-rose-300 border border-rose-500/30 flex items-center gap-1.5';
      badge.innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-rose-400" aria-hidden="true"></span><span>数据拉取失败</span>';
      return;
    }

    const mode = (accountData && accountData.api_status && accountData.api_status.mode) || (accountData && accountData.data_source) || '';
    if (mode === 'BINANCE_REALTIME' || mode === 'BINANCE_API') {
      badge.className = 'px-2 py-0.5 text-[11px] font-semibold rounded-md bg-emerald-500/10 text-emerald-300 border border-emerald-500/20 flex items-center gap-1.5';
      badge.innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" aria-hidden="true"></span><span>币安实盘在线</span>';
    } else if (mode === 'BINANCE_PARTIAL') {
      badge.className = 'px-2 py-0.5 text-[11px] font-semibold rounded-md bg-amber-500/10 text-amber-300 border border-amber-500/30 flex items-center gap-1.5';
      badge.innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-amber-400" aria-hidden="true"></span><span>币安部分数据可用</span>';
    } else {
      badge.className = 'px-2 py-0.5 text-[11px] font-semibold rounded-md bg-slate-800 text-amber-300 border border-amber-500/20 flex items-center gap-1.5';
      badge.innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-amber-400" aria-hidden="true"></span><span>离线风控同步</span>';
    }
  }

  function updateKpis(summary, positions) {
    setText('kpi-total-equity', formatMoney(summary.total_equity));
    setText(
      'kpi-initial-capital',
      '本金: ' + (isFiniteNumber(summary.initial_capital) ? formatMoney(summary.initial_capital, { decimals: 0 }) : '--')
    );

    const retEl = setText('kpi-total-return', formatPct(summary.total_return_pct));
    setSignedColorClass(retEl, summary.total_return_pct);

    setText('kpi-available-margin', formatMoney(summary.available_margin));
    setText('kpi-position-margin', formatMoney(summary.position_margin));
    const marginBadge = document.getElementById('kpi-margin-badge');
    if (marginBadge) {
      if (isFiniteNumber(summary.total_equity) && Number(summary.total_equity) > 0 && isFiniteNumber(summary.position_margin)) {
        const utilPct = (Number(summary.position_margin) / Number(summary.total_equity)) * 100;
        marginBadge.textContent = utilPct > 0 ? ('占用 ' + utilPct.toFixed(1) + '%') : '安全在岸';
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
    const openCount = isFiniteNumber(summary.open_trades) ? Number(summary.open_trades) : positions.length;
    setText('kpi-open-trades', openCount + ' 笔在途');

    setText('kpi-win-rate', formatPct(summary.win_rate_pct, { signed: false, decimals: 1 }));
    const winCount = isFiniteNumber(summary.win_count) ? Number(summary.win_count) : 0;
    const lossCount = isFiniteNumber(summary.loss_count) ? Number(summary.loss_count) : 0;
    const beCount = isFiniteNumber(summary.breakeven_count) ? Number(summary.breakeven_count) : 0;
    setText('kpi-wl-record', winCount + 'W / ' + lossCount + 'L / ' + beCount + 'BE');
    setText('kpi-total-trades', isFiniteNumber(summary.closed_trades) ? Number(summary.closed_trades) + ' 笔' : '--');

    let pfText = '--';
    if (isFiniteNumber(summary.profit_factor)) {
      pfText = Number(summary.profit_factor).toFixed(2);
    } else if (winCount > 0 && lossCount === 0) {
      pfText = '∞ (全胜)';
    }
    setText('kpi-profit-factor', pfText);
    setText('kpi-avg-win', '均盈: ' + formatMoney(summary.avg_win, { signed: true }));
    setText(
      'kpi-avg-loss',
      '均亏: ' + (isFiniteNumber(summary.avg_loss) ? formatMoney(-Math.abs(Number(summary.avg_loss))) : '--')
    );

    setText(
      'kpi-max-dd-pct',
      isFiniteNumber(summary.max_drawdown_pct) ? ('-' + Math.abs(Number(summary.max_drawdown_pct)).toFixed(2) + '%') : '--'
    );
    setText(
      'kpi-max-dd-usd',
      isFiniteNumber(summary.max_drawdown_usd) ? formatMoney(-Math.abs(Number(summary.max_drawdown_usd))) : '--'
    );

    setText('kpi-sharpe', formatRatio(summary.sharpe_ratio, 2));
    const sharpeRating = document.getElementById('kpi-sharpe-rating');
    if (sharpeRating) {
      sharpeRating.classList.remove('text-emerald-400', 'text-sky-400', 'text-amber-300', 'text-rose-400', 'text-slate-400', 'text-slate-300');
      if (!isFiniteNumber(summary.sharpe_ratio)) {
        sharpeRating.textContent = '样本不足';
        sharpeRating.classList.add('text-slate-400');
      } else {
        const sr = Number(summary.sharpe_ratio);
        if (sr >= 2.0) {
          sharpeRating.textContent = '顶级稳健';
          sharpeRating.classList.add('text-emerald-400');
        } else if (sr >= 1.0) {
          sharpeRating.textContent = '表现稳健';
          sharpeRating.classList.add('text-sky-400');
        } else if (sr >= 0) {
          sharpeRating.textContent = '收益一般';
          sharpeRating.classList.add('text-amber-300');
        } else {
          sharpeRating.textContent = '负收益警告';
          sharpeRating.classList.add('text-rose-400');
        }
      }
    }

    setText('kpi-sortino', formatRatio(summary.sortino_ratio, 2));
    const sortinoRating = document.getElementById('kpi-sortino-rating');
    if (sortinoRating) {
      sortinoRating.classList.remove('text-purple-300', 'text-sky-400', 'text-amber-300', 'text-rose-400', 'text-slate-400', 'text-slate-300', 'text-emerald-400');
      if (!isFiniteNumber(summary.sortino_ratio)) {
        if (winCount > 0 && lossCount === 0) {
          sortinoRating.textContent = '零下行波动';
          sortinoRating.classList.add('text-emerald-400');
        } else {
          sortinoRating.textContent = '样本不足';
          sortinoRating.classList.add('text-slate-400');
        }
      } else {
        const so = Number(summary.sortino_ratio);
        if (so >= 2.5) {
          sortinoRating.textContent = '极低尾部风险';
          sortinoRating.classList.add('text-purple-300');
        } else if (so >= 1.0) {
          sortinoRating.textContent = '下行风险可控';
          sortinoRating.classList.add('text-sky-400');
        } else if (so >= 0) {
          sortinoRating.textContent = '下行收益一般';
          sortinoRating.classList.add('text-amber-300');
        } else {
          sortinoRating.textContent = '下行风险偏高';
          sortinoRating.classList.add('text-rose-400');
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
    if (posEmpty) posEmpty.classList.toggle('hidden', positions.length > 0);
    const posWrap = document.getElementById('active-positions-table-wrap');
    if (posWrap) posWrap.classList.toggle('hidden', positions.length === 0);

    const shortWarn = document.getElementById('short-position-warning');
    if (shortWarn) {
      const hasUnmonitored = positions.some(p => {
        const side = String(p.side || '').toUpperCase();
        return side === 'SHORT' || p.risk_monitored === false;
      });
      shortWarn.classList.toggle('hidden', !hasUnmonitored);
    }

    if (!posTbody) return;
    if (positions.length === 0) {
      posTbody.innerHTML = '<tr><td colspan="9" class="py-6 text-center text-slate-500 font-sans">当前在岸安全，无活跃持仓</td></tr>';
      return;
    }

    posTbody.innerHTML = positions.map(p => {
      const sym = String(p.symbol || '--');
      const safeSym = escapeHtml(sym);
      const side = String(p.side || 'UNKNOWN').toUpperCase();
      const safeSide = escapeHtml(side);
      const riskMonitored = Boolean(p.risk_monitored);
      const sideBadge = side === 'SHORT'
        ? '<span class="px-1.5 py-0.2 rounded text-[9px] font-bold bg-rose-500/20 text-rose-300 border border-rose-500/30">SHORT</span>'
        : (side === 'LONG'
          ? '<span class="px-1.5 py-0.2 rounded text-[9px] font-bold bg-emerald-500/20 text-emerald-300 border border-emerald-500/30">LONG</span>'
          : '<span class="px-1.5 py-0.2 rounded text-[9px] font-bold bg-slate-800 text-slate-300 border border-slate-700">' + safeSide + '</span>');

      const pnlVal = isFiniteNumber(p.unrealized_pnl) ? Number(p.unrealized_pnl) : null;
      const roeVal = isFiniteNumber(p.roe_pct) ? Number(p.roe_pct) : null;
      const pnlCls = pnlVal === null ? 'text-slate-400' : (pnlVal >= 0 ? 'text-emerald-400' : 'text-rose-400');
      const pnlStr = formatMoney(pnlVal, { signed: true });
      const roeStr = roeVal === null ? '--' : (formatPct(roeVal) + ' ROE');

      const levStr = isFiniteNumber(p.leverage) ? (Number(p.leverage) + 'x') : '--';
      const notionalStr = formatMoney(p.notional);
      const amtStr = isFiniteNumber(p.amount) ? (Number(p.amount).toLocaleString('en-US') + ' 币') : '--';

      const tvUrl = 'https://www.tradingview.com/chart/ig0LuDL8/?symbol=' + encodeURIComponent('BINANCE:' + sym + '.P');
      const safeTlId = p.trendline_id ? escapeHtml(p.trendline_id) : '--';
      const trailingHtml = riskMonitored
        ? (p.trailing_stop_triggered ? '<span class="text-emerald-400 font-semibold">已触发</span>' : '未触发')
        : '<span class="text-amber-300/80">未纳入风控（仅多头）</span>';
      const slHtml = riskMonitored
        ? (p.stop_loss_notified ? '<span class="text-rose-300 font-semibold">已提醒</span>' : '监控中')
        : '<span class="px-2 py-0.5 rounded text-[10px] font-bold bg-slate-800 text-amber-300 border border-amber-500/30">未受风控监控（空单）</span>';

      return (
        '<tr class="hover:bg-slate-800/40 transition-colors border-b border-slate-800/60 font-mono text-xs">' +
          '<td class="px-3 py-2.5 font-bold font-sans text-white">' +
            '<div class="flex items-center gap-1.5">' +
              '<a href="' + escapeHtml(tvUrl) + '" target="_blank" rel="noopener noreferrer" class="text-sky-400 hover:underline flex items-center gap-1">' +
                '<span>' + safeSym + '</span><span class="text-[9px] text-slate-500">↗</span>' +
              '</a>' +
              sideBadge +
            '</div>' +
          '</td>' +
          '<td class="px-3 py-2.5 tabular-nums text-slate-300">' + escapeHtml(levStr) + ' · ' + escapeHtml(notionalStr) + '<div class="text-[10px] text-slate-500">' + escapeHtml(amtStr) + '</div></td>' +
          '<td class="px-3 py-2.5 tabular-nums text-slate-300">' + escapeHtml(formatDollarPrice(p.entry_price)) + '</td>' +
          '<td class="px-3 py-2.5 tabular-nums text-white font-bold">' + escapeHtml(formatDollarPrice(p.mark_price)) + '</td>' +
          '<td class="px-3 py-2.5 tabular-nums font-bold ' + pnlCls + '"><div>' + escapeHtml(pnlStr) + '</div><div class="text-[10px]">' + escapeHtml(roeStr) + '</div></td>' +
          '<td class="px-3 py-2.5 tabular-nums text-rose-400">' + escapeHtml(formatDollarPrice(p.liquidation_price)) + '</td>' +
          '<td class="px-3 py-2.5 text-slate-400">' + trailingHtml + '</td>' +
          '<td class="px-3 py-2.5 text-slate-400">' + slHtml + '</td>' +
          '<td class="px-3 py-2.5 text-slate-400">' + (riskMonitored ? safeTlId : '--') + '</td>' +
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
      let valA, valB;
      if (col === 'net_pnl' || col === 'pnl_pct' || col === 'closed_ts') {
        const aOk = isFiniteNumber(a[col]);
        const bOk = isFiniteNumber(b[col]);
        if (!aOk && !bOk) return 0;
        if (!aOk) return 1;
        if (!bOk) return -1;
        valA = Number(a[col]);
        valB = Number(b[col]);
      } else if (col === 'symbol') {
        valA = String(a.symbol || '');
        valB = String(b.symbol || '');
      } else {
        return 0;
      }
      if (valA < valB) return asc ? -1 : 1;
      if (valA > valB) return asc ? 1 : -1;
      return 0;
    });

    return filtered;
  }

  function updateSortIndicators() {
    document.querySelectorAll('#trade-history-table th.sortable').forEach(th => {
      const icon = th.querySelector('.sort-icon');
      if (!icon) return;
      if (th.dataset.sort === HISTORY_SORT.column) {
        icon.textContent = HISTORY_SORT.asc ? '▲' : '▼';
      } else {
        icon.textContent = '↕';
      }
    });
  }

  function renderCloseReasonBadge(reason) {
    const safeReason = escapeHtml(reason || '平仓');
    if (reason.includes('保本') || reason.includes('锁利')) {
      return '<span class="px-2 py-0.5 rounded text-[10px] font-bold bg-emerald-500/20 text-emerald-300 border border-emerald-500/30">' + safeReason + '</span>';
    }
    if (reason.includes('止损') || reason.includes('跌破')) {
      return '<span class="px-2 py-0.5 rounded text-[10px] font-bold bg-rose-500/20 text-rose-300 border border-rose-500/30">' + safeReason + '</span>';
    }
    if (reason.includes('实盘') || reason.includes('BINANCE')) {
      return '<span class="px-2 py-0.5 rounded text-[10px] font-bold bg-indigo-500/20 text-indigo-300 border border-indigo-500/30">' + safeReason + '</span>';
    }
    return '<span class="px-2 py-0.5 rounded text-[10px] font-bold bg-slate-800 text-slate-300 border border-slate-700">' + safeReason + '</span>';
  }

  function renderHistoryTable() {
    const histTbody = document.getElementById('history-tbody');
    if (!histTbody || !currentAccountData) return;

    const fullHistory = currentAccountData.trade_history || [];
    const rows = getFilteredAndSortedHistory(fullHistory);
    setText('history-count-badge', '显示 ' + rows.length + ' / 共 ' + fullHistory.length + ' 笔');
    updateSortIndicators();

    if (rows.length === 0) {
      histTbody.innerHTML = '<tr><td colspan="10" class="py-8 text-center text-slate-500">' +
        (fullHistory.length === 0 ? '暂无开平仓流水记录' : '没有匹配的交易记录') + '</td></tr>';
      return;
    }

    histTbody.innerHTML = rows.map(h => {
      const sym = String(h.symbol || '--');
      const safeSym = escapeHtml(sym);
      const side = String(h.side || 'UNKNOWN').toUpperCase();
      const sideCls = side === 'SHORT'
        ? 'bg-rose-500/15 text-rose-300 border-rose-500/30'
        : (side === 'LONG'
          ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20'
          : 'bg-slate-800 text-slate-300 border-slate-700');

      const pnlVal = isFiniteNumber(h.net_pnl) ? Number(h.net_pnl) : null;
      const pnlCls = pnlVal === null ? 'text-slate-400' : (pnlVal >= 0 ? 'text-emerald-400' : 'text-rose-400');
      const pctText = formatPct(h.pnl_pct);
      const cTime = escapeHtml(h.closed_at || '--');
      const eTime = escapeHtml(h.entry_time || '--');
      const dur = escapeHtml(h.duration_str || '--');
      const tvUrl = 'https://www.tradingview.com/chart/ig0LuDL8/?symbol=' + encodeURIComponent('BINANCE:' + sym + '.P');

      const entryStr = isFiniteNumber(h.entry_price) && Number(h.entry_price) > 0 ? ('$' + formatCryptoPrice(h.entry_price)) : '--';
      const exitStr = isFiniteNumber(h.exit_price) && Number(h.exit_price) > 0 ? ('$' + formatCryptoPrice(h.exit_price)) : '--';
      const amtStr = isFiniteNumber(h.amount) && Number(h.amount) > 0 ? (Number(h.amount).toLocaleString('en-US') + ' 币') : '--';
      const notionalStr = isFiniteNumber(h.entry_notional) && Number(h.entry_notional) > 0 ? formatMoney(h.entry_notional) : '--';

      return (
        '<tr class="trade-row hover:bg-slate-800/40 transition-colors border-b border-slate-800/60 font-mono text-[11px]">' +
          '<td class="px-3 py-2.5 text-slate-400">' +
            '<div class="text-white font-medium">' + cTime + '</div>' +
            '<div class="text-[10px] text-slate-500">开仓: ' + eTime + '</div>' +
          '</td>' +
          '<td class="px-3 py-2.5 font-sans font-black text-white">' +
            '<div class="flex items-center gap-1.5">' +
              '<a href="' + escapeHtml(tvUrl) + '" target="_blank" rel="noopener noreferrer" class="text-sky-400 hover:underline flex items-center gap-0.5">' +
                '<span>' + safeSym + '</span><span class="text-[9px] text-slate-500">↗</span>' +
              '</a>' +
              '<span class="px-1.5 py-0.2 rounded text-[9px] font-bold border ' + sideCls + '">' + escapeHtml(side) + '</span>' +
            '</div>' +
          '</td>' +
          '<td class="px-3 py-2.5 tabular-nums text-slate-300"><div>' + escapeHtml(notionalStr) + '</div><div class="text-[10px] text-slate-500">' + escapeHtml(amtStr) + '</div></td>' +
          '<td class="px-3 py-2.5 tabular-nums text-slate-300">' + escapeHtml(entryStr) + '</td>' +
          '<td class="px-3 py-2.5 tabular-nums text-white font-bold">' + escapeHtml(exitStr) + '</td>' +
          '<td class="px-3 py-2.5 tabular-nums font-bold ' + pnlCls + '"><div>' + escapeHtml(formatMoney(pnlVal, { signed: true })) + '</div></td>' +
          '<td class="px-3 py-2.5 tabular-nums font-bold ' + pnlCls + '"><div>' + escapeHtml(pctText) + '</div></td>' +
          '<td class="px-3 py-2.5 text-slate-300">' + dur + '</td>' +
          '<td class="px-3 py-2.5">' + renderCloseReasonBadge(String(h.close_reason || '平仓')) + '</td>' +
          '<td class="px-3 py-2.5 text-slate-400">' + escapeHtml(h.trendline_label || '--') + '</td>' +
        '</tr>'
      );
    }).join('');
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
    const summary = accountData.summary || {};
    const equityCurve = accountData.equity_curve || [];
    const dailyPnl = accountData.daily_pnl || [];
    const pnlDist = accountData.pnl_distribution || {};
    const bins = pnlDist.bins || [];

    if (equityCurve.length > 0) {
      const firstPt = equityCurve[0];
      const lastPt = equityCurve[equityCurve.length - 1];
      setText('equity-range-label', (firstPt.datetime || '--').slice(5, 16) + ' ~ ' + (lastPt.datetime || '--').slice(5, 16));
      let peakEq = null;
      equityCurve.forEach(pt => {
        if (isFiniteNumber(pt.equity)) {
          peakEq = peakEq === null ? Number(pt.equity) : Math.max(peakEq, Number(pt.equity));
        }
      });
      setText('equity-peak-label', '峰值: ' + formatMoney(peakEq));
      setText('equity-current-label', '当前: ' + formatMoney(lastPt.equity));
    } else {
      setText('equity-range-label', '--');
      setText('equity-peak-label', '峰值: --');
      setText('equity-current-label', '当前: --');
    }

    setText('daily-days-badge', dailyPnl.length + ' 天');

    const winCount = isFiniteNumber(summary.win_count) ? Number(summary.win_count) : 0;
    const lossCount = isFiniteNumber(summary.loss_count) ? Number(summary.loss_count) : 0;
    const beCount = isFiniteNumber(summary.breakeven_count) ? Number(summary.breakeven_count) : 0;
    setText('win-loss-summary-badge', winCount + '胜 / ' + lossCount + '负 / ' + beCount + '平');

    const totalBinned = bins.reduce((sum, b) => sum + (isFiniteNumber(b.count) ? Number(b.count) : 0), 0);
    setText('dist-summary-badge', totalBinned + ' 笔样本');

    if (typeof Chart === 'undefined') return;

    Chart.defaults.color = '#94a3b8';
    Chart.defaults.font.family = '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "JetBrains Mono", sans-serif';
    Chart.defaults.borderColor = 'rgba(51, 65, 85, 0.35)';

    const tooltipConfig = {
      backgroundColor: 'rgba(15, 23, 42, 0.95)',
      titleColor: '#f8fafc',
      bodyColor: '#cbd5e1',
      borderColor: 'rgba(51, 65, 85, 0.8)',
      borderWidth: 1,
      padding: 10,
      boxPadding: 4,
      usePointStyle: true
    };

    const eqCanvas = document.getElementById('chart-equity-curve');
    if (eqCanvas) {
      const eqLabels = equityCurve.map(p => (p.datetime || '--').slice(5, 16));
      const eqValues = equityCurve.map(p => (isFiniteNumber(p.equity) ? Number(p.equity) : null));
      const hasInitialCapital = isFiniteNumber(summary.initial_capital);
      const datasets = [{
        label: '账户总资产 ($)',
        data: eqValues,
        borderColor: '#38bdf8',
        backgroundColor: function(c) {
          const chart = c.chart;
          const ctx = chart.ctx;
          const chartArea = chart.chartArea;
          if (!chartArea) return null;
          const g = ctx.createLinearGradient(0, chartArea.top, 0, chartArea.bottom);
          g.addColorStop(0, 'rgba(56, 189, 248, 0.25)');
          g.addColorStop(1, 'rgba(56, 189, 248, 0.0)');
          return g;
        },
        fill: true,
        tension: 0.25,
        pointRadius: eqValues.length > 20 ? 1 : 4,
        pointBackgroundColor: '#38bdf8',
        borderWidth: 2.5
      }];
      if (hasInitialCapital) {
        const cap = Number(summary.initial_capital);
        datasets.push({
          label: '本金基准线 ($)',
          data: equityCurve.map(() => cap),
          borderColor: '#64748b',
          borderDash: [5, 5],
          borderWidth: 1.5,
          pointRadius: 0,
          fill: false
        });
      }
      upsertChart('eq', eqCanvas, {
        type: 'line',
        data: { labels: eqLabels, datasets: datasets },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            tooltip: {
              ...tooltipConfig,
              callbacks: {
                label: c => c.dataset.label + ': ' + formatMoney(c.raw)
              }
            },
            legend: { position: 'top', labels: { boxWidth: 12, font: { size: 11 } } }
          },
          scales: {
            x: { grid: { display: false }, ticks: { maxTicksLimit: 8, font: { size: 10 } } },
            y: { grid: { color: 'rgba(51, 65, 85, 0.25)' }, ticks: { font: { size: 10 }, callback: v => '$' + Number(v).toLocaleString('en-US') } }
          }
        }
      });
    }

    const dailyCanvas = document.getElementById('chart-daily-pnl');
    if (dailyCanvas) {
      const dLabels = dailyPnl.map(d => d.date);
      const dValues = dailyPnl.map(d => (isFiniteNumber(d.net_pnl) ? Number(d.net_pnl) : 0));
      upsertChart('daily', dailyCanvas, {
        type: 'bar',
        data: {
          labels: dLabels,
          datasets: [{
            label: '当日净盈亏 ($)',
            data: dValues,
            backgroundColor: dValues.map(v => (v >= 0 ? '#10b981' : '#f43f5e')),
            borderRadius: 4,
            borderSkipped: false
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            tooltip: {
              ...tooltipConfig,
              callbacks: {
                label: c => {
                  const list = (currentAccountData && currentAccountData.daily_pnl) || [];
                  const item = list[c.dataIndex];
                  const cnt = item && isFiniteNumber(item.trades_count) ? item.trades_count : '--';
                  return ['当日净盈亏: ' + formatMoney(c.raw, { signed: true }), '成交笔数: ' + cnt + ' 笔'];
                }
              }
            },
            legend: { display: false }
          },
          scales: {
            x: { grid: { display: false }, ticks: { font: { size: 10 } } },
            y: { grid: { color: 'rgba(51, 65, 85, 0.25)' }, ticks: { font: { size: 10 }, callback: v => '$' + v } }
          }
        }
      });
    }

    const winLossCanvas = document.getElementById('chart-win-loss');
    if (winLossCanvas) {
      upsertChart('winLoss', winLossCanvas, {
        type: 'doughnut',
        data: {
          labels: ['盈利', '亏损', '保本'],
          datasets: [{
            data: [winCount, lossCount, beCount],
            backgroundColor: ['#10b981', '#f43f5e', '#64748b'],
            borderColor: '#0f172a',
            borderWidth: 2
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          cutout: '62%',
          plugins: {
            tooltip: tooltipConfig,
            legend: { position: 'bottom', labels: { boxWidth: 12, font: { size: 11 } } }
          }
        }
      });
    }

    const distCanvas = document.getElementById('chart-pnl-dist');
    if (distCanvas) {
      const binLabels = bins.map(b => b.label);
      const binCounts = bins.map(b => (isFiniteNumber(b.count) ? Number(b.count) : 0));
      const binColors = bins.map(b => {
        if (isFiniteNumber(b.max) && Number(b.max) <= -0.0001) return '#f43f5e';
        if (isFiniteNumber(b.min) && Number(b.min) >= 0.0001) return '#10b981';
        return '#64748b';
      });
      upsertChart('dist', distCanvas, {
        type: 'bar',
        data: {
          labels: binLabels,
          datasets: [{
            label: '交易笔数',
            data: binCounts,
            backgroundColor: binColors,
            borderRadius: 4,
            borderSkipped: false
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: {
            tooltip: {
              ...tooltipConfig,
              callbacks: {
                label: c => {
                  const curBins = (currentAccountData && currentAccountData.pnl_distribution && currentAccountData.pnl_distribution.bins) || [];
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
              grid: { color: 'rgba(51, 65, 85, 0.25)' },
              ticks: { precision: 0, font: { size: 10 } }
            }
          }
        }
      });
    }
  }

  function renderIncomeBanner(accountData) {
    const existing = document.getElementById('income-page-full-banner');
    const bd = accountData.income_breakdown;
    if (bd && bd.page_full) {
      const cStart = escapeHtml(bd.coverage_start || '--');
      const cEnd = escapeHtml(bd.coverage_end || '--');
      const textHtml = '<span>已达单页上限，更早流水未纳入 (流水覆盖区间: ' + cStart + ' 至 ' + cEnd + ')</span>';
      if (!existing) {
        const banner = document.createElement('div');
        banner.id = 'income-page-full-banner';
        banner.className = 'px-3.5 py-2.5 rounded-xl bg-amber-500/10 border border-amber-500/30 text-xs text-amber-300 flex items-center justify-between';
        banner.innerHTML = textHtml;
        const mainContent = document.getElementById('account-report');
        const kpiSection = document.getElementById('kpi-matrix-section');
        if (mainContent && kpiSection) {
          mainContent.insertBefore(banner, kpiSection);
        }
      } else {
        existing.classList.remove('hidden');
        existing.innerHTML = textHtml;
      }
    } else if (existing) {
      existing.classList.add('hidden');
    }
  }

  function renderDashboard(accountData) {
    if (!accountData) return;
    currentAccountData = accountData;

    const summary = accountData.summary || {};
    const positions = accountData.active_positions || [];

    setText('sync-time-text', accountData.generated_at || '--');
    updateApiStatusBadge(accountData, false);
    renderIncomeBanner(accountData);
    updateKpis(summary, positions);
    renderActivePositions(positions);
    renderHistoryTable();
    updateCharts(accountData);
  }

  function bindControlsOnce() {
    const searchInput = document.getElementById('history-search-input');
    if (searchInput && !searchInput.dataset.bound) {
      searchInput.dataset.bound = 'true';
      searchInput.addEventListener('input', renderHistoryTable);
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
        if (searchInput) searchInput.value = '';
        if (outcomeSelect) outcomeSelect.value = '';
        renderHistoryTable();
      });
    }

    document.querySelectorAll('#trade-history-table th.sortable').forEach(th => {
      if (th.dataset.bound) return;
      th.dataset.bound = 'true';
      th.addEventListener('click', function() {
        const col = this.dataset.sort;
        if (!col) return;
        if (HISTORY_SORT.column === col) {
          HISTORY_SORT.asc = !HISTORY_SORT.asc;
        } else {
          HISTORY_SORT.column = col;
          HISTORY_SORT.asc = col === 'symbol';
        }
        renderHistoryTable();
      });
    });

    const refreshBtn = document.getElementById('btn-manual-refresh');
    if (refreshBtn && !refreshBtn.dataset.bound) {
      refreshBtn.dataset.bound = 'true';
      refreshBtn.addEventListener('click', function() {
        const spinner = document.getElementById('refresh-spinner');
        if (spinner) spinner.style.transform = 'rotate(360deg)';
        tryLoadData().finally(() => {
          if (spinner) spinner.style.transform = '';
        });
      });
    }
  }

  async function tryLoadData() {
    if (refreshInFlight) return;
    refreshInFlight = true;
    const status = document.getElementById('account-load-status');
    const report = document.getElementById('account-report');
    try {
      const headers = {};
      if (lastEtag) headers['If-None-Match'] = lastEtag;
      const response = await fetch('api/account', { cache: 'no-store', headers });
      if (response.status === 304) {
        if (currentAccountData) updateApiStatusBadge(currentAccountData, false);
        if (status) status.hidden = true;
        return;
      }
      if (!response.ok) throw new Error('HTTP ' + response.status);
      const etag = response.headers.get('ETag');
      if (etag) lastEtag = etag;
      const snapshot = await response.text();
      if (snapshot !== latestSnapshot) {
        const data = JSON.parse(snapshot);
        if (report) report.hidden = false;
        renderDashboard(data);
        latestSnapshot = snapshot;
      } else if (currentAccountData) {
        updateApiStatusBadge(currentAccountData, false);
      }
      if (status) status.hidden = true;
    } catch (error) {
      console.error('Failed to load account snapshot:', error);
      if (report) report.hidden = !latestSnapshot;
      updateApiStatusBadge(currentAccountData, true);
      if (status) {
        status.hidden = false;
        status.className = 'px-3 py-2 text-sm rounded-lg bg-rose-500/10 border border-rose-500/30 text-rose-300';
        status.textContent = latestSnapshot
          ? ('账户数据刷新失败 (' + error.message + ')，当前展示上次快照。')
          : ('暂时无法加载账户数据 (' + error.message + ')，将自动重试。');
      }
    } finally {
      refreshInFlight = false;
    }
  }

  function init() {
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
    if (!document.hidden) tryLoadData();
  });
})();
