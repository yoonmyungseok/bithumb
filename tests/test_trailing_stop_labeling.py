"""
Trailing Stop Labeling and Margin Enhancement Tests
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from order_safety import OrderFillProcessor, OrderJournal, OrderStatus
from risk_manager import DailyRiskManager, TrailingStopTracker
from trade_memory import TradeMemoryManager


class TrailingStopLabelingTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_trail_label_")
        self.tracker = TrailingStopTracker(start_profit_pct=0.02, trailing_drop_pct=0.012, data_dir=self.test_dir)
        self.journal = OrderJournal(data_dir=self.test_dir)
        self.trade_memory = TradeMemoryManager(data_dir=self.test_dir)
        self.risk_manager = DailyRiskManager(data_dir=self.test_dir)
        self.processor = OrderFillProcessor(
            order_journal=self.journal,
            risk_manager=self.risk_manager,
            trade_memory=self.trade_memory,
            trailing_tracker=self.tracker,
        )

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_trailing_stop_min_guaranteed_profit_margin(self):
        """트레일링 스탑의 최소 안전 보장선이 +0.5%(1.005)로 상향되었는지 검증"""
        # 1. 매수가 1,000원 -> 최고점 1,020원 등록 (+2.0% 도달)
        self.tracker.check_position("KRW-TEST", 1020.0, 1000.0)
        # 2. 가격이 1,004원으로 하락 시 1007.76원 이하이므로 트레일링 스탑 발동
        action, peak_p, trigger_p, peak_pct, profit_pct = self.tracker.check_position("KRW-TEST", 1004.0, 1000.0)
        self.assertEqual(action, "TRAILING_STOP")
        self.assertGreaterEqual(trigger_p, 1005.0)

        # 3. peak=1008원 (+0.8% 비상방어) -> 드롭폭 계산가는 1003.96원이지만 최소 1005.0원으로 클램핑
        self.tracker.set_macro_defensive_mode(True)
        self.tracker.check_position("KRW-TEST2", 1008.0, 1000.0)
        action, peak_p, trigger_p, peak_pct, profit_pct = self.tracker.check_position("KRW-TEST2", 1004.0, 1000.0)
        self.assertEqual(action, "TRAILING_STOP")
        self.assertAlmostEqual(trigger_p, 1005.0, places=2)

    def test_order_fill_processor_trailing_label_positive(self):
        """수익 실현 시 '트레일링 익절'로 기록되는지 검증"""
        client_id = self.journal.record_intent(
            market="KRW-JUP",
            side="ask",
            volume=10.0,
            price=1030.0,
            ord_type="market",
            exit_reason="TRAILING_STOP",
            avg_buy_price=1000.0,
        )
        self.processor.process_order_fill(
            order_identifier=client_id,
            status=OrderStatus.FILLED,
            executed_volume=10.0,
            avg_price=1030.0,
            fee=8.0,
            exit_reason="TRAILING_STOP",
            avg_buy_price=1000.0,
        )
        recent = self.trade_memory.get_recent_trades(limit=1)
        self.assertEqual(recent[0]["reason"], "트레일링 익절")

    def test_order_fill_processor_trailing_label_breakeven(self):
        """-0.2% 미세 손실 시 '트레일링 본전방어'로 기록되는지 검증"""
        client_id = self.journal.record_intent(
            market="KRW-JUP",
            side="ask",
            volume=10.0,
            price=998.0,
            ord_type="market",
            exit_reason="TRAILING_STOP",
            avg_buy_price=1000.0,
        )
        self.processor.process_order_fill(
            order_identifier=client_id,
            status=OrderStatus.FILLED,
            executed_volume=10.0,
            avg_price=998.0,
            fee=8.0,
            exit_reason="TRAILING_STOP",
            avg_buy_price=1000.0,
        )
        recent = self.trade_memory.get_recent_trades(limit=1)
        self.assertEqual(recent[0]["reason"], "트레일링 본전방어")

    def test_order_fill_processor_trailing_label_defense_exit(self):
        """-0.65% 손실 시 '트레일링 방어매도'로 기록되는지 검증"""
        client_id = self.journal.record_intent(
            market="KRW-JUP",
            side="ask",
            volume=10.0,
            price=993.5,
            ord_type="market",
            exit_reason="TRAILING_STOP",
            avg_buy_price=1000.0,
        )
        self.processor.process_order_fill(
            order_identifier=client_id,
            status=OrderStatus.FILLED,
            executed_volume=10.0,
            avg_price=993.5,
            fee=8.0,
            exit_reason="TRAILING_STOP",
            avg_buy_price=1000.0,
        )
        recent = self.trade_memory.get_recent_trades(limit=1)
        self.assertEqual(recent[0]["reason"], "트레일링 방어매도")


if __name__ == "__main__":
    unittest.main()
