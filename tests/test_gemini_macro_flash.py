"""
Unit tests for Gemini Macro Regime Dedicated Flash Routing & 4H Factor Integration
"""

import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from ai_provider import BithumbGeminiProvider
from gemini_analyzer import GeminiAnalyzer, MACRO_JSON_SCHEMA
from gemini_telemetry import GeminiTelemetry


class GeminiMacroFlashTests(unittest.TestCase):
    def setUp(self):
        GeminiAnalyzer.clear_caches()
        GeminiTelemetry.reset_for_test()

    def test_macro_model_priority_sorting(self):
        """거시 레짐 모델 정렬 시 일반 Flash가 최우선(3.8 > 3.7 > 3.5), Flash-Lite가 차순위, Pro 배제 검증"""
        raw_models = [
            "gemini-3.5-flash-lite",
            "gemini-3.8-flash",
            "gemini-pro",
            "gemini-3.5-flash",
            "gemini-3.7-flash",
            "gemini-3.1-flash-lite",
        ]

        sorted_models = sorted(raw_models, key=GeminiAnalyzer._macro_model_priority_key, reverse=True)

        # 1. 1위는 가장 최신 일반 flash인 3.8-flash
        self.assertEqual(sorted_models[0], "gemini-3.8-flash")
        # 2. 일반 Flash 순위: 3.8 > 3.7 > 3.5
        self.assertEqual(sorted_models[1], "gemini-3.7-flash")
        self.assertEqual(sorted_models[2], "gemini-3.5-flash")
        # 3. 그 다음 순위는 Flash-Lite 순차
        self.assertEqual(sorted_models[3], "gemini-3.5-flash-lite")
        self.assertEqual(sorted_models[4], "gemini-3.1-flash-lite")
        # 4. Pro 모델은 최하위 (배제 대상)
        self.assertEqual(sorted_models[5], "gemini-pro")

    @patch("requests.get")
    def test_fetch_available_macro_models_filters_and_sorts(self, mock_get):
        """ListModels 조회 시 Pro/미디어 모델 배제 및 일반 Flash 우선 정렬 검증"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "models": [
                {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
                {"name": "models/gemini-2.5-pro", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-3.5-flash-lite", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-3.7-flash", "supportedGenerationMethods": ["generateContent"]},
            ]
        }
        mock_get.return_value = mock_resp

        models = GeminiAnalyzer.fetch_available_macro_models(api_key="fake-key")

        self.assertNotIn("text-embedding-004", models)
        self.assertNotIn("gemini-2.5-pro", models)
        self.assertEqual(models[0], "gemini-3.8-flash")
        self.assertEqual(models[1], "gemini-3.7-flash")
        self.assertIn("gemini-3.5-flash-lite", models)

    @patch("ai_provider.requests.post")
    def test_diagnose_macro_regime_prompt_includes_4h_and_uses_flash(self, mock_post):
        """diagnose_macro_regime 호출 시 4시간봉 팩터가 프롬프트에 포함되고 일반 Flash 모델이 전달되는지 검증"""
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "candidates": [{
                "content": {
                    "parts": [{
                        "text": '{"regime": "BULL_TREND", "risk_score": 25, "recommended_cash_ratio": 0.2, "summary": "BTC 4H 정배열 및 강세 지속", "action_guideline": "알트코인 적극 매수"}'
                    }]
                }
            }]
        }
        mock_post.return_value = mock_resp

        analyzer = GeminiAnalyzer(api_key="test-api-key")

        # mock 1H / 4H 캔들 데이터 생성
        candles_1h = [{"trade_price": 95000000.0 - i * 100000.0} for i in range(30)]
        candles_4h = [{"trade_price": 95000000.0 - i * 200000.0} for i in range(30)]
        fng = {"value": "75", "desc": "75점 (탐욕)"}

        result = analyzer.diagnose_macro_regime(candles_1h, btc_candles_4h=candles_4h, fng_index=fng)

        self.assertEqual(result["regime"], "BULL_TREND")
        self.assertEqual(result["risk_score"], 25)
        self.assertEqual(result["recommended_cash_ratio"], 0.2)

        # post 호출 인자 검증
        called_url = mock_post.call_args.args[0]
        self.assertIn("gemini-3.8-flash", called_url)  # 최우선 모델인 3.8-flash로 호출되었는지 확인

        # 프롬프트 내용에 4시간봉 지표(EMA, 4H, 고점 대비)가 주입되었는지 확인
        post_json = mock_post.call_args.kwargs.get("json", {})
        prompt_text = post_json.get("contents", [{}])[0].get("parts", [{}])[0].get("text", "")
        self.assertIn("4시간 중기 추세", prompt_text)
        self.assertIn("EMA20=", prompt_text)
        self.assertIn("75점 (탐욕)", prompt_text)

    @patch("ai_provider.requests.post")
    def test_diagnose_macro_regime_fallback_on_429(self, mock_post):
        """일반 Flash 모델이 429(할당량 초과)를 반환할 때 Flash-Lite로 순차 폴백 성공하는지 검증"""
        # 첫 번째 호출(3.8-flash): 429 에러
        err_resp = MagicMock(status_code=429)
        err_resp.json.return_value = {"error": {"code": 429, "message": "Resource Exhausted"}}

        # 두 번째 호출(차순위 모델): 200 성공
        ok_resp = MagicMock(status_code=200)
        ok_resp.json.return_value = {
            "candidates": [{
                "content": {
                    "parts": [{
                        "text": '{"regime": "NORMAL", "risk_score": 40, "recommended_cash_ratio": 0.3, "summary": "정상 순항", "action_guideline": "분할 매매"}'
                    }]
                }
            }]
        }
        mock_post.side_effect = [err_resp, ok_resp]

        analyzer = GeminiAnalyzer(api_key="test-api-key")
        candles_1h = [{"trade_price": 95000000.0} for _ in range(25)]

        result = analyzer.diagnose_macro_regime(candles_1h)

        self.assertEqual(result["regime"], "NORMAL")
        self.assertEqual(mock_post.call_count, 2)
        # 첫 호출은 3.8-flash, 두 번째 폴백 호출은 차순위 모델
        first_url = mock_post.call_args_list[0].args[0]
        second_url = mock_post.call_args_list[1].args[0]
        self.assertIn("gemini-3.8-flash", first_url)
        self.assertIn("gemini-3.7-flash", second_url)

    @patch("ai_provider.requests.get")
    def test_bithumb_gemini_provider_models_for_purpose(self, mock_get):
        """빗썸 Provider에서 purpose='trading'은 3.5-flash-lite 단일 모델을 유지하고, purpose='macro'는 Flash 후보군을 반환하는지 검증"""
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "models": [
                {"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-3.5-flash-lite", "supportedGenerationMethods": ["generateContent"]},
            ]
        }
        mock_get.return_value = mock_resp

        provider = BithumbGeminiProvider("bithumb-key")

        # trading 목적: 신규 BUY는 오직 TRADING_MODEL 단일 항목만
        trading_models = provider.models_for("trading")
        self.assertEqual(trading_models, ["gemini-3.5-flash-lite"])

        # macro 목적: 거시 진단은 Flash 우선 후보군 반환
        macro_models = provider.models_for("macro")
        self.assertEqual(macro_models[0], "gemini-3.8-flash")
        self.assertIn("gemini-3.5-flash-lite", macro_models)

    @patch("ai_provider.requests.post")
    def test_diagnose_macro_regime_background_non_blocking_and_updates_cache(self, mock_post):
        """background=True 호출 시 콜드 스타트에서 메인 스레드가 블로킹 없이 즉시 반환되고 백그라운드 스레드에서 캐시가 갱신되는지 검증"""
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "candidates": [{
                "content": {
                    "parts": [{
                        "text": '{"regime": "BULL_TREND", "risk_score": 20, "recommended_cash_ratio": 0.2, "summary": "백그라운드 진단 완료", "action_guideline": "매수 유지"}'
                    }]
                }
            }]
        }
        mock_post.return_value = mock_resp

        analyzer = GeminiAnalyzer(api_key="test-api-key")
        candles_1h = [{"trade_price": 95000000.0} for _ in range(25)]

        # 1. 콜드 스타트에서 background=True 호출
        start_ts = time.monotonic()
        immediate_result = analyzer.diagnose_macro_regime(candles_1h, background=True)
        elapsed = time.monotonic() - start_ts

        # 메인 스레드는 0.1초 미만으로 즉시 반환되어야 함
        self.assertLess(elapsed, 0.1)
        # 콜드 스타트 즉시 결과는 기본 fallback 레짐이어야 함
        self.assertIn(immediate_result["regime"], ("NORMAL", "CAUTION_PULLBACK"))

        # 백그라운드 스레드가 완료될 때까지 대기
        worker = getattr(GeminiAnalyzer, "_MACRO_DIAG_WORKER_THREAD", None)
        if worker is not None and worker.is_alive():
            worker.join(timeout=3.0)

        # 백그라운드 완료 후 캐시가 최신 진단 결과로 업데이트되었는지 확인
        cached = analyzer.diagnose_macro_regime(candles_1h, background=True)
        self.assertEqual(cached["regime"], "BULL_TREND")
        self.assertEqual(cached["summary"], "백그라운드 진단 완료")

    @patch("ai_provider.requests.post")
    def test_diagnose_macro_regime_background_stale_revalidate(self, mock_post):
        """기존 캐시가 있고 30분(1800초) 이상 지난 경우, background=True 호출 시 기존 캐시를 즉시 반환하고 백그라운드 갱신을 트리거하는지 검증"""
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "candidates": [{
                "content": {
                    "parts": [{
                        "text": '{"regime": "BEAR_REGIME", "risk_score": 80, "recommended_cash_ratio": 0.8, "summary": "하락장 전환", "action_guideline": "현금 확보"}'
                    }]
                }
            }]
        }
        mock_post.return_value = mock_resp

        analyzer = GeminiAnalyzer(api_key="test-api-key")
        candles_1h = [{"trade_price": 95000000.0} for _ in range(25)]

        # 40분 전 캐시 사전 주입
        old_diag = {
            "regime": "NORMAL",
            "risk_score": 35,
            "recommended_cash_ratio": 0.3,
            "summary": "직전 정상 장세",
            "action_guideline": "분할 매매",
        }
        GeminiAnalyzer._MACRO_DIAG_CACHE = dict(old_diag)
        GeminiAnalyzer._LAST_MACRO_DIAG_TS = time.time() - 2400.0  # 40분 전

        # background=True 호출
        start_ts = time.monotonic()
        immediate = analyzer.diagnose_macro_regime(candles_1h, background=True)
        elapsed = time.monotonic() - start_ts

        # 0ms로 직전 캐시 반환
        self.assertLess(elapsed, 0.05)
        self.assertEqual(immediate["regime"], "NORMAL")
        self.assertEqual(immediate["summary"], "직전 정상 장세")

        # 백그라운드 스레드 완료 대기
        worker = getattr(GeminiAnalyzer, "_MACRO_DIAG_WORKER_THREAD", None)
        if worker is not None and worker.is_alive():
            worker.join(timeout=3.0)

        # 갱신 완료 후 캐시가 최신 진단 결과로 교체되었는지 확인
        updated = analyzer.diagnose_macro_regime(candles_1h, background=True)
        self.assertEqual(updated["regime"], "BEAR_REGIME")
        self.assertEqual(updated["summary"], "하락장 전환")

    @patch("ai_provider.requests.post")
    def test_diagnose_macro_regime_background_deduplication(self, mock_post):
        """백그라운드 작업이 이미 진행 중일 때 추가 background=True 호출이 중복 스레드를 띄우지 않는지 검증"""
        analyzer = GeminiAnalyzer(api_key="test-api-key")
        candles_1h = [{"trade_price": 95000000.0} for _ in range(25)]

        # 강제로 실행 중 플래그 활성화
        GeminiAnalyzer._MACRO_DIAG_RUNNING = True
        try:
            triggered = analyzer._trigger_macro_regime_background(candles_1h)
            self.assertFalse(triggered)
            self.assertEqual(mock_post.call_count, 0)
        finally:
            GeminiAnalyzer._MACRO_DIAG_RUNNING = False


if __name__ == "__main__":
    unittest.main()
