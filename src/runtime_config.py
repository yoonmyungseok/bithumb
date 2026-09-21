"""Runtime configuration normalization shared by both exchange entry points."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


def get_fraction_setting(name: str, default: float, *, positive: bool = True) -> float:
    """Read a ratio as a decimal fraction, accepting legacy percentage input safely.

    Values such as ``5`` and ``-5`` are interpreted as 5% for backwards
    compatibility.  The returned value is always positive and less than one;
    invalid safety settings fail at startup rather than silently disabling a
    risk guard.
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        value = float(default)
    else:
        try:
            value = float(raw.strip())
        except ValueError as exc:
            raise ValueError(f"{name} must be a numeric ratio, e.g. 0.05 for 5%") from exc

    value = abs(value)
    if value >= 1.0:
        value /= 100.0
    if (positive and value <= 0.0) or value >= 1.0:
        raise ValueError(f"{name} must be greater than 0 and less than 1 after normalization")
    return value


def get_exchange_env_setting(
    exchange_key: str,
    key: str,
    default: Any = "",
    type_cast: Any = None,
) -> Any:
    """거래소 전용 환경변수를 우선 읽고 없으면 공통 환경변수, 기본값 순으로 반환한다.

    예: exchange_key="upbit", key="MOMENTUM_BREAKOUT_ENABLED" ->
        UPBIT_MOMENTUM_BREAKOUT_ENABLED -> MOMENTUM_BREAKOUT_ENABLED -> default
    """
    ex_prefix = (exchange_key or "").strip().upper()
    ex_key = f"{ex_prefix}_{key}" if ex_prefix else ""

    val_str = ""
    if ex_key and ex_key in os.environ and os.environ[ex_key].strip():
        val_str = os.environ[ex_key].strip()
    elif key in os.environ and os.environ[key].strip():
        val_str = os.environ[key].strip()
    else:
        return default

    if type_cast is bool:
        return val_str.lower() in {"1", "true", "yes", "on", "enable", "enabled"}
    if type_cast is int:
        try:
            return int(float(val_str))
        except (ValueError, TypeError):
            return default
    if type_cast is float:
        try:
            return float(val_str)
        except (ValueError, TypeError):
            return default
    if type_cast is not None and callable(type_cast):
        try:
            return type_cast(val_str)
        except Exception:
            return default
    return val_str


def normalize_trading_mode(raw_mode: str | None, default: str = "LIVE") -> str:
    """거래소별로 혼용되던 REAL/LIVE 모드 표기를 LIVE로 정규화한다."""
    m = (raw_mode or default).strip().upper()
    if m in ("REAL", "LIVE"):
        return "LIVE"
    if m in ("PAPER", "SIMULATION", "MOCK", "TEST"):
        return "PAPER"
    return m


@dataclass(frozen=True)
class RuntimeRiskSettings:
    """Normalized risk ratios consumed by every live trading cycle."""

    btc_crash_threshold_pct: float
    max_daily_loss_pct: float
    trailing_start_pct: float
    trailing_stop_pct: float


def load_runtime_risk_settings() -> RuntimeRiskSettings:
    """Load all risk ratios through the one compatibility-safe configuration path."""
    return RuntimeRiskSettings(
        btc_crash_threshold_pct=get_fraction_setting("BTC_CRASH_THRESHOLD_PCT", 0.015),
        max_daily_loss_pct=get_fraction_setting("MAX_DAILY_LOSS_PCT", 0.05),
        trailing_start_pct=get_fraction_setting("TRAILING_START_PCT", 0.02),
        trailing_stop_pct=get_fraction_setting("TRAILING_STOP_PCT", 0.020),
    )


# ------------------------------------------------------------------------------
# 공통 설정 스키마 및 런타임 동적 설정 매니저 (SSOT)
# ------------------------------------------------------------------------------

@dataclass(frozen=True)
class ConfigFieldDef:
    """공통 설정 단일 항목 정의"""
    key: str
    type_name: str  # "percent", "int", "krw", "bool"
    default: Any
    min_val: float | None = None
    max_val: float | None = None
    category: str = "general"
    label: str = ""
    description: str = ""
    unit: str = ""


