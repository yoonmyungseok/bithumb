import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from strategy_engine import calculate_chandelier_exit, calculate_macd, entry_signal


class StrategyEngineTests(unittest.TestCase):
    def test_rejects_insufficient_history(self):
        self.assertFalse(entry_signal([])["allow_buy"])

    def test_returns_risk_levels_for_complete_history(self):
        candles = []
        for index in range(30):
            price = 100 + ((index % 4) - 1) * 0.5
            candles.append({"trade_price": price, "high_price": price + 1, "low_price": price - 1})
        signal = entry_signal(candles)
        self.assertGreater(signal["target_price"], signal["entry_price"])
        self.assertLess(signal["stop_loss"], signal["entry_price"])
        self.assertIn("RSI", signal["reason"])
        self.assertIn("risk_reward_ratio", signal)
        self.assertGreaterEqual(signal["risk_reward_ratio"], 1.0)

    def test_calculate_macd_standard(self):
        # 50 prices in steady uptrend
        prices = [100.0 + i for i in range(50)]
        # prices are newest-first
        prices_newest_first = prices[::-1]
        macd = calculate_macd(prices_newest_first, 12, 26, 9)
        self.assertIn("macd", macd)
        self.assertIn("signal", macd)
        self.assertIn("hist", macd)
        self.assertEqual(macd["trend"], "BULLISH")

    def test_chandelier_exit(self):
        candles = [{"high_price": 105.0, "low_price": 95.0, "trade_price": 100.0} for _ in range(20)]
        ch_stop = calculate_chandelier_exit(candles, period=14, multiplier=1.5)
        self.assertLess(ch_stop, 105.0)

    def test_btc_regime_rejection(self):
        candles = [{"trade_price": 100.0, "high_price": 101.0, "low_price": 99.0} for _ in range(30)]
        signal = entry_signal(candles, btc_regime="CRASH")
        self.assertFalse(signal["allow_buy"])
        self.assertIn("레짐 경보", signal["reason"])


if __name__ == "__main__":
    unittest.main()
