"""일일 퀀트 성과 분석 보고서 생성기 및 폴백/재시도 단위 테스트."""

import datetime
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.daily_quant_reporter import (
    DailyQuantReportGenerator,
    GenerationOutcome,
    GEMINI_FLASH_CANDIDATE_MODELS,
)


class TestDailyQuantReportGenerator(unittest.TestCase):
    """DailyQuantReportGenerator 단위 테스트."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.output_dir = Path(self.tmp_dir.name)
        self.start_dt = datetime.datetime(2026, 11, 6, 9, 0, 0)
        self.end_dt = datetime.datetime(2026, 11, 7, 8, 59, 59)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_pro_models_excluded_from_candidate_list(self):
        """Pro 모델이 후보 목록에서 완전히 배제되었는지 검증."""
        for model in GEMINI_FLASH_CANDIDATE_MODELS:
            self.assertNotIn("pro", model.lower(), f"Pro 모델이 포함되어 있습니다: {model}")
            self.assertIn("flash", model.lower(), f"Flash 계열이 아닙니다: {model}")

    @patch("src.daily_quant_reporter.analyze_exchange_data")
    @patch("src.daily_quant_reporter.fetch_btc_macro")
    @patch.object(DailyQuantReportGenerator, "_call_gemini_flash")
    def test_gemini_flash_success(self, mock_gemini, mock_btc, mock_ex):
        """1단계: Gemini Flash 성공 시 보고서 저장 및 성공 반환 검증."""
        mock_btc.return_value = {"open": 100, "close": 101, "change_pct": 1.0, "summary": "안정"}
        mock_ex.return_value = {
            "start_equity": 1000000.0,
            "total_trades": 2,
            "win_trades": 2,
            "loss_trades": 0,
            "even_trades": 0,
            "win_rate": 100.0,
            "total_pnl": 5000.0,
            "pnl_pct_on_equity": 0.5,
            "rr_ratio": 2.0,
            "total_fee": 100.0,
            "mdd_pct": 0.1,
            "mdd_krw": 1000.0,
        }
        mock_gemini.return_value = ("# 암호화폐 퀀트 매매 성과 분석 보고서 (24시간 롤링)\nGemini Flash 분석 내용", "gemini-3.5-flash")

        generator = DailyQuantReportGenerator(output_dir=self.output_dir)
        outcome = generator.generate(start_dt=self.start_dt, end_dt=self.end_dt)

        self.assertTrue(outcome.success)
        self.assertEqual(outcome.used_provider, "gemini_flash")
        self.assertEqual(outcome.used_model, "gemini-3.5-flash")
        self.assertFalse(outcome.needs_retry)
        self.assertTrue(os.path.exists(outcome.report_filepath))
        with open(outcome.report_filepath, "r", encoding="utf-8") as f:
            content = f.read()
            self.assertIn("Gemini Flash 분석 내용", content)

    @patch("src.daily_quant_reporter.analyze_exchange_data")
    @patch("src.daily_quant_reporter.fetch_btc_macro")
    @patch.object(DailyQuantReportGenerator, "_call_gemini_flash")
    @patch.object(DailyQuantReportGenerator, "_call_groq")
    def test_gemini_fail_groq_fallback_success(self, mock_groq, mock_gemini, mock_btc, mock_ex):
        """2단계: Gemini Flash 실패 시 Groq 폴백 호출 성공 검증."""
        mock_btc.return_value = {"open": 100, "close": 101, "change_pct": 1.0, "summary": "안정"}
        mock_ex.return_value = None
        mock_gemini.return_value = (None, "GEMINI_FLASH_FAILED")
        mock_groq.return_value = ("# 암호화폐 퀀트 매매 성과 분석 보고서 (24시간 롤링)\nGroq 폴백 분석 내용", "openai/gpt-oss-120b")

        generator = DailyQuantReportGenerator(output_dir=self.output_dir)
        outcome = generator.generate(start_dt=self.start_dt, end_dt=self.end_dt)

        self.assertTrue(outcome.success)
        self.assertEqual(outcome.used_provider, "groq")
        self.assertEqual(outcome.used_model, "openai/gpt-oss-120b")
        self.assertFalse(outcome.needs_retry)
        self.assertTrue(os.path.exists(outcome.report_filepath))
        with open(outcome.report_filepath, "r", encoding="utf-8") as f:
            content = f.read()
            self.assertIn("Groq 폴백 분석 내용", content)

    @patch("src.daily_quant_reporter.analyze_exchange_data")
    @patch("src.daily_quant_reporter.fetch_btc_macro")
    @patch.object(DailyQuantReportGenerator, "_call_gemini_flash")
    @patch.object(DailyQuantReportGenerator, "_call_groq")
    def test_all_llm_fail_template_fallback_and_retry(self, mock_groq, mock_gemini, mock_btc, mock_ex):
        """3단계: Gemini Flash & Groq 모두 실패 시 팩트 템플릿 발행 및 30분 재시도 플래그 설정 검증."""
        mock_btc.return_value = {"open": 100, "close": 101, "change_pct": 1.0, "summary": "안정"}
        mock_ex.return_value = {
            "start_equity": 1000000.0,
            "total_trades": 0,
            "win_trades": 0,
            "loss_trades": 0,
            "even_trades": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "pnl_pct_on_equity": 0.0,
            "rr_ratio": 0.0,
            "total_fee": 0.0,
            "mdd_pct": 0.0,
            "mdd_krw": 0.0,
        }
        mock_gemini.return_value = (None, "GEMINI_FLASH_FAILED")
        mock_groq.return_value = (None, "GROQ_FAILED")

        generator = DailyQuantReportGenerator(output_dir=self.output_dir)
        outcome = generator.generate(start_dt=self.start_dt, end_dt=self.end_dt)

        self.assertTrue(outcome.success)
        self.assertEqual(outcome.used_provider, "template")
        self.assertTrue(outcome.needs_retry)
        self.assertEqual(outcome.retry_delay_minutes, 30)
        self.assertTrue(os.path.exists(outcome.report_filepath))
        with open(outcome.report_filepath, "r", encoding="utf-8") as f:
            content = f.read()
            self.assertIn("정량 팩트 데이터 기반 템플릿", content)


if __name__ == "__main__":
    unittest.main()
