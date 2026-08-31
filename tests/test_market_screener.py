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
        return [{"market": "KRW-BTC"}, {"market": "KRW-BAD"}, {"market": "KRW-WIDE"}, {"market": "KRW-THIN"}]

    def get_tickers(self, markets):
        return [
            {"market": "KRW-BAD", "trade_price": None, "signed_change_rate": None, "acc_trade_price_24h": None},
            {"market": "KRW-BTC", "trade_price": "100000", "signed_change_rate": "0.03", "acc_trade_price_24h": "6000000000"},
            {"market": "KRW-WIDE", "trade_price": "1000", "signed_change_rate": "0.05", "acc_trade_price_24h": "5000000000"},
            {"market": "KRW-THIN", "trade_price": "500", "signed_change_rate": "0.04", "acc_trade_price_24h": "4000000000"},
        ]

    def get_orderbook(self, market):
        if market == "KRW-BTC":
            return {
                "orderbook_units": [
                    {"ask_price": 100100.0, "bid_price": 100000.0, "bid_size": 300.0}  # Spread 0.1%, Depth 30M
                ]
            }
        elif market == "KRW-WIDE":
            return {
                "orderbook_units": [
                    {"ask_price": 1010.0, "bid_price": 1000.0, "bid_size": 30000.0}  # Spread 1.0% (> 0.35%)
                ]
            }
        elif market == "KRW-THIN":
            return {
                "orderbook_units": [
                    {"ask_price": 501.0, "bid_price": 500.0, "bid_size": 100.0}  # Spread 0.2%, Depth 50,000 (< 20M)
                ]
            }
        return {}


class MarketScreenerTests(unittest.TestCase):
    def test_null_ticker_fields_are_skipped_without_failing_cycle(self):
        result = MarketScreener(FakeAPI(), min_trade_value_krw=1).scan_markets(top_count=1)
        self.assertEqual([item["market"] for item in result], ["KRW-BTC"])

    def test_wide_spread_and_thin_depth_are_filtered_out(self):
        result = MarketScreener(FakeAPI(), min_trade_value_krw=1).scan_markets(top_count=3)
        selected_markets = [item["market"] for item in result]
        self.assertIn("KRW-BTC", selected_markets)
        self.assertNotIn("KRW-WIDE", selected_markets)
        self.assertNotIn("KRW-THIN", selected_markets)

    def test_early_breakout_candidate_is_selected_only_when_enabled(self):
        """초기 돌파 후보는 명시적으로 활성화한 경우에만 확인형 후보와 분리되어 반환된다."""
        class EarlyBreakoutAPI(FakeAPI):
            def get_all_markets(self):
                return [{"market": "KRW-BTC"}, {"market": "KRW-CONF"}, {"market": "KRW-EARLY"}]

            def get_tickers(self, markets):
                return [
                    {"market": "KRW-BTC", "trade_price": "100000", "signed_change_rate": "0.005", "acc_trade_price_24h": "0"},
                    {"market": "KRW-CONF", "trade_price": "2000", "signed_change_rate": "0.02", "acc_trade_price_24h": "5000000000"},
                    {"market": "KRW-EARLY", "trade_price": "1000", "signed_change_rate": "0.007", "acc_trade_price_24h": "5000000000"},
                ]

            def get_orderbook(self, market):
                return {"orderbook_units": [{"ask_price": 1001.0, "bid_price": 1000.0, "bid_size": 30000.0}]}

        disabled = MarketScreener(
            EarlyBreakoutAPI(), min_trade_value_krw=1, min_change_rate=0.01, enable_early_breakout=False,
        ).scan_markets(top_count=1)
        self.assertNotIn("KRW-EARLY", [item["market"] for item in disabled])

        enabled = MarketScreener(
            EarlyBreakoutAPI(), min_trade_value_krw=1, min_change_rate=0.01,
            enable_early_breakout=True, early_breakout_max_candidates=1,
        ).scan_markets(top_count=1)
        early = next(item for item in enabled if item["market"] == "KRW-EARLY")
        self.assertEqual(early["candidate_type"], "EARLY_BREAKOUT")


if __name__ == "__main__":
    unittest.main()
