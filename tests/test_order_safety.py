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

from order_safety import AmbiguousOrderError, OrderJournal, RiskGuard, SafeOrderExecutor


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


if __name__ == "__main__":
    unittest.main()
