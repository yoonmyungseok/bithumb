"""
Risk Management Pro Tests (Macro Flash-Crash Guard, Dynamic De-scaling, Hard-Stop)
"""

import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from order_safety import (
    CooldownManager,
    OrderJournal,
    OrderStatus,
    SafeOrderExecutor,
    calculate_risk_position_size,
)
from realtime_engine import RealtimeRiskEngine
from risk_manager import DailyRiskManager, TrailingStopTracker
from telegram_alert import TelegramAlert
from trade_memory import TradeMemoryManager


class RiskManagementProTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_risk_pro_")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_macro_defensive_mode_tightening(self):
        """비상 방어 모드(BTC 급락) 가동 시 +0.8%부터 즉각 트레일링 타이트닝 작동 검증"""
        tracker = TrailingStopTracker(start_profit_pct=0.02, trailing_drop_pct=0.012, data_dir=self.test_dir)
        market = "KRW-XRP"
        avg_buy_price = 1000.0

        # 1. 정상 모드 (+1.0% 상승): start_profit_pct(2.0%) 미달로 트레일링 미발동
        action, _, _, _, _ = tracker.check_position(market, 1010.0, avg_buy_price)
        self.assertEqual(action, "NONE")

        # 2. 비상 방어 모드 ON 설정
        tracker.set_macro_defensive_mode(True)
        self.assertTrue(tracker.macro_defensive_mode)

        # 3. +1.0% 상승 상태에서 최고점 기록 후 0.4% 하락 (1010원 -> 1005원)
        # 1010원 기준 0.4% 드롭 = 1010 * 0.996 = 1005.96원
        # 1005원은 익절 기준선 이하이므로 즉시 TRAILING_STOP 발동
        tracker.check_position(market, 1010.0, avg_buy_price)
        action, peak_p, trigger_p, _, realized = tracker.check_position(market, 1005.0, avg_buy_price)
        self.assertEqual(action, "TRAILING_STOP")
        self.assertEqual(peak_p, 1010.0)

    def test_dynamic_capital_descaling_sizing(self):
        """연속 손실 발생 시 자본 디스케일링(1.0 -> 0.8 -> 0.5) 검증"""
        drm = DailyRiskManager(data_dir=self.test_dir)
        total_equity = 1000000.0
        entry_price = 1000.0
        stop_loss = 980.0

        # 0연패: 스케일 팩터 1.0
        self.assertEqual(drm.get_risk_scale_factor(), 1.0)
        size_0 = calculate_risk_position_size(
            total_equity=total_equity,
            entry_price=entry_price,
            stop_loss=stop_loss,
            risk_scale_factor=drm.get_risk_scale_factor(),
        )

        # 1연패 발생: 스케일 팩터 0.8
        drm.add_realized_trade(-10000.0, is_win=False)
        self.assertEqual(drm.get_risk_scale_factor(), 0.8)
        size_1 = calculate_risk_position_size(
            total_equity=total_equity,
            entry_price=entry_price,
            stop_loss=stop_loss,
            risk_scale_factor=drm.get_risk_scale_factor(),
        )
        self.assertLess(size_1, size_0)
        self.assertAlmostEqual(size_1 / size_0, 0.8, places=2)

        # 2연패 발생: 스케일 팩터 0.5
        drm.add_realized_trade(-10000.0, is_win=False)
        self.assertEqual(drm.get_risk_scale_factor(), 0.5)
        size_2 = calculate_risk_position_size(
            total_equity=total_equity,
            entry_price=entry_price,
            stop_loss=stop_loss,
            risk_scale_factor=drm.get_risk_scale_factor(),
        )
        self.assertLess(size_2, size_1)
        self.assertAlmostEqual(size_2 / size_0, 0.5, places=2)

    def test_realtime_engine_hard_stop_execution(self):
        """실시간 틱 급락(-4.5% 이하) 시 틱 카운트 지연 없이 즉각 하드스탑 청산 주문 제출 검증"""
        mock_exchange = MagicMock()
        mock_exchange.get_balances.return_value = {
            "BTC": {"balance": 0.01, "locked": 0.0, "avg_buy_price": 100000000.0},
            "KRW": {"balance": 100000.0, "locked": 0.0},
        }
        mock_exchange.get_korean_name.return_value = "비트코인"
        mock_exchange.round_price_to_tick.side_effect = lambda p: p

        journal = OrderJournal(data_dir=self.test_dir)
        executor = SafeOrderExecutor(journal)
        executor.submit = MagicMock(return_value={"uuid": "hard-stop-order-1", "client_order_id": "bot-hs-1"})

        risk_mgr = DailyRiskManager(data_dir=self.test_dir)
        cooldown_mgr = CooldownManager(data_dir=self.test_dir)
        trade_mem = TradeMemoryManager(data_dir=self.test_dir)
        trailing = TrailingStopTracker(data_dir=self.test_dir)
        mock_telegram = MagicMock()

        engine = RealtimeRiskEngine(
            exchange_factory=lambda: mock_exchange,
            order_executor=executor,
            order_journal=journal,
            risk_manager=risk_mgr,
            cooldown_manager=cooldown_mgr,
            trade_memory=trade_mem,
            trailing_tracker=trailing,
            telegram=mock_telegram,
            latest_strategies={"KRW-BTC": {"STOP_LOSS": 97000000.0}},
        )

        # 평단가 100,000,000원에서 95,000,000원(-5.0% 급락 틱) 수신
        # 하드 스탑(-4.5% 이하)이 즉시 트리거되어 주문 제출 확인
        engine.on_price_tick("KRW-BTC", 95000000.0)

        executor.submit.assert_called_once()
        call_kwargs = executor.submit.call_args[1]
        self.assertEqual(call_kwargs["market"], "KRW-BTC")
        self.assertEqual(call_kwargs["side"], "ask")
        self.assertEqual(call_kwargs["ord_type"], "market")


if __name__ == "__main__":
    unittest.main()