# 대시보드에서 변경 허용되는 공통 설정 목록 (민감정보 원천 차단)
COMMON_CONFIG_SCHEMA: dict[str, ConfigFieldDef] = {
    # 1. 리스크 & 손익
    "TRAILING_START_PCT": ConfigFieldDef(
        key="TRAILING_START_PCT",
        type_name="percent",
        default=0.02,
        min_val=0.005,
        max_val=0.20,
        category="risk",
        label="트레일링 스탑 시작 수익률",
        description="이 수익률에 도달하면 최고점 추적 및 트레일링 스탑을 가동합니다.",
        unit="%",
    ),
    "TRAILING_STOP_PCT": ConfigFieldDef(
        key="TRAILING_STOP_PCT",
        type_name="percent",
        default=0.020,
        min_val=0.005,
        max_val=0.10,
        category="risk",
        label="트레일링 청산 하락폭",
        description="트레일링 가동 후 최고점 대비 이 비율만큼 하락하면 시장가 청산합니다.",
        unit="%",
    ),
    "MAX_DAILY_LOSS_PCT": ConfigFieldDef(
        key="MAX_DAILY_LOSS_PCT",
        type_name="percent",
        default=0.05,
        min_val=0.01,
        max_val=0.20,
        category="risk",
        label="일일 누적 손실 한도",
        description="하루 누적 손실이 이 한도에 도달하면 신규 매수를 당일 전면 차단합니다.",
        unit="%",
    ),
    "BTC_CRASH_THRESHOLD_PCT": ConfigFieldDef(
        key="BTC_CRASH_THRESHOLD_PCT",
        type_name="percent",
        default=0.015,
        min_val=0.005,
        max_val=0.10,
        category="risk",
        label="BTC 급락 감지 임계치",
        description="비트코인이 직전 대비 이 비율 이상 급락하면 신규 매수를 일시 차단합니다.",
        unit="%",
    ),
    "ORDERBOOK_SLIPPAGE_ENFORCEMENT": ConfigFieldDef(
        key="ORDERBOOK_SLIPPAGE_ENFORCEMENT",
        type_name="bool",
        default=False,
        category="risk",
        label="호가 슬리피지 강제 차단",
        description="호가 잔량 부족 또는 예상 슬리피지 초과 시 실제 매수를 차단합니다.",
        unit="",
    ),

    # 2. 스크리닝 & 전략
    "TOP_COUNT": ConfigFieldDef(
        key="TOP_COUNT",
        type_name="int",
        default=3,
        min_val=1,
        max_val=10,
        category="screening",
        label="분석 대상 후보 수",
        description="각 사이클마다 정밀 분석을 수행할 상위 후보 코인 수입니다.",
        unit="개",
    ),
    "MIN_TRADE_VALUE": ConfigFieldDef(
        key="MIN_TRADE_VALUE",
        type_name="krw",
        default=1_000_000_000,
        min_val=100_000_000,
        max_val=100_000_000_000,
        category="screening",
        label="24시간 최소 거래대금",
        description="유동성이 부족한 잡코인을 배제하기 위한 24시간 거래대금 하한선입니다.",
        unit="원",
    ),
    "MIN_CHANGE_RATE": ConfigFieldDef(
        key="MIN_CHANGE_RATE",
        type_name="percent",
        default=0.005,
        min_val=0.0,
        max_val=0.10,
        category="screening",
        label="후보 최소 등락률",
        description="당일 변동성이 너무 적은 종목을 제외하기 위한 최소 상승률입니다.",
        unit="%",
    ),
    "MAX_CHANGE_RATE": ConfigFieldDef(
        key="MAX_CHANGE_RATE",
        type_name="percent",
        default=0.25,
        min_val=0.05,
        max_val=1.00,
        category="screening",
        label="후보 최대 등락률",
        description="이미 과열되어 상투 위험이 높은 종목을 배제하기 위한 상한선입니다.",
        unit="%",
    ),
    "MOMENTUM_BREAKOUT_ENABLED": ConfigFieldDef(
        key="MOMENTUM_BREAKOUT_ENABLED",
        type_name="bool",
        default=True,
        category="screening",
        label="확정봉 모멘텀 돌파 경로",
        description="급등 모멘텀 돌파 전략의 활성화 여부입니다.",
        unit="",
    ),
    "NEW_LISTING_ENABLED": ConfigFieldDef(
        key="NEW_LISTING_ENABLED",
        type_name="bool",
        default=True,
        category="screening",
        label="신규 상장 코인 추적",
        description="신규 상장 코인의 진입 후보 추적 경로 활성화 여부입니다.",
        unit="",
    ),
    "NEW_LISTING_ENFORCEMENT": ConfigFieldDef(
        key="NEW_LISTING_ENFORCEMENT",
        type_name="bool",
        default=False,
        category="screening",
        label="신규 상장 실주문 집행",
        description="활성화 시 신규 상장 코인의 실주문을 허용합니다. (비활성 시 관찰 모드)",
        unit="",
    ),

    # 3. 포트폴리오 한도
    "DYNAMIC_SLOTS_ENABLED": ConfigFieldDef(
        key="DYNAMIC_SLOTS_ENABLED",
        type_name="bool",
        default=True,
        category="portfolio",
        label="시장 레짐 연동 동적 슬롯",
        description="시장 국면(강세/횡보/약세)에 맞춰 슬롯 한도 및 총 투자 노출도를 유동적으로 자동 전환합니다.",
        unit="",
    ),
    "MAX_SCALP_POSITIONS": ConfigFieldDef(
        key="MAX_SCALP_POSITIONS",
        type_name="int",
        default=1,
        min_val=0,
        max_val=10,
        category="portfolio",
        label="단타 예약 슬롯 수",
        description="일반 5분봉 단타/스캘핑 전략에 배정할 최대 보유 종목 수입니다.",
        unit="개",
    ),
    "MAX_OPEN_POSITIONS": ConfigFieldDef(
        key="MAX_OPEN_POSITIONS",
        type_name="int",
        default=3,
        min_val=1,
        max_val=20,
        category="portfolio",
        label="총 동시 보유 포지션 수 (자동 합산)",
        description="단타 + 스윙 + 신규상장 슬롯의 합산으로 자동 결정되는 전체 최대 보유 종목 수입니다.",
        unit="개",
    ),
    "MAX_POSITION_PCT": ConfigFieldDef(
        key="MAX_POSITION_PCT",
        type_name="percent",
        default=0.35,
        min_val=0.05,
        max_val=1.00,
        category="portfolio",
        label="단일 포지션 최대 비중 (자동 계산)",
        description="총 동시 보유 슬롯 수에 맞춰 안전하게 자동 결정되는 종목당 최대 투자 비율입니다.",
        unit="%",
    ),
    "MAX_TOTAL_EXPOSURE_PCT": ConfigFieldDef(
        key="MAX_TOTAL_EXPOSURE_PCT",
        type_name="percent",
        default=0.90,
        min_val=0.10,
        max_val=1.00,
        category="portfolio",
        label="총 익스포저 최대 비중",
        description="전체 포지션 합계가 총 자산에서 차지할 수 있는 최대 비율입니다.",
        unit="%",
    ),
    "MAX_ORDER_KRW": ConfigFieldDef(
        key="MAX_ORDER_KRW",
        type_name="krw",
        default=20_000_000,
        min_val=100_000,
        max_val=500_000_000,
        category="portfolio",
        label="단일 주문 최대 금액",
        description="1회 매수 주문에 투입할 수 있는 절대 최대 금액 한도입니다.",
        unit="원",
    ),
    "MAX_SWING_POSITIONS": ConfigFieldDef(
        key="MAX_SWING_POSITIONS",
        type_name="int",
        default=1,
        min_val=0,
        max_val=5,
        category="portfolio",
        label="스윙 예약 슬롯 수",
        description="스윙 추세추종 전략에 배정할 최대 보유 종목 수입니다. (0이면 스윙 비활성화)",
        unit="개",
    ),
    "MAX_NEW_LISTING_POSITIONS": ConfigFieldDef(
        key="MAX_NEW_LISTING_POSITIONS",
        type_name="int",
        default=1,
        min_val=0,
        max_val=5,
        category="portfolio",
        label="신규상장 예약 슬롯 수",
        description="상장 72시간 이내 단타 전략에 배정할 최대 보유 종목 수입니다. (0이면 비활성화)",
        unit="개",
    ),
    "MAX_ALT_ALLOC_PCT": ConfigFieldDef(
        key="MAX_ALT_ALLOC_PCT",
        type_name="percent",
        default=0.15,
        min_val=0.05,
        max_val=0.50,
        category="portfolio",
        label="알트코인 단일 최대 비중 (상한 캡)",
        description="알트코인 단타 진입 시 종목당 투입할 수 있는 최대 비중 상한선입니다.",
        unit="%",
    ),
    "MIN_ALT_ALLOC_PCT": ConfigFieldDef(
        key="MIN_ALT_ALLOC_PCT",
        type_name="percent",
        default=0.10,
        min_val=0.05,
        max_val=0.30,
        category="portfolio",
        label="알트코인 단일 최소 비중 (하한 캡)",
        description="알트코인 진입 시 수수료 대비 실익을 확보하기 위한 최소 비중 하한선입니다.",
        unit="%",
    ),
}


