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
        return [{"market": "KRW-GOOD"}, {"market": "KRW-BAD"}, {"market": "KRW-WIDE"}, {"market": "KRW-THIN"}]

    def get_tickers(self, markets):
        return [
            {"market": "KRW-BAD", "trade_price": None, "signed_change_rate": None, "acc_trade_price_24h": None},
            {"market": "KRW-GOOD", "trade_price": "100000", "signed_change_rate": "0.03", "acc_trade_price_24h": "6000000000"},
            {"market": "KRW-WIDE", "trade_price": "1000", "signed_change_rate": "0.05", "acc_trade_price_24h": "5000000000"},
            {"market": "KRW-THIN", "trade_price": "500", "signed_change_rate": "0.04", "acc_trade_price_24h": "4000000000"},
        ]

    def get_orderbook(self, market):
        if market == "KRW-GOOD":
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
        self.assertEqual([item["market"] for item in result], ["KRW-GOOD"])

    def test_wide_spread_and_thin_depth_are_filtered_out(self):
        result = MarketScreener(FakeAPI(), min_trade_value_krw=1).scan_markets(top_count=3)
        selected_markets = [item["market"] for item in result]
        self.assertIn("KRW-GOOD", selected_markets)
        self.assertNotIn("KRW-WIDE", selected_markets)
        self.assertNotIn("KRW-THIN", selected_markets)

    def test_momentum_breakout_candidate_is_selected_only_when_enabled(self):
        """모멘텀 돌파 후보는 명시적으로 활성화한 경우에만 확인형 후보와 분리되어 반환된다."""
        class EarlyBreakoutAPI(FakeAPI):
            def get_all_markets(self):
                return [{"market": "KRW-BTC"}, {"market": "KRW-CONF"}, {"market": "KRW-EARLY"}]

            def get_tickers(self, markets):
                return [
                    {"market": "KRW-BTC", "trade_price": "100000", "signed_change_rate": "-0.005", "acc_trade_price_24h": "0"},
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
        self.assertEqual(early["candidate_type"], "MOMENTUM_BREAKOUT")
        self.assertEqual(early["momentum_phase"], "EARLY")

    def test_extended_momentum_is_tagged_for_runtime_chase_block(self):
        """6% 이하 후보는 EARLY로 분류되고, 6% 초과 확장 후보는 EXTENDED 단계로 전달된다."""
        class ExtendedMomentumAPI(FakeAPI):
            def get_all_markets(self):
                return [{"market": "KRW-BTC"}, {"market": "KRW-EARLY5"}, {"market": "KRW-EXT"}]

            def get_tickers(self, markets):
                return [
                    {"market": "KRW-BTC", "trade_price": "100000", "signed_change_rate": "0.0", "acc_trade_price_24h": "0"},
                    {"market": "KRW-EARLY5", "trade_price": "1000", "signed_change_rate": "0.05", "acc_trade_price_24h": "5000000000"},
                    {"market": "KRW-EXT", "trade_price": "1000", "signed_change_rate": "0.07", "acc_trade_price_24h": "5000000000"},
                ]

            def get_orderbook(self, market):
                return {"orderbook_units": [{"ask_price": 1001.0, "bid_price": 1000.0, "bid_size": 30000.0}]}

        selected = MarketScreener(
            ExtendedMomentumAPI(), min_trade_value_krw=1, min_change_rate=0.01,
            enable_early_breakout=True,
        ).scan_markets(top_count=2)
        early5 = next(item for item in selected if item["market"] == "KRW-EARLY5")
        self.assertEqual(early5["candidate_type"], "MOMENTUM_BREAKOUT")
        self.assertEqual(early5["momentum_phase"], "EARLY")

        extended = next(item for item in selected if item["market"] == "KRW-EXT")
        self.assertEqual(extended["candidate_type"], "MOMENTUM_BREAKOUT")
        self.assertEqual(extended["momentum_phase"], "EXTENDED")

    def test_scan_swing_markets_uses_get_tickers_and_filters_candidates(self):
        """scan_swing_markets는 get_ticker가 없는 get_tickers 전용 API에서도 정상 동작해야 한다."""
        class SwingOnlyAPI:
            def get_all_markets(self, is_details=False):
                return [
                    {"market": "KRW-BTC"},
                    {"market": "KRW-SWING1"},
                    {"market": "KRW-SWING2"},
                    {"market": "KRW-WARN", "market_event": {"warning": True}},
                ]

            def get_tickers(self, markets):
                data = {
                    "KRW-BTC": {"market": "KRW-BTC", "trade_price": "100000000", "signed_change_rate": "0.01", "acc_trade_price_24h": "500000000000"},
                    "KRW-SWING1": {"market": "KRW-SWING1", "trade_price": "5000", "signed_change_rate": "0.05", "acc_trade_price_24h": "50000000000"},
                    "KRW-SWING2": {"market": "KRW-SWING2", "trade_price": "1000", "signed_change_rate": "0.03", "acc_trade_price_24h": "40000000000"},
                    "KRW-WARN": {"market": "KRW-WARN", "trade_price": "2000", "signed_change_rate": "0.10", "acc_trade_price_24h": "100000000000"},
                }
                return [data[m] for m in markets if m in data]

        api = SwingOnlyAPI()
        # get_ticker attribute should not exist
        self.assertFalse(hasattr(api, "get_ticker"))

        screener = MarketScreener(api)

        # BTC CRASH 레짐에서는 즉시 빈 목록 반환
        crash_res = screener.scan_swing_markets(btc_regime="CRASH")
        self.assertEqual(crash_res, [])

        # NORMAL 레짐에서 스윙 후보 추출
        normal_res = screener.scan_swing_markets(top_count=2, btc_regime="NORMAL")
        self.assertTrue(len(normal_res) > 0)
        markets = [item["market"] for item in normal_res]
        self.assertIn("KRW-SWING1", markets)
        self.assertNotIn("KRW-WARN", markets)
        self.assertEqual(normal_res[0]["strategy_mode"], "SWING")


if __name__ == "__main__":
    unittest.main()
