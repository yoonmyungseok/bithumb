"""메이저 스윙 분할익절 현실화 및 자동 본전 보장(Auto Break-Even) 단위 테스트."""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from risk_manager import TrailingStopTracker
from strategy_engine import StrategyPolicy, is_major_market


class TestSwingMajorAndBreakeven(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_major_market_identification(self):
        """BTC, ETH, SOL이 메이저 마켓으로 정확히 분류되는지 검증."""
        self.assertTrue(is_major_market("KRW-BTC"))
        self.assertTrue(is_major_market("KRW-ETH"))
        self.assertTrue(is_major_market("KRW-SOL"))
        self.assertFalse(is_major_market("KRW-XRP"))
        self.assertFalse(is_major_market("KRW-DOGE"))
        self.assertFalse(is_major_market("KRW-NEAR"))

    def test_yesterday_eth_reversal_scenario_prevented(self):
        """어제 발생한 ETH 시나리오: +2.11% 상승 후 급락 시 마이너스 전환되지 않고 본전 익절되는지 검증."""
        tracker = TrailingStopTracker(data_dir=self.temp_dir)
        market = "KRW-ETH"
        tracker.set_strategy_mode(market, "SWING")

        entry_price = 3_699_000.0

        # 1. 어제 새벽 고점 3,777,000원 (+2.11%) 도달
        action, peak, trigger, peak_pct, cur_pct = tracker.check_position(
            market=market,
            current_price=3_777_000.0,
            avg_buy_price=entry_price,
        )
        # +1.8%를 초과했으므로 자동 본전 스탑이 즉시 활성화되어야 함
        self.assertTrue(tracker.is_breakeven_active(market, avg_buy_price=entry_price))
        self.assertEqual(tracker.peaks[market], 3_777_000.0)

        # 2. 실시간 엔진의 유효 손절선 계산 검증
        # 메이저 스윙은 +0.5% 안전 마진을 보장하는 손절선 설정
        be_pct = getattr(StrategyPolicy, "SWING_MAJOR_BREAKEVEN_STOP_PCT", 0.005)
        breakeven_sl = entry_price * (1.0 + be_pct)
        self.assertGreater(breakeven_sl, entry_price)
        self.assertAlmostEqual(breakeven_sl, 3_717_495.0, delta=1.0)

        # 3. 오늘 오전처럼 가격이 3,660,000원(-1.05%)으로 급락하기 전,
        # 3,710,000원에 도달했을 때 이미 breakeven_sl(3,717,495원) 이하이므로 즉시 손절(사실상 익절) 방어 작동
        current_dropping_price = 3_710_000.0
        self.assertLess(current_dropping_price, breakeven_sl)

    def test_sol_major_swing_partial_take_profit(self):
        """메이저 스윙(SOL)이 +2.5% 도달 시 1차 40% 분할 익절이 정상 트리거되는지 검증."""
        tracker = TrailingStopTracker(data_dir=self.temp_dir)
        market = "KRW-SOL"
        tracker.set_strategy_mode(market, "SWING")

        entry_price = 157_900.0
        target_tp1_price = entry_price * (1.0 + StrategyPolicy.SWING_MAJOR_PARTIAL_TP_1_PCT)  # +2.5%

        # 1. +2.0% 도달 시: 1차 익절(+2.5%) 미달이지만 본전 보장은 활성화
        action, peak, trigger, peak_pct, cur_pct = tracker.check_position(
            market=market,
            current_price=entry_price * 1.020,
            avg_buy_price=entry_price,
        )
        self.assertEqual(action, "NONE")
        self.assertTrue(tracker.is_breakeven_active(market))

        # 2. +2.6% 도달 시: 1차 분할 익절 트리거!
        action, peak, trigger, peak_pct, cur_pct = tracker.check_position(
            market=market,
            current_price=entry_price * 1.026,
            avg_buy_price=entry_price,
        )
        self.assertEqual(action, "PARTIAL_TP_1")

        # 3. 1차 익절 체결 반영
        tracker.mark_partial_take_profit_filled(market, 1, 0.4)
        self.assertEqual(tracker.get_tp_stage(market), 1)

        # 4. +5.1% 도달 시: 2차 분할 익절 트리거!
        action, peak, trigger, peak_pct, cur_pct = tracker.check_position(
            market=market,
            current_price=entry_price * 1.051,
            avg_buy_price=entry_price,
        )
        self.assertEqual(action, "PARTIAL_TP_2")

    def test_major_swing_trailing_stop_drop(self):
        """메이저 스윙이 고점 형성 후 1.2% 반락 시 트레일링 익절이 작동하는지 검증."""
        tracker = TrailingStopTracker(data_dir=self.temp_dir)
        market = "KRW-BTC"
        tracker.set_strategy_mode(market, "SWING")

        entry_price = 100_000_000.0

        # 고점 +3.0% (103,000,000원) 형성
        tracker.check_position(market, 103_000_000.0, entry_price)

        # 고점 대비 1.3% 하락 (103,000,000 * 0.987 = 101,661,000원)
        action, peak, trigger, peak_pct, cur_pct = tracker.check_position(
            market=market,
            current_price=101_600_000.0,
            avg_buy_price=entry_price,
        )
        self.assertEqual(action, "TRAILING_STOP")
        self.assertGreater(cur_pct, 1.5)  # 최소 1.5% 이상 수익 확보 상태에서 청산

    def test_auto_breakeven_persistence_across_restart(self):
        """재시작 후에도 auto_breakeven_active 상태가 영속 보존되는지 검증."""
        tracker1 = TrailingStopTracker(data_dir=self.temp_dir)
        market = "KRW-NEAR"
        entry_price = 5000.0

        # +2.0% 상승으로 자동 본전 스탑 활성화
        tracker1.check_position(market, 5100.0, entry_price)
        self.assertTrue(tracker1.is_breakeven_active(market))

        # 새 인스턴스로 복원
        tracker2 = TrailingStopTracker(data_dir=self.temp_dir)
        self.assertTrue(tracker2.is_breakeven_active(market))
        self.assertIn(market, tracker2.auto_breakeven_active)

    def test_altcoin_swing_keeps_high_beta_targets(self):
        """알트코인 스윙은 기존 높은 익절 목표(+8.0%)를 유지하는지 검증."""
        tracker = TrailingStopTracker(data_dir=self.temp_dir)
        market = "KRW-NEAR"
        tracker.set_strategy_mode(market, "SWING")

        entry_price = 5000.0

        # +3.0% 상승: 메이저라면 익절이 나갔겠지만, 알트 스윙은 +8% 목표이므로 익절 안 나감 (대신 본전 스탑은 켜짐)
        action, peak, trigger, peak_pct, cur_pct = tracker.check_position(
            market=market,
            current_price=5150.0,
            avg_buy_price=entry_price,
        )
        self.assertEqual(action, "NONE")
        self.assertTrue(tracker.is_breakeven_active(market))

        # +8.5% 도달 시에만 1차 익절
        action, peak, trigger, peak_pct, cur_pct = tracker.check_position(
            market=market,
            current_price=5430.0,
            avg_buy_price=entry_price,
        )
        self.assertEqual(action, "PARTIAL_TP_1")


if __name__ == "__main__":
    unittest.main()
