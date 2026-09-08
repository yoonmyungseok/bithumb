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
        self.assertIn("반드시 한국어로", sent_prompt)

    @patch("requests.post")
    def test_recovery_rebound_prompt_mentions_1b_min_trade_value(self, mock_post):
        """RECOVERY_REBOUND 모드에서 프롬프트에 10억 원 이상 거래대금 조건이 명시되는지 검증"""
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
            btc_regime="RISK_OFF",
            entry_policy_mode="RECOVERY_REBOUND",
        )

        sent_prompt = mock_post.call_args.kwargs["json"]["contents"][0]["parts"][0]["text"]
        self.assertIn("24시간 거래대금 10억 원 이상", sent_prompt)

    @patch("requests.post")
    def test_new_listing_prompt_mentions_policy_and_alpha_threshold(self, mock_post):
        """NEW_LISTING 후보 유형에서 경로 조건·4H 면제·알파 기준·RISK_OFF 엄격 기준 문구가 포함되어야 한다."""
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
            candidate_type="NEW_LISTING",
            entry_policy_mode="STANDARD",
            btc_regime="NORMAL",
            is_night=False,
        )

        sent_prompt = mock_post.call_args.kwargs["json"]["contents"][0]["parts"][0]["text"]
        self.assertIn("후보 유형: NEW_LISTING", sent_prompt)
        self.assertIn("신규 상장 단타(NEW_LISTING) 전용 경로", sent_prompt)
        self.assertIn("4H/1H MTF 게이트는 면제", sent_prompt)
        self.assertIn("현재 알파 승인 기준: 75점 이상", sent_prompt)
        self.assertIn("20억 원", sent_prompt)
        self.assertIn("1.5%~8%", sent_prompt)
        self.assertIn("MOMENTUM_BREAKOUT/RECOVERY_REBOUND/CONFIRMED 근거로 NEW_LISTING 조건을 대체하지 마세요", sent_prompt)
        self.assertIn("신규상장 초기 수급을 일반 확인형·모멘텀·반등 근거로 바꾸어 해석하지 마세요", sent_prompt)

        analyzer.analyze(
            market="KRW-TEST",
            current_price=1000.0,
            candles=candles,
            krw_balance=1_000_000.0,
            coin_balance=0.0,
            avg_buy_price=0.0,
            candidate_type="NEW_LISTING",
            entry_policy_mode="STANDARD",
            btc_regime="RISK_OFF",
            is_night=True,
        )
        risk_off_prompt = mock_post.call_args.kwargs["json"]["contents"][0]["parts"][0]["text"]
        self.assertIn("현재 알파 승인 기준: 85점 이상", risk_off_prompt)
        self.assertIn("약세장 신규 상장 단타(NEW_LISTING) 경로", risk_off_prompt)
        self.assertIn("30억 원 이상", risk_off_prompt)


if __name__ == "__main__":
    unittest.main()

