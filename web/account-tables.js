(function (global) {
  'use strict';

  const Common = global.SecondLegCommon;
  const {
    escapeHtml,
    isFiniteNum: isFiniteNumber,
    formatCryptoPrice: commonFormatCryptoPrice,
    formatSignedPct,
    getTvUrl,
    setIfChanged,
  } = Common;

  const EXT_ARROW_SVG = typeof Common.getExtArrowSvg === 'function'
    ? Common.getExtArrowSvg('text-[#7f91a8]')
    : Common.EXT_ARROW_SVG;

  const symbolCollator = typeof Intl !== 'undefined' && typeof Intl.Collator === 'function'
    ? new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' })
    : null;

  const HISTORY_PAGE_SIZE = 50;
  let lastActivePosSignature = null;
  let historyFilterCache = null;
  let lastHistoryPageRows = [];

  const API_STATUS_BADGE_MAP = {
    BINANCE_REALTIME: {
      className: 'px-2 py-0.5 font-medium bg-[#22c58b]/10 text-[#38d99f] inline-flex items-center gap-1.5',
      dotClass: 'bg-[#22c58b]',
      label: '币安实盘在线',
    },
    BINANCE_PARTIAL: {
      className: 'px-2 py-0.5 font-medium bg-[#f0b429]/[0.12] text-[#f5c451] inline-flex items-center gap-1.5',
      dotClass: 'bg-[#f0b429]',
      label: '币安部分数据可用',
    },
    BINANCE_SNAPSHOT_PENDING_INCOME: {
      className: 'px-2 py-0.5 font-medium bg-[#f0b429]/[0.12] text-[#f5c451] inline-flex items-center gap-1.5',
      dotClass: 'bg-[#f0b429]',
      label: '币安快照同步中',
    },
    'OFFLINE_FALLBACK': {
      className: 'px-2 py-0.5 font-medium bg-[#151c27] text-[#f5c451] inline-flex items-center gap-1.5 whitespace-nowrap',
      dotClass: 'bg-[#f0b429]',
      label: '离线风控同步',
      title: '',
    },
    NOT_CONFIGURED: {
      className: 'px-2 py-0.5 font-medium bg-[#151c27] text-[#9fb0c5] inline-flex items-center gap-1.5 whitespace-nowrap',
      dotClass: 'bg-[#4b5c75]',
      label: '未配置币安密钥',
      title: '尚未配置币安 API 凭证，当前展示本地或未连接状态',
    },
  };

  function getLastHistoryPageRows() {
    return lastHistoryPageRows;
  }

  function renderApiStatusBadge(badge, cache, accountData, fetchError) {
    if (!badge) return;
    if (fetchError) {
      Common.updateStatusBadge(badge, cache, {
        className: 'px-2 py-0.5 font-medium bg-[#f25565]/10 text-[#f8717e] inline-flex items-center gap-1.5',
        dotClass: 'bg-[#f25565]',
        label: '数据拉取失败',
      });
      return;
    }
    const mode = (accountData && accountData.api_status && accountData.api_status.mode) || '';
    const spec = API_STATUS_BADGE_MAP[mode] || {
      className: 'px-2 py-0.5 font-medium bg-[#f25565]/10 text-[#f8717e] inline-flex items-center gap-1.5 whitespace-nowrap',
      dotClass: 'bg-[#f25565]',
      label: mode ? ('接口状态异常 (' + mode + ')') : '接口状态缺失',
      title: '',
    };
    Common.updateStatusBadge(badge, cache, spec);
  }

  function formatLoadErrorMessage(error, hasLatestSnapshot) {
    const errMsg = error && error.message ? String(error.message) : '未知错误';
    const isSchemaError = Boolean(
      (error && error.name === 'SyntaxError') ||
      /^Invalid (summary|\/api\/account payload|active|trade_history|equity_curve|daily_pnl|pnl_distribution)/.test(errMsg)
    );
    if (isSchemaError) {
      return hasLatestSnapshot
        ? ('账户快照摘要字段缺失或格式异常 (' + errMsg + ')，当前展示上次有效快照。')
        : ('账户快照摘要字段缺失或数据格式不兼容 (' + errMsg + ')，请确认后台分析数据版本后重试。');
    }
    return hasLatestSnapshot
      ? ('账户数据刷新失败 (' + errMsg + ')，当前展示上次快照。')
      : ('暂时无法加载账户数据 (' + errMsg + ')，请检查后台 API 服务，系统将自动重试。');
  }

  function getAccountModalCollection(kind, accountData) {
    if (!accountData) return [];
    if (kind === 'position') {
      return Array.isArray(accountData.active_positions) ? accountData.active_positions : [];
    }
    if (kind === 'trade') {
      return Array.isArray(lastHistoryPageRows) ? lastHistoryPageRows : [];
    }
    return [];
  }

  function openAccountKlineModal(KlineModal, accountData, kind, idx, triggerEl) {
    if (!KlineModal || typeof KlineModal.open !== 'function') return null;
    const list = getAccountModalCollection(kind, accountData);
    if (!list.length || idx < 0 || idx >= list.length) return null;
    const item = list[idx];
    const sym = item && typeof item.symbol === 'string' ? item.symbol.trim() : '';
    if (!sym) return null;
    if (typeof KlineModal.setNavMeta === 'function') {
      KlineModal.setNavMeta(idx, list.length);
    }
    const tvSym = typeof Common.getTvSymbol === 'function' ? Common.getTvSymbol(sym) : (sym + '.P');
    const context = kind === 'position' ? { type: 'position', position: item } : { type: 'trade', trade: item };
    KlineModal.open(sym, tvSym, triggerEl, context);
    return { kind, idx };
  }

  function formatMoney(val, options) {
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

  function formatCryptoPrice(val) {
    return commonFormatCryptoPrice(val, {
      positiveOnly: true,
      useGrouping: true,
    });
  }

  function formatDollarPrice(val) {
    const formatted = formatCryptoPrice(val);
    return formatted === '--' ? '--' : ('$' + formatted);
  }

  function getBaseAssetUnit(symbol) {
    if (typeof symbol !== 'string') return '';
    const trimmed = symbol.trim();
    return trimmed.endsWith('USDT') && trimmed.length > 4 ? trimmed.slice(0, -4) : trimmed;
  }

  function formatAmountWithUnit(amount, symbol, requirePositive) {
    if (!isFiniteNumber(amount)) return '--';
    const num = Number(amount);
    if (requirePositive && num <= 0) return '--';
    const unit = getBaseAssetUnit(symbol);
    const numStr = num.toLocaleString('en-US');
    return unit ? (numStr + ' ' + unit) : numStr;
  }

  function formatTrendlineDisplay(item) {
    if (!item || typeof item !== 'object') return '--';
    const rawLabel = item.trendline_label;
    if (typeof rawLabel === 'string') {
      const trimmedLabel = rawLabel.trim();
      if (trimmedLabel && trimmedLabel !== '--') return trimmedLabel;
    } else if (rawLabel !== null && rawLabel !== undefined && typeof rawLabel !== 'boolean') {
      return String(rawLabel);
    }
    const rawId = item.trendline_id;
    if (rawId !== null && rawId !== undefined && typeof rawId !== 'boolean') {
      const idStr = String(rawId).trim();
      if (idStr !== '') return idStr;
    }
    return '--';
  }

  function formatRatio(val, decimals) {
    if (!isFiniteNumber(val)) return '--';
    return Number(val).toFixed(decimals !== undefined ? decimals : 2);
  }

  function setSignedColorClass(el, val) {
    if (!el || !el.classList) return;
    el.classList.remove('text-[#38d99f]', 'text-[#f8717e]', 'text-[#f1f5f9]', 'text-[#9fb0c5]');
    if (!isFiniteNumber(val)) {
      el.classList.add('text-[#9fb0c5]');
      return;
    }
    const num = Number(val);
    if (num > 0) {
      el.classList.add('text-[#38d99f]');
    } else if (num < 0) {
      el.classList.add('text-[#f8717e]');
    } else {
      el.classList.add('text-[#f1f5f9]');
    }
  }

  function setText(id, text, getEl) {
    const el = typeof getEl === 'function' ? getEl(id) : global.document.getElementById(id);
    if (el) setIfChanged(el, 'textContent', text);
    return el;
  }

  function updateKpis(summary, getEl) {
    setText('kpi-total-equity', formatMoney(summary.total_equity), getEl);
    setText(
      'kpi-initial-capital',
      '本金: ' + (isFiniteNumber(summary.initial_capital) ? formatMoney(summary.initial_capital, { decimals: 0 }) : '--'),
      getEl
    );

    const retEl = setText('kpi-total-return', formatSignedPct(summary.total_return_pct), getEl);
    setSignedColorClass(retEl, summary.total_return_pct);

    setText('kpi-available-margin', formatMoney(summary.available_margin), getEl);
    setText('kpi-position-margin', formatMoney(summary.position_margin), getEl);
    const marginBadge = typeof getEl === 'function' ? getEl('kpi-margin-badge') : global.document.getElementById('kpi-margin-badge');
    if (marginBadge) {
      if (isFiniteNumber(summary.total_equity) && Number(summary.total_equity) > 0 && isFiniteNumber(summary.position_margin)) {
        const utilPct = (Number(summary.position_margin) / Number(summary.total_equity)) * 100;
        setIfChanged(marginBadge, 'textContent', utilPct > 0 ? ('占用 ' + utilPct.toFixed(1) + '%') : '当前空仓');
      } else {
        setIfChanged(marginBadge, 'textContent', '--');
      }
    }

    const realizedEl = setText('kpi-net-realized-pnl', formatMoney(summary.net_realized_pnl, { signed: true }), getEl);
    setSignedColorClass(realizedEl, summary.net_realized_pnl);
    setText(
      'kpi-total-fee',
      '手续费: ' + (isFiniteNumber(summary.total_fee) ? formatMoney(-Math.abs(Number(summary.total_fee))) : '--'),
      getEl
    );
    setText('kpi-funding-fee', '资费: ' + formatMoney(summary.total_funding_fee, { signed: true }), getEl);

    const unrealizedEl = setText('kpi-unrealized-pnl', formatMoney(summary.total_unrealized_pnl, { signed: true }), getEl);
    setSignedColorClass(unrealizedEl, summary.total_unrealized_pnl);
    const openCountText = isFiniteNumber(summary.open_trades) ? (Number(summary.open_trades) + ' 笔') : '--';
    setText('kpi-open-trades', openCountText, getEl);

    setText('kpi-win-rate', formatSignedPct(summary.win_rate_pct, { signed: false, decimals: 1 }), getEl);
    const winCount = isFiniteNumber(summary.win_count) ? Number(summary.win_count) : null;
    const lossCount = isFiniteNumber(summary.loss_count) ? Number(summary.loss_count) : null;
    const beCount = isFiniteNumber(summary.breakeven_count) ? Number(summary.breakeven_count) : null;
    if (winCount === null && lossCount === null && beCount === null) {
      setText('kpi-wl-record', '--', getEl);
    } else {
      setText(
        'kpi-wl-record',
        (winCount !== null ? winCount + 'W' : '--') + ' / ' +
        (lossCount !== null ? lossCount + 'L' : '--') + ' / ' +
        (beCount !== null ? beCount + 'BE' : '--'),
        getEl
      );
    }
    setText('kpi-total-trades', isFiniteNumber(summary.closed_trades) ? Number(summary.closed_trades) + ' 笔' : '--', getEl);

    let pfText = '--';
    if (isFiniteNumber(summary.profit_factor)) {
      pfText = Number(summary.profit_factor).toFixed(2);
    } else if (winCount !== null && lossCount !== null && winCount > 0 && lossCount === 0) {
      pfText = '全胜';
    }
    setText('kpi-profit-factor', pfText, getEl);
    setText('kpi-avg-win', '均盈: ' + formatMoney(summary.avg_win, { signed: true }), getEl);
    setText(
      'kpi-avg-loss',
      '均亏: ' + (isFiniteNumber(summary.avg_loss) ? formatMoney(-Math.abs(Number(summary.avg_loss))) : '--'),
      getEl
    );

    const ddPctVal = isFiniteNumber(summary.max_drawdown_pct) ? Math.abs(Number(summary.max_drawdown_pct)) : null;
    const ddPctFixed = ddPctVal === null ? null : ddPctVal.toFixed(2);
    const ddPctEl = setText(
      'kpi-max-dd-pct',
      ddPctFixed === null ? '--' : (ddPctFixed === '0.00' ? '0.00%' : ('-' + ddPctFixed + '%')),
      getEl
    );
    const ddUsdEl = setText(
      'kpi-max-dd-usd',
      isFiniteNumber(summary.max_drawdown_usd) ? formatMoney(-Math.abs(Number(summary.max_drawdown_usd))) : '--',
      getEl
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

    setText('kpi-sharpe', formatRatio(summary.sharpe_ratio, 2), getEl);
    const sharpeRating = typeof getEl === 'function' ? getEl('kpi-sharpe-rating') : global.document.getElementById('kpi-sharpe-rating');
    if (sharpeRating) {
      sharpeRating.classList.remove('text-[#38d99f]', 'text-[#8ab8ff]', 'text-[#f5c451]', 'text-[#f8717e]', 'text-[#9fb0c5]');
      if (!isFiniteNumber(summary.sharpe_ratio)) {
        setIfChanged(sharpeRating, 'textContent', '样本不足');
        sharpeRating.classList.add('text-[#9fb0c5]');
      } else {
        const sr = Number(summary.sharpe_ratio);
        if (sr >= 2.0) {
          setIfChanged(sharpeRating, 'textContent', '优异稳健');
          sharpeRating.classList.add('text-[#38d99f]');
        } else if (sr >= 1.0) {
          setIfChanged(sharpeRating, 'textContent', '表现稳健');
          sharpeRating.classList.add('text-[#8ab8ff]');
        } else if (sr >= 0) {
          setIfChanged(sharpeRating, 'textContent', '收益一般');
          sharpeRating.classList.add('text-[#f5c451]');
        } else {
          setIfChanged(sharpeRating, 'textContent', '负收益警告');
          sharpeRating.classList.add('text-[#f8717e]');
        }
      }
    }

    setText('kpi-sortino', formatRatio(summary.sortino_ratio, 2), getEl);
    const sortinoRating = typeof getEl === 'function' ? getEl('kpi-sortino-rating') : global.document.getElementById('kpi-sortino-rating');
    if (sortinoRating) {
      sortinoRating.classList.remove('text-[#38d99f]', 'text-[#8ab8ff]', 'text-[#f5c451]', 'text-[#f8717e]', 'text-[#9fb0c5]');
      if (!isFiniteNumber(summary.sortino_ratio)) {
        if (summary.sharpe_method !== 'NONE' && winCount !== null && lossCount !== null && winCount > 0 && lossCount === 0) {
          setIfChanged(sortinoRating, 'textContent', '零下行波动');
          sortinoRating.classList.add('text-[#38d99f]');
        } else {
          setIfChanged(sortinoRating, 'textContent', '样本不足');
          sortinoRating.classList.add('text-[#9fb0c5]');
        }
      } else {
        const so = Number(summary.sortino_ratio);
        if (so >= 2.5) {
          setIfChanged(sortinoRating, 'textContent', '极低尾部风险');
          sortinoRating.classList.add('text-[#38d99f]');
        } else if (so >= 1.0) {
          setIfChanged(sortinoRating, 'textContent', '下行风险可控');
          sortinoRating.classList.add('text-[#8ab8ff]');
        } else if (so >= 0) {
          setIfChanged(sortinoRating, 'textContent', '下行收益一般');
          sortinoRating.classList.add('text-[#f5c451]');
        } else {
          setIfChanged(sortinoRating, 'textContent', '下行风险偏高');
          sortinoRating.classList.add('text-[#f8717e]');
        }
      }
    }

    const maxWins = isFiniteNumber(summary.max_consecutive_wins) ? summary.max_consecutive_wins : '--';
    const maxLosses = isFiniteNumber(summary.max_consecutive_losses) ? summary.max_consecutive_losses : '--';
    setText('kpi-max-streak', maxWins + 'W / ' + maxLosses + 'L', getEl);
    setText('kpi-avg-hold', summary.avg_hold_duration_str || '--', getEl);
  }

  function renderActivePositions(positions, getEl) {
    const posTbody = typeof getEl === 'function' ? getEl('active-positions-tbody') : global.document.getElementById('active-positions-tbody');
    setText('active-pos-count-badge', positions.length + ' 活跃仓位', getEl);

    const posEmpty = typeof getEl === 'function' ? getEl('active-positions-empty') : global.document.getElementById('active-positions-empty');
    if (posEmpty) {
      posEmpty.classList.toggle('hidden', positions.length > 0);
      posEmpty.classList.toggle('inline-flex', positions.length === 0);
    }
    const posWrap = typeof getEl === 'function' ? getEl('active-positions-table-wrap') : global.document.getElementById('active-positions-table-wrap');
    if (posWrap) posWrap.classList.toggle('hidden', positions.length === 0);

    const shortWarn = typeof getEl === 'function' ? getEl('short-position-warning') : global.document.getElementById('short-position-warning');
    if (shortWarn) {
      const hasUnmonitored = positions.some((p) => {
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
      if (lastActivePosSignature !== 'EMPTY') {
        posTbody.innerHTML = '';
        lastActivePosSignature = 'EMPTY';
      }
      return;
    }

    const html = positions.map((p, idx) => {
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
      const amtStr = formatAmountWithUnit(p.amount, sym, false);

      const tvUrl = getTvUrl(sym);
      const tlDisplay = riskMonitored ? formatTrendlineDisplay(p) : '--';
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
          '<td class="px-3.5 py-2.5 font-sans font-bold text-[#f1f5f9] whitespace-nowrap">' +
            '<div class="flex items-center gap-1.5">' +
              '<a href="' + escapeHtml(tvUrl) + '" target="_blank" rel="noopener noreferrer" title="在新标签页打开 ' + safeSym + ' TradingView 图表" class="text-[#f1f5f9] hover:text-[#8ab8ff] hover:underline inline-flex items-center gap-1">' +
                '<span>' + safeSym + '</span>' + EXT_ARROW_SVG +
              '</a>' +
              sideBadge +
              '<button type="button" class="row-kline-chip" data-kline-kind="position" data-idx="' + idx + '" title="预览 ' + safeSym + ' 15m/1h K线">15m图</button>' +
            '</div>' +
          '</td>' +
          '<td class="px-3.5 py-2.5 text-right text-[#f1f5f9] whitespace-nowrap"><div>' + escapeHtml(levStr) + ' · ' + escapeHtml(notionalStr) + '</div><div class="text-[10px] text-[#7f91a8]">' + escapeHtml(amtStr) + '</div></td>' +
          '<td class="px-3.5 py-2.5 text-right text-[#9fb0c5] whitespace-nowrap">' + escapeHtml(formatDollarPrice(p.entry_price)) + '</td>' +
          '<td class="px-3.5 py-2.5 text-right text-[#f1f5f9] font-semibold whitespace-nowrap">' + escapeHtml(formatDollarPrice(p.mark_price)) + '</td>' +
          '<td class="px-3.5 py-2.5 text-right font-bold whitespace-nowrap ' + pnlCls + '"><div>' + escapeHtml(pnlStr) + '</div><div class="text-[10px] font-medium">' + escapeHtml(roeStr) + '</div></td>' +
          '<td class="px-3.5 py-2.5 text-right text-[#f8717e] whitespace-nowrap">' + escapeHtml(formatDollarPrice(p.liquidation_price)) + '</td>' +
          '<td class="px-3.5 py-2.5 font-sans text-xs whitespace-nowrap">' + trailingHtml + '</td>' +
          '<td class="px-3.5 py-2.5 font-sans text-xs whitespace-nowrap">' + slHtml + '</td>' +
          '<td class="px-3.5 py-2.5 ' + (tlDisplay !== '--' ? 'text-[#9fb0c5]' : 'text-[#7f91a8]') + ' whitespace-nowrap">' + escapeHtml(tlDisplay) + '</td>' +
        '</tr>'
      );
    }).join('');

    if (lastActivePosSignature !== html) {
      posTbody.innerHTML = html;
      lastActivePosSignature = html;
    }
  }

  function buildHistorySearchKey(h) {
    if (!h || typeof h !== 'object') return '';
    return [
      h.symbol || '',
      h.close_reason || '',
      h.trendline_label || '',
      h.trendline_id || '',
      h.side || '',
      h.closed_at || '',
    ].join(' ').toLowerCase();
  }

  function prepareHistorySearchKeys(history) {
    historyFilterCache = null;
    for (let i = 0; i < history.length; i += 1) {
      const h = history[i];
      if (h && typeof h === 'object' && Object.isExtensible(h)) {
        h._searchKey = buildHistorySearchKey(h);
      }
    }
  }

  function getFilteredAndSortedHistory(history, sortState, getEl) {
    const searchInput = typeof getEl === 'function' ? getEl('history-search-input') : global.document.getElementById('history-search-input');
    const sideSelect = typeof getEl === 'function' ? getEl('history-side-filter') : global.document.getElementById('history-side-filter');
    const outcomeSelect = typeof getEl === 'function' ? getEl('history-outcome-filter') : global.document.getElementById('history-outcome-filter');
    const query = (searchInput ? searchInput.value : '').trim().toLowerCase();
    const sideFilter = sideSelect ? sideSelect.value : '';
    const outcomeFilter = outcomeSelect ? outcomeSelect.value : '';
    const col = sortState.column;
    const asc = sortState.asc;

    if (
      historyFilterCache &&
      historyFilterCache.history === history &&
      historyFilterCache.length === history.length &&
      historyFilterCache.query === query &&
      historyFilterCache.sideFilter === sideFilter &&
      historyFilterCache.outcomeFilter === outcomeFilter &&
      historyFilterCache.col === col &&
      historyFilterCache.asc === asc
    ) {
      return historyFilterCache.rows;
    }

    const filtered = history.filter((h) => {
      if (sideFilter && String(h.side || '').trim().toUpperCase() !== sideFilter) {
        return false;
      }
      if (outcomeFilter && String(h.outcome || '').toUpperCase() !== outcomeFilter) {
        return false;
      }
      if (!query) return true;
      const haystack = typeof h._searchKey === 'string' ? h._searchKey : buildHistorySearchKey(h);
      return haystack.includes(query);
    });

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

    historyFilterCache = {
      history,
      length: history.length,
      query,
      sideFilter,
      outcomeFilter,
      col,
      asc,
      rows: filtered,
    };
    return filtered;
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

  function updateHistoryPaginationControls(totalFiltered, startIdx, endIdx, totalPages, currentPage, getEl) {
    const bar = typeof getEl === 'function' ? getEl('history-pagination-bar') : global.document.getElementById('history-pagination-bar');
    if (!bar) return;
    if (totalFiltered <= HISTORY_PAGE_SIZE) {
      bar.hidden = true;
      return;
    }
    bar.hidden = false;
    setText('history-page-range', '显示第 ' + (startIdx + 1) + ' - ' + endIdx + ' 笔 · 共 ' + totalFiltered + ' 笔', getEl);
    setText('history-page-info', currentPage + ' / ' + totalPages, getEl);

    const prevBtn = typeof getEl === 'function' ? getEl('btn-history-prev-page') : global.document.getElementById('btn-history-prev-page');
    if (prevBtn) {
      const prevDisabled = currentPage <= 1;
      prevBtn.disabled = prevDisabled;
      if (prevBtn.classList) {
        prevBtn.classList.toggle('opacity-75', prevDisabled);
        prevBtn.classList.toggle('pointer-events-none', prevDisabled);
      }
    }
    const nextBtn = typeof getEl === 'function' ? getEl('btn-history-next-page') : global.document.getElementById('btn-history-next-page');
    if (nextBtn) {
      const nextDisabled = currentPage >= totalPages;
      nextBtn.disabled = nextDisabled;
      if (nextBtn.classList) {
        nextBtn.classList.toggle('opacity-75', nextDisabled);
        nextBtn.classList.toggle('pointer-events-none', nextDisabled);
      }
    }
  }

  function renderHistoryTable(accountData, sortState, currentPage, deps) {
    const { getEl, updateSortIndicators } = deps;
    const histTbody = typeof getEl === 'function' ? getEl('history-tbody') : global.document.getElementById('history-tbody');
    if (!histTbody || !accountData) return currentPage;

    const fullHistory = accountData.trade_history;
    const rows = getFilteredAndSortedHistory(fullHistory, sortState, getEl);
    setText(
      'history-count-badge',
      rows.length === fullHistory.length
        ? ('共 ' + fullHistory.length + ' 笔')
        : ('显示 ' + rows.length + ' / 共 ' + fullHistory.length + ' 笔'),
      getEl
    );
    if (typeof updateSortIndicators === 'function') {
      updateSortIndicators();
    }

    const summaryEl = typeof getEl === 'function' ? getEl('history-filtered-summary') : global.document.getElementById('history-filtered-summary');
    if (summaryEl) {
      if (rows.length > 0 && rows.length < fullHistory.length) {
        let sumNet = 0;
        let netCount = 0;
        let winCount = 0;
        for (let i = 0; i < rows.length; i += 1) {
          const item = rows[i];
          if (isFiniteNumber(item.net_pnl)) {
            sumNet += Number(item.net_pnl);
            netCount += 1;
          }
          if (String(item.outcome || '').toUpperCase() === 'WIN') {
            winCount += 1;
          }
        }
        const winRateStr = ((winCount / rows.length) * 100).toFixed(1) + '%';
        const summaryText = netCount > 0
          ? ('筛选净盈亏 ' + formatMoney(sumNet, { signed: true }) + ' · 胜率 ' + winRateStr)
          : ('筛选胜率 ' + winRateStr);
        summaryEl.hidden = false;
        setIfChanged(summaryEl, 'textContent', summaryText);
        setSignedColorClass(summaryEl, netCount > 0 ? sumNet : null, { zeroClass: 'text-[#9fb0c5]' });
      } else {
        summaryEl.hidden = true;
        setIfChanged(summaryEl, 'textContent', '');
      }
    }

    if (rows.length === 0) {
      lastHistoryPageRows = [];
      updateHistoryPaginationControls(0, 0, 0, 1, 1, getEl);
      histTbody.innerHTML = '<tr><td colspan="10" class="py-10 text-center text-[#9fb0c5] text-xs font-sans">' +
        (fullHistory.length === 0 ? '暂无历史开平仓流水记录' : '当前筛选条件下无匹配交易记录') + '</td></tr>';
      return 1;
    }

    const totalPages = Math.max(1, Math.ceil(rows.length / HISTORY_PAGE_SIZE));
    let nextPage = currentPage;
    if (nextPage > totalPages) nextPage = totalPages;
    if (nextPage < 1) nextPage = 1;
    const startIdx = (nextPage - 1) * HISTORY_PAGE_SIZE;
    const endIdx = Math.min(rows.length, startIdx + HISTORY_PAGE_SIZE);
    const pageRows = rows.slice(startIdx, endIdx);
    lastHistoryPageRows = pageRows;
    updateHistoryPaginationControls(rows.length, startIdx, endIdx, totalPages, nextPage, getEl);

    histTbody.innerHTML = pageRows.map((h, idx) => {
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
      const amtStr = formatAmountWithUnit(h.amount, sym, true);
      const notionalStr = isFiniteNumber(h.entry_notional) && Number(h.entry_notional) > 0 ? formatMoney(h.entry_notional) : '--';
      const notionalCellHtml = (notionalStr === '--' && amtStr === '--')
        ? '<span class="text-[#7f91a8]">--</span>'
        : '<div>' + escapeHtml(notionalStr) + '</div><div class="text-[10px] text-[#7f91a8]">' + escapeHtml(amtStr) + '</div>';
      const tlDisplay = formatTrendlineDisplay(h);

      return (
        '<tr class="hover:bg-[#17202e] transition-colors font-mono tabular-nums text-[11px]">' +
          '<td class="px-2.5 sm:px-3.5 py-2.5 text-[#9fb0c5] whitespace-nowrap">' +
            '<div class="text-[#f1f5f9] font-medium">' + cTime + '</div>' +
            entryTimeHtml +
          '</td>' +
          '<td class="px-2.5 sm:px-3.5 py-2.5 font-sans font-bold text-[#f1f5f9] whitespace-nowrap">' +
            '<div class="flex items-center gap-1.5">' +
              '<a href="' + escapeHtml(tvUrl) + '" target="_blank" rel="noopener noreferrer" title="在新标签页打开 ' + safeSym + ' TradingView 图表" class="text-[#f1f5f9] hover:text-[#8ab8ff] hover:underline inline-flex items-center gap-1">' +
                '<span>' + safeSym + '</span>' + EXT_ARROW_SVG +
              '</a>' +
              '<span class="text-[10px] font-mono font-semibold ' + sideCls + '">' + escapeHtml(side) + '</span>' +
              '<button type="button" class="row-kline-chip" data-kline-kind="trade" data-idx="' + idx + '" title="预览 ' + safeSym + ' 15m/1h K线">15m图</button>' +
            '</div>' +
          '</td>' +
          '<td class="px-2.5 sm:px-3.5 py-2.5 text-right text-[#f1f5f9] whitespace-nowrap">' + notionalCellHtml + '</td>' +
          '<td class="px-2.5 sm:px-3.5 py-2.5 text-right ' + (entryStr === '--' ? 'text-[#7f91a8]' : 'text-[#9fb0c5]') + ' whitespace-nowrap">' + escapeHtml(entryStr) + '</td>' +
          '<td class="px-2.5 sm:px-3.5 py-2.5 text-right ' + (exitStr === '--' ? 'text-[#7f91a8]' : 'text-[#f1f5f9] font-semibold') + ' whitespace-nowrap">' + escapeHtml(exitStr) + '</td>' +
          '<td class="px-2.5 sm:px-3.5 py-2.5 text-right whitespace-nowrap ' + pnlCls + '">' + escapeHtml(formatMoney(pnlVal, { signed: true })) + '</td>' +
          '<td class="px-2.5 sm:px-3.5 py-2.5 text-right whitespace-nowrap ' + pctCls + '">' + escapeHtml(pctText) + '</td>' +
          '<td class="px-2.5 sm:px-3.5 py-2.5 text-right ' + (dur === '--' ? 'text-[#7f91a8]' : 'text-[#9fb0c5]') + ' whitespace-nowrap">' + dur + '</td>' +
          '<td class="px-2.5 sm:px-3.5 py-2.5 whitespace-nowrap">' + renderCloseReasonBadge(h.close_reason) + '</td>' +
          '<td class="px-2.5 sm:px-3.5 py-2.5 ' + (tlDisplay !== '--' ? 'text-[#9fb0c5]' : 'text-[#7f91a8]') + ' whitespace-nowrap">' + escapeHtml(tlDisplay) + '</td>' +
        '</tr>'
      );
    }).join('');
    return nextPage;
  }

  global.SecondLegAccountTables = Object.freeze({
    HISTORY_PAGE_SIZE,
    formatMoney,
    formatCryptoPrice,
    formatDollarPrice,
    formatTrendlineDisplay,
    formatRatio,
    setSignedColorClass,
    setText,
    updateKpis,
    renderActivePositions,
    buildHistorySearchKey,
    prepareHistorySearchKeys,
    getFilteredAndSortedHistory,
    renderHistoryTable,
    getLastHistoryPageRows,
    renderApiStatusBadge,
    formatLoadErrorMessage,
    getAccountModalCollection,
    openAccountKlineModal,
  });
})(window);
