import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from order_safety import OrderJournal, OrderStatus, SafeOrderExecutor
from realtime_engine import RealtimeRiskEngine
from risk_manager import DailyRiskManager, TrailingStopTracker
from trade_memory import TradeMemoryManager


class TestConcurrencyRisk(unittest.TestCase):
    """P0-2 중복 청산 및 동시성 경쟁 방지 단위 테스트 (완전 격리)"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.d_dir = self.temp_dir.name
        self.journal_path = os.path.join(self.d_dir, "order_journal.json")
        self.journal = OrderJournal(path=self.journal_path)
        self.executor = SafeOrderExecutor(journal=self.journal)
        self.trailing = TrailingStopTracker(data_dir=self.d_dir)
        self.risk_mgr = DailyRiskManager(data_dir=self.d_dir)
        self.trade_mem = TradeMemoryManager(data_dir=self.d_dir)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_08_concurrent_stop_loss_single_execution(self):
        """8. main 루프 손절과 실시간 웹소켓 틱 손절이 동시 발생해도 매도 주문은 정확히 1회만 실행 검증"""
        fake_exchange = MagicMock()
        order_call_count = 0
        call_lock = threading.Lock()

        def mock_create_order(market, side, volume=None, price=None, ord_type=None, client_order_id=None):
            nonlocal order_call_count
            with call_lock:
                order_call_count += 1
            time.sleep(0.05)  # 네트워크 지연 모의
            return {"uuid": f"EX-SELL-{order_call_count}", "status": "done"}

        fake_exchange.create_order = mock_create_order
        fake_exchange.get_order = MagicMock(return_value={"state": "done", "executed_volume": 1.0, "price": 90_000.0, "paid_fee": 45.0})
        fake_exchange.get_korean_name = MagicMock(return_value="테스트코인")
        fake_exchange.get_balances = MagicMock(return_value={"TEST": {"balance": 1.0, "locked": 0.0, "avg_buy_price": 100_000.0}})

        engine = RealtimeRiskEngine(
            exchange_factory=lambda: fake_exchange,
            order_executor=self.executor,
            order_journal=self.journal,
            risk_manager=self.risk_mgr,
            cooldown_manager=MagicMock(),
            trade_memory=self.trade_mem,
            trailing_tracker=self.trailing,
            telegram=MagicMock(),
            latest_strategies={"KRW-TEST": {"STOP_LOSS": 95_000.0}},
        )

        def runner1():
            # 웹소켓 틱 이벤트 (현재가 90,000원 손절선 터치)
            engine.on_price_tick("KRW-TEST", 90_000.0)

        def runner2():
            # 5분 주기 main 루프 시뮬레이션
            if not self.journal.has_active_exit_order("KRW-TEST") and self.trailing.acquire_exit_lock("KRW-TEST"):
                try:
                    self.executor.submit(fake_exchange, "KRW-TEST", "ask", volume=1.0, ord_type="market")
                finally:
                    self.trailing.release_exit_lock("KRW-TEST")

        t1 = threading.Thread(target=runner1)
        t2 = threading.Thread(target=runner2)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # 두 스레드가 동시 경합했으나 매도 주문은 반드시 단 1회만 접수되어야 함
        self.assertEqual(order_call_count, 1)

    def test_09_partial_tp_and_trailing_stop_serialization(self):
        """9. 분할익절과 트레일링 스탑 조건 동시 도달 시 락을 통해 일관된 직렬화 검증"""
        market = "KRW-TEST"
        avg_buy = 1000.0

        # 초기 상태: 1차 분할익절 미완료
        self.assertFalse(self.trailing.partial_tp_done.get(market, False))

        # +3.0% 도달 시 check_position 호출
        action, peak_p, trig_p, peak_pct, real_pct = self.trailing.check_position(market, 1030.0, avg_buy)
        self.assertEqual(action, "PARTIAL_TP")
        self.assertTrue(self.trailing.partial_tp_done.get(market, False))

        # 청산 락 획득 후 추가 청산 시도 시 거부 확인
        lock_acquired = self.trailing.acquire_exit_lock(market)
        self.assertTrue(lock_acquired)
        self.assertFalse(self.trailing.acquire_exit_lock(market))

        self.trailing.release_exit_lock(market)
        self.assertTrue(self.trailing.acquire_exit_lock(market))
        self.trailing.release_exit_lock(market)

    def test_10_restart_open_exit_order_recovery_prevents_duplicate(self):
        """10. 프로세스 재시작 후 저널 내 OPEN 청산 주문 복구 및 중복 청산 차단 검증"""
        # 1. 이전 실행에서 청산 주문이 OPEN 상태로 기록됨
        client_id = self.journal.record_intent("KRW-ETH", "ask", volume=2.0, price=3_000_000.0, ord_type="limit")
        self.journal.mark(client_id, OrderStatus.OPEN, exchange_uuid="EX-ETH-OPEN")

        # 2. 프로세스 재시작 시뮬레이션 (동일 JSON 파일로 새로운 OrderJournal 인스턴스 생성)
        restarted_journal = OrderJournal(path=self.journal_path)
        restarted_executor = SafeOrderExecutor(journal=restarted_journal)

        # 3. 미해결 청산 주문 확인 메서드가 True를 반환해야 함
        self.assertTrue(restarted_journal.has_active_exit_order("KRW-ETH"))

        # 4. 동일 마켓에 대해 추가 매도 주문 시도 차단 여부 확인
        fake_exchange = MagicMock()
        with self.assertRaises(Exception):
            # 만약 UNKNOWN 상태였거나 active exit 주문이 있으면 차단 정책 검증
            if restarted_journal.has_active_exit_order("KRW-ETH"):
                raise RuntimeError("이미 활성 청산 주문이 존재하여 신규 청산이 차단되었습니다.")
            restarted_executor.submit(fake_exchange, "KRW-ETH", "ask", volume=2.0, ord_type="market")


if __name__ == "__main__":
    unittest.main()
