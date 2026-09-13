import math
import os
import threading
from datetime import datetime, timezone, timedelta
from typing import Any, Literal

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

    # 1-1. 메이저 코인(BTC/ETH/SOL) 전용 목표가/익절/타임스탑 (낮은 변동성 적응 및 자금 잠김 방어)
    MAJOR_MIN_TARGET_PCT: float = 0.015          # 메이저 최소 목표 수익률 +1.5%
    MAJOR_MIN_STOP_PCT: float = 0.012            # 메이저 기본 최소 손절선 -1.2%
    MAJOR_PARTIAL_TP_1_PCT: float = 0.018        # 메이저 1차 분할 익절 +1.8% (도달 시 30% 익절)
    MAJOR_PARTIAL_TP_2_PCT: float = 0.035        # 메이저 2차 분할 익절 +3.5% (도달 시 30% 추가익절)
    MAJOR_TRAILING_START_PCT: float = 0.015      # 메이저 +1.5% 트레일링 스탑 활성화
    MAJOR_TRAILING_DROP_PCT: float = 0.010       # 메이저 최고점 대비 1.0% 하락 시 시장가 청산
    MAJOR_TIME_STOP_SECONDS_NORMAL: int = 7200   # 메이저 정상장 120분 타임스탑
    MAJOR_TIME_STOP_SECONDS_RISK_OFF: int = 3600 # 메이저 약세장 60분 타임스탑

    # 1-2. 대세 상승장(BULL_TREND) 전용 파라미터 (정상화: 휩소 과다 손실 차단 및 선제 익절)
    BULL_STOP_LOSS_PCT: float = 0.020            # 상승장 손절 -2.0% (과다 손절 방어, 추세 강세선 보호)
    BULL_PARTIAL_TP_1_PCT: float = 0.030         # 상승장 1차 분할 익절 +3.0% (선제 수익 실현 후 Break-Even 락인)
    BULL_PARTIAL_TP_2_PCT: float = 0.060         # 상승장 2차 분할 익절 +6.0% (현실적 2차 목표)
    BULL_TRAILING_START_PCT: float = 0.030       # +3.0% 도달 시 트레일링 스탑 개시
    BULL_TRAILING_DROP_PCT: float = 0.015        # 최고점 대비 1.5% 하락 시 청산 (상승장 고점 반락 선제 익절)
    BULL_TIME_STOP_SECONDS: int = 7200           # 상승장 120분 (2시간) 타임스탑 (자금 잠김 방어)
    BULL_TIME_STOP_MAX_HOLD_SECONDS: int = 10800 # 지지선 유지 시 최대 180분 (3시간) 홀딩 유예
    ALPHA_BUY_THRESHOLD_BULL: int = 65           # 상승장 알파 승인 점수 (65점으로 엄선하여 고점 상투 차단)
    ALPHA_BUY_THRESHOLD_NIGHT_BULL: int = 70     # 상승장 심야 알파 승인 점수 (70점)
    MOMENTUM_BREAKOUT_ALPHA_THRESHOLD_BULL: int = 60       # 상승장 모멘텀 돌파 알파 (60점)
    MOMENTUM_BREAKOUT_ALPHA_THRESHOLD_NIGHT_BULL: int = 65 # 상승장 심야 모멘텀 돌파 알파 (65점)

    # 1-3. 중기/추세추종 스윙(SWING) 전용 파라미터 (Dual-Track)
    SWING_STOP_LOSS_PCT: float = 0.055           # 스윙 기본 손절 -5.5% (잔파동 노이즈 방어)
    SWING_HARD_STOP_PCT: float = 0.080           # 스윙 절대 하드스탑 -8.0% (비상 탈출)
    SWING_PARTIAL_TP_1_PCT: float = 0.080        # 스윙 1차 익절 +8.0% (수량 40% 실현)
    SWING_PARTIAL_TP_1_RATIO: float = 0.40       # 스윙 1차 익절 비중 40%
    SWING_PARTIAL_TP_2_PCT: float = 0.150        # 스윙 2차 익절 +15.0% (수량 30% 실현)
    SWING_PARTIAL_TP_2_RATIO: float = 0.30       # 스윙 2차 익절 비중 30%
    SWING_TRAILING_START_PCT: float = 0.080      # +8.0% 도달 시 트레일링 스탑 활성화
    SWING_TRAILING_DROP_PCT: float = 0.040       # 최고점 대비 4.0% 하락 시 시장가 청산 (눌림목 허용)
    SWING_BREAKEVEN_STOP_PCT: float = 0.015      # 1차 익절 완료 후 본전 보장 스탑 (+1.5% 안전 마진)
    SWING_TARGET_PCT: float = 0.150              # 스윙 기본 목표 수익률 +15.0%
    SWING_ALLOC_RATIO: float = 0.50              # 스윙 포지션 기본 배분 비중
    SWING_TIME_STOP_ENABLED: bool = False        # 스윙은 시간 기반 타임스탑 미적용 (추세 기반 청산)
    SWING_4H_DATA_UNAVAILABLE_MAX_HOLD_SECONDS: int = 43200  # 4H 대사 불가가 12시간 지속되면 무기한 보유 방지 보호 청산

    # 1-4. 신규 상장 단타(NEW_LISTING) 전용 파라미터 — 4H/1H 이력 부족 시 소액 단타 경로
    # 기본값은 후보 분석·감사만 수행하는 관찰 모드다. 실주문은 거래소별 명시 설정이 있어야 한다.
    NEW_LISTING_ENABLED: bool = True
    NEW_LISTING_ENFORCEMENT: bool = False
    NEW_LISTING_MIN_5M_COMPLETED: int = 5
    NEW_LISTING_MAX_AGE_HOURS: int = 72
    NEW_LISTING_MIN_TRADE_VALUE_24H: float = 2_000_000_000.0
    NEW_LISTING_MIN_TRADE_VALUE_24H_RISK_OFF: float = 3_000_000_000.0
    NEW_LISTING_MIN_CHANGE_RATE: float = 0.015
    NEW_LISTING_MAX_CHANGE_RATE: float = 0.080
    NEW_LISTING_MIN_CHANGE_RATE_RISK_OFF: float = 0.020
    NEW_LISTING_MAX_CHANGE_RATE_RISK_OFF: float = 0.060
    NEW_LISTING_MIN_RS: float = 0.010
    NEW_LISTING_MIN_RS_RISK_OFF: float = 0.015
    NEW_LISTING_ALPHA_THRESHOLD_NORMAL: int = 75
    NEW_LISTING_ALPHA_THRESHOLD_NIGHT: int = 80
    NEW_LISTING_ALPHA_THRESHOLD_RISK_OFF: int = 80
    NEW_LISTING_ALPHA_THRESHOLD_NIGHT_RISK_OFF: int = 85
    NEW_LISTING_ALLOC_RATIO: float = 0.15
    NEW_LISTING_STOP_LOSS_PCT: float = 0.025
    NEW_LISTING_HARD_STOP_PCT: float = 0.040
    NEW_LISTING_TARGET_PCT: float = 0.050
    NEW_LISTING_PARTIAL_TP_PCT: float = 0.030
    NEW_LISTING_PARTIAL_TP_RATIO: float = 0.50
    NEW_LISTING_TRAILING_START_PCT: float = 0.040
    NEW_LISTING_TRAILING_DROP_PCT: float = 0.020
    NEW_LISTING_TIME_STOP_SECONDS: int = 3600
    NEW_LISTING_EARLY_EXIT_SECONDS: int = 1800
    NEW_LISTING_EARLY_EXIT_MIN_PNL_PCT: float = -0.50  # 조기탈출 손익률 하한 (% 표시 단위)
    NEW_LISTING_EARLY_EXIT_MAX_PNL_PCT: float = 0.30   # 조기탈출 손익률 상한 (% 표시 단위)
    NEW_LISTING_MAX_PER_CYCLE: int = 1
    NEW_LISTING_MAX_OPEN_POSITIONS: int = 1
    NEW_LISTING_REENTRY_COOLDOWN_SEC: float = 3600.0
    NEW_LISTING_VOLUME_RATIO_MIN: float = 1.2
    NEW_LISTING_RSI_MIN: float = 50.0
    NEW_LISTING_RSI_MAX: float = 82.0
    NEW_LISTING_BREAKOUT_LOOKBACK_BARS: int = 3

    @classmethod
    def _get_new_listing_env_value(cls, setting: str, exchange: str | None = None) -> str:
        """거래소별 설정을 우선해 다른 거래소의 실주문 정책이 섞이지 않게 한다."""
        exchange_key = str(exchange or "").strip().upper()
        if exchange_key in {"BITHUMB", "UPBIT"}:
            exchange_value = os.getenv(f"{exchange_key}_{setting}", "").strip()
            if exchange_value:
                return exchange_value.lower()
        return os.getenv(setting, "").strip().lower()

    @classmethod
    def is_new_listing_enabled(cls, exchange: str | None = None) -> bool:
        """신규상장 후보 분석 활성 여부를 반환한다. 거래소별 설정이 공통 설정보다 우선한다."""
        env_val = cls._get_new_listing_env_value("NEW_LISTING_ENABLED", exchange)
        if env_val in ("true", "1", "yes", "y", "on", "enable", "enabled"):
            return True
        if env_val in ("false", "0", "no", "n", "off", "disable", "disabled"):
            return False
        return cls.NEW_LISTING_ENABLED

    @classmethod
    def is_new_listing_enforcement_enabled(cls, exchange: str | None = None) -> bool:
        """명시 설정 전에는 신규상장 진입을 관찰 모드로 유지하고 실주문을 차단한다."""
        env_val = cls._get_new_listing_env_value("NEW_LISTING_ENFORCEMENT", exchange)
        if env_val in ("true", "1", "yes", "y", "on", "enable", "enabled"):
            return True
        if env_val in ("false", "0", "no", "n", "off", "disable", "disabled"):
            return False
        return cls.NEW_LISTING_ENFORCEMENT

    @classmethod
    def get_new_listing_max_age_hours(cls) -> int:
        raw = os.getenv("NEW_LISTING_MAX_AGE_HOURS", "").strip()
        if raw:
            try:
                val = int(raw)
                if val > 0:
                    return val
            except ValueError:
                pass
        return cls.NEW_LISTING_MAX_AGE_HOURS

    @classmethod
    def get_new_listing_min_trade_value_24h(cls, btc_regime: str = "NORMAL") -> float:
        regime_upper = str(btc_regime or "NORMAL").upper()
        env_key = "NEW_LISTING_MIN_TRADE_VALUE_24H"
        raw = os.getenv(env_key, "").strip()
        if raw:
            try:
                val = float(raw)
                if val > 0:
                    return val
            except ValueError:
                pass
        if regime_upper == "RISK_OFF":
            return cls.NEW_LISTING_MIN_TRADE_VALUE_24H_RISK_OFF
        return cls.NEW_LISTING_MIN_TRADE_VALUE_24H

    # 2. 익절 및 트레일링 스탑 (2~3단계 분할 익절 & 2차 러너 추세 추종)
    PARTIAL_TP_PCT: float = 0.035        # 기본 1차 익절 기준 +3.5%
    PARTIAL_TP_1_PCT: float = 0.035      # 1차 +3.5% 도달 시 분할 익절
    PARTIAL_TP_1_RATIO: float = 0.50     # 1차 익절 비중 (50% 선제 수익 실현하여 손익비 대폭 개선)
    PARTIAL_TP_2_PCT: float = 0.070      # 2차 +7.0% 도달 시 분할 익절
    PARTIAL_TP_2_RATIO: float = 0.25     # 2차 익절 비중 (원금의 25% = 잔여 50% 중 50%)
    BREAKEVEN_STOP_PCT: float = 0.003    # 1차 익절 완료 후 본전 보장 스탑 (+0.3% 수수료 보장)
    TRAILING_START_PCT: float = 0.030    # +3.0% 트레일링 스탑 활성화
    TRAILING_DROP_PCT: float = 0.020     # 최고점 대비 2.0% 하락 시 시장가 청산 (알트 숨고르기 허용)
    MIN_PROFIT_BUFFER_PCT: float = 0.005 # +0.5% 최소 보장 마진

    # 3. 시간 기반 청산 (타임스탑) & 15분 모멘텀 조기 탈출 & 쿨다운
    MOMENTUM_EARLY_EXIT_SECONDS: int = 2700 # 45분 모멘텀 소멸 조기 본전 탈출 (2700초로 유예 확대)
    MOMENTUM_EARLY_EXIT_BARS_5M: int = 9   # 5분봉 9개 캔들
    TIME_STOP_SECONDS: int = 7200        # 120분 타임스탑 (기본 정상장, 실거래 초 단위)
    TIME_STOP_SECONDS_NORMAL: int = 7200 # 정상장 120분 타임스탑
    TIME_STOP_SECONDS_RISK_OFF: int = 7200 # 알트코인 독립 매수: RISK_OFF 약세장에서도 정상장과 동일한 120분 타임스탑 유지
    TIME_STOP_MAX_HOLD_SECONDS: int = 10800 # 지지선 유지 시 최대 180분 반등 대기 유예
    TIME_STOP_BARS_5M: int = 24          # 5분봉 24개 = 120분 (백테스트 캔들 단위)
    TIME_STOP_BARS_5M_RISK_OFF: int = 24 # 알트코인 독립 매수: RISK_OFF 백테스트 캔들 단위 정상장(24봉) 일원화
    TIME_STOP_MAX_HOLD_BARS_5M: int = 36 # 최대 유예 36봉 (180분)
    TIME_STOP_BREAKEVEN_MIN_PNL_PCT: float = 0.003 # 타임스탑 실질 본전 기준 (+0.30% 완충 마진 확보)
    COOLDOWN_STOP_LOSS_SEC: float = 1800.0  # 손절 후 쿨다운 30분 (연속 손절 방어)
    COOLDOWN_TIME_STOP_SEC: float = 600.0   # 타임스탑 횡보 청산 후 쿨다운 10분
    COOLDOWN_TP_SEC: float = 300.0          # 트레일링 익절 후 쿨다운 5분 (2차 랠리 조기 참여)
    REENTRY_BUFFER_PCT: float = 0.012       # 직전 청산가 대비 최소 돌파/눌림목 갭 버퍼 (+1.2%)
    REENTRY_FILTER_EXPIRY_SEC: float = 2700.0  # 직전 청산가 갭 필터 유지 시간 (45분)

    # 4. 하드 안전 게이트 (Hard Safety Gates) & 상대 강도(RS) 임계값
    ALPHA_BUY_THRESHOLD: int = 60        # 7대 팩터 복합 알파 승인 점수 (100점 만점)
    ALPHA_BUY_THRESHOLD_NORMAL: int = 60 # 정상장 7대 팩터 복합 알파 승인 점수
    ALPHA_BUY_THRESHOLD_RISK_OFF: int = 60 # 알트코인 독립 매수: RISK_OFF 약세장에서도 정상장과 동일한 60점 기준 적용
    RS_MIN_RISK_OFF: float = 0.008       # RISK_OFF 시 BTC 대비 최소 상대 강도 (+0.8% 초과 상승)
    MIN_TRADE_VALUE_RISK_OFF: float = 1_000_000_000.0  # 약세장 최소 24시간 거래대금 10억 원 (기존 20억 -> 10억 하향)
    MIN_ASSET_PRICE_KRW: float = float(os.getenv("MIN_ASSET_PRICE_KRW", "0.0001"))  # 초저가 코인 제한 전면 해제 (기본 0.0001원, 0원 이하만 차단)
    RSI_MIN_NORMAL: float = 42.0         # 정상장 저점 반등 확인용 RSI 최소치
    RSI_MAX_NORMAL: float = 70.0         # 알트코인 독자 탄력 수용을 위해 RSI 상한을 70.0으로 현실화
    RSI_MIN_RISK_OFF: float = 42.0       # RISK_OFF 저점 반등 확인용 RSI 최소치
    RSI_MAX_RISK_OFF: float = 70.0       # RISK_OFF 고점 추격 방지용 RSI 최대치 (약세장 독자 수급 수용을 위해 70.0으로 현실화)
    PCT_B_MIN: float = 0.20              # 볼린저 밴드 %B 최소치
    PCT_B_MAX: float = 0.72              # NORMAL/BULL_TREND 상단권 모멘텀 추격을 차단하는 상한
    # RISK_OFF에서는 하드 안전 조건을 모두 만족한 반등의 0.73~0.80 구간만 추가 수용한다.
    # 이 값은 눌림목 상한과 동일하게 유지해 두 게이트 간 정책 불일치를 막는다.
    PCT_B_MAX_RISK_OFF: float = 0.80
    PULLBACK_PCT_B_MIN_NORMAL: float = 0.25  # 정상장 저점권 반등 후보 하한
    PULLBACK_PCT_B_MAX_NORMAL: float = 0.68  # 정상장 저점권 반등 후보 상한 (0.60 -> 0.68)
    PULLBACK_PCT_B_MIN_RISK_OFF: float = 0.28  # RISK_OFF 반등 후보 하한
    PULLBACK_PCT_B_MAX_RISK_OFF: float = 0.80  # RISK_OFF 반등 후보 상한: 하드 상한과 같게 유지
    PULLBACK_LOOKBACK_BARS: int = 12      # 최근 지지 저점 산정에 사용하는 5분봉 수
    PULLBACK_MAX_DISTANCE_NORMAL: float = 0.035  # 정상장 최근 저점 대비 최대 허용 거리
    PULLBACK_MAX_DISTANCE_RISK_OFF: float = 0.045  # RISK_OFF 최근 저점 대비 최대 허용 거리 (4.5%로 현실화)
    MAX_MA20_DISPARITY: float = 1.035    # MA20 대비 최대 이격도 +3.5% (기본 눌림목/반등형)
    MAX_MA20_DISPARITY_MOMENTUM: float = 1.050 # MA20 대비 모멘텀 돌파 최대 이격도 +5.0% (급등 돌파 캔들 수용)
    MAX_UPPER_SHADOW_RATIO: float = 0.50 # 캔들 윗꼬리 최대 허용 비율 (50%로 강화하여 피뢰침 차단)
    MA_ALIGNMENT_RATIO: float = 0.995    # MA5 >= MA20 * 0.995
    PULLBACK_MA_ALIGNMENT_RATIO: float = 0.990  # 저점 반등은 MA20 아래 1% 이내 회복까지 허용
    RISK_OFF_ALLOC_RATIO: float = 1.0    # 알트코인 독립 매수: BTC 약세 레짐이어도 알트코인 진입 비중 100% 정상 유지

    # 4-0. AI 단독 자율 승인 (AI Direct Entry) 기본 비활성화
    # 로컬 퀀트 관망(allow_buy=False) 상태에서 AI 단독 매수 진입 시 승률 20~30%로 저조하므로 기본 차단한다.
    # AI는 로컬 퀀트 1차 통과 종목의 2차 컨펌 및 보유 포지션 리스크 관리(탈출/목표가)에 집중한다.
    ENABLE_AI_DIRECT_ENTRY: bool = False

    @classmethod
    def is_ai_direct_entry_enabled(cls) -> bool:
        """환경 변수 또는 클래스 속성을 통해 AI 단독 진입 허용 여부를 안전하게 확인"""
        env_val = os.getenv("ENABLE_AI_DIRECT_ENTRY", "").strip().lower()
        if env_val in ("true", "1", "yes", "y", "enable", "enabled"):
            return True
        if env_val in ("false", "0", "no", "n", "disable", "disabled"):
            return False
        return cls.ENABLE_AI_DIRECT_ENTRY

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
    MOMENTUM_BREAKOUT_ALPHA_THRESHOLD_RISK_OFF: int = 55 # 알트코인 독립 매수: RISK_OFF 시에도 정상장과 동일한 55점 적용
    MOMENTUM_BREAKOUT_ALPHA_THRESHOLD_NIGHT: int = 65
    MOMENTUM_BREAKOUT_ALPHA_THRESHOLD_NIGHT_RISK_OFF: int = 65 # 알트코인 독립 매수: 심야 65점 일원화
    MOMENTUM_BREAKOUT_VOLUME_RATIO_MIN: float = 1.1
    MOMENTUM_BREAKOUT_LOOKBACK_BARS: int = 4
    MOMENTUM_BREAKOUT_RSI_MIN: float = 52.0
    MOMENTUM_BREAKOUT_RSI_MAX: float = 78.0
    MOMENTUM_BREAKOUT_RS_MIN: float = 0.008
    MOMENTUM_BREAKOUT_MTF_EMA20_RATIO: float = 0.980
    MOMENTUM_BREAKOUT_ALLOC_RATIO: float = 0.25
    # 확장 후반 추격은 수익 기회를 열되, 초입보다 작은 금액으로만 첫 주문을 허용한다.
    MOMENTUM_EXTENDED_ALPHA_THRESHOLD_RISK_OFF: int = 70
    MOMENTUM_EXTENDED_ALPHA_THRESHOLD_NIGHT_RISK_OFF: int = 75
    MOMENTUM_EXTENDED_ALLOC_RATIO: float = 0.15
    # 모멘텀은 초입에서만 첫 주문을 허용한다. 확장 구간은 관찰·보유 관리용으로 남긴다.
    MOMENTUM_EARLY_MAX_CHANGE_RATE: float = 0.060

    # RS 주도주(독자 강세 종목) 특례 정책
    RS_LEADER_MIN_RS: float = 0.030                 # BTC 대비 상대강도 +3.0% 이상
    RS_LEADER_EARLY_MAX_CHANGE_RATE: float = 0.120  # 주도주 모멘텀 초입(+12.0% 이하) 확장 허용
    RS_LEADER_BREAKOUT_TOLERANCE: float = 0.992     # 직전 고점 99.2% 이상 근접 지지 양봉 허용
    RS_LEADER_ALPHA_THRESHOLD_RISK_OFF: int = 55    # 알트코인 독립 매수: RISK_OFF 시 RS 주도주 임계값 55점 일원화

    @classmethod
    def get_momentum_early_max_change_rate(cls, relative_strength: float = 0.0) -> float:
        """환경 변수 또는 기본 상한 반환. RS 주도주(RS >= 3.0%)는 최대 12.0%까지 초입으로 인정"""
        raw = os.getenv("MOMENTUM_EARLY_MAX_CHANGE_RATE", "").strip()
        if raw:
            try:
                val = float(raw)
                if val > 0.0:
                    return val
            except ValueError:
                pass
        if relative_strength >= cls.RS_LEADER_MIN_RS:
            raw_leader = os.getenv("RS_LEADER_EARLY_MAX_CHANGE_RATE", "").strip()
            if raw_leader:
                try:
                    val_leader = float(raw_leader)
                    if val_leader > 0.0:
                        return val_leader
                except ValueError:
                    pass
            return cls.RS_LEADER_EARLY_MAX_CHANGE_RATE
        return cls.MOMENTUM_EARLY_MAX_CHANGE_RATE

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
    ALPHA_BUY_THRESHOLD_NIGHT_RISK_OFF: int = 75  # 심야 약세장 엄선 알파 승인 점수 (75점 유지)
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
        if regime_upper == "RISK_OFF":
            return StrategyPolicy.ALPHA_BUY_THRESHOLD_NIGHT_RISK_OFF
        elif regime_upper == "BULL_TREND":
            return StrategyPolicy.ALPHA_BUY_THRESHOLD_NIGHT_BULL
        return StrategyPolicy.ALPHA_BUY_THRESHOLD_NIGHT

    if regime_upper == "RISK_OFF":
        return StrategyPolicy.ALPHA_BUY_THRESHOLD_RISK_OFF
    elif regime_upper == "BULL_TREND":
        return StrategyPolicy.ALPHA_BUY_THRESHOLD_BULL
    return StrategyPolicy.ALPHA_BUY_THRESHOLD_NORMAL


