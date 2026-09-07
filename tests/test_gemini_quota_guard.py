"""Unit tests for Gemini Free Tier Quota Guard and Hard Cutoff."""

import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from gemini_analyzer import GeminiAnalyzer
from gemini_telemetry import GeminiTelemetry


class TestGeminiQuotaGuard(unittest.TestCase):
    """Google AI Studio 무료 티어 쿼터 보호 및 하드 컷오프 검증"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        GeminiTelemetry.configure(data_dir=self.temp_dir)
        GeminiTelemetry.reset(persist=True)
        GeminiAnalyzer.clear_caches()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        data_dir = os.path.join(project_root, "data")
        GeminiTelemetry.configure(data_dir=data_dir)
        GeminiAnalyzer.clear_caches()

    def test_can_call_model_dynamic_threshold_flash_and_lite(self):
        """일반 Flash(한도 20회)와 Flash-Lite(한도 500회)의 85% 임계값 기반 선제 차단 검증"""
        # 1. 일반 Flash (한도 20회): 85% = 17회
        self.assertTrue(GeminiTelemetry.can_call_model("gemini-3.5-flash", for_emergency_exit=False))
        # 16회 호출 기록 시 여전히 허용
        for _ in range(16):
            GeminiTelemetry.record_api_success("gemini-3.5-flash", "TEST")
        self.assertTrue(GeminiTelemetry.can_call_model("gemini-3.5-flash", for_emergency_exit=False))
        # 17회 도달 시 차단
        GeminiTelemetry.record_api_success("gemini-3.5-flash", "TEST")
        self.assertFalse(GeminiTelemetry.can_call_model("gemini-3.5-flash", for_emergency_exit=False))
        # 긴급 탈출(95% = 19회) 모드에서는 17회 시 허용
        self.assertTrue(GeminiTelemetry.can_call_model("gemini-3.5-flash", for_emergency_exit=True))

        # 2. Flash-Lite (한도 500회): 85% = 425회
        self.assertTrue(GeminiTelemetry.can_call_model("gemini-3.5-flash-lite", for_emergency_exit=False))
        for _ in range(424):
            GeminiTelemetry.record_api_success("gemini-3.5-flash-lite", "TEST")
        self.assertTrue(GeminiTelemetry.can_call_model("gemini-3.5-flash-lite", for_emergency_exit=False))
        # 425회 도달 시 평시 차단
        GeminiTelemetry.record_api_success("gemini-3.5-flash-lite", "TEST")
        self.assertFalse(GeminiTelemetry.can_call_model("gemini-3.5-flash-lite", for_emergency_exit=False))
        # 긴급 탈출(95% = 475회) 모드에서는 허용
        self.assertTrue(GeminiTelemetry.can_call_model("gemini-3.5-flash-lite", for_emergency_exit=True))

    def test_get_candidate_models_hard_cutoff(self):
        """모든 가용 모델이 85% 쿼터에 도달했을 때 빈 리스트 []를 반환하여 429 요청을 원천 차단하는지 검증"""
        analyzer = GeminiAnalyzer(api_key="test-key")

        with patch.object(GeminiAnalyzer, "get_available_models", return_value=["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]):
            # 초기에는 2개 모델 모두 후보로 반환
            candidates = analyzer.get_candidate_models(limit=2)
            self.assertEqual(len(candidates), 2)

            # 3.5-flash-lite 425회 소진
            for _ in range(425):
                GeminiTelemetry.record_api_success("gemini-3.5-flash-lite", "TEST")
            # 3.5는 빠지고 3.1만 1개 반환
            candidates = analyzer.get_candidate_models(limit=2)
            self.assertEqual(candidates, ["gemini-3.1-flash-lite"])

            # 3.1-flash-lite도 425회 소진
            for _ in range(425):
                GeminiTelemetry.record_api_success("gemini-3.1-flash-lite", "TEST")
            # 모든 모델 쿼터 소진 시 빈 리스트 [] 반환 (하드 컷오프)
            candidates = analyzer.get_candidate_models(limit=2)
            self.assertEqual(candidates, [])

    def test_min_call_interval_is_safe_for_15_rpm(self):
        """_MIN_CALL_INTERVAL_SEC가 6.0초 이상으로 분당 최대 10회 이하로 제한되는지 검증"""
        self.assertGreaterEqual(GeminiAnalyzer._MIN_CALL_INTERVAL_SEC, 6.0)
        max_rpm = 60.0 / GeminiAnalyzer._MIN_CALL_INTERVAL_SEC
        self.assertLessEqual(max_rpm, 10.0)


if __name__ == "__main__":
    unittest.main()
