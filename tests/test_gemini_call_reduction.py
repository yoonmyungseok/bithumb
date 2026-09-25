"""
Gemini API 호출량 절감 및 1단계 안정화 기능 검증 단위 테스트:
1. 실패 응답 네거티브 캐싱 (Negative Caching - 300초)
2. 서버 에러(503) 및 타임아웃 발생 시 단기 모델 쿨다운(120초)
3. AI Direct Entry 알파 점수 기준 상향 (기본 65점)
4. EntryGatingResult의 called_ai 보존 및 사이클당 예산 정상 차감
"""

import os
import sys
import time
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import pytest
import requests

from ai_provider import BaseGeminiProvider, GeminiProvider, ProviderResult
from gemini_analyzer import GeminiAnalyzer
from strategy_engine import StrategyPolicy
from trading_runtime import EntryGatingResult


@pytest.fixture(autouse=True)
def cleanup():
    GeminiAnalyzer.clear_caches()
    BaseGeminiProvider.clear_cooldowns()
    yield
    GeminiAnalyzer.clear_caches()
    BaseGeminiProvider.clear_cooldowns()


def test_negative_caching_on_api_failure():
    """Gemini API 호출 실패(fail-closed) 시 300초 동안 네거티브 캐시가 유지되어 재호출되지 않는지 검증."""
    mock_provider = MagicMock(spec=GeminiProvider)
    mock_provider.name = "gemini"
    mock_provider.exchange = "upbit"
    mock_provider.is_entry_fail_closed = True
    mock_provider.complete_json.return_value = ProviderResult(
        value=None, model="gemini-3.5-flash-lite", error_kind="http_error", status_code=503
    )

    analyzer = GeminiAnalyzer(api_key="test-key", provider=mock_provider)

    sample_candles = [
        {"opening_price": 1000.0, "high_price": 1020.0, "low_price": 990.0, "trade_price": 1010.0, "candle_acc_trade_volume": 10.0, "timestamp": 1234567890}
        for _ in range(30)
    ]

    # 1회차 호출: 실패 발생 -> fail-closed HOLD 반환 및 네거티브 캐싱
    res1 = analyzer.analyze(
        market="KRW-TEST",
        current_price=1010.0,
        candles=sample_candles,
        krw_balance=1000000.0,
        coin_balance=0.0,
        avg_buy_price=0.0,
        btc_regime="NORMAL",
    )
    assert res1["action"] == "HOLD"
    assert "신규 BUY 차단" in res1["reason"]
    assert mock_provider.complete_json.call_count == 1

    # 2회차 호출: 동일 캔들/조건 -> 네거티브 캐시에서 즉시 반환 (complete_json 재호출 0회)
    res2 = analyzer.analyze(
        market="KRW-TEST",
        current_price=1010.0,
        candles=sample_candles,
        krw_balance=1000000.0,
        coin_balance=0.0,
        avg_buy_price=0.0,
        btc_regime="NORMAL",
    )
    assert res2["action"] == "HOLD"
    assert mock_provider.complete_json.call_count == 1  # 호출 횟수가 증가하지 않아야 함!


def test_server_error_and_timeout_cooldown():
    """503 에러 및 타임아웃 발생 시 모델에 120초 쿨다운이 등록되는지 검증."""
    provider = GeminiProvider(api_key="test-key")

    # 1. 503 Service Unavailable 응답 시뮬레이션
    mock_response_503 = MagicMock()
    mock_response_503.status_code = 503
    mock_response_503.json.return_value = {"error": {"code": 503, "status": "UNAVAILABLE"}}
    mock_response_503.text = "Service Unavailable"

    with patch("requests.post", return_value=mock_response_503):
        result = provider.complete_json(
            prompt="test",
            models=["gemini-3.5-flash-lite"],
            schema={"type": "object"},
            context="test_503",
            timeout=5.0,
            max_tokens=100,
        )
        assert result.error_kind == "http_error"
        assert provider.is_model_cooling_down("gemini-3.5-flash-lite") is True

    # 2. Timeout 발생 시뮬레이션
    BaseGeminiProvider.clear_cooldowns()
    with patch("requests.post", side_effect=requests.exceptions.Timeout("Connection timed out")):
        result = provider.complete_json(
            prompt="test",
            models=["gemini-3.1-flash-lite"],
            schema={"type": "object"},
            context="test_timeout",
            timeout=5.0,
            max_tokens=100,
        )
        assert result.error_kind == "timeout"
        assert provider.is_model_cooling_down("gemini-3.1-flash-lite") is True


def test_ai_direct_entry_min_alpha():
    """AI Direct Entry 최소 알파 점수(기본 55점) 및 환경변수 오버라이드 검증."""
    assert StrategyPolicy.get_ai_direct_entry_min_alpha() == 55

    with patch.dict("os.environ", {"AI_DIRECT_ENTRY_MIN_ALPHA": "70"}):
        assert StrategyPolicy.get_ai_direct_entry_min_alpha() == 70

    with patch.dict("os.environ", {"AI_DIRECT_ENTRY_MIN_ALPHA": "40"}):
        # 50점 밑으로는 클램핑됨
        assert StrategyPolicy.get_ai_direct_entry_min_alpha() == 50


def test_entry_gating_called_ai_preservation():
    """EntryGatingResult가 should_continue=True여도 called_ai 플래그를 정상 보존하는지 검증."""
    result_active = EntryGatingResult(should_continue=False, called_ai=True)
    assert result_active.called_ai is True

    result_inactive = EntryGatingResult(should_continue=True, called_ai=True)
    assert result_inactive.called_ai is True