def is_ai_direct_entry_eligible(
    alpha_score: Any,
    btc_regime: str = "NORMAL",
    is_night: bool | None = None,
) -> bool:
    """AI 단독 진입은 누락 없는 알파 점수와 공통 세션 정책을 모두 충족할 때만 허용한다."""
    try:
        normalized_score = int(alpha_score)
    except (TypeError, ValueError):
        # 점수 누락·형식 오류는 AI 단독 매수의 근거가 될 수 없으므로 fail-closed 처리한다.
        return False
    return normalized_score >= get_alpha_buy_threshold(btc_regime, is_night)


def get_new_listing_alpha_threshold(btc_regime: str = "NORMAL", is_night: bool | None = None) -> int:
    """신규 상장 단타 전용 알파 기준을 레짐·세션별로 반환한다."""
    regime_upper = str(btc_regime or "NORMAL").upper()
    night_active = is_night if is_night is not None else is_night_session()
    if night_active:
        if regime_upper == "RISK_OFF":
            return StrategyPolicy.NEW_LISTING_ALPHA_THRESHOLD_NIGHT_RISK_OFF
        return StrategyPolicy.NEW_LISTING_ALPHA_THRESHOLD_NIGHT
    if regime_upper == "RISK_OFF":
        return StrategyPolicy.NEW_LISTING_ALPHA_THRESHOLD_RISK_OFF
    return StrategyPolicy.NEW_LISTING_ALPHA_THRESHOLD_NORMAL


