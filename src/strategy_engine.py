import math
import os
import threading
from datetime import datetime, timezone, timedelta
from typing import Any

KST = timezone(timedelta(hours=9))


def get_kst_now() -> datetime:
    """Get current datetime in Korea Standard Time (UTC+9)."""
    return datetime.now(timezone.utc).astimezone(KST)


def is_night_session(dt: datetime | None = None) -> bool:
    """Check if current or provided time is within KST Night Session (00:00 ~ 07:00)."""
    if dt is None:
        kst_dt = get_kst_now()
    else:
        if getattr(dt, "tzinfo", None) is not None:
            kst_dt = dt.astimezone(KST)
        else:
            kst_dt = dt
    return StrategyPolicy.NIGHT_SESSION_START_HOUR <= kst_dt.hour < StrategyPolicy.NIGHT_SESSION_END_HOUR


class StrategyPolicy:
    """
    실거래 및 백테스트 공통 전략 파라미터 및 단일 실행 정책 (Single Source of Truth, SSOT)
    - 진입 목표가, 손절가, 부분익절, 트레일링, 타임스탑, 쿨다운, 알파 하드게이트 일원화
    """
    # 1. 목표가 / 손절가 / 손익비 (ATR 기반 동적 산출)
    ATR_TARGET_MULTIPLIER: float = 2.2   # ATR 기반 목표가 배수 (상향)
    ATR_STOP_MULTIPLIER: float = 1.6     # ATR 기반 손절가 배수 (노이즈 방어)
    MIN_TARGET_PCT: float = 0.035        # 최소 목표 수익률 +3.5%
    PROFIT_TARGET_PCT: float = 0.035     # 기본 목표 수익률 호환 별칭 (+3.5%)
    MIN_STOP_PCT: float = 0.018          # 기본 최소 손절선 -1.8%
    STOP_LOSS_PCT: float = 0.022         # 기본 손절 -2.2% (단기 노이즈 휩소 방어)

    # 1-1. 메이저 코인(BTC/ETH) 전용 목표가/익절/타임스탑 (낮은 변동성 적응 및 자금 잠김 방어)
    MAJOR_MIN_TARGET_PCT: float = 0.015          # 메이저 최소 목표 수익률 +1.5%
    MAJOR_MIN_STOP_PCT: float = 0.012            # 메이저 기본 최소 손절선 -1.2%
    MAJOR_PARTIAL_TP_1_PCT: float = 0.018        # 메이저 1차 분할 익절 +1.8% (도달 시 30% 익절)
    MAJOR_PARTIAL_TP_2_PCT: float = 0.035        # 메이저 2차 분할 익절 +3.5% (도달 시 30% 추가익절)
    MAJOR_TRAILING_START_PCT: float = 0.015      # 메이저 +1.5% 트레일링 스탑 활성화
    MAJOR_TRAILING_DROP_PCT: float = 0.010       # 메이저 최고점 대비 1.0% 하락 시 시장가 청산
    MAJOR_TIME_STOP_SECONDS_NORMAL: int = 7200   # 메이저 정상장 120분 타임스탑
    MAJOR_TIME_STOP_SECONDS_RISK_OFF: int = 3600 # 메이저 약세장 60분 타임스탑

    # 2. 익절 및 트레일링 스탑 (3단계 다단계 분할 익절 & 40% 러너 추세 추종)
    PARTIAL_TP_PCT: float = 0.035        # 기본 1차 익절 기준 +3.5%
    PARTIAL_TP_1_PCT: float = 0.035      # 1차 +3.5% 도달 시 30% 분할 익절
    PARTIAL_TP_1_RATIO: float = 0.30     # 1차 익절 비중 (30% 선제 수익 실현)
    PARTIAL_TP_2_PCT: float = 0.070      # 2차 +7.0% 도달 시 원금의 30% 분할 익절
    PARTIAL_TP_2_RATIO: float = 0.30     # 2차 익절 비중 (잔여 중 42.85% = 원금의 30%)
    BREAKEVEN_STOP_PCT: float = 0.003    # 1차 익절 완료 후 본전 보장 스탑 (+0.3% 수수료 보장)
    TRAILING_START_PCT: float = 0.030    # +3.0% 트레일링 스탑 활성화
    TRAILING_DROP_PCT: float = 0.020     # 최고점 대비 2.0% 하락 시 시장가 청산 (알트 숨고르기 허용)
    MIN_PROFIT_BUFFER_PCT: float = 0.005 # +0.5% 최소 보장 마진

    # 3. 시간 기반 청산 (타임스탑) & 15분 모멘텀 조기 탈출 & 쿨다운
    MOMENTUM_EARLY_EXIT_SECONDS: int = 1800 # 30분 모멘텀 소멸 조기 본전 탈출 (1800초)
    MOMENTUM_EARLY_EXIT_BARS_5M: int = 6   # 5분봉 6개 캔들
    TIME_STOP_SECONDS: int = 7200        # 120분 타임스탑 (기본 정상장, 실거래 초 단위)
    TIME_STOP_SECONDS_NORMAL: int = 7200 # 정상장 120분 타임스탑
    TIME_STOP_SECONDS_RISK_OFF: int = 3600 # RISK_OFF 약세장 60분 단축 타임스탑
    TIME_STOP_MAX_HOLD_SECONDS: int = 10800 # 지지선 유지 시 최대 180분 반등 대기 유예
    TIME_STOP_BARS_5M: int = 24          # 5분봉 24개 = 120분 (백테스트 캔들 단위)
    TIME_STOP_BARS_5M_RISK_OFF: int = 12 # 5분봉 12개 = 60분 (RISK_OFF 백테스트 캔들 단위)
    TIME_STOP_MAX_HOLD_BARS_5M: int = 36 # 최대 유예 36봉 (180분)
    TIME_STOP_BREAKEVEN_MIN_PNL_PCT: float = 0.0005 # 타임스탑 실질 본전 기준 (+0.05% 수수료 세이브)
    COOLDOWN_STOP_LOSS_SEC: float = 1200.0  # 손절 후 쿨다운 20분
    COOLDOWN_TIME_STOP_SEC: float = 600.0   # 타임스탑 횡보 청산 후 쿨다운 10분
    COOLDOWN_TP_SEC: float = 300.0          # 트레일링 익절 후 쿨다운 5분 (2차 랠리 조기 참여)
    REENTRY_BUFFER_PCT: float = 0.012       # 직전 청산가 대비 최소 돌파/눌림목 갭 버퍼 (+1.2%)
    REENTRY_FILTER_EXPIRY_SEC: float = 2700.0  # 직전 청산가 갭 필터 유지 시간 (45분)

    # 4. 하드 안전 게이트 (Hard Safety Gates) & 상대 강도(RS) 임계값
    ALPHA_BUY_THRESHOLD: int = 60        # 7대 팩터 복합 알파 승인 점수 (100점 만점)
    ALPHA_BUY_THRESHOLD_NORMAL: int = 60 # 정상장 7대 팩터 복합 알파 승인 점수
    ALPHA_BUY_THRESHOLD_RISK_OFF: int = 60 # RISK_OFF 약세장 공격형 승인 점수
    RS_MIN_RISK_OFF: float = 0.008       # RISK_OFF 시 BTC 대비 최소 상대 강도 (+0.8% 초과 상승)
    MIN_TRADE_VALUE_RISK_OFF: float = 2_000_000_000.0  # 약세장 최소 24시간 거래대금 20억 원
    MIN_ASSET_PRICE_KRW: float = 10.0    # 10원 미만 극초저가 코인 차단
    RSI_MIN_NORMAL: float = 42.0         # 정상장 저점 반등 확인용 RSI 최소치
    RSI_MAX_NORMAL: float = 60.0         # 정상장 고점 추격 방지용 RSI 최대치
    RSI_MIN_RISK_OFF: float = 42.0       # RISK_OFF 저점 반등 확인용 RSI 최소치
    RSI_MAX_RISK_OFF: float = 58.0       # RISK_OFF 고점 추격 방지용 RSI 최대치
    PCT_B_MIN: float = 0.20              # 볼린저 밴드 %B 최소치
    PCT_B_MAX: float = 0.65              # 상단권 모멘텀 추격을 차단하는 절대 상한
    PULLBACK_PCT_B_MIN_NORMAL: float = 0.25  # 정상장 저점권 반등 후보 하한
    PULLBACK_PCT_B_MAX_NORMAL: float = 0.60  # 정상장 저점권 반등 후보 상한
    PULLBACK_PCT_B_MIN_RISK_OFF: float = 0.28  # RISK_OFF 반등 후보 하한
    PULLBACK_PCT_B_MAX_RISK_OFF: float = 0.55  # RISK_OFF 반등 후보 상한
    PULLBACK_LOOKBACK_BARS: int = 12      # 최근 지지 저점 산정에 사용하는 5분봉 수
    PULLBACK_MAX_DISTANCE_NORMAL: float = 0.035  # 정상장 최근 저점 대비 최대 허용 거리
    PULLBACK_MAX_DISTANCE_RISK_OFF: float = 0.025  # RISK_OFF 최근 저점 대비 최대 허용 거리
    MAX_MA20_DISPARITY: float = 1.025    # MA20 대비 최대 이격도 +2.5%
    MAX_UPPER_SHADOW_RATIO: float = 0.55 # 캔들 윗꼬리 최대 허용 비율 (55%)
    MA_ALIGNMENT_RATIO: float = 0.995    # MA5 >= MA20 * 0.995
    PULLBACK_MA_ALIGNMENT_RATIO: float = 0.990  # 저점 반등은 MA20 아래 1% 이내 회복까지 허용
    RISK_OFF_ALLOC_RATIO: float = 0.6    # RISK_OFF 진입 비중 (60%로 확대하여 알트 불장 수익 확보)

    # 4-2. 급락 후 반등 전용 정책: 일반 RISK_OFF 기준을 낮추지 않고, 별도·축소 비중으로만 사용한다.
    RECOVERY_REBOUND_ENABLED: bool = True
    # 반등 전용 조건을 통과한 경우에만 실제 주문 경로를 허용한다.
    RECOVERY_REBOUND_LIVE_ENABLED: bool = True
    RECOVERY_REBOUND_ALPHA_THRESHOLD: int = 75
    RECOVERY_REBOUND_RS_MIN: float = 0.015
    RECOVERY_REBOUND_MTF_EMA20_RATIO: float = 0.990
    RECOVERY_REBOUND_ALLOC_RATIO: float = 0.35

    # 4-1. 공격형 모멘텀 돌파는 미완성 봉이 아닌 최신 확정봉만으로 평가한다.
    MOMENTUM_BREAKOUT_ALPHA_THRESHOLD_NORMAL: int = 55
    MOMENTUM_BREAKOUT_ALPHA_THRESHOLD_RISK_OFF: int = 60
    MOMENTUM_BREAKOUT_ALPHA_THRESHOLD_NIGHT: int = 65
    MOMENTUM_BREAKOUT_ALPHA_THRESHOLD_NIGHT_RISK_OFF: int = 70
    MOMENTUM_BREAKOUT_VOLUME_RATIO_MIN: float = 1.3
    MOMENTUM_BREAKOUT_LOOKBACK_BARS: int = 4
    MOMENTUM_BREAKOUT_RSI_MIN: float = 52.0
    MOMENTUM_BREAKOUT_RSI_MAX: float = 70.0
    MOMENTUM_BREAKOUT_RS_MIN: float = 0.008
    MOMENTUM_BREAKOUT_MTF_EMA20_RATIO: float = 0.990
    MOMENTUM_BREAKOUT_ALLOC_RATIO: float = 0.25

    # 5. 거시 시장 리스크 및 거래소 비용
    BTC_CRASH_THRESHOLD_PCT: float = 0.015  # BTC 15분 -1.5% 급락 시 차단
    FEE_RATE: float = 0.0004             # 편도 수수료 0.04%
    SLIPPAGE_RATE: float = 0.001         # 편도 슬리피지 0.10%
    MIN_ORDER_KRW: float = 5000.0        # 최소 주문금액
    MAX_DAILY_LOSS_PCT: float = 0.05     # 일일 손실 한도 5%
    # 초기 호가 관측은 단일 스냅샷 왜곡을 막기 위해 중립값으로 감쇠한다.
    ORDERBOOK_MIN_SAMPLES_CANDIDATE: int = 3
    # RISK_OFF는 성과 검증 전 현행 축소 비중을 유지하며 자동 차단을 활성화하지 않는다.
    RISK_OFF_POLICY_MODE: str = "reduced_size"
    RISK_OFF_BLOCK_ENABLED: bool = False
    RISK_OFF_MIN_SAMPLE_CANDIDATE: int = 30

    # 6. 심야 세션 (00:00 ~ 07:00 KST) 동적 필터 및 비중 정책
    NIGHT_SESSION_START_HOUR: int = 0
    NIGHT_SESSION_END_HOUR: int = 7
    ALPHA_BUY_THRESHOLD_NIGHT: int = 75           # 심야 정상장 알파 승인 점수 (60 -> 75 상향)
    ALPHA_BUY_THRESHOLD_NIGHT_RISK_OFF: int = 70  # 심야 약세장 공격형 알파 승인 점수
    NIGHT_SESSION_ALLOC_RATIO: float = 0.50       # 심야 진입 자금 비중 50% 축소
    NIGHT_PARTIAL_TP_1_PCT: float = 0.015         # 심야 1차 분할 익절 +1.5% (조기 수익 확정)
    NIGHT_TIME_STOP_SECONDS: int = 5400           # 심야 90분 단축 타임스탑
    NIGHT_TRADE_VALUE_MULTIPLIER: float = 1.5     # 심야 최소 거래대금 1.5배 상향


