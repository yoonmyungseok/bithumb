"""스윙 4시간봉 EMA20 청산 및 데이터 불능 보호 테스트."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from strategy_engine import (
    StrategyPolicy,
    evaluate_swing_trend_exit,
    has_confirmed_swing_trend_candles,
    should_force_swing_data_unavailable_exit,
)


class SwingFourHourSafetyTests(unittest.TestCase):
    """1H가 아닌 4H 확정봉과 데이터 불능 시 보호 경계를 검증한다."""

    def test_only_confirmed_four_hour_candles_enable_ema20_trend_exit(self):
        """진행 중 첫 봉을 제외한 20개 4H 봉이 있을 때만 EMA20 이탈을 판정한다."""
        candles_4h = [{"trade_price": 100.0} for _ in range(21)]
        self.assertTrue(has_confirmed_swing_trend_candles(candles_4h))
        is_exit, reason = evaluate_swing_trend_exit(candles_4h, current_price=97.0)
        self.assertTrue(is_exit)
        self.assertIn("4H EMA20", reason)

    def test_missing_four_hour_data_blocks_new_buy_and_uses_bounded_protection_fallback(self):
        """4H 부족은 진입을 위한 확정봉 검증을 실패시키고 12시간 뒤에만 보호 청산한다."""
        self.assertFalse(has_confirmed_swing_trend_candles([{"trade_price": 100.0}] * 20))
        is_exit, reason = evaluate_swing_trend_exit([], current_price=100.0)
        self.assertFalse(is_exit)
        self.assertIn("신규 BUY 차단", reason)
        self.assertFalse(should_force_swing_data_unavailable_exit(
            StrategyPolicy.SWING_4H_DATA_UNAVAILABLE_MAX_HOLD_SECONDS - 1,
        ))
        self.assertTrue(should_force_swing_data_unavailable_exit(
            StrategyPolicy.SWING_4H_DATA_UNAVAILABLE_MAX_HOLD_SECONDS,
        ))


if __name__ == "__main__":
    unittest.main()
