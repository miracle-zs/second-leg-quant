(function (global) {
  'use strict';

  const Common = global.SecondLegCommon;
  const {
    TV_LAYOUT_ID,
    TV_INTERVAL_MAP,
    escapeHtml,
    isFiniteNum,
    formatCryptoPrice,
    getTvUrl,
    fetchJsonWithTimeout,
    toBeijingDate,
  } = Common;

  const VALID_BAR_COUNTS = new Set([60, 120, 168]);

  let modalCurrentSymbol = '';
  let modalCurrentTvSymbol = '';
  let modalCurrentInterval = '15m';
  let modalVisibleBars = 120;
  let modalActiveEngine = 'native';
  let modalCandles = [];
  let modalHoverIndex = -1;
  let modalFetchController = null;
  let modalRenderRafId = null;
  let onVisibleBarsChange = null;

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
    const bj = toBeijingDate(tsMs);
    const mm = String(bj.getUTCMonth() + 1).padStart(2, '0');
    const dd = String(bj.getUTCDate()).padStart(2, '0');
    if (interval === '1d') return `${mm}/${dd}`;
    const hh = String(bj.getUTCHours()).padStart(2, '0');
    const mi = String(bj.getUTCMinutes()).padStart(2, '0');
    return `${mm}/${dd} ${hh}:${mi}`;
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
      ctx.fillText(formatCryptoPrice(p), chartW + 6, y + 3);
    }

    const n = visibleCandles.length;
    const slot = chartW / n;
    const barW = Math.max(slot * 0.68, 2);

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
      ctx.fillText(formatCryptoPrice(last.close), chartW + 6, lastY + 4);
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

      const bj = toBeijingDate(c.time);
      const timeStr = `${bj.getUTCMonth() + 1}/${bj.getUTCDate()} ${String(bj.getUTCHours()).padStart(2, '0')}:${String(bj.getUTCMinutes()).padStart(2, '0')}`;
      const chg = c.open > 0 ? (((c.close - c.open) / c.open) * 100).toFixed(2) : null;
      const isBull = c.close >= c.open;
      const chgLabel = chg !== null ? `${Number(chg) > 0 ? '+' : ''}${chg}%` : '--';

      modalTooltip.innerHTML = `
        <div class="flex items-center gap-2 mb-1">
          <span class="text-white font-bold">${escapeHtml(timeStr)}</span>
          <span class="${isBull ? 'text-emerald-400' : 'text-rose-400'} font-bold">${escapeHtml(chgLabel)}</span>
        </div>
        <div class="grid grid-cols-4 gap-x-2.5 gap-y-0.5 text-slate-400">
          <div>开: <span class="text-white">${escapeHtml(formatCryptoPrice(c.open))}</span></div>
          <div>高: <span class="text-white">${escapeHtml(formatCryptoPrice(c.high))}</span></div>
          <div>低: <span class="text-white">${escapeHtml(formatCryptoPrice(c.low))}</span></div>
          <div>收: <span class="text-white">${escapeHtml(formatCryptoPrice(c.close))}</span></div>
        </div>
        <div class="flex items-center gap-2.5 mt-1 pt-1 border-t border-slate-800 text-[10px]">
          <span class="text-amber-400">EMA5: ${escapeHtml(formatCryptoPrice(c.ema5))}</span>
          <span class="text-purple-400">EMA10: ${escapeHtml(formatCryptoPrice(c.ema10))}</span>
          <span class="text-slate-400">量: ${escapeHtml((c.volume / 1000).toFixed(1))}k</span>
        </div>
      `;
      modalTooltip.classList.remove('hidden');
    } else if (modalTooltip) {
      modalTooltip.classList.add('hidden');
    }
  }

  function clearModalChartDisplay() {
    modalCandles = [];
    modalHoverIndex = -1;
    if (modalTooltip) modalTooltip.classList.add('hidden');
    if (modalChartPrice) {
      modalChartPrice.textContent = '--';
      modalChartPrice.className = 'font-mono text-emerald-400 font-bold text-sm';
    }
    if (modalHudEma5) modalHudEma5.textContent = '--';
    if (modalHudEma10) modalHudEma10.textContent = '--';
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

  function loadTvIframe() {
    if (!modalTvIframe || !modalCurrentTvSymbol) return;
    const tvInterval = TV_INTERVAL_MAP[modalCurrentInterval];
    if (!tvInterval) {
      throw new Error(`Unsupported modal interval: ${modalCurrentInterval}`);
    }
    modalTvIframe.src = `https://s.tradingview.com/widgetembed/?symbol=${encodeURIComponent(modalCurrentTvSymbol)}&interval=${tvInterval}&theme=dark&style=1&timezone=Asia%2FShanghai&locale=zh_CN`;
  }

  async function fetchModalKlines(sym, interval) {
    if (!modalNativeView || !modalLoading) return;
    if (modalFetchController) modalFetchController.abort();
    modalFetchController = new AbortController();
    const signal = modalFetchController.signal;

    clearModalChartDisplay();
    modalLoading.classList.remove('hidden');
    if (modalError) modalError.classList.add('hidden');

    try {
      const url = `api/binance/klines?symbol=${encodeURIComponent(sym)}&interval=${encodeURIComponent(interval)}&limit=168`;
      const { data: raw } = await fetchJsonWithTimeout(url, { signal }, 12000);
      if (signal.aborted) return;
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
        modalChartPrice.className = `font-mono font-bold text-sm ${isUp ? 'text-emerald-400' : 'text-rose-400'}`;
        if (modalHudEma5) modalHudEma5.textContent = formatCryptoPrice(last.ema5);
        if (modalHudEma10) modalHudEma10.textContent = formatCryptoPrice(last.ema10);
      }
      renderModalChart();
    } catch (err) {
      if (err && err.name === 'AbortError') return;
      console.error('Failed to load klines:', err);
      if (modalError) modalError.classList.remove('hidden');
    } finally {
      if (modalLoading && !signal.aborted) modalLoading.classList.add('hidden');
    }
  }

  function openTvModal(sym, tvSym) {
    if (!chartModal) return;
    modalCurrentSymbol = sym;
    modalCurrentTvSymbol = tvSym;
    clearModalChartDisplay();

    if (modalChartTitle) modalChartTitle.textContent = sym;
    if (modalExternalLink) modalExternalLink.href = getTvUrl(tvSym, TV_LAYOUT_ID, modalCurrentInterval);
    if (!chartModal.open) {
      chartModal.showModal();
    }

    if (modalActiveEngine === 'native') {
      if (modalNativeView) modalNativeView.classList.remove('hidden');
      if (modalTvView) modalTvView.classList.add('hidden');
      fetchModalKlines(modalCurrentSymbol, modalCurrentInterval);
    } else {
      if (modalNativeView) modalNativeView.classList.add('hidden');
      if (modalTvView) modalTvView.classList.remove('hidden');
      loadTvIframe();
    }
  }

  function resetModalStateOnClose() {
    if (modalFetchController) modalFetchController.abort();
    if (modalTvIframe) modalTvIframe.src = 'about:blank';
    clearModalChartDisplay();
  }

  function closeTvModal() {
    if (!chartModal || !chartModal.open) return;
    chartModal.close();
  }

  function scheduleModalRender() {
    if (modalRenderRafId !== null) return;
    modalRenderRafId = requestAnimationFrame(() => {
      modalRenderRafId = null;
      renderModalChart();
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
      } else if (modalHoverIndex !== -1) {
        modalHoverIndex = -1;
        scheduleModalRender();
      }
    });
    modalNativeView.addEventListener('mouseleave', () => {
      if (modalHoverIndex !== -1) {
        modalHoverIndex = -1;
        scheduleModalRender();
      }
    });
  }

  modalBarsBtns.forEach((btn) => {
    btn.addEventListener('click', () => {
      const bars = Number(btn.getAttribute('data-bars'));
      if (!VALID_BAR_COUNTS.has(bars)) return;
      modalVisibleBars = bars;
      modalHoverIndex = -1;
      updateModalBarsUi();
      if (typeof onVisibleBarsChange === 'function') {
        onVisibleBarsChange(bars);
      }
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

      const nextInterval = btn.getAttribute('data-interval');
      if (!nextInterval || !Object.hasOwn(TV_INTERVAL_MAP, nextInterval)) {
        throw new Error(`Unsupported modal interval: ${nextInterval}`);
      }
      modalCurrentInterval = nextInterval;
      if (modalExternalLink && modalCurrentTvSymbol) {
        modalExternalLink.href = getTvUrl(modalCurrentTvSymbol, TV_LAYOUT_ID, modalCurrentInterval);
      }
      if (modalActiveEngine === 'native') {
        fetchModalKlines(modalCurrentSymbol, modalCurrentInterval);
      } else {
        loadTvIframe();
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
      loadTvIframe();
    });
  }

  if (modalRetryBtn) {
    modalRetryBtn.addEventListener('click', () => fetchModalKlines(modalCurrentSymbol, modalCurrentInterval));
  }
  if (modalCloseBtn) modalCloseBtn.addEventListener('click', closeTvModal);
  if (chartModal) {
    chartModal.addEventListener('close', resetModalStateOnClose);
    const outsideDialog = (e) => {
      const r = chartModal.getBoundingClientRect();
      return e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom;
    };
    let pressedOutside = false;
    chartModal.addEventListener('pointerdown', (e) => {
      pressedOutside = outsideDialog(e);
    });
    chartModal.addEventListener('pointerup', (e) => {
      if (pressedOutside && outsideDialog(e)) closeTvModal();
    });
  }
  global.addEventListener('resize', () => {
    if (chartModal && chartModal.open && modalActiveEngine === 'native') {
      renderModalChart();
    }
  });

  function init(opts) {
    const options = opts || {};
    if (VALID_BAR_COUNTS.has(Number(options.visibleBars))) {
      modalVisibleBars = Number(options.visibleBars);
    }
    onVisibleBarsChange = typeof options.onVisibleBarsChange === 'function' ? options.onVisibleBarsChange : null;
    updateModalBarsUi();
  }

  global.SecondLegKlineModal = Object.freeze({
    VALID_BAR_COUNTS,
    init,
    open: openTvModal,
    close: closeTvModal,
    isOpen: () => Boolean(chartModal && chartModal.open),
    getVisibleBars: () => modalVisibleBars,
  });
})(window);
