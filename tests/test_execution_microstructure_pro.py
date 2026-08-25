"""
Execution and Microstructure Pro Tests (Slippage Tracking, Smart Pegged Re-quoter)
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
    OrderFillProcessor,
    OrderJournal,
    OrderStatus,
    SafeOrderExecutor,
)
from realtime_engine import RealtimeRiskEngine
from risk_manager import DailyRiskManager, TrailingStopTracker
from trade_memory import TradeMemoryManager


class ExecutionMicrostructureProTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_exec_pro_")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_slippage_bps_calculation_on_buy(self):
        """매수 체결 시 슬리피지(bps) 정밀 계산 및 저널 저장 검증"""
        journal = OrderJournal(data_dir=self.test_dir)
        client_id = journal.record_intent(
            market="KRW-BTC",
            side="bid",
            volume=0.01,
            price=100000000.0,
            ord_type="limit",
            expected_price=100000000.0,
        )

        processor = OrderFillProcessor(order_journal=journal)
        # 실제 체결가가 100,200,000원으로 200,000원(0.2% = 20bps) 슬리피지 발생
        res = processor.process_order_fill(
            order_identifier=client_id,
            status=OrderStatus.FILLED,
            executed_volume=0.01,
            avg_price=100200000.0,
            fee=400.0,
            remaining_volume=0.0,
            expected_price=100000000.0,
        )

        self.assertTrue(res["processed"])
        self.assertAlmostEqual(res["slippage_bps"], 20.0, places=1)

        # 저널 내 저장된 slippage_bps 확인
        saved_order = journal.get_order_by_client_id(client_id)
        self.assertIsNotNone(saved_order)
        self.assertAlmostEqual(saved_order.get("slippage_bps", 0.0), 20.0, places=1)

    def test_slippage_bps_calculation_on_sell(self):
        """매도 체결 시 슬리피지(bps) 정밀 계산 및 저널 저장 검증"""
        journal = OrderJournal(data_dir=self.test_dir)
        client_id = journal.record_intent(
            market="KRW-ETH",
            side="ask",
            volume=1.0,
            price=4000000.0,
            ord_type="market",
            expected_price=4000000.0,
            avg_buy_price=3900000.0,
        )

        processor = OrderFillProcessor(order_journal=journal)
        # 시장가 매도 시 3,988,000원에 체결 (12,000원 차이 = 0.3% = 30bps 슬리피지)
        res = processor.process_order_fill(
            order_identifier=client_id,
            status=OrderStatus.FILLED,
            executed_volume=1.0,
            avg_price=3988000.0,
            fee=1600.0,
            remaining_volume=0.0,
            expected_price=4000000.0,
            avg_buy_price=3900000.0,
        )

        self.assertTrue(res["processed"])
        self.assertAlmostEqual(res["slippage_bps"], 30.0, places=1)

    def test_smart_requoter_pegging(self):
        """60초 이상 미체결 매수 주문의 최우선 호가 페깅 재정정 검증"""
        mock_exchange = MagicMock()
        mock_exchange.get_open_orders.return_value = [
            {
                "uuid": "open-order-1",
                "market": "KRW-SOL",
                "side": "bid",
                "price": 200000.0,
                "volume": 0.5,
                # 100초 전 생성된 주문
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - 100)),
            }
        ]
        # 현재가가 200,500원(+0.25%)으로 소폭 상승
        mock_exchange.get_current_price.return_value = 200500.0
        mock_exchange.round_price_to_tick.side_effect = lambda p: p

        journal = OrderJournal(data_dir=self.test_dir)
        # 봇 관리 주문으로 등록
        client_id = journal.record_intent(
            market="KRW-SOL",
            side="bid",
            volume=0.5,
            price=200000.0,
            ord_type="limit",
        )
        journal.mark(client_id, OrderStatus.OPEN, exchange_uuid="open-order-1")

        executor = SafeOrderExecutor(journal)
        executor.submit = MagicMock(return_value={"uuid": "requoted-order-2"})

        engine = RealtimeRiskEngine(
            exchange_factory=lambda: mock_exchange,
            order_executor=executor,
            order_journal=journal,
            risk_manager=DailyRiskManager(data_dir=self.test_dir),
            cooldown_manager=CooldownManager(data_dir=self.test_dir),
            trade_memory=TradeMemoryManager(data_dir=self.test_dir),
            trailing_tracker=TrailingStopTracker(data_dir=self.test_dir),
            telegram=MagicMock(),
        )

        count = engine.requote_pending_orders()
        self.assertEqual(count, 1)
        mock_exchange.cancel_order.assert_called_once_with("open-order-1")
        executor.submit.assert_called_once()
        submit_kwargs = executor.submit.call_args[1]
        self.assertEqual(submit_kwargs["market"], "KRW-SOL")
        self.assertEqual(submit_kwargs["price"], 200500.0)


if __name__ == "__main__":
    unittest.main()
