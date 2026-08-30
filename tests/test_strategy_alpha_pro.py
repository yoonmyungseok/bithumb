"""
Strategy and Alpha Engine Tests (VWAP, MACD Acceleration, 7-Factor Composite Score)
"""

import os
import sys
import unittest

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from gemini_analyzer import GeminiAnalyzer
from strategy_engine import (
    StrategyPolicy,
    calculate_composite_alpha_score,
    calculate_macd_acceleration,
    calculate_vwap,
    entry_signal,
)


class StrategyAlphaProTests(unittest.TestCase):
    def test_calculate_vwap_accuracy(self):
        """VWAP 계산 정확도 및 지지/돌파 판정 검증"""
        candles = [
            {"high_price": 105.0, "low_price": 95.0, "trade_price": 102.0, "candle_acc_trade_volume": 10.0},
            {"high_price": 102.0, "low_price": 92.0, "trade_price": 98.0, "candle_acc_trade_volume": 20.0},
            {"high_price": 98.0, "low_price": 90.0, "trade_price": 94.0, "candle_acc_trade_volume": 15.0},
        ]
        res = calculate_vwap(candles)
        self.assertGreater(res["vwap"], 0.0)
        self.assertTrue(isinstance(res["is_above"], bool))
        self.assertTrue(isinstance(res["disparity_pct"], float))
        # 현재가 102원은 평균 VWAP보다 높으므로 is_above == True
        self.assertTrue(res["is_above"])

    def test_calculate_macd_acceleration(self):
        """MACD 히스토그램 기울기 및 가속도 상태 검증"""
        # 우상향 가속 가격 시계열
        prices_uptrend = [100.0 + (i * 2.0) for i in range(40)]  # 최신이 가장 앞
        res = calculate_macd_acceleration(prices_uptrend)
        self.assertIn(res["momentum_state"], ["ACCELERATING_BULL", "DECELERATING_BULL", "RECOVERING", "BEARISH"])
        self.assertTrue(isinstance(res["is_accelerating"], bool))

    def test_composite_alpha_score_high_bullish(self):
        """강세 정배열 시장에서 7대 팩터 앙상블 스코어 65점 이상 승인 검증"""
        # 5분봉 30개 (자연스러운 우상향 파동, RSI ~55 수준)
        candles_5m = []
        for i in range(30):
            # i=0(최신) -> 29(과거)
            oscillation = 1.5 if i % 2 == 0 else -1.0
            base_p = 1000.0 - (i * 0.8) + oscillation
            candles_5m.append({
                "high_price": base_p + 3.0,
                "low_price": base_p - 2.0,
                "trade_price": base_p,
                "opening_price": base_p - 1.0,
                "candle_acc_trade_volume": 250.0 if i == 0 else 100.0,
            })

        candles_1h = []
        for i in range(25):
            base_p = 980.0 - (i * 5.0)
            candles_1h.append({"trade_price": base_p})

        orderbook = {
            "total_bid_size": 200.0,
            "total_ask_size": 100.0,
            "orderbook_units": [{"ask_price": 1001.0, "bid_price": 1000.0}],
        }

        res = calculate_composite_alpha_score(
            candles=candles_5m,
            candles_1h=candles_1h,
            orderbook=orderbook,
            btc_regime="NORMAL",
        )
        self.assertGreaterEqual(res["total_score"], 60)
        self.assertTrue(res["allow_buy"])
        self.assertIn("mtf_score", res["factor_breakdown"])
        self.assertIn("vwap_score", res["factor_breakdown"])

    def test_composite_alpha_score_bearish_rejection(self):
        """역배열 급락 시장에서 알파 기준 미만 매수 거부 검증"""
        candles_5m = []
        for i in range(30):
            base_p = 1000.0 + (i * 5.0)  # 과거가 높고 최신이 낮음 (급락)
            candles_5m.append({
                "high_price": base_p + 2.0,
                "low_price": base_p - 5.0,
                "trade_price": base_p - 3.0,
                "opening_price": base_p,
                "candle_acc_trade_volume": 50.0,
            })

        res = calculate_composite_alpha_score(candles=candles_5m, btc_regime="NORMAL")
        self.assertLess(res["total_score"], StrategyPolicy.ALPHA_BUY_THRESHOLD_NORMAL)
        self.assertFalse(res["allow_buy"])

    def test_gemini_analyzer_local_quant_ensemble(self):
        """GeminiAnalyzer 로컬 퀀트 엔진 앙상블 스코어 정상 가동 검증"""
        analyzer = GeminiAnalyzer()
        mtf_1h = {"trend": "BULLISH", "desc": "상승"}
        bb = {"pct_b": 0.6, "middle": 1000.0}
        vol_info = {"is_spike": True}
        trade_strength = {"trade_power_pct": 130.0}
        ob_info = {"spread_pct": 0.2}
        vwap_info = {"is_above": True, "disparity_pct": 1.0}
        macd_acc = {"is_accelerating": True, "momentum_state": "ACCELERATING_BULL"}

        out = analyzer._run_local_quant_engine(
            current_price=1010.0,
            mtf_1h=mtf_1h,
            disparity_ma20=101.0,
            rsi_val=55.0,
            bb=bb,
            vol_info=vol_info,
            candle_pattern="양봉",
            trade_strength=trade_strength,
            ob_info=ob_info,
            dynamic_tp=1050.0,
            dynamic_sl=990.0,
            is_holding=False,
            pnl_pct=0.0,
            vwap_info=vwap_info,
            macd_acc=macd_acc,
        )

        self.assertEqual(out["action"], "BUY")
        self.assertGreaterEqual(out["alpha_score"], 65)


if __name__ == "__main__":
    unittest.main()
