"""Gemini 신규 진입 프롬프트의 전략·안전 계약을 검증한다."""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch


# 테스트 실행 위치와 무관하게 프로젝트 src 경로를 등록한다.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from gemini_analyzer import GeminiAnalyzer


class TestGeminiEntryPromptContract(unittest.TestCase):
    """신규 진입 AI 요청이 증거 우선·경로 분리 안전 계약을 포함하는지 검증한다."""

    @patch("requests.post")
    def test_entry_prompt_requires_evidence_and_invalidation(self, mock_post):
        """실제 API 전송 본문에 판정불가 HOLD와 반증 절차가 포함되어야 한다."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": (
                '{"STATUS":"ACTIVE","ACTION":"HOLD","ENTRY_PRICE":1000,'
                '"TARGET_PRICE":1030,"STOP_LOSS":980,"ALLOC_PCT":0,'
                '"ALPHA_SCORE":0,"REASON":"판정불가"}'
            )}]}}]
        }
        mock_post.return_value = mock_response

        analyzer = GeminiAnalyzer(api_key="test-key")
        candles = [
            {
                "trade_price": 1000.0,
                "opening_price": 995.0,
                "high_price": 1010.0,
                "low_price": 990.0,
                "candle_acc_trade_volume": 100.0,
                "candle_date_time_utc": "2026-09-06T00:00:00",
            }
            for _ in range(30)
        ]

        analyzer.analyze(
            market="KRW-TEST",
            current_price=1000.0,
            candles=candles,
            krw_balance=1_000_000.0,
            coin_balance=0.0,
            avg_buy_price=0.0,
            candidate_type="MOMENTUM_BREAKOUT",
            entry_policy_mode="STANDARD",
            momentum_phase="EXTENDED",
        )

        sent_prompt = mock_post.call_args.kwargs["json"]["contents"][0]["parts"][0]["text"]
        self.assertIn("증거 우선 의사결정 절차", sent_prompt)
        self.assertIn("판정불가", sent_prompt)
        self.assertIn("반대 근거", sent_prompt)
        self.assertIn("무효화 조건", sent_prompt)
        self.assertIn("MOMENTUM_BREAKOUT", sent_prompt)
        self.assertIn("모멘텀 단계: EXTENDED", sent_prompt)
        self.assertIn("신규 BUY는 EARLY 단계에서만", sent_prompt)
        self.assertIn("RECOVERY_REBOUND", sent_prompt)
        self.assertIn('"ALPHA_SCORE"', sent_prompt)
        self.assertIn('"REASON"', sent_prompt)


if __name__ == "__main__":
    unittest.main()
