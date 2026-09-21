"""스윙 4시간봉 EMA20 청산 및 데이터 불능 보호 테스트."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from strategy_engine import (
    StrategyPolicy,
    evaluate_swing_trend_entry,
    evaluate_swing_trend_exit,
    has_confirmed_swing_trend_candles,
    should_force_swing_data_unavailable_exit,
)


class SwingFourHourSafetyTests(unittest.TestCase):
    """1H가 아닌 4H 확정봉과 데이터 불능 시 보호 경계를 검증한다."""

    def test_swing_buffer_constants(self):
        """스윙 진입 및 청산 버퍼 비율 상수가 올바르게 정의되어 있는지 검증한다."""
        self.assertEqual(StrategyPolicy.SWING_ENTRY_EMA20_BUFFER_RATIO, 1.005)
        self.assertEqual(StrategyPolicy.SWING_TREND_EXIT_BUFFER_RATIO, 0.985)

    def test_swing_trend_entry_requires_ema20_support(self):
        """스윙 진입은 4H EMA20 상단 지지(>= 100%)가 확인될 때만 허용하고, 미달 시 차단한다."""
        candles_4h = [{"trade_price": 100.0} for _ in range(21)]
        # 1. EMA20(100.0) 미달 (97.0원: -3.0%) -> 차단
        allowed, reason = evaluate_swing_trend_entry(candles_4h, current_price=97.0)
        self.assertFalse(allowed)
        self.assertIn("4H EMA20 지지선 미달", reason)

        # 2. EMA20(100.0) 소폭 미달 (99.0원: -1.0%) -> 차단 (진입 기준 1.000 미달)
        allowed, reason = evaluate_swing_trend_entry(candles_4h, current_price=99.0)
        self.assertFalse(allowed)
        self.assertIn("4H EMA20 지지선 미달", reason)

        # 3. EMA20(100.0) 상단 지지 (101.0원: +1.0%) -> 승인
        allowed, reason = evaluate_swing_trend_entry(candles_4h, current_price=101.0)
        self.assertTrue(allowed)
        self.assertIn("4H EMA20 추세 지지 확인", reason)

    def test_swing_trend_entry_blocks_on_insufficient_candles(self):
        """4H 확정봉이 20개 미만이면 진입을 Fail-Closed로 차단한다."""
        candles_4h = [{"trade_price": 100.0} for _ in range(15)]
        allowed, reason = evaluate_swing_trend_entry(candles_4h, current_price=105.0)
        self.assertFalse(allowed)
        self.assertIn("4H 확정봉 부족", reason)

        allowed, reason = evaluate_swing_trend_entry([], current_price=105.0)
        self.assertFalse(allowed)
        self.assertIn("4H 확정봉 부족", reason)

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
