import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from bithumb_api import BithumbAPI
from bot_controller import BotController
from order_safety import CooldownManager, OrderJournal, RiskGuard, SafeOrderExecutor
from realtime_engine import RealtimeRiskEngine
from risk_manager import DailyRiskManager, TrailingStopTracker
from telegram_alert import TelegramAlert
from trade_memory import TradeMemoryManager
from web_server import DashboardWebServer


class StartupAndIntegrationAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.data_dir = self.tmp_dir.name

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_dashboard_web_server_startup_and_stop(self):
        data_calls = []
        action_calls = []

        def mock_data_provider():
            data_calls.append(True)
            return {"status": "ok", "total_equity": 100000}

        def mock_action_handler(action: str):
            action_calls.append(action)
            return f"handled {action}"

        server = DashboardWebServer(
            host="127.0.0.1",
            port=7980,
            data_provider=mock_data_provider,
            action_handler=mock_action_handler,
        )
        server.start()
        self.assertIsNotNone(server.server)
        server.stop()

    def test_bithumb_api_round_volume_and_tick(self):
        vol = BithumbAPI.round_volume("KRW-BTC", 0.12345678)
        self.assertEqual(vol, 0.1235)

        vol_zero = BithumbAPI.round_volume("KRW-XRP", -1.0)
        self.assertEqual(vol_zero, 0.0)

        tick = BithumbAPI.round_price_to_tick(12345.67)
        self.assertEqual(tick, 12350.0)

    def test_full_system_wiring(self):
        journal = OrderJournal(path=os.path.join(self.data_dir, "journal.json"))
        executor = SafeOrderExecutor(journal)
        cooldown = CooldownManager(state_file=os.path.join(self.data_dir, "cd.json"))
        rm = DailyRiskManager(data_dir=self.data_dir)
        tst = TrailingStopTracker(data_dir=self.data_dir)
        tm = TradeMemoryManager(data_dir=self.data_dir)
        tg = TelegramAlert("test-token", "123456")

        mock_exchange = types.SimpleNamespace(
            get_balances=lambda: {
                "KRW": {"balance": 100000.0, "locked": 0.0},
                "XRP": {"balance": 10.0, "locked": 0.0, "avg_buy_price": 1000.0},
            },
            get_korean_name=lambda m: "리플",
            get_current_price=lambda m: 1050.0,
            get_open_orders=lambda market=None: [],
            cancel_order=lambda uuid: {"status": "0000"},
            round_volume=BithumbAPI.round_volume,
            round_price_to_tick=BithumbAPI.round_price_to_tick,
            create_order=lambda market, side, volume, price=None, ord_type="limit", client_order_id="": {"uuid": "ord-999"},
        )

        paused = [False]

        rt_engine = RealtimeRiskEngine(
            exchange_factory=lambda: mock_exchange,
            order_executor=executor,
            order_journal=journal,
            risk_manager=rm,
            cooldown_manager=cooldown,
            trade_memory=tm,
            trailing_tracker=tst,
            telegram=tg,
            min_order_krw=5000.0,
        )

        controller = BotController(
            exchange_factory=lambda: mock_exchange,
            order_executor=executor,
            order_journal=journal,
            risk_manager=rm,
            trailing_tracker=tst,
            trade_memory=tm,
            telegram=tg,
            get_is_paused=lambda: paused[0],
            set_is_paused=lambda v: paused.__setitem__(0, v),
        )

        status_msg = controller.get_status_message()
        self.assertIn("총 평가 자산", status_msg)

        bal_msg = controller.get_balance_message()
        self.assertIn("KRW (원화)", bal_msg)

        dash_data = controller.get_dashboard_data()
        self.assertIn("total_equity", dash_data)
        self.assertIn("positions", dash_data)

        rt_engine.on_price_tick("KRW-XRP", 1050.0)
        self.assertFalse(paused[0])

    def test_full_system_wiring_upbit(self):
        from upbit_api import UpbitAPI
        journal = OrderJournal(data_dir=self.data_dir)
        executor = SafeOrderExecutor(journal)
        cooldown = CooldownManager(data_dir=self.data_dir)
        rm = DailyRiskManager(data_dir=self.data_dir)
        tst = TrailingStopTracker(data_dir=self.data_dir)
        tm = TradeMemoryManager(data_dir=self.data_dir)
        tg = TelegramAlert("test-token", "123456")

        mock_upbit = types.SimpleNamespace(
            get_balances=lambda: {
                "KRW": {"balance": 200000.0, "locked": 0.0},
                "ETH": {"balance": 0.05, "locked": 0.0, "avg_buy_price": 3800000.0},
            },
            get_korean_name=lambda m: "이더리움",
            get_current_price=lambda m: 3900000.0,
            get_open_orders=lambda market=None: [],
            cancel_order=lambda uuid_str="", client_order_id="": {"status": "ok"},
            round_volume=UpbitAPI.round_volume,
            round_price_to_tick=UpbitAPI.round_price_to_tick,
            create_order=lambda market, side, volume=None, price=None, ord_type="limit", client_order_id="": {"uuid": "upbit-ord-999"},
        )

        paused = [False]

        rt_engine = RealtimeRiskEngine(
            exchange_factory=lambda: mock_upbit,
            order_executor=executor,
            order_journal=journal,
            risk_manager=rm,
            cooldown_manager=cooldown,
            trade_memory=tm,
            trailing_tracker=tst,
            telegram=tg,
            min_order_krw=5000.0,
        )

        controller = BotController(
            exchange_factory=lambda: mock_upbit,
            order_executor=executor,
            order_journal=journal,
            risk_manager=rm,
            trailing_tracker=tst,
            trade_memory=tm,
            telegram=tg,
            get_is_paused=lambda: paused[0],
            set_is_paused=lambda v: paused.__setitem__(0, v),
            exchange_name="업비트",
            web_port=7980,
        )

        status_msg = controller.get_status_message()
        self.assertIn("업비트 AI 퀀트 봇", status_msg)
        self.assertIn("7980", status_msg)

        bal_msg = controller.get_balance_message()
        self.assertIn("업비트", bal_msg)
        self.assertIn("이더리움", bal_msg)

        dash_data = controller.get_dashboard_data()
        self.assertIn("total_equity", dash_data)
        self.assertEqual(dash_data["total_equity"], 395000)

        rt_engine.on_price_tick("KRW-ETH", 3900000.0)
        self.assertFalse(paused[0])


if __name__ == "__main__":
    unittest.main()