def test_pacing_budget_calculation():
    """GeminiTelemetry 및 AIProviderTelemetry의 24시간 쿼터 페이싱 예산 계산 검증."""
    from ai_provider import AIProviderTelemetry
    from gemini_telemetry import GeminiTelemetry

    with patch("gemini_telemetry.get_pt_reset_info", return_value={"remaining_sec": 36000}):  # 10시간 = 120 사이클
        with patch.object(GeminiTelemetry, "_api_calls", 200):  # 950 - 200 = 750 / 120 = 6.25회 -> 2개 허용
            pacing = GeminiTelemetry.get_pacing_budget()
            assert pacing["remaining_cycles"] == 120
            assert pacing["allowed_candidates"] == 2
            assert pacing["is_pacing_restricted"] is False

        with patch.object(GeminiTelemetry, "_api_calls", 850):  # 950 - 850 = 100 / 120 = 0.83회 -> 1개로 제한
            pacing = GeminiTelemetry.get_pacing_budget()
            assert pacing["allowed_candidates"] == 1
            assert pacing["is_pacing_restricted"] is True

        with patch.object(GeminiTelemetry, "_api_calls", 960):  # 950 초과 -> 0개 차단
            pacing = GeminiTelemetry.get_pacing_budget()
            assert pacing["allowed_candidates"] == 0
            assert pacing["is_pacing_restricted"] is True


def test_screener_rank_relaxed_cache_synthesis():
    """스크리너 후보 중 40% 이상(8개 중 4개) 유효 캐시 보유 시 API 호출 없이 합성되는지 검증."""
    mock_provider = MagicMock(spec=GeminiProvider)
    mock_provider.name = "gemini"
    mock_provider.exchange = "upbit"
    analyzer = GeminiAnalyzer(api_key="test-key", provider=mock_provider)

    now_ts = time.time()
    # 4개 종목 캐시 사전 주입 (8개 중 4개 = 50% >= 40%)
    GeminiAnalyzer._MARKET_AI_SCORE_CACHE["KRW-A"] = {"ai_rank": 1, "ai_tier": "TIER_1", "ai_score": 90.0, "ai_reason": "캐시A", "cached_at": now_ts}
    GeminiAnalyzer._MARKET_AI_SCORE_CACHE["KRW-B"] = {"ai_rank": 2, "ai_tier": "TIER_1", "ai_score": 85.0, "ai_reason": "캐시B", "cached_at": now_ts}
    GeminiAnalyzer._MARKET_AI_SCORE_CACHE["KRW-C"] = {"ai_rank": 3, "ai_tier": "TIER_2", "ai_score": 70.0, "ai_reason": "캐시C", "cached_at": now_ts}
    GeminiAnalyzer._MARKET_AI_SCORE_CACHE["KRW-D"] = {"ai_rank": 4, "ai_tier": "TIER_2", "ai_score": 65.0, "ai_reason": "캐시D", "cached_at": now_ts}

    candidates = [
        {"market": f"KRW-{c}", "trade_price": 1000.0, "change_rate": 0.02, "acc_trade_price_24h": 1e10}
        for c in ["A", "B", "C", "D", "E", "F", "G", "H"]
    ]

    with patch.object(analyzer, "_call_gemini_json") as mock_call:
        ranked = analyzer.rank_candidate_markets(candidates, btc_regime="NORMAL")
        mock_call.assert_not_called()  # API 호출이 없어야 함!
        assert len(ranked) == 8
        assert ranked[0]["market"] == "KRW-A"


def test_holding_eval_loss_alert_300s_ttl():
    """LOSS_ALERT(-2% 이하) 상태에서도 300초 동안 캐시가 유지되어 재호출되지 않는지 검증."""
    mock_provider = MagicMock(spec=GeminiProvider)
    mock_provider.name = "gemini"
    mock_provider.exchange = "upbit"
    mock_provider.models_for.return_value = ["gemini-3.5-flash-lite"]
    mock_provider.complete_json.return_value = ProviderResult(
        value={"ACTION": "HOLD", "REASON": "테스트 유지", "ADJUSTED_TARGET_PRICE": 1100.0, "ADJUSTED_STOP_LOSS": 900.0, "CONFIDENCE": 80},
        model="gemini-3.5-flash-lite",
    )

    analyzer = GeminiAnalyzer(api_key="test-key", provider=mock_provider)
    sample_candles = [{"opening_price": 1000.0, "high_price": 1010.0, "low_price": 950.0, "trade_price": 970.0, "candle_acc_trade_volume": 10.0}]

    # 평단가 1,000원, 현재가 970원 = -3.0% (LOSS_ALERT)
    res1 = analyzer.evaluate_holding_position(
        market="KRW-TEST",
        current_price=970.0,
        avg_buy_price=1000.0,
        candles=sample_candles,
    )
    assert res1["action"] == "HOLD"
    assert mock_provider.complete_json.call_count == 1

    # 10초 후 재호출 (300초 미만이므로 캐시 적중)
    res2 = analyzer.evaluate_holding_position(
        market="KRW-TEST",
        current_price=970.0,
        avg_buy_price=1000.0,
        candles=sample_candles,
    )
    assert res2["action"] == "HOLD"
    assert mock_provider.complete_json.call_count == 1
