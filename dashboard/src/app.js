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
  }

  // 서버가 전달한 안전 상태는 HTML로 삽입하지 않고 DOM 텍스트로만 표시한다.
  function renderSafetyPanel(safety) {
    const data = (safety && typeof safety === 'object') ? safety : {};
    const isReady = data.entry_ready === true;
    const badge = document.getElementById('entry_ready_badge');
    const summary = document.getElementById('safety_summary');
    const reasonsEl = document.getElementById('entry_block_reasons');
    const feedEl = document.getElementById('feed_health');
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
    if (feedEl) {
      const feed = (data.feed && typeof data.feed === 'object') ? data.feed : {};
      const feeds = (feed.by_exchange && typeof feed.by_exchange === 'object') ? feed.by_exchange : null;
      const feedStatusMap = {
        CONNECTED: '정상 연결',
        CONNECTING: '연결 중',
        RECONNECTING: '재연결 중',
        DISCONNECTED: '연결 끊김',
        DATA_UNAVAILABLE: '데이터 대기',
        HEALTHY: '정상',
        UNHEALTHY: '지연 감지',
      };
      const formatFeed = (label, item) => {
        const isHealthy = item && item.is_healthy === true;
        const latency = Number(item && item.latency_seconds);
        const latencyText = Number.isFinite(latency) && latency < 9999 ? `, 마지막 틱 ${latency.toFixed(1)}초 전` : '';
        const rawStatus = (item && item.status) || 'DATA_UNAVAILABLE';
        const statusKr = feedStatusMap[rawStatus] || rawStatus;
        return `${label}: ${isHealthy ? '정상' : '비정상'} (${statusKr}${latencyText})`;
      };
      if (feeds) {
        feedEl.textContent = [
          formatFeed('빗썸', feeds.bithumb),
          formatFeed('업비트', feeds.upbit)
        ].join(' · ');
      } else {
        feedEl.textContent = formatFeed('현재 거래소', feed);
      }
      feedEl.className = feed.is_healthy === true ? 'text-emerald-300' : 'text-rose-300';
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
    const cardUpbit = document.getElementById('api_card_upbit');

    // 탭별 카드 시각적 강조/흐리게 처리
    if (cardBithumb) {
      if (activeExchange === 'upbit') {
        cardBithumb.classList.add('opacity-30');
      } else {
        cardBithumb.classList.remove('opacity-30');
      }
    }
    if (cardUpbit) {
      if (activeExchange === 'bithumb') {
        cardUpbit.classList.add('opacity-30');
      } else {
        cardUpbit.classList.remove('opacity-30');
      }
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
    const btMethodsEl = document.getElementById('bithumb_api_methods');
    if (btMethodsEl) {
      const getC = (btData.by_method && btData.by_method.GET) || 0;
      const postC = (btData.by_method && btData.by_method.POST) || 0;
      btMethodsEl.innerText = `GET ${getC.toLocaleString()} / POST ${postC.toLocaleString()}`;
    }
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
    const upQuotaEl = document.getElementById('upbit_api_quota');
    if (upQuotaEl) {
      const sec = upData.remaining_sec !== null && upData.remaining_sec !== undefined ? `${upData.remaining_sec}/10` : '-';
      const min = upData.remaining_min !== null && upData.remaining_min !== undefined ? `${upData.remaining_min}/600` : '-';
      upQuotaEl.innerText = `초당: ${sec} / 분당: ${min}`;
      if (upData.remaining_sec !== null && upData.remaining_sec <= 2) {
        upQuotaEl.className = 'font-medium text-amber-400';
      } else {
        upQuotaEl.className = 'font-medium text-emerald-400';
      }
    }
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

    // 3. Gemini AI API
    const geminiData = apiUsage.gemini || {};
    const geminiCalls = geminiData.api_calls || 0;
    const geminiLimit = geminiData.quota_limit || 1500;
    const geminiPct = geminiData.quota_used_pct !== undefined ? geminiData.quota_used_pct : (geminiLimit > 0 ? Math.round((geminiCalls / geminiLimit) * 1000) / 10 : 0);

    const geminiCallsRatioEl = document.getElementById('gemini_api_calls_ratio');
    if (geminiCallsRatioEl) {
      geminiCallsRatioEl.innerText = `${geminiCalls.toLocaleString()} / ${geminiLimit.toLocaleString()}회`;
    }
    const geminiBarEl = document.getElementById('gemini_quota_bar');
    if (geminiBarEl) {
      const clampPct = Math.min(100, Math.max(0, geminiPct));
      geminiBarEl.style.width = `${clampPct}%`;
      if (clampPct >= 90) {
        geminiBarEl.className = 'bg-rose-500 h-2 rounded-full transition-all duration-500';
      } else if (clampPct >= 70) {
        geminiBarEl.className = 'bg-amber-500 h-2 rounded-full transition-all duration-500';
      } else {
        geminiBarEl.className = 'bg-gradient-to-r from-purple-500 to-indigo-500 h-2 rounded-full transition-all duration-500';
      }
    }
    const geminiBadgeEl = document.getElementById('gemini_api_badge');
    if (geminiBadgeEl) {
      geminiBadgeEl.innerText = `${geminiPct}% 소진`;
      if (geminiPct >= 90) {
        geminiBadgeEl.className = 'px-2 py-0.5 rounded text-[10px] font-bold bg-rose-500/20 text-rose-300 border border-rose-500/30';
      } else if (geminiPct >= 70) {
        geminiBadgeEl.className = 'px-2 py-0.5 rounded text-[10px] font-bold bg-amber-500/20 text-amber-300 border border-amber-500/30';
      } else {
        geminiBadgeEl.className = 'px-2 py-0.5 rounded text-[10px] font-bold bg-purple-500/20 text-purple-300 border border-purple-500/30';
      }
    }
    const geminiCacheEl = document.getElementById('gemini_api_cache');
    if (geminiCacheEl) {
      geminiCacheEl.innerText = `${(geminiData.cache_hits || 0).toLocaleString()}회`;
    }
    const geminiFallbackEl = document.getElementById('gemini_api_fallback');
    if (geminiFallbackEl) {
      geminiFallbackEl.innerText = `${(geminiData.local_fallback || 0).toLocaleString()}회`;
    }
    const geminiLastEl = document.getElementById('gemini_api_last');
    if (geminiLastEl) {
      geminiLastEl.innerText = geminiData.last_event || '-';
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
    const parts = [`보유: ${formatHoldSeconds(state.hold_seconds)}`, `분할익절: ${stage}단계`];
    if (peak > 0) parts.push(`고점: ${formatPrice(peak)}원`);
    if (state.exit_in_progress === true) parts.push('청산 주문 진행 중');
    return `<div class="mt-1 text-[11px] text-amber-200">${parts.join(' · ')}</div>`;
  }

  function renderOrderProgress(order) {
    const executed = Number(order.executed_volume || 0);
    const requested = Number(order.volume || 0);
    const remaining = Number(order.remaining_volume || 0);
    const quantity = requested > 0 ? `${executed.toFixed(6)} / ${requested.toFixed(6)}` : `${executed.toFixed(6)} 체결`;
    const remainingText = remaining > 0 ? `잔여 ${remaining.toFixed(6)}` : '잔여 없음';
    return `<div class="font-mono text-slate-200">${quantity}</div><div class="text-[11px] text-slate-400">${remainingText}</div>`;
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

    renderSafetyPanel(d.safety);
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
    renderCandidatesTable(candidates);
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
            ${renderPositionRiskState(pos.risk_state)}
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
