import os
import sys
import tempfile
import types
import unittest

try:
    import requests
except ModuleNotFoundError:  # Keep unit tests runnable before optional runtime deps are installed.
    class RequestException(Exception):
        pass

    class Timeout(RequestException):
        pass

    class ConnectionError(RequestException):
        pass

    requests = types.SimpleNamespace(
        exceptions=types.SimpleNamespace(
            RequestException=RequestException,
            Timeout=Timeout,
            ConnectionError=ConnectionError,
        )
    )
    sys.modules["requests"] = requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from order_safety import (
    AmbiguousOrderError,
    CooldownManager,
    OrderJournal,
    RiskGuard,
    SafeOrderExecutor,
    calculate_risk_position_size,
)


class TimeoutBithumb:
    def create_order(self, *args, **kwargs):
        raise requests.exceptions.Timeout("response lost")


class SuccessBithumb:
    def create_order(self, *args, **kwargs):
        return {"uuid": "exchange-1"}

    def get_order(self, uuid):
        return {"uuid": uuid, "state": "done"}


class OrderSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.journal = OrderJournal(os.path.join(self.temp_dir.name, "orders.json"))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_timeout_is_recorded_and_blocks_a_duplicate_buy(self):
        executor = SafeOrderExecutor(self.journal)
        with self.assertRaises(AmbiguousOrderError):
            executor.submit(TimeoutBithumb(), "KRW-BTC", "bid", volume=1, price=10000)
        self.assertEqual(self.journal.orders[-1]["status"], "UNKNOWN")
        self.assertTrue(self.journal.has_unresolved_market("KRW-BTC"))

    def test_exchange_completion_unblocks_the_market(self):
        executor = SafeOrderExecutor(self.journal)
        executor.submit(SuccessBithumb(), "KRW-BTC", "bid", volume=1, price=10000)
        self.assertTrue(self.journal.has_unresolved_market("KRW-BTC"))
        self.assertEqual(self.journal.reconcile_exchange_statuses(SuccessBithumb().get_order), 1)
        self.assertEqual(self.journal.orders[-1]["status"], "FILLED")
        self.assertFalse(self.journal.has_unresolved_market("KRW-BTC"))

    def test_exchange_partial_fill_updates_status_to_partially_filled(self):
        executor = SafeOrderExecutor(self.journal)
        executor.submit(SuccessBithumb(), "KRW-BTC", "bid", volume=1, price=10000)
        
        def mock_get_order(_uuid):
            return {"uuid": _uuid, "status": "trade", "executed_volume": "0.5", "remaining_volume": "0.5"}
        
        self.assertEqual(self.journal.reconcile_exchange_statuses(mock_get_order), 1)
        self.assertEqual(self.journal.orders[-1]["status"], "PARTIALLY_FILLED")
        self.assertTrue(self.journal.has_unresolved_market("KRW-BTC"))

    def test_private_fill_event_updates_journal_by_client_order_id(self):
        client_id = self.journal.record_intent("KRW-BTC", "bid", 1, 10000, "limit")
        self.assertTrue(self.journal.apply_private_order_event({
            "client_order_id": client_id,
            "state": "trade",
            "order_id": "order-1",
            "executed_volume": "0.4",
            "remaining_volume": "0.6",
        }))
        order = self.journal.orders[-1]
        self.assertEqual(order["status"], "PARTIALLY_FILLED")
        self.assertEqual(order["exchange_order_id"], "order-1")

    def test_risk_guard_enforces_position_and_exposure_limits(self):
        guard = RiskGuard(5000, max_open_positions=2, max_position_pct=0.35, max_total_exposure_pct=0.85, max_order_krw=0)
        self.assertEqual(guard.validate_buy("KRW-BTC", 400_000, 900_000, 1_000_000, []), (False, "종목당 비중 한도 초과"))
        self.assertEqual(guard.validate_buy("KRW-BTC", 100_000, 900_000, 1_000_000, ["KRW-ETH", "KRW-XRP"]), (False, "동시 보유 종목 수 한도 초과"))
        self.assertEqual(guard.validate_buy("KRW-BTC", 100_000, 900_000, 1_000_000, []), (True, "OK"))

    def test_cooldown_manager(self):
        cd = CooldownManager(default_sl_cooldown=100.0, default_tp_cooldown=50.0)
        cd.record_exit("KRW-BTC", "STOP_LOSS")
        is_cd, rem = cd.is_in_cooldown("KRW-BTC")
        self.assertTrue(is_cd)
        self.assertGreater(rem, 0.0)

        is_cd_eth, _ = cd.is_in_cooldown("KRW-ETH")
        self.assertFalse(is_cd_eth)

    def test_calculate_risk_position_size(self):
        # 1,000,000 total equity, entry=100,000, SL=98,000 (2% stop), risk 1% = 10,000 max loss
        # Expected position = 10,000 / (~0.0218) ~= 450,000 -> capped at 35% (350,000)
        size = calculate_risk_position_size(
            total_equity=1_000_000.0,
            entry_price=100_000.0,
            stop_loss=98_000.0,
            risk_fraction=0.01,
            max_position_pct=0.35,
        )
    def test_is_managed_order_filters_external_orders(self):
        client_id = self.journal.record_intent("KRW-BTC", "bid", 1, 10000, "limit")
        self.journal.mark(client_id, "OPEN", exchange_uuid="uuid-bot-1")

        # 봇이 생성한 주문 ID / UUID는 True
        self.assertTrue(self.journal.is_managed_order(client_id))
        self.assertTrue(self.journal.is_managed_order("uuid-bot-1"))

        # 수동으로 생성된 외부 주문은 False
        self.assertFalse(self.journal.is_managed_order("uuid-manual-external-999"))
        self.assertFalse(self.journal.is_managed_order(""))


if __name__ == "__main__":
    unittest.main()
