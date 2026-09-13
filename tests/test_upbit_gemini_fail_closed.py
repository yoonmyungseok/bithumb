"""업비트 Gemini 키 격리 및 Flash-Lite fail-closed 경계를 검증한다."""

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from ai_provider import AIProviderTelemetry, GeminiProvider
from upbit_ai import build_upbit_analyzer, get_upbit_ai_config_block_reason, get_upbit_ai_entry_block_reason


class UpbitGeminiFailClosedTests(unittest.TestCase):
    """업비트도 빗썸과 동일하게 AI 응답 실패 및 Flash-Lite 부재 시 신규 BUY를 차단한다."""

    def setUp(self):
        self.env = dict(os.environ)
        self.temp_dir = tempfile.mkdtemp()
        AIProviderTelemetry.configure(data_dir=self.temp_dir, storage_filename="upbit_entry_safety_test.json")
        AIProviderTelemetry.reset(persist=True)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)

    @patch("ai_provider.requests.post")
    def test_http_failure_blocks_upbit_new_buy(self, mock_post):
        """429 응답은 안전 상태를 닫고 로컬 BUY 승인을 남기지 않아야 한다."""
        mock_post.return_value = MagicMock(status_code=429)
        provider = GeminiProvider("test-key")

        result = provider.complete_json(
            "분석", ["gemini-test"], {"type": "object", "required": [], "properties": {}},
            context="KRW-TEST", timeout=1.0, max_tokens=100,
        )

        self.assertIsNone(result.value)
        self.assertTrue(provider.is_entry_fail_closed)
        self.assertTrue(AIProviderTelemetry.snapshot("upbit")["entry_safety"]["entry_blocked"])
        block_reason = AIProviderTelemetry.get_entry_block_reason("upbit")
        self.assertTrue(block_reason)
        self.assertIn("업비트", block_reason)
        self.assertNotIn("빗썸", block_reason)

    def test_upbit_uses_only_dedicated_key_and_ignores_shared_key(self):
        """업비트는 UPBIT_GEMINI_API_KEY만 사용하며 공용 GEMINI_API_KEY는 무시해야 한다."""
        # 1. 공용 키만 있고 업비트 전용 키가 없는 경우 -> 신규 BUY 차단
        os.environ["GEMINI_API_KEY"] = "shared-key-must-not-be-used"
        os.environ.pop("UPBIT_GEMINI_API_KEY", None)

        self.assertTrue(get_upbit_ai_config_block_reason())
        self.assertIn("UPBIT_GEMINI_API_KEY", get_upbit_ai_config_block_reason())
        self.assertIsNone(build_upbit_analyzer())
        entry_reason = get_upbit_ai_entry_block_reason()
        self.assertIn("UPBIT_GEMINI_API_KEY", entry_reason)

        # 2. 업비트 전용 키가 설정된 경우 -> 정상 해제 및 분석기 생성
        os.environ["UPBIT_GEMINI_API_KEY"] = "upbit-dedicated-key"
        self.assertEqual(get_upbit_ai_config_block_reason(), "")
        analyzer = build_upbit_analyzer()
        self.assertIsNotNone(analyzer)
        self.assertEqual(analyzer.provider.exchange, "upbit")
        self.assertEqual(analyzer.provider.api_key, "upbit-dedicated-key")

    @patch("ai_provider.requests.get")
    def test_upbit_requires_concrete_flash_lite_models(self, mock_get):
        """업비트 신규 BUY도 빗썸과 동일하게 구체 Flash-Lite(3.5 및 3.1) 순차 모델만 허용한다."""
        # 1. 3.5와 3.1 모두 존재하는 경우 -> 3.5 최우선 후 3.1 순차 반환
        response_both = MagicMock(status_code=200)
        response_both.json.return_value = {"models": [
            {"name": "models/gemini-3.5-flash-lite", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-3.1-flash-lite", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-2.5-pro", "supportedGenerationMethods": ["generateContent"]},
        ]}
        mock_get.return_value = response_both

        provider = GeminiProvider("upbit-test-key")
        models = provider.models_for("trading")
        self.assertEqual(models, ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"])

        # 2. 3.1 단독 존재하는 경우 -> 3.1 반환
        response_31 = MagicMock(status_code=200)
        response_31.json.return_value = {"models": [
            {"name": "models/gemini-3.1-flash-lite", "supportedGenerationMethods": ["generateContent"]},
        ]}
        mock_get.return_value = response_31
        provider_31 = GeminiProvider("upbit-test-key")
        self.assertEqual(provider_31.models_for("trading"), ["gemini-3.1-flash-lite"])

        # 3. 허용된 Flash-Lite 모델이 없거나 latest 별칭만 있는 경우 -> required_model_unavailable 차단
        response_no_lite = MagicMock(status_code=200)
        response_no_lite.json.return_value = {"models": [
            {"name": "models/gemini-flash-lite-latest", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-2.5-pro", "supportedGenerationMethods": ["generateContent"]},
        ]}
        mock_get.return_value = response_no_lite
        provider_none = GeminiProvider("upbit-test-key")
        self.assertEqual(provider_none.models_for("trading"), [])
        safety = AIProviderTelemetry.snapshot("upbit")["entry_safety"]
        self.assertTrue(safety["entry_blocked"])
        self.assertEqual(safety["reason"], "required_model_unavailable")

    @patch("ai_provider.requests.get")
    def test_upbit_list_models_failure_blocks_new_buy(self, mock_get):
        """모델 목록 API 429 에러 시 업비트 신규 BUY를 fail-closed 차단한다."""
        mock_get.return_value = MagicMock(status_code=429)
        provider = GeminiProvider("upbit-test-key")
        self.assertEqual(provider.models_for("trading"), [])
        safety = AIProviderTelemetry.snapshot("upbit")["entry_safety"]
        self.assertTrue(safety["entry_blocked"])
        self.assertEqual(safety["http_status"], 429)


if __name__ == "__main__":
    unittest.main()
