import unittest
from src.strategy_engine import StrategyPolicy, calculate_relative_strength


class TestRelativeStrengthAndMomentumExit(unittest.TestCase):
    def test_calculate_relative_strength_outlier(self):
        # Asset rises +5%, BTC drops -2%
        candles_asset = [
            {"trade_price": 105.0},
            {"trade_price": 103.0},
            {"trade_price": 100.0},
        ]
        candles_btc = [
            {"trade_price": 98.0},
            {"trade_price": 99.0},
            {"trade_price": 100.0},
        ]
        rs_res = calculate_relative_strength(candles_asset, candles_btc, lookback_bars=2)
        self.assertGreater(rs_res["rs_pct"], 5.0)
        self.assertTrue(rs_res["is_outlier"])
        self.assertIn("독자 강세", rs_res["desc"])

    def test_calculate_relative_strength_correlated_drop(self):
        # Asset drops -3%, BTC drops -2%
        candles_asset = [
            {"trade_price": 97.0},
            {"trade_price": 99.0},
            {"trade_price": 100.0},
        ]
        candles_btc = [
            {"trade_price": 98.0},
            {"trade_price": 99.0},
            {"trade_price": 100.0},
        ]
        rs_res = calculate_relative_strength(candles_asset, candles_btc, lookback_bars=2)
        self.assertLess(rs_res["rs_pct"], 0.0)
        self.assertFalse(rs_res["is_outlier"])
        self.assertIn("언더퍼폼/약세", rs_res["desc"])

    def test_strategy_policy_ssot_constants(self):
        self.assertEqual(StrategyPolicy.PARTIAL_TP_1_RATIO, 0.50)
        self.assertEqual(StrategyPolicy.STOP_LOSS_PCT, 0.015)
        self.assertEqual(StrategyPolicy.MOMENTUM_EARLY_EXIT_SECONDS, 900)
        self.assertEqual(StrategyPolicy.RS_MIN_RISK_OFF, 0.015)


if __name__ == "__main__":
    unittest.main()
