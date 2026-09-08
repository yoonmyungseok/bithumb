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


if __name__ == "__main__":
    unittest.main()
