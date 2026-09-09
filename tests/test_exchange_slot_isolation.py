"""빗썸·업비트 운영 슬롯과 노출 한도 결합 회귀 테스트."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from risk_controls import RiskGuard


class ExchangeSlotIsolationTests(unittest.TestCase):
    """실제 거래소별 기본 정책과 동일한 전체·단타·스윙·신규상장·노출 경계를 검증한다."""

    def _guard(
        self,
        *,
        max_open_positions,
        max_position_pct,
        max_exposure,
        max_swing_positions,
        max_new_listing_positions=0,
    ):
        return RiskGuard(
            min_order_krw=5_000.0,
            max_open_positions=max_open_positions,
            max_position_pct=max_position_pct,
            max_total_exposure_pct=max_exposure,
            max_order_krw=0.0,
            max_swing_positions=max_swing_positions,
            max_new_listing_positions=max_new_listing_positions,
        )

    def test_bithumb_triple_track_reserves_swing_new_listing_and_one_scalp(self):
        """빗썸 3슬롯: 스윙 1 + 신규상장 1 + 단타 1 구성을 검증한다."""
        guard = self._guard(
            max_open_positions=3,
            max_position_pct=0.35,
            max_exposure=0.90,
            max_swing_positions=1,
            max_new_listing_positions=1,
        )
        common = dict(order_krw=200_000.0, available_krw=500_000.0, total_equity=1_000_000.0)
        new_listing_common = dict(order_krw=50_000.0, available_krw=500_000.0, total_equity=1_000_000.0)

        self.assertEqual(
            guard.validate_buy(
                "KRW-SWING",
                held_markets=["KRW-A"],
                held_swing_markets=[],
                held_new_listing_markets=[],
                strategy_mode="SWING",
                **common,
            ),
            (True, "OK"),
        )
        self.assertEqual(
            guard.validate_buy(
                "KRW-NEW",
                held_markets=["KRW-A", "KRW-SWING"],
                held_swing_markets=["KRW-SWING"],
                held_new_listing_markets=[],
                strategy_mode="NEW_LISTING",
                **new_listing_common,
            ),
            (True, "OK"),
        )
        allowed, reason = guard.validate_buy(
            "KRW-SCALP2",
            held_markets=["KRW-A", "KRW-SWING", "KRW-NEW"],
            held_swing_markets=["KRW-SWING"],
            held_new_listing_markets=["KRW-NEW"],
            strategy_mode="SCALP",
            **common,
        )
        self.assertFalse(allowed)
        self.assertIn("단타 전용", reason)

    def test_second_new_listing_buy_blocked_when_slot_full(self):
        """신규상장 1건 보유 시 두 번째 NEW_LISTING BUY는 RiskGuard가 차단한다."""
        guard = self._guard(
            max_open_positions=3,
            max_position_pct=0.35,
            max_exposure=0.90,
            max_swing_positions=1,
            max_new_listing_positions=1,
        )
        common = dict(order_krw=50_000.0, available_krw=500_000.0, total_equity=1_000_000.0)
        allowed, reason = guard.validate_buy(
            "KRW-NEW2",
            held_markets=["KRW-NEW1"],
            held_swing_markets=[],
            held_new_listing_markets=["KRW-NEW1"],
            strategy_mode="NEW_LISTING",
            **common,
        )
        self.assertFalse(allowed)
        self.assertIn("신규상장 전용", reason)

    def test_upbit_two_slots_reserve_one_swing_one_new_listing_zero_scalp(self):
        """업비트 2슬롯 레거시/최소 설정에서는 스윙·신규상장 예약 후 단타 슬롯이 0이 된다."""
        guard = self._guard(
            max_open_positions=2,
            max_position_pct=0.50,
            max_exposure=0.95,
            max_swing_positions=1,
            max_new_listing_positions=1,
        )
        common = dict(order_krw=200_000.0, available_krw=600_000.0, total_equity=1_000_000.0)

        allowed, reason = guard.validate_buy(
            "KRW-ALT1",
            held_markets=[],
            held_swing_markets=[],
            held_new_listing_markets=[],
            strategy_mode="SCALP",
            **common,
        )
        self.assertFalse(allowed)
        self.assertIn("단타 전용", reason)
        self.assertEqual(
            guard.validate_buy(
                "KRW-SWING",
                held_markets=[],
                held_swing_markets=[],
                held_new_listing_markets=[],
                strategy_mode="SWING",
                **common,
            ),
            (True, "OK"),
        )

    def test_upbit_three_slots_standard_alignment(self):
        """업비트 3슬롯 정합화 설정: 스윙 1 + 신규상장 1 + 단타 1 슬롯을 지원한다."""
        guard = self._guard(
            max_open_positions=3,
            max_position_pct=0.35,
            max_exposure=0.90,
            max_swing_positions=1,
            max_new_listing_positions=1,
        )
        common = dict(order_krw=200_000.0, available_krw=500_000.0, total_equity=1_000_000.0)
        allowed, reason = guard.validate_buy(
            "KRW-ALT1",
            held_markets=[],
            held_swing_markets=[],
            held_new_listing_markets=[],
            strategy_mode="SCALP",
            **common,
        )
        self.assertTrue(allowed)
        self.assertEqual(reason, "OK")

    def test_dual_track_backward_compat_when_new_listing_slot_zero(self):
        """신규상장 슬롯 미예약(0)이면 기존 Dual-Track 단타 2슬롯 동작을 유지한다."""
        guard = self._guard(
            max_open_positions=3,
            max_position_pct=0.35,
            max_exposure=0.90,
            max_swing_positions=1,
            max_new_listing_positions=0,
        )
        common = dict(order_krw=200_000.0, available_krw=500_000.0, total_equity=1_000_000.0)

        self.assertEqual(
            guard.validate_buy(
                "KRW-SWING",
                held_markets=["KRW-A", "KRW-B"],
                held_swing_markets=[],
                strategy_mode="SWING",
                **common,
            ),
            (True, "OK"),
        )
        allowed, reason = guard.validate_buy(
            "KRW-C",
            held_markets=["KRW-A", "KRW-B", "KRW-SWING"],
            held_swing_markets=["KRW-SWING"],
            strategy_mode="SCALP",
            **common,
        )
        self.assertFalse(allowed)
        self.assertIn("단타 전용", reason)

    def test_exposure_and_position_limits_still_override_free_slots(self):
        """슬롯 여유가 있어도 종목당 및 총 투자 비중을 넘는 주문은 차단한다."""
        guard = self._guard(
            max_open_positions=3,
            max_position_pct=0.35,
            max_exposure=0.90,
            max_swing_positions=1,
            max_new_listing_positions=1,
        )
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
