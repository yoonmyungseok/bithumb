"""Private WebSocket 주문 이벤트 큐 포화 fail-closed 연동 테스트."""

import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from order_safety import OrderJournal, OrderStatus
from private_websocket_manager import BithumbPrivateWebSocketClient
from upbit_private_websocket import UpbitPrivateWebSocketClient


class PrivateWsQueueOverflowTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_private_ws_overflow_")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_suspend_entry_for_reconciliation_sets_pending(self):
        journal = OrderJournal(data_dir=self.test_dir, exchange_scope="bithumb")
        journal.reconciliation_state = "READY"
        journal.suspend_entry_for_reconciliation("private_ws_queue_full")
        self.assertEqual(journal.reconciliation_state, "PENDING")
        self.assertFalse(journal.is_entry_ready())
        self.assertEqual(journal.reconciliation_metrics["last_suspend_reason"], "private_ws_queue_full")
        self.assertGreater(journal.reconciliation_metrics["last_suspend_at"], 0.0)

    def test_suspend_entry_debounces_duplicate_reason(self):
        journal = OrderJournal(data_dir=self.test_dir, exchange_scope="upbit")
        journal.suspend_entry_for_reconciliation("private_ws_queue_full")
        first_at = journal.reconciliation_metrics["last_suspend_at"]
        journal.suspend_entry_for_reconciliation("private_ws_queue_full")
        self.assertEqual(journal.reconciliation_metrics["last_suspend_at"], first_at)

    def test_bithumb_queue_full_invokes_overflow_callback(self):
        overflow_calls = []
        client = BithumbPrivateWebSocketClient(
            "access",
            "secret",
            on_order=lambda _event: None,
            on_queue_overflow=lambda: overflow_calls.append(True),
        )
        for idx in range(client._order_event_queue.maxsize):
            client._order_event_queue.put_nowait({"type": "myorder", "id": idx})
        client._on_message(None, '{"type":"myOrder","identifier":"overflow-test"}')
        self.assertEqual(len(overflow_calls), 1)

    def test_upbit_queue_full_invokes_overflow_callback(self):
        overflow_calls = []
        client = UpbitPrivateWebSocketClient(
            "access",
            "secret",
            on_order=lambda _event: None,
            on_queue_overflow=lambda: overflow_calls.append(True),
        )
        for idx in range(client._order_event_queue.maxsize):
            client._order_event_queue.put_nowait({"type": "myorder", "id": idx})
        client._on_message(None, '{"type":"myOrder","identifier":"overflow-test"}')
        self.assertEqual(len(overflow_calls), 1)

    def test_overflow_callback_suspends_journal_entry(self):
        journal = OrderJournal(data_dir=self.test_dir, exchange_scope="bithumb")
        journal.reconciliation_state = "READY"

        def on_overflow() -> None:
            journal.suspend_entry_for_reconciliation("private_ws_queue_full")

        client = BithumbPrivateWebSocketClient(
            "access",
            "secret",
            on_order=lambda _event: None,
            on_queue_overflow=on_overflow,
        )
        for idx in range(client._order_event_queue.maxsize):
            client._order_event_queue.put_nowait({"type": "myorder", "id": idx})
        client._on_message(None, '{"type":"myOrder","identifier":"overflow-test"}')
        self.assertFalse(journal.is_entry_ready())

    def test_complete_reconciliation_restores_ready_after_overflow(self):
        journal = OrderJournal(data_dir=self.test_dir, exchange_scope="upbit")
        journal.suspend_entry_for_reconciliation("private_ws_queue_full")
        self.assertFalse(journal.is_entry_ready())
        self.assertTrue(journal.complete_reconciliation_if_safe())
        self.assertTrue(journal.is_entry_ready())

    def test_complete_reconciliation_stays_pending_with_unresolved_order(self):
        journal = OrderJournal(data_dir=self.test_dir, exchange_scope="upbit")
        client_id = journal.record_intent("KRW-BTC", "bid", 1.0, 100.0, "limit", exchange="upbit")
        journal.mark(client_id, OrderStatus.OPEN, exchange_uuid="uuid-1")
        journal.suspend_entry_for_reconciliation("private_ws_queue_full")
        self.assertFalse(journal.complete_reconciliation_if_safe())
        self.assertFalse(journal.is_entry_ready())


if __name__ == "__main__":
    unittest.main()
