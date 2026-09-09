"""
Unit tests for macro_regime timeout safety, thinking budget control, and entry gating isolation.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from ai_provider import AIProviderTelemetry, GeminiProvider, BithumbGeminiProvider
from gemini_analyzer import GeminiAnalyzer, MACRO_JSON_SCHEMA


class MacroRegimeTimeoutSafetyTests(unittest.TestCase):
    def setUp(self):
        AIProviderTelemetry.configure()
        with AIProviderTelemetry._lock:
            AIProviderTelemetry._entry_safety.clear()
        GeminiAnalyzer.clear_caches()

    @patch("ai_provider.requests.post")
    def test_macro_regime_timeout_does_not_block_upbit_entry(self, mock_post):
        """거시 레짐(macro_regime) 진단 타임아웃 실패 시 신규 BUY 진입 게이트가 차단되지 않는다."""
        import requests
        mock_post.side_effect = requests.exceptions.Timeout("Gemini API timeout")

        provider = GeminiProvider("upbit-test-key")
        result = provider.complete_json(
            "거시 진단 프롬프트",
            ["gemini-3.8-flash", "gemini-3.7-flash"],
            MACRO_JSON_SCHEMA,
            context="macro_regime",
            timeout=15.0,
            max_tokens=500,
        )

        self.assertIsNone(result.value)
        self.assertEqual(result.error_kind, "timeout")
        # macro_regime 컨텍스트 실패는 신규 BUY 진입 게이트를 오염시키지 않아야 한다.
        self.assertEqual(AIProviderTelemetry.get_entry_block_reason("upbit"), "")

    @patch("ai_provider.requests.post")
    def test_trading_timeout_strictly_blocks_upbit_entry(self, mock_post):
        """반면 신규 BUY 직접 판정(trading, screener_rank) 타임아웃은 엄격하게 신규 BUY를 fail-closed 차단한다."""
        import requests
        mock_post.side_effect = requests.exceptions.Timeout("Gemini API timeout")

        provider = GeminiProvider("upbit-test-key")
        result = provider.complete_json(
            "종목 매수 분석",
            ["gemini-3.5-flash-lite"],
            {"type": "object"},
            context="trading",
            timeout=15.0,
            max_tokens=500,
        )

        self.assertIsNone(result.value)
        self.assertEqual(result.error_kind, "timeout")
        block_reason = AIProviderTelemetry.get_entry_block_reason("upbit")
        self.assertIn("업비트 Gemini 분석 장애(timeout, trading)로 신규 BUY를 차단합니다.", block_reason)

    @patch("ai_provider.requests.post")
    def test_thinking_budget_zero_injected_for_reasoning_models(self, mock_post):
        """gemini-3.7-flash 등 추론 모델 호출 시 thinkingBudget: 0 설정이 주입되어 불필요한 생각 지연을 차단한다."""
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "candidates": [{
                "content": {
                    "parts": [{
                        "text": '{"regime": "NORMAL", "risk_score": 35, "recommended_cash_ratio": 0.3, "summary": "정상", "action_guideline": "운용"}'
                    }]
                }
            }]
        }
        mock_post.return_value = mock_resp

        provider = GeminiProvider("upbit-test-key")
        result = provider.complete_json(
            "프롬프트",
            ["gemini-3.7-flash"],
            MACRO_JSON_SCHEMA,
            context="macro_regime",
            timeout=15.0,
            max_tokens=500,
        )

        self.assertIsNotNone(result.value)
        call_kwargs = mock_post.call_args.kwargs
        payload = call_kwargs.get("json", {})
        gen_config = payload.get("generationConfig", {})
        self.assertIn("thinkingConfig", gen_config)
        self.assertEqual(gen_config["thinkingConfig"].get("thinkingBudget"), 0)

    def test_get_macro_candidate_models_includes_flash_lite_fallback(self):
        """get_macro_candidate_models(limit=3) 호출 시 일반 Flash뿐만 아니라 신속 폴백용 Flash-Lite가 포함된다."""
        analyzer = GeminiAnalyzer(api_key="test-key")
        candidates = analyzer.get_macro_candidate_models(limit=3)

        self.assertLessEqual(len(candidates), 3)
        # 상위 1, 2순위는 일반 Flash (3.8, 3.7 등)
        self.assertTrue(any("3.8-flash" in m or "3.7-flash" in m for m in candidates[:2]))
        # 순차 폴백 목록에 flash-lite가 포함되어 타임아웃 연쇄 발생을 방지
        self.assertTrue(any("flash-lite" in m for m in candidates))


if __name__ == "__main__":
    unittest.main()
