"""업비트 Gemini 키 격리 및 Flash-Lite fail-closed 경계를 검증한다."""

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import shutil
from ai_provider import AIProviderTelemetry, BaseGeminiProvider, GeminiProvider
from gemini_telemetry import GeminiTelemetry
from upbit_ai import build_upbit_analyzer, get_upbit_ai_config_block_reason, get_upbit_ai_entry_block_reason


class UpbitGeminiFailClosedTests(unittest.TestCase):
    """업비트도 빗썸과 동일하게 AI 응답 실패 및 Flash-Lite 부재 시 신규 BUY를 차단한다."""

    def setUp(self):
        self.env = dict(os.environ)
        self.temp_dir = tempfile.mkdtemp()
        GeminiTelemetry.configure(data_dir=self.temp_dir)
        GeminiTelemetry.reset(persist=True)
        AIProviderTelemetry.configure(data_dir=self.temp_dir, storage_filename="upbit_entry_safety_test.json")
        AIProviderTelemetry.reset(persist=True)
        BaseGeminiProvider.clear_cooldowns()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        os.environ.clear()
        os.environ.update(self.env)
        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        GeminiTelemetry.configure(data_dir=os.path.join(project_root, "data"))
        AIProviderTelemetry.configure(data_dir=os.path.join(project_root, "data"), storage_filename="upbit_entry_safety_test.json")
        BaseGeminiProvider.clear_cooldowns()

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

    @patch("ai_provider.requests.get")
    def test_upbit_macro_models_for_prefers_flash_and_includes_3_6(self, mock_get):
        """업비트 거시 레짐 모델은 일반 Flash(3.8, 3.7, 3.6 등) 순차 폴백 후 Flash-Lite 목록을 반환해야 한다."""
        response = MagicMock(status_code=200)
        response.json.return_value = {"models": [
            {"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-3.7-flash", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-3.6-flash", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-3.5-flash-lite", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-2.5-pro", "supportedGenerationMethods": ["generateContent"]},
        ]}
        mock_get.return_value = response
        provider = GeminiProvider("upbit-test-key")
        macro_models = provider.models_for("macro")

        self.assertEqual(macro_models, ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"])
        self.assertNotIn("gemini-2.5-pro", macro_models)

    @patch("ai_provider.requests.get")
    def test_upbit_briefing_models_for_prefers_flash_and_falls_back_to_lite(self, mock_get):
        """업비트 브리핑 모델은 일반 Flash 최우선 후 Flash-Lite 순차 폴백 목록을 반환해야 한다."""
        response = MagicMock(status_code=200)
        response.json.return_value = {"models": [
            {"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-3.7-flash", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-3.6-flash", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-3.5-flash-lite", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-2.5-pro", "supportedGenerationMethods": ["generateContent"]},
        ]}
        mock_get.return_value = response
        provider = GeminiProvider("upbit-test-key")
        briefing_models = provider.models_for("briefing")

        self.assertEqual(briefing_models, ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"])
        self.assertNotIn("gemini-2.5-pro", briefing_models)

    @patch("ai_provider.requests.post")
    def test_upbit_briefing_uses_briefing_system_instruction(self, mock_post):
        """업비트 브리핑 context 호출 시 BRIEFING_SYSTEM_INSTRUCTION이 주입되어야 한다."""
        prompt = GeminiProvider.BRIEFING_SYSTEM_INSTRUCTION
        for phrase in ("업비트", "빗썸", "ACK는 체결이 아닙니다", "신규 BUY", "주문 실행", "한국어"):
            self.assertIn(phrase, prompt)

        success_resp = MagicMock(status_code=200)
        success_resp.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": "• [거시 시황]: 정상\n• [계좌 진단]: 정상\n• [전략 제언]: 정상"}]}}]
        }
        mock_post.return_value = success_resp

        provider = GeminiProvider("upbit-test-key")
        provider.complete_text("시황 작성", ["gemini-3.8-flash"], context="upbit_briefing", timeout=5.0, max_tokens=1000)

        call_json = mock_post.call_args[1]["json"]
        self.assertIn("systemInstruction", call_json)
        sys_text = call_json["systemInstruction"]["parts"][0]["text"]
        self.assertEqual(sys_text, GeminiProvider.BRIEFING_SYSTEM_INSTRUCTION)
        self.assertNotIn("SCALP", sys_text)
        self.assertNotIn("MOMENTUM_BREAKOUT", sys_text)

    @patch("gemini_analyzer.GeminiAnalyzer.get_briefing_candidate_models", return_value=["gemini-3.8-flash"])
    @patch("ai_provider.requests.post")
    def test_generate_market_briefing_logs_details_on_validation_failure(self, mock_post, mock_candidates):
        """품질 검증 실패 시 모델명, 텍스트 길이, 응답 내용이 포함된 warning 로그가 기록되어야 한다."""
        fail_resp = MagicMock(status_code=200)
        # 40자 미만이며 불릿이 없는 부적격 응답
        fail_resp.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": "데이터 부족"}]}}]
        }
        mock_post.return_value = fail_resp
        os.environ["UPBIT_GEMINI_API_KEY"] = "upbit-test-key"
        analyzer = build_upbit_analyzer()
        self.assertIsNotNone(analyzer)
        with self.assertLogs("gemini_analyzer", level="WARNING") as cm:
            comment = analyzer.generate_market_briefing(
                exchange_name="업비트",
                total_equity=1_000_000.0,
                daily_pnl_krw=0.0,
                daily_pnl_pct=0.0,
                held_positions_desc="없음",
                macro_diag={"regime": "NORMAL", "risk_score": 40, "summary": "정상"},
                fng_desc="50/100 (Neutral)",
            )

        # 기본 브리핑으로 fallback 되었는지 검증
        self.assertIn("리스크 안전선 내에서 정상 운용 중입니다", comment)
        # 상세 warning 로그에 모델, 길이, 응답 미리보기가 포함되었는지 검증
        warning_output = "\n".join(cm.output)
        self.assertIn("AI 브리핑 품질 검증 실패로 기본 브리핑을 사용합니다", warning_output)
        self.assertIn("모델:", warning_output)
        self.assertIn("길이:", warning_output)
        self.assertIn("데이터 부족", warning_output)


if __name__ == "__main__":
    unittest.main()
