import os
import sys
import types
import unittest

try:
    import requests  # noqa: F401
except ModuleNotFoundError:
    module = types.ModuleType("requests")
    module.exceptions = types.SimpleNamespace(
        RequestException=Exception,
        Timeout=TimeoutError,
        ConnectionError=ConnectionError,
    )
    sys.modules["requests"] = module

if "jwt" not in sys.modules:
    sys.modules["jwt"] = types.SimpleNamespace(encode=lambda *args, **kwargs: "test-token")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from market_screener import MarketScreener


class FakeAPI:
    def get_all_markets(self):
        return [{"market": "KRW-BTC"}, {"market": "KRW-BAD"}]

    def get_tickers(self, markets):
        return [
            {"market": "KRW-BAD", "trade_price": None, "signed_change_rate": None, "acc_trade_price_24h": None},
            {"market": "KRW-BTC", "trade_price": "100000", "signed_change_rate": "0.03", "acc_trade_price_24h": "6000000000"},
        ]


class MarketScreenerTests(unittest.TestCase):
    def test_null_ticker_fields_are_skipped_without_failing_cycle(self):
        result = MarketScreener(FakeAPI(), min_trade_value_krw=1).scan_markets(top_count=1)
        self.assertEqual([item["market"] for item in result], ["KRW-BTC"])


if __name__ == "__main__":
    unittest.main()
