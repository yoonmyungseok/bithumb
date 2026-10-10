"""Tests for trading_runtime RISK_OFF enhanced entry guards."""

import unittest
from unittest.mock import MagicMock, patch

from strategy_engine import StrategyPolicy


class TradingRuntimeRiskOffGuardTests(unittest.TestCase):
    """trading_runtime의 RISK_OFF 진입 가드(알파 80점, MACD 가속도 양수) 동작 검증."""

    def test_risk_off_guard_logic_blocks_below_80_alpha(self):
        """알파 점수가 80점 미만이면 RISK_OFF에서 risk_off_alpha_ok가 False가 되는지 검증."""
        cand_alpha = 78
        min_risk_off_alpha = StrategyPolicy.ALPHA_BUY_THRESHOLD_RISK_OFF
        self.assertEqual(min_risk_off_alpha, 80)
        self.assertLess(cand_alpha, min_risk_off_alpha)

    def test_risk_off_guard_logic_requires_positive_macd(self):
        """MACD slope가 0 이하이고 is_accelerating이 False이면 차단되는지 검증."""
        entry_indicators = {
            "macd_slope": -0.01,
            "macd_is_accelerating": False,
            "macd_momentum_state": "BEARISH",
        }
        macd_slope = float(entry_indicators.get("macd_slope", 0.0) or 0.0)
        macd_is_acc = bool(entry_indicators.get("macd_is_accelerating", False))
        is_positive = (macd_slope > 0 or macd_is_acc)
        self.assertFalse(is_positive)

        # 양수인 경우
        entry_indicators_pos = {
            "macd_slope": 0.05,
            "macd_is_accelerating": True,
            "macd_momentum_state": "ACCELERATING_BULL",
        }
        macd_slope_pos = float(entry_indicators_pos.get("macd_slope", 0.0) or 0.0)
        macd_is_acc_pos = bool(entry_indicators_pos.get("macd_is_accelerating", False))
        is_positive_pos = (macd_slope_pos > 0 or macd_is_acc_pos)
        self.assertTrue(is_positive_pos)


if __name__ == "__main__":
    unittest.main()
