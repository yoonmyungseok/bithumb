"""빗썸 Groq 전환의 모델 고정·fail-closed·업비트 격리를 검증한다."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from ai_provider import AIProviderTelemetry, GroqProvider
from bithumb_ai import build_bithumb_analyzer, get_bithumb_ai_entry_block_reason


def _candles() -> list[dict[str, float | str]]:
    """과열 가드가 아닌 Provider 결과를 확인할 최소 정상 캔들을 생성한다."""
    return [{
        "trade_price": 100.0, "opening_price": 100.0, "high_price": 101.0, "low_price": 99.0,
        "candle_acc_trade_volume": 100.0, "candle_date_time_utc": "2026-09-07T00:00:00",
    } for _ in range(30)]


class BithumbGroqProviderTests(unittest.TestCase):
    """빗썸 전용 Groq 경계가 주문 경계 밖에서 fail-closed되는지 확인한다."""

    def setUp(self):
        self.env = dict(os.environ)
        self.project_root = Path(__file__).resolve().parents[1]
        self.telemetry_storage_filename = "groq_telemetry_test.json"
        self.telemetry_storage_path = self.project_root / "data" / self.telemetry_storage_filename
        # 모든 Groq 호출 테스트를 운영 계측 파일과 분리한다.
        self.telemetry_storage_path.unlink(missing_ok=True)
        AIProviderTelemetry.configure(
            data_dir=str(self.project_root / "data"), storage_filename=self.telemetry_storage_filename,
        )
        AIProviderTelemetry.reset()
        os.environ.update({
            "BITHUMB_AI_PROVIDER": "groq", "BITHUMB_GROQ_API_KEY": "test-secret",
            "BITHUMB_GROQ_FAST_MODEL": "openai/gpt-oss-20b",
            "BITHUMB_GROQ_DEEP_MODEL": "openai/gpt-oss-120b",
        })

    def tearDown(self):
        # 테스트 중 생성된 계측 파일과 메모리 바인딩을 운영 경로에서 분리한다.
        self.telemetry_storage_path.unlink(missing_ok=True)
        AIProviderTelemetry.configure(data_dir=str(self.project_root / "data"))
        os.environ.clear()
        os.environ.update(self.env)

    @patch("ai_provider.requests.post")
    def test_fast_trading_uses_20b_and_strict_schema(self, mock_post):
        """신규 진입 분석은 20B 한 모델과 strict JSON Schema만 사용해야 한다."""
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
        self.assertTrue(payload["response_format"]["json_schema"]["strict"])
        self.assertNotIn(GroqProvider.DEEP_BRIEFING, str(payload))
        snapshot = AIProviderTelemetry.snapshot("bithumb")
        self.assertEqual(snapshot["reset_info"]["raw"], "1h15m")
        self.assertEqual(snapshot["reset_info"]["source"], "x-ratelimit-reset-requests")

    def test_bithumb_provider_label_is_groq(self):
        """빗썸 실행 로그의 Provider 표기가 Gemini로 남지 않도록 검증한다."""
        analyzer = build_bithumb_analyzer()
        self.assertIsNotNone(analyzer)
        self.assertEqual(analyzer.provider_label, "Groq")

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
