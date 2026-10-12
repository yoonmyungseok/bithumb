import datetime
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from strategy_engine import (
    StrategyPolicy,
    entry_signal,
    get_momentum_breakout_alpha_threshold,
    is_weekend_session,
    KST,
)


class StrategyWeekendRegimeTests(unittest.TestCase):
    def test_is_weekend_session_classification(self):
        """KST 기준 주말 저유동성 세션(금 23:00 ~ 일 23:59:59) 판별 검증."""
        # 평일 수요일 14:00 (2026-10-14 14:00:00 KST)
        wednesday = datetime.datetime(2026, 10, 14, 14, 0, 0, tzinfo=KST)
        self.assertFalse(is_weekend_session(wednesday))

        # 금요일 22:59 (2026-10-16 22:59:00 KST)
        friday_before_night = datetime.datetime(2026, 10, 16, 22, 59, 0, tzinfo=KST)
        self.assertFalse(is_weekend_session(friday_before_night))

        # 금요일 23:00 (2026-10-16 23:00:00 KST) -> 주말 세션 진입
        friday_night = datetime.datetime(2026, 10, 16, 23, 0, 0, tzinfo=KST)
        self.assertTrue(is_weekend_session(friday_night))

        # 토요일 15:30 (2026-10-17 15:30:00 KST)
        saturday = datetime.datetime(2026, 10, 17, 15, 30, 0, tzinfo=KST)
        self.assertTrue(is_weekend_session(saturday))

        # 일요일 23:59 (2026-10-18 23:59:00 KST)
        sunday_end = datetime.datetime(2026, 10, 18, 23, 59, 0, tzinfo=KST)
        self.assertTrue(is_weekend_session(sunday_end))

        # 월요일 00:00 (2026-10-19 00:00:00 KST) -> 주말 세션 종료
        monday_start = datetime.datetime(2026, 10, 19, 0, 0, 0, tzinfo=KST)
        self.assertFalse(is_weekend_session(monday_start))

    def test_get_momentum_breakout_alpha_threshold_on_weekend(self):
        """주말 세션에서 모멘텀 돌파 알파 점수 기준이 78점 이상으로 상향되는지 검증."""
        # 평일 주간 정상장 기준 -> 72점
        normal_thresh = get_momentum_breakout_alpha_threshold(
            btc_regime="NORMAL", is_night=False, is_weekend=False
        )
        self.assertEqual(normal_thresh, StrategyPolicy.MOMENTUM_BREAKOUT_ALPHA_THRESHOLD_NORMAL)
        self.assertEqual(normal_thresh, 72)

        # 주말 세션 -> 78점으로 동적 상향
        weekend_thresh = get_momentum_breakout_alpha_threshold(
            btc_regime="NORMAL", is_night=False, is_weekend=True
        )
        self.assertEqual(weekend_thresh, StrategyPolicy.WEEKEND_MOMENTUM_ALPHA_THRESHOLD)
        self.assertEqual(weekend_thresh, 78)

    @patch("strategy_engine.is_weekend_session", return_value=True)
    def test_entry_signal_momentum_breakout_rejects_sub_1_5x_volume_on_weekend(self, _mock_weekend):
        """주말 저유동성 세션에서 거래량 배수가 1.5배 미달(예: 1.3배)이면 모멘텀 돌파가 차단되는지 검증."""
        # 30개 봉 캔들 생성: 직전 평균 거래량 1000개, 현재 봉 거래량 1300개 (1.3배 -> 평일 1.2배는 통과하나 주말 1.5배 미달)
        candles = []
        for i in range(30):
            candles.append({
                "trade_price": 100.0 + (i * 0.1),
                "opening_price": 99.5 + (i * 0.1),
                "high_price": 101.0 + (i * 0.1),
                "low_price": 99.0 + (i * 0.1),
                "candle_acc_trade_volume": 1000.0,
            })
        # 현재 봉 (0번)
        candles[0] = {
            "trade_price": 105.0,  # 전고점 돌파
            "opening_price": 102.0,
            "high_price": 105.1,
            "low_price": 101.9,
            "candle_acc_trade_volume": 1300.0,  # 1.3배
        }

        signal = entry_signal(
            candles=candles,
            entry_type="MOMENTUM_BREAKOUT",
            btc_regime="NORMAL",
            is_night=False,
        )

        # 주말 거래량 1.5배 미달로 매수 차단 확인
        self.assertFalse(signal["allow_buy"])
        self.assertFalse(signal["momentum_breakout_passed"])
        self.assertIn("주말 저유동성 가드(최소 1.5배)", signal["reason"])


if __name__ == "__main__":
    unittest.main()