def get_alpha_buy_threshold(btc_regime: str = "NORMAL", is_night: bool | None = None) -> int:
    """BTC 레짐과 심야 여부에 따른 신규 진입 알파 기준을 단일 기준으로 반환한다."""
    regime_upper = str(btc_regime or "NORMAL").upper()
    night_active = is_night if is_night is not None else is_night_session()

    # 점수 표시, 일반 진입, 반등 전용 진입이 같은 심야 보수 기준을 사용해야 한다.
    if night_active:
        return (
            StrategyPolicy.ALPHA_BUY_THRESHOLD_NIGHT_RISK_OFF
            if regime_upper == "RISK_OFF"
            else StrategyPolicy.ALPHA_BUY_THRESHOLD_NIGHT
        )
    return (
        StrategyPolicy.ALPHA_BUY_THRESHOLD_RISK_OFF
        if regime_upper == "RISK_OFF"
        else StrategyPolicy.ALPHA_BUY_THRESHOLD_NORMAL
    )


def get_momentum_breakout_alpha_threshold(btc_regime: str = "NORMAL", is_night: bool | None = None) -> int:
    """확정봉 모멘텀 돌파 전용 알파 기준을 세션과 BTC 레짐별로 반환한다."""
    regime_upper = str(btc_regime or "NORMAL").upper()
    night_active = is_night if is_night is not None else is_night_session()
    if night_active:
        return (
            StrategyPolicy.MOMENTUM_BREAKOUT_ALPHA_THRESHOLD_NIGHT_RISK_OFF
            if regime_upper == "RISK_OFF"
            else StrategyPolicy.MOMENTUM_BREAKOUT_ALPHA_THRESHOLD_NIGHT
        )
    return (
        StrategyPolicy.MOMENTUM_BREAKOUT_ALPHA_THRESHOLD_RISK_OFF
        if regime_upper == "RISK_OFF"
        else StrategyPolicy.MOMENTUM_BREAKOUT_ALPHA_THRESHOLD_NORMAL
    )


