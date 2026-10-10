"""Tests for enhanced RISK_OFF entry gates: minimum alpha score 80 and positive MACD acceleration."""

import unittest
from unittest.mock import MagicMock, patch

from strategy_engine import (
    StrategyPolicy,
    calculate_composite_alpha_score,
    entry_signal,
    get_alpha_buy_threshold,
)


class RiskOffEnhancedGatesTests(unittest.TestCase):
    """RISK_OFF 약세장 진입 허들 상향(알파 80점, MACD 가속도 양수) 검증."""

    def test_risk_off_alpha_threshold_is_80(self):
        """RISK_OFF 레짐의 알파 임계값이 80점(심야 포함)으로 상향되었는지 검증."""
        self.assertEqual(StrategyPolicy.ALPHA_BUY_THRESHOLD_RISK_OFF, 80)
        self.assertEqual(StrategyPolicy.ALPHA_BUY_THRESHOLD_NIGHT_RISK_OFF, 80)

        # 주간 및 심야 모두 80점 이상 반환
        self.assertEqual(get_alpha_buy_threshold("RISK_OFF", is_night=False), 80)
        self.assertEqual(get_alpha_buy_threshold("RISK_OFF", is_night=True), 80)

    def test_risk_off_blocks_entry_when_alpha_score_is_78(self):
        """RISK_OFF 약세장에서 알파 점수가 78점(80점 미만)인 종목(MET형)은 진입이 차단된다."""
        candles = [
            {
                "trade_price": 100.0,
                "opening_price": 99.0,
                "high_price": 101.0,
                "low_price": 98.0,
                "candle_acc_trade_volume": 1000.0,
            }
            for _ in range(30)
        ]
        alpha_78 = {
            "total_score": 78,
            "allow_buy": False,
            "factor_breakdown": {
                "orderflow_score": 10,
                "macd_slope": 0.05,
                "macd_is_accelerating": True,
            },
        }

        with patch("strategy_engine.calculate_composite_alpha_score", return_value=alpha_78), \
             patch("strategy_engine.calculate_bollinger_bands", return_value={"middle": 100.0, "upper": 110.0, "lower": 90.0, "width_pct": 0.2, "pct_b": 0.50}), \
             patch("strategy_engine.calculate_rsi", return_value=50.0):
            res = entry_signal(candles, btc_regime="RISK_OFF", is_night=False)

        self.assertFalse(res["allow_buy"])
        self.assertEqual(res["checklist_details"]["alpha_threshold"], 80)
        self.assertEqual(res["alpha_score"], 78)

    def test_risk_off_blocks_entry_when_macd_acceleration_is_neutral_or_negative(self):
        """RISK_OFF 약세장에서 알파 점수가 85점으로 높아도 MACD 가속도(slope)가 0 이하(NEUTRAL/NEGATIVE)이면 차단된다."""
        candles = [
            {
                "trade_price": 100.0,
                "opening_price": 99.0,
                "high_price": 101.0,
                "low_price": 98.0,
                "candle_acc_trade_volume": 1000.0,
            }
            for _ in range(30)
        ]

        # 1. Slope <= 0 (음수/0)인 경우
        alpha_negative_macd = {
            "total_score": 85,
            "allow_buy": True,
            "factor_breakdown": {
                "orderflow_score": 10,
                "macd_slope": -0.02,
                "macd_is_accelerating": False,
                "macd_momentum_state": "DECELERATING_BULL",
            },
        }

        with patch("strategy_engine.calculate_composite_alpha_score", return_value=alpha_negative_macd), \
             patch("strategy_engine.calculate_bollinger_bands", return_value={"middle": 100.0, "upper": 110.0, "lower": 90.0, "width_pct": 0.2, "pct_b": 0.50}), \
             patch("strategy_engine.calculate_rsi", return_value=50.0):
            res = entry_signal(candles, btc_regime="RISK_OFF", is_night=False)

        self.assertFalse(res["allow_buy"])
        self.assertIn("MACD가속도 비양수차단", res["reason"])
        self.assertFalse(res["strategy_snapshot"]["indicators"]["hard_gate_macd_risk_off"])

    def test_risk_off_allows_entry_when_both_alpha_gte_80_and_macd_positive(self):
        """RISK_OFF 약세장에서 알파 점수 80점 이상 및 MACD 가속도 양수(slope > 0)를 모두 충족하면 하드게이트를 통과한다."""
        candles = [
            {
                "trade_price": 100.0,
                "opening_price": 99.0,
                "high_price": 101.0,
                "low_price": 98.0,
                "candle_acc_trade_volume": 1000.0,
            }
            for _ in range(30)
        ]

        alpha_valid = {
            "total_score": 82,
            "allow_buy": True,
            "factor_breakdown": {
                "orderflow_score": 10,
                "macd_slope": 0.03,
                "macd_is_accelerating": True,
                "macd_momentum_state": "ACCELERATING_BULL",
                "orderbook_smoothed_ratio": 1.2,
            },
        }

        with patch("strategy_engine.calculate_composite_alpha_score", return_value=alpha_valid), \
             patch("strategy_engine.calculate_bollinger_bands", return_value={"middle": 100.0, "upper": 110.0, "lower": 90.0, "width_pct": 0.2, "pct_b": 0.50}), \
             patch("strategy_engine.calculate_rsi", return_value=50.0):
            res = entry_signal(candles, btc_regime="RISK_OFF", is_night=False)

        self.assertTrue(res["allow_buy"])
        self.assertIn("MACD가속도 양수통과", res["reason"])
        self.assertTrue(res["strategy_snapshot"]["indicators"]["hard_gate_macd_risk_off"])


if __name__ == "__main__":
    unittest.main()
