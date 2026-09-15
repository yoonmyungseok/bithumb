"""주문 REST·Private WS 교차 대사 및 ACK/체결 분리 회귀 테스트."""

import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from order_safety import OrderFillProcessor, OrderJournal, OrderStatus, SafeOrderExecutor
from risk_manager import DailyRiskManager, TrailingStopTracker
from trade_memory import TradeMemoryManager


class OrderReconciliationStabilityTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_order_reconcile_stability_")
        self.journal = OrderJournal(data_dir=self.test_dir, exchange_scope="bithumb")
        self.risk = DailyRiskManager(data_dir=self.test_dir)
        self.memory = TradeMemoryManager(data_dir=self.test_dir, exchange_scope="bithumb")
        self.processor = OrderFillProcessor(
            self.journal,
            self.risk,
            self.memory,
            TrailingStopTracker(data_dir=self.test_dir),
        )
        self.executor = SafeOrderExecutor(self.journal)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_ack_submit_does_not_update_position_or_pnl(self):
        """주문 접수(ACK)만으로는 체결량·손익·쿨다운이 갱신되지 않는다."""
        exchange = MagicMock()
        exchange.create_order.return_value = {"order_id": "bithumb-ack-1"}
        exchange.get_current_price.return_value = 500.0
        self.executor.submit(exchange, "KRW-WLD", "bid", volume=10.0, price=500.0, exchange_name="bithumb")

        client_id = self.journal.orders[-1]["client_order_id"]
        result = self.processor.process_order_fill(
            client_id,
            OrderStatus.ACKNOWLEDGED,
            executed_volume=0.0,
            avg_price=500.0,
            remaining_volume=10.0,
        )
        order = self.journal.get_order_by_client_id(client_id)
        self.assertFalse(result["processed"])
        self.assertEqual(order["status"], OrderStatus.ACKNOWLEDGED)
        self.assertEqual(order["processed_executed_volume"], 0.0)
        self.assertEqual(self.risk.total_trades_today, 0)

    def test_reconciliation_pending_blocks_entry_even_when_state_ready(self):
        """RECONCILIATION_PENDING 주문이 있으면 reconciliation_state=READY여도 신규 BUY를 막는다."""
        self.journal.reconciliation_state = "READY"
        client_id = self.journal.record_intent("KRW-WLD", "bid", 1.0, 500.0, "limit", exchange="bithumb")
        self.journal.mark(client_id, OrderStatus.RECONCILIATION_PENDING, exchange_uuid="wld-pending-1")
        self.assertFalse(self.journal.is_entry_ready())

    def test_private_ws_defers_fill_and_blocks_buy_until_rest(self):
        """Private WS(done)는 REST 대기로 전환하고 실현 손익·진입 재개를 막는다."""
        self.journal.reconciliation_state = "READY"
        client_id = self.journal.record_intent(
            "KRW-WLD", "ask", 5.0, None, "market", avg_buy_price=480.0, exchange="bithumb",
        )
        self.journal.mark(client_id, OrderStatus.ACKNOWLEDGED, exchange_uuid="wld-sell-1")
        applied = self.journal.apply_private_order_event(
            {
                "client_order_id": client_id,
                "state": "done",
                "executed_volume": 5.0,
                "remaining_volume": 0.0,
                "price": 0.0,
                "order_id": "wld-sell-1",
            },
            fill_processor=self.processor,
            require_rest_confirmation=True,
        )
        self.assertTrue(applied)
        self.assertEqual(self.journal.get_order_by_client_id(client_id)["status"], OrderStatus.RECONCILIATION_PENDING)
        self.assertFalse(self.journal.is_entry_ready())
        self.assertEqual(self.risk.total_trades_today, 0)

    def test_partial_fill_via_rest_increments_processed_volume_only(self):
        """REST 대사로 확인된 부분체결만 processed_executed_volume에 반영한다."""
        client_id = self.journal.record_intent("KRW-WLD", "bid", 10.0, 500.0, "limit", exchange="bithumb")
        self.journal.mark(client_id, OrderStatus.OPEN, exchange_uuid="wld-partial-1")
        get_order = MagicMock(return_value={
            "order_id": "wld-partial-1",
            "state": "trade",
            "executed_volume": "4.0",
            "remaining_volume": "6.0",
            "paid_fee": "2.0",
            "trades": [{"price": "500", "volume": "4.0"}],
        })
        self.journal.reconcile_exchange_statuses(get_order, fill_processor=self.processor)
        order = self.journal.get_order_by_client_id(client_id)
        self.assertEqual(order["status"], OrderStatus.PARTIALLY_FILLED)
        self.assertEqual(order["processed_executed_volume"], 4.0)
        self.assertEqual(self.risk.total_trades_today, 0)

    def test_cancel_unfilled_via_rest(self):
        """미체결 취소는 체결 증가분 없이 CANCELED로만 전이한다."""
        client_id = self.journal.record_intent("KRW-WLD", "bid", 3.0, 500.0, "limit", exchange="bithumb")
        self.journal.mark(client_id, OrderStatus.RECONCILIATION_PENDING, exchange_uuid="wld-cancel-1")
        get_order = MagicMock(return_value={
            "order_id": "wld-cancel-1",
            "state": "cancel",
            "executed_volume": "0",
            "remaining_volume": "3.0",
            "paid_fee": "0",
        })
        self.journal.reconcile_exchange_statuses(get_order, fill_processor=self.processor)
        order = self.journal.get_order_by_client_id(client_id)
        self.assertEqual(order["status"], OrderStatus.CANCELED)
        self.assertEqual(order["processed_executed_volume"], 0.0)

    def test_ws_done_rest_wait_stays_pending_until_rest_confirms(self):
        """WS done 신호 후 REST가 wait·0체결이면 FILLED로 확정하지 않는다."""
        client_id = self.journal.record_intent("KRW-WLD", "bid", 2.0, 500.0, "limit", exchange="bithumb")
        self.journal.mark(client_id, OrderStatus.ACKNOWLEDGED, exchange_uuid="wld-mismatch-1")
        self.journal.apply_private_order_event(
            {
                "client_order_id": client_id,
                "state": "done",
                "executed_volume": 2.0,
                "remaining_volume": 0.0,
                "price": 500.0,
                "order_id": "wld-mismatch-1",
            },
            fill_processor=self.processor,
            require_rest_confirmation=True,
        )
        get_order = MagicMock(return_value={
            "order_id": "wld-mismatch-1",
            "state": "wait",
            "executed_volume": "0",
            "remaining_volume": "2.0",
            "paid_fee": "0",
        })
        self.journal.reconcile_exchange_statuses(get_order, fill_processor=self.processor)
        order = self.journal.get_order_by_client_id(client_id)
        self.assertEqual(order["status"], OrderStatus.OPEN)
        self.assertEqual(order["processed_executed_volume"], 0.0)

    def test_rest_validation_failure_blocks_entry(self):
        """REST 수량 모순 시 RECONCILIATION_PENDING·last_failed_count로 신규 BUY를 차단한다."""
        self.journal.reconciliation_state = "READY"
        client_id = self.journal.record_intent("KRW-WLD", "bid", 2.0, 500.0, "limit", exchange="bithumb")
        self.journal.mark(client_id, OrderStatus.OPEN, exchange_uuid="wld-invalid-1")
        get_order = MagicMock(return_value={
            "order_id": "wld-invalid-1",
            "state": "done",
            "executed_volume": "2.0",
            "remaining_volume": "2.0",
            "paid_fee": "0",
        })
        self.journal.reconcile_exchange_statuses(get_order, fill_processor=self.processor)
        self.assertFalse(self.journal.is_entry_ready())
        self.assertGreater(self.journal.reconciliation_metrics.get("last_failed_count", 0), 0)

    def test_fill_processor_rejects_filled_without_fill_delta(self):
        """체결 증가분이 없으면 FILLED 상태 전이를 거부한다."""
        client_id = self.journal.record_intent("KRW-WLD", "bid", 1.0, 500.0, "limit", exchange="bithumb")
        self.journal.mark(client_id, OrderStatus.ACKNOWLEDGED, exchange_uuid="wld-no-fill")
        self.processor.process_order_fill(
            client_id, OrderStatus.FILLED, executed_volume=0.0, avg_price=500.0, remaining_volume=0.0,
        )
        order = self.journal.get_order_by_client_id(client_id)
        self.assertEqual(order["status"], OrderStatus.ACKNOWLEDGED)
        self.assertEqual(order["processed_executed_volume"], 0.0)

    def test_mark_by_uuid_does_not_downgrade_filled_to_open(self):
        """UUID 경로 갱신도 확정 FILLED를 OPEN/ACK로 역행하지 않는다."""
        client_id = self.journal.record_intent("KRW-WLD", "bid", 1.0, 500.0, "limit", exchange="bithumb")
        self.journal.mark(client_id, OrderStatus.FILLED, exchange_uuid="wld-terminal", processed_executed_volume=1.0)
        self.journal.mark_by_uuid("wld-terminal", OrderStatus.OPEN)
        order = self.journal.get_order_by_client_id(client_id)
        self.assertEqual(order["status"], OrderStatus.FILLED)


if __name__ == "__main__":
    unittest.main()
