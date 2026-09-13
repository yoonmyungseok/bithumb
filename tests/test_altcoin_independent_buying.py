import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from strategy_engine import (
    StrategyPolicy,
    entry_signal,
    get_alpha_buy_threshold,
    get_momentum_breakout_alpha_threshold,
)
from market_screener import MarketScreener


class TestAltcoinIndependentBuying(unittest.TestCase):
    def test_alpha_buy_threshold_risk_off_equals_normal(self):
        """알트코인 독립 매수에 따라 RISK_OFF 레짐의 알파 임계치가 NORMAL(60점)과 동일해야 한다."""
        self.assertEqual(get_alpha_buy_threshold("RISK_OFF", is_night=False), 60)
        self.assertEqual(get_alpha_buy_threshold("NORMAL", is_night=False), 60)
        self.assertEqual(StrategyPolicy.ALPHA_BUY_THRESHOLD_RISK_OFF, 60)
        self.assertEqual(StrategyPolicy.RISK_OFF_ALLOC_RATIO, 1.0)

    def test_momentum_alpha_threshold_risk_off_equals_normal(self):
        """모멘텀 돌파 알파 임계치는 RISK_OFF에서 일반 70점, RS 주도주는 65점으로 우대되어야 한다."""
        self.assertEqual(get_momentum_breakout_alpha_threshold("RISK_OFF", is_night=False, relative_strength=0.01), 70)
        self.assertEqual(get_momentum_breakout_alpha_threshold("RISK_OFF", is_night=False, relative_strength=0.04), 65)

    @patch("strategy_engine.calculate_composite_alpha_score")
    def test_entry_signal_allows_buy_in_risk_off_with_alpha_60(self, mock_alpha):
        """BTC 레짐이 RISK_OFF이더라도 독자 강세(RS >= 2%) 알트코인은 알파 60점 이상이면 매수가 승인되어야 한다."""
        candles_5m = [
            {"trade_price": 100.0, "opening_price": 99.5, "high_price": 100.2, "low_price": 99.4, "candle_acc_trade_volume": 5000.0}
            for _ in range(30)
        ]
        candles_1h = [
            {"trade_price": 100.0, "opening_price": 99.0, "high_price": 101.0, "low_price": 98.0, "candle_acc_trade_volume": 50000.0}
            for _ in range(50)
        ]
        mock_alpha.return_value = {
            "total_score": 65,
            "rsi": 52.0,
            "pct_b": 0.50,
            "ma5": 100.0,
            "ma20": 99.8,
            "ema20_1h": 100.5,
            "factor_breakdown": {},
        }
        res = entry_signal(
            candles_5m,
            candles_1h=candles_1h,
            btc_regime="RISK_OFF",
            orderbook={"orderbook_units": [{"ask_price": 100.1, "bid_price": 100.0, "bid_size": 1000}]},
            market="KRW-ALT",
            is_night=False,
            relative_strength=0.025,
        )
        self.assertTrue(res["allow_buy"], f"RISK_OFF 매수 허용 실패: {res.get('reason')}")

    def test_entry_signal_blocks_on_btc_crash(self):
        """BTC 레짐이 CRASH인 경우 알트코인 매수는 fail-closed로 전면 차단되어야 한다."""
        candles_5m = [
            {"trade_price": 100.0, "opening_price": 99.5, "high_price": 100.2, "low_price": 99.4, "candle_acc_trade_volume": 5000.0}
            for _ in range(30)
        ]
        candles_1h = [
            {"trade_price": 100.0, "opening_price": 99.0, "high_price": 101.0, "low_price": 98.0, "candle_acc_trade_volume": 50000.0}
            for _ in range(50)
        ]
        res = entry_signal(
            candles_5m,
            candles_1h=candles_1h,
            btc_regime="CRASH",
            orderbook={"orderbook_units": [{"ask_price": 100.1, "bid_price": 100.0, "bid_size": 1000}]},
            market="KRW-ALT",
            is_night=False,
        )
        self.assertFalse(res["allow_buy"])
        self.assertIn("CRASH", res["reason"])

    def test_market_screener_falls_back_even_in_risk_off(self):
        """스크리너는 RISK_OFF 상태에서도 알트코인 후보가 부족하면 fallback 종목을 발굴해야 한다."""
        class MockAPI:
            def get_all_markets(self, is_details=False):
                return [{"market": "KRW-BTC"}, {"market": "KRW-ALT1"}, {"market": "KRW-ALT2"}]

            def get_tickers(self, markets):
                return [
                    {"market": "KRW-BTC", "trade_price": 100000000, "signed_change_rate": -0.01, "acc_trade_price_24h": 500000000000},
                    {"market": "KRW-ALT1", "trade_price": 500, "signed_change_rate": -0.005, "acc_trade_price_24h": 5000000000},
                    {"market": "KRW-ALT2", "trade_price": 200, "signed_change_rate": -0.010, "acc_trade_price_24h": 3000000000},
                ]

            def get_orderbook(self, market):
                return {
                    "orderbook_units": [{"ask_price": 501.0, "bid_price": 500.0, "bid_size": 100000.0}]
                }

            def get_orderbooks(self, markets):
                return [
                    {"market": m, "orderbook_units": [{"ask_price": 501.0, "bid_price": 500.0, "bid_size": 100000.0}]}
                    for m in markets
                ]

        screener = MarketScreener(MockAPI(), min_trade_value_krw=100_000_000, min_change_rate=0.01)
        selected = screener.scan_markets(top_count=2, btc_regime="RISK_OFF")
        self.assertTrue(len(selected) >= 1, "RISK_OFF 상태에서도 fallback 알트코인 후보가 발굴되어야 함")


if __name__ == "__main__":
    unittest.main()
