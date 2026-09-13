"""Runtime configuration normalization shared by both exchange entry points."""

from __future__ import annotations

import os
from dataclasses import dataclass


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
    "MAX_OPEN_POSITIONS": ConfigFieldDef(
        key="MAX_OPEN_POSITIONS",
        type_name="int",
        default=3,
        min_val=1,
        max_val=10,
        category="portfolio",
        label="최대 보유 포지션 수",
        description="동시에 보유할 수 있는 최대 코인 종목 수입니다.",
        unit="개",
    ),
    "MAX_POSITION_PCT": ConfigFieldDef(
        key="MAX_POSITION_PCT",
        type_name="percent",
        default=0.35,
        min_val=0.05,
        max_val=1.00,
        category="portfolio",
        label="단일 포지션 최대 비중",
        description="단일 종목이 전체 운용 평가금액에서 차지할 수 있는 최대 비율입니다.",
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
                # 사용자가 2.5(백분율) 또는 0.025(소수 비율) 중 어떤 형식으로 입력해도 안전하게 처리
                val_pct = abs(float(str(input_value).strip()))
                # 1.0 이상이면 백분율(%)로 간주
                normalized = val_pct / 100.0 if val_pct >= 1.0 else val_pct

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


