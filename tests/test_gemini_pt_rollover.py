"""
Unit tests for PT (Pacific Time) Midnight Reset & Per-Model (3.5 / 3.1 Flash-Lite) Quotas
"""

import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from gemini_telemetry import (
    GeminiTelemetry,
    get_pt_today_str,
    get_pt_reset_info,
    canonical_model_name,
    PT_TZ,
)


class TestGeminiPTRollover(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        GeminiTelemetry.configure(data_dir=self.temp_dir)
        GeminiTelemetry.reset(persist=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        data_dir = os.path.join(project_root, "data")
        GeminiTelemetry.configure(data_dir=data_dir)

    def test_canonical_model_name(self):
        """다양한 모델 식별자가 3.5 / 3.1 표준 모델명으로 올바르게 정규화되는지 검증"""
        self.assertEqual(canonical_model_name("gemini-3.5-flash-lite"), "gemini-3.5-flash-lite")
        self.assertEqual(canonical_model_name("models/gemini-3.5-flash-lite-001"), "gemini-3.5-flash-lite")
        self.assertEqual(canonical_model_name("gemini-3.1-flash-lite"), "gemini-3.1-flash-lite")
        self.assertEqual(canonical_model_name("gemini-3.1-flash-lite-preview"), "gemini-3.1-flash-lite")
        self.assertEqual(canonical_model_name("unknown-model"), "gemini-3.5-flash-lite")

    def test_pt_reset_info(self):
        """PT 자정 기준 리셋 시간 및 KST 환산 정보 산출 검증"""
        info = get_pt_reset_info()
        self.assertIn("reset_time_kst", info)
        # PDT(서머타임)인 경우 16:00 KST, PST(해제)인 경우 17:00 KST
        self.assertIn(info["reset_time_kst"], ("16:00 KST", "17:00 KST"))
        self.assertGreaterEqual(info["remaining_sec"], 0)
        self.assertLessEqual(info["remaining_sec"], 86400)
        self.assertIn("후 리셋", info["remaining_str"])
        self.assertIn("America/Los_Angeles", info["timezone"])

    def test_per_model_telemetry_counting(self):
        """3.5와 3.1 모델 각각의 호출 및 소진율이 분리 집계되는지 검증"""
        # 3.5 모델 10회 성공, 1회 429
        for _ in range(10):
            GeminiTelemetry.record_api_success("gemini-3.5-flash-lite", "KRW-BTC")
        GeminiTelemetry.record_rate_limited("gemini-3.5-flash-lite", "KRW-BTC")

        # 3.1 모델 5회 성공
        for _ in range(5):
            GeminiTelemetry.record_api_success("gemini-3.1-flash-lite", "KRW-ETH")

        snap = GeminiTelemetry.snapshot().to_dict()
        self.assertEqual(snap["api_calls"], 16)
        self.assertEqual(snap["api_success"], 15)
        self.assertEqual(snap["rate_limited"], 1)

        models = snap["models"]
        # 3.5 통계 검증
        self.assertEqual(models["gemini-3.5-flash-lite"]["calls"], 11)
        self.assertEqual(models["gemini-3.5-flash-lite"]["success"], 10)
        self.assertEqual(models["gemini-3.5-flash-lite"]["rate_limited"], 1)
        self.assertEqual(models["gemini-3.5-flash-lite"]["quota_limit"], 500)
        self.assertEqual(models["gemini-3.5-flash-lite"]["quota_used_pct"], 2.2)

        # 3.1 통계 검증
        self.assertEqual(models["gemini-3.1-flash-lite"]["calls"], 5)
        self.assertEqual(models["gemini-3.1-flash-lite"]["success"], 5)
        self.assertEqual(models["gemini-3.1-flash-lite"]["rate_limited"], 0)
        self.assertEqual(models["gemini-3.1-flash-lite"]["quota_limit"], 500)
        self.assertEqual(models["gemini-3.1-flash-lite"]["quota_used_pct"], 1.0)

    def test_can_call_model_guard(self):
        """특정 모델이 450회(일반) 또는 490회(긴급) 도달 시 개별 가드가 동작하는지 검증"""
        GeminiTelemetry._by_model["gemini-3.5-flash-lite"]["calls"] = 450
        GeminiTelemetry._by_model["gemini-3.1-flash-lite"]["calls"] = 100

        # 3.5 모델은 일반 매수 분석 차단, 긴급 탈출은 허용
        self.assertFalse(GeminiTelemetry.can_call_model("gemini-3.5-flash-lite", for_emergency_exit=False))
        self.assertTrue(GeminiTelemetry.can_call_model("gemini-3.5-flash-lite", for_emergency_exit=True))

        # 3.1 모델은 둘 다 허용
        self.assertTrue(GeminiTelemetry.can_call_model("gemini-3.1-flash-lite", for_emergency_exit=False))
        self.assertTrue(GeminiTelemetry.can_call_model("gemini-3.1-flash-lite", for_emergency_exit=True))

        # 3.5 모델이 490회에 도달하면 긴급 탈출도 차단
        GeminiTelemetry._by_model["gemini-3.5-flash-lite"]["calls"] = 490
        self.assertFalse(GeminiTelemetry.can_call_model("gemini-3.5-flash-lite", for_emergency_exit=True))

    def test_pt_midnight_rollover(self):
        """PT 자정 날짜 변경 시 모델별 및 전체 카운터가 원자적으로 초기화되는지 검증"""
        GeminiTelemetry.record_api_success("gemini-3.5-flash-lite", "KRW-BTC")
        self.assertEqual(GeminiTelemetry.snapshot().api_calls, 1)

        # 다음 날짜로 모의하여 롤오버 트리거
        tomorrow_pt = (datetime.now(PT_TZ) + timedelta(days=1)).strftime("%Y-%m-%d")
        with patch("gemini_telemetry.get_pt_today_str", return_value=tomorrow_pt):
            snap = GeminiTelemetry.snapshot().to_dict()
            self.assertEqual(snap["date"], tomorrow_pt)
            self.assertEqual(snap["api_calls"], 0)
            self.assertEqual(snap["models"]["gemini-3.5-flash-lite"]["calls"], 0)
            self.assertEqual(snap["models"]["gemini-3.1-flash-lite"]["calls"], 0)


if __name__ == "__main__":
    unittest.main()