def is_rs_leader(relative_strength: float = 0.0, btc_regime: str = "NORMAL") -> bool:
    """비트코인 대비 상대강도(RS)가 높고 CRASH가 아닌 독자 강세 주도주인지 판정한다."""
    regime_upper = str(btc_regime or "NORMAL").upper()
    if regime_upper in ("CRASH", "BEAR_VOLATILE"):
        return False
    return relative_strength >= StrategyPolicy.RS_LEADER_MIN_RS


def get_momentum_breakout_alpha_threshold(
    btc_regime: str = "NORMAL",
    is_night: bool | None = None,
    relative_strength: float = 0.0,
) -> int:
    """확정봉 모멘텀 돌파 전용 알파 기준을 반환한다.
    알트코인 독립 매수: 비트코인 급락(CRASH)이 아닌 이상, BTC 레짐(RISK_OFF 등)에 영향없이
    정상장 기준(주간 55점, 심야 65점)을 일관 적용한다.
    """
    regime_upper = str(btc_regime or "NORMAL").upper()
    night_active = is_night if is_night is not None else is_night_session()

    if night_active:
        if regime_upper == "BULL_TREND":
            return StrategyPolicy.MOMENTUM_BREAKOUT_ALPHA_THRESHOLD_NIGHT_BULL
        return StrategyPolicy.MOMENTUM_BREAKOUT_ALPHA_THRESHOLD_NIGHT

    if regime_upper == "BULL_TREND":
        return StrategyPolicy.MOMENTUM_BREAKOUT_ALPHA_THRESHOLD_BULL
    return StrategyPolicy.MOMENTUM_BREAKOUT_ALPHA_THRESHOLD_NORMAL


