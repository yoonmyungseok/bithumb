"""업비트 Gemini 장애가 신규 BUY를 열지 않는지 검증한다."""

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from ai_provider import AIProviderTelemetry, GeminiProvider


class UpbitGeminiFailClosedTests(unittest.TestCase):
    """업비트도 빗썸과 동일하게 AI 응답 실패에서 신규 BUY를 차단한다."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        AIProviderTelemetry.configure(data_dir=self.temp_dir, storage_filename="upbit_entry_safety_test.json")
        AIProviderTelemetry.reset(persist=True)

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
        self.assertTrue(AIProviderTelemetry.get_entry_block_reason("upbit"))


if __name__ == "__main__":
    unittest.main()
