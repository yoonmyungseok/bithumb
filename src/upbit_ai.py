"""업비트 전용 AI Provider 설정과 fail-closed 진입 차단을 담당합니다."""

from __future__ import annotations

import os

from ai_provider import AIProviderTelemetry, GeminiProvider
from gemini_analyzer import GeminiAnalyzer


def get_upbit_ai_config_block_reason() -> str:
    """업비트 전용 Gemini 키 설정이 불완전할 때만 반환합니다. 공용·빗썸 키 fallback을 일절 배제합니다."""
    # 공용 GEMINI_API_KEY나 BITHUMB_GEMINI_API_KEY fallback을 읽지 않아 거래소 자격 증명 경계를 엄격히 고정한다.
    if not os.getenv("UPBIT_GEMINI_API_KEY", "").strip():
        return "업비트 Gemini API 키(UPBIT_GEMINI_API_KEY)가 없어 신규 BUY를 차단합니다."
    return ""


def get_upbit_ai_entry_block_reason() -> str:
    """설정 오류 또는 Gemini 런타임 장애 시 모든 신규 BUY 경로가 읽는 차단 사유를 반환합니다."""
    config_reason = get_upbit_ai_config_block_reason()
    if config_reason:
        return config_reason
    # 구성 오류뿐 아니라 직전 Gemini 분석 실패도 모멘텀·반등·신규상장을 포함한 모든 신규 진입을 닫는다.
    return AIProviderTelemetry.get_entry_block_reason("upbit")


def build_upbit_analyzer() -> GeminiAnalyzer | None:
    """업비트는 전용 Gemini 설정이 유효할 때만 분석기를 생성하고 장애는 주문 게이트에서 차단합니다."""
    if get_upbit_ai_config_block_reason():
        return None
    provider = GeminiProvider(api_key=os.getenv("UPBIT_GEMINI_API_KEY", "").strip())
    return GeminiAnalyzer(provider=provider)
