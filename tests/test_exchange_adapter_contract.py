import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from exchange_adapter import BithumbAdapter, ExchangeClient, UpbitAdapter
from trading_orchestrator import TradingOrchestrator


class FakeExchangeClient:
    def __init__(self):
        self.created_orders = []

    def get_balances(self): return {"KRW": {"balance": 1_000_000.0}}
    def get_candles(self, unit=5, count=30, market="KRW-BTC", to=None): return [{"market": market, "unit": unit, "count": count}]
    def get_orderbook(self, market="KRW-BTC"): return {"market": market}
    def get_current_price(self, market="KRW-BTC"): return 100.0
    def adjust_price_to_tick(self, price, side="bid", mode=None): return price
    def get_open_orders(self, market=None): return []
    def get_order(self, uuid_str="", client_order_id=""): return {"uuid": uuid_str, "client_order_id": client_order_id}
    def create_order(self, **kwargs):
        self.created_orders.append(kwargs)
        return {"uuid": "test-order", **kwargs}
    def cancel_order(self, uuid_str="", client_order_id=""): return {"uuid": uuid_str, "client_order_id": client_order_id}


class ExchangeAdapterContractTests(unittest.TestCase):
    def test_existing_clients_satisfy_the_shared_contract(self):
        self.assertIsInstance(FakeExchangeClient(), ExchangeClient)

    def test_common_market_data_and_order_contract_is_preserved(self):
        client = FakeExchangeClient()
        adapter = BithumbAdapter(client, data_dir="data/bithumb", web_port=7979)

        self.assertEqual(adapter.key, "bithumb")
        self.assertEqual(adapter.get_candles(market="KRW-BTC")[0]["market"], "KRW-BTC")
        order = adapter.create_order("KRW-BTC", "bid", volume=1.0, price=100.0, client_order_id="entry-1")
        self.assertEqual(order["client_order_id"], "entry-1")
        self.assertEqual(client.created_orders[0]["side"], "bid")

    def test_upbit_protected_market_is_rejected_at_the_adapter_boundary(self):
        client = FakeExchangeClient()
        adapter = UpbitAdapter(client)

        self.assertFalse(adapter.is_tradeable_market("krw-holo"))
        with self.assertRaises(ValueError):
            adapter.create_order("KRW-HOLO", "bid", volume=1.0, price=100.0)
        self.assertEqual(client.created_orders, [])

    def test_shared_orchestrator_reconciles_using_the_adapter_contract(self):
        class Journal:
            def __init__(self): self.completed = False
            def reconcile_exchange_statuses(self, **kwargs):
                self.get_order = kwargs["get_order"]
                return 2
            def complete_reconciliation_if_safe(self): self.completed = True

        journal = Journal()
        adapter = BithumbAdapter(FakeExchangeClient())
        reconciled = TradingOrchestrator(__import__("logging").getLogger("test")).reconcile_orders(adapter, journal, object())
        self.assertEqual(reconciled, 2)
        self.assertTrue(journal.completed)
        self.assertEqual(journal.get_order("uuid-1")["uuid"], "uuid-1")

    def test_shared_market_selection_merges_manual_and_held_markets(self):
        class Screener:
            def scan_markets(self, **kwargs): return [{"market": "KRW-XRP"}, {"market": "KRW-HOLO"}]

        orchestrator = TradingOrchestrator(__import__("logging").getLogger("test"))
        adapter = UpbitAdapter(FakeExchangeClient())
        manual = orchestrator.select_target_markets(
            adapter, held_markets=["KRW-BTC"], is_auto_mode=False, raw_markets="KRW-ETH,KRW-HOLO",
            max_positions=2, top_count=3, create_screener=Screener,
        )
        automated = orchestrator.select_target_markets(
            adapter, held_markets=[], is_auto_mode=True, raw_markets="AUTO",
            max_positions=2, top_count=3, create_screener=Screener,
        )
        self.assertEqual(manual, ["KRW-BTC", "KRW-ETH"])
        self.assertEqual(automated, ["KRW-XRP"])


if __name__ == "__main__":
    unittest.main()