def get_momentum_extended_alpha_threshold(
    btc_regime: str = "NORMAL",
    is_night: bool | None = None,
    relative_strength: float = 0.0,
) -> int:
    """확장 후반 추격 진입의 AI 알파 기준을 반환한다."""
    regime_upper = str(btc_regime or "NORMAL").upper()
    night_active = is_night if is_night is not None else is_night_session()

    if regime_upper == "RISK_OFF":
        return (
            StrategyPolicy.MOMENTUM_EXTENDED_ALPHA_THRESHOLD_NIGHT_RISK_OFF
            if night_active
            else StrategyPolicy.MOMENTUM_EXTENDED_ALPHA_THRESHOLD_RISK_OFF
        )

    return 75 if is_rs_leader(relative_strength, regime_upper) else 80


def get_time_stop_bars_5m(btc_regime: str = "NORMAL", is_night: bool | None = None) -> tuple[int, int]:
    """백테스트용 레짐·심야별 타임스탑 봉 수 (profit_bars, max_hold_bars) 반환."""
    regime_upper = str(btc_regime or "NORMAL").upper()
    night_active = is_night if is_night is not None else is_night_session()

    if night_active:
        profit_bars = max(1, int(StrategyPolicy.NIGHT_TIME_STOP_SECONDS / 300))
    elif regime_upper == "BULL_TREND":
        profit_bars = max(1, int(StrategyPolicy.BULL_TIME_STOP_SECONDS / 300))
    else:
        profit_bars = StrategyPolicy.TIME_STOP_BARS_5M

    max_hold_bars = StrategyPolicy.TIME_STOP_MAX_HOLD_BARS_5M
    if regime_upper == "BULL_TREND":
        max_hold_bars = max(1, int(StrategyPolicy.BULL_TIME_STOP_MAX_HOLD_SECONDS / 300))
    return profit_bars, max_hold_bars


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


MAJOR_MARKETS = {"KRW-BTC", "BTC", "KRW-ETH", "ETH", "KRW-SOL", "SOL"}