class OrderbookFlowTracker:
    """
    호가창 단일 스냅샷 왜곡 방지 및 최근 N회 호가 잔량비 롤링 평균 추적기 (과제 E)
    - 실시간 허매수/허매도(Spoofing) 왜곡 완충
    - 메모리 롤링 큐(최대 5회) 기반 스레드 안전성 보장
    """
    def __init__(self, max_history: int = 5):
        self.max_history = max_history
        self._history: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def record_snapshot(self, market: str, total_bid: float, total_ask: float) -> float:
        """시장별 호가 잔량비를 기록하고 기존 호출자 호환을 위해 평균만 반환한다."""
        ratio = total_bid / total_ask if total_ask > 0 else 1.0
        with self._lock:
            if market not in self._history:
                self._history[market] = []
            buf = self._history[market]
            buf.append(ratio)
            if len(buf) > self.max_history:
                buf.pop(0)
            return sum(buf) / len(buf)

    def get_sample_count(self, market: str) -> int:
        """특정 시장 호가 버퍼의 관측 수를 안전하게 반환한다."""
        with self._lock:
            return len(self._history.get(market, []))

    def get_smoothed_ratio(self, market: str, fallback_ratio: float = 1.0) -> float:
        """현재 저장된 롤링 호가 잔량비 반환"""
        with self._lock:
            buf = self._history.get(market, [])
            if not buf:
                return fallback_ratio
            return sum(buf) / len(buf)


def build_orderbook_tracker_key(market: str, exchange: str = "") -> str:
    """거래소와 마켓을 함께 사용해 서로 다른 주문장 이력을 격리한다."""
    normalized_market = (market or "UNKNOWN_MARKET").upper()
    normalized_exchange = (exchange or "").strip().lower()
    return f"{normalized_exchange}:{normalized_market}" if normalized_exchange else normalized_market


MAJOR_MARKETS = {"KRW-BTC", "BTC", "KRW-ETH", "ETH"}


def is_major_market(market: str) -> bool:
    """시가총액 상위 대형 메이저 코인(BTC, ETH) 여부 판별"""
    if not market:
        return False
    m = str(market).strip().upper()
    return m in MAJOR_MARKETS or m.replace("KRW-", "") in {"BTC", "ETH"}


def select_completed_candles(candles: list[dict[str, Any]], minimum_count: int) -> list[dict[str, Any]]:
    """최신순 API 캔들에서 진행 중인 첫 봉을 제외하고 유효한 확정봉만 반환한다."""
    if len(candles) < minimum_count + 1:
        return []
    completed = candles[1:]
    # 가격 필드가 비어 있으면 지표가 정상처럼 계산되지 않도록 신규 진입을 차단한다.
    if any(float(candle.get("trade_price", 0.0) or 0.0) <= 0.0 for candle in completed[:minimum_count]):
        return []
    timestamp_keys = ("candle_date_time_utc", "candle_date_time_kst", "timestamp")
    current_value = next((candles[0].get(key) for key in timestamp_keys if candles[0].get(key) is not None), None)
    completed_value = next((candles[1].get(key) for key in timestamp_keys if candles[1].get(key) is not None), None)
    # 시각이 제공되는 응답에서 최신 봉과 확정 봉의 시간이 같으면 정렬/데이터 오류로 판단한다.
    if current_value is not None and completed_value is not None and current_value == completed_value:
        return []
    return completed


# 글로벌 롤링 호가 추적기 싱글톤
global_orderbook_tracker = OrderbookFlowTracker(max_history=5)



def calculate_rsi(prices: list[float], period: int = 14) -> float:
    """Calculate standard Relative Strength Index (RSI)."""
    if len(prices) < period + 1:
        return 50.0
    chronological = prices[::-1]
    changes = [chronological[i] - chronological[i - 1] for i in range(1, len(chronological))]
    gains = [max(change, 0.0) for change in changes[-period:]]
    losses = [max(-change, 0.0) for change in changes[-period:]]
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = (sum(gains) / period) / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


def calculate_bollinger_bands(prices: list[float], period: int = 20, num_std: float = 2.0) -> dict[str, float]:
    """Calculate Bollinger Bands (upper, middle, lower, width_pct, %B)."""
    if len(prices) < period:
        current = prices[0] if prices else 0.0
        return {"upper": current * 1.02, "middle": current, "lower": current * 0.98, "width_pct": 4.0, "pct_b": 0.5}

    subset = prices[:period]
    middle = sum(subset) / len(subset)
    variance = sum((x - middle) ** 2 for x in subset) / len(subset)
    std = math.sqrt(variance)

    upper = middle + (num_std * std)
    lower = middle - (num_std * std)
    width_pct = ((upper - lower) / middle) * 100.0 if middle > 0 else 0.0
    pct_b = (prices[0] - lower) / (upper - lower) if (upper - lower) > 0 else 0.5

    return {
        "upper": round(upper, 2),
        "middle": round(middle, 2),
        "lower": round(lower, 2),
        "width_pct": round(width_pct, 2),
        "pct_b": round(pct_b, 2),
    }


def bollinger(prices: list[float], period: int = 20) -> tuple[float, float]:
    """Compatibility helper returning (middle_ma20, pct_b)."""
    bands = calculate_bollinger_bands(prices, period=period)
    return bands["middle"], bands["pct_b"]


def calculate_ema(prices: list[float], period: int) -> float:
    """Calculate Exponential Moving Average (EMA)."""
    if len(prices) < period:
        return sum(prices) / len(prices) if prices else 0.0

    chronological = prices[::-1]
    k = 2.0 / (period + 1)
    ema = sum(chronological[:period]) / period
    for price in chronological[period:]:
        ema = (price * k) + (ema * (1.0 - k))
    return ema


def calculate_ema_series(prices: list[float], period: int) -> list[float]:
    """Calculate full EMA series for chronological prices (oldest-first)."""
    if not prices:
        return []
    if len(prices) < period:
        # Fallback: simple expanding mean
        result = []
        acc = 0.0
        for i, p in enumerate(prices, 1):
            acc += p
            result.append(acc / i)
        return result

    k = 2.0 / (period + 1)
    ema = sum(prices[:period]) / period
    result = [prices[i] for i in range(period - 1)] + [ema]
    for price in prices[period:]:
        ema = (price * k) + (ema * (1.0 - k))
        result.append(ema)
    return result


