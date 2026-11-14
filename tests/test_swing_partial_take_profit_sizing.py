"""빗썸·업비트 스윙 분할익절 원보유 수량 정합성 테스트."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from order_safety import calculate_partial_take_profit_volume


class SwingPartialTakeProfitSizingTests(unittest.TestCase):
    """거래소 경계와 무관하게 확정 원보유 수량을 기준으로 스윙 수량을 계산한다."""

    def test_bithumb_swing_first_and_second_take_profit_use_original_fill_volume(self):
        """빗썸은 최초 100개 체결에서 1차 40개, 잔여 60개에서 2차 30개를 제출한다."""
        self.assertEqual(
            calculate_partial_take_profit_volume(
                is_swing=True, stage=1, available_volume=100.0, original_filled_volume=100.0,
            ),
            40.0,
        )
        self.assertEqual(
            calculate_partial_take_profit_volume(
                is_swing=True, stage=2, available_volume=60.0, original_filled_volume=100.0,
            ),
            30.0,
        )

    def test_upbit_swing_second_take_profit_remains_thirty_percent_after_partial_first_fill(self):
        """업비트 1차가 20개만 체결되어도 2차는 잔여 80개 중 원보유 기준 30개를 제출한다."""
        self.assertEqual(
            calculate_partial_take_profit_volume(
                is_swing=True, stage=2, available_volume=80.0, original_filled_volume=100.0,
            ),
            30.0,
        )

    def test_swing_never_oversells_remaining_balance_and_scalp_ratio_is_unchanged(self):
        """스윙 잔고 부족 시 과매도를 막고 단타의 기존 잔여 비율 계산은 보존한다."""
        self.assertEqual(
            calculate_partial_take_profit_volume(
                is_swing=True, stage=2, available_volume=25.0, original_filled_volume=100.0,
            ),
            25.0,
        )
        self.assertEqual(
            calculate_partial_take_profit_volume(
                is_swing=False, stage=2, available_volume=60.0, scalp_stage_ratio=0.5,
            ),
            30.0,
        )

    def test_small_account_falls_back_to_full_exit_when_partial_or_remainder_below_min_order(self):
        """소액 포지션에서 분할 매도액 또는 잔여 평가액이 최소주문금액 미만이면 전량 익절로 전환한다."""
        # 보유 10,000원(10개 * 1,000원), 40% 분할 시 매도액 4,000원 < 5,000원 -> 전량 10개 익절
        self.assertEqual(
            calculate_partial_take_profit_volume(
                is_swing=False, stage=1, available_volume=10.0, scalp_stage_ratio=0.4,
                current_price=1000.0, min_order_krw=5000.0,
            ),
            10.0,
        )
        # 보유 11,000원(11개 * 1,000원), 60% 분할 시 매도액 6,600원은 통과하나 잔여 4,400원 < 5,000원 -> Dust 방지를 위해 전량 11개 익절
        self.assertEqual(
            calculate_partial_take_profit_volume(
                is_swing=False, stage=1, available_volume=11.0, scalp_stage_ratio=0.6,
                current_price=1000.0, min_order_krw=5000.0,
            ),
            11.0,
        )

    def test_scaled_account_keeps_partial_take_profit_when_both_sides_meet_min_order(self):
        """시드 증액으로 분할 매도액과 잔여액이 모두 최소주문금액 이상이면 원래 분할 비율을 유지한다."""
        # 보유 50,000원(50개 * 1,000원), 60% 분할 시 매도액 30,000원 >= 5,000원, 잔여 20,000원 >= 5,000원
        self.assertEqual(
            calculate_partial_take_profit_volume(
                is_swing=False, stage=1, available_volume=50.0, scalp_stage_ratio=0.6,
                current_price=1000.0, min_order_krw=5000.0,
            ),
            30.0,
        )


if __name__ == "__main__":
    unittest.main()