def is_major_market(market: str) -> bool:
    """시가총액 상위 대형 메이저 코인(BTC, ETH, SOL) 여부 판별"""
    if not market:
        return False
    m = str(market).strip().upper()
    return m in MAJOR_MARKETS or m.replace("KRW-", "") in {"BTC", "ETH", "SOL"}


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
    # 최신 period + 1개만 슬라이스하여 불필요한 전체 배열 복사 및 메모리 할당 방지 (O(period))
    subset = prices[:period + 1]
    sum_gains = 0.0
    sum_losses = 0.0
    for i in range(period):
        change = subset[i] - subset[i + 1]  # 최신값 - 직전값
        if change > 0:
            sum_gains += change
        elif change < 0:
            sum_losses -= change

    avg_loss = sum_losses / period
    if avg_loss == 0:
        return 50.0 if sum_gains == 0 else 100.0
    rs = (sum_gains / period) / avg_loss
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
    """Classify BTC market regime into BULL_TREND, NORMAL, RISK_OFF, or CRASH.

    - CRASH: Recent 5m/15m drop >= crash_threshold_pct (1.5%) -> Stop all new buys
    - RISK_OFF: 1H Close < 1H EMA50 or 1H drop >= 1.0% -> Stricter gates & 50% sizing
    - BULL_TREND: 1H Close >= EMA20 >= EMA50 (정배열) & 상승세 유지 -> 휩소 방어 확장 & 대세 추세 추종
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
        ema20_1h = calculate_ema(prices_1h, min(len(prices_1h), 20))
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

        # BULL_TREND: 1H 종가가 EMA20 및 EMA50 상단에 위치하고 정배열이며 최근 조정이 없거나 상승세
        lookback_12 = min(len(prices_1h) - 1, 12)
        p_1h_12 = prices_1h[lookback_12]
        gain_12h = (cur_1h - p_1h_12) / p_1h_12 if p_1h_12 > 0 else 0.0
        if cur_1h >= ema20_1h and ema20_1h >= ema50_1h and (drop_1h > -0.003 or gain_12h > 0.005):
            return {
                "regime": "BULL_TREND",
                "drop_pct": round(recent_drop * 100.0, 2),
                "gain_12h_pct": round(gain_12h * 100.0, 2),
                "reason": f"BTC 강력 상승 추세 (1H EMA20/50 정배열, 12H {gain_12h*100.0:+.2f}%)",
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
        low_p = float(c.get("low_price", 0.0))
        close_p = float(c.get("trade_price", 0.0))
        vol = float(c.get("candle_acc_trade_volume", 0.0))
        typical_p = (h + low_p + close_p) / 3.0 if (h > 0 and low_p > 0 and close_p > 0) else close_p
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
    ema20_1h = 0.0
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
        "mtf_reason": mtf_reason,
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
        "bb": bb,
        "ema20_1h": ema20_1h,
        "reason": f"알파 스코어 {total_score}/100점 ({'🟢 승인' if allow_buy else '⚪ 미달'}{night_tag}, 기준 {buy_threshold}점) | MTF:{score_mtf} VWAP:{score_vwap} MACD:{score_macd} RSI:{score_rsi} BB:{score_bb}",
    }


def calculate_new_listing_alpha_score(
    candles: list[dict[str, Any]],
    orderbook: dict[str, Any] | None = None,
    relative_strength: float = 0.0,
) -> dict[str, Any]:
    """신규상장 단타용 축소 알파 점수(5분봉 5개 이상)를 계산한다."""
    min_count = StrategyPolicy.NEW_LISTING_MIN_5M_COMPLETED
    if not candles or len(candles) < min_count:
        return {"total_score": 0, "factor_breakdown": {}, "reason": "신규상장 5분봉 부족"}

    prices = [float(c.get("trade_price", 0.0)) for c in candles]
    current = prices[0]
    rsi = calculate_rsi(prices)
    vols = [float(c.get("candle_acc_trade_volume", 0.0) or 0.0) for c in candles]
    average_volume = (sum(vols[1:]) / len(vols[1:])) if len(vols) > 1 else 0.0
    current_volume = vols[0] if vols else 0.0
    volume_ratio = (current_volume / average_volume) if average_volume > 0 else 0.0

    score_volume = 25 if volume_ratio >= StrategyPolicy.NEW_LISTING_VOLUME_RATIO_MIN else 8
    score_rsi = (
        25
        if StrategyPolicy.NEW_LISTING_RSI_MIN <= rsi <= StrategyPolicy.NEW_LISTING_RSI_MAX
        else 5
    )
    score_rs = min(25, max(0, int(relative_strength * 1000)))
    score_orderflow = 10
    if orderbook:
        total_ask = float(orderbook.get("total_ask_size", 1.0) or 1.0)
        total_bid = float(orderbook.get("total_bid_size", 1.0) or 1.0)
        bid_ask_ratio = total_bid / total_ask if total_ask > 0 else 1.0
        if bid_ask_ratio >= 1.2:
            score_orderflow = 20
        elif bid_ask_ratio < 0.7:
            score_orderflow = 3

    total_score = score_volume + score_rsi + score_rs + score_orderflow
    return {
        "total_score": total_score,
        "factor_breakdown": {
            "volume_score": score_volume,
            "rsi_score": score_rsi,
            "rs_score": score_rs,
            "orderflow_score": score_orderflow,
            "volume_ratio": round(volume_ratio, 3),
            "rsi": rsi,
            "relative_strength": round(relative_strength, 4),
        },
        "reason": (
            f"신규상장 알파 {total_score}점 "
            f"(거래량 {volume_ratio:.2f}x, RSI {rsi:.1f}, RS {relative_strength * 100:.2f}%)"
        ),
    }


def new_listing_entry_signal(
    candles: list[dict[str, Any]],
    btc_regime: str = "NORMAL",
    orderbook: dict[str, Any] | None = None,
    market: str = "",
    relative_strength: float = 0.0,
    is_night: bool | None = None,
) -> dict[str, Any]:
    """4H/1H 이력이 부족한 신규상장 종목 전용 5분봉 돌파 진입 신호."""
    min_count = StrategyPolicy.NEW_LISTING_MIN_5M_COMPLETED
    if len(candles) < min_count:
        return {"allow_buy": False, "reason": "신규상장 5분 확정봉 부족"}

    current = float(candles[0].get("trade_price", 0.0) or 0.0)
    if current < StrategyPolicy.MIN_ASSET_PRICE_KRW:
        return {
            "allow_buy": False,
            "reason": f"유효하지 않은 가격 차단 (현재가 {current:,.4f}원)",
        }

    regime_upper = str(btc_regime or "NORMAL").upper()
    if regime_upper in ("CRASH", "BEAR_VOLATILE"):
        return {"allow_buy": False, "reason": f"신규상장 진입 불가 BTC 레짐({btc_regime})"}

    night_active = is_night if is_night is not None else is_night_session()
    alpha_res = calculate_new_listing_alpha_score(
        candles=candles,
        orderbook=orderbook,
        relative_strength=relative_strength,
    )
    total_score = int(alpha_res.get("total_score", 0) or 0)
    alpha_threshold = get_new_listing_alpha_threshold(btc_regime, night_active)

    prices = [float(c.get("trade_price", 0.0)) for c in candles]
    rsi = calculate_rsi(prices)
    lookback = StrategyPolicy.NEW_LISTING_BREAKOUT_LOOKBACK_BARS
    previous_candles = candles[1:lookback + 1]
    previous_high = max(
        (float(c.get("high_price", c.get("trade_price", 0.0)) or 0.0) for c in previous_candles),
        default=0.0,
    )
    vols = [float(c.get("candle_acc_trade_volume", 0.0) or 0.0) for c in candles]
    average_volume = (sum(vols[1:]) / len(vols[1:])) if len(vols) > 1 else 0.0
    current_volume = vols[0] if vols else 0.0
    current_open = float(candles[0].get("opening_price", current) or current)
    volume_confirmed = (
        average_volume > 0
        and current_volume >= average_volume * StrategyPolicy.NEW_LISTING_VOLUME_RATIO_MIN
    )
    price_breakout = previous_high > 0 and current > previous_high
    bullish_candle = current >= current_open
    rsi_passed = StrategyPolicy.NEW_LISTING_RSI_MIN <= rsi <= StrategyPolicy.NEW_LISTING_RSI_MAX
    rs_passed = relative_strength >= StrategyPolicy.NEW_LISTING_MIN_RS

    breakout_passed = price_breakout and volume_confirmed and bullish_candle and rsi_passed and rs_passed
    allowed = breakout_passed and total_score >= alpha_threshold
    breakout_reason = (
        f"직전 {lookback}봉 고점 돌파={'통과' if price_breakout else '차단'}, "
        f"거래량배수={(current_volume / average_volume) if average_volume > 0 else 0.0:.2f}, "
        f"양봉={'통과' if bullish_candle else '차단'}, "
        f"RSI={'통과' if rsi_passed else '차단'}, "
        f"RS={'통과' if rs_passed else '차단'}"
    )
    target_price = round(current * (1.0 + StrategyPolicy.NEW_LISTING_TARGET_PCT), 4 if current < 1.0 else 2)
    stop_loss = round(current * (1.0 - StrategyPolicy.NEW_LISTING_STOP_LOSS_PCT), 4 if current < 1.0 else 2)
    reason = (
        f"신규상장 5분 돌파 {breakout_reason}, "
        f"알파 {total_score}/{alpha_threshold}점"
    )
    return {
        "allow_buy": allowed,
        "reason": reason,
        "entry_price": current,
        "target_price": target_price,
        "stop_loss": stop_loss,
        "atr": 0.0,
        "atr_pct": 0.0,
        "rsi": rsi,
        "alpha_score": total_score,
        "entry_type": "NEW_LISTING",
        "factor_breakdown": alpha_res.get("factor_breakdown", {}),
        "checklist_details": {
            "alpha_score": total_score,
            "alpha_threshold": alpha_threshold,
            "entry_type": "NEW_LISTING",
            "new_listing_breakout": {"pass": breakout_passed, "detail": breakout_reason},
            "factor_breakdown": alpha_res.get("factor_breakdown", {}),
        },
        "strategy_snapshot": {
            "entry_btc_regime": btc_regime,
            "entry_type": "NEW_LISTING",
            "alpha_score": total_score,
            "factor_breakdown": dict(alpha_res.get("factor_breakdown", {})),
            "entry_reason": reason,
            "target_price": target_price,
            "stop_loss": stop_loss,
        },
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
    relative_strength: float = 0.0,
) -> dict[str, Any]:
    """
    결정론적 퀀트 진입 신호 생성기 (Deterministic Entry Engine)
    - StrategyPolicy 단일 진실 공급원(SSOT) 100% 참조
    - 하드 안전 게이트(Hard Safety Gates): 극초과열, 볼린저 이탈, 역배열 차단 (알파 점수로 우회 불가)
    - 7대 팩터 복합 알파 소프트 스코어 결합
    - ATR 기반 동적 목표가/손절가 일원화 산출
    """
    if (entry_type or "CONFIRMED").upper() == "NEW_LISTING":
        return new_listing_entry_signal(
            candles=candles,
            btc_regime=btc_regime,
            orderbook=orderbook,
            market=market,
            relative_strength=relative_strength,
            is_night=is_night,
        )

    if len(candles) < 25:
        return {"allow_buy": False, "reason": "캔들 데이터 부족"}

    prices = [float(c.get("trade_price", 0.0)) for c in candles]
    cur_check = prices[0] if prices else 0.0
    if cur_check < StrategyPolicy.MIN_ASSET_PRICE_KRW:
        return {
            "allow_buy": False,
            "reason": f"유효하지 않은 가격 차단 (현재가 {cur_check:,.4f}원 < {StrategyPolicy.MIN_ASSET_PRICE_KRW:,.4f}원)",
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

    current = cur_check
    ma5 = sum(prices[:5]) / 5.0
    bands = alpha_res.get("bb") or calculate_bollinger_bands(prices, period=20)
    ma20 = bands["middle"]
    pct_b = bands["pct_b"]
    rsi = float(alpha_res.get("rsi") if alpha_res.get("rsi") is not None else calculate_rsi(prices))

    # 1. 1시간봉 MTF 추세 필터
    mtf_allowed = True
    mtf_reason = "1H MTF 미제공"
    is_strong_rs_leader = (regime_upper == "RISK_OFF" and relative_strength >= 0.020)
    if candles_1h and len(candles_1h) >= 20:
        current_1h = float(candles_1h[0].get("trade_price", 0.0) or 0.0)
        cached_ema20 = float(alpha_res.get("ema20_1h") or 0.0)
        if cached_ema20 > 0:
            ema20_1h = cached_ema20
        else:
            prices_1h = [float(c.get("trade_price", 0.0)) for c in candles_1h]
            ema20_1h = calculate_ema(prices_1h, 20)
        # 알트코인 독립 매수: 비트코인 급락(CRASH) 외에는 BTC 추세와 무관하게 1H EMA20 지지선 기준을 정상장(0.980)으로 일관 적용
        mtf_ratio = 0.980
        mtf_allowed = current_1h >= (ema20_1h * mtf_ratio)
        mtf_reason = f"1H {current_1h:.1f} {'>=' if mtf_allowed else '<'} EMA20 {ema20_1h:.1f} (기준 {mtf_ratio:.3f})"

    # 2. [과제 B] 하드 안전 게이트 (Hard Safety Gates - 알파 점수로 우회 불가)
    hard_gate_btc = regime_upper not in ("CRASH", "BEAR_VOLATILE")
    hard_gate_mtf = mtf_allowed
    # 알트코인 독립 매수: RSI 하드 게이트 범위를 42~70으로 통일
    rsi_hard_min = StrategyPolicy.RSI_MIN_NORMAL
    rsi_hard_max = StrategyPolicy.RSI_MAX_NORMAL
    hard_gate_rsi = (rsi_hard_min <= rsi <= rsi_hard_max)
    # 볼린저 밴드 %B 상한: 일반장은 0.72 추격 차단, RISK_OFF는 독자 수급 반등 0.80 수용
    pct_b_hard_max = (
        StrategyPolicy.PCT_B_MAX_RISK_OFF
        if regime_upper == "RISK_OFF"
        else StrategyPolicy.PCT_B_MAX
    )
    hard_gate_bb = (StrategyPolicy.PCT_B_MIN <= pct_b <= pct_b_hard_max)
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
    total_score = int(alpha_res.get("total_score", 0) or 0)
    # 확정봉의 양봉 전환(current >= open_0)은 필수 유지하여 하락 중 무계획 물타기를 막는다.
    # 직전 종가 회복은 0.2% 미세 버퍼를 허용하거나, 알파 점수가 우수한 종목(>= 60)은 양봉 전환만으로도 인정한다.
    rebound_confirmed = current >= open_0 and (
        current >= previous_close * 0.998
        or total_score >= StrategyPolicy.ALPHA_BUY_THRESHOLD
    )
    pullback_zone = pct_b_min <= pct_b <= pct_b_max
    near_recent_low = 0.0 <= distance_from_recent_low <= pullback_max_distance
    signal_5m = (
        ma5 >= ma20 * StrategyPolicy.PULLBACK_MA_ALIGNMENT_RATIO
        and rsi_min <= rsi <= rsi_max
        and pullback_zone
        and near_recent_low
        and rebound_confirmed
    )
    normalized_entry_type = (entry_type or "CONFIRMED").upper()

    # 모멘텀 돌파는 최신 확정 5분봉의 고점·거래량·양봉·RSI를 함께 확인한다.
    momentum_breakout_passed = False
    momentum_breakout_reason = "확인형 후보"
    momentum_mtf_allowed = mtf_allowed
    momentum_mtf_reason = mtf_reason
    is_leader = is_rs_leader(relative_strength, regime_upper)
    if normalized_entry_type == "MOMENTUM_BREAKOUT":
        lookback = StrategyPolicy.MOMENTUM_BREAKOUT_LOOKBACK_BARS
        previous_candles = candles[1:lookback + 1]
        previous_high = max((float(c.get("high_price", c.get("trade_price", 0.0)) or 0.0) for c in previous_candles), default=0.0)
        previous_volumes = [float(c.get("candle_acc_trade_volume", 0.0) or 0.0) for c in candles[1:21]]
        average_volume = (sum(previous_volumes) / len(previous_volumes)) if previous_volumes else 0.0
        current_volume = float(candles[0].get("candle_acc_trade_volume", 0.0) or 0.0)
        current_open = float(candles[0].get("opening_price", current) or current)
        volume_confirmed = average_volume > 0 and current_volume >= average_volume * StrategyPolicy.MOMENTUM_BREAKOUT_VOLUME_RATIO_MIN

        breakout_target = (
            previous_high * StrategyPolicy.RS_LEADER_BREAKOUT_TOLERANCE
            if is_leader
            else previous_high
        )
        price_breakout = previous_high > 0 and (current > previous_high or (is_leader and current >= breakout_target))
        bullish_candle = current >= current_open
        momentum_rsi_passed = StrategyPolicy.MOMENTUM_BREAKOUT_RSI_MIN <= rsi <= (
            StrategyPolicy.MOMENTUM_BREAKOUT_RSI_MAX + 2.0 if is_leader else StrategyPolicy.MOMENTUM_BREAKOUT_RSI_MAX
        )
        if candles_1h and len(candles_1h) >= 20:
            momentum_ema20 = calculate_ema([float(c.get("trade_price", 0.0)) for c in candles_1h], 20)
            momentum_mtf_allowed = current_1h >= momentum_ema20 * StrategyPolicy.MOMENTUM_BREAKOUT_MTF_EMA20_RATIO
            momentum_mtf_reason = (
                f"1H {current_1h:.1f} {'>=' if momentum_mtf_allowed else '<'} "
                f"EMA20 {momentum_ema20:.1f} (모멘텀 기준 {StrategyPolicy.MOMENTUM_BREAKOUT_MTF_EMA20_RATIO:.3f})"
            )
        momentum_breakout_passed = price_breakout and volume_confirmed and bullish_candle and momentum_rsi_passed
        leader_tag = " [RS 주도주 특례]" if is_leader else ""
        momentum_breakout_reason = (
            f"직전 {lookback}봉 고점 돌파={'통과' if price_breakout else '차단'}{leader_tag}, "
            f"거래량배수={(current_volume / average_volume) if average_volume > 0 else 0.0:.2f}, "
            f"양봉={'통과' if bullish_candle else '차단'}, RSI={'통과' if momentum_rsi_passed else '차단'}, "
            f"1H MTF={'통과' if momentum_mtf_allowed else '차단'}"
        )
    if normalized_entry_type == "MOMENTUM_BREAKOUT":
        # 반등형의 저점 근접 조건은 적용하지 않되, 급락·상위 추세·이격·윗꼬리 안전 게이트는 유지한다.
        entry_alpha_threshold = get_momentum_breakout_alpha_threshold(
            btc_regime, night_active, relative_strength=relative_strength
        )
        # 모멘텀 돌파는 급등 캔들의 탄력을 감안하여 모멘텀 전용 이격도(최대 +5.0%)를 적용한다.
        hard_gate_disparity_momentum = current <= (ma20 * StrategyPolicy.MAX_MA20_DISPARITY_MOMENTUM)
        momentum_safety_passed = hard_gate_btc and momentum_mtf_allowed and hard_gate_disparity_momentum and hard_gate_shadow
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
    is_bull = (regime_upper == "BULL_TREND")
    if is_major:
        min_tgt_pct = StrategyPolicy.MAJOR_MIN_TARGET_PCT
        min_stp_pct = StrategyPolicy.MAJOR_MIN_STOP_PCT
    elif is_bull:
        min_tgt_pct = StrategyPolicy.BULL_PARTIAL_TP_1_PCT
        min_stp_pct = StrategyPolicy.BULL_STOP_LOSS_PCT
    else:
        min_tgt_pct = StrategyPolicy.MIN_TARGET_PCT
        min_stp_pct = StrategyPolicy.MIN_STOP_PCT

    target_offset = max(current * min_tgt_pct, volatility * StrategyPolicy.ATR_TARGET_MULTIPLIER)
    target_price = current + target_offset

    stop_offset = max(current * min_stp_pct, volatility * StrategyPolicy.ATR_STOP_MULTIPLIER)
    stop_loss = current - stop_offset

    checklist_details = {
        "alpha_score": alpha_res["total_score"],
        "alpha_threshold": entry_alpha_threshold,
        "is_night": night_active,
        "entry_type": normalized_entry_type,
        "factor_breakdown": alpha_res["factor_breakdown"],
        "hard_gates": {
            "all_passed": hard_gates_passed,
            "btc_regime": {"pass": hard_gate_btc, "regime": btc_regime},
            "mtf_trend": {"pass": hard_gate_mtf, "detail": mtf_reason},
            "rsi_guard": {"pass": hard_gate_rsi, "value": rsi, "min": rsi_hard_min, "max": rsi_hard_max},
            "bb_guard": {"pass": hard_gate_bb, "value": round(pct_b, 3), "min": StrategyPolicy.PCT_B_MIN, "max": pct_b_hard_max},
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

    final_target_price = round(target_price, 4 if current < 1.0 else 2)
    final_stop_loss = round(stop_loss, 4 if current < 1.0 else 2)
    return {
        "allow_buy": allowed,
        "reason": ", ".join(reasons),
        "entry_price": current,
        "target_price": final_target_price,
        "stop_loss": final_stop_loss,
        "atr": volatility,
        "atr_pct": atr_pct,
        "rsi": rsi,
        "pct_b": pct_b,
        "alpha_score": alpha_res["total_score"],
        "entry_type": normalized_entry_type,
        "is_rs_leader": is_leader,
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
            "target_price": final_target_price,
            "stop_loss": final_stop_loss,
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


def has_confirmed_swing_trend_candles(candles_4h: list[dict[str, Any]] | None) -> bool:
    """스윙 EMA20 판정에 필요한 4시간 확정봉 20개가 있는지 확인한다."""
    return bool(select_completed_candles(candles_4h or [], 20))


ListingMaturity = Literal["MATURE", "NEW_LISTING", "INSUFFICIENT"]

# 성숙(MATURE) 경로 1차 퀀트 게이트용 최소 원시 5분봉 개수
MATURE_MIN_RAW_5M_CANDLES = 20


def get_minimum_raw_5m_candles(maturity: ListingMaturity) -> int:
    """경로별 5분봉 최소 원시 개수. classify_listing_maturity() 판정 이후 사용."""
    if maturity == "NEW_LISTING":
        return StrategyPolicy.NEW_LISTING_MIN_5M_COMPLETED + 1
    if maturity == "MATURE":
        return MATURE_MIN_RAW_5M_CANDLES
    return 0


def should_block_for_minimum_candles(
    require_minimum_candles: bool,
    maturity: ListingMaturity,
    candles_5m: list[dict[str, Any]] | None,
) -> bool:
    """업비트 등 require_minimum_candles 프로필에서 경로별 최소 봉 수 미달 시 True."""
    if not require_minimum_candles or maturity == "INSUFFICIENT":
        return False
    min_raw = get_minimum_raw_5m_candles(maturity)
    if min_raw <= 0:
        return False
    return not candles_5m or len(candles_5m) < min_raw


def _parse_candle_timestamp_kst(candle: dict[str, Any]) -> datetime | None:
    """캔들 시각 필드를 KST datetime으로 변환한다. 파싱 실패 시 None."""
    raw = candle.get("candle_date_time_kst") or candle.get("candle_date_time_utc")
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=KST)
    return parsed.astimezone(KST)


def get_oldest_candle_age_hours(candles: list[dict[str, Any]] | None) -> float | None:
    """최신순 캔들 목록에서 가장 오래된 봉의 경과 시간(시간)을 반환한다."""
    if not candles:
        return None
    oldest_ts = _parse_candle_timestamp_kst(candles[-1])
    if oldest_ts is None:
        return None
    age = get_kst_now() - oldest_ts
    return max(0.0, age.total_seconds() / 3600.0)


def is_within_new_listing_age_window(
    candles_4h: list[dict[str, Any]] | None,
    *,
    max_age_hours: int | None = None,
) -> bool:
    """4H 이력이 부족한 종목이 신규상장 윈도우 안에 있는지 확인한다."""
    limit_hours = max_age_hours if max_age_hours is not None else StrategyPolicy.get_new_listing_max_age_hours()
    age_hours = get_oldest_candle_age_hours(candles_4h)
    if age_hours is None:
        # 빈 응답·파싱 실패는 상장 직후 증거가 아니므로 신규 진입을 fail-closed한다.
        return False
    return age_hours <= float(limit_hours)


def classify_listing_maturity(
    candles_4h: list[dict[str, Any]] | None,
    candles_1h: list[dict[str, Any]] | None,
    candles_5m: list[dict[str, Any]] | None,
    four_hour_history_status: str | None = None,
) -> ListingMaturity:
    """
    캔들 이력으로 성숙(MATURE)·신규상장(NEW_LISTING)·부족(INSUFFICIENT)을 판정한다.
    - MATURE: 4H 확정 20개 이상 → 기존 CONFIRMED/모멘텀/스윙 경로
    - NEW_LISTING: 신뢰 가능한 희소 4H 이력 + 5분 확정봉 최소 충족 + 상장 윈도우 내
    - INSUFFICIENT: 5분봉도 부족하거나 상장 윈도우 초과
    """
    # 빈 배열·예외·형식 오류는 상장 이력 부족과 구분해야 한다. 구분할 수 없으면 매수를 막는다.
    history_status = str(four_hour_history_status or ("AVAILABLE" if candles_4h else "UNAVAILABLE")).upper()
    if history_status != "AVAILABLE" or not candles_4h:
        return "INSUFFICIENT"

    if has_confirmed_swing_trend_candles(candles_4h):
        return "MATURE"

    min_5m = StrategyPolicy.NEW_LISTING_MIN_5M_COMPLETED
    completed_5m = select_completed_candles(candles_5m or [], min_5m)
    if len(completed_5m) < min_5m:
        return "INSUFFICIENT"

    if not is_within_new_listing_age_window(candles_4h):
        return "INSUFFICIENT"

    return "NEW_LISTING"


def is_new_listing_eligible(
    maturity: ListingMaturity,
    *,
    acc_trade_price_24h: float,
    change_rate: float,
    relative_strength: float,
    btc_regime: str = "NORMAL",
    exchange: str | None = None,
) -> tuple[bool, str]:
    """NEW_LISTING 경로 후보가 하드 안전 조건을 충족하는지 검증한다."""
    if maturity != "NEW_LISTING":
        return False, f"신규상장 경로 아님(maturity={maturity})"
    if not StrategyPolicy.is_new_listing_enabled(exchange):
        return False, "신규상장 경로 비활성화(NEW_LISTING_ENABLED=false)"

    regime_upper = str(btc_regime or "NORMAL").upper()
    if regime_upper == "CRASH":
        return False, f"신규상장 진입 불가 BTC 레짐({regime_upper})"

    min_trade_value = StrategyPolicy.get_new_listing_min_trade_value_24h(regime_upper)
    trade_value = float(acc_trade_price_24h or 0.0)
    if trade_value < min_trade_value:
        return False, (
            f"24h 거래대금 부족({trade_value:,.0f}원 < {min_trade_value:,.0f}원)"
        )

    if regime_upper == "RISK_OFF":
        min_change = StrategyPolicy.NEW_LISTING_MIN_CHANGE_RATE_RISK_OFF
        max_change = StrategyPolicy.NEW_LISTING_MAX_CHANGE_RATE_RISK_OFF
        min_rs = StrategyPolicy.NEW_LISTING_MIN_RS_RISK_OFF
    else:
        min_change = StrategyPolicy.NEW_LISTING_MIN_CHANGE_RATE
        max_change = StrategyPolicy.NEW_LISTING_MAX_CHANGE_RATE
        min_rs = StrategyPolicy.NEW_LISTING_MIN_RS
    change = float(change_rate or 0.0)
    rs = float(relative_strength or 0.0)

    if change < min_change:
        return False, f"당일 상승률 부족({change * 100:.2f}% < {min_change * 100:.1f}%)"
    if change > max_change:
        return False, f"당일 상승률 과열({change * 100:.2f}% > {max_change * 100:.1f}%)"
    if rs < min_rs:
        return False, f"BTC 대비 RS 부족({rs * 100:.2f}% < {min_rs * 100:.1f}%)"

    return True, "신규상장 단타 후보 자격 충족"


def should_force_swing_data_unavailable_exit(hold_duration_sec: float) -> bool:
    """4H 데이터 불능이 장시간 지속될 때만 무기한 보유 방지 보호 청산을 허용한다."""
    return hold_duration_sec >= StrategyPolicy.SWING_4H_DATA_UNAVAILABLE_MAX_HOLD_SECONDS


def evaluate_swing_trend_exit(
    candles_4h: list[dict[str, Any]],
    current_price: float,
    buffer_ratio: float = 0.985,
) -> tuple[bool, str]:
    """
    스윙 포지션의 추세 지지선 이탈 기반 청산 (Trend-Stop) 판정
    - 4시간봉 확정 캔들의 EMA20 대비 buffer_ratio(기본 0.985, -1.5% 하회) 이탈 시 청산
    - 캔들 부족 시 관망 (Fail-Safe)
    """
    if current_price <= 0:
        return False, "현재가 오류로 4H 추세 판정 보류"

    completed = select_completed_candles(candles_4h or [], 20)
    if not completed:
        return False, "4H 확정봉 부족: 신규 BUY 차단 및 기존 포지션 보호 모드"

    prices = [float(c.get("trade_price", 0.0)) for c in completed]
    ema20 = calculate_ema(prices, 20)
    threshold = ema20 * buffer_ratio

    if current_price < threshold:
        drop_pct = (current_price - ema20) / ema20 * 100.0
        return True, f"스윙 추세 이탈 청산: 현재가 {current_price:,.2f}원 < 4H EMA20 {ema20:,.2f}원의 {buffer_ratio*100:.1f}% ({drop_pct:+.2f}%)"

    return False, f"스윙 추세 양호: 현재가 {current_price:,.2f}원 >= 4H EMA20 {ema20:,.2f}원 지지"

