(function (global) {
  'use strict';

  const TV_LAYOUT_ID = 'ig0LuDL8';
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

  function formatSignedPct(val, decimalsOrOpts) {
    if (!isFiniteNum(val)) return '--';
    const num = Number(val);
    const opts = typeof decimalsOrOpts === 'object' && decimalsOrOpts !== null
      ? decimalsOrOpts
      : { decimals: decimalsOrOpts, plusOnZero: true };
    const decimals = opts.decimals !== undefined ? opts.decimals : 2;
    const signed = opts.signed !== undefined ? opts.signed : true;
    const plusOnZero = opts.plusOnZero !== undefined ? opts.plusOnZero : false;
    const prefix = signed && (num > 0 || (plusOnZero && num === 0)) ? '+' : '';
    return prefix + num.toFixed(decimals) + '%';
  }

  function getTvSymbol(symbol) {
    if (typeof symbol !== 'string' || !symbol.trim()) {
      throw new Error('getTvSymbol requires a non-empty symbol string');
    }
    const sym = symbol.trim();
    if (sym.includes(':')) return sym;
    return CHINESE_TV_MAP[sym] || `BINANCE:${sym}.P`;
  }

  function getTvUrl(symbol, layoutId, interval) {
    const tvSym = getTvSymbol(symbol);
    const lid = layoutId || TV_LAYOUT_ID;
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

  global.SecondLegCommon = Object.freeze({
    TV_LAYOUT_ID,
    TV_INTERVAL_MAP,
    escapeHtml,
    isFiniteNum,
    formatCryptoPrice,
    formatSignedPct,
    getTvSymbol,
    getTvUrl,
    fetchJsonWithTimeout,
    toBeijingDate,
  });
})(window);
