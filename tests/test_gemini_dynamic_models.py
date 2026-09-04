"""
Unit tests for Gemini Dynamic Model Router & Self-Healing Lifecycle Management
"""

import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

import requests

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from gemini_analyzer import GeminiAnalyzer


class GeminiDynamicModelsTests(unittest.TestCase):
    def setUp(self):
        # 테스트 격리를 위해 캐시 및 쿨다운 초기화
        GeminiAnalyzer._CACHED_MODELS = []
        GeminiAnalyzer._MODELS_CACHED_AT = 0.0
        GeminiAnalyzer._MODEL_COOLDOWNS = {}
        GeminiAnalyzer._MODEL_BLACKLIST = {}

    def test_model_priority_sorting(self):
        """Lite 최우선 및 최신 버전 우선순위 정렬 검증"""
        raw_models = [
            "gemini-3.5-flash",
            "gemini-3.8-flash",
            "gemini-3.5-flash-lite",
            "gemini-3.8-flash-lite",
            "gemini-flash-lite-latest",
            "gemini-3.7-flash-lite",
            "gemini-3.7-flash",
            "gemini-flash-latest",
        ]

        sorted_models = sorted(raw_models, key=GeminiAnalyzer._model_priority_key, reverse=True)

        # 1. 1위는 무조건 가장 최신 flash-lite여야 함
        self.assertEqual(sorted_models[0], "gemini-3.8-flash-lite")

        # 2. 상위 4개는 모두 flash-lite 계열이어야 함 (Lite 절대적 최우선)
        lite_models = sorted_models[:4]
        for m in lite_models:
            self.assertIn("flash-lite", m)

        # 3. Lite 내부 순서: 3.8 > 3.7 > 3.5 > latest
        self.assertEqual(
            lite_models,
            [
                "gemini-3.8-flash-lite",
                "gemini-3.7-flash-lite",
                "gemini-3.5-flash-lite",
                "gemini-flash-lite-latest",
            ],
        )

        # 4. 차순위 Flash 내부 순서: 3.8 > 3.7 > 3.5 > latest
        flash_models = sorted_models[4:]
        self.assertEqual(
            flash_models,
            [
                "gemini-3.8-flash",
                "gemini-3.7-flash",
                "gemini-3.5-flash",
                "gemini-flash-latest",
            ],
        )

    @patch("requests.get")
    def test_fetch_available_models_filters_and_sorts(self, mock_get):
        """ListModels API 호출 시 미디어(image, tts) 및 비적합 모델 필터링과 Lite 우선 정렬 검증"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "models": [
                {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
                {"name": "models/gemini-pro", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-3.1-flash-lite-image", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-2.5-flash-preview-tts", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-3.8-flash-lite", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-3.5-flash", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-3.5-flash-lite", "supportedGenerationMethods": ["generateContent"]},
            ]
        }
        mock_get.return_value = mock_resp

        models = GeminiAnalyzer.fetch_available_models(api_key="fake-key")

        # embedding, pro뿐 아니라 이미지/음성 모델 및 일반 Flash(일일 20회 제한)도 완전 배제 확인
        self.assertNotIn("text-embedding-004", models)
        self.assertNotIn("gemini-pro", models)
        self.assertNotIn("gemini-3.1-flash-lite-image", models)
        self.assertNotIn("gemini-2.5-flash-preview-tts", models)
        self.assertNotIn("gemini-3.8-flash", models)
        self.assertNotIn("gemini-3.5-flash", models)

        # 오직 순수 텍스트 Flash-Lite 계열만 선별 및 버전 순 정렬 확인
        self.assertEqual(models, ["gemini-3.8-flash-lite", "gemini-3.5-flash-lite"])

    @patch("requests.get")
    def test_fallback_when_api_fails(self, mock_get):
        """ListModels API 실패 시 기본 Fallback 모델 리스트 안전 반환 검증 (Flash-Lite 전용)"""
        mock_get.side_effect = requests.exceptions.ConnectionError("Network Down")

        models = GeminiAnalyzer.fetch_available_models(api_key="fake-key")
        self.assertEqual(models, GeminiAnalyzer.FALLBACK_MODELS)
        self.assertEqual(models[0], "gemini-3.5-flash-lite")
        for m in models:
            self.assertIn("flash-lite", m)

    def test_auto_blacklist_on_deprecated_model(self):
        """404 Not Found 또는 지원 종료 모델 감지 시 24시간 블랙리스트 등록 검증"""
        analyzer = GeminiAnalyzer(api_key="fake-key")
        GeminiAnalyzer._CACHED_MODELS = ["gemini-3.5-flash-lite", "gemini-3.8-flash-lite"]
        GeminiAnalyzer._MODELS_CACHED_AT = time.time()

        # 수동 블랙리스트 등록 시뮬레이션 (404 발생 상황)
        deprecated_model = "gemini-3.5-flash-lite"
        GeminiAnalyzer._MODEL_BLACKLIST[deprecated_model] = time.time() + 86400.0

        # get_available_models에서 제외되고 3.8-flash-lite만 남는지 검증
        available = analyzer.get_available_models()
        self.assertNotIn(deprecated_model, available)
        self.assertIn("gemini-3.8-flash-lite", available)

    def test_cooldown_candidates_selection(self):
        """쿨다운 중인 모델은 후보에서 제외되고 차순위 모델이 선정되는지 검증"""
        analyzer = GeminiAnalyzer(api_key="fake-key")
        GeminiAnalyzer._CACHED_MODELS = [
            "gemini-3.8-flash-lite",
            "gemini-3.7-flash-lite",
            "gemini-3.5-flash-lite",
        ]
        GeminiAnalyzer._MODELS_CACHED_AT = time.time()

        # 1위인 3.8-flash-lite가 쿨다운에 걸린 상황
        GeminiAnalyzer._MODEL_COOLDOWNS["gemini-3.8-flash-lite"] = time.time() + 300.0

        candidates = analyzer.get_candidate_models(limit=2)
        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0], "gemini-3.7-flash-lite")
        self.assertEqual(candidates[1], "gemini-3.5-flash-lite")

    @patch("requests.get")
    def test_ttl_caching_behavior(self, mock_get):
        """TTL 6시간 동안에는 추가 네트워크 요청 없이 캐시된 모델 재사용 검증"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "models": [{"name": "models/gemini-3.8-flash-lite", "supportedGenerationMethods": ["generateContent"]}]
        }
        mock_get.return_value = mock_resp

        # 첫 번째 호출: 네트워크 조회 발생
        models1 = GeminiAnalyzer.get_available_models(api_key="fake-key")
        self.assertEqual(mock_get.call_count, 1)

        # 두 번째 호출: 캐시 만료 전이므로 네트워크 호출 없이 캐시 반환
        models2 = GeminiAnalyzer.get_available_models(api_key="fake-key")
        self.assertEqual(mock_get.call_count, 1)
        self.assertEqual(models1, models2)

    def test_fallback_models_are_strictly_lite(self):
        """기본 Fallback 모델 목록이 100% Flash-Lite 계열로만 구성되어 있는지 검증 (20 RPD Flash 원천 차단)"""
        for model in GeminiAnalyzer.FALLBACK_MODELS:
            self.assertIn("flash-lite", model.lower())
            self.assertNotIn("-flash\b", model.lower())

    @patch("time.sleep")
    def test_rate_limiting_wait(self, mock_sleep):
        """연속 호출 시 15 RPM을 준수하기 위해 time.sleep으로 지연을 주입하는지 검증"""
        GeminiAnalyzer._LAST_CALL_TS = time.time()
        GeminiAnalyzer._wait_for_rate_limit()
        self.assertTrue(mock_sleep.called)
        sleep_arg = mock_sleep.call_args[0][0]
        self.assertGreater(sleep_arg, 0.0)
        self.assertLessEqual(sleep_arg, GeminiAnalyzer._MIN_CALL_INTERVAL_SEC)


if __name__ == "__main__":
    unittest.main()

