(function (global) {
  'use strict';

  let activeTvLayoutId = '';
  let tvConfigStatus = 'idle';
  let configPromise = null;
  const boundTvLinks = typeof WeakSet === 'function' ? new WeakSet() : null;
  const TV_INTERVAL_MAP = Object.freeze({
    '15m': '15',
    '1h': '60',
    '4h': '240',
    '1d': '1D',
  });
  const CHINESE_TV_MAP = Object.freeze({
    '龙虾USDT': 'BINANCE:LONGXIAUSDT.P',
    '哈基米USDT': 'BINANCE:HAJIMIUSDT.P',
    '我踏马来了USDT': 'BINANCE:WOTAMALAILIAOUSDT.P',
    '币安人生USDT': 'BINANCE:BIANRENSHENGUSDT.P',
    '牛来USDT': 'BINANCE:NIULAIUSDT.P',
  });
  const FORBIDDEN_CONFIG_KEYS = new Set(['__proto__', 'constructor', 'prototype']);

  let activeChineseTvMap = Object.freeze({ ...CHINESE_TV_MAP });

  function mergeChineseTvMap(overrideMap) {
    const merged = { ...CHINESE_TV_MAP };
    if (overrideMap && typeof overrideMap === 'object' && !Array.isArray(overrideMap)) {
      for (const [rawKey, rawVal] of Object.entries(overrideMap)) {
        if (FORBIDDEN_CONFIG_KEYS.has(rawKey)) continue;
        const key = typeof rawKey === 'string' ? rawKey.trim() : '';
        const val = typeof rawVal === 'string' ? rawVal.trim() : '';
        if (key && val && !FORBIDDEN_CONFIG_KEYS.has(key)) {
          merged[key] = val;
        }
      }
    }
    return Object.freeze(merged);
  }

  function buildConfigSnapshot(layoutId, chineseTvMap, rawData) {
    const snapshot = {};
    if (rawData && typeof rawData === 'object' && !Array.isArray(rawData)) {
      for (const [k, v] of Object.entries(rawData)) {
        if (FORBIDDEN_CONFIG_KEYS.has(k)) continue;
        if (k === 'tradingview_layout_id' || k === 'chinese_tv_map') continue;
        if (v === null || typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') {
          snapshot[k] = v;
        }
      }
    }
    snapshot.tradingview_layout_id = layoutId || '';
    snapshot.chinese_tv_map = chineseTvMap;
    return Object.freeze(snapshot);
  }

  let activeConfig = buildConfigSnapshot(activeTvLayoutId, activeChineseTvMap, null);

  function escapeHtml(val) {
    if (val === null || val === undefined) return '';
    return String(val)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function isFiniteNum(val) {
    if (typeof val === 'number') return Number.isFinite(val);
    if (typeof val === 'string') {
      return val.trim() !== '' && Number.isFinite(Number(val));
    }
    return false;
  }

  function formatCryptoPrice(val, opts) {
    if (!isFiniteNum(val)) return '--';
    const num = Number(val);
    const options = opts || {};
    if (options.positiveOnly && num <= 0) return '--';
    const absV = Math.abs(num);
    const dec4Threshold = options.dec4Threshold !== undefined ? options.dec4Threshold : 0.01;
    let baseDec = 8;
    if (absV >= 100) baseDec = 2;
    else if (absV >= dec4Threshold) baseDec = 4;
    else if (absV >= 0.0001) baseDec = 6;
    if (isFiniteNum(options.refDiff)) {
      const absDiff = Math.abs(Number(options.refDiff));
      if (absDiff > 0 && absDiff < Math.pow(10, -baseDec)) {
        baseDec = Math.min(8, baseDec + 2);
      }
    }
    if (options.useGrouping && absV >= 100) {
      return num.toLocaleString('en-US', {
        minimumFractionDigits: baseDec,
        maximumFractionDigits: baseDec,
      });
    }
    return num.toFixed(baseDec);
  }

  function getExtArrowSvg(extraClass) {
    const cls = typeof extraClass === 'string' && extraClass.trim()
      ? `w-2.5 h-2.5 ${escapeHtml(extraClass.trim())} shrink-0`
      : 'w-2.5 h-2.5 shrink-0';
    return `<svg class="${cls}" viewBox="0 0 12 12" fill="none" aria-hidden="true"><path d="M3.5 8.5L8.5 3.5M8.5 3.5H4.5M8.5 3.5V7.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
  }

  const EXT_ARROW_SVG = getExtArrowSvg();

  function debounce(fn, delayMs) {
    if (typeof fn !== 'function') {
      throw new TypeError('debounce requires a function');
    }
    const wait = typeof delayMs === 'number' && Number.isFinite(delayMs) && delayMs >= 0 ? delayMs : 0;
    let timerId = null;
    function debounced(...args) {
      if (timerId !== null) {
        clearTimeout(timerId);
      }
      timerId = setTimeout(() => {
        timerId = null;
        fn.apply(this, args);
      }, wait);
    }
    debounced.cancel = () => {
      if (timerId !== null) {
        clearTimeout(timerId);
        timerId = null;
      }
    };
    return debounced;
  }

  function syncAriaSort(rootElOrHeaders, activeColumn, sortAsc) {
    if (!rootElOrHeaders) return;
    let headers = null;
    if (typeof rootElOrHeaders.querySelectorAll === 'function') {
      headers = rootElOrHeaders.querySelectorAll('th.sortable');
    } else if (typeof rootElOrHeaders.forEach === 'function') {
      headers = rootElOrHeaders;
    } else if (typeof rootElOrHeaders[Symbol.iterator] === 'function') {
      headers = Array.from(rootElOrHeaders);
    }
    if (!headers || typeof headers.forEach !== 'function') return;
    headers.forEach((th) => {
      if (!th || typeof th.getAttribute !== 'function') return;
      const col = th.getAttribute('data-sort');
      if (activeColumn && col === activeColumn) {
        if (typeof th.setAttribute === 'function') {
          th.setAttribute('aria-sort', sortAsc ? 'ascending' : 'descending');
        }
      } else if (typeof th.removeAttribute === 'function') {
        th.removeAttribute('aria-sort');
      }
    });
  }

  function bindSortButtons(rootEl, onSort) {
    if (typeof onSort !== 'function') {
      throw new TypeError('bindSortButtons requires an onSort callback');
    }
    if (!rootEl) {
      return { syncAria: () => {} };
    }
    const supportsInvokerCommands = typeof global.HTMLButtonElement !== 'undefined' && 'commandForElement' in global.HTMLButtonElement.prototype;
    if (supportsInvokerCommands) {
      const alreadyBound = Boolean(rootEl.dataset && rootEl.dataset.sortBound === 'true');
      if (!alreadyBound) {
        if (rootEl.dataset) rootEl.dataset.sortBound = 'true';
        if (typeof rootEl.addEventListener === 'function') {
          rootEl.addEventListener('command', (e) => {
            if (e && e.command === '--sort' && e.source && typeof e.source.closest === 'function') {
              const th = e.source.closest('th.sortable');
              if (th) onSort(th);
            }
          });
        }
      }
    } else {
      const headers = typeof rootEl.querySelectorAll === 'function' ? rootEl.querySelectorAll('th.sortable') : [];
      if (headers && typeof headers.forEach === 'function') {
        headers.forEach((th) => {
          if (!th) return;
          if (th.dataset && th.dataset.sortBound === 'true') return;
          if (th.dataset) th.dataset.sortBound = 'true';
          const btn = typeof th.querySelector === 'function' ? th.querySelector('button[command="--sort"]') : null;
          const target = btn || th;
          if (target && typeof target.addEventListener === 'function') {
            target.addEventListener('click', () => onSort(th));
          }
        });
      }
    }
    return {
      syncAria(activeColumn, sortAsc) {
        syncAriaSort(rootEl, activeColumn, sortAsc);
      },
    };
  }

  function formatSignedPct(val, decimalsOrOpts) {
    if (!isFiniteNum(val)) return '--';
    const num = Number(val);
    const opts = typeof decimalsOrOpts === 'object' && decimalsOrOpts !== null
      ? decimalsOrOpts
      : { decimals: decimalsOrOpts };
    const decimals = opts.decimals !== undefined ? opts.decimals : 2;
    const signed = opts.signed !== undefined ? opts.signed : true;
    const plusOnZero = opts.plusOnZero !== undefined ? opts.plusOnZero : false;
    let fixed = num.toFixed(decimals);
    const isZeroDisplay = Number(fixed) === 0;
    if (isZeroDisplay && fixed.startsWith('-')) {
      fixed = (0).toFixed(decimals);
    }
    const prefix = signed && ((num > 0 && !isZeroDisplay) || (plusOnZero && isZeroDisplay)) ? '+' : '';
    return prefix + fixed + '%';
  }

  function getTvSymbol(symbol) {
    if (typeof symbol !== 'string' || !symbol.trim()) {
      throw new Error('getTvSymbol requires a non-empty symbol string');
    }
    const sym = symbol.trim();
    if (sym.includes(':')) return sym;
    if (Object.prototype.hasOwnProperty.call(activeChineseTvMap, sym)) {
      return activeChineseTvMap[sym];
    }
    return `BINANCE:${sym}.P`;
  }

  function getTvLayoutId() {
    if (!activeTvLayoutId) {
      throw new Error('TradingView layout ID has not been configured');
    }
    return activeTvLayoutId;
  }

  function getConfig() {
    return activeConfig;
  }

  function getConfigValue(key, fallback) {
    if (typeof key !== 'string' || !key || FORBIDDEN_CONFIG_KEYS.has(key)) {
      return fallback;
    }
    if (Object.prototype.hasOwnProperty.call(activeConfig, key)) {
      const val = activeConfig[key];
      return val !== undefined ? val : fallback;
    }
    return fallback;
  }

  function applyTvLayoutLinks() {
    if (typeof global.document === 'undefined' || typeof global.document.querySelectorAll !== 'function') {
      return;
    }
    const links = global.document.querySelectorAll('a[data-tv-layout-link]');
    if (!links || typeof links.forEach !== 'function') return;

    const baseUrl = activeTvLayoutId
      ? `https://cn.tradingview.com/chart/${encodeURIComponent(activeTvLayoutId)}/`
      : '';
    const pendingState = tvConfigStatus === 'unconfigured'
      ? 'unconfigured'
      : (tvConfigStatus === 'error' ? 'error' : 'loading');

    links.forEach((el) => {
      if (!el) return;
      const canGetAttr = typeof el.getAttribute === 'function';
      const canSetAttr = typeof el.setAttribute === 'function';
      const canRemoveAttr = typeof el.removeAttribute === 'function';

      if (canGetAttr && canSetAttr && !el.getAttribute('data-tv-default-title') && el.title) {
        el.setAttribute('data-tv-default-title', el.title);
      }

      if (typeof el.addEventListener === 'function' && (!boundTvLinks || !boundTvLinks.has(el))) {
        if (boundTvLinks) boundTvLinks.add(el);
        el.addEventListener('click', (e) => {
          if (!activeTvLayoutId) {
            if (e && typeof e.preventDefault === 'function') e.preventDefault();
            if (tvConfigStatus !== 'loading' && !configPromise) {
              ensureConfig().catch((err) => console.error('Failed to load /api/config:', err));
            }
          }
        });
      }

      if (activeTvLayoutId) {
        el.href = baseUrl;
        if (canRemoveAttr) {
          el.removeAttribute('data-tv-pending');
          el.removeAttribute('aria-disabled');
        }
        if (canGetAttr && el.getAttribute('data-tv-layout-title')) {
          el.title = `打开 TradingView 专属画线布局 (${activeTvLayoutId})`;
        } else if (canGetAttr && el.getAttribute('data-tv-default-title')) {
          el.title = el.getAttribute('data-tv-default-title');
        }
      } else {
        if (canSetAttr) {
          el.setAttribute('data-tv-pending', pendingState);
          el.setAttribute('aria-disabled', 'true');
        }
        if (pendingState === 'unconfigured') {
          el.title = 'TradingView 布局 ID (tradingview_layout_id) 未配置，暂不可跳转';
        } else if (pendingState === 'error') {
          el.title = 'TradingView 布局配置加载失败，点击重试加载';
        } else {
          el.title = '正在加载 TradingView 专属画线布局配置，请稍候…';
        }
      }
    });
  }

  function setTvLayoutId(layoutId) {
    if (typeof layoutId !== 'string' || !layoutId.trim()) {
      throw new Error('TradingView layout ID must be a non-empty string');
    }
    activeTvLayoutId = layoutId.trim();
    tvConfigStatus = 'ready';
    activeConfig = buildConfigSnapshot(activeTvLayoutId, activeChineseTvMap, activeConfig);
    applyTvLayoutLinks();
    return activeTvLayoutId;
  }

  function ensureConfig() {
    if (activeTvLayoutId) return Promise.resolve(activeTvLayoutId);
    if (
      typeof global.fetch !== 'function' ||
      typeof global.document === 'undefined' ||
      typeof global.document.querySelector !== 'function'
    ) {
      return Promise.resolve(activeTvLayoutId);
    }
    if (!configPromise) {
      tvConfigStatus = 'loading';
      applyTvLayoutLinks();
      configPromise = fetchJsonWithTimeout('api/config', { cache: 'no-store' }, 8000)
        .then(({ data }) => {
          if (!data || typeof data.tradingview_layout_id !== 'string' || !data.tradingview_layout_id.trim()) {
            tvConfigStatus = 'unconfigured';
            applyTvLayoutLinks();
            throw new Error('Invalid /api/config payload: missing tradingview_layout_id');
          }
          activeChineseTvMap = mergeChineseTvMap(data.chinese_tv_map);
          activeConfig = buildConfigSnapshot(data.tradingview_layout_id.trim(), activeChineseTvMap, data);
          return setTvLayoutId(data.tradingview_layout_id);
        })
        .catch((err) => {
          configPromise = null;
          if (tvConfigStatus !== 'unconfigured') {
            tvConfigStatus = 'error';
            applyTvLayoutLinks();
          }
          throw err;
        });
    }
    return configPromise;
  }

  function getTvUrl(symbol, layoutId, interval) {
    const tvSym = getTvSymbol(symbol);
    const lid = (typeof layoutId === 'string' && layoutId.trim()) ? layoutId.trim() : getTvLayoutId();
    let url = `https://cn.tradingview.com/chart/${encodeURIComponent(lid)}/?symbol=${encodeURIComponent(tvSym)}`;
    if (interval) {
      const tvInterval = TV_INTERVAL_MAP[interval] || interval;
      url += `&interval=${encodeURIComponent(tvInterval)}`;
    }
    return url;
  }

  async function fetchJsonWithTimeout(url, options, timeoutMs) {
    const opts = options ? { ...options } : {};
    const externalSignal = opts.signal;
    const ms = typeof timeoutMs === 'number' && timeoutMs > 0 ? timeoutMs : 12000;
    const controller = new AbortController();

    const onExternalAbort = () => {
      controller.abort(externalSignal.reason);
    };
    if (externalSignal) {
      if (externalSignal.aborted) {
        controller.abort(externalSignal.reason);
      } else {
        externalSignal.addEventListener('abort', onExternalAbort, { once: true });
      }
    }

    const timerId = setTimeout(() => {
      controller.abort(new DOMException('Request timeout', 'TimeoutError'));
    }, ms);

    opts.signal = controller.signal;
    try {
      const res = await fetch(url, opts);
      const etag = res.headers.get('ETag');
      if (res.status === 304) {
        return { status: 304, notModified: true, etag, data: null, text: '' };
      }
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }
      const text = await res.text();
      const data = JSON.parse(text);
      return { status: res.status, notModified: false, etag, data, text };
    } finally {
      clearTimeout(timerId);
      if (externalSignal) {
        externalSignal.removeEventListener('abort', onExternalAbort);
      }
    }
  }

  function toBeijingDate(tsMs) {
    return new Date(tsMs + 8 * 3600 * 1000);
  }

  if (typeof global.document !== 'undefined' && typeof global.document.querySelector === 'function') {
    const syncLinks = () => {
      ensureConfig().catch((err) => console.error('Failed to load /api/config:', err));
    };
    if (global.document.readyState === 'loading' && typeof global.document.addEventListener === 'function') {
      global.document.addEventListener('DOMContentLoaded', syncLinks);
    } else {
      syncLinks();
    }
  }

  global.SecondLegCommon = Object.freeze({
    TV_INTERVAL_MAP,
    CHINESE_TV_MAP,
    EXT_ARROW_SVG,
    getExtArrowSvg,
    debounce,
    bindSortButtons,
    syncAriaSort,
    escapeHtml,
    isFiniteNum,
    formatCryptoPrice,
    formatSignedPct,
    getTvSymbol,
    getTvLayoutId,
    setTvLayoutId,
    getConfig,
    getConfigValue,
    ensureConfig,
    getTvUrl,
    fetchJsonWithTimeout,
    toBeijingDate,
  });
})(window);
