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
  let supportsDisplayTransition;

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

  function canTransitionDisplay() {
    if (supportsDisplayTransition !== undefined) return supportsDisplayTransition;
    if (
      !global.CSS ||
      typeof global.CSS.supports !== 'function' ||
      !global.CSS.supports('transition-behavior', 'allow-discrete') ||
      !global.document ||
      !global.document.body ||
      typeof global.document.createElement !== 'function' ||
      typeof global.getComputedStyle !== 'function'
    ) {
      supportsDisplayTransition = false;
      return false;
    }
    const probe = global.document.createElement('div');
    probe.style.cssText = 'transition: display 1s allow-discrete !important; display: block;';
    global.document.body.appendChild(probe);
    global.getComputedStyle(probe).display;
    probe.style.display = 'none';
    supportsDisplayTransition = global.getComputedStyle(probe).display === 'block';
    probe.remove();
    return supportsDisplayTransition;
  }

  function supportsTopLayerExit() {
    return Boolean(
      global.CSS &&
      typeof global.CSS.supports === 'function' &&
      global.CSS.supports('overlay', 'auto') &&
      canTransitionDisplay()
    );
  }

  async function closeTopLayer(dialogEl) {
    if (!dialogEl || !dialogEl.open) return;
    if (!supportsTopLayerExit() && typeof dialogEl.getAnimations === 'function') {
      dialogEl.setAttribute('data-closing', '');
      const animations = dialogEl.getAnimations({ subtree: true });
      if (animations.length > 0) {
        await Promise.race([
          Promise.allSettled(animations.map((a) => a.finished)),
          new Promise((r) => setTimeout(r, 400)),
        ]);
      }
      if (!dialogEl.hasAttribute('data-closing')) return;
      dialogEl.removeAttribute('data-closing');
    }
    if (dialogEl.open) {
      dialogEl.close();
    }
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

    const topPadding = 14;
    const bottomAxisH = 22;
    const plotH = Math.max(96, h - bottomAxisH);
    const priceH = plotH * 0.78;
    const priceSpanH = Math.max(40, priceH - topPadding);
    const volH = plotH * 0.17;
    const rightPadding = 72;
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

    const getY = (p) => topPadding + priceSpanH - ((p - minP) / fullSpan) * priceSpanH;
    const getVolY = (v) => plotH - 3 - (v / (maxVol || 1)) * volH;
    const lastCandle = visibleCandles[visibleCandles.length - 1];
    const lastCloseY = lastCandle ? getY(lastCandle.close) : null;

    ctx.fillStyle = '#0b0e13';
    ctx.fillRect(0, 0, w, h);

    ctx.strokeStyle = '#1b2432';
    ctx.lineWidth = 1;
    ctx.fillStyle = '#8b9bb0';
    ctx.font = '10px "JetBrains Mono", "SF Mono", monospace';
    ctx.textAlign = 'left';

    const steps = 6;
    for (let i = 0; i <= steps; i++) {
      const p = minP + (fullSpan / steps) * i;
      const y = getY(p);
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(chartW, y);
      ctx.stroke();
      if (lastCloseY === null || Math.abs(y - lastCloseY) >= 13) {
        ctx.fillText(formatCryptoPrice(p), chartW + 7, y + 3);
      }
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
    ctx.fillStyle = '#8b9bb0';
    ctx.font = '10px "JetBrains Mono", "SF Mono", monospace';
    ctx.textAlign = 'center';
    for (let i = 0; i < n; i += tickStep) {
      const x = i * slot + slot / 2;
      if (x < 36 || x > chartW - 36) continue;
      ctx.beginPath();
      ctx.moveTo(x, plotH);
      ctx.lineTo(x, plotH + 4);
      ctx.stroke();
      ctx.fillText(formatAxisTime(visibleCandles[i].time, modalCurrentInterval), x, plotH + 15);
    }
    ctx.textAlign = 'left';

    visibleCandles.forEach((c, i) => {
      const x = i * slot + slot / 2;
      const isBull = c.close >= c.open;
      const col = isBull ? '#22c58b' : '#f25565';
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
      ctx.fillStyle = isBull ? 'rgba(34, 197, 139, 0.32)' : 'rgba(242, 85, 101, 0.32)';
      ctx.fillRect(x - barW / 2, vY, barW, plotH - 3 - vY);
    });

    const drawEmaLine = (field, color) => {
      ctx.beginPath();
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.5;
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

    drawEmaLine('ema5', '#f0b429');
    drawEmaLine('ema10', '#60a5fa');

    const last = visibleCandles[visibleCandles.length - 1];
    if (last) {
      const lastY = getY(last.close);
      const isBull = last.close >= last.open;
      ctx.beginPath();
      ctx.setLineDash([3, 3]);
      ctx.strokeStyle = isBull ? '#22c58b' : '#f25565';
      ctx.moveTo(0, lastY);
      ctx.lineTo(chartW, lastY);
      ctx.stroke();
      ctx.setLineDash([]);

      ctx.fillStyle = isBull ? '#159668' : '#d13b4b';
      ctx.fillRect(chartW + 2, lastY - 9, rightPadding - 4, 18);
      ctx.fillStyle = '#ffffff';
      ctx.font = 'bold 10px "JetBrains Mono", "SF Mono", monospace';
      ctx.fillText(formatCryptoPrice(last.close), chartW + 6, lastY + 4);
    }

    if (modalHoverIndex >= 0 && modalHoverIndex < n && modalTooltip) {
      const c = visibleCandles[modalHoverIndex];
      const hx = modalHoverIndex * slot + slot / 2;
      const hy = getY(c.close);

      ctx.strokeStyle = '#647894';
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
          <span class="text-[#f1f5f9] font-bold">${escapeHtml(timeStr)}</span>
          <span class="${isBull ? 'text-[#38d99f]' : 'text-[#f8717e]'} font-bold">${escapeHtml(chgLabel)}</span>
        </div>
        <div class="grid grid-cols-4 gap-x-3 gap-y-0.5 text-[#9fb0c5]">
          <div>开 <span class="text-[#f1f5f9]">${escapeHtml(formatCryptoPrice(c.open))}</span></div>
          <div>高 <span class="text-[#f1f5f9]">${escapeHtml(formatCryptoPrice(c.high))}</span></div>
          <div>低 <span class="text-[#f1f5f9]">${escapeHtml(formatCryptoPrice(c.low))}</span></div>
          <div>收 <span class="text-[#f1f5f9]">${escapeHtml(formatCryptoPrice(c.close))}</span></div>
        </div>
        <div class="flex items-center gap-3 mt-1 pt-1 border-t border-[#222b38] text-[10px]">
          <span class="text-[#f5c451]">EMA5 ${escapeHtml(formatCryptoPrice(c.ema5))}</span>
          <span class="text-[#8ab8ff]">EMA10 ${escapeHtml(formatCryptoPrice(c.ema10))}</span>
          <span class="text-[#9fb0c5]">成交量 ${escapeHtml((c.volume / 1000).toFixed(1))}k</span>
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
      modalChartPrice.className = 'font-mono tabular-nums text-[#38d99f] font-bold text-sm';
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
        modalChartPrice.className = `font-mono tabular-nums font-bold text-sm ${isUp ? 'text-[#38d99f]' : 'text-[#f8717e]'}`;
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
    chartModal.removeAttribute('data-closing');
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
    if (chartModal) chartModal.removeAttribute('data-closing');
    if (modalFetchController) modalFetchController.abort();
    if (modalTvIframe) modalTvIframe.src = 'about:blank';
    clearModalChartDisplay();
  }

  function closeTvModal() {
    if (!chartModal || !chartModal.open) return;
    closeTopLayer(chartModal);
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
      b.classList.toggle('bg-[#192231]', active);
      b.classList.toggle('text-[#f1f5f9]', active);
      b.classList.toggle('font-semibold', active);
      b.classList.toggle('hover:bg-[#151c27]', !active);
      b.classList.toggle('text-[#9fb0c5]', !active);
    });
  }

  if (modalNativeView) {
    modalNativeView.addEventListener('mousemove', (e) => {
      const rect = modalNativeView.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const chartW = modalNativeView.clientWidth - 72;
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
        b.classList.remove('bg-[#192231]', 'text-[#f1f5f9]', 'font-semibold');
        b.classList.add('hover:bg-[#151c27]', 'text-[#9fb0c5]');
      });
      btn.classList.add('bg-[#192231]', 'text-[#f1f5f9]', 'font-semibold');
      btn.classList.remove('hover:bg-[#151c27]', 'text-[#9fb0c5]');

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
      btnEngineNative.className = 'px-2 py-0.5 bg-[#192231] text-[#f1f5f9] font-semibold';
      btnEngineTv.className = 'px-2 py-0.5 hover:bg-[#151c27] text-[#9fb0c5]';
      if (modalBarsGroup) modalBarsGroup.classList.remove('hidden');
      if (modalTvView) modalTvView.classList.add('hidden');
      if (modalNativeView) modalNativeView.classList.remove('hidden');
      fetchModalKlines(modalCurrentSymbol, modalCurrentInterval);
    });
    btnEngineTv.addEventListener('click', () => {
      modalActiveEngine = 'tv';
      btnEngineTv.className = 'px-2 py-0.5 bg-[#192231] text-[#f1f5f9] font-semibold';
      btnEngineNative.className = 'px-2 py-0.5 hover:bg-[#151c27] text-[#9fb0c5]';
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
    chartModal.addEventListener('cancel', (event) => {
      event.preventDefault();
      closeTopLayer(chartModal);
    });
    if (typeof global.HTMLDialogElement === 'undefined' || !('closedBy' in global.HTMLDialogElement.prototype)) {
      chartModal.addEventListener('click', (event) => {
        if (event.target !== chartModal) return;
        const rect = chartModal.getBoundingClientRect();
        const isDialogContent = (
          rect.top <= event.clientY &&
          event.clientY <= rect.top + rect.height &&
          rect.left <= event.clientX &&
          event.clientX <= rect.left + rect.width
        );
        if (!isDialogContent) {
          closeTvModal();
        }
      });
    }
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
