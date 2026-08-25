"""
Private WebSocket Fill Integration, Idempotency, and ACK vs Execution Separation Tests
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from order_safety import OrderFillProcessor, OrderJournal, OrderStatus, SafeOrderExecutor
from risk_manager import DailyRiskManager, TrailingStopTracker
from trade_memory import TradeMemoryManager


class PrivateWSFillIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_pws_fill_")
        self.journal = OrderJournal(data_dir=self.test_dir)
        self.risk_manager = DailyRiskManager(data_dir=self.test_dir)
        self.trailing_tracker = TrailingStopTracker(data_dir=self.test_dir)
        self.trade_memory = TradeMemoryManager(data_dir=self.test_dir)
        self.executor = SafeOrderExecutor(self.journal)
        self.processor = OrderFillProcessor(
            order_journal=self.journal,
            risk_manager=self.risk_manager,
            trade_memory=self.trade_memory,
            trailing_tracker=self.trailing_tracker,
        )

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_ack_does_not_mutate_pnl_or_entry_time(self):
        """주문 접수 ACK 단계에서는 손익/진입시간/쿨다운이 일절 갱신되지 않음을 검증 (ACK는 체결이 아님)"""
        mock_exchange = MagicMock()
        mock_exchange.create_order.return_value = {
            "order_id": "bithumb-ord-100",
            "state": "wait",
        }

        # 주문 접수 제출
        order_res = self.executor.submit(
            mock_exchange,
            market="KRW-BTC",
            side="bid",
            volume=0.01,
            price=100000000.0,
            ord_type="limit",
            position_id="KRW-BTC",
        )

        # 1. 저널에는 접수 확인(ACKNOWLEDGED) 상태로 기록됨
        client_id = order_res.get("client_order_id")
        self.assertIsNotNone(client_id)
        order_rec = next((o for o in self.journal.orders if o.get("client_order_id") == client_id), None)
        self.assertIsNotNone(order_rec)
        self.assertEqual(order_rec["status"], OrderStatus.ACKNOWLEDGED)

        # 2. 실체결 전이므로 진입시간 및 손익은 0이어야 함
        self.assertEqual(self.trailing_tracker.get_entry_time("KRW-BTC"), 0.0)
        self.assertEqual(self.risk_manager.total_trades_today, 0)
        self.assertEqual(self.risk_manager.realized_pnl_krw, 0.0)
        self.assertEqual(len(self.trade_memory.get_recent_trades()), 0)

    def test_private_ws_fill_event_routes_to_fill_processor(self):
        """Private WebSocket 체결 이벤트 수신 시 fill_processor를 통해 진입시간/손익/메모리가 갱신되는지 검증"""
        # 1. 매수 주문 의도 기록
        client_id = self.journal.record_intent(
            market="KRW-ETH",
            side="bid",
            volume=1.0,
            price=4000000.0,
            ord_type="limit",
        )

        # 2. Private WebSocket 체결 이벤트 도착
        buy_event = {
            "client_order_id": client_id,
            "state": "done",
            "executed_volume": 1.0,
            "price": 4000000.0,
            "paid_fee": 1600.0,
            "remaining_volume": 0.0,
            "order_id": "upbit-eth-999",
        }
        res = self.journal.apply_private_order_event(buy_event, fill_processor=self.processor)
        self.assertTrue(res)

        # 매수 체결 확인: 진입시간 등록됨
        self.assertGreater(self.trailing_tracker.get_entry_time("KRW-ETH"), 0.0)

        # 3. 매도 주문 의도 기록
        sell_client_id = self.journal.record_intent(
            market="KRW-ETH",
            side="ask",
            volume=1.0,
            price=4200000.0,
            ord_type="limit",
            exit_reason="TRAILING_STOP",
            avg_buy_price=4000000.0,
        )

        # 4. Private WebSocket 매도 체결 이벤트 도착
        sell_event = {
            "client_order_id": sell_client_id,
            "state": "done",
            "executed_volume": 1.0,
            "price": 4200000.0,
            "paid_fee": 1680.0,
            "remaining_volume": 0.0,
            "order_id": "upbit-eth-sell-999",
        }
        self.journal.apply_private_order_event(sell_event, fill_processor=self.processor)

        # 매도 체결 확인: 일일 리스크 매니저 실현손익 및 매매 메모리 갱신
        self.assertEqual(self.risk_manager.total_trades_today, 1)
        self.assertGreater(self.risk_manager.realized_pnl_krw, 0.0)  # +198,320원 (4,200,000 - 4,000,000 - 1,680)
        recent_trades = self.trade_memory.get_recent_trades(limit=1)
        self.assertEqual(len(recent_trades), 1)
        self.assertEqual(recent_trades[0]["market"], "KRW-ETH")
        self.assertEqual(recent_trades[0]["reason"], "트레일링 익절")

    def test_partial_fill_and_idempotency(self):
        """부분 체결 누적 및 중복 이벤트 수신 시 멱등성 검증"""
        client_id = self.journal.record_intent(
            market="KRW-SOL",
            side="ask",
            volume=10.0,
            price=200000.0,
            ord_type="limit",
            exit_reason="TRAILING_STOP",
            avg_buy_price=180000.0,
        )

        # 1차 부분 체결 4개 (40%)
        event_partial_1 = {
            "client_order_id": client_id,
            "state": "trade",
            "executed_volume": 4.0,
            "price": 200000.0,
            "paid_fee": 320.0,
            "remaining_volume": 6.0,
        }
        self.journal.apply_private_order_event(event_partial_1, fill_processor=self.processor)
        self.assertEqual(self.risk_manager.total_trades_today, 1)
        prev_pnl = self.risk_manager.realized_pnl_krw

        # 중복 이벤트 수신 (멱등성: 손익 추가 발생 없음)
        self.journal.apply_private_order_event(event_partial_1, fill_processor=self.processor)
        self.assertEqual(self.risk_manager.total_trades_today, 1)
        self.assertEqual(self.risk_manager.realized_pnl_krw, prev_pnl)

        # 2차 완전 체결 10개 (추가 6개)
        event_done = {
            "client_order_id": client_id,
            "state": "done",
            "executed_volume": 10.0,
            "price": 200000.0,
            "paid_fee": 800.0,
            "remaining_volume": 0.0,
        }
        self.journal.apply_private_order_event(event_done, fill_processor=self.processor)
        self.assertEqual(self.risk_manager.total_trades_today, 2)
        self.assertGreater(self.risk_manager.realized_pnl_krw, prev_pnl)

    def test_rest_reconciliation_recovers_unhandled_orders(self):
        """REST reconcile_exchange_statuses 호출 시 미체결/체결 완료 주문이 안전하게 복구되는지 검증"""
        client_id = self.journal.record_intent(
            market="KRW-DOGE",
            side="bid",
            volume=100.0,
            price=200.0,
            ord_type="limit",
        )
        self.journal.mark(client_id, OrderStatus.OPEN, exchange_uuid="doge-remote-123")

        mock_get_order = MagicMock(return_value={
            "uuid": "doge-remote-123",
            "state": "done",
            "executed_volume": "100.0",
            "remaining_volume": "0.0",
            "price": "200.0",
            "paid_fee": "8.0",
        })

        updated = self.journal.reconcile_exchange_statuses(
            get_order=mock_get_order,
            fill_processor=self.processor,
        )
        self.assertEqual(updated, 1)
        self.assertGreater(self.trailing_tracker.get_entry_time("KRW-DOGE"), 0.0)


if __name__ == "__main__":
    unittest.main()