def calculate_macd(prices: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict[str, Any]:
    """Calculate MACD Line, Signal Line, Histogram and Trend using standard exponential series."""
    if len(prices) < slow + signal:
        return {"macd": 0.0, "signal": 0.0, "hist": 0.0, "trend": "NEUTRAL"}

    chronological = prices[::-1]
    ema_fast_series = calculate_ema_series(chronological, fast)
    ema_slow_series = calculate_ema_series(chronological, slow)

    # Compute MACD series for available overlap
    macd_series = [f - s for f, s in zip(ema_fast_series, ema_slow_series)]
    signal_series = calculate_ema_series(macd_series, signal)

    macd_line = macd_series[-1]
    signal_line = signal_series[-1]
    hist = macd_line - signal_line

    trend = "BULLISH" if macd_line > signal_line and macd_line > 0 else ("BEARISH" if macd_line < signal_line and macd_line < 0 else "NEUTRAL")

    return {
        "macd": round(macd_line, 2),
        "signal": round(signal_line, 2),
        "hist": round(hist, 2),
        "trend": trend,
    }


def calculate_atr(candles: list[dict[str, Any]], period: int = 14) -> dict[str, float]:
    """Calculate Average True Range (ATR) and percentage."""
    if not candles or len(candles) < 2:
        return {"atr": 0.0, "atr_pct": 2.0}

    ranges = []
    for index in range(min(len(candles) - 1, period)):
        candle, previous = candles[index], candles[index + 1]
        high = float(candle.get("high_price", 0.0))
        low = float(candle.get("low_price", 0.0))
        prior_close = float(previous.get("trade_price", 0.0))
        ranges.append(max(high - low, abs(high - prior_close), abs(low - prior_close)))

    atr_val = sum(ranges) / len(ranges) if ranges else 0.0
    current_price = float(candles[0].get("trade_price", 1.0))
    atr_pct = (atr_val / current_price * 100.0) if current_price > 0 else 2.0

    return {"atr": round(atr_val, 2), "atr_pct": round(atr_pct, 2)}


def atr(candles: list[dict[str, Any]], period: int = 14) -> float:
    """Compatibility helper returning raw ATR value."""
    return calculate_atr(candles, period=period)["atr"]


def calculate_chandelier_exit(candles: list[dict[str, Any]], period: int = 14, multiplier: float = 1.5) -> float:
    """Calculate Chandelier Exit trailing stop price (Highest High - multiplier * ATR)."""
    if not candles:
        return 0.0
    subset = candles[:min(len(candles), period)]
    highest_high = max(float(c.get("high_price", 0.0)) for c in subset)
    atr_val = atr(candles, period=period)
    return round(highest_high - (multiplier * atr_val), 2)


def classify_btc_regime(
    candles_5m: list[dict[str, Any]],
    candles_1h: list[dict[str, Any]] | None = None,
    crash_threshold_pct: float = 0.015,
) -> dict[str, Any]:
    """Classify BTC market regime into NORMAL, RISK_OFF, or CRASH.

    - CRASH: Recent 5m/15m drop >= crash_threshold_pct (1.5%) -> Stop all new buys
    - RISK_OFF: 1H Close < 1H EMA50 or 1H drop >= 1.0% -> Stricter gates & 50% sizing
    - NORMAL: Healthy uptrend/stable state
    """
    if not candles_5m or len(candles_5m) < 3:
        return {"regime": "NORMAL", "reason": "BTC 데이터 부족"}

    cur_p = float(candles_5m[0].get("trade_price", 0.0))
    p_3 = float(candles_5m[min(len(candles_5m) - 1, 3)].get("trade_price", cur_p))
    recent_drop = (cur_p - p_3) / p_3 if p_3 > 0 else 0.0

    if recent_drop <= -crash_threshold_pct:
        return {
            "regime": "CRASH",
            "drop_pct": round(recent_drop * 100.0, 2),
            "reason": f"BTC 15분 급락 경보 ({recent_drop*100.0:.2f}%)",
        }

    # 1H Check
    if candles_1h and len(candles_1h) >= 20:
        prices_1h = [float(c.get("trade_price", 0.0)) for c in candles_1h]
        ema50_1h = calculate_ema(prices_1h, min(len(prices_1h), 50))
        cur_1h = prices_1h[0]
        p_1h_prev = prices_1h[min(len(prices_1h) - 1, 3)]
        drop_1h = (cur_1h - p_1h_prev) / p_1h_prev if p_1h_prev > 0 else 0.0

        if cur_1h < ema50_1h or drop_1h <= -0.010:
            sub_reason = "1H EMA50 하회" if cur_1h < ema50_1h else f"1H {drop_1h*100.0:.1f}% 하락"
            return {
                "regime": "RISK_OFF",
                "drop_pct": round(drop_1h * 100.0, 2),
                "reason": f"BTC 약세/조정 ({sub_reason})",
            }

    return {"regime": "NORMAL", "drop_pct": round(recent_drop * 100.0, 2), "reason": "BTC 정상 안정세"}


def calculate_relative_strength(
    candles_asset: list[dict[str, Any]],
    candles_btc: list[dict[str, Any]],
    lookback_bars: int = 12,
) -> dict[str, Any]:
    """
    비트코인 대비 자산의 상대 강도(RS, Relative Strength) 산출
    - RS(%) = (자산 최근 N봉 변동률%) - (BTC 최근 N봉 변동률%)
    - 양수(+) : 비트코인 대비 초과 상승(독자 강세)
    - 음수(-) : 비트코인 대비 약세/언더퍼폼
    """
    if not candles_asset or not candles_btc or len(candles_asset) < 2 or len(candles_btc) < 2:
        return {"rs_pct": 0.0, "asset_chg_pct": 0.0, "btc_chg_pct": 0.0, "is_outlier": False, "desc": "RS 계산 데이터 부족"}

    n_asset = min(len(candles_asset) - 1, lookback_bars)
    n_btc = min(len(candles_btc) - 1, lookback_bars)

    p_asset_now = float(candles_asset[0].get("trade_price", 0.0))
    p_asset_past = float(candles_asset[n_asset].get("trade_price", p_asset_now))
    chg_asset = ((p_asset_now - p_asset_past) / p_asset_past * 100.0) if p_asset_past > 0 else 0.0

    p_btc_now = float(candles_btc[0].get("trade_price", 0.0))
    p_btc_past = float(candles_btc[n_btc].get("trade_price", p_btc_now))
    chg_btc = ((p_btc_now - p_btc_past) / p_btc_past * 100.0) if p_btc_past > 0 else 0.0

    rs_pct = chg_asset - chg_btc
    is_outlier = (rs_pct >= 1.5) and (chg_asset > 0.0)

    if rs_pct >= 2.0:
        desc = f"🔥 BTC 대비 압도적 독자 강세 (RS: +{rs_pct:.2f}% | 코인 {chg_asset:+.2f}% vs BTC {chg_btc:+.2f}%)"
    elif rs_pct >= 0.5:
        desc = f"🟢 BTC 대비 상대적 강세 (RS: +{rs_pct:.2f}% | 코인 {chg_asset:+.2f}% vs BTC {chg_btc:+.2f}%)"
    elif rs_pct <= -1.0:
        desc = f"🔴 BTC 대비 언더퍼폼/약세 (RS: {rs_pct:.2f}% | 코인 {chg_asset:+.2f}% vs BTC {chg_btc:+.2f}%)"
    else:
        desc = f"⚪ BTC와 유사/동조화 (RS: {rs_pct:+.2f}%)"

    return {
        "rs_pct": round(rs_pct, 2),
        "asset_chg_pct": round(chg_asset, 2),
        "btc_chg_pct": round(chg_btc, 2),
        "is_outlier": is_outlier,
        "desc": desc,
    }


def calculate_vwap(candles: list[dict[str, Any]]) -> dict[str, Any]:
    """Calculate Volume Weighted Average Price (VWAP) from recent candles."""
    if not candles:
        return {"vwap": 0.0, "disparity_pct": 0.0, "is_above": False}

    cum_pv = 0.0
    cum_vol = 0.0
    for c in candles[:min(len(candles), 30)]:
        h = float(c.get("high_price", 0.0))
        l = float(c.get("low_price", 0.0))
        close_p = float(c.get("trade_price", 0.0))
        vol = float(c.get("candle_acc_trade_volume", 0.0))
        typical_p = (h + l + close_p) / 3.0 if (h > 0 and l > 0 and close_p > 0) else close_p
        cum_pv += typical_p * vol
        cum_vol += vol

    current_price = float(candles[0].get("trade_price", 0.0))
    if cum_vol <= 0 or cum_pv <= 0:
        return {"vwap": current_price, "disparity_pct": 0.0, "is_above": True}

    vwap_val = cum_pv / cum_vol
    disparity_pct = ((current_price - vwap_val) / vwap_val * 100.0) if vwap_val > 0 else 0.0
    is_above = current_price >= (vwap_val * 0.998)

    return {
        "vwap": round(vwap_val, 2),
        "disparity_pct": round(disparity_pct, 2),
        "is_above": is_above,
    }


def calculate_macd_acceleration(
    prices: list[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> dict[str, Any]:
    """Calculate MACD Histogram Slope & Momentum Acceleration."""
    if len(prices) < slow + signal + 3:
        macd_base = calculate_macd(prices, fast, slow, signal)
        return {
            "macd": macd_base.get("macd", 0.0),
            "signal": macd_base.get("signal", 0.0),
            "hist": macd_base.get("hist", 0.0),
            "slope": 0.0,
            "is_accelerating": False,
            "momentum_state": "NEUTRAL",
        }

    chronological = prices[::-1]
    ema_fast_series = calculate_ema_series(chronological, fast)
    ema_slow_series = calculate_ema_series(chronological, slow)

    macd_series = [f - s for f, s in zip(ema_fast_series, ema_slow_series)]
    signal_series = calculate_ema_series(macd_series, signal)

    hist_series = [m - s for m, s in zip(macd_series, signal_series)]
    hist_now = hist_series[-1]
    hist_prev1 = hist_series[-2] if len(hist_series) >= 2 else hist_now
    hist_prev2 = hist_series[-3] if len(hist_series) >= 3 else hist_prev1

    slope = hist_now - hist_prev1
    is_accelerating = (slope > 0) and (hist_now > hist_prev1 >= hist_prev2 or hist_now > 0)

    if hist_now > 0 and is_accelerating:
        state = "ACCELERATING_BULL"
    elif hist_now > 0 and not is_accelerating:
        state = "DECELERATING_BULL"
    elif is_accelerating:
        state = "RECOVERING"
    else:
        state = "BEARISH"

    return {
        "macd": round(macd_series[-1], 2),
        "signal": round(signal_series[-1], 2),
        "hist": round(hist_now, 2),
        "slope": round(slope, 4),
        "is_accelerating": is_accelerating,
        "momentum_state": state,
    }


def calculate_composite_alpha_score(
    candles: list[dict[str, Any]],
    candles_1h: list[dict[str, Any]] | None = None,
    orderbook: dict[str, Any] | None = None,
    btc_regime: str = "NORMAL",
    market: str = "",
    exchange: str = "",
    is_night: bool | None = None,
) -> dict[str, Any]:
    """Calculate 7-Factor Composite Quantitative Alpha Score (0 ~ 100 points)."""
    if not candles or len(candles) < 20:
        return {"total_score": 0, "allow_buy": False, "factor_breakdown": {}, "reason": "데이터 부족"}

    prices = [float(c.get("trade_price", 0.0)) for c in candles]
    current = prices[0]

    # 1. MTF 1H 추세 (15점)
    score_mtf = 0
    mtf_reason = "1H 미제공"
    if candles_1h and len(candles_1h) >= 20:
        prices_1h = [float(c.get("trade_price", 0.0)) for c in candles_1h]
        ema20_1h = calculate_ema(prices_1h, 20)
        if prices_1h[0] >= ema20_1h:
            score_mtf = 15
            mtf_reason = "1H 정배열 강세"
        elif prices_1h[0] >= (ema20_1h * 0.980):
            score_mtf = 10
            mtf_reason = "1H 지지/초기 반등권"
        else:
            score_mtf = 3
            mtf_reason = "1H 역배열 약세"
    else:
        score_mtf = 10

    # 2. VWAP 지지/돌파 및 단기 이격 점수 (15점)
    vwap_data = calculate_vwap(candles)
    if vwap_data["is_above"] and vwap_data["disparity_pct"] <= 2.5:
        score_vwap = 15  # VWAP 상단 안정적 지지 안착권
    elif vwap_data["is_above"] and vwap_data["disparity_pct"] <= 3.5:
        score_vwap = 10  # 완만한 이격
    else:
        score_vwap = 0   # 과도한 이격 과열 또는 VWAP 하회

    # 3. MACD 히스토그램 가속도 (15점)
    macd_acc = calculate_macd_acceleration(prices)
    if macd_acc["momentum_state"] == "ACCELERATING_BULL":
        score_macd = 15
    elif macd_acc["is_accelerating"]:
        score_macd = 12
    elif macd_acc["hist"] > 0:
        score_macd = 8
    else:
        score_macd = 0

    # 4. RSI 골든존 (15점) - 상승 초입(40~62) 집중 가산, 과매수(66 초과) 0점
    rsi_val = calculate_rsi(prices, 14)
    if 40.0 <= rsi_val <= 62.0:
        score_rsi = 15  # 최적의 상승 초입/눌림목 반등 구간
    elif (36.0 <= rsi_val < 40.0) or (62.0 < rsi_val <= 66.0):
        score_rsi = 10  # 경계 구간
    else:
        score_rsi = 0   # 과매수(>66) 또는 과매도 침체(<36)

    # 5. 볼린저 밴드 중심선 지지 및 안정적 밴드 내 상승 (15점)
    bb = calculate_bollinger_bands(prices, 20, 2.0)
    ma5 = sum(prices[:5]) / 5.0
    if ma5 >= (bb["middle"] * 0.998) and (0.30 <= bb["pct_b"] <= 0.72):
        score_bb = 15   # 밴드 중심선 상단 안착 및 여유 공간 확보 (초입)
    elif (0.20 <= bb["pct_b"] < 0.30) or (0.72 < bb["pct_b"] <= 0.78):
        score_bb = 10   # 밴드 하단 근접 또는 상단 근접
    else:
        score_bb = 0    # 밴드 상단 꼭대기(>0.78) 또는 하단 이탈(<0.20)

    # 6. 수급 / 체결강도 및 호가창 잔량비 (15점)
    score_orderflow = 10  # 호가 데이터가 없을 때의 중립 점수
    raw_ratio = 1.0
    smoothed_ratio = 1.0
    orderbook_sample_count = 0
    if orderbook:
        total_ask = float(orderbook.get("total_ask_size", 1.0))
        total_bid = float(orderbook.get("total_bid_size", 1.0))
        # 롤링 호가 잔량비 반영 (단일 스냅샷 왜곡 완충, 과제 E)
        raw_ratio = total_bid / total_ask if total_ask > 0 else 1.0
        tracker_key = build_orderbook_tracker_key(market, exchange)
        smoothed_ratio = global_orderbook_tracker.record_snapshot(tracker_key, total_bid, total_ask)
        orderbook_sample_count = global_orderbook_tracker.get_sample_count(tracker_key)
        effective_ratio = (raw_ratio * 0.5) + (smoothed_ratio * 0.5)
        # 관측 수가 적을수록 단일 호가창 이상치가 알파를 과대평가하지 않게 중립값으로 감쇠한다.
        min_samples = max(1, StrategyPolicy.ORDERBOOK_MIN_SAMPLES_CANDIDATE)
        confidence = min(1.0, orderbook_sample_count / min_samples)
        effective_ratio = 1.0 + ((effective_ratio - 1.0) * confidence)
        # 충분한 관측 전에는 매수 가산 또는 매도 감산을 확정하지 않고 중립 점수를 유지한다.
        if orderbook_sample_count < min_samples:
            score_orderflow = 10
        elif effective_ratio >= 1.4:
            score_orderflow = 15
        elif effective_ratio < 0.6:
            score_orderflow = 3

    # 7. 볼륨 스파이크 (10점)
    vols = [float(c.get("candle_acc_trade_volume", 0.0)) for c in candles]
    avg_vol_20 = (sum(vols[1:21]) / 20.0) if len(vols) >= 21 else (vols[0] if vols else 1.0)
    current_vol = vols[0] if vols else 0.0
    vol_ratio = (current_vol / avg_vol_20) if avg_vol_20 > 0 else 1.0
    if vol_ratio >= 2.0 and current >= float(candles[0].get("opening_price", current)):
        score_vol = 10
    elif vol_ratio >= 1.2:
        score_vol = 7
    else:
        score_vol = 4

    regime_upper = btc_regime.upper()
    night_active = is_night if is_night is not None else is_night_session()
    # 알파 계산 결과와 최종 진입 판정이 동일한 정책 기준을 참조한다.
    buy_threshold = get_alpha_buy_threshold(btc_regime, night_active)
    total_score = score_mtf + score_vwap + score_macd + score_rsi + score_bb + score_orderflow + score_vol
    allow_buy = (total_score >= buy_threshold) and (regime_upper not in ("CRASH", "BEAR_VOLATILE"))

    breakdown = {
        "mtf_score": score_mtf,
        "vwap_score": score_vwap,
        "macd_score": score_macd,
        "rsi_score": score_rsi,
        "bollinger_score": score_bb,
        "orderflow_score": score_orderflow,
        "volume_score": score_vol,
        "orderbook_raw_ratio": round(raw_ratio, 6),
        "orderbook_smoothed_ratio": round(smoothed_ratio, 6),
        "orderbook_sample_count": orderbook_sample_count,
        "is_night": night_active,
    }

    night_tag = " [🌙심야세션]" if night_active else ""
    return {
        "total_score": total_score,
        "allow_buy": allow_buy,
        "factor_breakdown": breakdown,
        "vwap": vwap_data["vwap"],
        "macd_state": macd_acc["momentum_state"],
        "rsi": rsi_val,
        "pct_b": bb["pct_b"],
        "reason": f"알파 스코어 {total_score}/100점 ({'🟢 승인' if allow_buy else '⚪ 미달'}{night_tag}, 기준 {buy_threshold}점) | MTF:{score_mtf} VWAP:{score_vwap} MACD:{score_macd} RSI:{score_rsi} BB:{score_bb}",
    }


def entry_signal(
    candles: list[dict[str, Any]],
    candles_1h: list[dict[str, Any]] | None = None,
    btc_regime: str = "NORMAL",
    orderbook: dict[str, Any] | None = None,
    market: str = "",
    exchange: str = "",
    entry_type: str = "CONFIRMED",
    is_night: bool | None = None,
) -> dict[str, Any]:
    """
    결정론적 퀀트 진입 신호 생성기 (Deterministic Entry Engine)
    - StrategyPolicy 단일 진실 공급원(SSOT) 100% 참조
    - 하드 안전 게이트(Hard Safety Gates): 극초과열, 볼린저 이탈, 역배열 차단 (알파 점수로 우회 불가)
    - 7대 팩터 복합 알파 소프트 스코어 결합
    - ATR 기반 동적 목표가/손절가 일원화 산출
    """
    if len(candles) < 25:
        return {"allow_buy": False, "reason": "캔들 데이터 부족"}

    prices_check = [float(c.get("trade_price", 0.0)) for c in candles]
    cur_check = prices_check[0] if prices_check else 0.0
    if cur_check < StrategyPolicy.MIN_ASSET_PRICE_KRW:
        return {
            "allow_buy": False,
            "reason": f"초저가 종목 호가 갭 위험 차단 (현재가 {cur_check:,.2f}원 < {StrategyPolicy.MIN_ASSET_PRICE_KRW:,.1f}원)",
        }

    regime_upper = btc_regime.upper()
    if regime_upper in ("CRASH", "BEAR_VOLATILE"):
        return {"allow_buy": False, "reason": f"BTC 시장 레짐 경보 ({btc_regime})"}

    # 한 번 확정한 심야 상태를 점수 계산과 최종 주문 게이트에 함께 전달한다.
    night_active = is_night if is_night is not None else is_night_session()
    alpha_res = calculate_composite_alpha_score(
        candles=candles,
        candles_1h=candles_1h,
        orderbook=orderbook,
        btc_regime=btc_regime,
        market=market,
        exchange=exchange,
        is_night=night_active,
    )


    prices = [float(c.get("trade_price", 0.0)) for c in candles]
    current = prices[0]
    ma5 = sum(prices[:5]) / 5.0
    bands = calculate_bollinger_bands(prices, period=20)
    ma20 = bands["middle"]
    pct_b = bands["pct_b"]
    rsi = calculate_rsi(prices)

    # 1. 1시간봉 MTF 추세 필터
    mtf_allowed = True
    mtf_reason = "1H MTF 미제공"
    if candles_1h and len(candles_1h) >= 20:
        prices_1h = [float(c.get("trade_price", 0.0)) for c in candles_1h]
        ema20_1h = calculate_ema(prices_1h, 20)
        current_1h = prices_1h[0]
        if regime_upper == "RISK_OFF":
            # 약세장에서는 높은 점수보다 상위 시간봉 지지 확인을 우선한다.
            mtf_ratio = 0.998
        else:
            mtf_ratio = 0.980
        mtf_allowed = current_1h >= (ema20_1h * mtf_ratio)
        mtf_reason = f"1H {current_1h:.1f} {'>=' if mtf_allowed else '<'} EMA20 {ema20_1h:.1f} (기준 {mtf_ratio:.3f})"

    # 2. [과제 B] 하드 안전 게이트 (Hard Safety Gates - 알파 점수로 우회 불가)
    hard_gate_btc = regime_upper not in ("CRASH", "BEAR_VOLATILE")
    hard_gate_mtf = mtf_allowed
    if regime_upper == "RISK_OFF":
        rsi_hard_min = StrategyPolicy.RSI_MIN_RISK_OFF
        rsi_hard_max = StrategyPolicy.RSI_MAX_RISK_OFF
    else:
        rsi_hard_min = StrategyPolicy.RSI_MIN_NORMAL
        rsi_hard_max = StrategyPolicy.RSI_MAX_NORMAL
    hard_gate_rsi = (rsi_hard_min <= rsi <= rsi_hard_max)
    hard_gate_bb = (StrategyPolicy.PCT_B_MIN <= pct_b <= StrategyPolicy.PCT_B_MAX)
    # 저점 반등은 단기 이평이 중심선에 완전히 복귀하기 전의 회복 구간도 허용한다.
    hard_gate_ma = ma5 >= ma20 * StrategyPolicy.PULLBACK_MA_ALIGNMENT_RATIO

    # 2-1. MA20 단기 이격 과열 차단 (이격도 +2.5% 이하)
    hard_gate_disparity = current <= (ma20 * StrategyPolicy.MAX_MA20_DISPARITY)

    # 2-2. 캔들 윗꼬리(피뢰침/매도 폭탄) 차단
    high_0 = float(candles[0].get("high_price", current) or current)
    low_0 = float(candles[0].get("low_price", current) or current)
    open_0 = float(candles[0].get("opening_price", current) or current)
    candle_range = high_0 - low_0
    upper_shadow = high_0 - max(open_0, current)
    upper_shadow_ratio = (upper_shadow / candle_range) if candle_range > 0 else 0.0
    hard_gate_shadow = (upper_shadow_ratio <= StrategyPolicy.MAX_UPPER_SHADOW_RATIO)

    hard_gates_passed = (
        hard_gate_btc and hard_gate_mtf and hard_gate_rsi and hard_gate_bb
        and hard_gate_ma and hard_gate_disparity and hard_gate_shadow
    )

    # 3. 저점권 반등 정량 게이트: 점수가 높아도 상단권 추격을 허용하지 않는다.
    rsi_min, rsi_max = (
        (StrategyPolicy.RSI_MIN_RISK_OFF, StrategyPolicy.RSI_MAX_RISK_OFF)
        if regime_upper == "RISK_OFF"
        else (StrategyPolicy.RSI_MIN_NORMAL, StrategyPolicy.RSI_MAX_NORMAL)
    )
    pct_b_min, pct_b_max = (
        (StrategyPolicy.PULLBACK_PCT_B_MIN_RISK_OFF, StrategyPolicy.PULLBACK_PCT_B_MAX_RISK_OFF)
        if regime_upper == "RISK_OFF"
        else (StrategyPolicy.PULLBACK_PCT_B_MIN_NORMAL, StrategyPolicy.PULLBACK_PCT_B_MAX_NORMAL)
    )
    pullback_max_distance = (
        StrategyPolicy.PULLBACK_MAX_DISTANCE_RISK_OFF
        if regime_upper == "RISK_OFF"
        else StrategyPolicy.PULLBACK_MAX_DISTANCE_NORMAL
    )
    pullback_candles = candles[:StrategyPolicy.PULLBACK_LOOKBACK_BARS]
    recent_low = min(
        (float(c.get("low_price", c.get("trade_price", current)) or current) for c in pullback_candles),
        default=current,
    )
    recent_high = max(
        (float(c.get("high_price", c.get("trade_price", current)) or current) for c in pullback_candles),
        default=current,
    )
    distance_from_recent_low = ((current / recent_low) - 1.0) if recent_low > 0 else float("inf")
    distance_below_recent_high = ((recent_high / current) - 1.0) if current > 0 else 0.0
    previous_close = float(candles[1].get("trade_price", current) or current)
    # 확정봉의 양봉 전환과 직전 종가 회복을 함께 요구해 하락 중 무계획 물타기를 막는다.
    rebound_confirmed = current >= open_0 and current > previous_close
    pullback_zone = pct_b_min <= pct_b <= pct_b_max
    near_recent_low = 0.0 <= distance_from_recent_low <= pullback_max_distance
    signal_5m = (
        ma5 >= ma20 * StrategyPolicy.PULLBACK_MA_ALIGNMENT_RATIO
        and rsi_min <= rsi <= rsi_max
        and pullback_zone
        and near_recent_low
        and rebound_confirmed
    )

    total_score = int(alpha_res.get("total_score", 0) or 0)
    normalized_entry_type = (entry_type or "CONFIRMED").upper()

    # 모멘텀 돌파는 최신 확정 5분봉의 고점·거래량·양봉·RSI를 함께 확인한다.
    momentum_breakout_passed = False
    momentum_breakout_reason = "확인형 후보"
    momentum_mtf_allowed = mtf_allowed
    momentum_mtf_reason = mtf_reason
    if normalized_entry_type == "MOMENTUM_BREAKOUT":
        lookback = StrategyPolicy.MOMENTUM_BREAKOUT_LOOKBACK_BARS
        previous_candles = candles[1:lookback + 1]
        previous_high = max((float(c.get("high_price", c.get("trade_price", 0.0)) or 0.0) for c in previous_candles), default=0.0)
        previous_volumes = [float(c.get("candle_acc_trade_volume", 0.0) or 0.0) for c in candles[1:21]]
        average_volume = (sum(previous_volumes) / len(previous_volumes)) if previous_volumes else 0.0
        current_volume = float(candles[0].get("candle_acc_trade_volume", 0.0) or 0.0)
        current_open = float(candles[0].get("opening_price", current) or current)
        volume_confirmed = average_volume > 0 and current_volume >= average_volume * StrategyPolicy.MOMENTUM_BREAKOUT_VOLUME_RATIO_MIN
        price_breakout = previous_high > 0 and current > previous_high
        bullish_candle = current >= current_open
        momentum_rsi_passed = StrategyPolicy.MOMENTUM_BREAKOUT_RSI_MIN <= rsi <= StrategyPolicy.MOMENTUM_BREAKOUT_RSI_MAX
        if candles_1h and len(candles_1h) >= 20:
            momentum_ema20 = calculate_ema([float(c.get("trade_price", 0.0)) for c in candles_1h], 20)
            momentum_mtf_allowed = current_1h >= momentum_ema20 * StrategyPolicy.MOMENTUM_BREAKOUT_MTF_EMA20_RATIO
            momentum_mtf_reason = (
                f"1H {current_1h:.1f} {'>=' if momentum_mtf_allowed else '<'} "
                f"EMA20 {momentum_ema20:.1f} (모멘텀 기준 {StrategyPolicy.MOMENTUM_BREAKOUT_MTF_EMA20_RATIO:.3f})"
            )
        momentum_breakout_passed = price_breakout and volume_confirmed and bullish_candle and momentum_rsi_passed
        momentum_breakout_reason = (
            f"직전 {lookback}봉 고점 돌파={'통과' if price_breakout else '차단'}, "
            f"거래량배수={(current_volume / average_volume) if average_volume > 0 else 0.0:.2f}, "
            f"양봉={'통과' if bullish_candle else '차단'}, RSI={'통과' if momentum_rsi_passed else '차단'}, "
            f"1H MTF={'통과' if momentum_mtf_allowed else '차단'}"
        )
    if normalized_entry_type == "MOMENTUM_BREAKOUT":
        # 반등형의 저점 근접 조건은 적용하지 않되, 급락·상위 추세·이격·윗꼬리 안전 게이트는 유지한다.
        entry_alpha_threshold = get_momentum_breakout_alpha_threshold(btc_regime, night_active)
        momentum_safety_passed = hard_gate_btc and momentum_mtf_allowed and hard_gate_disparity and hard_gate_shadow
        allowed = momentum_safety_passed and momentum_breakout_passed and total_score >= entry_alpha_threshold
    else:
        # 알파 점수는 후보 품질 확인용이며, 저점권 반등 하드 게이트를 우회할 수 없다.
        # 심야 기준도 점수 계산과 같은 단일 함수에서 가져와 주문 경로 불일치를 막는다.
        entry_alpha_threshold = get_alpha_buy_threshold(btc_regime, night_active)
        allowed = hard_gates_passed and signal_5m and total_score >= entry_alpha_threshold

    # 4. [과제 A] StrategyPolicy SSOT 기반 동적 손익비 산출
    atr_data = calculate_atr(candles, period=14)
    volatility = atr_data["atr"]
    atr_pct = atr_data["atr_pct"]

    is_major = is_major_market(market)
    min_tgt_pct = StrategyPolicy.MAJOR_MIN_TARGET_PCT if is_major else StrategyPolicy.MIN_TARGET_PCT
    min_stp_pct = StrategyPolicy.MAJOR_MIN_STOP_PCT if is_major else StrategyPolicy.MIN_STOP_PCT

    target_offset = max(current * min_tgt_pct, volatility * StrategyPolicy.ATR_TARGET_MULTIPLIER)
    target_price = current + target_offset

    stop_offset = max(current * min_stp_pct, volatility * StrategyPolicy.ATR_STOP_MULTIPLIER)
    stop_loss = current - stop_offset

    checklist_details = {
        "alpha_score": alpha_res["total_score"],
        "alpha_threshold": entry_alpha_threshold,
        "is_night": night_active,
        "entry_type": normalized_entry_type,
        "momentum_breakout": {
            "pass": momentum_breakout_passed,
            "detail": momentum_breakout_reason,
        },
        "factor_breakdown": alpha_res["factor_breakdown"],
        "hard_gates": {
            "all_passed": hard_gates_passed,
            "btc_regime": {"pass": hard_gate_btc, "regime": btc_regime},
            "mtf_trend": {"pass": hard_gate_mtf, "detail": mtf_reason},
            "rsi_guard": {"pass": hard_gate_rsi, "value": rsi, "min": rsi_hard_min, "max": rsi_hard_max},
            "bb_guard": {"pass": hard_gate_bb, "value": round(pct_b, 3), "min": StrategyPolicy.PCT_B_MIN, "max": StrategyPolicy.PCT_B_MAX},
            "ma_alignment": {"pass": hard_gate_ma, "ma5": round(ma5, 2), "ma20": round(ma20, 2)},
            "disparity_guard": {"pass": hard_gate_disparity, "current": round(current, 2), "limit": round(ma20 * StrategyPolicy.MAX_MA20_DISPARITY, 2)},
            "shadow_guard": {"pass": hard_gate_shadow, "ratio": round(upper_shadow_ratio, 3), "max": StrategyPolicy.MAX_UPPER_SHADOW_RATIO},
            "pullback_zone": {"pass": pullback_zone, "value": round(pct_b, 3), "min": pct_b_min, "max": pct_b_max},
            "near_recent_low": {"pass": near_recent_low, "recent_low": round(recent_low, 2), "distance_pct": round(distance_from_recent_low * 100, 2), "max_distance_pct": round(pullback_max_distance * 100, 2)},
            "rebound_confirmation": {"pass": rebound_confirmed, "current": round(current, 2), "previous_close": round(previous_close, 2), "open": round(open_0, 2)},
        },
        "ma_alignment": {"pass": hard_gate_ma, "ma5": round(ma5, 2), "ma20": round(ma20, 2)},
        "rsi_range": {"pass": (rsi_min <= rsi <= rsi_max), "value": rsi, "min": rsi_min, "max": rsi_max},
        "bollinger_pct_b": {"pass": (pct_b_min <= pct_b <= pct_b_max), "value": round(pct_b, 3), "min": pct_b_min, "max": pct_b_max},
        "mtf_1h_trend": {"pass": mtf_allowed, "detail": mtf_reason},
        "momentum_breakout": {
            "pass": momentum_breakout_passed,
            "detail": momentum_breakout_reason,
            "mtf_pass": momentum_mtf_allowed,
            "mtf_detail": momentum_mtf_reason,
        },
        "btc_regime": {"pass": regime_upper not in ("CRASH", "BEAR_VOLATILE"), "regime": btc_regime},
    }

    reasons = [
        f"하드게이트 {'통과' if hard_gates_passed else '차단'}",
        f"알파스코어 {alpha_res['total_score']}점",
        f"MA5 {'>' if ma5 > ma20 else '<='} MA20",
        f"RSI {rsi:.1f}",
        f"%B {pct_b:.2f}",
        f"저점거리 {distance_from_recent_low * 100:.2f}%",
        f"최근고점대비 {distance_below_recent_high * 100:.2f}%",
        f"반등확인 {'통과' if rebound_confirmed else '차단'}",
        f"이격 {'안정' if hard_gate_disparity else '과열차단'}",
        mtf_reason,
    ]
    if normalized_entry_type == "MOMENTUM_BREAKOUT":
        reasons.append(f"모멘텀 돌파 {momentum_breakout_reason}")

    return {
        "allow_buy": allowed,
        "reason": ", ".join(reasons),
        "entry_price": current,
        "target_price": round(target_price, 2),
        "stop_loss": round(stop_loss, 2),
        "atr": volatility,
        "atr_pct": atr_pct,
        "rsi": rsi,
        "pct_b": pct_b,
        "alpha_score": alpha_res["total_score"],
        "entry_type": normalized_entry_type,
        "momentum_breakout_passed": momentum_breakout_passed,
        "momentum_breakout": {
            "pass": momentum_breakout_passed,
            "detail": momentum_breakout_reason,
        },
        # 주문 원장에 그대로 보관할 수 있는 진입 시점의 결정론적 지표 스냅샷이다.
        "strategy_snapshot": {
            "entry_btc_regime": btc_regime,
            "entry_type": normalized_entry_type,
            "momentum_breakout": {
                "pass": momentum_breakout_passed,
                "detail": momentum_breakout_reason,
            },
            "alpha_score": alpha_res["total_score"],
            "factor_breakdown": dict(alpha_res["factor_breakdown"]),
            "indicators": {
                "rsi": rsi,
                "pct_b": pct_b,
                "recent_low": recent_low,
                "recent_high": recent_high,
                "distance_from_recent_low_pct": distance_from_recent_low * 100,
                "distance_below_recent_high_pct": distance_below_recent_high * 100,
                "pullback_zone": pullback_zone,
                "rebound_confirmed": rebound_confirmed,
                "atr": volatility,
                "atr_pct": atr_pct,
                "mtf_state": mtf_reason,
                "orderbook_score": alpha_res["factor_breakdown"].get("orderflow_score", 10),
                "orderbook_raw_ratio": alpha_res["factor_breakdown"].get("orderbook_raw_ratio", 1.0),
                "orderbook_smoothed_ratio": alpha_res["factor_breakdown"].get("orderbook_smoothed_ratio", 1.0),
                "orderbook_sample_count": alpha_res["factor_breakdown"].get("orderbook_sample_count", 0),
            },
            "entry_reason": ", ".join(reasons),
            "target_price": round(target_price, 2),
            "stop_loss": round(stop_loss, 2),
        },
        "factor_breakdown": alpha_res["factor_breakdown"],
        "checklist": checklist_details,
        "risk_reward_ratio": round(target_offset / stop_offset, 2) if stop_offset > 0 else 1.5,
        "checklist_details": checklist_details,
    }


def recovery_rebound_signal(
    candles: list[dict[str, Any]],
    candles_1h: list[dict[str, Any]] | None,
    btc_regime: str,
    orderbook: dict[str, Any] | None,
    market: str,
    exchange: str,
    relative_strength: float,
    candidate_trade_value: float,
    is_night: bool | None = None,
) -> dict[str, Any]:
    """급락 뒤 반등 후보를 위한 별도 진입 신호를 계산한다.

    일반 RISK_OFF 신호의 기준을 완화하지 않는다. 일반 신호가 상위 시간봉 EMA20을
    엄격히 요구해 반등 초입을 놓친 경우에만, 충분한 독자 강도와 확정봉 반등을 갖춘
    후보를 축소 비중으로 승인하기 위한 보수적 보조 경로다.
    """
    base = entry_signal(
        candles=candles,
        candles_1h=candles_1h,
        btc_regime=btc_regime,
        orderbook=orderbook,
        market=market,
        exchange=exchange,
        is_night=is_night,
    )
    checklist = dict(base.get("checklist_details", {}))
    hard_gates = dict(checklist.get("hard_gates", {}))
    regime_upper = str(btc_regime).upper()

    # 반등 경로는 CRASH/데이터 부족을 절대로 우회하지 않는다.
    if not StrategyPolicy.RECOVERY_REBOUND_ENABLED:
        return {**base, "allow_buy": False, "policy_mode": "RECOVERY_REBOUND", "reason": "반등 전용 정책 비활성"}
    if regime_upper != "RISK_OFF":
        return {**base, "allow_buy": False, "policy_mode": "RECOVERY_REBOUND", "reason": f"반등 전용 정책 대상 아님 (레짐: {btc_regime})"}
    if not candles_1h or len(candles_1h) < 20:
        return {**base, "allow_buy": False, "policy_mode": "RECOVERY_REBOUND", "reason": "1시간봉 데이터 부족으로 반등 진입 차단"}

    prices_1h = [float(c.get("trade_price", 0.0) or 0.0) for c in candles_1h]
    current_1h = prices_1h[0]
    ema20_1h = calculate_ema(prices_1h, 20)
    mtf_recovery_pass = current_1h >= ema20_1h * StrategyPolicy.RECOVERY_REBOUND_MTF_EMA20_RATIO

    # 기존 하드게이트 중 MTF만 반등용 허용 폭으로 대체한다. 나머지 안전 조건은 동일하다.
    core_gate_names = ("btc_regime", "rsi_guard", "bb_guard", "ma_alignment", "disparity_guard", "shadow_guard")
    core_gates_passed = all(bool(hard_gates.get(name, {}).get("pass", False)) for name in core_gate_names)
    pullback_passed = all(bool(hard_gates.get(name, {}).get("pass", False)) for name in ("pullback_zone", "near_recent_low", "rebound_confirmation"))
    alpha_score = int(base.get("alpha_score", 0) or 0)
    rs_passed = float(relative_strength) >= StrategyPolicy.RECOVERY_REBOUND_RS_MIN
    liquidity_passed = float(candidate_trade_value) >= StrategyPolicy.MIN_TRADE_VALUE_RISK_OFF

    # 반등 전용의 기본 엄선 기준과 현 세션의 일반 알파 기준 중 더 높은 값을 적용한다.
    # 이로써 심야 RISK_OFF에서 75점 반등 신호가 80점 일반 심야 기준을 우회하지 못한다.
    alpha_threshold = max(
        StrategyPolicy.RECOVERY_REBOUND_ALPHA_THRESHOLD,
        get_alpha_buy_threshold(btc_regime, is_night),
    )
    allow_buy = core_gates_passed and pullback_passed and mtf_recovery_pass and rs_passed and liquidity_passed and alpha_score >= alpha_threshold
    reasons = [
        f"반등 전용 {'승인' if allow_buy else '차단'}",
        f"알파 {alpha_score}점(기준 {alpha_threshold}점)",
        f"RS {relative_strength * 100:+.2f}%",
        f"1H {current_1h:.2f} {'>=' if mtf_recovery_pass else '<'} EMA20 {ema20_1h:.2f} × {StrategyPolicy.RECOVERY_REBOUND_MTF_EMA20_RATIO:.3f}",
        f"기존 핵심게이트 {'통과' if core_gates_passed else '차단'}",
        f"확정봉 반등 {'통과' if pullback_passed else '차단'}",
    ]
    snapshot = dict(base.get("strategy_snapshot", {}))
    snapshot.update({
        "entry_type": "RECOVERY_REBOUND",
        "recovery_rebound": {
            "allow_buy": allow_buy,
            "relative_strength": float(relative_strength),
            "candidate_trade_value": float(candidate_trade_value),
            "mtf_recovery_pass": mtf_recovery_pass,
            "alpha_threshold": alpha_threshold,
        },
    })
    return {
        **base,
        "allow_buy": allow_buy,
        "policy_mode": "RECOVERY_REBOUND",
        "reason": ", ".join(reasons),
        "strategy_snapshot": snapshot,
        "recovery_checklist": {
            "core_gates_passed": core_gates_passed,
            "pullback_passed": pullback_passed,
            "mtf_recovery_pass": mtf_recovery_pass,
            "rs_passed": rs_passed,
            "liquidity_passed": liquidity_passed,
            "alpha_passed": alpha_score >= alpha_threshold,
        },
    }
