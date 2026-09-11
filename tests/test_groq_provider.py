import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from groq_provider import DEFAULT_GROQ_MODELS, GroqProvider, GroqResult


class TestGroqProvider(unittest.TestCase):
    def setUp(self):
        # 환경변수 격리
        self._orig_groq = os.environ.get("GROQ_API_KEY")
        self._orig_bt_groq = os.environ.get("BITHUMB_GROQ_API_KEY")
        self._orig_up_groq = os.environ.get("UPBIT_GROQ_API_KEY")
        for k in ["GROQ_API_KEY", "BITHUMB_GROQ_API_KEY", "UPBIT_GROQ_API_KEY"]:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in [
            ("GROQ_API_KEY", self._orig_groq),
            ("BITHUMB_GROQ_API_KEY", self._orig_bt_groq),
            ("UPBIT_GROQ_API_KEY", self._orig_up_groq),
        ]:
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

    def test_availability_and_key_resolution(self):
        # 키 없을 때
        provider = GroqProvider(exchange_scope="bithumb")
        self.assertFalse(provider.is_available)
        res = provider.complete_json("test prompt")
        self.assertFalse(res.success)
        self.assertEqual(res.error_kind, "API_KEY_MISSING")

        # 거래소별 전용 키 확인
        os.environ["BITHUMB_GROQ_API_KEY"] = "gsk_bithumb_key"
        bt_provider = GroqProvider(exchange_scope="bithumb")
        self.assertTrue(bt_provider.is_available)
        self.assertEqual(bt_provider._api_key, "gsk_bithumb_key")

        # 업비트 격리 확인
        up_provider = GroqProvider(exchange_scope="upbit")
        self.assertFalse(up_provider.is_available)
        os.environ["UPBIT_GROQ_API_KEY"] = "gsk_upbit_key"
        up_provider2 = GroqProvider(exchange_scope="upbit")
        self.assertTrue(up_provider2.is_available)
        self.assertEqual(up_provider2._api_key, "gsk_upbit_key")

    @patch("requests.post")
    def test_complete_json_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": '{"regime": "NORMAL", "risk_score": 40, "recommended_cash_ratio": 0.3, "market_summary": "정상 안정세", "action_guideline": "분할 매매"}'
                    }
                }
            ]
        }
        mock_post.return_value = mock_resp

        provider = GroqProvider(api_key="gsk_dummy_key", exchange_scope="bithumb")
        schema = {
            "required": ["regime", "risk_score", "recommended_cash_ratio", "market_summary", "action_guideline"]
        }
        result = provider.complete_json("analyze btc", schema=schema)

        self.assertTrue(result.success)
        self.assertIsInstance(result.value, dict)
        self.assertEqual(result.value["regime"], "NORMAL")
        self.assertEqual(result.value["risk_score"], 40)
        self.assertEqual(result.model, DEFAULT_GROQ_MODELS[0])
        self.assertEqual(provider.success_calls, 1)

    @patch("requests.post")
    def test_model_fallback_on_429(self, mock_post):
        # 첫 번째 모델 429 -> 두 번째 모델 성공
        resp_429 = MagicMock()
        resp_429.status_code = 429

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {
            "choices": [{"message": {"content": '{"regime": "BULL_TREND"}'}}]
        }

        mock_post.side_effect = [resp_429, resp_200]

        provider = GroqProvider(api_key="gsk_dummy_key")
        result = provider.complete_json("prompt", models=["model-1", "model-2"])

        self.assertTrue(result.success)
        self.assertEqual(result.model, "model-2")
        self.assertEqual(result.value.get("regime"), "BULL_TREND")
        self.assertEqual(provider.total_calls, 2)
        self.assertEqual(provider.failed_calls, 1)
        self.assertEqual(provider.success_calls, 1)


if __name__ == "__main__":
    unittest.main()
