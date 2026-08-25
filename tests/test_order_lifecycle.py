import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from order_safety import AmbiguousOrderError, OrderJournal, OrderStatus, SafeOrderExecutor
from trade_memory import TradeMemoryManager
import requests


class TestOrderLifecycle(unittest.TestCase):
    """P0-1 & P0-3 주문 수명주기 및 접수/체결 분리 단위 테스트 (완전 격리)"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.journal_path = os.path.join(self.temp_dir.name, "order_journal.json")
        self.memory_path = os.path.join(self.temp_dir.name, "trade_memory.json")
        self.journal = OrderJournal(path=self.journal_path)
        self.executor = SafeOrderExecutor(journal=self.journal)
        self.trade_memory = TradeMemoryManager(memory_file=self.memory_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_01_submit_acknowledged_status(self):
        """1. POST 응답 성공 직후 상태는 ACKNOWLEDGED이며 가상 손익 미생성 검증"""
        fake_exchange = MagicMock()
        fake_exchange.create_order.return_value = {"uuid": "EX-1001", "status": "wait"}

        res = self.executor.submit(
            fake_exchange,
            market="KRW-BTC",
            side="bid",
            volume=0.01,
            price=100_000_000.0,
            ord_type="limit",
        )
        self.assertIn("client_order_id", res)
        client_id = res["client_order_id"]

        order = self.journal.get_order_by_client_id(client_id)
        self.assertIsNotNone(order)
        self.assertEqual(order["status"], OrderStatus.ACKNOWLEDGED)
        self.assertEqual(order["exchange_uuid"], "EX-1001")
        # 체결 전이므로 체결 수량은 0
        self.assertEqual(order["executed_volume"], 0.0)

    def test_02_private_websocket_fill_event(self):
        """2. Private WebSocket FILLED 이벤트 후에만 정식 체결 상태 갱신 검증"""
        fake_exchange = MagicMock()
        fake_exchange.create_order.return_value = {"uuid": "EX-1002"}

        res = self.executor.submit(fake_exchange, market="KRW-ETH", side="bid", volume=1.0, price=3_000_000.0)
        client_id = res["client_order_id"]

        # Private WS done 이벤트 수신 시뮬레이션
        ws_event = {
            "type": "myOrder",
            "client_order_id": client_id,
            "order_id": "EX-1002",
            "state": "done",
            "executed_volume": 1.0,
            "remaining_volume": 0.0,
            "price": 3_005_000.0,
            "paid_fee": 1500.0,
        }
        success = self.journal.apply_private_order_event(ws_event)
        self.assertTrue(success)

        updated_order = self.journal.get_order_by_client_id(client_id)
        self.assertEqual(updated_order["status"], OrderStatus.FILLED)
        self.assertEqual(updated_order["executed_volume"], 1.0)
        self.assertEqual(updated_order["avg_price"], 3_005_000.0)
        self.assertEqual(updated_order["fee"], 1500.0)

    def test_03_partial_fill_handling(self):
        """3. 부분체결 시 체결수량과 잔여수량 정확 분리 검증"""
        fake_exchange = MagicMock()
        fake_exchange.create_order.return_value = {"uuid": "EX-1003"}

        res = self.executor.submit(fake_exchange, market="KRW-SOL", side="ask", volume=10.0, price=200_000.0)
        client_id = res["client_order_id"]

        # 부분체결 이벤트 (4개 체결, 6개 잔여)
        ws_event = {
            "type": "myOrder",
            "client_order_id": client_id,
            "state": "trade",
            "executed_volume": 4.0,
            "remaining_volume": 6.0,
            "price": 200_000.0,
            "paid_fee": 400.0,
        }
        self.journal.apply_private_order_event(ws_event)

        order = self.journal.get_order_by_client_id(client_id)
        self.assertEqual(order["status"], OrderStatus.PARTIALLY_FILLED)
        self.assertEqual(order["executed_volume"], 4.0)
        self.assertEqual(order["remaining_volume"], 6.0)

    def test_04_partial_fill_then_cancel(self):
        """4. 부분체결 후 취소 시 실제 체결수량만 저널에 보존 검증"""
        fake_exchange = MagicMock()
        fake_exchange.create_order.return_value = {"uuid": "EX-1004"}

        res = self.executor.submit(fake_exchange, market="KRW-XRP", side="bid", volume=100.0, price=800.0)
        client_id = res["client_order_id"]

        # 30개 부분체결
        self.journal.apply_private_order_event({
            "client_order_id": client_id,
            "state": "trade",
            "executed_volume": 30.0,
            "remaining_volume": 70.0,
            "price": 800.0,
        })
        # 나머지 70개 취소
        self.journal.apply_private_order_event({
            "client_order_id": client_id,
            "state": "cancel",
            "executed_volume": 30.0,
            "remaining_volume": 0.0,
        })

        order = self.journal.get_order_by_client_id(client_id)
        self.assertEqual(order["status"], OrderStatus.CANCELED)
        self.assertEqual(order["executed_volume"], 30.0)

    def test_05_timeout_unknown_blocks_duplicate(self):
        """5. Timeout 발생 시 UNKNOWN 기록 및 동일 종목 신규 주문 차단 검증"""
        fake_exchange = MagicMock()
        fake_exchange.create_order.side_effect = requests.exceptions.Timeout("Read timed out")

        with self.assertRaises(AmbiguousOrderError):
            self.executor.submit(fake_exchange, market="KRW-DOGE", side="bid", volume=1000.0, price=200.0)

        # UNKNOWN 상태 주문 존재 확인
        self.assertTrue(self.journal.has_unknown_market("KRW-DOGE"))

        # 동일 마켓 신규 주문 시도 시 원천 차단 확인
        with self.assertRaises(RuntimeError) as cm:
            self.executor.submit(fake_exchange, market="KRW-DOGE", side="bid", volume=1000.0, price=200.0)
        self.assertIn("UNKNOWN", str(cm.exception))

    def test_06_reconcile_resolves_unknown(self):
        """6. REST 주문 조회를 통한 reconciliation으로 UNKNOWN 해소 검증"""
        fake_exchange = MagicMock()
        fake_exchange.create_order.side_effect = requests.exceptions.Timeout("Timeout")

        try:
            self.executor.submit(fake_exchange, market="KRW-ADA", side="bid", volume=50.0, price=500.0)
        except AmbiguousOrderError:
            pass

        orders = self.journal.orders
        self.assertEqual(orders[-1]["status"], OrderStatus.UNKNOWN)
        client_id = orders[-1]["client_order_id"]

        # REST 조회 모의
        def fake_get_order_by_client_id(cid):
            if cid == client_id:
                return {
                    "uuid": "EX-ADA-RESOLVED",
                    "state": "done",
                    "executed_volume": "50.0",
                    "remaining_volume": "0.0",
                    "price": "500.0",
                    "paid_fee": "12.5",
                }
            return {}

        updated_cnt = self.journal.reconcile_exchange_statuses(
            get_order=MagicMock(),
            get_order_by_client_id=fake_get_order_by_client_id,
        )
        self.assertEqual(updated_cnt, 1)

        resolved_order = self.journal.get_order_by_client_id(client_id)
        self.assertEqual(resolved_order["status"], OrderStatus.FILLED)
        self.assertFalse(self.journal.has_unknown_market("KRW-ADA"))

    def test_07_realized_pnl_calculation_and_position_stats(self):
        """7. 실제 체결평균가와 수수료 기반 손익 계산 및 포지션 단위 통계 검증"""
        # 동일 포지션(pos-1)에 대해 1차 50% 분할익절 + 2차 전량청산 기록
        self.trade_memory.record_completed_trade(
            market="KRW-BTC",
            side="PARTIAL_TP",
            entry_price=100_000_000.0,
            exit_price=103_000_000.0,
            filled_volume=0.05,
            fee=2575.0,
            pnl_pct=3.0,
            pnl_krw=147_425.0,
            reason="1차 분할익절",
            timestamp="2026-08-25 12:00:00",
            position_id="pos-1",
        )
        self.trade_memory.record_completed_trade(
            market="KRW-BTC",
            side="TRAILING_STOP",
            entry_price=100_000_000.0,
            exit_price=104_000_000.0,
            filled_volume=0.05,
            fee=2600.0,
            pnl_pct=4.0,
            pnl_krw=197_400.0,
            reason="트레일링 스탑 완결",
            timestamp="2026-08-25 12:30:00",
            position_id="pos-1",
        )

        stats = self.trade_memory.get_position_level_stats()
        # 2건의 주문이 동일 position_id이므로 총 포지션 수는 1개로 집계되어야 함 (P0-3 승률 왜곡 방지)
        self.assertEqual(stats["total_positions"], 1)
        self.assertEqual(stats["win_positions"], 1)
        self.assertEqual(stats["position_win_rate_pct"], 100.0)
        self.assertAlmostEqual(stats["total_realized_pnl_krw"], 344_825.0, places=1)


if __name__ == "__main__":
    unittest.main()
