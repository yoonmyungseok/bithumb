"""빗썸·업비트 운영 슬롯과 노출 한도 결합 회귀 테스트."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from risk_controls import RiskGuard


class ExchangeSlotIsolationTests(unittest.TestCase):
    """실제 거래소별 기본 정책과 동일한 전체·단타·스윙·노출 경계를 검증한다."""

    def _guard(self, *, max_open_positions, max_position_pct, max_exposure, max_swing_positions):
        return RiskGuard(
            min_order_krw=5_000.0,
            max_open_positions=max_open_positions,
            max_position_pct=max_position_pct,
            max_total_exposure_pct=max_exposure,
            max_order_krw=0.0,
            max_swing_positions=max_swing_positions,
        )

    def test_bithumb_three_slots_reserve_one_swing_and_two_scalps(self):
        """빗썸 3슬롯에서 단타 2개 뒤 스윙 1개만 허용하고 네 번째는 차단한다."""
        guard = self._guard(max_open_positions=3, max_position_pct=0.35, max_exposure=0.90, max_swing_positions=1)
        common = dict(order_krw=200_000.0, available_krw=500_000.0, total_equity=1_000_000.0)

        self.assertEqual(
            guard.validate_buy("KRW-SWING", held_markets=["KRW-A", "KRW-B"], held_swing_markets=[], strategy_mode="SWING", **common),
            (True, "OK"),
        )
        allowed, reason = guard.validate_buy(
            "KRW-C", held_markets=["KRW-A", "KRW-B", "KRW-SWING"],
            held_swing_markets=["KRW-SWING"], strategy_mode="SCALP", **common,
        )
        self.assertFalse(allowed)
        self.assertIn("단타 전용", reason)

    def test_upbit_two_slots_reserve_one_swing_and_one_scalp(self):
        """업비트 2슬롯에서는 스윙과 단타가 각각 하나씩만 점유한다."""
        guard = self._guard(max_open_positions=2, max_position_pct=0.50, max_exposure=0.95, max_swing_positions=1)
        common = dict(order_krw=200_000.0, available_krw=600_000.0, total_equity=1_000_000.0)

        allowed, reason = guard.validate_buy(
            "KRW-ALT2", held_markets=["KRW-ALT1"], held_swing_markets=[], strategy_mode="SCALP", **common,
        )
        self.assertFalse(allowed)
        self.assertIn("단타 전용", reason)
        self.assertEqual(
            guard.validate_buy("KRW-SWING", held_markets=["KRW-ALT1"], held_swing_markets=[], strategy_mode="SWING", **common),
            (True, "OK"),
        )

    def test_exposure_and_position_limits_still_override_free_slots(self):
        """슬롯 여유가 있어도 종목당 및 총 투자 비중을 넘는 주문은 차단한다."""
        guard = self._guard(max_open_positions=3, max_position_pct=0.35, max_exposure=0.90, max_swing_positions=1)
        self.assertEqual(
            guard.validate_buy("KRW-A", 360_000.0, 900_000.0, 1_000_000.0, [], strategy_mode="SCALP"),
            (False, "종목당 비중 한도 초과"),
        )
        self.assertEqual(
            guard.validate_buy("KRW-A", 200_000.0, 250_000.0, 1_000_000.0, [], strategy_mode="SCALP"),
            (False, "총 투자 비중 한도 초과"),
        )


if __name__ == "__main__":
    unittest.main()
