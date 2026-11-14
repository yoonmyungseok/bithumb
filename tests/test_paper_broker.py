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


class OrderbookPrices:
    def __init__(self, best_ask=10_000.0, best_bid=9_990.0):
        self.best_ask = best_ask
        self.best_bid = best_bid

    def get_current_price(self, market):
        return self.best_ask

    def get_orderbook(self, market):
        return {
            "orderbook_units": [
                {"ask_price": self.best_ask, "ask_size": 2.0, "bid_price": self.best_bid, "bid_size": 2.0},
                {"ask_price": self.best_ask + 100.0, "ask_size": 10.0, "bid_price": self.best_bid - 100.0, "bid_size": 10.0},
            ]
        }


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

    def test_orderbook_vwap_slippage_on_market_buy_and_sell(self):
        with tempfile.TemporaryDirectory() as directory:
            api = OrderbookPrices(best_ask=10_000.0, best_bid=9_990.0)
            broker = PaperBroker(api, 100_000, fee_rate=0.0, data_dir=directory, slippage_rate=0.0005)
            # 50,000원 시장가 매수 -> 1호가 2.0개(20,000원) + 2호가(10,100원) 소진 -> 평균 체결가 > 10,000
            buy_res = broker.create_order("KRW-BTC", "bid", price=50_000, ord_type="price")
            self.assertEqual(buy_res["state"], "done")
            self.assertGreater(buy_res["avg_price"], 10_000.0)

            qty = broker.get_balances()["BTC"]["balance"]
            sell_res = broker.create_order("KRW-BTC", "ask", volume=qty, ord_type="market")
            self.assertEqual(sell_res["state"], "done")
            self.assertLess(sell_res["avg_price"], 9_990.0)

    def test_limit_order_waits_when_away_from_spread_and_cancels(self):
        with tempfile.TemporaryDirectory() as directory:
            api = OrderbookPrices(best_ask=10_000.0, best_bid=9_990.0)
            broker = PaperBroker(api, 100_000, fee_rate=0.0, data_dir=directory)
            res = broker.create_order("KRW-BTC", "bid", volume=3.0, price=9_500.0, ord_type="limit")
            self.assertEqual(res["state"], "wait")
            self.assertAlmostEqual(broker.get_balances()["KRW"]["balance"], 100_000.0 - 28_500.0)
            self.assertAlmostEqual(broker.get_balances()["KRW"]["locked"], 28_500.0)
            self.assertEqual(len(broker.get_open_orders("KRW-BTC")), 1)

            canceled = broker.cancel_order(uuid_str=res["uuid"])
            self.assertEqual(canceled["state"], "cancel")
            self.assertAlmostEqual(broker.get_balances()["KRW"]["balance"], 100_000.0)
            self.assertAlmostEqual(broker.get_balances()["KRW"]["locked"], 0.0)

    def test_limit_order_fills_when_orderbook_crosses(self):
        with tempfile.TemporaryDirectory() as directory:
            api = OrderbookPrices(best_ask=10_000.0, best_bid=9_990.0)
            broker = PaperBroker(api, 100_000, fee_rate=0.0, data_dir=directory)
            res = broker.create_order("KRW-BTC", "bid", volume=2.0, price=9_500.0, ord_type="limit")
            self.assertEqual(res["state"], "wait")

            api.best_ask = 9_500.0
            checked = broker.get_order(uuid_str=res["uuid"])
            self.assertEqual(checked["state"], "done")
            self.assertAlmostEqual(broker.get_balances()["BTC"]["balance"], 2.0)
            self.assertAlmostEqual(broker.get_balances()["KRW"]["locked"], 0.0)


if __name__ == "__main__":
    unittest.main()
