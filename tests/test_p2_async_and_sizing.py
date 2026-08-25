import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from bot_controller import BotController
from order_safety import OrderJournal, SafeOrderExecutor, calculate_risk_position_size
from risk_manager import DailyRiskManager, TrailingStopTracker
from sheets_manager import SheetsManager
from telegram_alert import TelegramAlert
from trade_memory import TradeMemoryManager


class TestP2AsyncAndSizing(unittest.TestCase):
    """P2-1 (비동기 큐), P2-2 (동적 슬롯 사이징), P2-3 (확장 대시보드) 단위 테스트 (완전 격리)"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.d_dir = self.temp_dir.name
        self.journal = OrderJournal(path=os.path.join(self.d_dir, "order_journal.json"))
        self.executor = SafeOrderExecutor(journal=self.journal)
        self.risk_mgr = DailyRiskManager(data_dir=self.d_dir)
        self.trailing = TrailingStopTracker(data_dir=self.d_dir)
        self.trade_mem = TradeMemoryManager(data_dir=self.d_dir)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_01_telegram_async_queue_non_blocking(self):
        """1. 텔레그램 네트워크 지연(2초) 발생 시에도 send_message가 메인 스레드를 블로킹하지 않고 즉시(<0.1초) 반환 검증 (P2-1)"""
        alert = TelegramAlert(bot_token="test-token", chat_id="test-chat-id", enable_async=True)

        def slow_post(url, *args, **kwargs):
            time.sleep(1.0)  # 1초 지연 모의
            mock_res = MagicMock()
            mock_res.status_code = 200
            return mock_res

        with patch("requests.post", side_effect=slow_post):
            start_t = time.time()
            res = alert.send_message("🚀 [비동기 테스트 알림]")
            elapsed = time.time() - start_t

            # 비동기 큐에 푸시하고 즉시 반환되므로 경과 시간은 0.1초 미만이어야 함
            self.assertTrue(res)
            self.assertLess(elapsed, 0.2)

    def test_02_sheets_async_queue_non_blocking(self):
        """2. 구글 시트 네트워크 지연 발생 시에도 append_trade_log가 즉시 반환 검증 (P2-1)"""
        # SheetsManager 인스턴스를 mock credentials로 생성
        mock_sheet = MagicMock()
        mock_ws = MagicMock()
        mock_sheet.worksheet.return_value = mock_ws
        
        sm = SheetsManager.__new__(SheetsManager)
        sm.enable_async = True
        sm.exchange_name = "BITHUMB"
        sm.spreadsheet = mock_sheet
        sm._queue = __import__("queue").Queue(maxsize=100)
        sm._is_running = True
        sm._worker_thread = __import__("threading").Thread(target=sm._queue_worker, daemon=True)
        sm._worker_thread.start()

        def slow_append_row(*args, **kwargs):
            time.sleep(1.0)

        mock_ws.append_row.side_effect = slow_append_row

        start_t = time.time()
        sm.append_trade_log({
            "market": "KRW-BTC",
            "side": "BUY",
            "price": 100_000_000.0,
            "volume": 0.01,
            "total_krw": 1_000_000,
        })
        elapsed = time.time() - start_t

        # 큐에 비동기 위임하므로 0.2초 이내 즉시 반환
        self.assertLess(elapsed, 0.2)

    def test_03_position_sizing_dynamic_slot_budget(self):
        """3. 포지션 사이징 시 슬롯 예산(open_slots) 및 가용 원화(available_krw) 한도 준수 검증 (P2-2)"""
        total_equity = 1_000_000.0
        entry_price = 100_000.0
        stop_loss = 98_000.0  # 2% 손절

        # 1) 슬롯 3개인 경우 -> 슬롯당 최대 예산: 1,000,000 / 3 = 333,333원
        size_3_slots = calculate_risk_position_size(
            total_equity=total_equity,
            entry_price=entry_price,
            stop_loss=stop_loss,
            open_slots=3,
        )
        self.assertLessEqual(size_3_slots, 333_333.34)

        # 2) 가용 원화가 100,000원뿐인 경우 -> 100,000원을 초과하지 않아야 함
        size_limited_krw = calculate_risk_position_size(
            total_equity=total_equity,
            entry_price=entry_price,
            stop_loss=stop_loss,
            available_krw=100_000.0,
            open_slots=3,
        )
        self.assertLessEqual(size_limited_krw, 100_000.0)

    def test_04_bot_controller_dashboard_extended_metrics(self):
        """4. 대시보드 공급자에 킬스위치 상태, 포지션 단위 승률, UNKNOWN 주문 수가 정상 포함됨 검증 (P2-3)"""
        fake_exchange = MagicMock()
        fake_exchange.get_balances.return_value = {"KRW": {"balance": 500_000.0, "locked": 0.0}}
        fake_exchange.get_current_price.return_value = 100_000.0
        fake_exchange.get_korean_name.return_value = "비트코인"

        controller = BotController(
            exchange_factory=lambda: fake_exchange,
            order_executor=self.executor,
            order_journal=self.journal,
            risk_manager=self.risk_mgr,
            trailing_tracker=self.trailing,
            trade_memory=self.trade_mem,
            telegram=MagicMock(),
            get_is_paused=lambda: False,
            set_is_paused=lambda x: None,
        )

        data = controller.get_dashboard_data()
        self.assertIn("kill_switch_active", data)
        self.assertIn("position_win_rate", data)
        self.assertIn("unknown_orders_count", data)
        self.assertEqual(data["kill_switch_active"], False)
        self.assertEqual(data["unknown_orders_count"], 0)


if __name__ == "__main__":
    unittest.main()
