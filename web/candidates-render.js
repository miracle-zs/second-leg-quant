(function (global) {
  'use strict';

  const Common = global.SecondLegCommon;
  const {
    EXT_ARROW_SVG,
    escapeHtml,
    isFiniteNum,
    formatCryptoPrice,
    formatSignedPct,
    getTvSymbol,
    getTvUrl,
    setIfChanged,
  } = Common;

  const DEFAULT_HIGH_TURNOVER_THRESHOLD = 1.5;
  const MAX_CONCURRENT_PRICE_FLASH = 24;
  const priceFlashTimers = new WeakMap();

  function isValidFundingIntervalHours(val) {
    if (!isFiniteNum(val)) return false;
    const num = Number(val);
    return Number.isInteger(num) && num > 0;
  }

  function formatMillions(val) {
    if (!isFiniteNum(val)) return '--';
    return '$' + (val / 1e6).toFixed(1) + 'M';
  }

  function getEffectiveSupply(c) {
    if (isFiniteNum(c.circulating_supply) && c.circulating_supply > 0 && isFiniteNum(c.multiplier) && c.multiplier > 0) {
      return c.circulating_supply / c.multiplier;
    }
    return 0;
  }

  function classifyCandidate(c, highTurnoverThreshold) {
    const threshold = isFiniteNum(highTurnoverThreshold) && Number(highTurnoverThreshold) > 0
      ? Number(highTurnoverThreshold)
      : DEFAULT_HIGH_TURNOVER_THRESHOLD;
    const isEmaBull = Boolean(c.is_ema_bull);
    const isHighTr = isFiniteNum(c.turnover_ratio) && Number(c.turnover_ratio) >= threshold;
    const isNegFr = isFiniteNum(c.funding_rate_4h_pct) && c.funding_rate_4h_pct < 0;
    const isResonance = isEmaBull && isHighTr;
    c.is_ema_bull = isEmaBull;
    c.is_high_turnover = isHighTr;
    c.is_neg_funding = isNegFr;
    c.is_resonance = isResonance;
  }

  function getFundingClass(fr4h) {
    if (!isFiniteNum(fr4h)) return 'sym-fr-4h font-mono tabular-nums text-[#7f91a8]';
    if (fr4h <= -0.025) return 'sym-fr-4h font-mono tabular-nums text-[#f8717e] font-bold';
    if (fr4h < 0) return 'sym-fr-4h font-mono tabular-nums text-[#f8717e]';
    return 'sym-fr-4h font-mono tabular-nums text-[#9fb0c5]';
  }

  function getTurnoverBadgeClass(tr, highTurnoverThreshold) {
    const threshold = isFiniteNum(highTurnoverThreshold) && Number(highTurnoverThreshold) > 0
      ? Number(highTurnoverThreshold)
      : DEFAULT_HIGH_TURNOVER_THRESHOLD;
    if (!isFiniteNum(tr)) return 'text-[#7f91a8]';
    if (tr >= 5.0) return 'text-[#f1f5f9] font-bold';
    if (tr >= 2.0) return 'text-[#8ab8ff] font-semibold';
    if (tr >= threshold) return 'text-[#8ab8ff] font-medium';
    if (tr >= 0.8) return 'text-[#9fb0c5]';
    return 'text-[#7f91a8]';
  }

  function getPosBarColor(posPct) {
    if (!isFiniteNum(posPct)) return 'bg-[#3b82f6]';
    if (posPct <= 25) return 'bg-[#22c58b]';
    if (posPct >= 75) return 'bg-[#f0b429]';
    return 'bg-[#3b82f6]';
  }

  function getRowClass(c) {
    if (c.is_resonance && c.is_neg_funding) {
      return 'resonance-neg-row candidate-row';
    }
    if (c.is_resonance) {
      return 'resonance-gold-row candidate-row';
    }
    return 'candidate-row';
  }

  function formatTickerDisplayHtml(sym) {
    if (sym.endsWith('USDT') && sym.length > 4) {
      const base = escapeHtml(sym.slice(0, -4));
      return `<span class="text-[#f1f5f9] font-bold font-sans text-[13px] tracking-tight">${base}</span><span class="text-[#7f91a8] font-mono text-[10px]">USDT</span>`;
    }
    return `<span class="text-[#f1f5f9] font-bold font-sans text-[13px] tracking-tight">${escapeHtml(sym)}</span>`;
  }

  function buildIntervalBadge(interval) {
    if (interval === 1) {
      return '<span class="text-[10px] font-mono font-semibold text-[#f5c451]" title="每 1 小时结算资金费率">1h</span>';
    }
    if (interval === 4) {
      return '<span class="text-[10px] font-mono text-[#7f91a8]">4h</span>';
    }
    if (interval === 8) {
      return '<span class="text-[10px] font-mono text-[#7f91a8]">8h</span>';
    }
    if (isFiniteNum(interval) && interval > 0) {
      return `<span class="text-[10px] font-mono text-[#7f91a8]">${escapeHtml(interval)}h</span>`;
    }
    return '<span class="text-[10px] font-mono text-[#7f91a8]">--</span>';
  }

  function buildEmaHtml(c) {
    if (!isFiniteNum(c.ema5) || !isFiniteNum(c.ema10)) {
      return '<span class="text-[#7f91a8] text-[11px]">--</span>';
    }
    const diffEma = c.ema5 - c.ema10;
    const ema5Str = escapeHtml(formatCryptoPrice(c.ema5, { refDiff: diffEma }));
    const ema10Str = escapeHtml(formatCryptoPrice(c.ema10, { refDiff: diffEma }));
    const spread = isFiniteNum(c.ema_spread_pct) ? c.ema_spread_pct : null;
    const spreadText = spread !== null ? escapeHtml(formatSignedPct(spread, 2)) : '--';

    let dotClass = 'bg-[#4b5c75]';
    let textClass = 'text-[#7f91a8]';
    let stateBadge = '';
    let statePrefix = '未顺势';
    if (spread !== null && Math.abs(spread) <= 0.35) {
      dotClass = 'bg-[#f0b429]';
      textClass = 'text-[#f5c451] font-semibold';
      stateBadge = '<span class="text-[10px] font-sans font-medium text-[#f5c451]">粘合</span>';
      statePrefix = 'EMA临界粘合';
    } else if (c.is_ema_bull) {
      dotClass = 'bg-[#22c58b]';
      textClass = 'text-[#38d99f] font-semibold';
      stateBadge = '<span class="sr-only">多头顺势</span>';
      statePrefix = '多头顺势';
    }

    return `<div class="inline-flex items-center justify-end gap-1.5 whitespace-nowrap" title="${statePrefix} · EMA5 ${ema5Str} / EMA10 ${ema10Str}">
      <span class="w-1.5 h-1.5 rounded-full ${dotClass} shrink-0" aria-hidden="true"></span>
      <span class="text-[11px] font-mono tabular-nums ${textClass}">${spreadText}</span>${stateBadge}
    </div>`;
  }

  function buildDurationHtml(c) {
    if (!c.is_resonance) {
      return '<span class="text-[#7f91a8] font-mono text-xs">--</span>';
    }
    const entryStr = c.resonance_entry_time ? `<div class="text-[10px] font-mono tabular-nums text-[#7f91a8] mt-0.5">${escapeHtml(c.resonance_entry_time)}</div>` : '';
    if (!isFiniteNum(c.resonance_duration_hours) || Number(c.resonance_duration_hours) < 0) {
      return `<div><span class="text-[#7f91a8] font-mono text-xs">--</span>${entryStr}</div>`;
    }
    const resHours = Math.floor(Number(c.resonance_duration_hours));
    let badge;
    if (resHours > 0) {
      if (resHours <= 3) {
        badge = `<span class="text-[11px] font-sans tabular-nums font-semibold text-[#38d99f] inline-flex items-center justify-end gap-1.5"><span class="w-1.5 h-1.5 rounded-full bg-[#22c58b]" aria-hidden="true"></span>新进 ${resHours}h</span>`;
      } else if (resHours <= 12) {
        badge = `<span class="text-[11px] font-sans tabular-nums font-semibold text-[#f5c451] inline-flex items-center justify-end gap-1.5"><span class="w-1.5 h-1.5 rounded-full bg-[#f0b429]" aria-hidden="true"></span>活跃 ${resHours}h</span>`;
      } else {
        const days = Math.floor(resHours / 24);
        const remH = resHours % 24;
        const dStr = days > 0 ? `${days}d ${remH}h` : `${resHours}h`;
        badge = `<span class="text-[11px] font-sans tabular-nums font-medium text-[#9fb0c5] inline-flex items-center justify-end gap-1.5">驻留 ${escapeHtml(dStr)}</span>`;
      }
    } else {
      badge = '<span class="text-[11px] font-sans font-semibold text-[#38d99f] inline-flex items-center justify-end gap-1.5"><span class="w-1.5 h-1.5 rounded-full bg-[#22c58b]" aria-hidden="true"></span>刚入圈</span>';
    }
    return `<div>${badge}${entryStr}</div>`;
  }

  function computePositionPct(p, low, high) {
    if (!isFiniteNum(p) || !isFiniteNum(low) || !isFiniteNum(high) || high <= low) return null;
    return Math.max(0, Math.min(100, ((p - low) / (high - low)) * 100));
  }

  function getSortMetric(c, col) {
    switch (col) {
      case 'price':
        return isFiniteNum(c.price) ? c.price : NaN;
      case 'chg':
        return isFiniteNum(c.price_chg_24h) ? c.price_chg_24h : NaN;
      case 'mc':
        return isFiniteNum(c.market_cap_usd) ? c.market_cap_usd : NaN;
      case 'vol':
        return isFiniteNum(c.volume_24h_usd) ? c.volume_24h_usd : NaN;
      case 'tr':
        return isFiniteNum(c.turnover_ratio) ? c.turnover_ratio : NaN;
      case 'fr':
        return isFiniteNum(c.funding_rate_4h_pct) ? c.funding_rate_4h_pct : NaN;
      case 'ema':
        return isFiniteNum(c.ema_spread_pct) ? c.ema_spread_pct : NaN;
      case 'duration':
        return c.is_resonance && isFiniteNum(c.resonance_duration_hours) ? c.resonance_duration_hours : NaN;
      default:
        return NaN;
    }
  }

  function getRankStickyBg(c) {
    if (c.is_resonance && c.is_neg_funding) return '#16141c';
    if (c.is_resonance) return '#16181d';
    return '#10151d';
  }

  function getResBadgeProps(c) {
    if (c.is_resonance && c.is_neg_funding) {
      return {
        text: '贴水',
        title: '贴水双核共振',
        className: 'sym-res-badge shrink-0 inline-block px-1 py-0.5 rounded-[3px] text-[10px] font-sans font-medium bg-[#f25565]/15 text-[#f8717e] border border-[#f25565]/30',
      };
    }
    if (c.is_resonance) {
      return {
        text: '共振',
        title: '顺势高换手共振',
        className: 'sym-res-badge shrink-0 inline-block px-1 py-0.5 rounded-[3px] text-[10px] font-sans font-medium bg-[#f0b429]/10 text-[#f5c451] border border-[#f0b429]/30',
      };
    }
    return {
      text: '',
      title: '',
      className: 'sym-res-badge hidden',
    };
  }

  function updateKpiCards(kpiEls, list, highTurnoverThreshold) {
    const total = list.length;
    const resonance = list.filter((c) => c.is_resonance);
    const negRes = resonance.filter((c) => c.is_neg_funding);
    const pureResCount = resonance.length - negRes.length;
    const negFr = list.filter((c) => c.is_neg_funding);
    const highTr = list.filter((c) => c.is_high_turnover);
    const emaBull = list.filter((c) => c.is_ema_bull);
    const overflow = list.filter((c) => Boolean(c.is_overflow_7d));

    const sampleRes = resonance.slice(0, 3).map((c) => c.symbol.replace(/USDT$/, '')).join(' · ') || '--';
    const sampleNeg = negFr
      .filter((c) => isFiniteNum(c.funding_rate_4h_pct))
      .sort((a, b) => Number(a.funding_rate_4h_pct) - Number(b.funding_rate_4h_pct))
      .slice(0, 2)
      .map((c) => `${c.symbol.replace(/USDT$/, '')}(${formatSignedPct(c.funding_rate_4h_pct, 2)})`)
      .join('、') || '--';
    const sampleTr = highTr
      .filter((c) => isFiniteNum(c.turnover_ratio))
      .sort((a, b) => Number(b.turnover_ratio) - Number(a.turnover_ratio))
      .slice(0, 2)
      .map((c) => `${c.symbol.replace(/USDT$/, '')}(${Number(c.turnover_ratio).toFixed(1)}x)`)
      .join('、') || '--';
    const sampleEma = emaBull
      .filter((c) => isFiniteNum(c.ema_spread_pct))
      .sort((a, b) => Number(b.ema_spread_pct) - Number(a.ema_spread_pct))
      .slice(0, 2)
      .map((c) => `${c.symbol.replace(/USDT$/, '')}(${formatSignedPct(c.ema_spread_pct, 1)})`)
      .join('、') || '--';

    const pctLabel = (cnt) => (total > 0 ? `${((cnt / total) * 100).toFixed(1)}%` : '--');
    const pctWidth = (cnt) => (total > 0 ? `${Math.min(100, (cnt / total) * 100).toFixed(1)}%` : '0%');

    setIfChanged(kpiEls.resCount, 'textContent', String(resonance.length));
    setIfChanged(kpiEls.resSub, 'textContent', `${pureResCount} 黄金 / ${negRes.length} 贴水`);
    setIfChanged(kpiEls.resPct, 'textContent', pctLabel(resonance.length));
    if (kpiEls.resBar && kpiEls.resBar.style) setIfChanged(kpiEls.resBar.style, 'width', pctWidth(resonance.length));
    if (kpiEls.resSample) {
      setIfChanged(kpiEls.resSample, 'textContent', `代表: ${sampleRes}`);
      setIfChanged(kpiEls.resSample, 'title', sampleRes);
    }

    setIfChanged(kpiEls.negCount, 'textContent', String(negFr.length));
    setIfChanged(kpiEls.negPct, 'textContent', pctLabel(negFr.length));
    if (kpiEls.negBar && kpiEls.negBar.style) setIfChanged(kpiEls.negBar.style, 'width', pctWidth(negFr.length));
    if (kpiEls.negSample) {
      setIfChanged(kpiEls.negSample, 'textContent', `贴水深: ${sampleNeg}`);
      setIfChanged(kpiEls.negSample, 'title', sampleNeg);
    }

    setIfChanged(kpiEls.highTrCount, 'textContent', String(highTr.length));
    setIfChanged(kpiEls.highTrSub, 'textContent', `换手 ≥ ${highTurnoverThreshold}x`);
    setIfChanged(kpiEls.highTrPct, 'textContent', pctLabel(highTr.length));
    if (kpiEls.highTrBar && kpiEls.highTrBar.style) setIfChanged(kpiEls.highTrBar.style, 'width', pctWidth(highTr.length));
    if (kpiEls.highTrSample) {
      setIfChanged(kpiEls.highTrSample, 'textContent', `最高: ${sampleTr}`);
      setIfChanged(kpiEls.highTrSample, 'title', sampleTr);
    }

    setIfChanged(kpiEls.emaBullCount, 'textContent', String(emaBull.length));
    setIfChanged(kpiEls.emaBullPct, 'textContent', pctLabel(emaBull.length));
    if (kpiEls.emaBullBar && kpiEls.emaBullBar.style) setIfChanged(kpiEls.emaBullBar.style, 'width', pctWidth(emaBull.length));
    if (kpiEls.emaBullSample) {
      setIfChanged(kpiEls.emaBullSample, 'textContent', `多头: ${sampleEma}`);
      setIfChanged(kpiEls.emaBullSample, 'title', sampleEma);
    }

    setIfChanged(kpiEls.allCount, 'textContent', String(total));
    setIfChanged(kpiEls.allSub, 'textContent', `含 ${overflow.length} 溢出跟踪`);
    if (kpiEls.allSample) {
      setIfChanged(kpiEls.allSample, 'textContent', '门槛: 市值 < $30M · 7d 溢出 ≤ $80M');
      setIfChanged(kpiEls.allSample, 'title', 'CMC Pro 流通市值低于 $30M，溢出后保持 7 天跟踪（≤ $80M）');
    }
    if (kpiEls.allBar && kpiEls.allBar.style) setIfChanged(kpiEls.allBar.style, 'width', total > 0 ? '100%' : '0%');
  }

  function extractRowRefs(rowEl) {
    const cells = rowEl.cells;
    if (cells && cells.length >= 11) {
      const symWrap = cells[1].firstElementChild;
      const priceCell = cells[3];
      const posBoxEl = priceCell.children[1] || null;
      const posTrack = posBoxEl ? posBoxEl.firstElementChild : null;
      const mcCell = cells[5];
      const trCell = cells[7];
      const frCell = cells[8];
      const frTop = frCell.firstElementChild;
      return {
        rowEl,
        dirty: false,
        lastValues: null,
        rankEl: cells[0],
        resBadgeEl: symWrap ? symWrap.children[1] || null : null,
        priceEl: priceCell.firstElementChild,
        posBoxEl,
        posBarEl: posTrack ? posTrack.firstElementChild : null,
        posTextEl: posBoxEl ? posBoxEl.children[1] || null : null,
        chgEl: cells[4],
        mcValEl: mcCell.firstElementChild
          ? (mcCell.firstElementChild.classList && mcCell.firstElementChild.classList.contains('sym-mc-val')
            ? mcCell.firstElementChild
            : (mcCell.firstElementChild.firstElementChild || (mcCell.querySelector ? mcCell.querySelector('.sym-mc-val') : null)))
          : (mcCell.querySelector ? mcCell.querySelector('.sym-mc-val') : null),
        volEl: cells[6],
        trBadgeEl: trCell.firstElementChild,
        fr4hEl: frTop ? frTop.firstElementChild : null,
        frSettleEl: frCell.children[1] || null,
        durationEl: cells[10],
      };
    }
    return {
      rowEl,
      dirty: false,
      lastValues: null,
      rankEl: rowEl.querySelector('.sym-rank'),
      resBadgeEl: rowEl.querySelector('.sym-res-badge'),
      priceEl: rowEl.querySelector('.sym-price'),
      posBoxEl: rowEl.querySelector('.sym-pos-box'),
      posBarEl: rowEl.querySelector('.sym-pos-bar'),
      posTextEl: rowEl.querySelector('.sym-pos-text'),
      chgEl: rowEl.querySelector('.sym-chg'),
      mcValEl: rowEl.querySelector('.sym-mc-val'),
      volEl: rowEl.querySelector('.sym-vol'),
      trBadgeEl: rowEl.querySelector('.sym-tr span'),
      fr4hEl: rowEl.querySelector('.sym-fr-4h'),
      frSettleEl: rowEl.querySelector('.sym-fr-settle'),
      durationEl: rowEl.querySelector('.sym-duration'),
    };
  }

  function renderTableRows(tableBody, list, rowRefsBySym, highTurnoverThreshold) {
    if (!tableBody) return;
    rowRefsBySym.clear();

    const emptyRowHtml = `
      <tr id="empty-search-row" class="hidden">
        <td colspan="11" class="py-14 text-center text-[#9fb0c5] font-sans">
          <div id="empty-state-title" class="text-sm font-semibold text-[#f1f5f9]">当前筛选下无匹配合约</div>
          <div id="empty-state-hint" class="text-xs text-[#7f91a8] mt-1">按 <kbd class="px-1.5 py-0.5 bg-[#151c27] rounded-[3px] border border-[#283446] font-mono text-[#9fb0c5]">Esc</kbd> 清空搜索词或点击下方按钮重置筛选</div>
          <button type="button" id="btn-empty-reset-filter" class="retry-candidates-btn px-3 py-1.5 bg-[#151c27] hover:bg-[#1d2736] text-[#f1f5f9] rounded-[4px] border border-[#283446] hover:border-[#3b4d66] text-xs font-medium transition-colors">重置筛选</button>
        </td>
      </tr>`;

    const rowsHtml = list.map((c, idx) => {
      const sym = c.symbol;
      const safeSym = escapeHtml(sym);
      const tvSym = getTvSymbol(sym);
      const safeTvSym = escapeHtml(tvSym);
      const tvUrl = getTvUrl(tvSym, null, '15m');
      const binanceUrl = `https://www.binance.com/zh-CN/futures/${encodeURIComponent(sym)}`;

      const pStr = escapeHtml(formatCryptoPrice(c.price));
      const lowStr = escapeHtml(formatCryptoPrice(c.low_24h));
      const highStr = escapeHtml(formatCryptoPrice(c.high_24h));
      const posPct = computePositionPct(c.price, c.low_24h, c.high_24h);
      const posWidth = posPct !== null ? posPct.toFixed(1) : '0.0';
      const posLabel = posPct !== null ? `${Math.round(posPct)}%` : '--';
      const posBarCol = getPosBarColor(posPct);
      const posTitle = posPct !== null
        ? escapeHtml(`24h振幅区间: ${lowStr} ~ ${highStr} (现价分位: ${Math.round(posPct)}%)`)
        : '24h振幅区间缺失';

      const chgClass = isFiniteNum(c.price_chg_24h)
        ? (c.price_chg_24h > 0 ? 'text-[#38d99f] font-semibold' : (c.price_chg_24h < 0 ? 'text-[#f8717e] font-semibold' : 'text-[#f1f5f9] font-medium'))
        : 'text-[#7f91a8]';
      const chgText = escapeHtml(formatSignedPct(c.price_chg_24h, 2));

      const mcStr = escapeHtml(formatMillions(c.market_cap_usd));
      let mcHtml;
      if (c.is_overflow_7d) {
        const daysLeft = isFiniteNum(c.overflow_days_left) ? c.overflow_days_left.toFixed(1) : '--';
        mcHtml = `<div class="inline-flex flex-col items-end">
          <div class="text-[#f5c451] font-mono tabular-nums font-semibold sym-mc-val">${mcStr}</div>
          <span class="text-[10px] font-sans tabular-nums text-[#f5c451]/80 mt-0.5" title="曾落入 $30M 以下底池，市值溢出后保持 7 天跟踪">7d 溢出 · ${escapeHtml(daysLeft)}d</span>
        </div>`;
      } else {
        mcHtml = `<div class="text-[#f1f5f9] font-mono tabular-nums sym-mc-val">${mcStr}</div>`;
      }

      const volStr = escapeHtml(formatMillions(c.volume_24h_usd));
      const trStr = isFiniteNum(c.turnover_ratio) ? c.turnover_ratio.toFixed(2) + 'x' : '--';
      const trClass = getTurnoverBadgeClass(c.turnover_ratio, highTurnoverThreshold);

      const validInterval = isValidFundingIntervalHours(c.funding_interval_hours);
      const fr4hStr = escapeHtml(formatSignedPct(c.funding_rate_4h_pct, 4));
      const frSettleStr = escapeHtml(formatSignedPct(c.funding_rate_pct, 4));
      const frColor = getFundingClass(c.funding_rate_4h_pct);
      const cycleBadge = buildIntervalBadge(c.funding_interval_hours);
      const showSettleNote = (!validInterval || c.funding_interval_hours !== 4) && isFiniteNum(c.funding_rate_pct);
      const rankBg = getRankStickyBg(c);
      const resBadge = getResBadgeProps(c);

      return `<tr class="${getRowClass(c)}" data-sym="${safeSym}" data-tv-sym="${safeTvSym}">
        <td class="py-2.5 px-2 text-center text-[#7f91a8] font-mono tabular-nums text-[11px] sym-rank" style="position: sticky; inset-inline-start: 0; z-index: 5; width: 2.25rem; min-width: 2.25rem; max-width: 2.25rem; background-color: ${rankBg};">${idx + 1}</td>
        <th scope="row" class="py-2 px-3 font-normal sticky-col" style="inset-inline-start: 2.25rem; min-width: 112px; max-width: clamp(112px, 34vw, 190px);">
          <div class="flex items-center gap-1" style="min-width: 0;">
            <button type="button" class="btn-preview-kline row-symbol-btn" style="max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" data-sym="${safeSym}" data-tv-sym="${safeTvSym}" title="预览 ${safeSym} K线图表">${formatTickerDisplayHtml(sym)}</button>
            <span class="${resBadge.className}"${resBadge.title ? ` title="${resBadge.title}"` : ''}>${resBadge.text}</span>
          </div>
        </th>
        <td class="py-2 px-2.5 whitespace-nowrap font-sans">
          <div class="flex items-center gap-1">
            <a href="${escapeHtml(tvUrl)}" target="_blank" rel="noopener noreferrer" class="row-micro-btn" title="在新标签页打开 ${safeSym} TradingView 15m 专属画线布局"><span>TV</span>${EXT_ARROW_SVG}</a>
            <a href="${escapeHtml(binanceUrl)}" target="_blank" rel="noopener noreferrer" class="row-micro-btn" title="在新标签页打开 ${safeSym} 币安永续合约盘口"><span>币安</span>${EXT_ARROW_SVG}</a>
            <button type="button" class="btn-copy-sym row-micro-btn" data-copy="${safeTvSym}" data-label="复制" title="复制 TV 代码 (${safeTvSym})">复制</button>
          </div>
        </td>
        <td class="py-2.5 px-2.5 whitespace-nowrap text-right">
          <div class="text-[#f1f5f9] font-mono tabular-nums font-semibold sym-price">${pStr}</div>
          <div class="sym-pos-box w-[76px] ml-auto mt-1 flex items-center justify-end gap-1.5 text-[10px]" title="${posTitle}">
            <div class="w-12 h-1 bg-[#1c2532] rounded-[1px] relative overflow-hidden">
              <div class="sym-pos-bar h-full ${posBarCol} transition-all duration-300" style="width: ${posWidth}%"></div>
            </div>
            <span class="sym-pos-text font-mono tabular-nums text-[10px] text-[#9fb0c5] w-7 text-right">${posLabel}</span>
          </div>
        </td>
        <td class="py-2.5 px-2.5 font-mono tabular-nums sym-chg whitespace-nowrap text-right ${chgClass}">${chgText}</td>
        <td class="py-2.5 px-2.5 whitespace-nowrap text-right border-l border-[#1c2532]">${mcHtml}</td>
        <td class="py-2.5 px-2.5 text-[#9fb0c5] font-mono tabular-nums sym-vol whitespace-nowrap text-right">${volStr}</td>
        <td class="py-2.5 px-2.5 sym-tr font-mono tabular-nums whitespace-nowrap text-right"><span class="${trClass}">${escapeHtml(trStr)}</span></td>
        <td class="py-2.5 px-2.5 sym-fr whitespace-nowrap text-right border-l border-[#1c2532]">
          <div class="inline-flex items-center justify-end gap-1">
            <span class="${frColor}">${fr4hStr}</span>
            ${cycleBadge}
          </div>
          <div class="text-[10px] text-[#7f91a8] font-mono tabular-nums sym-fr-settle mt-0.5${showSettleNote ? '' : ' hidden'}">${showSettleNote ? `(${frSettleStr})` : ''}</div>
        </td>
        <td class="py-2.5 px-3 font-sans whitespace-nowrap text-right">${buildEmaHtml(c)}</td>
        <td class="py-2.5 px-3 font-sans whitespace-nowrap text-right sym-duration border-l border-[#1c2532]">${buildDurationHtml(c)}</td>
      </tr>`;
    }).join('');

    tableBody.innerHTML = emptyRowHtml + rowsHtml;

    const candidateRows = tableBody.children && tableBody.children.length > 1
      ? tableBody.children
      : tableBody.querySelectorAll('tr.candidate-row');
    for (let i = 0; i < candidateRows.length; i += 1) {
      const rowEl = candidateRows[i];
      if (!rowEl || typeof rowEl.getAttribute !== 'function') continue;
      const sym = rowEl.getAttribute('data-sym');
      if (!sym) continue;
      const refs = extractRowRefs(rowEl);
      const cand = list[rowRefsBySym.size];
      if (cand && cand.symbol === sym) {
        refs.lastValues = {
          price: cand.price,
          low_24h: cand.low_24h,
          high_24h: cand.high_24h,
          price_chg_24h: cand.price_chg_24h,
          volume_24h_usd: cand.volume_24h_usd,
          market_cap_usd: cand.market_cap_usd,
          turnover_ratio: cand.turnover_ratio,
          funding_rate_4h_pct: cand.funding_rate_4h_pct,
          funding_rate_pct: cand.funding_rate_pct,
          funding_interval_hours: cand.funding_interval_hours,
        };
      }
      rowRefsBySym.set(sym, refs);
    }
  }

  function triggerPriceFlash(priceEl, isUp) {
    if (!priceEl) return;
    if (priceEl.classList && typeof priceEl.classList.add === 'function') {
      const flashClass = isUp ? 'price-flash-up' : 'price-flash-down';
      const prevTimer = priceFlashTimers.get(priceEl);
      if (prevTimer) {
        clearTimeout(prevTimer);
        priceFlashTimers.delete(priceEl);
      }
      const hadSameClass = typeof priceEl.classList.contains === 'function' && priceEl.classList.contains(flashClass);
      priceEl.classList.remove('price-flash-up', 'price-flash-down');
      const applyCls = () => {
        priceEl.classList.add(flashClass);
        const tid = setTimeout(() => {
          priceEl.classList.remove('price-flash-up', 'price-flash-down');
          priceFlashTimers.delete(priceEl);
        }, 520);
        if (tid && typeof tid.unref === 'function') tid.unref();
        priceFlashTimers.set(priceEl, tid);
      };
      if (hadSameClass && typeof requestAnimationFrame === 'function') {
        requestAnimationFrame(applyCls);
      } else {
        applyCls();
      }
    } else if (typeof priceEl.animate === 'function') {
      priceEl.animate(
        [
          { color: isUp ? '#38d99f' : '#f8717e' },
          { color: '#f1f5f9' },
        ],
        { duration: 480, easing: 'cubic-bezier(0.16, 1, 0.3, 1)' }
      );
    }
  }

  function syncRowDom(c, refs, opts) {
    if (!refs) return;
    const {
      animatePrice = false,
      prevPrice = undefined,
      prevRes = c.is_resonance,
      prevNegFr = c.is_neg_funding,
      flashBudget = null,
      prefersReducedMotion = false,
      highTurnoverThreshold = DEFAULT_HIGH_TURNOVER_THRESHOLD,
    } = opts || {};
    const last = refs.lastValues;

    if (isFiniteNum(c.price) && refs.priceEl) {
      if (animatePrice && prevPrice !== undefined && prevPrice !== c.price && !prefersReducedMotion) {
        if (!flashBudget || flashBudget.count < MAX_CONCURRENT_PRICE_FLASH) {
          if (flashBudget) flashBudget.count += 1;
          triggerPriceFlash(refs.priceEl, c.price > prevPrice);
        }
      }
      if (!last || last.price !== c.price) {
        setIfChanged(refs.priceEl, 'textContent', formatCryptoPrice(c.price));
      }
    }

    if (!last || last.price !== c.price || last.low_24h !== c.low_24h || last.high_24h !== c.high_24h) {
      const posPct = computePositionPct(c.price, c.low_24h, c.high_24h);
      if (refs.posBarEl && posPct !== null) {
        const nextWidth = posPct.toFixed(1) + '%';
        if (refs.posBarEl.style.width !== nextWidth) refs.posBarEl.style.width = nextWidth;
        setIfChanged(refs.posBarEl, 'className', `sym-pos-bar h-full ${getPosBarColor(posPct)} transition-all duration-300`);
      }
      if (refs.posTextEl) {
        setIfChanged(refs.posTextEl, 'textContent', posPct !== null ? `${Math.round(posPct)}%` : '--');
      }
      if (refs.posBoxEl && isFiniteNum(c.low_24h) && isFiniteNum(c.high_24h) && posPct !== null) {
        const nextTitle = `24h振幅区间: ${formatCryptoPrice(c.low_24h)} ~ ${formatCryptoPrice(c.high_24h)} (现价分位: ${Math.round(posPct)}%)`;
        if (refs.posBoxEl.getAttribute('title') !== nextTitle) {
          refs.posBoxEl.setAttribute('title', nextTitle);
        }
      }
    }

    if ((!last || last.price_chg_24h !== c.price_chg_24h) && isFiniteNum(c.price_chg_24h) && refs.chgEl) {
      setIfChanged(refs.chgEl, 'textContent', formatSignedPct(c.price_chg_24h, 2));
      const nextChgCls = c.price_chg_24h > 0
        ? 'text-[#38d99f] font-semibold'
        : (c.price_chg_24h < 0 ? 'text-[#f8717e] font-semibold' : 'text-[#f1f5f9] font-medium');
      setIfChanged(refs.chgEl, 'className', `py-2.5 px-2.5 font-mono tabular-nums sym-chg whitespace-nowrap text-right ${nextChgCls}`);
    }

    if ((!last || last.volume_24h_usd !== c.volume_24h_usd) && isFiniteNum(c.volume_24h_usd) && refs.volEl) {
      setIfChanged(refs.volEl, 'textContent', formatMillions(c.volume_24h_usd));
    }
    if ((!last || last.market_cap_usd !== c.market_cap_usd) && isFiniteNum(c.market_cap_usd) && refs.mcValEl) {
      setIfChanged(refs.mcValEl, 'textContent', formatMillions(c.market_cap_usd));
    }
    if ((!last || last.turnover_ratio !== c.turnover_ratio) && isFiniteNum(c.turnover_ratio) && refs.trBadgeEl) {
      setIfChanged(refs.trBadgeEl, 'textContent', c.turnover_ratio.toFixed(2) + 'x');
      setIfChanged(refs.trBadgeEl, 'className', getTurnoverBadgeClass(c.turnover_ratio, highTurnoverThreshold));
    }

    if (
      !last ||
      last.funding_rate_pct !== c.funding_rate_pct ||
      last.funding_rate_4h_pct !== c.funding_rate_4h_pct ||
      last.funding_interval_hours !== c.funding_interval_hours
    ) {
      const validInterval = isValidFundingIntervalHours(c.funding_interval_hours);
      if (refs.frSettleEl) {
        if ((!validInterval || c.funding_interval_hours !== 4) && isFiniteNum(c.funding_rate_pct)) {
          setIfChanged(refs.frSettleEl, 'textContent', `(${formatSignedPct(c.funding_rate_pct, 4)})`);
          refs.frSettleEl.classList.remove('hidden');
        } else {
          setIfChanged(refs.frSettleEl, 'textContent', '');
          refs.frSettleEl.classList.add('hidden');
        }
      }
      if (refs.fr4hEl) {
        const nextFr4hText = isFiniteNum(c.funding_rate_4h_pct) ? formatSignedPct(c.funding_rate_4h_pct, 4) : '--';
        setIfChanged(refs.fr4hEl, 'textContent', nextFr4hText);
        setIfChanged(refs.fr4hEl, 'className', getFundingClass(c.funding_rate_4h_pct));
        if (!isFiniteNum(c.funding_rate_4h_pct) && !validInterval && isFiniteNum(c.funding_rate_pct)) {
          setIfChanged(refs.fr4hEl, 'title', '缺少有效结算周期，暂无法折算 4h 费率');
        } else if (refs.fr4hEl.title) {
          setIfChanged(refs.fr4hEl, 'title', '');
        }
      }
    }

    if (prevRes !== c.is_resonance || prevNegFr !== c.is_neg_funding) {
      if (refs.rowEl) setIfChanged(refs.rowEl, 'className', getRowClass(c));
      if (refs.rankEl && refs.rankEl.style) {
        const nextBg = getRankStickyBg(c);
        if (refs.rankEl.style.backgroundColor !== nextBg) {
          refs.rankEl.style.backgroundColor = nextBg;
        }
      }
      if (refs.resBadgeEl) {
        const badge = getResBadgeProps(c);
        setIfChanged(refs.resBadgeEl, 'className', badge.className);
        setIfChanged(refs.resBadgeEl, 'textContent', badge.text);
        setIfChanged(refs.resBadgeEl, 'title', badge.title);
      }
    }
    if (refs.durationEl && prevRes !== c.is_resonance) {
      refs.durationEl.innerHTML = buildDurationHtml(c);
    }

    refs.lastValues = {
      price: c.price,
      low_24h: c.low_24h,
      high_24h: c.high_24h,
      price_chg_24h: c.price_chg_24h,
      volume_24h_usd: c.volume_24h_usd,
      market_cap_usd: c.market_cap_usd,
      turnover_ratio: c.turnover_ratio,
      funding_rate_4h_pct: c.funding_rate_4h_pct,
      funding_rate_pct: c.funding_rate_pct,
      funding_interval_hours: c.funding_interval_hours,
    };
    refs.dirty = false;
  }

  function applySortOrder({ tableBody, candidatesState, rowRefsBySym, sortColumn, sortAsc }) {
    if (!tableBody) return false;
    if (sortColumn) {
      candidatesState.sort((a, b) => {
        const valA = getSortMetric(a, sortColumn);
        const valB = getSortMetric(b, sortColumn);
        const okA = Number.isFinite(valA);
        const okB = Number.isFinite(valB);
        if (!okA && !okB) return a.originalOrder - b.originalOrder;
        if (!okA) return 1;
        if (!okB) return -1;
        const diff = sortAsc ? valA - valB : valB - valA;
        return diff !== 0 ? diff : a.originalOrder - b.originalOrder;
      });
    } else {
      candidatesState.sort((a, b) => a.originalOrder - b.originalOrder);
    }

    const visibleEntries = [];
    for (let i = 0; i < candidatesState.length; i += 1) {
      const c = candidatesState[i];
      const refs = rowRefsBySym.get(c.symbol);
      if (refs && refs.rowEl && refs.rowEl.style.display !== 'none') {
        visibleEntries.push({ c, refs });
      }
    }
    const hiddenOrderDirty = visibleEntries.length < candidatesState.length;

    const domVisibleRows = [];
    const childList = tableBody.children && tableBody.children.length > 0
      ? tableBody.children
      : tableBody.querySelectorAll('tr.candidate-row');
    for (let i = 0; i < childList.length; i += 1) {
      const rowEl = childList[i];
      if (
        rowEl &&
        rowEl.id !== 'empty-search-row' &&
        rowEl.style &&
        rowEl.style.display !== 'none'
      ) {
        domVisibleRows.push(rowEl);
      }
    }

    let orderChanged = domVisibleRows.length !== visibleEntries.length;
    if (!orderChanged) {
      for (let i = 0; i < visibleEntries.length; i += 1) {
        if (domVisibleRows[i] !== visibleEntries[i].refs.rowEl) {
          orderChanged = true;
          break;
        }
      }
    }

    if (orderChanged) {
      if (typeof tableBody.moveBefore === 'function') {
        visibleEntries.forEach(({ refs }) => {
          tableBody.moveBefore(refs.rowEl, null);
        });
      } else {
        const frag = global.document.createDocumentFragment();
        visibleEntries.forEach(({ refs }) => {
          frag.appendChild(refs.rowEl);
        });
        tableBody.appendChild(frag);
      }
    }

    for (let i = 0; i < visibleEntries.length; i += 1) {
      setIfChanged(visibleEntries[i].refs.rankEl, 'textContent', String(i + 1));
    }
    return hiddenOrderDirty;
  }

  global.SecondLegCandidatesRender = Object.freeze({
    DEFAULT_HIGH_TURNOVER_THRESHOLD,
    isValidFundingIntervalHours,
    formatMillions,
    getEffectiveSupply,
    classifyCandidate,
    getSortMetric,
    updateKpiCards,
    renderTableRows,
    syncRowDom,
    applySortOrder,
  });
})(window);
