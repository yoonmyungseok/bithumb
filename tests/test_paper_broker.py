import os
import sys
import tempfile
import types
import unittest

try:
    import requests  # noqa: F401
except ModuleNotFoundError:
    module = types.ModuleType("requests")
    module.exceptions = types.SimpleNamespace(RequestException=Exception, Timeout=Exception, ConnectionError=Exception)
    sys.modules["requests"] = module

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from paper_broker import PaperBroker


class PublicPrices:
    def get_current_price(self, market):
        return 10_000.0


class PaperBrokerTests(unittest.TestCase):
    def test_buy_and_sell_only_change_virtual_balances(self):
        with tempfile.TemporaryDirectory() as directory:
            broker = PaperBroker(PublicPrices(), 100_000, fee_rate=0.001)
            broker.state_path = os.path.join(directory, "paper.json")
            broker.create_order("KRW-BTC", "bid", volume=5, price=10_000, ord_type="limit")
            self.assertLess(broker.get_balances()["KRW"]["balance"], 100_000)
            self.assertGreater(broker.get_balances()["BTC"]["balance"], 0)
            quantity = broker.get_balances()["BTC"]["balance"]
            broker.create_order("KRW-BTC", "ask", volume=quantity, price=10_000, ord_type="limit")
            self.assertAlmostEqual(broker.get_balances()["BTC"]["balance"], 0.0)


if __name__ == "__main__":
    unittest.main()
