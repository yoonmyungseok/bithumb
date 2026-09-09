"""빗썸 전용 AI Provider 설정과 fail-closed 진입 차단을 담당합니다."""

from __future__ import annotations

import os

from ai_provider import AIProviderTelemetry, BithumbGeminiProvider
from gemini_analyzer import GeminiAnalyzer


def get_bithumb_ai_config_block_reason() -> str:
    """빗썸 전용 Gemini 키·Provider 설정이 불완전할 때만 반환합니다."""
    provider = os.getenv("BITHUMB_AI_PROVIDER", "").strip().lower()
    if provider != "gemini":
        return "빗썸 AI Provider가 gemini로 설정되지 않아 신규 BUY를 차단합니다."
    # 공용·업비트 키 fallback을 읽지 않아 거래소 자격 증명 경계를 고정한다.
    if not os.getenv("BITHUMB_GEMINI_API_KEY", "").strip():
        return "빗썸 Gemini API 키가 없어 신규 BUY를 차단합니다."
    return ""


def get_bithumb_ai_entry_block_reason() -> str:
    """설정 오류 또는 Gemini 런타임 장애 시 모든 신규 BUY 경로가 읽는 차단 사유를 반환합니다."""
    config_reason = get_bithumb_ai_config_block_reason()
    if config_reason:
        return config_reason
    # 구성 오류뿐 아니라 직전 Gemini 분석 실패도 모멘텀·반등·신규상장을 포함한 모든 신규 진입을 닫는다.
    return AIProviderTelemetry.get_entry_block_reason("bithumb")


def build_bithumb_analyzer() -> GeminiAnalyzer | None:
    """빗썸은 전용 Gemini 설정이 유효할 때만 분석기를 생성하고 장애는 주문 게이트에서 차단합니다."""
    if get_bithumb_ai_config_block_reason():
        return None
    provider = BithumbGeminiProvider(api_key=os.getenv("BITHUMB_GEMINI_API_KEY", ""))
    return GeminiAnalyzer(provider=provider)
