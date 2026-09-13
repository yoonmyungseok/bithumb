import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from strategy_engine import StrategyPolicy, entry_signal


class MomentumEntryEnhancementTests(unittest.TestCase):
    def setUp(self):
        self.candles = [
            {
                "trade_price": 100.0,
                "opening_price": 99.0,
                "high_price": 101.0,
                "low_price": 98.0,
                "candle_acc_trade_volume": 1000.0,
            }
            for _ in range(30)
        ]
        self.candles[0] = {
            "trade_price": 102.0,
            "opening_price": 100.0,
            "high_price": 103.0,
            "low_price": 99.5,
            "candle_acc_trade_volume": 1200.0,
        }
        self.candles_1h = [
            {"trade_price": 100.0, "high_price": 102.0, "low_price": 98.0, "opening_price": 99.0}
            for _ in range(25)
        ]

    def test_momentum_breakout_allows_higher_rsi_up_to_78(self):
        """기존 72 상한에서 차단되던 RSI 75 모멘텀 돌파 후보가 완화된 78 상한으로 정상 승인된다."""
        approved_alpha = {"total_score": 75, "allow_buy": True, "factor_breakdown": {}}
        with patch("strategy_engine.calculate_composite_alpha_score", return_value=approved_alpha), \
             patch("strategy_engine.calculate_rsi", return_value=75.0), \
             patch("strategy_engine.calculate_bollinger_bands", return_value={"middle": 100.0, "upper": 105.0, "lower": 95.0, "pct_b": 0.70, "width_pct": 0.1}):
            res = entry_signal(
                self.candles,
                candles_1h=self.candles_1h,
                btc_regime="NORMAL",
                entry_type="MOMENTUM_BREAKOUT",
                is_night=False,
            )
            self.assertTrue(res["allow_buy"])

    def test_momentum_breakout_allows_disparity_up_to_5_percent(self):
        """MA20 대비 +4.0% 이격(104원)인 급등 돌파 캔들도 모멘텀 전용 5% 이격도 내에서 승인된다."""
        candles_high_disparity = list(self.candles)
        candles_high_disparity[0] = {
            "trade_price": 104.0,
            "opening_price": 101.0,
            "high_price": 104.5,
            "low_price": 100.5,
            "candle_acc_trade_volume": 1500.0,
        }
        approved_alpha = {"total_score": 75, "allow_buy": True, "factor_breakdown": {}}
        with patch("strategy_engine.calculate_composite_alpha_score", return_value=approved_alpha), \
             patch("strategy_engine.calculate_rsi", return_value=68.0), \
             patch("strategy_engine.calculate_bollinger_bands", return_value={"middle": 100.0, "upper": 105.0, "lower": 95.0, "pct_b": 0.85, "width_pct": 0.1}):
            res = entry_signal(
                candles_high_disparity,
                candles_1h=self.candles_1h,
                btc_regime="NORMAL",
                entry_type="MOMENTUM_BREAKOUT",
                is_night=False,
            )
            self.assertTrue(res["allow_buy"])

    def test_strong_rs_leader_in_risk_off_relaxes_mtf_ratio(self):
        """BTC 약세(RISK_OFF) 환경에서도 알트코인은 독립 매수 원칙에 따라 1H MTF 0.980 기준을 적용받아 정상 진입 허용된다."""
        candles_1h_dip = list(self.candles_1h)
        candles_1h_dip[0] = {"trade_price": 98.5, "high_price": 100.0, "low_price": 97.0, "opening_price": 99.0}

        approved_alpha = {"total_score": 75, "allow_buy": True, "factor_breakdown": {}}
        with patch("strategy_engine.calculate_composite_alpha_score", return_value=approved_alpha), \
             patch("strategy_engine.calculate_rsi", return_value=58.0), \
             patch("strategy_engine.calculate_bollinger_bands", return_value={"middle": 100.0, "upper": 105.0, "lower": 95.0, "pct_b": 0.55, "width_pct": 0.1}), \
             patch("strategy_engine.calculate_ema", return_value=100.0):
            res_normal_rs = entry_signal(
                self.candles,
                candles_1h=candles_1h_dip,
                btc_regime="RISK_OFF",
                entry_type="CONFIRMED",
                is_night=False,
                relative_strength=0.005,
            )
            # 알트코인 독립 매수: 비트코인 추세와 무관하게 1H EMA20 0.980 지지 만족 시 일반 종목도 매수 승인
            self.assertTrue(res_normal_rs["allow_buy"])

            res_strong_rs = entry_signal(
                self.candles,
                candles_1h=candles_1h_dip,
                btc_regime="RISK_OFF",
                entry_type="CONFIRMED",
                is_night=False,
                relative_strength=0.025,
            )
            self.assertTrue(res_strong_rs["allow_buy"])


if __name__ == "__main__":
    unittest.main()
