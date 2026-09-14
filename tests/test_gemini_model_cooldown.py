"""
Unit tests for Gemini AI 429 Cooldown & Dynamic Priority Promotion.
Verifies that 429 or quota-exhausted models are skipped without HTTP requests,
and secondary models (e.g. gemini-3.1-flash-lite) are directly called.
"""

import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from ai_provider import BaseGeminiProvider, GeminiProvider, BithumbGeminiProvider, AIProviderTelemetry
from gemini_analyzer import GeminiAnalyzer
from gemini_telemetry import GeminiTelemetry


class TestGeminiModelCooldown(unittest.TestCase):
    """Gemini 429 수신 시 자동 쿨다운 등록 및 차순위 모델 자동 승격 검증"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        GeminiTelemetry.configure(data_dir=self.temp_dir)
        GeminiTelemetry.reset(persist=True)
        AIProviderTelemetry.reset(persist=True)
        GeminiAnalyzer.clear_caches()
        BaseGeminiProvider.clear_cooldowns()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        GeminiAnalyzer.clear_caches()
        BaseGeminiProvider.clear_cooldowns()

    @patch("requests.post")
    def test_429_triggers_cooldown_and_skips_on_next_call(self, mock_post):
        """
        3.5 모델에서 429 수신 시 즉시 쿨다운 등록되고,
        다음 complete_json 호출 시 3.5에 대한 HTTP 요청을 건너뛰고 3.1로 다이렉트 호출되는지 검증
        """
        provider = GeminiProvider(api_key="test-key")

        # 1차 호출: 3.5 호출 시 429 에러 -> 3.1 폴백 성공
        resp_429 = MagicMock(status_code=429, headers={"Retry-After": "60"})
        resp_429.json.return_value = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "Quota exceeded"}}
        resp_429.text = '{"error": {"code": 429, "message": "Quota exceeded"}}'

        resp_200 = MagicMock(status_code=200)
        resp_200.json.return_value = {
            "candidates": [
                {"content": {"parts": [{"text": '{"action": "BUY", "confidence": 0.85, "reason": "3.1 fallback"}'}]}}
            ]
        }
        mock_post.side_effect = [resp_429, resp_200]

        res1 = provider.complete_json(
            prompt="analyze",
            models=["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"],
            schema={"type": "object", "properties": {"action": {"type": "string"}}},
            context="trading",
            timeout=5.0,
            max_tokens=100,
        )

        # 3.1 모델로 정상 성공 확인
        self.assertIsNotNone(res1.value)
        self.assertEqual(res1.model, "gemini-3.1-flash-lite")
        self.assertEqual(mock_post.call_count, 2)

        # 3.5 모델이 쿨다운 상태로 등록되었는지 확인
        self.assertTrue(provider.is_model_cooling_down("gemini-3.5-flash-lite"))

        # 2차 호출: 3.5는 쿨다운 중이므로 HTTP 요청을 아예 건너뛰고 3.1로 즉시 1회만 호출해야 함
        mock_post.reset_mock()
        mock_post.side_effect = [resp_200]

        res2 = provider.complete_json(
            prompt="analyze2",
            models=["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"],
            schema={"type": "object", "properties": {"action": {"type": "string"}}},
            context="trading",
            timeout=5.0,
            max_tokens=100,
        )

        # 3.1 모델로 즉시 성공하였고, HTTP 호출은 단 1회(3.1만 호출)여야 함
        self.assertIsNotNone(res2.value)
        self.assertEqual(res2.model, "gemini-3.1-flash-lite")
        self.assertEqual(mock_post.call_count, 1)
        first_call_url = mock_post.call_args_list[0][0][0]
        self.assertIn("gemini-3.1-flash-lite", first_call_url)
        self.assertNotIn("gemini-3.5-flash-lite", first_call_url)

    def test_quota_exhausted_skips_model_in_models_for(self):
        """
        GeminiTelemetry에서 3.5 모델 쿼터가 85%(425회)에 도달하면,
        models_for("trading")에서 3.5가 사전 제외되고 3.1만 반환되는지 검증
        """
        provider = GeminiProvider(api_key="test-key")
        provider._models = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]

        # 3.5 모델의 호출 횟수를 425회로 기록 (85% 임계값)
        for _ in range(425):
            GeminiTelemetry.record_api_success("gemini-3.5-flash-lite", "trading")

        # models_for 호출 시 3.5는 제외되고 3.1만 1순위로 반환되어야 함
        active_models = provider.models_for("trading", for_emergency_exit=False)
        self.assertEqual(active_models, ["gemini-3.1-flash-lite"])

    def test_all_models_quota_exhausted_returns_empty_and_fails_closed(self):
        """
        3.5와 3.1 모두 쿼터 소진 시 models_for("trading")이 빈 리스트 []를 반환하여
        신규 BUY가 안전하게 차단되는지 검증
        """
        provider = GeminiProvider(api_key="test-key")
        provider._models = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]

        # 3.5 및 3.1 모델 모두 425회 소진
        for _ in range(425):
            GeminiTelemetry.record_api_success("gemini-3.5-flash-lite", "trading")
            GeminiTelemetry.record_api_success("gemini-3.1-flash-lite", "trading")

        active_models = provider.models_for("trading", for_emergency_exit=False)
        self.assertEqual(active_models, [])

        # analyzer의 신규 BUY 분석 호출 시 fail-closed 차단 확인
        analyzer = GeminiAnalyzer(provider=provider)
        candle = {
            "opening_price": 100000000.0,
            "high_price": 100000000.0,
            "low_price": 100000000.0,
            "trade_price": 100000000.0,
            "candle_acc_trade_volume": 10.0,
        }
        res = analyzer.analyze(
            market="KRW-BTC",
            current_price=100000000,
            candles=[candle],
            krw_balance=1000000,
            coin_balance=0.0,
            avg_buy_price=0.0,
        )
        self.assertEqual(res["action"], "HOLD")
        self.assertEqual(res["status"], "PAUSE")
        self.assertTrue("가용 모델 없음" in res["reason"] or "쿼터" in res["reason"])
        self.assertIn("차단", res["reason"])

    def test_exchange_isolation_between_upbit_and_bithumb(self):
        """
        업비트의 3.5 쿨다운이 빗썸 Provider의 3.5 쿨다운에 영향을 주지 않는지 검증
        """
        upbit_provider = GeminiProvider(api_key="upbit-key")
        bithumb_provider = BithumbGeminiProvider(api_key="bithumb-key")

        # 업비트에 쿨다운 설정
        upbit_provider.set_model_cooldown("gemini-3.5-flash-lite", duration_sec=300.0, reason="Upbit 429")

        # 업비트는 쿨다운 상태여야 함
        self.assertTrue(upbit_provider.is_model_cooling_down("gemini-3.5-flash-lite"))

        # 빗썸은 쿨다운 상태가 아니어야 함 (완벽 격리)
        self.assertFalse(bithumb_provider.is_model_cooling_down("gemini-3.5-flash-lite"))

    def test_cooldown_expiration(self):
        """쿨다운 시간이 지나면 자동으로 해제되어 정상 복귀하는지 검증"""
        provider = GeminiProvider(api_key="test-key")

        # 0.05초 쿨다운 설정
        provider.set_model_cooldown("gemini-3.5-flash-lite", duration_sec=0.05, reason="Short cooldown")
        self.assertTrue(provider.is_model_cooling_down("gemini-3.5-flash-lite"))

        # 0.1초 대기 후 만료 확인
        time.sleep(0.1)
        self.assertFalse(provider.is_model_cooling_down("gemini-3.5-flash-lite"))


if __name__ == "__main__":
    unittest.main()
