(function() {
  let chartInstances = {};

  function formatMoney(num) {
    if (num === null || num === undefined || isNaN(num)) return '$0.00';
    return (num < 0 ? '-$' : '$') + Math.abs(num).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function formatPct(num) {
    if (num === null || num === undefined || isNaN(num)) return '0.00%';
    return (num > 0 ? '+' : '') + num.toFixed(2) + '%';
  }

  function formatCryptoPrice(val) {
    if (val === null || val === undefined || isNaN(val) || val <= 0) return '--';
    if (val >= 100) return val.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    if (val >= 1) return val.toFixed(4);
    if (val >= 0.0001) return val.toFixed(6);
    return val.toFixed(8);
  }

  function resolveApiUrl() {
    if (window.location.pathname.startsWith('/ai-coin-selector')) {
      return '/ai-coin-selector/api/account';
    }
    return 'api/account';
  }

  function renderDashboard(accountData) {
    if (!accountData) return;
    const summary = accountData.summary || {};
    const positions = accountData.active_positions || [];
    const history = accountData.trade_history || [];
    const equityCurve = accountData.equity_curve || [];
    const dailyPnl = accountData.daily_pnl || [];

    const syncTimeText = document.getElementById('sync-time-text');
    if (syncTimeText && accountData.generated_at) {
      syncTimeText.textContent = accountData.generated_at;
    }

    const apiStatusBadge = document.getElementById('api-status-badge');
    if (apiStatusBadge) {
      const mode = (accountData.api_status && accountData.api_status.mode) || accountData.data_source || '';
      if (mode.includes('REALTIME') || mode.includes('API') || mode.includes('BINANCE')) {
        apiStatusBadge.className = 'px-2 py-0.5 text-[11px] font-semibold rounded-md bg-emerald-500/10 text-emerald-300 border border-emerald-500/20 flex items-center gap-1.5';
        apiStatusBadge.innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse"></span><span>🟢 币安实盘在线</span>';
      } else {
        apiStatusBadge.className = 'px-2 py-0.5 text-[11px] font-semibold rounded-md bg-amber-500/10 text-amber-300 border border-amber-500/20 flex items-center gap-1.5';
        apiStatusBadge.innerHTML = '<span class="w-1.5 h-1.5 rounded-full bg-amber-400"></span><span>离线风控同步</span>';
      }
    }

    const incomeNotice = document.getElementById('income-page-full-banner');
    if (accountData.income_breakdown && accountData.income_breakdown.page_full) {
      const cStart = accountData.income_breakdown.coverage_start || '';
      const cEnd = accountData.income_breakdown.coverage_end || '';
      if (!incomeNotice) {
        const banner = document.createElement('div');
        banner.id = 'income-page-full-banner';
        banner.className = 'px-3.5 py-2.5 rounded-xl bg-amber-500/10 border border-amber-500/30 text-xs text-amber-300 flex items-center justify-between';
        banner.innerHTML = `<span>⚠️ 已达单页上限，更早流水未纳入 (流水覆盖区间: ${cStart} 至 ${cEnd})</span>`;
        const mainContent = document.getElementById('account-report');
        const kpiSection = document.getElementById('kpi-matrix-section');
        if (mainContent && kpiSection) {
          mainContent.insertBefore(banner, kpiSection);
        }
      } else {
        incomeNotice.classList.remove('hidden');
        incomeNotice.innerHTML = `<span>⚠️ 已达单页上限，更早流水未纳入 (流水覆盖区间: ${cStart} 至 ${cEnd})</span>`;
      }
    } else if (incomeNotice) {
      incomeNotice.classList.add('hidden');
    }

    const elEq = document.getElementById('kpi-total-equity');
    if (elEq) elEq.textContent = formatMoney(summary.total_equity);

    const elAvail = document.getElementById('kpi-available-margin');
    if (elAvail) elAvail.textContent = formatMoney(summary.available_margin);

    const elPosMargin = document.getElementById('kpi-position-margin');
    if (elPosMargin) elPosMargin.textContent = formatMoney(summary.position_margin);

    const elOpenPos = document.getElementById('kpi-open-positions');
    if (elOpenPos) elOpenPos.textContent = positions.length + ' 笔在途';

    const elRealized = document.getElementById('kpi-realized-pnl');
    if (elRealized) {
      const pnl = summary.net_realized_pnl !== undefined ? summary.net_realized_pnl : (summary.cumulative_realized_pnl || 0);
      elRealized.textContent = (pnl > 0 ? '+' : '') + formatMoney(pnl);
      elRealized.className = 'text-xl md:text-2xl font-black font-mono tabular-nums tracking-tight ' + (pnl >= 0 ? 'text-emerald-400' : 'text-rose-400');
    }

    const elUnrealized = document.getElementById('kpi-unrealized-pnl');
    if (elUnrealized) {
      const u = summary.total_unrealized_pnl || 0;
      elUnrealized.textContent = (u > 0 ? '+' : '') + formatMoney(u);
      elUnrealized.className = 'text-xl md:text-2xl font-black font-mono tabular-nums tracking-tight ' + (u >= 0 ? 'text-emerald-400' : 'text-rose-400');
    }

    const elRet = document.getElementById('kpi-total-return');
    if (elRet) {
      const r = summary.total_return_pct || 0;
      elRet.textContent = formatPct(r);
      elRet.className = 'text-xl md:text-2xl font-black font-mono tabular-nums tracking-tight ' + (r >= 0 ? 'text-emerald-400' : 'text-rose-400');
    }

    const elWr = document.getElementById('kpi-win-rate');
    if (elWr) elWr.textContent = (summary.win_rate_pct || 0).toFixed(1) + '%';

    const elPf = document.getElementById('kpi-profit-factor');
    if (elPf) {
      const pf = summary.profit_factor || 0;
      elPf.textContent = pf >= 999.0 ? '∞ (全胜)' : pf.toFixed(2);
    }

    const elMdd = document.getElementById('kpi-max-drawdown');
    if (elMdd) elMdd.textContent = (summary.max_drawdown_pct || 0).toFixed(2) + '%';

    const elSharpe = document.getElementById('kpi-sharpe-ratio');
    if (elSharpe) elSharpe.textContent = (summary.sharpe_ratio || 0).toFixed(2);

    const elSortino = document.getElementById('kpi-sortino-ratio');
    if (elSortino) {
      const so = summary.sortino_ratio || 0;
      elSortino.textContent = so >= 999.0 ? '∞' : so.toFixed(2);
    }

    const elTrades = document.getElementById('kpi-total-trades');
    if (elTrades) {
      const count = summary.closed_trades || summary.total_closed_trades || summary.total_trades || history.length || 0;
      elTrades.innerHTML = count + ' <span class="text-xs font-normal text-slate-400 font-sans">笔</span>';
    }

    const posTbody = document.getElementById('active-positions-tbody');
    const posCount = document.getElementById('active-positions-count');
    if (posCount) posCount.textContent = positions.length + ' 活跃仓位';
    const posEmpty = document.getElementById('active-positions-empty');
    if (posEmpty) posEmpty.classList.toggle('hidden', positions.length > 0);
    const posWrap = document.getElementById('active-positions-table-wrap');
    if (posWrap) posWrap.classList.toggle('hidden', positions.length === 0);

    if (posTbody) {
      if (positions.length === 0) {
        posTbody.innerHTML = '<tr><td colspan="10" class="py-6 text-center text-slate-500 font-sans">当前在岸安全，无活跃持仓</td></tr>';
      } else {
        posTbody.innerHTML = positions.map(p => {
          const sym = p.symbol || '--';
          const side = p.side || 'LONG';
          const sideBadge = side === 'SHORT'
            ? '<span class="px-1.5 py-0.2 rounded text-[9px] font-bold bg-rose-500/20 text-rose-300 border border-rose-500/30">SHORT</span>'
            : '<span class="px-1.5 py-0.2 rounded text-[9px] font-bold bg-emerald-500/20 text-emerald-300 border border-emerald-500/30">LONG</span>';
          const pnl = p.unrealized_pnl || 0;
          const roe = p.roe_pct || 0;
          const pnlCls = pnl >= 0 ? 'text-emerald-400' : 'text-rose-400';
          const tvUrl = 'https://www.tradingview.com/chart/ig0LuDL8/?symbol=BINANCE:' + sym + '.P';
          const notional = p.notional || (p.entry_price * p.amount) || 0;
          return `
            <tr class="hover:bg-slate-800/40 transition-colors border-b border-slate-800/60 font-mono text-xs">
              <td class="px-3 py-2.5 font-bold font-sans text-white">
                <div class="flex items-center gap-1.5">
                  <a href="${tvUrl}" target="_blank" class="text-sky-400 hover:underline flex items-center gap-1">
                    <span>${sym}</span><span class="text-[9px] text-slate-500">↗</span>
                  </a>
                  ${sideBadge}
                </div>
              </td>
              <td class="px-3 py-2.5 tabular-nums text-slate-300">${p.leverage || 5}x · $${Number(notional).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}<div class="text-[10px] text-slate-500">${Number(p.amount || 0).toLocaleString()} 币</div></td>
              <td class="px-3 py-2.5 tabular-nums text-slate-300">$${formatCryptoPrice(p.entry_price)}</td>
              <td class="px-3 py-2.5 tabular-nums text-white font-bold">$${formatCryptoPrice(p.mark_price || p.entry_price)}</td>
              <td class="px-3 py-2.5 tabular-nums font-bold ${pnlCls}"><div>${pnl > 0 ? '+' : ''}$${pnl.toFixed(2)}</div><div class="text-[10px]">${roe > 0 ? '+' : ''}${roe.toFixed(2)}% ROE</div></td>
              <td class="px-3 py-2.5 tabular-nums text-rose-400">$${formatCryptoPrice(p.liquidation_price)}</td>
              <td class="px-3 py-2.5 text-slate-400">${p.trailing_stop_triggered ? '已触发' : '未触发'}</td>
              <td class="px-3 py-2.5 text-slate-400">${p.stop_loss_notified ? '已提醒' : '--'}</td>
              <td class="px-3 py-2.5 text-slate-400">${p.trendline_id || '--'}</td>
            </tr>
          `;
        }).join('');
      }
    }

    const histTbody = document.getElementById('trade-history-tbody');
    const visibleCountEl = document.getElementById('visible-trades-count');
    const wins = history.filter(h => (h.net_pnl !== undefined ? h.net_pnl : h.pnl || 0) > 0.0001).length;
    const losses = history.filter(h => (h.net_pnl !== undefined ? h.net_pnl : h.pnl || 0) < -0.0001).length;
    const netPnl = history.reduce((sum, h) => sum + (h.net_pnl !== undefined ? h.net_pnl : h.pnl || 0), 0);
    const hcBadge = document.getElementById('history-count-badge');
    if (hcBadge) hcBadge.textContent = '共 ' + history.length + ' 笔记录';
    const hwBadge = document.getElementById('history-winrate-badge');
    if (hwBadge) hwBadge.textContent = '胜率: ' + (history.length ? wins / history.length * 100 : 0).toFixed(1) + '% (' + wins + '胜 / ' + losses + '负)';
    const hpBadge = document.getElementById('history-pnl-badge');
    if (hpBadge) hpBadge.textContent = '净盈亏: ' + formatMoney(netPnl);
    const totalCountEl = document.getElementById('total-trades-count');
    if (totalCountEl) totalCountEl.textContent = history.length;

    if (histTbody) {
      if (history.length === 0) {
        histTbody.innerHTML = '<tr><td colspan="10" class="py-8 text-center text-slate-500">暂无开平仓流水记录</td></tr>';
        if (visibleCountEl) visibleCountEl.textContent = '0';
      } else {
        histTbody.innerHTML = history.map(h => {
          const sym = h.symbol || '--';
          const pnl = h.net_pnl !== undefined ? h.net_pnl : (h.realized_pnl !== undefined ? h.realized_pnl : (h.pnl || 0));
          const pnlPct = h.pnl_pct == null ? null : Number(h.pnl_pct);
          const pnlCls = pnl >= 0 ? 'text-emerald-400' : 'text-rose-400';
          const pnlSign = pnl > 0 ? '+' : (pnl < 0 ? '-' : '');
          const pctText = pnlPct == null ? '--' : (pnlPct > 0 ? '+' : '') + pnlPct.toFixed(2) + '%';
          const cTime = h.closed_at || '--';
          const eTime = h.entry_time || '--';
          const dur = h.duration_str || h.holding_duration || '--';
          const reason = h.close_reason || '平仓';
          let statusTag = 'manual';
          let badgeHtml = '<span class="px-2 py-0.5 rounded text-[10px] font-bold bg-slate-800 text-slate-300 border border-slate-700">🖐️ 战法手动平仓</span>';
          if (reason.includes('保本') || reason.includes('锁利')) {
            statusTag = 'breakeven';
            badgeHtml = '<span class="px-2 py-0.5 rounded text-[10px] font-bold bg-emerald-500/20 text-emerald-300 border border-emerald-500/30">🎯 动态移动保本</span>';
          } else if (reason.includes('止损') || reason.includes('跌破')) {
            statusTag = 'stoploss';
            badgeHtml = '<span class="px-2 py-0.5 rounded text-[10px] font-bold bg-rose-500/20 text-rose-300 border border-rose-500/30">🛡️ 破线止损清仓</span>';
          } else if (reason.includes('实盘') || reason.includes('BINANCE')) {
            statusTag = 'binance';
            badgeHtml = '<span class="px-2 py-0.5 rounded text-[10px] font-bold bg-indigo-500/20 text-indigo-300 border border-indigo-500/30">⚡ 币安实盘平仓</span>';
          }
          const winLossTag = pnl > 0.0001 ? 'win' : (pnl < -0.0001 ? 'loss' : 'breakeven');
          const tvUrl = 'https://www.tradingview.com/chart/ig0LuDL8/?symbol=BINANCE:' + sym + '.P';
          const entryStr = h.entry_price > 0 ? '$' + formatCryptoPrice(h.entry_price) : '--';
          const exitStr = h.exit_price > 0 ? '$' + formatCryptoPrice(h.exit_price) : '--';
          const amtStr = h.amount > 0 ? Number(h.amount).toLocaleString() + ' 币' : '--';
          const notionalStr = h.entry_notional > 0 ? '$' + Number(h.entry_notional).toFixed(2) : '--';

          return `
            <tr class="trade-row hover:bg-slate-800/40 transition-colors border-b border-slate-800/60 font-mono text-[11px]"
                data-symbol="${sym}"
                data-winloss="${winLossTag}"
                data-status="${statusTag}"
                data-pnl="${pnl}"
                data-pnlpct="${pnlPct == null ? '' : pnlPct}"
                data-closetime="${cTime}">
              <td class="px-3 py-2.5 text-slate-400">
                <div class="text-white font-medium">${cTime}</div>
                <div class="text-[10px] text-slate-500">开仓: ${eTime}</div>
              </td>
              <td class="px-3 py-2.5 font-sans font-black text-white">
                <div class="flex items-center gap-1.5">
                  <a href="${tvUrl}" target="_blank" class="text-sky-400 hover:underline flex items-center gap-0.5">
                    <span>${sym}</span><span class="text-[9px] text-slate-500">↗</span>
                  </a>
                  <span class="px-1.5 py-0.2 rounded text-[9px] font-bold bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">${h.side || 'LONG'}</span>
                </div>
              </td>
              <td class="px-3 py-2.5 tabular-nums text-slate-300">
                <div>${notionalStr}</div><div class="text-[10px] text-slate-500">${amtStr}</div>
              </td>
              <td class="px-3 py-2.5 tabular-nums text-slate-300">${entryStr}</td>
              <td class="px-3 py-2.5 tabular-nums text-white font-bold">${exitStr}</td>
              <td class="px-3 py-2.5 tabular-nums font-bold ${pnlCls}"><div>${pnlSign}$${Math.abs(pnl).toFixed(2)}</div></td>
              <td class="px-3 py-2.5 tabular-nums font-bold ${pnlCls}"><div>${pctText}</div></td>
              <td class="px-3 py-2.5 text-slate-300">${dur}</td>
              <td class="px-3 py-2.5">${badgeHtml}</td>
              <td class="px-3 py-2.5 text-slate-400">${h.trendline_label || '--'}</td>
            </tr>
          `;
        }).join('');
        if (visibleCountEl) visibleCountEl.textContent = history.length;
      }
    }

    if (typeof Chart !== 'undefined') {
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

      const ctxEq = document.getElementById('chart-equity-curve');
      if (ctxEq) {
        if (chartInstances.eq) chartInstances.eq.destroy();
        const eqLabels = equityCurve.map(p => (p.datetime || String(p.timestamp || '')).slice(5, 16));
        const eqValues = equityCurve.map(p => p.equity);
        const benchValues = equityCurve.map(p => p.benchmark || summary.initial_capital || 1000);

        chartInstances.eq = new Chart(ctxEq, {
          type: 'line',
          data: {
            labels: eqLabels,
            datasets: [
              {
                label: '账户总资产 ($)',
                data: eqValues,
                borderColor: '#38bdf8',
                backgroundColor: function(c) {
                  const chart = c.chart;
                  const { ctx, chartArea } = chart;
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
              },
              {
                label: '本金基准线 ($)',
                data: benchValues,
                borderColor: '#64748b',
                borderDash: [5, 5],
                borderWidth: 1.5,
                pointRadius: 0,
                fill: false
              }
            ]
          },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
              tooltip: {
                ...tooltipConfig,
                callbacks: {
                  label: c => c.dataset.label + ': $' + Number(c.raw).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
                }
              },
              legend: { position: 'top', labels: { boxWidth: 12, font: { size: 11 } } }
            },
            scales: {
              x: { grid: { display: false }, ticks: { maxTicksLimit: 8, font: { size: 10 } } },
              y: { grid: { color: 'rgba(51, 65, 85, 0.25)' }, ticks: { font: { size: 10 }, callback: v => '$' + v.toLocaleString() } }
            }
          }
        });
      }

      const ctxDd = document.getElementById('chart-pnl-drawdown');
      if (ctxDd) {
        if (chartInstances.dd) chartInstances.dd.destroy();
        const ddLabels = equityCurve.map(p => (p.datetime || String(p.timestamp || '')).slice(5, 16));
        const cumPnlValues = equityCurve.map(p => p.cumulative_pnl || 0);
        const ddValues = equityCurve.map(p => -Math.abs(p.drawdown_pct || 0));

        chartInstances.dd = new Chart(ctxDd, {
          type: 'line',
          data: {
            labels: ddLabels,
            datasets: [
              {
                label: '累计实现盈亏 ($)',
                data: cumPnlValues,
                borderColor: '#10b981',
                backgroundColor: 'rgba(16, 185, 129, 0.08)',
                fill: true,
                tension: 0.2,
                pointRadius: cumPnlValues.length > 20 ? 1 : 3,
                borderWidth: 2,
                yAxisID: 'y'
              },
              {
                label: '水下回撤 (%)',
                data: ddValues,
                borderColor: '#f43f5e',
                backgroundColor: 'rgba(244, 63, 94, 0.25)',
                fill: true,
                tension: 0.2,
                pointRadius: 0,
                borderWidth: 1.5,
                yAxisID: 'y1'
              }
            ]
          },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
              tooltip: {
                ...tooltipConfig,
                callbacks: {
                  label: c => c.datasetIndex === 0 ? '累计盈亏: $' + Number(c.raw).toFixed(2) : '水下回撤: ' + Number(c.raw).toFixed(2) + '%'
                }
              },
              legend: { position: 'top', labels: { boxWidth: 12, font: { size: 11 } } }
            },
            scales: {
              x: { grid: { display: false }, ticks: { maxTicksLimit: 8, font: { size: 10 } } },
              y: { type: 'linear', position: 'left', grid: { color: 'rgba(51, 65, 85, 0.25)' }, ticks: { font: { size: 10 }, callback: v => '$' + v } },
              y1: { type: 'linear', position: 'right', grid: { display: false }, max: 0, ticks: { font: { size: 10 }, callback: v => v + '%' } }
            }
          }
        });
      }

      const ctxDaily = document.getElementById('chart-daily-pnl');
      if (ctxDaily) {
        if (chartInstances.daily) chartInstances.daily.destroy();
        const dLabels = dailyPnl.map(d => d.date);
        const dValues = dailyPnl.map(d => d.net_pnl ?? d.pnl ?? 0);
        const bgColors = dValues.map(v => v >= 0 ? '#10b981' : '#f43f5e');

        chartInstances.daily = new Chart(ctxDaily, {
          type: 'bar',
          data: {
            labels: dLabels,
            datasets: [{
              label: '当日盈亏 ($)',
              data: dValues,
              backgroundColor: bgColors,
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
                    const item = dailyPnl[c.dataIndex];
                    return ['当日盈亏: $' + Number(c.raw).toFixed(2), '成交笔数: ' + (item ? (item.trades_count ?? item.trade_count ?? 0) : 0) + ' 笔'];
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

      const ctxDist = document.getElementById('chart-pnl-dist');
      if (ctxDist) {
        if (chartInstances.dist) chartInstances.dist.destroy();
        const scatterData = history.map((h, i) => ({
          x: i + 1,
          y: h.pnl_pct,
          symbol: h.symbol,
          pnl: h.net_pnl !== undefined ? h.net_pnl : (h.realized_pnl || 0)
        })).filter(point => point.y != null);

        chartInstances.dist = new Chart(ctxDist, {
          type: 'scatter',
          data: {
            datasets: [{
              label: '单笔盈亏率 (%)',
              data: scatterData,
              backgroundColor: c => (c.raw && c.raw.y >= 0) ? '#10b981' : '#f43f5e',
              pointRadius: 6,
              pointHoverRadius: 8
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
                    const item = c.raw;
                    return ['标的: ' + item.symbol + ' (第 ' + item.x + ' 笔)', '盈亏率: ' + item.y.toFixed(2) + '%', '盈亏金额: $' + Number(item.pnl).toFixed(2)];
                  }
                }
              },
              legend: { display: false }
            },
            scales: {
              x: { title: { display: true, text: '交易序号', font: { size: 10 } }, grid: { color: 'rgba(51, 65, 85, 0.2)' }, ticks: { font: { size: 10 } } },
              y: { title: { display: true, text: '单笔收益率 (%)', font: { size: 10 } }, grid: { color: 'rgba(51, 65, 85, 0.25)' }, ticks: { font: { size: 10 }, callback: v => v + '%' } }
            }
          }
        });
      }
    }

    initTableFilters();
  }

  function initTableFilters() {
    const searchInput = document.getElementById('search-input');
    const filterBtns = document.querySelectorAll('.filter-btn');
    let currentFilter = Array.from(filterBtns).find(btn => btn.classList.contains('active'))?.dataset.filter || 'all';

    function applyFilters() {
      const tableRows = document.querySelectorAll('#trade-history-tbody .trade-row');
      const visibleCountEl = document.getElementById('visible-trades-count');
      const query = (searchInput ? searchInput.value : '').trim().toLowerCase();
      let visibleCount = 0;

      tableRows.forEach(row => {
        const sym = (row.dataset.symbol || '').toLowerCase();
        const winloss = row.dataset.winloss || '';
        const status = row.dataset.status || '';
        const text = row.textContent.toLowerCase();

        const matchesQuery = !query || text.includes(query) || sym.includes(query);
        let matchesFilter = true;
        if (currentFilter === 'win') matchesFilter = (winloss === 'win');
        else if (currentFilter === 'loss') matchesFilter = (winloss === 'loss');
        else if (currentFilter === 'breakeven') matchesFilter = (status === 'breakeven');
        else if (currentFilter === 'stoploss') matchesFilter = (status === 'stoploss');

        if (matchesQuery && matchesFilter) {
          row.style.display = '';
          visibleCount++;
        } else {
          row.style.display = 'none';
        }
      });

      if (visibleCountEl) visibleCountEl.textContent = visibleCount;
    }

    if (searchInput && !searchInput.dataset.bound) {
      searchInput.dataset.bound = 'true';
      searchInput.addEventListener('input', applyFilters);
    }

    filterBtns.forEach(btn => {
      if (!btn.dataset.bound) {
        btn.dataset.bound = 'true';
        btn.addEventListener('click', function() {
          filterBtns.forEach(b => {
            b.classList.remove('active', 'bg-sky-500/20', 'text-sky-300', 'border', 'border-sky-500/30');
            b.classList.add('text-slate-400');
          });
          this.classList.add('active', 'bg-sky-500/20', 'text-sky-300', 'border', 'border-sky-500/30');
          this.classList.remove('text-slate-400');
          currentFilter = this.dataset.filter || 'all';
          applyFilters();
        });
      }
    });

    applyFilters();

    const sortableHeaders = document.querySelectorAll('th.sortable');
    let currentSort = { column: null, asc: false };
    sortableHeaders.forEach(th => {
      if (!th.dataset.bound) {
        th.dataset.bound = 'true';
        th.addEventListener('click', function() {
          const col = this.dataset.sort;
          if (currentSort.column === col) {
            currentSort.asc = !currentSort.asc;
          } else {
            currentSort.column = col;
            currentSort.asc = false;
          }

          const tbody = document.getElementById('trade-history-tbody');
          const rows = Array.from(tbody.querySelectorAll('.trade-row'));

          rows.sort((a, b) => {
            let valA, valB;
            if (col === 'pnl') {
              valA = parseFloat(a.dataset.pnl || '0');
              valB = parseFloat(b.dataset.pnl || '0');
            } else if (col === 'pnlpct') {
              valA = parseFloat(a.dataset.pnlpct || '0');
              valB = parseFloat(b.dataset.pnlpct || '0');
            } else if (col === 'time') {
              valA = a.dataset.closetime || '';
              valB = b.dataset.closetime || '';
            } else if (col === 'symbol') {
              valA = a.dataset.symbol || '';
              valB = b.dataset.symbol || '';
            } else {
              return 0;
            }

            if (valA < valB) return currentSort.asc ? -1 : 1;
            if (valA > valB) return currentSort.asc ? 1 : -1;
            return 0;
          });

          rows.forEach(r => tbody.appendChild(r));
        });
      }
    });
  }

  let latestSnapshot = '';
  let refreshInFlight = false;
  async function tryLoadData() {
    if (refreshInFlight) return;
    refreshInFlight = true;
    const status = document.getElementById('account-load-status');
    const report = document.getElementById('account-report');
    try {
      const response = await fetch(resolveApiUrl(), { cache: 'no-store' });
      if (!response.ok) throw new Error('HTTP ' + response.status);
      const snapshot = await response.text();
      if (snapshot !== latestSnapshot) {
        const data = JSON.parse(snapshot);
        report.hidden = false;
        renderDashboard(data);
        latestSnapshot = snapshot;
      }
      if (status) status.hidden = true;
    } catch (error) {
      if (report) report.hidden = !latestSnapshot;
      if (status) {
        status.hidden = false;
        status.textContent = latestSnapshot ? '更新失败，显示上次读取的账户快照。' : '暂时无法加载账户数据，将自动重试。';
      }
    } finally {
      refreshInFlight = false;
    }
  }

  const refreshBtn = document.getElementById('btn-manual-refresh');
  if (refreshBtn) {
    refreshBtn.addEventListener('click', function() {
      const spinner = document.getElementById('refresh-spinner');
      if (spinner) spinner.style.transform = 'rotate(360deg)';
      tryLoadData().then(() => {
        if (spinner) spinner.style.transform = '';
      });
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', tryLoadData);
  } else {
    tryLoadData();
  }

  setInterval(() => {
    if (!document.hidden) tryLoadData();
  }, 30000);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) tryLoadData();
  });
})();
