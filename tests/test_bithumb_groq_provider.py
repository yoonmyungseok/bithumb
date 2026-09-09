"""빗썸 Groq 전환의 모델 고정·fail-closed·업비트 격리를 검증한다."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from ai_provider import AIProviderTelemetry, GroqProvider
from bithumb_ai import build_bithumb_analyzer, get_bithumb_ai_config_block_reason, get_bithumb_ai_entry_block_reason
from gemini_analyzer import GeminiAnalyzer, MACRO_JSON_SCHEMA, RANKING_JSON_SCHEMA


def _candles() -> list[dict[str, float | str]]:
    """과열 가드가 아닌 Provider 결과를 확인할 최소 정상 캔들을 생성한다."""
    return [{
        "trade_price": 100.0, "opening_price": 100.0, "high_price": 101.0, "low_price": 99.0,
        "candle_acc_trade_volume": 100.0, "candle_date_time_utc": "2026-09-07T00:00:00",
    } for _ in range(30)]


@unittest.skip("빗썸 AI 공급자가 Gemini 전용으로 전환되어 상세 회귀는 test_bithumb_gemini_provider.py에서 수행한다.")
class BithumbGroqProviderTests(unittest.TestCase):
    """빗썸 전용 Groq 경계가 주문 경계 밖에서 fail-closed되는지 확인한다."""

    def setUp(self):
        self.env = dict(os.environ)
        self.project_root = Path(__file__).resolve().parents[1]
        self.telemetry_storage_filename = "groq_telemetry_test.json"
        self.telemetry_storage_path = self.project_root / "data" / self.telemetry_storage_filename
        self.telemetry_lock_path = Path(f"{self.telemetry_storage_path}.lock")
        # 모든 Groq 호출 테스트를 운영 계측 파일과 분리한다.
        self.telemetry_storage_path.unlink(missing_ok=True)
        self.telemetry_lock_path.unlink(missing_ok=True)
        AIProviderTelemetry.configure(
            data_dir=str(self.project_root / "data"), storage_filename=self.telemetry_storage_filename,
        )
        AIProviderTelemetry.reset()
        # 클래스 단위 분석 캐시는 Provider 안전 상태가 다른 테스트로 전파되지 않게 비운다.
        GeminiAnalyzer.clear_caches()
        os.environ.update({
            "BITHUMB_AI_PROVIDER": "groq", "BITHUMB_GROQ_API_KEY": "test-secret",
            "BITHUMB_GROQ_FAST_MODEL": "openai/gpt-oss-20b",
            "BITHUMB_GROQ_DEEP_MODEL": "openai/gpt-oss-120b",
        })

    def tearDown(self):
        # 테스트 중 생성된 계측 파일과 메모리 바인딩을 운영 경로에서 분리한다.
        self.telemetry_storage_path.unlink(missing_ok=True)
        self.telemetry_lock_path.unlink(missing_ok=True)
        AIProviderTelemetry.configure(data_dir=str(self.project_root / "data"))
        os.environ.clear()
        os.environ.update(self.env)

    @patch("ai_provider.requests.post")
    def test_fast_trading_uses_20b_and_best_effort_schema(self, mock_post):
        """신규 진입 분석은 20B 모델과 best-effort(strict=False) JSON Schema 및 로컬 검증을 사용해야 한다."""
        response = MagicMock(status_code=200)
        response.headers = {"x-ratelimit-reset-requests": "1h15m"}
        response.json.return_value = {"choices": [{"message": {"content": (
            '{"STATUS":"ACTIVE","ACTION":"HOLD","ENTRY_PRICE":100,"TARGET_PRICE":103,'
            '"STOP_LOSS":98,"ALLOC_PCT":0,"ALPHA_SCORE":0,"REASON":"대기"}'
        )}}]}
        mock_post.return_value = response

        analyzer = build_bithumb_analyzer()
        self.assertIsNotNone(analyzer)
        result = analyzer.analyze("KRW-TEST", 100.0, _candles(), 1_000_000.0, 0.0, 0.0)

        self.assertEqual(result["action"], "HOLD")
        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["model"], GroqProvider.FAST_TRADING)
        self.assertFalse(payload["response_format"]["json_schema"]["strict"])
        self.assertNotIn(GroqProvider.DEEP_BRIEFING, str(payload))
        # 모든 20B 분석 호출에는 빗썸 전용 안전 지침이 system 메시지로 선행해야 한다.
        self.assertEqual(payload["messages"][0]["role"], "system")
        self.assertEqual(payload["messages"][0]["content"], GroqProvider.SYSTEM_INSTRUCTION)
        self.assertIn("ACK는 체결이 아닙니다", payload["messages"][0]["content"])
        self.assertIn("업비트, Gemini", payload["messages"][0]["content"])
        self.assertIn("NEW_LISTING", payload["messages"][0]["content"])
        self.assertIn("한국어로만 작성", payload["messages"][0]["content"])
        self.assertIn("반드시 한국어로", payload["messages"][1]["content"])
        snapshot = AIProviderTelemetry.snapshot("bithumb")
        self.assertEqual(snapshot["reset_info"]["raw"], "1h15m")
        self.assertEqual(snapshot["reset_info"]["source"], "x-ratelimit-reset-requests")

    def test_bithumb_provider_label_is_groq(self):
        """빗썸 실행 로그의 Provider 표기가 Gemini로 남지 않도록 검증한다."""
        analyzer = build_bithumb_analyzer()
        self.assertIsNotNone(analyzer)
        self.assertEqual(analyzer.provider_label, "Groq")

    def test_groq_system_instruction_mentions_new_listing_path(self):
        """빗썸 Groq system 지침에 신규상장 단타(NEW_LISTING) 경로가 명시되어야 한다."""
        self.assertIn("NEW_LISTING", GroqProvider.SYSTEM_INSTRUCTION)
        self.assertIn("SCALP", GroqProvider.SYSTEM_INSTRUCTION)
        self.assertIn("SWING", GroqProvider.SYSTEM_INSTRUCTION)
        self.assertIn("CRASH", GroqProvider.SYSTEM_INSTRUCTION)
        self.assertIn("스크리너 단계 신규상장 사전 필터", GroqProvider.SYSTEM_INSTRUCTION)
        self.assertIn("is_new_listing_eligible()", GroqProvider.SYSTEM_INSTRUCTION)

    def test_groq_telemetry_restores_after_reconfigure(self):
        """동일 KST 날짜의 Groq 호출량은 프로세스 재시작 뒤에도 복원되어야 한다."""
        AIProviderTelemetry.record(
            "groq", "bithumb", "openai/gpt-oss-20b", "KRW-TEST", 200, 123.4,
            reset_requests="2h30m15.5s",
        )

        # configure 재호출은 새 프로세스가 같은 데이터 파일을 읽는 상황을 모사한다.
        AIProviderTelemetry.configure(
            data_dir=str(self.project_root / "data"), storage_filename=self.telemetry_storage_filename,
        )
        snapshot = AIProviderTelemetry.snapshot("bithumb")

        self.assertEqual(snapshot["api_calls"], 1)
        self.assertEqual(snapshot["api_success"], 1)
        self.assertEqual(snapshot["models"]["openai/gpt-oss-20b"]["calls"], 1)
        self.assertEqual(snapshot["reset_info"]["source"], "x-ratelimit-reset-requests")
        self.assertEqual(snapshot["reset_info"]["raw"], "2h30m15.5s")
        self.assertGreater(snapshot["reset_info"]["remaining_seconds"], 8_900)

    def test_groq_telemetry_merges_stale_process_delta_without_resetting_calls(self):
        """늦게 저장하는 재시작 전 프로세스도 최신 파일의 호출량을 0으로 덮어쓰면 안 된다."""
        AIProviderTelemetry.record(
            "groq", "bithumb", "openai/gpt-oss-20b", "PROCESS-A", 200, 100.0,
        )

        # 두 번째 프로세스가 첫 저장 전에 읽은 빈 메모리를 가진 상황을 직접 모사한다.
        key = ("groq", "bithumb", "openai/gpt-oss-20b")
        AIProviderTelemetry._stats = {
            key: {
                "calls": 1, "success": 1, "rate_limited": 0, "errors": 0,
                "latency_total_ms": 80.0, "last_event": "PROCESS-B HTTP 200", "last_event_at": 2.0,
            }
        }
        AIProviderTelemetry._persisted_stats = {}
        AIProviderTelemetry._save_state_locked()

        snapshot = AIProviderTelemetry.snapshot("bithumb")
        self.assertEqual(snapshot["api_calls"], 2)
        self.assertEqual(snapshot["api_success"], 2)
        self.assertEqual(snapshot["models"]["openai/gpt-oss-20b"]["calls"], 2)

    @patch("ai_provider.requests.post")
    def test_fast_failure_blocks_buy_without_120b_escalation(self, mock_post):
        """20B 429는 로컬 BUY나 120B 승격 없이 PAUSE/HOLD여야 한다."""
        mock_post.return_value = MagicMock(status_code=429)
        analyzer = build_bithumb_analyzer()
        result = analyzer.analyze("KRW-TEST", 100.0, _candles(), 1_000_000.0, 0.0, 0.0)

        self.assertEqual(result["status"], "PAUSE")
        self.assertEqual(result["action"], "HOLD")
        self.assertEqual(mock_post.call_count, 1)
        self.assertEqual(mock_post.call_args.kwargs["json"]["model"], GroqProvider.FAST_TRADING)
        safety = AIProviderTelemetry.snapshot("bithumb")["entry_safety"]
        self.assertTrue(safety["entry_blocked"])
        self.assertEqual(safety["reason"], "rate_limited")

    @patch("ai_provider.requests.post")
    def test_fast_http_error_records_safe_code_and_success_reopens_entry(self, mock_post):
        """HTTP 400 원문을 저장하지 않고 코드만 남기며 정상 FAST 응답 뒤에만 신규 진입을 재개한다."""
        failed = MagicMock(status_code=400)
        failed.headers = {}
        failed.json.return_value = {"error": {"message": "비밀 프롬프트는 저장하지 않는다", "type": "invalid_request_error"}}
        succeeded = MagicMock(status_code=200)
        succeeded.headers = {}
        succeeded.json.return_value = {"choices": [{"message": {"content": (
            '{"STATUS":"ACTIVE","ACTION":"HOLD","ENTRY_PRICE":100,"TARGET_PRICE":103,'
            '"STOP_LOSS":98,"ALLOC_PCT":0,"ALPHA_SCORE":0,"REASON":"대기"}'
        )}}]}
        mock_post.side_effect = [failed, succeeded]
        analyzer = build_bithumb_analyzer()

        analyzer.analyze("KRW-TEST", 100.0, _candles(), 1_000_000.0, 0.0, 0.0)
        blocked = AIProviderTelemetry.snapshot("bithumb")["entry_safety"]
        self.assertTrue(blocked["entry_blocked"])
        self.assertEqual(blocked["http_status"], 400)
        self.assertEqual(blocked["error_code"], "invalid_request_error")
        self.assertNotIn("비밀", str(blocked))

        analyzer.analyze("KRW-TEST", 100.0, _candles(), 1_000_000.0, 0.0, 0.0)
        reopened = AIProviderTelemetry.snapshot("bithumb")["entry_safety"]
        self.assertFalse(reopened["entry_blocked"])
        self.assertEqual(reopened["status"], "NORMAL")

    def test_groq_safe_error_summary_masks_sensitive_info(self):
        """오류 요약은 민감정보(키/토큰)를 마스킹하고 안전한 메시지만 추출해야 한다."""
        response = MagicMock()
        response.json.return_value = {
            "error": {
                "message": "Failed to validate JSON: missing field with bearer token_12345 secret_abc and gsk_fakekey123",
                "code": "json_validate_failed",
            }
        }
        summary = GroqProvider._safe_error_summary(response)
        code = GroqProvider._safe_error_code(response)
        self.assertEqual(code, "json_validate_failed")
        self.assertIn("Failed to validate JSON", summary)
        self.assertNotIn("token_12345", summary)
        self.assertNotIn("secret_abc", summary)
        self.assertNotIn("gsk_fakekey123", summary)

    def test_complete_json_defaults_to_non_strict(self):
        """GroqProvider.complete_json은 기본적으로 strict=False를 적용해야 한다."""
        provider = GroqProvider("fake_key", GroqProvider.FAST_TRADING, GroqProvider.DEEP_BRIEFING)
        with patch.object(provider, "_post") as mock_post:
            mock_post.return_value = MagicMock(value='{"STATUS":"ACTIVE"}', status_code=200, error_code="")
            provider.complete_json("test prompt", [GroqProvider.FAST_TRADING], {"type": "object"}, context="test", timeout=10.0, max_tokens=100)
            payload = mock_post.call_args[0][1]
            self.assertFalse(payload["response_format"]["json_schema"]["strict"])

    def test_entry_safety_restores_after_reconfigure(self):
        """프로세스 재시작을 모사해도 FAST 장애 신규 BUY 차단 상태가 유지되어야 한다."""
        AIProviderTelemetry.record_entry_safety(
            "bithumb", blocked=True, reason="http_error", context="macro_regime",
            model=GroqProvider.FAST_TRADING, status_code=400, error_code="invalid_request_error",
        )
        AIProviderTelemetry.configure(
            data_dir=str(self.project_root / "data"), storage_filename=self.telemetry_storage_filename,
        )
        restored = AIProviderTelemetry.snapshot("bithumb")["entry_safety"]
        self.assertTrue(restored["entry_blocked"])
        self.assertEqual(restored["context"], "macro_regime")

    def test_runtime_fast_failure_still_builds_analyzer_for_recovery(self):
        """FAST 장애 중에도 분석기는 유지되어 재시도로 entry_safety를 해제할 수 있어야 한다."""
        AIProviderTelemetry.record_entry_safety(
            "bithumb", blocked=True, reason="http_error", context="macro_regime",
            model=GroqProvider.FAST_TRADING, status_code=400,
        )
        self.assertIn("FAST", get_bithumb_ai_entry_block_reason())
        self.assertEqual(get_bithumb_ai_config_block_reason(), "")
        self.assertIsNotNone(build_bithumb_analyzer())

    def test_groq_batch_schemas_use_object_root(self):
        """Groq strict 모드는 object 루트만 허용하므로 배치 스키마도 래퍼 객체를 사용해야 한다."""
        self.assertEqual(RANKING_JSON_SCHEMA.get("type"), "object")
        self.assertIn("rankings", RANKING_JSON_SCHEMA.get("properties", {}))
        self.assertEqual(MACRO_JSON_SCHEMA.get("type"), "object")

    @patch("ai_provider.requests.post")
    def test_groq_macro_failure_uses_defensive_unavailable_regime(self, mock_post):
        """Groq 거시 진단 실패는 NORMAL로 위장하지 않고 방어 레짐과 공통 BUY 차단을 함께 반환한다."""
        failed = MagicMock(status_code=400)
        failed.headers = {}
        failed.json.return_value = {"error": {"type": "invalid_request_error"}}
        mock_post.return_value = failed
        analyzer = build_bithumb_analyzer()

        result = analyzer.diagnose_macro_regime(_candles(), fng_index={"desc": "중립"})

        self.assertEqual(result["regime"], "CAUTION_PULLBACK")
        self.assertEqual(result["provider_status"], "AI_UNAVAILABLE")
        self.assertTrue(AIProviderTelemetry.snapshot("bithumb")["entry_safety"]["entry_blocked"])
        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["response_format"]["json_schema"]["name"], "bithumb_macro_result")
        self.assertFalse(payload["response_format"]["json_schema"]["strict"])

    @patch("ai_provider.requests.post")
    def test_groq_macro_success_uses_batch_schema_and_reopens_entry(self, mock_post):
        """거시 진단 FAST 성공은 best-effort schema로 호출하고 entry_safety를 NORMAL로 복구해야 한다."""
        AIProviderTelemetry.record_entry_safety(
            "bithumb", blocked=True, reason="http_error", context="macro_regime",
            model=GroqProvider.FAST_TRADING, status_code=400,
        )
        response = MagicMock(status_code=200)
        response.headers = {}
        response.json.return_value = {"choices": [{"message": {"content": (
            '{"regime":"NORMAL","risk_score":40,"recommended_cash_ratio":0.3,'
            '"summary":"안정","action_guideline":"정상 운용"}'
        )}}]}
        mock_post.return_value = response
        analyzer = build_bithumb_analyzer()

        result = analyzer.diagnose_macro_regime(_candles(), fng_index={"desc": "중립"})

        self.assertEqual(result["regime"], "NORMAL")
        self.assertFalse(AIProviderTelemetry.snapshot("bithumb")["entry_safety"]["entry_blocked"])
        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["response_format"]["json_schema"]["schema"]["type"], "object")
        self.assertFalse(payload["response_format"]["json_schema"]["strict"])

    @patch("ai_provider.requests.post")
    def test_deep_briefing_only_may_fallback_to_20b(self, mock_post):
        """120B 브리핑 오류일 때만 20B 요약 브리핑 폴백을 허용한다."""
        failed = MagicMock(status_code=503)
        succeeded = MagicMock(status_code=200)
        succeeded.json.return_value = {"choices": [{"message": {"content": "• [거시 시황]: 안정\n• [계좌 진단]: 정상\n• [전략 제언]: 관망"}}]}
        mock_post.side_effect = [failed, succeeded]
        analyzer = build_bithumb_analyzer()
        text = analyzer.generate_market_briefing("빗썸", 1_000_000, 0, 0.0, "없음", {}, "중립")

        self.assertIn("[거시 시황]", text)
        self.assertEqual([call.kwargs["json"]["model"] for call in mock_post.call_args_list], [
            GroqProvider.DEEP_BRIEFING, GroqProvider.FAST_TRADING,
        ])
        # 120B 원본과 20B 폴백도 같은 system 지침을 받는지 함께 검증한다.
        for call in mock_post.call_args_list:
            self.assertEqual(call.kwargs["json"]["messages"][0]["role"], "system")
            self.assertEqual(call.kwargs["json"]["messages"][0]["content"], GroqProvider.SYSTEM_INSTRUCTION)

    def test_missing_or_wrong_configuration_blocks_only_bithumb_entry(self):
        """키·고정 모델 누락은 빗썸 분석기를 만들지 않아 신규 BUY만 차단한다."""
        os.environ["BITHUMB_GROQ_FAST_MODEL"] = "other-model"
        self.assertIsNone(build_bithumb_analyzer())
        self.assertIn("고정 모델", get_bithumb_ai_entry_block_reason())

    def test_upbit_entrypoint_remains_gemini_isolated(self):
        """업비트 진입점은 Bithumb Groq 환경 변수 대신 UPBIT Gemini 키를 유지한다."""
        source = (Path(__file__).resolve().parents[1] / "src" / "main_upbit.py").read_text(encoding="utf-8")
        self.assertIn("UPBIT_GEMINI_API_KEY", source)
        self.assertNotIn("BITHUMB_GROQ_API_KEY", source)


if __name__ == "__main__":
    unittest.main()
