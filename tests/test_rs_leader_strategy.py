import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from strategy_engine import (
    StrategyPolicy,
    entry_signal,
    get_momentum_breakout_alpha_threshold,
    is_rs_leader,
)


class RSLeaderStrategyTests(unittest.TestCase):
    def test_is_rs_leader_qualification(self):
        """RS 주도주 자격 검증 (RS >= 3% 및 BTC CRASH 배제)"""
        # RS 부족
        self.assertFalse(is_rs_leader(relative_strength=0.015, btc_regime="NORMAL"))
        self.assertFalse(is_rs_leader(relative_strength=0.029, btc_regime="NORMAL"))

        # RS 충족 & 정상장/약세장
        self.assertTrue(is_rs_leader(relative_strength=0.030, btc_regime="NORMAL"))
        self.assertTrue(is_rs_leader(relative_strength=0.080, btc_regime="RISK_OFF"))

        # BTC CRASH 또는 BEAR_VOLATILE 시에는 주도주라도 전면 차단
        self.assertFalse(is_rs_leader(relative_strength=0.080, btc_regime="CRASH"))
        self.assertFalse(is_rs_leader(relative_strength=0.080, btc_regime="BEAR_VOLATILE"))

    def test_early_max_change_rate_dynamic_scaling(self):
        """RS 주도주에 대한 모멘텀 초입(EARLY) 상한 확장 검증 (+6% -> +12%)"""
        # 일반 종목
        self.assertEqual(StrategyPolicy.get_momentum_early_max_change_rate(relative_strength=0.010), 0.060)
        # RS 주도주
        self.assertEqual(StrategyPolicy.get_momentum_early_max_change_rate(relative_strength=0.035), 0.120)

    def test_momentum_breakout_alpha_threshold_relaxation(self):
        """RISK_OFF 레짐에서 RS 주도주 알파 기준 완화 검증 (70점 -> 65점)"""
        # 주간 RISK_OFF: 일반 70점 vs RS 주도주 65점
        threshold_normal = get_momentum_breakout_alpha_threshold("RISK_OFF", is_night=False, relative_strength=0.01)
        threshold_leader = get_momentum_breakout_alpha_threshold("RISK_OFF", is_night=False, relative_strength=0.04)
        self.assertEqual(threshold_normal, 70)
        self.assertEqual(threshold_leader, 65)

        # 심야 RISK_OFF: 일반 75점 vs RS 주도주 70점
        threshold_night_normal = get_momentum_breakout_alpha_threshold("RISK_OFF", is_night=True, relative_strength=0.01)
        threshold_night_leader = get_momentum_breakout_alpha_threshold("RISK_OFF", is_night=True, relative_strength=0.04)
        self.assertEqual(threshold_night_normal, 75)
        self.assertEqual(threshold_night_leader, 70)

    def test_momentum_breakout_near_high_tolerance(self):
        """직전 4봉 고점 대비 -0.8% 이내 근접 시 RS 주도주 돌파 셋업 통과 검증"""
        # RSI 60~65 수준을 유지하는 완만한 우상향 가격 패턴
        prices = [
            100.0, 99.0, 99.5, 98.5, 99.0, 98.0, 98.5, 97.5, 98.0, 97.0,
            97.5, 96.5, 97.0, 96.0, 96.5, 95.5, 96.0, 95.0, 95.5, 94.5,
            95.0, 94.0, 94.5, 93.5, 94.0, 93.0, 93.5, 92.5, 93.0, 92.0,
        ]
        candles = [
            {
                "trade_price": p,
                "high_price": p + 0.5,
                "low_price": p - 0.5,
                "opening_price": p - 0.2,
                "candle_acc_trade_volume": 1000.0,
            }
            for p in prices
        ]

        # 직전 1~4봉의 최고가
        p_high = max(c["high_price"] for c in candles[1:5])
        # 최신 0번 봉: 직전 최고가의 99.5%로 99.2% 이상 근접 지지 양봉
        candles[0] = {
            "trade_price": p_high * 0.995,
            "high_price": p_high * 0.996,
            "low_price": p_high * 0.990,
            "opening_price": p_high * 0.991,
            "candle_acc_trade_volume": 2000.0,
        }

        signal_normal = entry_signal(
            candles=candles,
            btc_regime="NORMAL",
            entry_type="MOMENTUM_BREAKOUT",
            relative_strength=0.01,
        )
        self.assertFalse(signal_normal["momentum_breakout_passed"])
        self.assertFalse(signal_normal["is_rs_leader"])
        self.assertIn("고점 돌파=차단", signal_normal["reason"])

        signal_leader = entry_signal(
            candles=candles,
            btc_regime="NORMAL",
            entry_type="MOMENTUM_BREAKOUT",
            relative_strength=0.04,
        )
        self.assertTrue(signal_leader["momentum_breakout_passed"])
        self.assertTrue(signal_leader["is_rs_leader"])
        self.assertIn("고점 돌파=통과 [RS 주도주 특례]", signal_leader["reason"])

    def test_btc_crash_fail_closed_even_for_rs_leader(self):
        """BTC CRASH 시에는 아무리 강한 RS 주도주라도 fail-closed 차단 유지 검증"""
        candles = []
        for i in range(30):
            candles.append({
                "trade_price": 100.0 + i,
                "high_price": 101.0 + i,
                "low_price": 99.0 + i,
                "opening_price": 100.0 + i,
                "candle_acc_trade_volume": 1000.0,
            })
        signal = entry_signal(
            candles=candles,
            btc_regime="CRASH",
            entry_type="MOMENTUM_BREAKOUT",
            relative_strength=0.15,
        )
        self.assertFalse(signal["allow_buy"])
        self.assertIn("레짐 경보", signal["reason"])


if __name__ == "__main__":
    unittest.main()
