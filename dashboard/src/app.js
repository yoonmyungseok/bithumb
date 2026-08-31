// AI Quant Trading Pro - Single Page Application Engine
(function () {
  'use strict';

  // State Management
  const state = {
    currentTab: 'all',
    activeExchange: window.location.port === '7980' ? 'upbit' : 'bithumb',
    customPort: window.location.port || '7979',
    autoRefreshInterval: 5000,
    timerId: null,
    isFetching: false,
    selectedCoin: null,
    lastData: null,
    countdown: 5,
    countdownTimer: null
  };

  // API Helper
  function getApiBaseUrl() {
    if (window.location.port) {
      return '';
    }
    return 'http://127.0.0.1:7979';
  }

  // Formatters
  function formatKrw(num) {
    if (num === undefined || num === null || isNaN(num)) return '0 원';
    return Math.round(num).toLocaleString('ko-KR') + ' 원';
  }

  function formatPct(num) {
    if (num === undefined || num === null || isNaN(num)) return '0.00%';
    const sign = num > 0 ? '+' : '';
    return `${sign}${Number(num).toFixed(2)}%`;
  }

  function formatPrice(price) {
    if (!price || isNaN(price)) return '0';
    const num = Number(price);
    if (num >= 1000) return num.toLocaleString('ko-KR');
    if (num >= 1) return num.toFixed(2);
    return num.toFixed(4);
  }

  // 1. 주문 일시 포맷터 (Unix 타임스탬프 숫자 및 ISO 문자열 완벽 처리)
  function formatOrderTime(ts) {
    if (!ts) return '-';
    let d;
    if (typeof ts === 'number' || (!isNaN(Number(ts)) && !String(ts).includes('-') && !String(ts).includes(':'))) {
      const num = Number(ts);
      d = new Date(num > 1e11 ? num : num * 1000);
    } else {
      d = new Date(String(ts).replace(' ', 'T'));
    }

    if (isNaN(d.getTime())) {
      return String(ts).replace('T', ' ').substring(0, 19);
    }

    const pad = n => String(n).padStart(2, '0');
    const month = pad(d.getMonth() + 1);
    const day = pad(d.getDate());
    const hours = pad(d.getHours());
    const minutes = pad(d.getMinutes());
    const seconds = pad(d.getSeconds());
    return `${month}-${day} ${hours}:${minutes}:${seconds}`;
  }

  // 7대 팩터 적격 수 뱃지 렌더러 (항상 7대 팩터 기준: "X / 7개 적격")
  function renderFactorChips(factors, alphaScore) {
    const sc = Number(alphaScore || 0);
    const f = (factors && typeof factors === 'object') ? factors : {};

    const standardFactors = [
      {
        label: '1H',
        isPass: f.mtf_score !== undefined
          ? Number(f.mtf_score) >= 10
          : (f.mtf_1h_trend === true || (typeof f.mtf_state === 'string' && f.mtf_state.includes('>=')) || sc >= 75),
        val: f.mtf_score ?? (typeof f.mtf_state === 'string' ? '1H추세' : (sc >= 75 ? '10' : '-'))
      },
      {
        label: 'VWAP',
        isPass: f.vwap_score !== undefined
          ? Number(f.vwap_score) >= 10
          : (f.vwap_ratio >= 1.0 || sc >= 70),
        val: f.vwap_score ?? (sc >= 70 ? '10' : '-')
      },
      {
        label: 'MACD',
        isPass: f.macd_score !== undefined
          ? Number(f.macd_score) >= 10
          : (f.macd_accel === true || sc >= 75),
        val: f.macd_score ?? (sc >= 75 ? '10' : '-')
      },
      {
        label: 'RSI',
        isPass: f.rsi_score !== undefined
          ? Number(f.rsi_score) >= 10
          : (f.rsi_golden_zone === true || (typeof f.rsi === 'number' && f.rsi >= 40 && f.rsi <= 68) || sc >= 65),
        val: f.rsi_score ?? (typeof f.rsi === 'number' ? f.rsi.toFixed(1) : (sc >= 65 ? '10' : '-'))
      },
      {
        label: 'BB',
        isPass: (f.bollinger_score !== undefined || f.bb_score !== undefined)
          ? Number(f.bollinger_score ?? f.bb_score) >= 10
          : (f.bollinger_b === true || (typeof f.pct_b === 'number' && f.pct_b >= 0.20 && f.pct_b <= 0.85) || sc >= 65),
        val: f.bollinger_score ?? f.bb_score ?? (typeof f.pct_b === 'number' ? f.pct_b.toFixed(2) : (sc >= 65 ? '10' : '-'))
      },
      {
        label: '수급',
        isPass: (f.orderflow_score !== undefined || f.orderbook_score !== undefined)
          ? Number(f.orderflow_score ?? f.orderbook_score) >= 10
          : (f.orderbook_imbalance === true || (typeof f.orderbook_raw_ratio === 'number' && f.orderbook_raw_ratio >= 1.0) || sc >= 75),
        val: f.orderflow_score ?? f.orderbook_score ?? (typeof f.orderbook_raw_ratio === 'number' ? f.orderbook_raw_ratio.toFixed(2) : (sc >= 75 ? '10' : '-'))
      },
      {
        label: '거래량',
        isPass: (f.volume_score !== undefined || f.vol_score !== undefined)
          ? Number(f.volume_score ?? f.vol_score) >= 7
          : (f.vol_spike === true || sc >= 70),
        val: f.volume_score ?? f.vol_score ?? (sc >= 70 ? '7' : '-')
      }
    ];

    let passCount = 0;
    const details = [];

    standardFactors.forEach(({ label, isPass, val }) => {
      if (isPass) passCount++;
      details.push(`${label}:${val}(${isPass ? '✓' : '✕'})`);
    });

    const totalCount = 7;
    const tooltip = `7대 팩터 상세: ${details.join(', ')}`;
    if (passCount >= 6) {
      return `<span class="px-2.5 py-0.5 rounded-full text-xs font-mono font-bold bg-emerald-500/20 text-emerald-300 border border-emerald-500/40 inline-flex items-center gap-1 cursor-help" title="${tooltip}">
        <span>✅</span><span>${passCount} / ${totalCount}개 적격</span>
      </span>`;
    }
    if (passCount >= 4) {
      return `<span class="px-2.5 py-0.5 rounded-full text-xs font-mono font-bold bg-blue-500/20 text-blue-300 border border-blue-500/40 inline-flex items-center gap-1 cursor-help" title="${tooltip}">
        <span>🔵</span><span>${passCount} / ${totalCount}개 적격</span>
      </span>`;
    }
    return `<span class="px-2.5 py-0.5 rounded-full text-xs font-mono font-medium bg-slate-800 text-slate-400 border border-slate-700 inline-flex items-center gap-1 cursor-help" title="${tooltip}">
      <span>⚪</span><span>${passCount} / ${totalCount}개 적격</span>
    </span>`;
  }

  // 7대 알파 스코어 조건부 서식 뱃지
  function renderAlphaBadge(score) {
    const sc = Number(score || 0);
    if (sc >= 85) {
      return `<span class="px-2.5 py-1 rounded-lg text-xs font-mono font-black bg-amber-500/20 text-amber-300 border border-amber-500/40 shadow-sm shadow-amber-500/20 flex items-center gap-1">
        <span>🔥</span><span>${sc}점 (A+특급)</span>
      </span>`;
    }
    if (sc >= 75) {
      return `<span class="px-2.5 py-1 rounded-lg text-xs font-mono font-bold bg-emerald-500/20 text-emerald-300 border border-emerald-500/40 flex items-center gap-1">
        <span>🟢</span><span>${sc}점 (승인)</span>
      </span>`;
    }
    if (sc >= 60) {
      return `<span class="px-2.5 py-1 rounded-lg text-xs font-mono font-bold bg-blue-500/20 text-blue-300 border border-blue-500/40 flex items-center gap-1">
        <span>🔵</span><span>${sc}점 (적격)</span>
      </span>`;
    }
    if (sc >= 50) {
      return `<span class="px-2.5 py-1 rounded-lg text-xs font-mono font-semibold bg-slate-700/60 text-slate-300 border border-slate-600 flex items-center gap-1">
        <span>⚪</span><span>${sc}점 (관망)</span>
      </span>`;
    }
    return `<span class="px-2.5 py-1 rounded-lg text-xs font-mono font-semibold bg-rose-500/20 text-rose-300 border border-rose-500/30 flex items-center gap-1">
      <span>🛑</span><span>${sc}점 (미달)</span>
    </span>`;
  }

  // AI 행동 뱃지
  function renderActionBadge(action) {
    const act = (action || '').toUpperCase();
    if (act === 'BUY' || act === 'BID' || act.includes('STRONG_BUY')) {
      return `<span class="px-2 py-0.5 rounded text-xs font-bold bg-emerald-500/20 text-emerald-300 border border-emerald-500/40">🚀 매수 승인</span>`;
    }
    if (act === 'SELL' || act === 'ASK' || act.includes('EXIT')) {
      return `<span class="px-2 py-0.5 rounded text-xs font-bold bg-rose-500/20 text-rose-300 border border-rose-500/40">🚨 즉시 청산</span>`;
    }
    if (act.includes('PARTIAL') || act.includes('TP')) {
      return `<span class="px-2 py-0.5 rounded text-xs font-bold bg-blue-500/20 text-blue-300 border border-blue-500/40">🎯 분할 익절</span>`;
    }
    if (act.includes('TRAILING')) {
      return `<span class="px-2 py-0.5 rounded text-xs font-bold bg-indigo-500/20 text-indigo-300 border border-indigo-500/40">🚀 트레일링</span>`;
    }
    if (act.includes('HOLD') || act.includes('WATCH')) {
      return `<span class="px-2 py-0.5 rounded text-xs font-bold bg-slate-700/60 text-slate-300 border border-slate-600">🛡️ 관망/유지</span>`;
    }
    return `<span class="px-2 py-0.5 rounded text-xs font-bold bg-slate-800 text-slate-400 border border-slate-700">${act || '-'}</span>`;
  }

  // 체결 사유 한글 변환
  function formatReason(r) {
    if (!r) return '-';
    return String(r)
      .replace(/MOMENTUM_EARLY_EXIT/gi, '⚡ 모멘텀 조기 본전탈출')
      .replace(/MOMENTUM_EXIT/gi, '⚡ 모멘텀 조기 탈출')
      .replace(/TIME_STOP/gi, '⏳ 타임스탑 (횡보 청산)')
      .replace(/TRAILING_STOP_PARTIAL/gi, '🚀 트레일링 분할익절')
      .replace(/TRAILING_STOP/gi, '🚀 가속 트레일링 익절')
      .replace(/PARTIAL_TP_1/gi, '🎯 1차 분할익절 (+2.5%)')
      .replace(/PARTIAL_TP_2/gi, '🎯 2차 분할익절 (+5.0%)')
      .replace(/PARTIAL_TP/gi, '🎯 1차 분할익절')
      .replace(/TAKE_PROFIT/gi, '🎯 목표가 전량익절')
      .replace(/PROFIT_TAKE/gi, '🎯 목표가 전량익절')
      .replace(/HARD_STOP/gi, '🛡️ 비상 하드스탑 손절')
      .replace(/STOP_LOSS/gi, '🛡️ 손절매 방어')
      .replace(/PANIC_SELL/gi, '🚨 긴급 전량매도')
      .replace(/MANUAL_EXIT/gi, '👤 수동 청산')
      .replace(/MANUAL/gi, '👤 수동 제어')
      .replace(/REGIME_FILTER/gi, '🛑 시장 급락 방어 청산')
      .replace(/REGIME_CRASH/gi, '🚨 BTC 급락 경보 청산');
  }

  // 주문 상태 뱃지
  function formatOrderStatusBadge(status) {
    if (!status) return '<span class="text-slate-500">-</span>';
    const s = String(status).toUpperCase();
    if (s === 'FILLED' || s === 'DONE') {
      return '<span class="px-2 py-0.5 rounded text-xs font-bold bg-emerald-500/20 text-emerald-300 border border-emerald-500/40 whitespace-nowrap">✅ 체결 완료</span>';
    }
    if (s === 'RECONCILIATION_PENDING') {
      return '<span class="px-2 py-0.5 rounded text-xs font-bold bg-amber-500/20 text-amber-300 border border-amber-500/40 whitespace-nowrap">🔄 체결 대사중</span>';
    }
    if (s === 'RECONCILED') {
      return '<span class="px-2 py-0.5 rounded text-xs font-bold bg-emerald-500/20 text-emerald-300 border border-emerald-500/40 whitespace-nowrap">✅ 대사 완료</span>';
    }
    if (s === 'PARTIALLY_FILLED') {
      return '<span class="px-2 py-0.5 rounded text-xs font-bold bg-amber-500/20 text-amber-300 border border-amber-500/40 whitespace-nowrap">🟡 부분 체결</span>';
    }
    if (s === 'OPEN' || s === 'WAIT' || s === 'ACK' || s === 'PENDING' || s === 'SUBMITTED') {
      return '<span class="px-2 py-0.5 rounded text-xs font-bold bg-blue-500/20 text-blue-300 border border-blue-500/40 whitespace-nowrap">⏳ 접수/대기</span>';
    }
    if (s === 'CANCELLED' || s === 'CANCELED' || s === 'CANCEL') {
      return '<span class="px-2 py-0.5 rounded text-xs font-semibold bg-slate-800 text-slate-400 border border-slate-700 whitespace-nowrap">❌ 취소 완료</span>';
    }
    if (s === 'REJECTED') {
      return '<span class="px-2 py-0.5 rounded text-xs font-bold bg-rose-500/20 text-rose-300 border border-rose-500/40 whitespace-nowrap">🛑 주문 거절</span>';
    }
    if (s === 'UNKNOWN') {
      return '<span class="px-2 py-0.5 rounded text-xs font-medium bg-purple-500/20 text-purple-300 border border-purple-500/40 whitespace-nowrap">❓ 확인 필요</span>';
    }
    return `<span class="px-2 py-0.5 rounded text-xs font-medium bg-slate-800 text-slate-300 border border-slate-700 whitespace-nowrap">${status}</span>`;
  }

  // 2. 거래 구분 뱃지 (긴 문장이 들어와도 간결하고 깔끔하게 분류)
  function formatTradeSideBadge(side, isWin, pnlKrw) {
    if (!side) return '<span class="px-2 py-0.5 rounded text-xs font-semibold bg-slate-800 text-slate-300">매도</span>';
    const s = String(side).toUpperCase();
    if (s === 'BID' || s === 'BUY' || s === '매수') {
      return '<span class="px-2 py-0.5 rounded text-xs font-bold bg-emerald-500/20 text-emerald-300 border border-emerald-500/40 whitespace-nowrap">매수</span>';
    }
    if (s.includes('TIME_STOP') || s.includes('타임스탑')) {
      return '<span class="px-2 py-0.5 rounded text-xs font-bold bg-amber-500/20 text-amber-300 border border-amber-500/40 whitespace-nowrap">타임스탑</span>';
    }
    if (s.includes('MOMENTUM') || s.includes('모멘텀')) {
      return '<span class="px-2 py-0.5 rounded text-xs font-bold bg-cyan-500/20 text-cyan-300 border border-cyan-500/40 whitespace-nowrap">모멘텀탈출</span>';
    }
    if (s.includes('TRAILING') || s.includes('트레일링')) {
      return '<span class="px-2 py-0.5 rounded text-xs font-bold bg-indigo-500/20 text-indigo-300 border border-indigo-500/40 whitespace-nowrap">트레일링</span>';
    }
    if (s.includes('TP') || s.includes('PARTIAL') || s.includes('익절')) {
      return '<span class="px-2 py-0.5 rounded text-xs font-bold bg-blue-500/20 text-blue-300 border border-blue-500/40 whitespace-nowrap">분할익절</span>';
    }
    if (s.includes('PANIC') || s.includes('긴급')) {
      return '<span class="px-2 py-0.5 rounded text-xs font-bold bg-rose-600 text-white whitespace-nowrap">긴급매도</span>';
    }
    if (s.includes('손절') || s.includes('STOP_LOSS') || s.includes('HARD_STOP') || s.includes('웹소켓 손절')) {
      return '<span class="px-2 py-0.5 rounded text-xs font-bold bg-rose-500/20 text-rose-300 border border-rose-500/40 whitespace-nowrap">손절</span>';
    }
    if (pnlKrw !== undefined && pnlKrw > 0) {
      return '<span class="px-2 py-0.5 rounded text-xs font-bold bg-emerald-500/20 text-emerald-300 border border-emerald-500/40 whitespace-nowrap">익절매도</span>';
    }
    return '<span class="px-2 py-0.5 rounded text-xs font-bold bg-rose-500/20 text-rose-300 border border-rose-500/40 whitespace-nowrap">손절매도</span>';
  }

  // Fetch Status Data
  async function fetchStatus() {
    if (state.isFetching) return;
    state.isFetching = true;

    const connStatusEl = document.getElementById('connection-status');
    try {
      const baseUrl = getApiBaseUrl();
      const res = await fetch(`${baseUrl}/api/status`, {
        headers: { 'Accept': 'application/json' }
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);

      const data = await res.json();
      state.lastData = data;
      renderDashboard(data);

      if (connStatusEl) {
        connStatusEl.innerHTML = `
          <span class="inline-block w-2.5 h-2.5 rounded-full bg-emerald-500 pulse-dot mr-2"></span>
          <span class="text-xs font-medium text-emerald-400">통합 게이트웨이 정상 연결됨 (:7979)</span>
        `;
      }
      state.countdown = 5;
    } catch (err) {
      console.warn('대시보드 데이터 수신 실패:', err);
      if (connStatusEl) {
        connStatusEl.innerHTML = `
          <span class="inline-block w-2.5 h-2.5 rounded-full bg-rose-500 mr-2"></span>
          <span class="text-xs font-medium text-rose-400">연결 끊김 (${err.message})</span>
        `;
      }
    } finally {
      state.isFetching = false;
    }
  }

  // Render Core Dashboard Data
  function renderDashboard(data) {
    if (!data) return;

    // 통합 게이트웨이 응답 구조 지원 (combined, bithumb, upbit)
    let d = data;
    if (data.combined) {
      if (state.activeExchange === 'upbit') {
        d = data.upbit || data.combined;
      } else if (state.activeExchange === 'bithumb') {
        d = data.bithumb || data.combined;
      } else {
        d = data.combined;
      }
    }

    // Header Title
    const titleEl = document.getElementById('bot-header-title');
    if (titleEl && d.title) {
      titleEl.innerText = d.title;
    }

    // 1. Total Equity
    const totalEqEl = document.getElementById('total_equity');
    if (totalEqEl) totalEqEl.innerText = formatKrw(d.total_equity);

    const krwAvailEl = document.getElementById('krw_avail');
    if (krwAvailEl) krwAvailEl.innerText = formatKrw(d.krw_available);

    // 2. Daily PnL
    const dailyPnlKrw = d.daily_pnl_krw || 0;
    const dailyPnlPct = d.daily_pnl_pct || 0;
    const dailyPnlEl = document.getElementById('daily_pnl');
    if (dailyPnlEl) {
      dailyPnlEl.innerText = `${formatKrw(dailyPnlKrw)} (${formatPct(dailyPnlPct)})`;
      dailyPnlEl.className = `text-2xl font-bold mt-1 ${dailyPnlKrw >= 0 ? 'text-emerald-400' : 'text-rose-400'}`;
    }

    const startEqEl = document.getElementById('start_equity');
    if (startEqEl) startEqEl.innerText = formatKrw(d.daily_start_equity);

    // 3. Realized PnL
    const realPnl = d.realized_pnl_krw || 0;
    const realPnlEl = document.getElementById('realized_pnl');
    if (realPnlEl) {
      realPnlEl.innerText = `${realPnl >= 0 ? '+' : ''}${formatKrw(realPnl)}`;
      realPnlEl.className = `text-2xl font-bold mt-1 ${realPnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}`;
    }

    const tradeStatsEl = document.getElementById('trade_stats');
    if (tradeStatsEl) {
      tradeStatsEl.innerText = `${d.total_trades || 0}회 중 ${d.win_trades || 0}승 (승률 ${(d.win_rate || 0).toFixed(0)}%)`;
    }

    // 4. Fear & Greed / BTC Market Regime
    const fgEl = document.getElementById('fear_greed');
    if (fgEl) fgEl.innerText = d.fear_and_greed || '-';

    const btcRegime = String(d.btc_regime || 'NORMAL').toUpperCase();
    const btcBadgeEl = document.getElementById('btc_regime_badge');
    if (btcBadgeEl) {
      if (btcRegime === 'RISK_OFF') {
        btcBadgeEl.innerText = '🟡 약세 조정장';
        btcBadgeEl.className = 'px-2.5 py-0.5 rounded-full text-xs font-semibold badge-regime-warn';
      } else if (btcRegime === 'CRASH') {
        btcBadgeEl.innerText = '🚨 급락 경보';
        btcBadgeEl.className = 'px-2.5 py-0.5 rounded-full text-xs font-semibold badge-regime-danger';
      } else {
        btcBadgeEl.innerText = '🟢 정상장';
        btcBadgeEl.className = 'px-2.5 py-0.5 rounded-full text-xs font-semibold badge-regime-normal';
      }
    }

    const btcDescEl = document.getElementById('btc_regime_desc');
    if (btcDescEl) {
      btcDescEl.innerText = d.btc_regime_reason ? `BTC: ${d.btc_regime_reason}` : (btcRegime === 'RISK_OFF' ? '1H EMA50 하회' : '1H EMA50 상회 안정세');
    }

    const botStateEl = document.getElementById('bot_state');
    if (botStateEl) {
      botStateEl.innerText = d.bot_state || '🟢 정상 가동 중';
    }

    // Tab Counts
    const positions = d.positions || [];
    const candidates = d.candidates || [];
    const countPosEl = document.getElementById('count_positions');
    const countCandEl = document.getElementById('count_candidates');
    if (countPosEl) countPosEl.innerText = positions.length;
    if (countCandEl) countCandEl.innerText = candidates.length;

    // Render Tables
    renderPositionsTable(positions);
    renderCandidatesTable(candidates);
    renderRecentTradesTable(d.recent_trades || []);
    renderOrderJournalTable(d.recent_orders || []);
  }

  // Render Positions Table
  function renderPositionsTable(positions) {
    const tbody = document.getElementById('positions_tbody');
    if (!tbody) return;

    if (!positions || positions.length === 0) {
      tbody.innerHTML = `
        <tr>
          <td colspan="8" class="p-8 text-center text-slate-500">
            <div class="text-3xl mb-2">💼</div>
            <div class="text-sm font-medium">현재 보유 중인 포지션이 없습니다. (100% 현금 대기 중)</div>
          </td>
        </tr>
      `;
      return;
    }

    tbody.innerHTML = positions.map(pos => {
      const pnlPct = Number(pos.pnl_pct || 0);
      const pnlKrw = Number(pos.pnl_krw || 0);
      const isProfit = pnlPct >= 0;
      const pnlCls = isProfit ? 'text-emerald-400' : 'text-rose-400';
      const targetStr = pos.target_price > 0 ? `${formatPrice(pos.target_price)} 원 (${pos.target_pct >= 0 ? '+' : ''}${(pos.target_pct || 0).toFixed(1)}%)` : '-';
      const stopStr = pos.stop_loss > 0 ? `${formatPrice(pos.stop_loss)} 원 (${pos.stop_pct || 0}%)` : '-';

      return `
        <tr class="hover:bg-slate-800/40 transition-colors border-b border-slate-800/80">
          <td class="p-3 whitespace-nowrap">
            <div class="font-bold text-slate-100 flex items-center gap-1.5 cursor-pointer hover:text-blue-400" onclick="window.showChartModal('${pos.market}')">
              <span>${pos.korean_name || pos.market}</span>
              <span class="text-xs text-slate-400 font-normal">(${pos.market})</span>
              <span class="text-xs text-blue-400">📈</span>
            </div>
          </td>
          <td class="p-3 whitespace-nowrap">
            <div class="font-medium text-slate-200">${formatPrice(pos.current_price)} 원</div>
            <div class="text-xs text-slate-400">평단: ${formatPrice(pos.avg_buy_price)} 원</div>
          </td>
          <td class="p-3 whitespace-nowrap">
            <div class="font-medium text-slate-200">${formatKrw(pos.value || pos.total_val)}</div>
            <div class="text-xs text-slate-400">${Number(pos.balance || pos.volume || 0).toFixed(4)} 개</div>
          </td>
          <td class="p-3 whitespace-nowrap font-bold ${pnlCls}">
            <div>${formatPct(pnlPct)}</div>
            <div class="text-xs font-normal opacity-80">${pnlKrw !== 0 ? (pnlKrw > 0 ? '+' : '') + formatKrw(pnlKrw) : ''}</div>
          </td>
          <td class="p-3 whitespace-nowrap">
            ${renderActionBadge(pos.action)}
          </td>
          <td class="p-3 whitespace-nowrap text-xs">
            <div class="text-emerald-400">목표: ${targetStr}</div>
            <div class="text-rose-400">손절: ${stopStr}</div>
          </td>
          <td class="p-3 whitespace-nowrap">
            ${renderAlphaBadge(pos.alpha_score)}
          </td>
          <td class="p-3 text-xs text-slate-300 max-w-xs break-words">
            ${formatReason(pos.reason)}
          </td>
        </tr>
      `;
    }).join('');
  }

  // Render Candidates Watchlist Table
  function renderCandidatesTable(candidates) {
    const tbody = document.getElementById('candidates_tbody');
    if (!tbody) return;

    if (!candidates || candidates.length === 0) {
      tbody.innerHTML = `
        <tr>
          <td colspan="7" class="p-8 text-center text-slate-500">
            <div class="text-3xl mb-2">🎯</div>
            <div class="text-sm font-medium">현재 진입 기준을 통과한 신규 스캔 후보 종목이 없습니다.</div>
          </td>
        </tr>
      `;
      return;
    }

    tbody.innerHTML = candidates.map((cand, idx) => {
      const candidateTypeBadge = cand.candidate_type === 'EARLY_BREAKOUT'
        ? '<span class="text-xs text-emerald-400">🌱 초기 돌파</span>'
        : '<span class="text-xs text-blue-400">📈 확인형</span>';
      const rawRr = Number(cand.risk_reward_ratio || cand.rr_ratio || 0);
      let rrDisplay = rawRr;
      if (rrDisplay <= 0 && cand.target_pct && cand.stop_pct) {
        rrDisplay = Math.abs(cand.target_pct) / Math.max(0.1, Math.abs(cand.stop_pct));
      }
      if (rrDisplay <= 0) rrDisplay = 1.0;
      const rrStr = rrDisplay.toFixed(1);

      const targetStr = cand.target_price > 0 ? `${formatPrice(cand.target_price)} 원 (${cand.target_pct >= 0 ? '+' : ''}${(cand.target_pct || 0).toFixed(1)}%)` : '-';
      const stopStr = cand.stop_loss > 0 ? `${formatPrice(cand.stop_loss)} 원 (${cand.stop_pct || 0}%)` : '-';

      return `
        <tr class="hover:bg-slate-800/40 transition-colors border-b border-slate-800/80">
          <td class="p-3 whitespace-nowrap">
            <div class="font-bold text-slate-100 flex items-center gap-1.5 cursor-pointer hover:text-blue-400" onclick="window.showChartModal('${cand.market}')">
              <span class="text-slate-500 text-xs font-mono">#${idx + 1}</span>
              <span>${cand.korean_name || cand.market}</span>
              <span class="text-xs text-slate-400 font-normal">(${cand.market})</span>
              ${candidateTypeBadge}
            </div>
          </td>
          <td class="p-3 whitespace-nowrap font-medium text-slate-200">
            ${formatPrice(cand.current_price)} 원
          </td>
          <td class="p-3 whitespace-nowrap">
            ${renderAlphaBadge(cand.alpha_score)}
          </td>
          <td class="p-3 whitespace-nowrap">
            ${renderActionBadge(cand.action || (cand.allow_buy ? 'BUY' : 'HOLD'))}
          </td>
          <td class="p-3 whitespace-nowrap text-xs">
            <div class="text-emerald-400">목표: ${targetStr}</div>
            <div class="text-rose-400">손절: ${stopStr}</div>
          </td>
          <td class="p-3 whitespace-nowrap text-xs">
            <span class="px-2 py-0.5 rounded font-mono font-bold bg-amber-500/10 text-amber-300 border border-amber-500/30">
              ${rrStr} : 1
            </span>
          </td>
          <td class="p-3 text-xs text-slate-300 max-w-xs break-words">
            ${formatReason(cand.reason)}
          </td>
        </tr>
      `;
    }).join('');
  }

  // Render Recent Completed Trades Table (구분 간결화 및 체결 사유 가로 레이아웃 보장)
  function renderRecentTradesTable(trades) {
    const tbody = document.getElementById('trades_tbody');
    if (!tbody) return;

    if (!trades || trades.length === 0) {
      tbody.innerHTML = `<tr><td colspan="5" class="p-4 text-center text-slate-500">완료된 거래 기록이 없습니다.</td></tr>`;
      return;
    }

    tbody.innerHTML = trades.map(t => {
      const pnlKrw = Number(t.pnl_krw || 0);
      const isProfit = pnlKrw >= 0;
      const pnlCls = isProfit ? 'text-emerald-400' : 'text-rose-400';
      const sideBadge = formatTradeSideBadge(t.side, isProfit, pnlKrw);
      const reasonDisplay = formatReason(t.reason || t.exit_reason || t.side);

      return `
        <tr class="hover:bg-slate-800/30 border-b border-slate-800/60">
          <td class="p-2.5 whitespace-nowrap text-slate-400 font-mono text-xs">${formatOrderTime(t.timestamp)}</td>
          <td class="p-2.5 whitespace-nowrap font-medium text-slate-200">${t.korean_name || t.market}</td>
          <td class="p-2.5 whitespace-nowrap text-center">${sideBadge}</td>
          <td class="p-2.5 whitespace-nowrap font-bold ${pnlCls}">${pnlKrw !== 0 ? (pnlKrw > 0 ? '+' : '') + formatKrw(pnlKrw) : '-'}</td>
          <td class="p-2.5 text-xs text-slate-300 min-w-[180px] break-words">${reasonDisplay}</td>
        </tr>
      `;
    }).join('');
  }

  // Render Order Journal Table
  function renderOrderJournalTable(orders) {
    const tbody = document.getElementById('orders_tbody');
    if (!tbody) return;

    if (!orders || orders.length === 0) {
      tbody.innerHTML = `<tr><td colspan="5" class="p-4 text-center text-slate-500">주문 저널 기록이 없습니다.</td></tr>`;
      return;
    }

    tbody.innerHTML = orders.map(o => {
      const isBuy = (o.side || '').toLowerCase() === 'bid' || (o.side || '').toLowerCase() === 'buy';

      return `
        <tr class="hover:bg-slate-800/30 border-b border-slate-800/60">
          <td class="p-2.5 whitespace-nowrap text-slate-300 font-mono text-xs">${formatOrderTime(o.timestamp)}</td>
          <td class="p-2.5 whitespace-nowrap font-medium text-slate-200">${o.korean_name || o.market}</td>
          <td class="p-2.5 whitespace-nowrap">
            <span class="px-2 py-0.5 rounded text-xs font-bold ${isBuy ? 'bg-emerald-500/20 text-emerald-300' : 'bg-rose-500/20 text-rose-300'}">
              ${isBuy ? '매수' : '매도'}
            </span>
          </td>
          <td class="p-2.5 whitespace-nowrap">
            ${formatOrderStatusBadge(o.status)}
          </td>
          <td class="p-2.5 whitespace-nowrap text-xs text-slate-300 font-mono">
            ${formatPrice(o.avg_price || o.price)} 원
          </td>
        </tr>
      `;
    }).join('');
  }

  // Quick Action Handler with Confirmation
  window.triggerAction = async function (actionName) {
    const actionTitles = {
      panic: '🚨 [긴급 전량 매도] 정말로 모든 보유 코인을 시장가로 전량 매도하고 봇을 일시정지하시겠습니까?',
      pause: '⏸️ [일시정지] 신규 매수를 중단하고 관망 모드로 전환하시겠습니까? (기존 보유분의 손절/익절은 유지됩니다)',
      resume: '▶️ [매매 재개] 자동매매 및 신규 진입 분석을 다시 가동하시겠습니까?'
    };

    const confirmMsg = actionTitles[actionName] || `${actionName} 명령을 실행하시겠습니까?`;
    if (!confirm(confirmMsg)) return;

    try {
      const baseUrl = getApiBaseUrl();
      const targetEx = state.activeExchange || 'all';
      const res = await fetch(`${baseUrl}/api/action/${actionName}?exchange=${targetEx}`, {
        method: 'POST'
      });
      const result = await res.json();
      alert(`[결과] ${result.message || '명령이 성공적으로 전달되었습니다.'}`);
      fetchStatus();
    } catch (err) {
      alert(`[오류] 명령 실행 실패: ${err.message}`);
    }
  };

  // Tab Switcher
  window.switchStrategyTab = function (tab) {
    state.currentTab = tab;
    document.querySelectorAll('.tab-button').forEach(btn => btn.classList.remove('active'));
    const activeBtn = document.getElementById(`tab_${tab}`);
    if (activeBtn) activeBtn.classList.add('active');

    const secPos = document.getElementById('section_positions');
    const secCand = document.getElementById('section_candidates');

    if (tab === 'all') {
      if (secPos) secPos.style.display = 'block';
      if (secCand) secCand.style.display = 'block';
    } else if (tab === 'positions') {
      if (secPos) secPos.style.display = 'block';
      if (secCand) secCand.style.display = 'none';
    } else if (tab === 'candidates') {
      if (secPos) secPos.style.display = 'none';
      if (secCand) secCand.style.display = 'block';
    }
  };

  // Exchange Switcher
  window.switchExchange = function (exchange) {
    state.activeExchange = exchange;
    document.querySelectorAll('.exchange-tab-btn').forEach(btn => {
      btn.classList.remove('bg-blue-600', 'text-white');
      btn.classList.add('text-slate-400');
    });
    const activeBtn = document.getElementById(`exchange_${exchange}`);
    if (activeBtn) {
      activeBtn.classList.add('bg-blue-600', 'text-white');
      activeBtn.classList.remove('text-slate-400');
    }
    if (state.lastData) {
      renderDashboard(state.lastData);
    }
    fetchStatus();
  };

  // Chart Modal Handler
  window.showChartModal = function (market) {
    const modal = document.getElementById('chart-modal');
    const container = document.getElementById('chart-container');
    const titleEl = document.getElementById('chart-coin-title');
    if (!modal || !container) return;

    state.selectedCoin = market;
    const cleanSymbol = market.replace('KRW-', '');
    if (titleEl) titleEl.innerText = `${market} 실시간 인터랙티브 차트 (TradingView)`;

    container.innerHTML = `
      <div id="tradingview_widget" style="height: 480px; width: 100%;"></div>
    `;

    if (window.TradingView) {
      new window.TradingView.widget({
        autosize: true,
        symbol: `${state.activeExchange === 'upbit' ? 'UPBIT' : 'BITHUMB'}:${cleanSymbol}KRW`,
        interval: '15',
        timezone: 'Asia/Seoul',
        theme: 'dark',
        style: '1',
        locale: 'kr',
        toolbar_bg: '#0b0e14',
        enable_publishing: false,
        allow_symbol_change: true,
        container_id: 'tradingview_widget'
      });
    } else {
      container.innerHTML = `
        <div class="p-12 text-center text-slate-400">
          <p class="mb-2">트레이딩뷰 차트 로드 중...</p>
          <a href="https://www.tradingview.com/symbols/${cleanSymbol}KRW/" target="_blank" class="text-blue-400 hover:underline">TradingView에서 직접 보기 ↗</a>
        </div>
      `;
    }

    modal.classList.remove('hidden');
  };

  window.closeChartModal = function () {
    const modal = document.getElementById('chart-modal');
    if (modal) modal.classList.add('hidden');
  };

  // Setup Countdown and Periodic Polling
  function setupPolling() {
    fetchStatus();
    state.timerId = setInterval(fetchStatus, state.autoRefreshInterval);
    state.countdownTimer = setInterval(() => {
      state.countdown = Math.max(0, state.countdown - 1);
      const cdEl = document.getElementById('refresh-countdown');
      if (cdEl) cdEl.innerText = `${state.countdown}s`;
      if (state.countdown === 0) state.countdown = 5;
    }, 1000);
  }

  // Initialize on DOM Ready
  document.addEventListener('DOMContentLoaded', () => {
    window.switchExchange('combined');
    setupPolling();
  });

})();
