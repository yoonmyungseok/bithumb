"""업비트 REST 대사 우선 원칙과 거래소별 상태 격리 회귀 테스트."""

import os
import json
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from order_safety import OrderFillProcessor, OrderJournal, OrderStatus
from private_websocket_manager import BithumbPrivateWebSocketClient
from risk_manager import DailyRiskManager, TrailingStopTracker
from trade_memory import TradeMemoryManager
from websocket_manager import BithumbWebSocketClient


class UpbitReconciliationSafetyTests(unittest.TestCase):
    """불완전한 WebSocket 체결 정보가 실현 손익으로 번지는 것을 방지한다."""

    def setUp(self):
        # 각 케이스는 독립 상태 파일을 사용해 체결·손익 결과가 섞이지 않게 한다.
        self.test_dir = tempfile.mkdtemp(prefix="test_upbit_reconcile_")
        self.journal = OrderJournal(data_dir=self.test_dir, exchange_scope="upbit")
        self.risk = DailyRiskManager(data_dir=self.test_dir)
        self.memory = TradeMemoryManager(data_dir=self.test_dir, exchange_scope="upbit")
        self.processor = OrderFillProcessor(
            self.journal, self.risk, self.memory, TrailingStopTracker(data_dir=self.test_dir),
        )

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_private_event_requires_rest_before_realized_pnl(self):
        """Private 이벤트의 주문가 0 또는 원수량은 체결·손익으로 확정하지 않는다."""
        client_id = self.journal.record_intent(
            "KRW-BTC", "ask", 1.0, None, "market", avg_buy_price=100_000_000.0, exchange="upbit",
        )
        applied = self.journal.apply_private_order_event(
            {"client_order_id": client_id, "state": "done", "volume": 1.0, "price": 0.0},
            fill_processor=self.processor,
            require_rest_confirmation=True,
        )
        self.assertTrue(applied)
        self.assertEqual(self.journal.get_order_by_client_id(client_id)["status"], OrderStatus.RECONCILIATION_PENDING)
        self.assertEqual(self.risk.total_trades_today, 0)
        self.assertEqual(self.memory.get_recent_trades(), [])

    def test_contradictory_rest_quantities_stay_pending(self):
        """체결수량과 잔량의 합이 주문수량을 넘으면 REST 응답도 신뢰하지 않는다."""
        client_id = self.journal.record_intent("KRW-ETH", "bid", 2.0, 4_000_000.0, "limit", exchange="upbit")
        self.journal.mark(client_id, OrderStatus.OPEN, exchange_uuid="upbit-1")
        get_order = MagicMock(return_value={
            "uuid": "upbit-1", "state": "done", "executed_volume": "2.0", "remaining_volume": "2.0",
            "price": "4000000", "paid_fee": "0",
        })
        self.assertEqual(self.journal.reconcile_exchange_statuses(get_order, fill_processor=self.processor), 0)
        order = self.journal.get_order_by_client_id(client_id)
        self.assertEqual(order["status"], OrderStatus.RECONCILIATION_PENDING)
        self.assertFalse(self.journal.complete_reconciliation_if_safe())

    def test_verified_rest_fill_allows_entry_after_reconciliation(self):
        """평균 체결가·수량이 검증된 REST 응답만 진입 시각과 READY 상태를 허용한다."""
        client_id = self.journal.record_intent("KRW-SOL", "bid", 1.0, 200_000.0, "limit", exchange="upbit")
        self.journal.mark(client_id, OrderStatus.OPEN, exchange_uuid="upbit-2")
        get_order = MagicMock(return_value={
            "uuid": "upbit-2", "state": "done", "executed_volume": "1.0", "remaining_volume": "0",
            "paid_fee": "80", "trades": [{"price": "200000", "volume": "1.0"}],
        })
        self.assertEqual(self.journal.reconcile_exchange_statuses(get_order, fill_processor=self.processor), 1)
        self.assertTrue(self.journal.complete_reconciliation_if_safe())
        self.assertTrue(self.journal.is_entry_ready())

    def test_exchange_scoped_memory_excludes_bithumb_trade(self):
        """업비트 AI 피드백은 빗썸 거래 결과를 포함하지 않는다."""
        self.memory.record_completed_trade(
            "KRW-BTC", "EXIT", 1.0, 2.0, 100.0, 1.0, "검증", "2026-08-25", exchange="bithumb",
        )
        self.assertEqual(self.memory.get_recent_trades(), [])

    def test_bithumb_private_events_are_deferred_to_main_thread(self):
        """빗썸 Private 이벤트는 수신 콜백에서 즉시 주문 저널을 수정하지 않는다."""
        received = []
        client = BithumbPrivateWebSocketClient("access", "secret", on_order=received.append)
        client._on_message(None, '{"type":"myOrder","identifier":"bot-1","state":"done"}')
        self.assertEqual(received, [])
        self.assertEqual(client.drain_order_events(), 1)
        self.assertEqual(received[0]["identifier"], "bot-1")

    def test_bithumb_public_callbacks_are_deferred_to_main_thread(self):
        """빗썸 시세 수신 스레드는 가격 캐시와 큐 적재만 수행한다."""
        received = []
        client = BithumbWebSocketClient(on_price_callback=lambda market, price: received.append((market, price)))
        client._on_message(None, '{"type":"ticker","code":"KRW-BTC","trade_price":100000000}')
        self.assertEqual(received, [])
        self.assertEqual(client.drain_callbacks(), 1)
        self.assertEqual(received, [("KRW-BTC", 100000000.0)])

    def test_scope_migration_rewrites_source_once_and_preserves_audit(self):
        """타 거래소 기록은 감사 파일에 남기고 원본을 전용 스키마로 한 번만 전환한다."""
        memory_path = os.path.join(self.test_dir, "trade_memory.json")
        with open(memory_path, "w", encoding="utf-8") as memory_file:
            json.dump([
                {"market": "KRW-BTC", "exchange": "upbit"},
                {"market": "KRW-ETH", "exchange": "bithumb"},
            ], memory_file)

        manager = TradeMemoryManager(memory_file=memory_path, exchange_scope="upbit")
        self.assertEqual([trade["market"] for trade in manager.trades], ["KRW-BTC"])
        with open(memory_path, "r", encoding="utf-8") as memory_file:
            migrated = json.load(memory_file)
        self.assertEqual(migrated["exchange_scope"], "upbit")
        self.assertEqual(len(migrated["completed_trades"]), 1)
        self.assertTrue(os.path.exists(f"{memory_path}.upbit.foreign-trades.audit.json"))
        self.assertTrue(os.path.exists(f"{memory_path}.upbit.pre-migration.audit.json"))

    def test_bithumb_legacy_memory_is_tagged_during_migration(self):
        """빗썸 전용 저장소의 구형 무표기 기록은 손실 없이 빗썸 범위로 승격한다."""
        memory_path = os.path.join(self.test_dir, "trade_memory.json")
        with open(memory_path, "w", encoding="utf-8") as memory_file:
            json.dump([{"market": "KRW-XRP", "exchange": ""}], memory_file)

        manager = TradeMemoryManager(
            memory_file=memory_path, exchange_scope="bithumb", legacy_exchange="bithumb",
        )
        self.assertEqual(manager.trades[0]["exchange"], "bithumb")


if __name__ == "__main__":
    unittest.main()
