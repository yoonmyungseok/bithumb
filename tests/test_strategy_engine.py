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

    def test_ma20_disparity_hard_gate(self):
        """MA20 대비 +3.5% 초과 이격 시 하드게이트 차단 검증"""
        # 29개 과거 캔들(100원 부근) + 최신 캔들(104원: 4% 초과 급등)
        candles = [{"trade_price": 100.0, "high_price": 100.5, "low_price": 99.5, "opening_price": 100.0} for _ in range(30)]
        candles[0] = {"trade_price": 104.0, "high_price": 104.2, "low_price": 103.5, "opening_price": 103.6}
        signal = entry_signal(candles, btc_regime="NORMAL")
        self.assertFalse(signal["allow_buy"])
        hard_gates = signal["checklist_details"]["hard_gates"]
        self.assertFalse(hard_gates["disparity_guard"]["pass"])

    def test_upper_shadow_hard_gate(self):
        """캔들 윗꼬리 비율 50% 초과 시 피뢰침 차단 검증"""
        candles = [{"trade_price": 100.0, "high_price": 100.5, "low_price": 99.5, "opening_price": 100.0} for _ in range(30)]
        # 최신 캔들: 저가 100, 시가 100.5, 종가 101, 고가 103 -> 전체 범위 3.0, 윗꼬리 (103 - 101) = 2.0 (66.7% > 40%)
        candles[0] = {"trade_price": 101.0, "high_price": 103.0, "low_price": 100.0, "opening_price": 100.5}
        signal = entry_signal(candles, btc_regime="NORMAL")
        self.assertFalse(signal["allow_buy"])
        hard_gates = signal["checklist_details"]["hard_gates"]
        self.assertFalse(hard_gates["shadow_guard"]["pass"])


if __name__ == "__main__":
    unittest.main()
