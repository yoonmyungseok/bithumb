"""Gemini/Groq 모델 체인 복원력, 시간 예산 상한 및 프롬프트 길이 보호 단위 테스트."""

import itertools
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ai_provider import BaseGeminiProvider
from groq_provider import DEFAULT_GROQ_MODELS, GroqProvider, _prepare_groq_prompt


class TestAIProviderResilience(unittest.TestCase):
    def setUp(self):
        BaseGeminiProvider.clear_cooldowns()

    def tearDown(self):
        BaseGeminiProvider.clear_cooldowns()

    def test_gemini_timeout_registers_600s_cooldown_and_stops_on_budget(self):
        """Gemini 타임아웃 시 600초 쿨다운을 등록하고, 시간 예산 초과 시 추가 폴백 호출을 중단한다."""
        provider = BaseGeminiProvider("dummy-key")
        provider.exchange = "upbit"

        with patch("ai_provider.requests.post", side_effect=requests.exceptions.Timeout("read timeout")) as mock_post, \
             patch("ai_provider.time.monotonic", side_effect=itertools.chain([0.0, 0.0], itertools.repeat(25.0))):
            res = provider.complete_json(
                prompt="test",
                models=["gemini-3.8-flash", "gemini-3.5-flash-lite"],
                schema={"required": ["action"]},
                context="macro",
                timeout=10.0,
                max_tokens=256,
            )

        self.assertIsNone(res.value)
        self.assertEqual(res.error_kind, "timeout")
        # 첫 모델 타임아웃 후 deadline(22.0s) 초과로 두 번째 모델 호출 없이 중단
        self.assertEqual(mock_post.call_count, 1)
        self.assertTrue(provider.is_model_cooling_down("gemini-3.8-flash"))

    def test_groq_default_models_exclude_deprecated_and_short_context_models(self):
        """Groq 기본 모델 목록에서 404 종료 모델 및 400 컨텍스트 초과 모델을 제외한다."""
        for bad in ("groq/compound", "groq/compound-mini", "allam-2-7b"):
            self.assertNotIn(bad, DEFAULT_GROQ_MODELS)
        self.assertIn("openai/gpt-oss-120b", DEFAULT_GROQ_MODELS)

    def test_groq_prompt_truncation_prevents_oversized_payload(self):
        """과도하게 긴 프롬프트는 앞뒤 핵심을 유지한 채 최대 길이 이내로 축약한다."""
        long_prompt = "HEAD_" + ("X" * 30000) + "_TAIL"
        trimmed = _prepare_groq_prompt(long_prompt, max_chars=1000)
        self.assertLessEqual(len(trimmed), 1000)
        self.assertTrue(trimmed.startswith("HEAD_"))
        self.assertTrue(trimmed.endswith("_TAIL"))
        self.assertIn("컨텍스트 길이 제한 보호", trimmed)

    def test_groq_complete_text_applies_prompt_truncation(self):
        """GroqProvider.complete_text 호출 시 실제 전송 payload가 축약된 프롬프트를 사용한다."""
        provider = GroqProvider(api_key="dummy-groq-key")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"choices": [{"message": {"content": "ok"}}]}

        with patch("groq_provider.requests.post", return_value=mock_resp) as mock_post:
            res = provider.complete_text("A" * 25000)

        self.assertTrue(res.success)
        sent_user_content = mock_post.call_args.kwargs["json"]["messages"][1]["content"]
        self.assertLessEqual(len(sent_user_content), 18000)


if __name__ == "__main__":
    unittest.main()
