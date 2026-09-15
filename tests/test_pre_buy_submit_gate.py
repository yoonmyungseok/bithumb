import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from base_websocket import WebSocketHealthState
from order_safety import (
    AckReconcileScheduler,
    CooldownManager,
    OrderJournal,
    OrderStatus,
    SafeOrderExecutor,
    evaluate_pre_buy_submit_gate,
)
from websocket_manager import BithumbWebSocketClient


class _PriceExchange:
    def get_current_price(self, market, force_refresh=False):
        return 100.0

    def create_order(self, *args, **kwargs):
        return {"uuid": "ex-1"}


class TestPreBuySubmitGate(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.journal_bithumb = OrderJournal(
            data_dir=os.path.join(self.temp_dir.name, "bithumb"),
            exchange_scope="bithumb",
        )
        self.journal_upbit = OrderJournal(
            data_dir=os.path.join(self.temp_dir.name, "upbit"),
            exchange_scope="upbit",
        )
        self.journal_bithumb.reconciliation_state = "READY"
        self.journal_upbit.reconciliation_state = "READY"
        self.journal_bithumb.reconciliation_metrics["last_completed_at"] = time.time()
        self.journal_upbit.reconciliation_metrics["last_completed_at"] = time.time()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _ready_ws(self, market: str = "KRW-ETH"):
        ws = BithumbWebSocketClient(initial_markets=[market])
        ws.is_connected = True
        ws.last_tick_time_by_market[market.upper()] = time.time()
        return ws

    def test_blocks_stale_ws_at_submit(self):
        ws = self._ready_ws()
        ws.last_tick_time_by_market["KRW-ETH"] = time.time() - 120.0
        allowed, code, reason, details = evaluate_pre_buy_submit_gate(
            market="KRW-ETH",
            exchange_name="bithumb",
            order_journal=self.journal_bithumb,
            current_price=100.0,
            ws_client=ws,
        )
        self.assertFalse(allowed)
        self.assertEqual(code, f"PRE_BUY_WS_{WebSocketHealthState.STALE}")
        self.assertIn("STALE", details["ws_status"])

    def test_blocks_processing_delay_ws(self):
        ws = self._ready_ws()
        ws._callback_queue.put(("price", ("KRW-ETH", 100.0), time.time()))
        for _ in range(150):
            ws._callback_queue.put(("noop", (), time.time()))
        allowed, code, _, _ = evaluate_pre_buy_submit_gate(
            market="KRW-ETH",
            exchange_name="bithumb",
            order_journal=self.journal_bithumb,
            current_price=100.0,
            ws_client=ws,
        )
        self.assertFalse(allowed)
        self.assertEqual(code, f"PRE_BUY_WS_{WebSocketHealthState.PROCESSING_DELAY}")

    def test_blocks_disconnected_ws(self):
        ws = self._ready_ws()
        ws.is_connected = False
        allowed, code, _, _ = evaluate_pre_buy_submit_gate(
            market="KRW-ETH",
            exchange_name="bithumb",
            order_journal=self.journal_bithumb,
            current_price=100.0,
            ws_client=ws,
        )
        self.assertFalse(allowed)
        self.assertEqual(code, f"PRE_BUY_WS_{WebSocketHealthState.DISCONNECTED}")

    def test_blocks_unsubscribed_market(self):
        ws = BithumbWebSocketClient(initial_markets=["KRW-BTC"])
        ws.is_connected = True
        ws.last_tick_time_by_market["KRW-BTC"] = time.time()
        allowed, code, _, _ = evaluate_pre_buy_submit_gate(
            market="KRW-SOL",
            exchange_name="bithumb",
            order_journal=self.journal_bithumb,
            current_price=100.0,
            ws_client=ws,
        )
        self.assertFalse(allowed)
        self.assertEqual(code, f"PRE_BUY_WS_{WebSocketHealthState.SUBSCRIPTION_FAILED}")

    def test_blocks_reconciliation_pending_market(self):
        cid = self.journal_bithumb.record_intent("KRW-ETH", "bid", 1.0, 100.0, "limit", exchange="bithumb")
        self.journal_bithumb.mark(cid, OrderStatus.RECONCILIATION_PENDING)
        allowed, code, _, _ = evaluate_pre_buy_submit_gate(
            market="KRW-ETH",
            exchange_name="bithumb",
            order_journal=self.journal_bithumb,
            current_price=100.0,
            ws_client=self._ready_ws("KRW-ETH"),
        )
        self.assertFalse(allowed)
        self.assertEqual(code, "PRE_BUY_RECONCILIATION_PENDING")

    def test_blocks_global_reconciliation_not_ready(self):
        self.journal_bithumb.reconciliation_state = "PENDING"
        allowed, code, _, _ = evaluate_pre_buy_submit_gate(
            market="KRW-ETH",
            exchange_name="bithumb",
            order_journal=self.journal_bithumb,
            current_price=100.0,
            ws_client=self._ready_ws("KRW-ETH"),
        )
        self.assertFalse(allowed)
        self.assertEqual(code, "PRE_BUY_GLOBAL_RECONCILIATION")

    def test_allows_healthy_state(self):
        allowed, code, _, _ = evaluate_pre_buy_submit_gate(
            market="KRW-ETH",
            exchange_name="bithumb",
            order_journal=self.journal_bithumb,
            current_price=100.0,
            ws_client=self._ready_ws("KRW-ETH"),
        )
        self.assertTrue(allowed)
        self.assertEqual(code, "OK")

    def test_exchange_journals_do_not_cross_block(self):
        cid = self.journal_upbit.record_intent("KRW-ETH", "bid", 1.0, 100.0, "limit", exchange="upbit")
        self.journal_upbit.mark(cid, OrderStatus.RECONCILIATION_PENDING)
        bithumb_ok, _, _, _ = evaluate_pre_buy_submit_gate(
            market="KRW-ETH",
            exchange_name="bithumb",
            order_journal=self.journal_bithumb,
            current_price=100.0,
            ws_client=self._ready_ws("KRW-ETH"),
        )
        upbit_blocked, code, _, _ = evaluate_pre_buy_submit_gate(
            market="KRW-ETH",
            exchange_name="upbit",
            order_journal=self.journal_upbit,
            current_price=100.0,
            ws_client=self._ready_ws("KRW-ETH"),
        )
        self.assertTrue(bithumb_ok)
        self.assertFalse(upbit_blocked)
        self.assertEqual(code, "PRE_BUY_RECONCILIATION_PENDING")

    def test_cooldown_recheck_at_submit(self):
        cd = CooldownManager(
            default_sl_cooldown=600.0,
            default_tp_cooldown=0.0,
            default_time_stop_cooldown=0.0,
            data_dir=self.temp_dir.name,
        )
        cd.record_exit("KRW-ETH", "STOP_LOSS", exit_price=100.0)
        allowed, code, _, _ = evaluate_pre_buy_submit_gate(
            market="KRW-ETH",
            exchange_name="bithumb",
            order_journal=self.journal_bithumb,
            current_price=95.0,
            ws_client=self._ready_ws("KRW-ETH"),
            cooldown_manager=cd,
        )
        self.assertFalse(allowed)
        self.assertEqual(code, "PRE_BUY_MARKET_COOLDOWN")

    def test_sell_submit_not_blocked_by_stale_ws_gate(self):
        ws = self._ready_ws()
        ws.last_tick_time_by_market["KRW-ETH"] = time.time() - 120.0
        executor = SafeOrderExecutor(self.journal_bithumb)
        mock_api = MagicMock()
        mock_api.create_order.return_value = {"uuid": "sell-1"}
        executor.submit(
            mock_api,
            market="KRW-ETH",
            side="ask",
            volume=1.0,
            ord_type="market",
            exit_reason="STOP_LOSS",
            avg_buy_price=100.0,
        )
        mock_api.create_order.assert_called_once()

    def test_executor_blocks_buy_when_journal_pending(self):
        self.journal_bithumb.reconciliation_state = "PENDING"
        executor = SafeOrderExecutor(self.journal_bithumb)
        with self.assertRaises(RuntimeError) as ctx:
            executor.submit(_PriceExchange(), "KRW-ETH", "bid", volume=1.0, price=100.0)
        self.assertIn("PRE_BUY_GLOBAL_RECONCILIATION", str(ctx.exception))

    def test_ack_scheduler_enqueues_without_duplicate_order(self):
        scheduler = AckReconcileScheduler(min_interval_sec=0.0)
        executor = SafeOrderExecutor(self.journal_bithumb, ack_reconcile_scheduler=scheduler)
        executor.submit(_PriceExchange(), "KRW-ETH", "bid", volume=1.0, price=100.0, expected_price=100.0)
        self.assertEqual(len(scheduler._queue), 1)
        mock_api = MagicMock()
        mock_api.get_order.return_value = {"uuid": "ex-1", "state": "wait", "executed_volume": "0", "remaining_volume": "1"}
        reconciled = scheduler.reconcile_next(self.journal_bithumb, mock_api, fill_processor=None)
        self.assertTrue(reconciled)
        mock_api.create_order.assert_not_called()


if __name__ == "__main__":
    unittest.main()
