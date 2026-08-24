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

from strategy_engine import (
    calculate_atr,
    calculate_bollinger_bands,
    calculate_ema,
    calculate_macd,
    calculate_rsi,
    entry_signal,
)
from gemini_analyzer import GeminiAnalyzer


class RealtimeRiskAndIndicatorTests(unittest.TestCase):
    def test_indicator_consistency_between_engine_and_analyzer(self):
        prices = [100.0 + i * 0.5 for i in range(30)]
        
        # RSI consistency
        engine_rsi = calculate_rsi(prices, 14)
        analyzer_rsi = GeminiAnalyzer.calculate_rsi(prices, 14)
        self.assertEqual(engine_rsi, analyzer_rsi)

        # Bollinger Bands consistency
        engine_bb = calculate_bollinger_bands(prices, 20)
        analyzer_bb = GeminiAnalyzer.calculate_bollinger_bands(prices, 20)
        self.assertEqual(engine_bb, analyzer_bb)

        # EMA consistency
        engine_ema = calculate_ema(prices, 12)
        analyzer_ema = GeminiAnalyzer.calculate_ema(prices, 12)
        self.assertEqual(engine_ema, analyzer_ema)

        # MACD consistency
        engine_macd = calculate_macd(prices)
        analyzer = GeminiAnalyzer(api_key="dummy")
        analyzer_macd = analyzer.calculate_macd(prices)
        self.assertEqual(engine_macd, analyzer_macd)

    def test_entry_signal_output_keys(self):
        candles = []
        for i in range(30):
            p = 1000 + i * 2
            candles.append({
                "trade_price": p,
                "high_price": p + 5,
                "low_price": p - 5,
                "opening_price": p - 1,
            })
        signal = entry_signal(candles)
        self.assertIn("allow_buy", signal)
        self.assertIn("reason", signal)
        self.assertIn("entry_price", signal)
        self.assertIn("target_price", signal)
        self.assertIn("stop_loss", signal)
        self.assertIn("rsi", signal)
        self.assertIn("pct_b", signal)


if __name__ == "__main__":
    unittest.main()
