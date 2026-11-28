(function (global) {
  'use strict';

  const Common = global.SecondLegCommon;
  const { isFiniteNum: isFiniteNumber } = Common;

  const MAX_EQUITY_CHART_POINTS = 500;
  const CHART_CANVAS_IDS = ['chart-equity-curve', 'chart-daily-pnl', 'chart-win-loss', 'chart-pnl-dist'];

  const chartInstances = {};
  const chartDataSignatures = {};
  let chartDefaultsConfigured = false;
  let cachedEqGradient = null;
  let cachedEqGradientTop = null;
  let cachedEqGradientHeight = null;
  let cachedEqGradientCtx = null;

  function formatMoneyShort(val, options) {
    const opts = options || {};
    if (!isFiniteNumber(val)) return '--';
    const num = Number(val);
    const decimals = opts.decimals !== undefined ? opts.decimals : 2;
    const absStr = Math.abs(num).toLocaleString('en-US', {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
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
          : '#f1f5f9';
        ctx.textBaseline = isNeg ? 'top' : 'bottom';
        const yPos = isNeg ? (bar.y + 4) : (bar.y - 4);
        ctx.fillText(label, bar.x, yPos);
      });
      ctx.restore();
    },
  };

  const emptyStatePlugin = {
    id: 'emptyState',
    afterDraw(chart, _args, pluginOpts) {
      if (!pluginOpts || pluginOpts.enabled === false) return;
      const text = pluginOpts.text;
      if (!text || !chart.chartArea) return;
      const datasets = chart.data && Array.isArray(chart.data.datasets) ? chart.data.datasets : [];
      const hasData = datasets.some((ds) => {
        if (!Array.isArray(ds.data) || ds.data.length === 0) return false;
        if (pluginOpts.emptyWhenNonPositive) {
          return ds.data.some((v) => Number(v) > 0);
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
    },
  };

  function createDoughnutCenterPlugin(getAccountData) {
    return {
      id: 'doughnutCenter',
      afterDraw(chart) {
        const accountData = typeof getAccountData === 'function' ? getAccountData() : null;
        if (!accountData || !accountData.summary || !chart.chartArea) return;
        const ctx = chart.ctx;
        if (!ctx) return;
        const s = accountData.summary;
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
        ctx.fillStyle = '#9fb0c5';
        ctx.font = '500 11px "JetBrains Mono", "SF Mono", monospace';
        ctx.fillText(subStr, cx, cy + 14);
        ctx.restore();
      },
    };
  }

  function ensureChartDefaults() {
    if (chartDefaultsConfigured || typeof global.Chart === 'undefined' || !global.Chart.defaults) return;
    global.Chart.defaults.color = '#9fb0c5';
    if (global.Chart.defaults.font) {
      global.Chart.defaults.font.family = '"JetBrains Mono", "Inter", -apple-system, BlinkMacSystemFont, sans-serif';
    }
    global.Chart.defaults.borderColor = 'rgba(34, 43, 56, 0.7)';
    chartDefaultsConfigured = true;
  }

  function isPlainObject(val) {
    return Object.prototype.toString.call(val) === '[object Object]';
  }

  function syncObjectInPlace(target, source) {
    if (!isPlainObject(target) || !isPlainObject(source)) return source;
    Object.keys(target).forEach((k) => {
      if (!Object.prototype.hasOwnProperty.call(source, k)) {
        delete target[k];
      }
    });
    Object.keys(source).forEach((k) => {
      const nextVal = source[k];
      const curVal = target[k];
      if (isPlainObject(curVal) && isPlainObject(nextVal)) {
        syncObjectInPlace(curVal, nextVal);
      } else {
        target[k] = nextVal;
      }
    });
    return target;
  }

  function resetEqGradientCache() {
    cachedEqGradient = null;
    cachedEqGradientTop = null;
    cachedEqGradientHeight = null;
    cachedEqGradientCtx = null;
  }

  function destroyAllCharts() {
    Object.keys(chartInstances).forEach((key) => {
      const chart = chartInstances[key];
      if (chart && typeof chart.destroy === 'function') {
        chart.destroy();
      }
      delete chartInstances[key];
      delete chartDataSignatures[key];
    });
    resetEqGradientCache();
  }

  function downsampleEquityCurve(points, maxPoints) {
    const limit = maxPoints > 2 ? maxPoints : MAX_EQUITY_CHART_POINTS;
    if (!Array.isArray(points) || points.length <= limit) return points;
    const sampled = [points[0]];
    const lastIdx = points.length - 1;
    const step = lastIdx / (limit - 1);
    let prevIdx = 0;
    for (let i = 1; i < limit - 1; i += 1) {
      const idx = Math.min(lastIdx - 1, Math.max(prevIdx + 1, Math.round(i * step)));
      if (idx > prevIdx && idx < lastIdx) {
        sampled.push(points[idx]);
        prevIdx = idx;
      }
    }
    sampled.push(points[lastIdx]);
    return sampled;
  }

  function showChartsUnavailableFallback(message) {
    const text = message || '图表组件 (Chart.js) 加载失败，暂无法渲染可视化图表，核心账本与交易数据仍可正常查看。';
    CHART_CANVAS_IDS.forEach((id) => {
      const canvas = global.document.getElementById(id);
      if (canvas) canvas.hidden = true;
      let fallback = global.document.getElementById(id + '-fallback');
      if (!fallback && canvas && canvas.parentElement && typeof global.document.createElement === 'function') {
        fallback = global.document.createElement('div');
        fallback.id = id + '-fallback';
        fallback.className = 'w-full h-full flex items-center justify-center text-center px-4 text-xs text-[#9fb0c5] font-sans bg-[#0b0e13]/80 border border-[#222b38] rounded-[4px]';
        canvas.parentElement.appendChild(fallback);
      }
      if (fallback) {
        fallback.textContent = text;
        fallback.hidden = false;
      }
    });
  }

  function hideChartsUnavailableFallback() {
    CHART_CANVAS_IDS.forEach((id) => {
      const canvas = global.document.getElementById(id);
      if (canvas) canvas.hidden = false;
      const fallback = global.document.getElementById(id + '-fallback');
      if (fallback) fallback.hidden = true;
    });
  }

  function syncChartDataInPlace(existingData, nextData) {
    if (
      !existingData ||
      !nextData ||
      existingData === nextData ||
      !Array.isArray(existingData.datasets) ||
      !Array.isArray(nextData.datasets) ||
      existingData.datasets.length !== nextData.datasets.length
    ) {
      return nextData;
    }
    existingData.labels = nextData.labels;
    for (let i = 0; i < nextData.datasets.length; i += 1) {
      const curDs = existingData.datasets[i];
      const nextDs = nextData.datasets[i];
      if (isPlainObject(curDs) && isPlainObject(nextDs)) {
        syncObjectInPlace(curDs, nextDs);
      } else {
        existingData.datasets[i] = nextDs;
      }
    }
    return existingData;
  }

  function upsertChart(key, canvas, config) {
    if (!canvas || typeof global.Chart === 'undefined') return;
    const existing = chartInstances[key];
    if (existing) {
      if (existing.canvas && existing.canvas !== canvas) {
        if (typeof existing.destroy === 'function') {
          existing.destroy();
        }
        delete chartInstances[key];
        delete chartDataSignatures[key];
      } else {
        existing.data = syncChartDataInPlace(existing.data, config.data);
        if (isPlainObject(existing.options) && isPlainObject(config.options)) {
          syncObjectInPlace(existing.options, config.options);
        } else {
          existing.options = config.options;
        }
        existing.update('none');
        return;
      }
    }
    chartInstances[key] = new global.Chart(canvas, config);
  }

  function isChartSignatureCurrent(key, canvas, signature) {
    const existing = chartInstances[key];
    return Boolean(existing && existing.canvas === canvas && chartDataSignatures[key] === signature);
  }

  function updateCharts(accountData, deps) {
    const { setText, setSignedColorClass, getAccountData } = deps;
    const summary = accountData.summary;
    const equityCurve = accountData.equity_curve;
    const dailyPnl = accountData.daily_pnl;
    const pnlDist = accountData.pnl_distribution;
    const bins = pnlDist.bins;

    let eqSigPrefix = 'empty';
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
      setText('equity-peak-label', '峰值: ' + formatMoneyShort(peakEq));
      const currEl = setText('equity-current-label', '当前: ' + formatMoneyShort(lastPt.equity));
      if (currEl && isFiniteNumber(summary.initial_capital)) {
        const netTransfers = isFiniteNumber(summary.net_external_transfers) ? Number(summary.net_external_transfers) : 0;
        const delta = Number(lastPt.equity) - Number(summary.initial_capital) - netTransfers;
        setSignedColorClass(currEl, delta);
      }
      eqSigPrefix = equityCurve.length + '|' + firstPt.datetime + '|' + lastPt.datetime + '|' + lastPt.equity + '|' + peakEq;
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

    if (typeof global.Chart === 'undefined') {
      showChartsUnavailableFallback();
      return;
    }
    hideChartsUnavailableFallback();
    ensureChartDefaults();

    const tooltipConfig = {
      backgroundColor: 'rgba(16, 21, 29, 0.96)',
      titleColor: '#f1f5f9',
      bodyColor: '#9fb0c5',
      borderColor: '#324055',
      borderWidth: 1,
      cornerRadius: 4,
      padding: 10,
      boxPadding: 4,
      usePointStyle: true,
    };

    const eqCanvas = global.document.getElementById('chart-equity-curve');
    if (eqCanvas) {
      const hasInitialCapital = isFiniteNumber(summary.initial_capital);
      const eqSig = eqSigPrefix + '|' + (hasInitialCapital ? summary.initial_capital : '') + '|' + (isFiniteNumber(summary.net_external_transfers) ? summary.net_external_transfers : '');
      if (!isChartSignatureCurrent('eq', eqCanvas, eqSig)) {
        const chartEquityCurve = downsampleEquityCurve(equityCurve, MAX_EQUITY_CHART_POINTS);
        const eqLabels = chartEquityCurve.map((p) => p.datetime.slice(5, 16));
        const eqValues = chartEquityCurve.map((p) => Number(p.equity));
        const datasets = [{
          label: '账户总资产',
          data: eqValues,
          borderColor: '#3b82f6',
          backgroundColor: function (c) {
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
          borderWidth: 1.75,
        }];
        if (hasInitialCapital) {
          const cap = Number(summary.initial_capital);
          const hasTransfers = isFiniteNumber(summary.net_external_transfers);
          const netTransfers = hasTransfers ? Number(summary.net_external_transfers) : 0;
          const baseline = cap + netTransfers;
          datasets.push({
            label: hasTransfers && netTransfers !== 0 ? '净投入基准线' : '本金基准线',
            data: chartEquityCurve.map(() => baseline),
            borderColor: '#7f91a8',
            borderDash: [4, 4],
            borderWidth: 1.2,
            pointRadius: 0,
            fill: false,
          });
        }
        upsertChart('eq', eqCanvas, {
          type: 'line',
          plugins: [emptyStatePlugin],
          data: { labels: eqLabels, datasets },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            plugins: {
              emptyState: { text: '暂无净值数据' },
              tooltip: {
                ...tooltipConfig,
                callbacks: {
                  label: (c) => c.dataset.label + ': ' + formatMoneyShort(c.raw),
                },
              },
              legend: { display: false },
            },
            scales: {
              x: { grid: { display: false }, ticks: { maxTicksLimit: 7, font: { size: 10 } } },
              y: {
                grid: { color: 'rgba(34, 43, 56, 0.6)' },
                ticks: { font: { size: 10 }, callback: formatAxisUsd },
              },
            },
          },
        });
        chartDataSignatures.eq = eqSig;
      }
    }

    const dailyCanvas = global.document.getElementById('chart-daily-pnl');
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
      const dailySig = dailyPnl.map((d) => `${d.date}:${d.net_pnl}:${d.trades_count}`).join('|');
      if (!isChartSignatureCurrent('daily', dailyCanvas, dailySig)) {
        upsertChart('daily', dailyCanvas, {
          type: 'bar',
          plugins: [barValueLabelsPlugin, emptyStatePlugin],
          data: {
            labels: dLabels,
            datasets: [{
              label: '当日净盈亏 ($)',
              data: dValues,
              backgroundColor: dValues.map((v) => (v >= 0 ? 'rgba(34, 197, 139, 0.85)' : 'rgba(242, 85, 101, 0.85)')),
              hoverBackgroundColor: dValues.map((v) => (v >= 0 ? '#22c58b' : '#f25565')),
              borderRadius: 3,
              borderSkipped: false,
              maxBarThickness: 38,
              barPercentage: 0.55,
              categoryPercentage: 0.65,
            }],
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
                },
              },
              tooltip: {
                ...tooltipConfig,
                callbacks: {
                  label: (c) => {
                    const curData = typeof getAccountData === 'function' ? getAccountData() : null;
                    const list = curData ? curData.daily_pnl : [];
                    const item = list[c.dataIndex];
                    const cnt = item && isFiniteNumber(item.trades_count) ? item.trades_count : '--';
                    return ['当日净盈亏: ' + formatMoneyShort(c.raw, { signed: true }), '成交笔数: ' + cnt + ' 笔'];
                  },
                },
              },
              legend: { display: false },
            },
            scales: {
              x: { grid: { display: false }, ticks: { maxTicksLimit: 10, font: { size: 10 } } },
              y: {
                grid: {
                  color: (ctx) => (ctx && ctx.tick && ctx.tick.value === 0 ? '#324055' : 'rgba(34, 43, 56, 0.55)'),
                  lineWidth: (ctx) => (ctx && ctx.tick && ctx.tick.value === 0 ? 1.5 : 1),
                },
                ticks: { font: { size: 10 }, callback: formatAxisUsd },
              },
            },
          },
        });
        chartDataSignatures.daily = dailySig;
      }
    }

    const winLossCanvas = global.document.getElementById('chart-win-loss');
    if (winLossCanvas) {
      const wlSig = `${winCount}|${lossCount}|${beCount}|${summary.win_rate_pct}`;
      if (!isChartSignatureCurrent('winLoss', winLossCanvas, wlSig)) {
        upsertChart('winLoss', winLossCanvas, {
          type: 'doughnut',
          plugins: [createDoughnutCenterPlugin(getAccountData)],
          data: {
            labels: ['盈利', '亏损', '保本'],
            datasets: [{
              data: [winCount, lossCount, beCount],
              backgroundColor: ['#22c58b', '#f25565', '#647894'],
              borderColor: '#10151d',
              borderWidth: 2,
              hoverOffset: 4,
            }],
          },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            cutout: '76%',
            plugins: {
              tooltip: tooltipConfig,
              legend: { position: 'bottom', labels: { boxWidth: 10, padding: 14, font: { size: 11 } } },
            },
          },
        });
        chartDataSignatures.winLoss = wlSig;
      }
    }

    const distCanvas = global.document.getElementById('chart-pnl-dist');
    if (distCanvas) {
      const distSig = bins.map((b) => `${b.label}:${b.count}:${Array.isArray(b.trades) ? b.trades.length : 0}`).join('|');
      if (!isChartSignatureCurrent('dist', distCanvas, distSig)) {
        const binLabels = bins.map((b) => b.label);
        const binCounts = bins.map((b) => Number(b.count));
        const binColors = bins.map((b) => {
          if (isFiniteNumber(b.max) && Number(b.max) <= -0.0001) return 'rgba(242, 85, 101, 0.85)';
          if (isFiniteNumber(b.min) && Number(b.min) >= 0.0001) return 'rgba(34, 197, 139, 0.85)';
          return '#647894';
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
              barPercentage: 0.6,
            }],
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
                formatter: (v) => (v > 0 ? String(v) : ''),
              },
              tooltip: {
                ...tooltipConfig,
                callbacks: {
                  label: (c) => {
                    const curData = typeof getAccountData === 'function' ? getAccountData() : null;
                    const curBins = curData && curData.pnl_distribution ? curData.pnl_distribution.bins : [];
                    const bin = curBins[c.dataIndex];
                    const syms = bin && Array.isArray(bin.trades) && bin.trades.length > 0
                      ? ' (' + bin.trades.slice(0, 5).join(', ') + (bin.trades.length > 5 ? '…' : '') + ')'
                      : '';
                    return '笔数: ' + c.raw + ' 笔' + syms;
                  },
                },
              },
              legend: { display: false },
            },
            scales: {
              x: { grid: { display: false }, ticks: { font: { size: 10 } } },
              y: {
                beginAtZero: true,
                grid: { color: 'rgba(34, 43, 56, 0.55)' },
                ticks: { precision: 0, font: { size: 10 } },
              },
            },
          },
        });
        chartDataSignatures.dist = distSig;
      }
    }
  }

  global.SecondLegAccountCharts = Object.freeze({
    ensureChartDefaults,
    destroyAllCharts,
    downsampleEquityCurve,
    showChartsUnavailableFallback,
    hideChartsUnavailableFallback,
    upsertChart,
    updateCharts,
  });
})(window);
