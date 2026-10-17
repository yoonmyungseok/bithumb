"""
BTC 매크로 쇼크 V자 반등 조기 해제(MACRO_SHOCK_V_SHAPE_EARLY_RELEASE) 단위 테스트
"""

import os
import sys
import time
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from strategy_engine import StrategyPolicy, classify_btc_regime
from trading_orchestrator import TradingOrchestrator


class TestMacroShockVShapeRelease(unittest.TestCase):
    def setUp(self):
        StrategyPolicy._MACRO_SHOCK_STATE.clear()
        StrategyPolicy.ENABLE_V_SHAPE_SHOCK_RELEASE = True

    def tearDown(self):
        StrategyPolicy._MACRO_SHOCK_STATE.clear()
        StrategyPolicy.ENABLE_V_SHAPE_SHOCK_RELEASE = True

    def test_v_shape_early_release_success(self):
        """저점 대비 +1.5% 이상 반등 및 거래량 1.8배 이상 돌파 시 매크로 쇼크 조기 해제"""
        scope = "bithumb"
        StrategyPolicy.set_macro_shock(scope, time.time() + 1800.0, "BTC 15분 급락", trough_price=100.0)

        # 5분봉: 현재가 101.6 (저점 100 대비 +1.6% 반등)
        # 20이평 거래량 평균: 10.0, 현재 거래량: 20.0 (2.0배)
        candles_5m = [
            {"trade_price": 101.6, "low_price": 101.2, "candle_acc_trade_volume": 20.0},
            {"trade_price": 100.5, "low_price": 100.0, "candle_acc_trade_volume": 12.0},
        ]
        for _ in range(18):
            candles_5m.append({"trade_price": 100.0, "low_price": 100.0, "candle_acc_trade_volume": 10.0})

        released, details = StrategyPolicy.check_macro_shock_v_shape_release(candles_5m, scope=scope)
        self.assertTrue(released)
        self.assertGreaterEqual(details["rebound_pct"], 1.5)
        self.assertGreaterEqual(details["volume_ratio"], 1.8)

        # 쿨다운이 실제로 해제되었는지 확인
        is_shock, _, _ = StrategyPolicy.is_macro_shock_active(scope)
        self.assertFalse(is_shock)

    def test_v_shape_rejected_if_rebound_insufficient(self):
        """반등률이 +1.5% 미만인 경우 거래량이 많아도 해제되지 않음"""
        scope = "upbit"
        StrategyPolicy.set_macro_shock(scope, time.time() + 1800.0, "BTC 15분 급락", trough_price=100.0)

        # 현재가 100.8 (+0.8% 반등)
        candles_5m = [
            {"trade_price": 100.8, "low_price": 100.5, "candle_acc_trade_volume": 30.0},
        ]
        for _ in range(20):
            candles_5m.append({"trade_price": 100.0, "low_price": 100.0, "candle_acc_trade_volume": 10.0})

        released, _ = StrategyPolicy.check_macro_shock_v_shape_release(candles_5m, scope=scope)
        self.assertFalse(released)
        is_shock, _, _ = StrategyPolicy.is_macro_shock_active(scope)
        self.assertTrue(is_shock)

    def test_v_shape_rejected_if_volume_insufficient(self):
        """반등률이 +2.0%여도 거래량 비율이 1.8배 미만이면 해제되지 않음"""
        scope = "bithumb"
        StrategyPolicy.set_macro_shock(scope, time.time() + 1800.0, "BTC 15분 급락", trough_price=100.0)

        # 현재가 102.0 (+2.0% 반등), 하지만 거래량 12.0 (20이평 10.0 대비 1.2배)
        candles_5m = [
            {"trade_price": 102.0, "low_price": 101.0, "candle_acc_trade_volume": 12.0},
        ]
        for _ in range(20):
            candles_5m.append({"trade_price": 100.0, "low_price": 100.0, "candle_acc_trade_volume": 10.0})

        released, _ = StrategyPolicy.check_macro_shock_v_shape_release(candles_5m, scope=scope)
        self.assertFalse(released)
        is_shock, _, _ = StrategyPolicy.is_macro_shock_active(scope)
        self.assertTrue(is_shock)

    def test_classify_market_regime_orchestrator_early_release(self):
        """TradingOrchestrator의 classify_market_regime 사이클 중 V자 반등 발생 시 쿨다운 해제 및 NORMAL 회복"""
        from unittest.mock import patch
        logger = MagicMock()
        orchestrator = TradingOrchestrator(logger)

        scope = "bithumb"
        StrategyPolicy.set_macro_shock(scope, time.time() + 1800.0, "BTC 매크로 쇼크", trough_price=100.0)

        # V자 반등 캔들 세팅
        candles_5m = [
            {"trade_price": 101.8, "low_price": 101.5, "candle_acc_trade_volume": 25.0},
            {"trade_price": 100.5, "low_price": 100.0, "candle_acc_trade_volume": 15.0},
        ]
        for _ in range(20):
            candles_5m.append({"trade_price": 100.0, "low_price": 100.0, "candle_acc_trade_volume": 10.0})
        candles_1h = [{"trade_price": 101.8} for _ in range(25)]

        mock_ex = MagicMock()
        mock_ex.exchange_name = scope
        mock_ex.get_candles.side_effect = [candles_5m, candles_1h]

        with patch("market_intelligence.MarketIntelligenceService.get_latest_intelligence", return_value=None):
            is_crashing, regime, reason = orchestrator.classify_market_regime(
                mock_ex, interval_minutes=5, crash_threshold_pct=0.015,
            )

        self.assertFalse(is_crashing)
        self.assertEqual(regime, "NORMAL")
        self.assertIn("V자 반등 조기 해제", reason)
        is_shock, _, _ = StrategyPolicy.is_macro_shock_active(scope)
        self.assertFalse(is_shock)


if __name__ == "__main__":
    unittest.main()
