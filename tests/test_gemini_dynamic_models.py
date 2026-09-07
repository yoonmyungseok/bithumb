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
from gemini_telemetry import GeminiTelemetry


class GeminiDynamicModelsTests(unittest.TestCase):
    def setUp(self):
        # 테스트 격리를 위해 전체 AI 캐시, 쿨다운, 블랙리스트, 텔레메트리 초기화
        GeminiAnalyzer.clear_caches()
        GeminiTelemetry.reset_for_test()

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

    def test_briefing_model_priority_sorting(self):
        """브리핑 전용 정렬: gemini-3.8-flash 최우선, 최신 Flash 순차 정렬 및 Pro 모델 배제 검증"""
        raw_models = [
            "gemini-3.5-flash",
            "gemini-3.8-flash",
            "gemini-3.6-flash",
            "gemini-3-flash",
            "gemini-3.5-flash-lite",
            "gemini-3.1-flash-lite",
            "gemini-2.5-pro",
            "gemini-pro",
            "gemini-3.7-flash",
        ]

        sorted_models = sorted(raw_models, key=GeminiAnalyzer._briefing_model_priority_key, reverse=True)

        # 1. 최우선 1위는 반드시 gemini-3.8-flash
        self.assertEqual(sorted_models[0], "gemini-3.8-flash")

        # 2. 상위권은 사용 가능한 최신 Flash 모델 순서여야 함 (3.8 > 3.7 > 3.6 > 3.5 > 3)
        self.assertEqual(sorted_models[1], "gemini-3.7-flash")
        self.assertEqual(sorted_models[2], "gemini-3.6-flash")
        self.assertEqual(sorted_models[3], "gemini-3.5-flash")
        self.assertEqual(sorted_models[4], "gemini-3-flash")

        # 3. 그 다음은 Lite 계열 순서
        self.assertEqual(sorted_models[5], "gemini-3.5-flash-lite")
        self.assertEqual(sorted_models[6], "gemini-3.1-flash-lite")

        # 4. Pro 모델은 tier -1로 맨 뒤에 배치됨
        self.assertIn(sorted_models[7], ["gemini-2.5-pro", "gemini-pro"])
        self.assertIn(sorted_models[8], ["gemini-2.5-pro", "gemini-pro"])

    @patch("requests.get")
    def test_fetch_briefing_models_excludes_pro_and_media(self, mock_get):
        """ListModels API로부터 조회 시 pro 모델 및 미디어 모델이 브리핑 목록에서 완전히 배제되는지 검증"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "models": [
                {"name": "models/gemini-2.5-pro", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-pro", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-3.7-flash", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-3.6-flash", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-3.5-flash", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-3-flash", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-3.5-flash-lite", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-3.1-flash-lite-image", "supportedGenerationMethods": ["generateContent"]},
            ]
        }
        mock_get.return_value = mock_resp

        models = GeminiAnalyzer.fetch_available_briefing_models(api_key="fake-key")

        # Pro 및 미디어 모델 완전 배제 확인
        self.assertNotIn("gemini-2.5-pro", models)
        self.assertNotIn("gemini-pro", models)
        self.assertNotIn("gemini-3.1-flash-lite-image", models)

        # 1위는 3.8-flash
        self.assertEqual(models[0], "gemini-3.8-flash")
        # Flash 모델 순서 유지 확인
        self.assertEqual(
            models[:5],
            [
                "gemini-3.8-flash",
                "gemini-3.7-flash",
                "gemini-3.6-flash",
                "gemini-3.5-flash",
                "gemini-3-flash",
            ],
        )

    def test_briefing_candidate_models_fallback(self):
        """3.8-flash 쿨다운 시 차순위 최신 모델로 순차 폴백되는지 검증 (Pro 모델은 절대 포함 안 됨)"""
        analyzer = GeminiAnalyzer(api_key="fake-key")
        GeminiAnalyzer._CACHED_BRIEFING_MODELS = [
            "gemini-3.8-flash",
            "gemini-3.7-flash",
            "gemini-3.6-flash",
            "gemini-3.5-flash",
        ]
        GeminiAnalyzer._BRIEFING_MODELS_CACHED_AT = time.time()

        # 정상 상태: 1위는 3.8-flash
        candidates = analyzer.get_briefing_candidate_models(limit=3)
        self.assertEqual(candidates[0], "gemini-3.8-flash")

        # 3.8-flash가 일시적 쿨다운/429인 경우
        GeminiAnalyzer._MODEL_COOLDOWNS["gemini-3.8-flash"] = time.time() + 300.0

        candidates_fallback = analyzer.get_briefing_candidate_models(limit=3)
        # 차순위인 3.7-flash가 1위로 자동 승격
        self.assertEqual(candidates_fallback[0], "gemini-3.7-flash")
        self.assertEqual(candidates_fallback[1], "gemini-3.6-flash")

        # 반환된 목록에 pro 모델이 일체 없는지 확인
        for c in candidates_fallback:
            self.assertNotIn("pro", c.lower())

    @patch("requests.post")
    def test_generate_market_briefing_calls_highest_priority_model(self, mock_post):
        """09:00 시황 브리핑 생성 시 gemini-3.8-flash로 우선 호출되는지 검증"""
        analyzer = GeminiAnalyzer(api_key="fake-key")
        GeminiAnalyzer._CACHED_BRIEFING_MODELS = [
            "gemini-3.8-flash",
            "gemini-3.5-flash",
        ]
        GeminiAnalyzer._BRIEFING_MODELS_CACHED_AT = time.time()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "candidates": [
                {"content": {"parts": [{"text": "• [거시 시황]: BTC 강세 지속\n• [계좌 진단]: 안전 운용 중\n• [전략 제언]: 관망 유지"}]}}
            ]
        }
        mock_post.return_value = mock_resp

        result = analyzer.generate_market_briefing(
            exchange_name="빗썸",
            total_equity=1000000.0,
            daily_pnl_krw=10000.0,
            daily_pnl_pct=1.0,
            held_positions_desc="KRW-BTC",
            macro_diag={"regime": "NORMAL", "risk_score": 30, "summary": "정상"},
            fng_desc="탐욕",
        )

        self.assertIn("[거시 시황]", result)
        # mock_post 호출된 url에 gemini-3.8-flash가 포함되어 있는지 확인
        called_url = mock_post.call_args[0][0]
        self.assertIn("gemini-3.8-flash", called_url)
        # payload에 maxOutputTokens가 3000으로 전달되는지 확인
        called_payload = mock_post.call_args[1]["json"]
        self.assertEqual(called_payload["generationConfig"]["maxOutputTokens"], 3000)

    @patch("requests.post")
    def test_generate_market_briefing_thinking_budget_for_3_7_flash(self, mock_post):
        """gemini-3.7-flash 등 추론 모델 호출 시 thinkingBudget=0 적용 검증"""
        analyzer = GeminiAnalyzer(api_key="fake-key")
        GeminiAnalyzer._CACHED_BRIEFING_MODELS = ["gemini-3.7-flash"]
        GeminiAnalyzer._BRIEFING_MODELS_CACHED_AT = time.time()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {
                        "parts": [{"text": "• [거시 시황]: BTC 강세 지속\n• [계좌 진단]: 안전 운용 중\n• [전략 제언]: 관망 유지"}]
                    },
                }
            ]
        }
        mock_post.return_value = mock_resp

        result = analyzer.generate_market_briefing(
            exchange_name="업비트",
            total_equity=1000000.0,
            daily_pnl_krw=0.0,
            daily_pnl_pct=0.0,
            held_positions_desc="KRW-BTC",
            macro_diag={"regime": "NORMAL", "risk_score": 30, "summary": "정상"},
            fng_desc="탐욕",
        )

        self.assertIn("[거시 시황]", result)
        called_payload = mock_post.call_args[1]["json"]
        self.assertEqual(called_payload["generationConfig"]["thinkingConfig"]["thinkingBudget"], 0)
        self.assertEqual(called_payload["generationConfig"]["maxOutputTokens"], 3000)

    @patch("requests.post")
    def test_generate_market_briefing_skips_truncated_response(self, mock_post):
        """finishReason=MAX_TOKENS 또는 불완전 텍스트 시 차순위 모델로 순차 전환 검증"""
        analyzer = GeminiAnalyzer(api_key="fake-key")
        GeminiAnalyzer._CACHED_BRIEFING_MODELS = [
            "gemini-3.7-flash",
            "gemini-3.6-flash",
        ]
        GeminiAnalyzer._BRIEFING_MODELS_CACHED_AT = time.time()

        # 1번째 호출(3.7-flash): MAX_TOKENS로 텍스트 잘림 발생
        resp_truncated = MagicMock()
        resp_truncated.status_code = 200
        resp_truncated.json.return_value = {
            "candidates": [
                {
                    "finishReason": "MAX_TOKENS",
                    "content": {"parts": [{"text": "• [거시 시황]: 시장 전반에 탐욕 심리(73점)가 잔존해 있으나, BTC 1시간봉 이평선"}]},
                }
            ]
        }

        # 2번째 호출(3.6-flash): 정상 완성 응답
        resp_ok = MagicMock()
        resp_ok.status_code = 200
        resp_ok.json.return_value = {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {
                        "parts": [{"text": "• [거시 시황]: BTC 강세 지속\n• [계좌 진단]: 안전 운용 중\n• [전략 제언]: 분할 매수 대기"}]
                    },
                }
            ]
        }

        mock_post.side_effect = [resp_truncated, resp_ok]

        result = analyzer.generate_market_briefing(
            exchange_name="빗썸",
            total_equity=1000000.0,
            daily_pnl_krw=0.0,
            daily_pnl_pct=0.0,
            held_positions_desc="KRW-BTC",
            macro_diag={"regime": "NORMAL", "risk_score": 30, "summary": "정상"},
            fng_desc="탐욕",
        )

        # 1번째 잘린 모델을 건너뛰고 2번째 모델의 정상 텍스트가 반환되어야 함
        self.assertIn("[전략 제언]", result)
        self.assertEqual(mock_post.call_count, 2)
        # 2번째 호출된 모델이 3.6-flash인지 확인
        second_call_url = mock_post.call_args_list[1][0][0]
        self.assertIn("gemini-3.6-flash", second_call_url)

    @patch("requests.post")
    def test_generate_market_briefing_retries_on_400_thinking_config(self, mock_post):
        """thinkingConfig 미지원으로 HTTP 400 발생 시 thinkingConfig 제거 후 재전송 성공 검증"""
        analyzer = GeminiAnalyzer(api_key="fake-key")
        GeminiAnalyzer._CACHED_BRIEFING_MODELS = ["gemini-3.7-flash"]
        GeminiAnalyzer._BRIEFING_MODELS_CACHED_AT = time.time()

        resp_400 = MagicMock()
        resp_400.status_code = 400
        resp_400.text = "Unknown field: thinkingConfig"

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {
                        "parts": [{"text": "• [거시 시황]: BTC 강세 지속\n• [계좌 진단]: 안전 운용 중\n• [전략 제언]: 관망 유지"}]
                    },
                }
            ]
        }

        mock_post.side_effect = [resp_400, resp_200]

        result = analyzer.generate_market_briefing(
            exchange_name="빗썸",
            total_equity=1000000.0,
            daily_pnl_krw=0.0,
            daily_pnl_pct=0.0,
            held_positions_desc="KRW-BTC",
            macro_diag={"regime": "NORMAL", "risk_score": 30, "summary": "정상"},
            fng_desc="탐욕",
        )

        self.assertIn("[거시 시황]", result)
        self.assertEqual(mock_post.call_count, 2)
        # 재시도 payload에 thinkingConfig가 제거되었는지 확인
        retry_payload = mock_post.call_args_list[1][1]["json"]
        self.assertNotIn("thinkingConfig", retry_payload["generationConfig"])


if __name__ == "__main__":
    unittest.main()


