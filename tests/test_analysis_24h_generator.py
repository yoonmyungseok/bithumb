"""수동 매수 종목 24h 시세 분석 보고서 생성기 단위 테스트."""

import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

from src.analysis_24h_generator import Analysis24hGenerator, KST
from src.groq_provider import GroqResult


class TestAnalysis24hGenerator(unittest.TestCase):
    """Analysis24hGenerator의 데이터 수집, 프롬프트 빌드, 파일 저장 및 실행 로직 검증."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.generator = Analysis24hGenerator(
            exchange_scope="all",
            output_dir=self.temp_dir,
            min_eval_krw=1000.0,
        )

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)

    def test_save_report_atomic(self):
        """save_report가 지정된 파일명 패턴으로 올바르게 원자적 저장되는지 검증."""
        currency = "HOLO"
        content = "# 홀로월드에이아이(HOLO) 24시간 시세 분석 보고서\n\n내용입니다."
        date_str = "20261029"

        saved_path = self.generator.save_report(currency, content, date_str)
        self.assertTrue(os.path.exists(saved_path))
        self.assertEqual(os.path.basename(saved_path), "HOLO_24h_20261029.md")

        with open(saved_path, "r", encoding="utf-8") as f:
            read_content = f.read()
        self.assertIn("# 홀로월드에이아이(HOLO)", read_content)

    def test_build_prompts_structure(self):
        """build_prompts가 팩트 데이터와 5개 섹션 마크다운 가이드라인을 정확히 포함하는지 검증."""
        pos = {
            "exchange": "UPBIT",
            "currency": "HOLO",
            "market": "KRW-HOLO",
            "volume": 100000.0,
            "avg_buy_price": 100.0,
            "current_price": 80.0,
            "total_buy_price": 10000000.0,
            "eval_amount": 8000000.0,
            "unrealized_pnl": -2000000.0,
            "unrealized_pnl_pct": -20.0,
        }
        mkt = {
            "current_price": 80.0,
            "change_rate": 2.5,
            "change_price": 2.0,
            "high_price_24h": 82.0,
            "low_price_24h": 77.0,
            "acc_trade_price_24h": 500000000.0,
            "acc_trade_volume_24h": 6250000.0,
            "daily_candles": [
                {
                    "date": "2026-10-28",
                    "opening_price": 78.0,
                    "high_price": 82.0,
                    "low_price": 77.0,
                    "trade_price": 80.0,
                    "candle_acc_trade_price": 500000000.0,
                }
            ],
        }
        korean_name = "홀로월드에이아이"
        total_account_buy = 20000000.0
        now_dt = datetime(2026, 10, 29, 9, 0, 0, tzinfo=KST)

        sys_prompt, user_prompt = self.generator.build_prompts(
            pos, mkt, korean_name, total_account_buy, now_dt
        )

        # 시스템 프롬프트 검증
        self.assertIn("암호화폐 퀀트 투자 분석가", sys_prompt)
        self.assertIn("마크다운", sys_prompt)

        # 유저 프롬프트 팩트 데이터 검증
        self.assertIn("KRW-HOLO", user_prompt)
        self.assertIn("100,000.0000 HOLO", user_prompt)
        self.assertIn("100.00 KRW", user_prompt)
        self.assertIn("80.00 KRW", user_prompt)
        self.assertIn("50.00%", user_prompt)  # 포트폴리오 비중 (1000만/2000만 = 50%)
        self.assertIn("## 1. 홀로월드에이아이(HOLO) 최근 24시간 흐름 분석", user_prompt)
        self.assertIn("## 2. 향후 24시간 단기 흐름 예측", user_prompt)
        self.assertIn("## 3. 단기 / 중기 / 장기 흐름 예측", user_prompt)
        self.assertIn("## 4. 보유 평단가 기반 대응 전략 분석", user_prompt)
        self.assertIn("## 5. 보고서 핵심 요약", user_prompt)

    @patch.object(Analysis24hGenerator, "fetch_market_data")
    def test_generate_report_for_position_success(self, mock_fetch):
        """generate_report_for_position이 Groq 응답을 받아 코드블록 정제 후 반환하는지 검증."""
        mock_fetch.return_value = {
            "current_price": 80.0,
            "change_rate": 1.0,
            "change_price": 1.0,
            "high_price_24h": 81.0,
            "low_price_24h": 78.0,
            "acc_trade_price_24h": 100000000.0,
            "acc_trade_volume_24h": 1250000.0,
            "daily_candles": [],
        }

        mock_groq = MagicMock()
        mock_groq.complete_text.return_value = GroqResult(
            value="```markdown\n# 홀로월드에이아이(HOLO) 보고서 본문\n```",
            model="openai/gpt-oss-120b",
            success=True,
            latency_sec=1.2,
        )
        self.generator.upbit_groq = mock_groq

        pos = {
            "exchange": "UPBIT",
            "currency": "HOLO",
            "market": "KRW-HOLO",
            "volume": 100.0,
            "avg_buy_price": 100.0,
            "current_price": 80.0,
        }

        content = self.generator.generate_report_for_position(pos, total_account_buy=10000.0)
        self.assertIsNotNone(content)
        self.assertEqual(content, "# 홀로월드에이아이(HOLO) 보고서 본문")

    @patch.object(Analysis24hGenerator, "get_target_positions")
    @patch.object(Analysis24hGenerator, "generate_report_for_position")
    def test_run_generates_and_saves_files(self, mock_gen, mock_get_pos):
        """run 메서드가 수동 종목 리스트를 순회하며 올바르게 저장하고 반환하는지 검증."""
        mock_get_pos.return_value = [
            {
                "exchange": "UPBIT",
                "currency": "HOLO",
                "market": "KRW-HOLO",
                "volume": 100.0,
                "total_buy_price": 10000.0,
            },
            {
                "exchange": "UPBIT",
                "currency": "RSR",
                "market": "KRW-RSR",
                "volume": 200.0,
                "total_buy_price": 20000.0,
            },
        ]
        mock_gen.side_effect = [
            "# HOLO 보고서 내용",
            "# RSR 보고서 내용",
        ]

        saved_files = self.generator.run(now_dt=datetime(2026, 10, 29, 9, 0, 0, tzinfo=KST))

        self.assertEqual(len(saved_files), 2)
        self.assertTrue(any("HOLO_24h_20261029.md" in p for p in saved_files))
        self.assertTrue(any("RSR_24h_20261029.md" in p for p in saved_files))


if __name__ == "__main__":
    unittest.main()