class CommonConfigManager:
    """공통 설정값 로드, 유효성 검증, .env 영속화 및 런타임 동기화 관리자"""

    def __init__(self, env_path: str | None = None):
        if env_path is None:
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            env_path = os.path.join(project_root, ".env")
        self.env_path = env_path

    def get_all_settings(self) -> dict[str, Any]:
        """현재 적용 중인 모든 공통 설정값을 스키마 메타데이터와 함께 반환"""
        results = {}
        for key, field_def in COMMON_CONFIG_SCHEMA.items():
            raw_val = os.getenv(key)
            val = self._parse_env_value(field_def, raw_val)
            # UI 친화적 표시값 (percent는 백분율 2.0 등으로 표시)
            display_val = val
            if field_def.type_name == "percent":
                display_val = round(val * 100.0, 2)

            results[key] = {
                "key": key,
                "value": val,
                "display_value": display_val,
                "type": field_def.type_name,
                "category": field_def.category,
                "label": field_def.label,
                "description": field_def.description,
                "unit": field_def.unit,
                "min": (field_def.min_val * 100.0) if (field_def.type_name == "percent" and field_def.min_val is not None) else field_def.min_val,
                "max": (field_def.max_val * 100.0) if (field_def.type_name == "percent" and field_def.max_val is not None) else field_def.max_val,
                "default": (field_def.default * 100.0) if field_def.type_name == "percent" else field_def.default,
            }
        return results

    def validate_and_normalize(self, key: str, input_value: Any) -> tuple[bool, Any, str]:
        """단일 설정값에 대한 타입 및 범위 유효성 검증 및 정규화 (Fail-closed)"""
        field_def = COMMON_CONFIG_SCHEMA.get(key)
        if not field_def:
            return False, None, f"알 수 없거나 변경이 허용되지 않는 설정 키입니다: {key}"

        if input_value is None or str(input_value).strip() == "":
            return False, None, f"{field_def.label} 값이 비어 있습니다."

        try:
            if field_def.type_name == "bool":
                if isinstance(input_value, bool):
                    return True, input_value, ""
                str_val = str(input_value).strip().lower()
                if str_val in ("true", "1", "yes", "y", "on"):
                    return True, True, ""
                elif str_val in ("false", "0", "no", "n", "off"):
                    return True, False, ""
                return False, None, f"{field_def.label}은(는) true 또는 false여야 합니다."

            elif field_def.type_name == "int":
                val_int = int(float(str(input_value).strip()))
                if field_def.min_val is not None and val_int < field_def.min_val:
                    return False, None, f"{field_def.label}은(는) 최소 {int(field_def.min_val)} 이상이어야 합니다."
                if field_def.max_val is not None and val_int > field_def.max_val:
                    return False, None, f"{field_def.label}은(는) 최대 {int(field_def.max_val)} 이하여야 합니다."
                return True, val_int, ""

            elif field_def.type_name in ("krw", "float"):
                val_float = float(str(input_value).strip())
                if field_def.min_val is not None and val_float < field_def.min_val:
                    return False, None, f"{field_def.label}은(는) 최소 {field_def.min_val:,.0f} 이상이어야 합니다."
                if field_def.max_val is not None and val_float > field_def.max_val:
                    return False, None, f"{field_def.label}은(는) 최대 {field_def.max_val:,.0f} 이하여야 합니다."
                return True, val_float, ""

            elif field_def.type_name == "percent":
                # 사용자가 백분율(예: 2.5, 40, 100) 또는 소수 비율(예: 0.025, 0.40, 1.0) 중 어떤 형식으로 입력해도 안전하게 처리
                val_pct = abs(float(str(input_value).strip()))
                threshold = field_def.max_val if (field_def.max_val is not None and field_def.max_val < 1.0) else 1.0
                normalized = val_pct / 100.0 if val_pct > threshold else val_pct

                if field_def.min_val is not None and normalized < field_def.min_val:
                    return False, None, f"{field_def.label}은(는) 최소 {field_def.min_val * 100:.1f}% 이상이어야 합니다."
                if field_def.max_val is not None and normalized > field_def.max_val:
                    return False, None, f"{field_def.label}은(는) 최대 {field_def.max_val * 100:.1f}% 이하여야 합니다."
                return True, round(normalized, 6), ""

            return False, None, f"지원되지 않는 데이터 타입입니다: {field_def.type_name}"

        except (ValueError, TypeError) as exc:
            return False, None, f"{field_def.label} 입력값 형식이 올바르지 않습니다: {exc}"

    def update_settings(self, new_settings: dict[str, Any]) -> tuple[bool, dict[str, Any], list[str]]:
        """복수 설정값을 검증 후 .env 파일 영구 저장 및 os.environ 동기화 수행"""
        normalized_updates: dict[str, Any] = {}
        errors: list[str] = []

        # 1. 모든 입력값 사전 유효성 검증 (전체 검증 성공 시에만 일괄 반영 - All-or-Nothing)
        for key, raw_val in new_settings.items():
            if key not in COMMON_CONFIG_SCHEMA:
                continue
            ok, val, err = self.validate_and_normalize(key, raw_val)
            if not ok:
                errors.append(err)
            else:
                normalized_updates[key] = val

        if errors:
            return False, {}, errors

        # 3대 슬롯(단타, 스윙, 신규상장) 입력 시 MAX_OPEN_POSITIONS 자동 합산 및 MAX_POSITION_PCT 자동 계산 동기화
        if (
            "MAX_SCALP_POSITIONS" in normalized_updates
            or "MAX_SWING_POSITIONS" in normalized_updates
            or "MAX_NEW_LISTING_POSITIONS" in normalized_updates
        ):
            scalp = normalized_updates.get(
                "MAX_SCALP_POSITIONS",
                self._parse_env_value(COMMON_CONFIG_SCHEMA["MAX_SCALP_POSITIONS"], os.getenv("MAX_SCALP_POSITIONS")),
            )
            swing = normalized_updates.get(
                "MAX_SWING_POSITIONS",
                self._parse_env_value(COMMON_CONFIG_SCHEMA["MAX_SWING_POSITIONS"], os.getenv("MAX_SWING_POSITIONS")),
            )
            new_listing = normalized_updates.get(
                "MAX_NEW_LISTING_POSITIONS",
                self._parse_env_value(COMMON_CONFIG_SCHEMA["MAX_NEW_LISTING_POSITIONS"], os.getenv("MAX_NEW_LISTING_POSITIONS")),
            )
            auto_total = max(1, int(scalp) + int(swing) + int(new_listing))
            normalized_updates["MAX_OPEN_POSITIONS"] = auto_total

            # 총 슬롯 수 기반 단일 포지션 최대 비중 자동 산출: min(0.50, max(0.15, round(1.0 / auto_total + 0.05, 2)))
            auto_max_pct = round(min(0.50, max(0.15, 1.0 / auto_total + 0.05)), 2)
            normalized_updates["MAX_POSITION_PCT"] = auto_max_pct
        elif "MAX_OPEN_POSITIONS" in normalized_updates:
            auto_total = max(1, int(normalized_updates["MAX_OPEN_POSITIONS"]))
            auto_max_pct = round(min(0.50, max(0.15, 1.0 / auto_total + 0.05)), 2)
            normalized_updates["MAX_POSITION_PCT"] = auto_max_pct

        if not normalized_updates:
            return True, {}, []

        # 2. .env 파일에 주석을 보존하며 안전하게 기록
        try:
            import dotenv
            for key, val in normalized_updates.items():
                field_def = COMMON_CONFIG_SCHEMA[key]
                if field_def.type_name == "bool":
                    str_val = "true" if val else "false"
                elif field_def.type_name == "int":
                    str_val = str(int(val))
                elif field_def.type_name == "krw":
                    str_val = str(int(val))
                else:
                    str_val = str(val)

                if os.path.exists(self.env_path):
                    dotenv.set_key(self.env_path, key, str_val, quote_mode="never")
                # 현재 프로세스의 os.environ 동기화
                os.environ[key] = str_val

        except Exception as exc:
            return False, {}, [f".env 파일 저장 중 예외가 발생했습니다: {exc}"]

        return True, normalized_updates, []

    def _parse_env_value(self, field_def: ConfigFieldDef, raw_val: str | None) -> Any:
        """환경 변수 원본 문자열을 타입에 맞게 안전 변환"""
        if raw_val is None or not str(raw_val).strip():
            if field_def.key == "MAX_SCALP_POSITIONS":
                try:
                    open_pos = int(float(os.getenv("MAX_OPEN_POSITIONS", "3")))
                    swing_pos = int(float(os.getenv("MAX_SWING_POSITIONS", "1")))
                    new_pos = int(float(os.getenv("MAX_NEW_LISTING_POSITIONS", "1")))
                    return max(0, open_pos - (swing_pos + new_pos))
                except Exception:
                    return field_def.default
            return field_def.default

        try:
            raw = str(raw_val).strip()
            if field_def.type_name == "bool":
                return raw.lower() in ("true", "1", "yes", "y", "on")
            elif field_def.type_name == "int":
                return int(float(raw))
            elif field_def.type_name == "krw":
                return float(raw)
            elif field_def.type_name == "percent":
                return get_fraction_setting(field_def.key, field_def.default)
            return float(raw)
        except Exception:
            return field_def.default


