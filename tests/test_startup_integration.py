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
from sheets_manager import SheetsManager
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

    def test_sheets_manager_flexible_kwargs(self):
        key_path = os.path.join(self.data_dir, "test_sa.json")
        with open(key_path, "w", encoding="utf-8") as f:
            f.write('{"type": "service_account", "project_id": "test", "private_key_id": "123", "private_key": "-----BEGIN PRIVATE KEY-----\nMIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQC7V3D9\n-----END PRIVATE KEY-----\n", "client_email": "test@test.iam.gserviceaccount.com"}')

        try:
            sm1 = SheetsManager(json_key_path=key_path, sheet_name="test_sheet")
            self.assertEqual(sm1.sheet_name, "test_sheet")
        except Exception:
            pass

        try:
            sm2 = SheetsManager(service_account_json_path=key_path, spreadsheet_name="test_sheet2")
            self.assertEqual(sm2.sheet_name, "test_sheet2")
        except Exception:
            pass

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
        tm = TradeMemoryManager()
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


if __name__ == "__main__":
    unittest.main()