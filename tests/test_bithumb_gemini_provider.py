"""빗썸 전용 Gemini 키 격리와 신규 BUY fail-closed 경계를 검증한다."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from ai_provider import AIProviderTelemetry, BithumbGeminiProvider
from bithumb_ai import build_bithumb_analyzer, get_bithumb_ai_config_block_reason, get_bithumb_ai_entry_block_reason
from gemini_analyzer import ENTRY_JSON_SCHEMA, GeminiProvider
from gemini_telemetry import GeminiTelemetry


class BithumbGeminiProviderTests(unittest.TestCase):
    """빗썸은 업비트와 독립된 Gemini 경계에서만 신규 BUY를 재개해야 한다."""

    def setUp(self):
        self.env = dict(os.environ)
        self.root = Path(__file__).resolve().parents[1]
        self.filename = "gemini_bithumb_telemetry_test.json"
        self.path = self.root / "data" / self.filename
        self.lock_path = Path(f"{self.path}.lock")
        self.path.unlink(missing_ok=True)
        self.lock_path.unlink(missing_ok=True)
        AIProviderTelemetry.configure(data_dir=str(self.root / "data"), storage_filename=self.filename)
        AIProviderTelemetry.reset()
        os.environ.update({
            "BITHUMB_AI_PROVIDER": "gemini", "BITHUMB_GEMINI_API_KEY": "bithumb-only-key",
            "GEMINI_API_KEY": "shared-key-must-not-be-used", "UPBIT_GEMINI_API_KEY": "upbit-key-must-not-be-used",
        })

    def tearDown(self):
        self.path.unlink(missing_ok=True)
        self.lock_path.unlink(missing_ok=True)
        AIProviderTelemetry.configure(data_dir=str(self.root / "data"), storage_filename="gemini_bithumb_telemetry.json")
        os.environ.clear()
        os.environ.update(self.env)

    @staticmethod
    def _models_response():
        response = MagicMock(status_code=200)
        response.json.return_value = {"models": [
            {"name": "models/gemini-3.5-flash-lite", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-2.5-pro", "supportedGenerationMethods": ["generateContent"]},
        ]}
        return response

    @staticmethod
    def _json_response():
        response = MagicMock(status_code=200)
        response.json.return_value = {"candidates": [{"content": {"parts": [{"text": (
            '{"STATUS":"ACTIVE","ACTION":"HOLD","ENTRY_PRICE":100,"TARGET_PRICE":103,'
            '"STOP_LOSS":98,"ALLOC_PCT":0,"ALPHA_SCORE":0,"REASON":"대기"}'
        )}]}}]}
        return response

    @patch("ai_provider.requests.post")
    @patch("ai_provider.requests.get")
    def test_bithumb_uses_only_dedicated_key_and_flash_lite(self, mock_get, mock_post):
        """ListModels와 generateContent 모두 빗썸 전용 키만 URL에 사용해야 한다."""
        mock_get.return_value = self._models_response()
        mock_post.return_value = self._json_response()
        provider = BithumbGeminiProvider(os.getenv("BITHUMB_GEMINI_API_KEY", ""))
        result = provider.complete_json("안전한 분석", provider.models_for("trading"), ENTRY_JSON_SCHEMA,
                                        context="KRW-TEST", timeout=1.0, max_tokens=300)

        self.assertIsInstance(result.value, dict)
        self.assertIn("bithumb-only-key", mock_get.call_args.args[0])
        self.assertIn("bithumb-only-key", mock_post.call_args.args[0])
        self.assertNotIn("shared-key-must-not-be-used", str(mock_get.call_args))
        self.assertNotIn("upbit-key-must-not-be-used", str(mock_post.call_args))
        self.assertIn("flash-lite", mock_post.call_args.args[0])
        self.assertNotIn("gemini-2.5-pro", mock_post.call_args.args[0])

    @patch("ai_provider.requests.get")
    def test_list_models_failure_blocks_all_bithumb_new_buy_paths(self, mock_get):
        """모델 목록 429는 공통 주문 게이트가 읽는 영속 안전 상태를 닫아야 한다."""
        mock_get.return_value = MagicMock(status_code=429)
        provider = BithumbGeminiProvider("bithumb-only-key")
        self.assertEqual(provider.models_for("trading"), [])
        self.assertIn("Gemini", get_bithumb_ai_entry_block_reason())
        safety = AIProviderTelemetry.snapshot("bithumb")["entry_safety"]
        self.assertTrue(safety["entry_blocked"])
        self.assertEqual(safety["http_status"], 429)

    @patch("ai_provider.requests.get")
    def test_bithumb_requires_the_same_concrete_model_as_upbit(self, mock_get):
        """latest 별칭 단독은 배제하고 구체 Flash-Lite 모델(3.5 및 3.1) 순차 목록을 지원한다."""
        # 1. latest 별칭만 있는 경우 -> 구체 모델 없으므로 required_model_unavailable 차단
        response_alias_only = MagicMock(status_code=200)
        response_alias_only.json.return_value = {"models": [
            {"name": "models/gemini-flash-lite-latest", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-2.5-pro", "supportedGenerationMethods": ["generateContent"]},
        ]}
        mock_get.return_value = response_alias_only

        provider = BithumbGeminiProvider("bithumb-only-key")
        self.assertEqual(provider.models_for("trading"), [])
        safety = AIProviderTelemetry.snapshot("bithumb")["entry_safety"]
        self.assertTrue(safety["entry_blocked"])
        self.assertEqual(safety["reason"], "required_model_unavailable")

        # 2. 3.1 모델만 있는 경우 -> 3.1 단독 정상 채택
        response_31_only = MagicMock(status_code=200)
        response_31_only.json.return_value = {"models": [
            {"name": "models/gemini-flash-lite-latest", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-3.1-flash-lite", "supportedGenerationMethods": ["generateContent"]},
        ]}
        mock_get.return_value = response_31_only
        provider_31 = BithumbGeminiProvider("bithumb-only-key")
        self.assertEqual(provider_31.models_for("trading"), ["gemini-3.1-flash-lite"])

        # 3. 3.5와 3.1 모두 있는 경우 -> 3.5 우선 후 3.1 순차 목록 반환
        response_both = MagicMock(status_code=200)
        response_both.json.return_value = {"models": [
            {"name": "models/gemini-3.5-flash-lite", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-3.1-flash-lite", "supportedGenerationMethods": ["generateContent"]},
        ]}
        mock_get.return_value = response_both
        provider_both = BithumbGeminiProvider("bithumb-only-key")
        self.assertEqual(provider_both.models_for("trading"), ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"])

    @patch("ai_provider.requests.post")
    def test_fallback_to_3_1_on_3_5_timeout_or_error(self, mock_post):
        """3.5 모델 타임아웃 시 3.1 모델로 즉시 폴백하여 정상 JSON 응답을 수신하고 진입 차단되지 않는다."""
        import requests
        mock_post.side_effect = [requests.exceptions.Timeout(), self._json_response()]
        provider = BithumbGeminiProvider("bithumb-only-key")
        models = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
        result = provider.complete_json(
            "분석", models, ENTRY_JSON_SCHEMA, context="KRW-BTC", timeout=1.0, max_tokens=100
        )
        self.assertIsInstance(result.value, dict)
        self.assertEqual(result.model, "gemini-3.1-flash-lite")
        safety = AIProviderTelemetry.snapshot("bithumb")["entry_safety"]
        self.assertFalse(safety["entry_blocked"])

    @patch("ai_provider.requests.post")
    @patch("ai_provider.requests.get")
    def test_invalid_schema_blocks_then_valid_schema_reopens_after_restart(self, mock_get, mock_post):
        """정상 로컬 스키마 응답만 안전 상태를 해제하고 재시작 뒤에도 상태를 복원한다."""
        mock_get.return_value = self._models_response()
        invalid = MagicMock(status_code=200)
        invalid.json.return_value = {"candidates": [{"content": {"parts": [{"text": "{bad json"}]}}]}
        mock_post.side_effect = [invalid, self._json_response()]
        provider = BithumbGeminiProvider("bithumb-only-key")
        models = provider.models_for("trading")
        self.assertIsNone(provider.complete_json("분석", models, ENTRY_JSON_SCHEMA, context="KRW-TEST", timeout=1, max_tokens=100).value)
        AIProviderTelemetry.configure(data_dir=str(self.root / "data"), storage_filename=self.filename)
        self.assertTrue(AIProviderTelemetry.snapshot("bithumb")["entry_safety"]["entry_blocked"])
        self.assertIsInstance(provider.complete_json("분석", models, ENTRY_JSON_SCHEMA, context="KRW-TEST", timeout=1, max_tokens=100).value, dict)
        self.assertFalse(AIProviderTelemetry.snapshot("bithumb")["entry_safety"]["entry_blocked"])

    def test_configuration_is_dedicated_and_upbit_provider_is_fail_closed(self):
        """두 거래소 모두 AI 장애 시 신규 BUY를 열지 않는 계약을 유지한다."""
        self.assertEqual(get_bithumb_ai_config_block_reason(), "")
        self.assertIsNotNone(build_bithumb_analyzer())
        os.environ["BITHUMB_AI_PROVIDER"] = "unsupported"
        self.assertIsNone(build_bithumb_analyzer())
        self.assertIn("gemini", get_bithumb_ai_config_block_reason())
        self.assertEqual(GeminiProvider.exchange, "upbit")
        self.assertTrue(GeminiProvider.is_entry_fail_closed)

    def test_prompt_contract_and_dashboard_label(self):
        """공통 시스템 지침과 표시 경계가 빗썸 Gemini 안전 계약을 포함해야 한다."""
        prompt = BithumbGeminiProvider.SYSTEM_INSTRUCTION
        for phrase in ("빗썸", "업비트", "ACK는 체결이 아닙니다", "HOLD", "주문 실행", "NEW_LISTING", "한국어"):
            self.assertIn(phrase, prompt)
        dashboard_js = (self.root / "dashboard" / "src" / "app.js").read_text(encoding="utf-8")
        dashboard_html = (self.root / "dashboard" / "index.html").read_text(encoding="utf-8")
        self.assertIn("빗썸 Gemini AI", dashboard_html)
        self.assertIn("gemini_bithumb", dashboard_js)

    def test_bithumb_card_uses_the_same_gemini_display_contract_as_upbit(self):
        """빗썸 카드가 업비트와 같은 Gemini 모델·색상·마크업·텔레메트리 계약을 사용한다."""
        dashboard_html = (self.root / "dashboard" / "index.html").read_text(encoding="utf-8")
        dashboard_js = (self.root / "dashboard" / "src" / "app.js").read_text(encoding="utf-8")

        self.assertIn('id="gemini_bithumb_quota_bar" class="bg-gradient-to-r from-blue-500 to-indigo-500', dashboard_html)
        self.assertIn('id="gemini_bithumb_models_list"', dashboard_html)
        self.assertIn('id="gemini_upbit_models_list"', dashboard_html)
        self.assertIn('id="bithumb_ai_provider_title"', dashboard_html)
        self.assertIn('id="upbit_ai_provider_title"', dashboard_html)
        self.assertIn("renderAiProviderCard('gemini_bithumb', btGeminiData, 'bg-gradient-to-r from-blue-500 to-indigo-500')", dashboard_js)
        self.assertIn("renderAiProviderCard('gemini_upbit', upGeminiData, 'bg-gradient-to-r from-blue-500 to-indigo-500')", dashboard_js)
        self.assertIn('gData.models_by_id || gData.models || {}', dashboard_js)
        self.assertNotIn("빗썸 Groq AI", dashboard_js)

        # 빗썸 AI Provider 스냅샷이 업비트와 동일한 쿼터 스키마 및 모델별 한도(Flash-Lite=500, Flash=20, list_models=0)를 제공하는지 검증
        AIProviderTelemetry.record("gemini", "bithumb", "gemini-3.5-flash-lite", "test", 200, 10.0)
        AIProviderTelemetry.record("gemini", "bithumb", "gemini-3.8-flash", "macro_regime", 200, 20.0)
        AIProviderTelemetry.record("gemini", "bithumb", "list_models", "list_models", 200, 5.0)
        bt_snap = AIProviderTelemetry.snapshot("bithumb")
        self.assertEqual(bt_snap["quota_limit"], 1000)
        self.assertIn("gemini-3.5-flash-lite", bt_snap["models"])
        self.assertEqual(bt_snap["models"]["gemini-3.5-flash-lite"]["quota_limit"], 500)
        self.assertIn("gemini-3.8-flash", bt_snap["models"])
        self.assertEqual(bt_snap["models"]["gemini-3.8-flash"]["quota_limit"], 20)
        self.assertIn("list_models", bt_snap["models"])
        self.assertEqual(bt_snap["models"]["list_models"]["quota_limit"], 0)
        self.assertIn("models_by_id", bt_snap)
        self.assertIn("remaining_str", bt_snap["reset_info"])

        # 업비트 Gemini 텔레메트리도 동일한 provider와 entry_safety를 제공하는지 검증
        up_snap = GeminiTelemetry.snapshot().to_dict()
        self.assertEqual(up_snap["provider"], "gemini")
        self.assertEqual(up_snap["exchange"], "upbit")
        self.assertIn("entry_safety", up_snap)

    def test_telemetry_file_is_separate_from_upbit_path(self):
        """빗썸 사용량·안전 상태 파일이 업비트 운영 파일과 겹치지 않아야 한다."""
        AIProviderTelemetry.record("gemini", "bithumb", "gemini-2.5-flash-lite", "KRW-TEST", 200, 1.0)
        self.assertTrue(self.path.exists())
        self.assertNotEqual(self.path.resolve(), (self.root / "data" / "upbit" / "gemini_telemetry.json").resolve())
        snapshot = AIProviderTelemetry.snapshot("bithumb")
        self.assertEqual(snapshot["provider"], "gemini")
        self.assertEqual(snapshot["reset_info"]["source"], "pt_midnight")

    def test_existing_position_protection_remains_outside_entry_block(self):
        """AI 장애 차단은 미보유 신규 BUY에만 적용돼 청산·대사 경로를 막지 않아야 한다."""
        runtime = (self.root / "src" / "trading_runtime.py").read_text(encoding="utf-8")
        self.assertIn("if provider_block_reason and not is_holding:", runtime)
        self.assertIn("AI Provider 불확실성은 기존 포지션 보호를 건드리지 않고 신규 BUY 경로만 닫는다", runtime)


if __name__ == "__main__":
    unittest.main()
