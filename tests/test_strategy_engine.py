import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from strategy_engine import entry_signal


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


if __name__ == "__main__":
    unittest.main()
