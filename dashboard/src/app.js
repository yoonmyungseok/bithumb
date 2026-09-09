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

  // 전략 모드 표시 라벨 (내부 strategy_mode 값은 유지)
  function formatStrategyModeLabel(mode) {
    const normalized = String(mode || '').toUpperCase();
    if (normalized === 'NEW_LISTING') return '🆕 신규상장 단타';
    if (normalized === 'SWING') return '🌊 스윙 추세';
    if (normalized === 'SCALP') return '⚡ 단타';
    return mode || '';
  }

  // AI 행동 뱃지
  function renderActionBadge(action) {
    const act = (action || '').toUpperCase();
    if (act === 'BUY' || act === 'BID' || act.includes('STRONG_BUY')) {
      return `<span class="px-2 py-0.5 rounded text-xs font-bold bg-emerald-500/20 text-emerald-300 border border-emerald-500/40">🚀 매수 승인</span>`;
    }
    if (act.includes('EMERGENCY') || act.includes('PANIC')) {
      return `<span class="px-2 py-0.5 rounded text-xs font-bold bg-rose-600 text-white border border-rose-500 whitespace-nowrap">🚨 비상 탈출</span>`;
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
    if (act.includes('TIGHTEN')) {
      return `<span class="px-2 py-0.5 rounded text-xs font-bold bg-amber-500/20 text-amber-300 border border-amber-500/40">🛡️ 손절선 상향</span>`;
    }
    if (act.includes('RUNNER')) {
      return `<span class="px-2 py-0.5 rounded text-xs font-bold bg-purple-500/20 text-purple-300 border border-purple-500/40">🏃 추세 홀딩</span>`;
    }
    if (act.includes('STOP') || act.includes('LOSS')) {
      return `<span class="px-2 py-0.5 rounded text-xs font-bold bg-rose-500/20 text-rose-300 border border-rose-500/40">🛡️ 손절 방어</span>`;
    }
    if (act.includes('HOLD') || act.includes('WATCH')) {
      return `<span class="px-2 py-0.5 rounded text-xs font-bold bg-slate-700/60 text-slate-300 border border-slate-600">🛡️ 관망/유지</span>`;
    }
    if (act.includes('FAIL') || act.includes('ERROR')) {
      return `<span class="px-2 py-0.5 rounded text-xs font-bold bg-rose-500/20 text-rose-300 border border-rose-500/40">❌ 실패</span>`;
    }
    return `<span class="px-2 py-0.5 rounded text-xs font-bold bg-slate-800 text-slate-400 border border-slate-700">${act || '-'}</span>`;
  }

  // 체결 및 전략 사유 한글 변환
  function formatReason(r) {
    if (!r) return '-';
    return String(r)
      // AI 관련 비상/손절 사유
      .replace(/AI_EMERGENCY_EXIT/gi, '🚨 AI 긴급 비상탈출')
      .replace(/AI_TIGHTENED_STOP/gi, '🤖🛡️ AI 손절선 상향 대응')
      .replace(/AI_EXIT/gi, '🤖 AI 청산')
      .replace(/AI_STOP/gi, '🤖 AI 방어 손절')
      .replace(/EMERGENCY_EXIT/gi, '🚨 긴급 비상탈출')
      .replace(/TIGHTENED_STOP/gi, '🛡️ 손절선 상향 방어')
      .replace(/TIGHTEN_STOP/gi, '🛡️ 손절선 상향 방어')
      .replace(/RUNNER_HOLD/gi, '🏃 추세 추종 홀딩')
      // 탈출/청산 사유
      .replace(/NEW_LISTING_EARLY_EXIT/gi, '⚡ 신규상장 조기탈출 (30분)')
      .replace(/NEW_LISTING_TIME_STOP/gi, '⏳ 신규상장 타임스탑 (60분)')
      .replace(/SWING_TREND_STOP/gi, '🌊 스윙 추세 이탈')
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
      .replace(/REGIME_CRASH/gi, '🚨 BTC 급락 경보 청산')
      // 진입 및 전략 팩터 관련
      .replace(/MOMENTUM_BREAKOUT/gi, '💥 모멘텀 돌파')
      .replace(/NEW_LISTING/gi, '🆕 신규상장 단타')
      .replace(/MOMENTUM_PULLBACK/gi, '🌊 눌림목 반등')
      .replace(/VOLATILITY_BREAKOUT/gi, '💥 변동성 돌파')
      .replace(/EARLY_BREAKOUT/gi, '🌱 초기 돌파')
      .replace(/CONFIRMED_BREAKOUT/gi, '📈 확인형 돌파')
      .replace(/ORDERBOOK_IMBALANCE/gi, '📊 호가 불균형')
      .replace(/MARKET_COOLDOWN/gi, '⏳ 재진입 쿨다운')
      .replace(/DAILY_LOSS_LIMIT/gi, '🛑 일일 손실 한도 도달')
      .replace(/CIRCUIT_BREAKER/gi, '⚡ 서킷 브레이커')
      .replace(/KILL_SWITCH/gi, '🛑 킬스위치 활성화')
      .replace(/MAX_POSITIONS_REACHED/gi, '⚠️ 최대 포지션 도달')
      .replace(/MAX_POSITIONS/gi, '⚠️ 최대 포지션 도달')
      .replace(/MAX_POSITION/gi, '⚠️ 최대 포지션 도달')
      .replace(/INSUFFICIENT_BALANCE/gi, '💰 잔고 부족')
      .replace(/LOW_ALPHA_SCORE/gi, '📉 알파 점수 미달')
      .replace(/DISPARITY_OVERHEAT/gi, '🌡️ 이격도 과열')
      .replace(/REGIME_BEAR/gi, '🐻 약세장 진입 제한')
      .replace(/BTC_REGIME_RISK_OFF/gi, '🛑 BTC 약세 리스크 오프')
      .replace(/BTC_CRASH/gi, '🚨 BTC 급락 경보')
      .replace(/FEED_UNHEALTHY/gi, '📡 시세 수신 지연')
      .replace(/DATA_UNAVAILABLE/gi, '📡 데이터 수신 불가')
      .replace(/RECONCILIATION_PENDING/gi, '🔄 체결 대사 진행 중')
      .replace(/RECONCILIATION_SYNC/gi, '🔄 체결 대사 동기화')
      .replace(/UNKNOWN/gi, '❓ 상태 불명')
      .replace(/\bFAILED\b/gi, '❌ 실패')
      .replace(/\bFAIL\b/gi, '❌ 실패')
      .replace(/\bERROR\b/gi, '⚠️ 오류')
      // 단독 거래 구분 치환 (t.side fallback)
      .replace(/\bBUY\b/gi, '📈 매수')
      .replace(/\bBID\b/gi, '📈 매수')
      .replace(/\bSELL\b/gi, '📉 매도')
      .replace(/\bASK\b/gi, '📉 매도');
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
    if (s === 'FAILED' || s === 'FAIL' || s === 'ERROR') {
      return '<span class="px-2 py-0.5 rounded text-xs font-bold bg-rose-500/20 text-rose-300 border border-rose-500/40 whitespace-nowrap">❌ 주문 실패</span>';
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
    if (s.includes('AI_EMERGENCY') || s.includes('EMERGENCY')) {
      return '<span class="px-2 py-0.5 rounded text-xs font-bold bg-rose-600 text-white whitespace-nowrap">🚨 AI비상탈출</span>';
    }
    if (s.includes('AI_TIGHTENED') || s.includes('TIGHTEN')) {
      return '<span class="px-2 py-0.5 rounded text-xs font-bold bg-amber-500/20 text-amber-300 border border-amber-500/40 whitespace-nowrap">🛡️ AI손절상향</span>';
    }
    if (s.includes('NEW_LISTING_EARLY_EXIT') || s.includes('신규상장 조기')) {
      return '<span class="px-2 py-0.5 rounded text-xs font-bold bg-amber-500/20 text-amber-300 border border-amber-500/40 whitespace-nowrap">신규상장조기탈출</span>';
    }
    if (s.includes('NEW_LISTING_TIME_STOP') || s.includes('신규상장 타임스탑')) {
      return '<span class="px-2 py-0.5 rounded text-xs font-bold bg-amber-500/20 text-amber-300 border border-amber-500/40 whitespace-nowrap">신규상장타임스탑</span>';
    }
    if (s.includes('SWING_TREND_STOP') || s.includes('스윙 추세')) {
      return '<span class="px-2 py-0.5 rounded text-xs font-bold bg-cyan-500/20 text-cyan-300 border border-cyan-500/40 whitespace-nowrap">스윙추세이탈</span>';
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
      // 상태 갱신과 같은 주기로 비정상 로그만 읽어 운영 이상을 빠르게 확인한다.
      fetchAlertLogs();

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

  // WARNING, ERROR, CRITICAL 로그 전용 조회. 로그 원문은 반드시 textContent로 렌더링한다.
  async function fetchAlertLogs() {
    const tbody = document.getElementById('alerts_tbody');
    const countEl = document.getElementById('alerts_count');
    if (!tbody) return;

    try {
      // 활성 거래소 탭에 맞춰 로그 범위를 서버에서 제한한다.
      const exchange = ['combined', 'bithumb', 'upbit'].includes(state.activeExchange)
        ? state.activeExchange
        : 'combined';
      const res = await fetch(`${getApiBaseUrl()}/api/alerts?exchange=${encodeURIComponent(exchange)}`, {
        headers: { 'Accept': 'application/json' }
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      renderAlertLogs(Array.isArray(data.alerts) ? data.alerts : []);
      if (countEl) countEl.textContent = `${Array.isArray(data.alerts) ? data.alerts.length : 0}건`;
    } catch (err) {
      tbody.replaceChildren();
      const row = document.createElement('tr');
      const cell = document.createElement('td');
      cell.colSpan = 4;
      cell.className = 'p-4 text-center text-rose-400';
      cell.textContent = `비정상 로그 조회 실패: ${err.message}`;
      row.appendChild(cell);
      tbody.appendChild(row);
      if (countEl) countEl.textContent = '조회 실패';
    }
  }

  // 서버 로그는 외부 입력으로 취급해 HTML 삽입 없이 셀 단위로 안전하게 표시한다.
  function renderAlertLogs(alerts) {
    const tbody = document.getElementById('alerts_tbody');
    if (!tbody) return;
    tbody.replaceChildren();

    if (alerts.length === 0) {
      const row = document.createElement('tr');
      const cell = document.createElement('td');
      cell.colSpan = 4;
      cell.className = 'p-4 text-center text-emerald-400';
      cell.textContent = '현재 WARNING 이상 비정상 로그가 없습니다.';
      row.appendChild(cell);
      tbody.appendChild(row);
      renderAlertIncidentSummary(alerts);
      return;
    }

    alerts.forEach(alert => {
      const row = document.createElement('tr');
      row.className = 'hover:bg-slate-800/30 border-b border-slate-800/60';
      const level = String(alert.level || '').toUpperCase();
      const levelClass = level === 'WARNING' ? 'text-amber-300' : 'text-rose-400';
      const cells = [
        { value: alert.timestamp || '-', className: 'p-2.5 whitespace-nowrap text-slate-400 font-mono' },
        { value: alert.source || '-', className: 'p-2.5 whitespace-nowrap text-slate-200' },
        { value: level || '-', className: `p-2.5 whitespace-nowrap font-bold ${levelClass}` },
        { value: alert.message || '-', className: 'p-2.5 text-slate-300 break-all whitespace-pre-wrap' }
      ];
      cells.forEach(({ value, className }) => {
        const cell = document.createElement('td');
        cell.className = className;
        cell.textContent = String(value);
        row.appendChild(cell);
      });
      tbody.appendChild(row);
    });
    renderAlertIncidentSummary(alerts);
  }

  // 같은 종류의 경고를 사건 단위로 묶어 운영자가 원문을 모두 읽기 전에 우선순위를 파악하게 한다.
  // 원문 메시지는 이 함수에서도 HTML로 삽입하지 않아 로그 기반 스크립트 실행을 차단한다.
  function renderAlertIncidentSummary(alerts) {
    const container = document.getElementById('alert_incident_summary');
    if (!container) return;
    container.replaceChildren();

    if (!alerts || alerts.length === 0) {
      const empty = document.createElement('span');
      empty.className = 'text-slate-500';
      empty.textContent = '최근 비정상 사건이 없습니다.';
      container.appendChild(empty);
      return;
    }

    const incidents = new Map();
    alerts.forEach(alert => {
      const message = String(alert && alert.message || '')
        .replace(/^\[?\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}\]?\s*/, '')
        .replace(/\d+/g, '#');
      const source = String(alert && alert.source || '알 수 없는 구성요소');
      const level = String(alert && alert.level || 'WARNING').toUpperCase();
      const key = `${source}|${level}|${message}`;
      const saved = incidents.get(key) || { source, level, message, count: 0 };
      saved.count += 1;
      incidents.set(key, saved);
    });

    [...incidents.values()]
      .sort((a, b) => b.count - a.count)
      .slice(0, 5)
      .forEach(incident => {
        const chip = document.createElement('span');
        const severe = incident.level === 'CRITICAL' || incident.level === 'ERROR';
        chip.className = severe
          ? 'px-2 py-1 rounded-lg border border-rose-500/40 bg-rose-500/10 text-rose-200'
          : 'px-2 py-1 rounded-lg border border-amber-500/40 bg-amber-500/10 text-amber-100';
        chip.textContent = `${incident.source} · ${incident.level} ${incident.count}건`;
        chip.title = incident.message || '메시지 없음';
        container.appendChild(chip);
      });
  }

  // 서버가 전달한 안전 상태는 HTML로 삽입하지 않고 DOM 텍스트로만 표시한다.
  function renderSafetyPanel(safety) {
    const data = (safety && typeof safety === 'object') ? safety : {};
    const isReady = data.entry_ready === true;
    const badge = document.getElementById('entry_ready_badge');
    const summary = document.getElementById('safety_summary');
    const reasonsEl = document.getElementById('entry_block_reasons');
    const countsEl = document.getElementById('order_status_counts');

    if (badge) {
      badge.textContent = isReady ? '🟢 신규 매수 가능' : '🛑 신규 매수 차단';
      badge.className = isReady
        ? 'px-3 py-1 rounded-full text-xs font-bold bg-emerald-500/20 text-emerald-300 border border-emerald-500/40'
        : 'px-3 py-1 rounded-full text-xs font-bold bg-rose-500/20 text-rose-300 border border-rose-500/40';
    }
    if (summary) {
      summary.textContent = isReady
        ? '시세·주문 대사·리스크 조건이 확인되어 신규 매수 분석을 진행할 수 있습니다.'
        : '기존 보유 포지션 보호는 계속되며, 신규 매수만 안전하게 차단됩니다.';
    }
    if (reasonsEl) {
      reasonsEl.replaceChildren();
      const reasons = Array.isArray(data.entry_block_reasons) ? data.entry_block_reasons : [];
      (reasons.length ? reasons : ['차단 사유 없음']).forEach(reason => {
        const item = document.createElement('li');
        item.textContent = `• ${String(reason)}`;
        item.className = isReady ? 'text-emerald-300' : 'text-amber-200';
        reasonsEl.appendChild(item);
      });
    }
    if (countsEl) {
      const counts = (data.order_status_counts && typeof data.order_status_counts === 'object') ? data.order_status_counts : {};
      // 저장 상태 코드는 그대로 두고, 사용자 화면에서만 한글 상태명으로 변환한다.
      const orderStatusLabels = {
        PENDING_SUBMISSION: '주문 제출 대기',
        ACKNOWLEDGED: '주문 접수',
        OPEN: '미체결 대기',
        PARTIALLY_FILLED: '부분 체결',
        FILLED: '체결 완료',
        CANCELED: '주문 취소',
        CANCELLED: '주문 취소',
        REJECTED: '주문 거절',
        FAILED: '주문 실패',
        UNKNOWN: '확인 필요',
        RECONCILIATION_PENDING: '체결 대사 진행 중',
        RECONCILED: '체결 대사 완료',
      };
      const text = Object.entries(counts)
        .map(([status, count]) => `${orderStatusLabels[String(status).toUpperCase()] || String(status)} ${count}건`)
        .join(' · ');
      countsEl.textContent = text || '최근 주문 없음';
    }

    const nlSlotEl = document.getElementById('new_listing_slot_status');
    if (nlSlotEl) {
      const used = Number(data.new_listing_slot_used || 0);
      const max = Number(data.new_listing_slot_max || 0);
      const enabled = data.new_listing_enabled === true;
      const enforcement = data.new_listing_enforcement === true;
      const modeLabel = !enabled
        ? '비활성'
        : (enforcement ? '차단 활성' : '관찰 모드(ENFORCEMENT=false)');
      nlSlotEl.textContent = enabled
        ? `신규상장 슬롯 ${used}/${max} · ${modeLabel}`
        : '신규상장 경로 비활성(NEW_LISTING_ENABLED=false)';
    }

    // 통합 탭은 거래소별 안전 원인을 나란히 보여 주되, 개별 탭에서는 현재 거래소 하나만 표시한다.
    renderExchangeSafetyDetails(data);
  }

  function renderExchangeSafetyDetails(safety) {
    const container = document.getElementById('safety_exchange_detail');
    if (!container) return;
    container.replaceChildren();

    const byExchange = safety && safety.by_exchange && typeof safety.by_exchange === 'object'
      ? safety.by_exchange
      : null;
    const details = byExchange || { current: safety || {} };
    const labels = { bithumb: '🟡 빗썸', upbit: '🔵 업비트', current: '현재 거래소' };

    Object.entries(details).forEach(([exchange, item]) => {
      const data = (item && typeof item === 'object') ? item : {};
      const ready = data.entry_ready === true;
      const feed = (data.feed && typeof data.feed === 'object') ? data.feed : {};
      const reasons = Array.isArray(data.entry_block_reasons) ? data.entry_block_reasons : [];
      const card = document.createElement('div');
      card.className = ready
        ? 'rounded-xl border border-emerald-500/30 bg-emerald-500/5 p-3'
        : 'rounded-xl border border-rose-500/30 bg-rose-500/5 p-3';
      const title = document.createElement('div');
      title.className = 'font-semibold text-slate-100';
      title.textContent = `${labels[exchange] || exchange}: ${ready ? '신규 매수 가능' : '신규 매수 차단'}`;
      const body = document.createElement('div');
      body.className = 'text-[11px] mt-1 text-slate-300';
      const feedStatus = String(feed.status || 'DATA_UNAVAILABLE');
      body.textContent = reasons.length
        ? reasons.slice(0, 2).join(' · ')
        : `시세 스트림 ${feedStatus}`;
      card.append(title, body);
      container.appendChild(card);
    });
  }

  // 거래소 Open API 잔여 쿼터 표시 (업비트 Remaining-Req, 빗썸 미제공 시 '-')
  function renderExchangeQuotaEl(el, data, secLimit = 10, minLimit = 600) {
    if (!el) return;
    data = data || {};
    const hasSec = data.remaining_sec !== null && data.remaining_sec !== undefined;
    const hasMin = data.remaining_min !== null && data.remaining_min !== undefined;
    const sec = hasSec ? `${data.remaining_sec}/${secLimit}` : '-';
    const min = hasMin ? `${data.remaining_min}/${minLimit}` : '-';
    el.innerText = `초당: ${sec} / 분당: ${min}`;
    if (hasSec && data.remaining_sec <= 2) {
      el.className = 'font-medium text-amber-400';
    } else if (hasSec) {
      el.className = 'font-medium text-emerald-400';
    } else {
      el.className = 'font-medium text-slate-400';
    }
  }

  // 거래소 Open API GET/POST 누적 (빗썸 등 잔여 쿼터 미제공 시 보조 관측)
  function renderExchangeMethodsEl(el, data) {
    if (!el) return;
    data = data || {};
    const getC = (data.by_method && data.by_method.GET) || 0;
    const postC = (data.by_method && data.by_method.POST) || 0;
    el.innerText = `GET ${getC.toLocaleString()} / POST ${postC.toLocaleString()}`;
  }

  // API 일일 사용량 & 쿼터 패널 렌더링
  function renderApiUsagePanel(apiUsage, activeExchange) {
    if (!apiUsage) return;

    // 날짜 표시
    const dateEl = document.getElementById('api_usage_date');
    const todayStr = (apiUsage.gemini && apiUsage.gemini.date) || 
                     (apiUsage.bithumb && apiUsage.bithumb.date) ||
                     (apiUsage.upbit && apiUsage.upbit.date) ||
                     (apiUsage.exchange && apiUsage.exchange.date) || '오늘';
    if (dateEl) {
      dateEl.innerText = `${todayStr} 기준`;
    }

    const cardBithumb = document.getElementById('api_card_bithumb');
    // 기존 DOM 식별자는 외부 UI 호환을 위해 유지하고, 빗썸 카드만 Gemini 전용 데이터로 표시한다.
    const cardBithumbGemini = document.getElementById('api_card_gemini_bithumb');
    const cardUpbit = document.getElementById('api_card_upbit');
    const cardUpbitGemini = document.getElementById('api_card_gemini_upbit');

    // 탭별 카드 시각적 강조/흐리게 처리
    if (activeExchange === 'upbit') {
      if (cardBithumb) cardBithumb.classList.add('opacity-30');
      if (cardBithumbGemini) cardBithumbGemini.classList.add('opacity-30');
      if (cardUpbit) cardUpbit.classList.remove('opacity-30');
      if (cardUpbitGemini) cardUpbitGemini.classList.remove('opacity-30');
    } else if (activeExchange === 'bithumb') {
      if (cardBithumb) cardBithumb.classList.remove('opacity-30');
      if (cardBithumbGemini) cardBithumbGemini.classList.remove('opacity-30');
      if (cardUpbit) cardUpbit.classList.add('opacity-30');
      if (cardUpbitGemini) cardUpbitGemini.classList.add('opacity-30');
    } else {
      if (cardBithumb) cardBithumb.classList.remove('opacity-30');
      if (cardBithumbGemini) cardBithumbGemini.classList.remove('opacity-30');
      if (cardUpbit) cardUpbit.classList.remove('opacity-30');
      if (cardUpbitGemini) cardUpbitGemini.classList.remove('opacity-30');
    }

    // 1. 빗썸 API
    let btData = apiUsage.bithumb;
    if (!btData && apiUsage.exchange && activeExchange === 'bithumb') {
      btData = apiUsage.exchange;
    }
    btData = btData || {};

    const btCallsEl = document.getElementById('bithumb_api_calls');
    if (btCallsEl) {
      btCallsEl.innerHTML = `${(btData.total_calls || 0).toLocaleString()}<span class="text-xs font-normal text-slate-400 ml-1">회</span>`;
    }
    renderExchangeMethodsEl(document.getElementById('bithumb_api_methods'), btData);
    const btErrorsEl = document.getElementById('bithumb_api_errors');
    if (btErrorsEl) {
      const errC = btData.errors || 0;
      const r429 = btData.rate_limited_429 || 0;
      btErrorsEl.innerText = `${errC} / 429: ${r429}회`;
      btErrorsEl.className = r429 > 0 ? 'font-medium text-rose-400' : 'font-medium text-slate-300';
    }
    const btBadgeEl = document.getElementById('bithumb_api_badge');
    if (btBadgeEl) {
      if ((btData.rate_limited_429 || 0) > 0) {
        btBadgeEl.innerText = '429 발생';
        btBadgeEl.className = 'px-2 py-0.5 rounded text-[10px] font-bold bg-amber-500/20 text-amber-300 border border-amber-500/30';
      } else if ((btData.total_calls || 0) > 0) {
        btBadgeEl.innerText = '정상';
        btBadgeEl.className = 'px-2 py-0.5 rounded text-[10px] font-bold bg-emerald-500/20 text-emerald-300 border border-emerald-500/30';
      } else {
        btBadgeEl.innerText = '대기';
        btBadgeEl.className = 'px-2 py-0.5 rounded text-[10px] font-bold bg-slate-700 text-slate-400 border border-slate-600';
      }
    }
    const btLastEl = document.getElementById('bithumb_api_last');
    if (btLastEl) {
      btLastEl.innerText = btData.last_endpoint ? `${btData.last_endpoint} (${btData.last_status || '-'})` : '-';
    }

    // 2. 업비트 API
    let upData = apiUsage.upbit;
    if (!upData && apiUsage.exchange && activeExchange === 'upbit') {
      upData = apiUsage.exchange;
    }
    upData = upData || {};

    const upCallsEl = document.getElementById('upbit_api_calls');
    if (upCallsEl) {
      upCallsEl.innerHTML = `${(upData.total_calls || 0).toLocaleString()}<span class="text-xs font-normal text-slate-400 ml-1">회</span>`;
    }
    renderExchangeQuotaEl(document.getElementById('upbit_api_quota'), upData);
    const upErrorsEl = document.getElementById('upbit_api_errors');
    if (upErrorsEl) {
      const errC = upData.errors || 0;
      const r429 = upData.rate_limited_429 || 0;
      upErrorsEl.innerText = `${errC} / 429: ${r429}회`;
      upErrorsEl.className = r429 > 0 ? 'font-medium text-rose-400' : 'font-medium text-slate-300';
    }
    const upBadgeEl = document.getElementById('upbit_api_badge');
    if (upBadgeEl) {
      if ((upData.rate_limited_429 || 0) > 0) {
        upBadgeEl.innerText = '429 발생';
        upBadgeEl.className = 'px-2 py-0.5 rounded text-[10px] font-bold bg-amber-500/20 text-amber-300 border border-amber-500/30';
      } else if ((upData.total_calls || 0) > 0) {
        upBadgeEl.innerText = '정상';
        upBadgeEl.className = 'px-2 py-0.5 rounded text-[10px] font-bold bg-emerald-500/20 text-emerald-300 border border-emerald-500/30';
      } else {
        upBadgeEl.innerText = '대기';
        upBadgeEl.className = 'px-2 py-0.5 rounded text-[10px] font-bold bg-slate-700 text-slate-400 border border-slate-600';
      }
    }
    const upLastEl = document.getElementById('upbit_api_last');
    if (upLastEl) {
      upLastEl.innerText = upData.last_endpoint ? `${upData.last_endpoint} (${upData.last_status || '-'})` : '-';
    }

    // 3. 빗썸·업비트 Gemini 카드는 같은 UI 계약으로 렌더링하되 원본 계측 데이터는 분리한다.
    let btGeminiData = apiUsage.gemini_bithumb || apiUsage.ai_provider_bithumb;
    if (!btGeminiData && activeExchange === 'bithumb') {
      btGeminiData = apiUsage.gemini;
    }
    let upGeminiData = apiUsage.gemini_upbit;
    if (!upGeminiData && activeExchange === 'upbit') {
      upGeminiData = apiUsage.gemini;
    }

    function renderAiProviderCard(prefix, gData, gradientClass) {
      gData = gData || {};
      const calls = gData.api_calls || 0;
      const limit = gData.quota_limit || 1000;
      const pct = gData.quota_used_pct !== undefined ? gData.quota_used_pct : (limit > 0 ? Math.round((calls / limit) * 1000) / 10 : 0);

      const ratioEl = document.getElementById(`${prefix}_api_calls_ratio`);
      if (ratioEl) ratioEl.innerText = `${calls.toLocaleString()} / ${limit.toLocaleString()}회`;

      const barEl = document.getElementById(`${prefix}_quota_bar`);
      if (barEl) {
        const clampPct = Math.min(100, Math.max(0, pct));
        barEl.style.width = `${clampPct}%`;
        if (clampPct >= 90) {
          barEl.className = 'bg-rose-500 h-2 rounded-full transition-all duration-500';
        } else if (clampPct >= 70) {
          barEl.className = 'bg-amber-500 h-2 rounded-full transition-all duration-500';
        } else {
          barEl.className = `${gradientClass} h-2 rounded-full transition-all duration-500`;
        }
      }

      const badgeEl = document.getElementById(`${prefix}_api_badge`);
      if (badgeEl) {
        // 두 거래소 모두 Gemini 장애 시 쿼터보다 신규 BUY 차단 상태를 우선 표시한다.
        const entrySafety = gData.entry_safety || {};
        if (entrySafety.entry_blocked) {
          badgeEl.innerText = '신규 BUY 차단';
          badgeEl.className = 'px-2 py-0.5 rounded text-[10px] font-bold bg-rose-500/20 text-rose-300 border border-rose-500/30';

        } else if (pct >= 90) {
          badgeEl.innerText = `${pct}% 소진`;
          badgeEl.className = 'px-2 py-0.5 rounded text-[10px] font-bold bg-rose-500/20 text-rose-300 border border-rose-500/30';
        } else if (pct >= 70) {
          badgeEl.innerText = `${pct}% 소진`;
          badgeEl.className = 'px-2 py-0.5 rounded text-[10px] font-bold bg-amber-500/20 text-amber-300 border border-amber-500/30';
        } else {
          badgeEl.innerText = `${pct}% 소진`;
          badgeEl.className = 'px-2 py-0.5 rounded text-[10px] font-bold bg-purple-500/20 text-purple-300 border border-purple-500/30';
        }
      }

      const cacheEl = document.getElementById(`${prefix}_api_cache`);
      if (cacheEl) cacheEl.innerText = `${(gData.cache_hits || 0).toLocaleString()}회`;

      const fallbackEl = document.getElementById(`${prefix}_api_fallback`);
      if (fallbackEl) {
        const fbCount = gData.local_fallback || 0;
        const r429 = gData.rate_limited || 0;
        fallbackEl.innerText = `${fbCount.toLocaleString()} / 429: ${r429.toLocaleString()}회`;
        fallbackEl.className = r429 > 0 ? 'font-medium text-rose-400' : 'font-medium text-slate-300';
      }

      const lastEl = document.getElementById(`${prefix}_api_last`);
      if (lastEl) {
        const entrySafety = gData.entry_safety || {};
        // 오류 메시지 원문 대신 서버가 제한한 목적·상태·코드만 표시한다.
        if (entrySafety.entry_blocked) {
          const details = [entrySafety.context, entrySafety.http_status ? `HTTP ${entrySafety.http_status}` : '', entrySafety.error_code]
            .filter(Boolean).join(' · ');
          lastEl.innerText = details ? `Gemini 장애: ${details}` : 'Gemini 장애: 신규 BUY 차단';
          lastEl.className = 'font-medium truncate max-w-[140px] text-rose-400';
        } else {
          lastEl.innerText = gData.last_event || '-';
          lastEl.className = 'font-medium truncate max-w-[140px] text-slate-400';
        }
      }

      // 실제 모델 ID와 ListModels 호출을 함께 표시해 하위 행의 합계가 총 호출 수와 같도록 한다.
      const modelListEl = document.getElementById(`${prefix}_models_list`);
      if (modelListEl) {
        const sourceModels = gData.models_by_id || gData.models || {};
        const rows = Object.entries(sourceModels)
          .filter(([model, stat]) => Number(stat?.calls || 0) > 0 && !model.includes('latest'))
          .sort(([, left], [, right]) => Number(right.calls || 0) - Number(left.calls || 0));
        const countedCalls = rows.reduce((sum, [, stat]) => sum + Number(stat.calls || 0), 0);
        const unclassifiedCalls = Math.max(0, calls - countedCalls);

        // 구버전 영속 파일처럼 모델별 상세가 일부 없더라도 총계와 화면 합계가 달라지지 않게 보정한다.
        if (unclassifiedCalls > 0) rows.push(['unclassified', { calls: unclassifiedCalls }]);
        if (!rows.length) rows.push(['empty', { calls: 0 }]);

        modelListEl.replaceChildren();
        rows.forEach(([model, stat]) => {
          const row = document.createElement('div');
          const label = document.createElement('div');
          const name = document.createElement('span');
          const ratio = document.createElement('span');
          const barWrap = document.createElement('div');
          const bar = document.createElement('div');
          const modelCalls = Number(stat.calls || 0);
          let quotaLimit = Number(stat.quota_limit || 0);
          if (!quotaLimit && model !== 'list_models' && (model.includes('flash-lite') || model.includes('flash_lite') || model.includes('gemini-'))) {
            quotaLimit = 500;
          }
          const quotaPct = quotaLimit > 0 ? Math.min(100, Math.max(0, (modelCalls / quotaLimit) * 100)) : 0;

          row.className = 'space-y-1';
          label.className = 'flex justify-between gap-2 text-slate-400';
          name.className = 'min-w-0 truncate font-medium text-amber-300/90 flex items-center gap-1';
          ratio.className = 'shrink-0 font-medium text-slate-300';
          const iconSpan = document.createElement('span');
          iconSpan.textContent = model === 'list_models' ? '📋' : '⚡';
          const textSpan = document.createElement('span');
          textSpan.textContent = model === 'list_models' ? '모델 목록 조회' : (model === 'unclassified' ? '기타 API 요청' : (model === 'empty' ? '모델 호출 기록 없음' : model));
          name.append(iconSpan, textSpan);
          ratio.textContent = quotaLimit > 0
            ? `${modelCalls.toLocaleString()} / ${quotaLimit.toLocaleString()}회 (${quotaPct.toFixed(1)}%)`
            : `${modelCalls.toLocaleString()}회`;
          barWrap.className = 'w-full bg-slate-800 h-1 rounded-full overflow-hidden';
          bar.className = quotaPct >= 90 ? 'bg-rose-500 h-1 rounded-full transition-all duration-500' : 'bg-amber-400 h-1 rounded-full transition-all duration-500';
          bar.style.width = `${quotaPct}%`;
          label.append(name, ratio);
          barWrap.appendChild(bar);
          row.append(label, barWrap);
          modelListEl.appendChild(row);
        });
      }

      // 리셋 정보 렌더링
      const resetInfo = gData.reset_info || {};
      const resetEl = document.getElementById(`${prefix}_api_reset`);
      if (resetEl) {
        // Google Gemini의 일일 쿼터 기준은 두 거래소에 동일하게 적용한다.
        let timeKst = resetInfo.reset_time_kst || '16:00 KST';
        const timeMatch = timeKst.match(/(\d{2}:\d{2})(?::\d{2})?\s*KST/i);
        if (timeMatch) {
          timeKst = `${timeMatch[1]} KST`;
        }
        let remStr = resetInfo.remaining_str ? ` (${resetInfo.remaining_str})` : '';
        if (!remStr && resetInfo.remaining_seconds) {
          const remH = Math.floor(resetInfo.remaining_seconds / 3600);
          const remM = Math.floor((resetInfo.remaining_seconds % 3600) / 60);
          remStr = ` (${remH}시간 ${remM.toString().padStart(2, '0')}분 후 리셋)`;
        }
        resetEl.innerText = `${timeKst}${remStr}`;
      }
    }

    renderAiProviderCard('gemini_bithumb', btGeminiData, 'bg-gradient-to-r from-blue-500 to-indigo-500');
    renderAiProviderCard('gemini_upbit', upGeminiData, 'bg-gradient-to-r from-blue-500 to-indigo-500');

    // 하위 호환: 기존 단일 gemini 카드 요소가 남아있을 경우 합산값 렌더링
    const combinedGemini = apiUsage.gemini;
    if (combinedGemini && document.getElementById('gemini_api_calls_ratio')) {
      renderAiProviderCard('gemini', combinedGemini, 'bg-gradient-to-r from-purple-500 to-indigo-500');
    }
  }

  // 표시 문구가 코드의 StrategyPolicy와 달라지지 않도록 API 값을 사용한다.
  function renderPolicyGuide(policy) {
    if (!policy || typeof policy !== 'object') return;
    const pct = value => `${(Number(value || 0) * 100).toFixed(1)}%`;
    const setText = (id, value) => {
      const el = document.getElementById(id);
      if (el) el.textContent = value;
    };
    setText('policy_partial_tp_1', `• 1차 +${pct(policy.partial_tp_1_pct)} 도달 시 ${pct(policy.partial_tp_1_ratio)} 익절`);
    setText('policy_partial_tp_2', `• 2차 +${pct(policy.partial_tp_2_pct)} 도달 시 ${pct(policy.partial_tp_2_ratio)} 추가 익절`);
    setText('policy_trailing_start', `• +${pct(policy.trailing_start_pct)} 수익 시 트레일링 감시 가동`);
    setText('policy_trailing_drop', `• 최고점 대비 ${pct(policy.trailing_drop_pct)} 하락 시 잔여분 청산`);
    setText('policy_alpha_threshold', `• ${policy.alpha_buy_threshold_normal ?? '-'}점(정상장) / ${policy.alpha_buy_threshold_risk_off ?? '-'}점(약세장) 미만 차단`);
    const normalMinutes = Math.round(Number(policy.time_stop_seconds_normal || 0) / 60);
    const riskOffMinutes = Math.round(Number(policy.time_stop_seconds_risk_off || 0) / 60);
    setText('policy_time_stop', `• 정상장 ${normalMinutes}분 / 약세장 ${riskOffMinutes}분 타임스탑 기준`);
    const nlPct = value => `${(Number(value || 0) * 100).toFixed(1)}%`;
    setText(
      'policy_new_listing_alloc',
      `• 진입 비중 ${nlPct(policy.new_listing_alloc_ratio)} (단타 슬롯 내 소액)`,
    );
    setText(
      'policy_new_listing_stop',
      `• 손절 ${nlPct(policy.new_listing_stop_loss_pct)} / 하드스탑 ${nlPct(policy.new_listing_hard_stop_pct)}`,
    );
    const nlTimeMin = Math.round(Number(policy.new_listing_time_stop_seconds || 0) / 60);
    const nlEarlyMin = Math.round(Number(policy.new_listing_early_exit_seconds || 0) / 60);
    setText(
      'policy_new_listing_time',
      `• ${nlEarlyMin}분 조기탈출 / ${nlTimeMin}분 타임스탑 · 재진입 ${Math.round(Number(policy.new_listing_reentry_cooldown_sec || 0) / 60)}분`,
    );
    setText(
      'policy_new_listing_alpha',
      `• 알파 ${policy.new_listing_alpha_threshold_normal ?? '-'}점(주간) / ${policy.new_listing_alpha_threshold_night ?? '-'}점(야간)`,
    );
  }

  function formatHoldSeconds(seconds) {
    const value = Number(seconds);
    if (!Number.isFinite(value) || value < 0) return '확정 체결 시각 없음';
    const hours = Math.floor(value / 3600);
    const minutes = Math.floor((value % 3600) / 60);
    return hours > 0 ? `${hours}시간 ${minutes}분` : `${minutes}분`;
  }

  function renderPositionRiskState(riskState) {
    const state = (riskState && typeof riskState === 'object') ? riskState : {};
    const stage = Number(state.partial_tp_stage || 0);
    const peak = Number(state.peak_price || 0);
    const parts = [];
    if (state.strategy_mode) {
      parts.push(formatStrategyModeLabel(state.strategy_mode));
    }
    parts.push(`보유: ${formatHoldSeconds(state.hold_seconds)}`, `분할익절: ${stage}단계`);
    if (peak > 0) parts.push(`고점: ${formatPrice(peak)}원`);
    if (state.exit_in_progress === true) parts.push('청산 주문 진행 중');
    return `<div class="mt-1 text-[11px] text-amber-200">${parts.join(' · ')}</div>`;
  }

  // 이 우선순위는 화면 정렬 전용이다. 주문·손절 조건이나 포지션 상태에는 어떠한 변경도 가하지 않는다.
  function getPositionOperationalPriority(position) {
    const pos = (position && typeof position === 'object') ? position : {};
    const riskState = (pos.risk_state && typeof pos.risk_state === 'object') ? pos.risk_state : {};
    const action = String(pos.action || '').toUpperCase();
    const current = Number(pos.current_price || 0);
    const stopLoss = Number(pos.stop_loss || 0);

    if (riskState.exit_in_progress === true || action.includes('EMERGENCY') || action.includes('EXIT')) {
      return { rank: 0, label: '즉시 확인', detail: '청산 대응 또는 주문 진행 상태', tone: 'rose' };
    }
    if (current > 0 && stopLoss > 0 && current <= stopLoss) {
      return { rank: 0, label: '즉시 확인', detail: '현재가가 표시 손절가 이하', tone: 'rose' };
    }
    // 손절가 1% 이내는 주문 신호가 아니라 운용자가 확인할 표시상 관찰 구간이다.
    if (current > 0 && stopLoss > 0 && ((current - stopLoss) / current * 100) <= 1.0) {
      return { rank: 1, label: '관찰', detail: '표시 손절가 1% 이내', tone: 'amber' };
    }
    return { rank: 2, label: '정상', detail: '즉시 확인 조건 없음', tone: 'emerald' };
  }

  function renderPositionOperationalPriority(priority) {
    const data = priority || { label: '정상', detail: '즉시 확인 조건 없음', tone: 'emerald' };
    const color = data.tone === 'rose'
      ? 'bg-rose-500/20 text-rose-200 border-rose-500/40'
      : (data.tone === 'amber'
        ? 'bg-amber-500/20 text-amber-200 border-amber-500/40'
        : 'bg-emerald-500/20 text-emerald-200 border-emerald-500/40');
    return `<div><span class="px-2 py-0.5 rounded text-xs font-bold border ${color}">${data.label}</span><div class="mt-1 text-[10px] text-slate-400">${data.detail}</div></div>`;
  }

  // 진입 가능성 칸은 상태를 빠르게 판단하는 용도이므로 긴 전략 근거를 반복하지 않는다.
  // 상세 근거는 표의 마지막 "AI / 퀀트 진입 분석 근거" 열에 그대로 남긴다.
  function summarizeCandidateReason(reason) {
    const text = String(reason || '').trim();
    if (!text) return '개별 전략 기준 미통과';
    const headline = text.split(':', 1)[0].trim() || text;
    const scoreMatch = text.match(/알파\s*스코어\s*([0-9]+(?:\.[0-9]+)?)점/i);
    return scoreMatch ? `${headline} · 알파 ${scoreMatch[1]}점` : headline;
  }

  // 후보의 최종 주문 가능 여부는 엔진이 다시 판정한다. 이 값은 화면 조회 시점의 설명일 뿐이다.
  function getCandidateEntryAvailability(candidate, safety) {
    const cand = (candidate && typeof candidate === 'object') ? candidate : {};
    const safe = (safety && typeof safety === 'object') ? safety : {};
    if (safe.entry_ready !== true) {
      const reasons = Array.isArray(safe.entry_block_reasons) ? safe.entry_block_reasons : [];
      return { label: '전역 차단', detail: reasons[0] || '안전 상태 확인 대기', tone: 'rose' };
    }
    if (cand.allow_buy !== true) {
      return { label: '전략 관망', detail: summarizeCandidateReason(cand.reason), tone: 'slate' };
    }
    return { label: '진입 검토 가능', detail: '주문 직전 엔진의 최종 안전 검증 필요', tone: 'emerald' };
  }

  function renderCandidateEntryAvailability(availability) {
    const data = availability || { label: '상태 확인 중', detail: '', tone: 'slate' };
    const color = data.tone === 'rose'
      ? 'bg-rose-500/20 text-rose-200 border-rose-500/40'
      : (data.tone === 'emerald'
        ? 'bg-emerald-500/20 text-emerald-200 border-emerald-500/40'
        : 'bg-slate-700/60 text-slate-200 border-slate-600');
    return `<span class="px-2 py-0.5 rounded text-xs font-bold border ${color}">${data.label}</span>`;
  }

  // 주문 상태만으로 관측 가능한 흐름을 표시한다. ACK를 체결 확정처럼 보이지 않게 분리한다.
  function getOrderLifecycle(order) {
    const status = String(order && order.status || '').toUpperCase();
    if (status === 'FILLED' || status === 'DONE') {
      return { label: '요청 → 접수 → 대사 → 체결 확정', detail: '체결 확정 상태', tone: 'emerald' };
    }
    if (status === 'RECONCILIATION_PENDING' || status === 'RECONCILED') {
      return { label: '요청 → 접수 → REST 대사', detail: status === 'RECONCILED' ? '대사 완료, 체결 상태 확인' : '체결 대사 진행 중', tone: 'amber' };
    }
    if (status === 'ACK' || status === 'ACKNOWLEDGED' || status === 'OPEN' || status === 'WAIT' || status === 'PENDING' || status === 'SUBMITTED') {
      return { label: '요청 → 접수(체결 아님)', detail: 'Private WS 또는 REST 확인 대기', tone: 'blue' };
    }
    if (status === 'UNKNOWN') {
      return { label: '상태 확인 필요', detail: 'REST 대사 전에는 체결로 처리하지 않음', tone: 'rose' };
    }
    return { label: '주문 상태 확인 중', detail: '확정 체결 여부 미판정', tone: 'slate' };
  }

  function renderOrderProgress(order) {
    const executed = Number(order.executed_volume || 0);
    const requested = Number(order.volume || 0);
    const remaining = Number(order.remaining_volume || 0);
    const quantity = requested > 0 ? `${executed.toFixed(6)} / ${requested.toFixed(6)}` : `${executed.toFixed(6)} 체결`;
    const remainingText = remaining > 0 ? `잔여 ${remaining.toFixed(6)}` : '잔여 없음';
    const lifecycle = getOrderLifecycle(order);
    return `<div class="font-mono text-slate-200">${quantity}</div><div class="text-[11px] text-slate-400">${remainingText}</div><div class="text-[10px] mt-1 ${lifecycle.tone === 'rose' ? 'text-rose-300' : (lifecycle.tone === 'amber' ? 'text-amber-300' : 'text-slate-400')}">${lifecycle.label}</div>`;
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

    const safetyWithSlots = Object.assign({}, d.safety || {}, {
      new_listing_slot_used: d.new_listing_slot_used,
      new_listing_slot_max: d.new_listing_slot_max,
      new_listing_enabled: d.new_listing_enabled,
      new_listing_enforcement: d.new_listing_enforcement,
      new_listing_markets: d.new_listing_markets,
    });
    renderSafetyPanel(safetyWithSlots);
    renderPolicyGuide(d.policy);
    renderApiUsagePanel(d.api_usage, state.activeExchange);

    // Tab Counts
    const positions = d.positions || [];
    const candidates = d.candidates || [];
    const countPosEl = document.getElementById('count_positions');
    const countCandEl = document.getElementById('count_candidates');
    if (countPosEl) countPosEl.innerText = positions.length;
    if (countCandEl) countCandEl.innerText = candidates.length;

    // Render Tables
    renderPositionsTable(positions);
    renderCandidatesTable(candidates, d.safety);
    renderDailyHistoryTable(d.daily_stats_history || []);
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
          <td colspan="9" class="p-8 text-center text-slate-500">
            <div class="text-3xl mb-2">💼</div>
            <div class="text-sm font-medium">현재 보유 중인 포지션이 없습니다. (100% 현금 대기 중)</div>
          </td>
        </tr>
      `;
      return;
    }

    const prioritizedPositions = positions
      .map(pos => ({ pos, priority: getPositionOperationalPriority(pos) }))
      .sort((left, right) => left.priority.rank - right.priority.rank);
    tbody.innerHTML = prioritizedPositions.map(({ pos, priority }) => {
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
              ${pos.strategy_mode ? `<span class="text-xs text-amber-300">${formatStrategyModeLabel(pos.strategy_mode)}</span>` : '<span class="text-xs text-blue-400">📈</span>'}
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
            ${renderPositionOperationalPriority(priority)}
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
            ${renderPositionRiskState(pos.risk_state)}
          </td>
        </tr>
      `;
    }).join('');
  }

  // Render Candidates Watchlist Table
  function renderCandidatesTable(candidates, safety) {
    const tbody = document.getElementById('candidates_tbody');
    if (!tbody) return;

    if (!candidates || candidates.length === 0) {
      tbody.innerHTML = `
        <tr>
          <td colspan="8" class="p-8 text-center text-slate-500">
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
        : cand.candidate_type === 'NEW_LISTING'
        ? '<span class="text-xs text-amber-400">🆕 신규상장 단타</span>'
        : cand.candidate_type === 'MOMENTUM_BREAKOUT'
        ? '<span class="text-xs text-purple-400">💥 모멘텀 돌파</span>'
        : cand.candidate_type === 'SWING'
        ? '<span class="text-xs text-cyan-400">🌊 스윙 추세</span>'
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
      const availability = getCandidateEntryAvailability(cand, safety);

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
          <td class="p-3 whitespace-nowrap">
            ${renderCandidateEntryAvailability(availability)}
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

  // Render Daily Asset & Performance History Table
  function renderDailyHistoryTable(history) {
    const tbody = document.getElementById('daily_history_tbody');
    if (!tbody) return;

    if (!history || history.length === 0) {
      tbody.innerHTML = `
        <tr>
          <td colspan="7" class="p-6 text-center text-slate-500">
            <div class="text-2xl mb-1">📅</div>
            <div class="text-xs font-medium">기록된 일일 자산 변동 데이터가 없습니다.</div>
          </td>
        </tr>
      `;
      return;
    }

    tbody.innerHTML = history.map(item => {
      const pnlKrw = Number(item.realized_pnl_krw || 0);
      const pnlPct = Number(item.pnl_pct || 0);
      const isProfit = pnlKrw >= 0;
      const pnlCls = isProfit ? (pnlKrw > 0 ? 'text-emerald-400 font-bold' : 'text-slate-300') : 'text-rose-400 font-bold';
      const pnlSign = pnlKrw > 0 ? '+' : '';
      const pnlPctCls = isProfit ? (pnlPct > 0 ? 'text-emerald-400' : 'text-slate-400') : 'text-rose-400';
      
      const totalTrades = Number(item.total_trades || 0);
      const winTrades = Number(item.win_trades || 0);
      const winRate = Number(item.win_rate || (totalTrades > 0 ? (winTrades / totalTrades * 100) : 0));
      
      const isKillSwitch = Boolean(item.kill_switch_active);
      const riskStatusBadge = isKillSwitch
        ? '<span class="px-2 py-0.5 rounded text-[11px] font-bold bg-rose-500/20 text-rose-300 border border-rose-500/40">🛑 킬스위치</span>'
        : (totalTrades > 0
          ? '<span class="px-2 py-0.5 rounded text-[11px] font-medium bg-emerald-500/20 text-emerald-300 border border-emerald-500/30">🟢 정상 운용</span>'
          : '<span class="px-2 py-0.5 rounded text-[11px] font-medium bg-slate-800 text-slate-400 border border-slate-700">⚪ 대기</span>');

      return `
        <tr class="hover:bg-slate-800/40 transition-colors border-b border-slate-800/80">
          <td class="p-3 whitespace-nowrap font-mono text-xs text-slate-300 font-semibold">
            ${item.date || '-'}
          </td>
          <td class="p-3 whitespace-nowrap font-medium text-slate-200 text-xs">
            ${formatKrw(item.start_equity || 0)}
          </td>
          <td class="p-3 whitespace-nowrap text-xs ${pnlCls}">
            ${pnlSign}${formatKrw(pnlKrw)}
          </td>
          <td class="p-3 whitespace-nowrap text-xs font-mono font-bold ${pnlPctCls}">
            ${formatPct(pnlPct)}
          </td>
          <td class="p-3 whitespace-nowrap text-xs text-slate-300">
            <span class="font-bold ${winTrades > 0 ? 'text-emerald-400' : 'text-slate-300'}">${winTrades}</span> / <span class="text-slate-400">${totalTrades} 회</span>
          </td>
          <td class="p-3 whitespace-nowrap text-xs font-mono">
            ${totalTrades > 0 ? `<span class="font-bold ${winRate >= 60 ? 'text-emerald-400' : (winRate >= 50 ? 'text-blue-400' : 'text-amber-400')}">${winRate.toFixed(1)}%</span>` : '<span class="text-slate-500">-</span>'}
          </td>
          <td class="p-3 whitespace-nowrap text-xs">
            ${riskStatusBadge}
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
      tbody.innerHTML = `<tr><td colspan="7" class="p-4 text-center text-slate-500">주문 저널 기록이 없습니다.</td></tr>`;
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
          <td class="p-2.5 whitespace-nowrap text-xs">
            ${renderOrderProgress(o)}
          </td>
          <td class="p-2.5 whitespace-nowrap text-xs text-slate-300 font-mono">
            ${formatPrice(o.avg_price || o.price)} 원
          </td>
          <td class="p-2.5 whitespace-nowrap text-[11px] text-slate-400 font-mono">
            <div>수수료 ${formatKrw(o.fee || 0)}</div>
            <div>슬리피지 ${Number(o.slippage_bps || 0).toFixed(1)} bps</div>
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
      // 토큰은 브라우저 세션에만 보관하며 서버가 인증을 요구할 때만 입력받는다.
      const actionToken = window.sessionStorage.getItem('dashboardActionToken') || '';
      const headers = actionToken ? { 'X-Dashboard-Action-Token': actionToken } : {};
      const res = await fetch(`${baseUrl}/api/action/${actionName}?exchange=${targetEx}`, {
        method: 'POST',
        headers
      });
      if (res.status === 401) {
        const suppliedToken = window.prompt('원격 제어 토큰을 입력하세요. 토큰은 이 브라우저 세션에만 저장됩니다.');
        if (!suppliedToken) throw new Error('원격 제어 인증이 필요합니다.');
        window.sessionStorage.setItem('dashboardActionToken', suppliedToken);
        alert('인증 토큰이 저장되었습니다. 안전 확인을 위해 명령을 다시 실행하세요.');
        return;
      }
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
