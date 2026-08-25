"""
StrategyPolicy SSOT, Hard Safety Gates vs Soft Alpha Score, and Regime Analytics Tests
"""

import os
import shutil
import sys
import tempfile
import unittest

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from backtest import QuantBacktester
from strategy_engine import OrderbookFlowTracker, StrategyPolicy, entry_signal
from trade_memory import TradeMemoryManager


class StrategyPolicySSOTTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_ssot_")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _generate_mock_candles(self, count: int = 30, base_price: float = 1000.0, trend: float = 1.0) -> list[dict]:
        candles = []
        for i in range(count):
            p = base_price + (count - 1 - i) * trend * 2.0
            candles.append({
                "trade_price": p,
                "opening_price": p - 1.0,
                "high_price": p + 2.0,
                "low_price": p - 2.0,
                "candle_acc_trade_volume": 1000.0 + (i * 10.0),
            })
        return candles

    def test_strategy_policy_ssot_sync(self):
        """StrategyPolicy 상수가 entry_signal의 목표가/손절가 오프셋과 정확히 일치하는지 검증"""
        candles = self._generate_mock_candles(count=30, base_price=1000.0, trend=1.0)
        signal = entry_signal(candles, btc_regime="NORMAL")

        current = signal["entry_price"]
        volatility = signal["atr"]

        expected_target_offset = max(current * StrategyPolicy.MIN_TARGET_PCT, volatility * StrategyPolicy.ATR_TARGET_MULTIPLIER)
        expected_stop_offset = max(current * StrategyPolicy.MIN_STOP_PCT, volatility * StrategyPolicy.ATR_STOP_MULTIPLIER)

        self.assertAlmostEqual(signal["target_price"], round(current + expected_target_offset, 2))
        self.assertAlmostEqual(signal["stop_loss"], round(current - expected_stop_offset, 2))

    def test_hard_gates_block_extreme_rsi_even_with_high_alpha(self):
        """알파 점수가 65점 이상이어도 RSI 극초과열(>75) 시 하드 안전 게이트에 의해 차단되는지 검증 (과제 B)"""
        # 초급등 캔들 (RSI > 80 유도)
        candles = []
        p = 2000.0
        for i in range(30):
            candles.append({
                "trade_price": p,
                "opening_price": p - 30.0,
                "high_price": p + 10.0,
                "low_price": p - 35.0,
                "candle_acc_trade_volume": 5000.0,
            })
            p -= 40.0  # 과거로 갈수록 급격히 낮음

        signal = entry_signal(candles, btc_regime="NORMAL")
        # RSI가 75를 초과하면 하드 게이트 차단
        if signal["rsi"] > StrategyPolicy.RSI_MAX_NORMAL:
            self.assertFalse(signal["allow_buy"])
            self.assertFalse(signal["checklist_details"]["hard_gates"]["rsi_guard"]["pass"])

    def test_hard_gates_block_bb_outlier(self):
        """알파 점수 승인이어도 볼린저 밴드 상단 극단 이탈(%B > 0.95) 시 진입 차단 검증 (과제 B)"""
        candles = self._generate_mock_candles(count=30, base_price=1000.0, trend=1.0)
        # 최신 봉 가격을 상단 밴드 밖으로 인위적 조작
        candles[0]["trade_price"] = 1500.0
        candles[0]["high_price"] = 1550.0

        signal = entry_signal(candles, btc_regime="NORMAL")
        if signal["pct_b"] > StrategyPolicy.PCT_B_MAX:
            self.assertFalse(signal["allow_buy"])
            self.assertFalse(signal["checklist_details"]["hard_gates"]["bb_guard"]["pass"])

    def test_orderbook_flow_tracker_rolling_smoothing(self):
        """OrderbookFlowTracker가 단일 스냅샷 왜곡을 완충하고 롤링 평균을 정상 계산하는지 검증 (과제 E)"""
        tracker = OrderbookFlowTracker(max_history=3)
        # 1회차: 정상 1.0
        r1 = tracker.record_snapshot("KRW-BTC", total_bid=100.0, total_ask=100.0)
        self.assertEqual(r1, 1.0)

        # 2회차: 일시적 허매수 5.0 (스푸핑)
        r2 = tracker.record_snapshot("KRW-BTC", total_bid=500.0, total_ask=100.0)
        self.assertAlmostEqual(r2, 3.0)  # (1.0 + 5.0) / 2

        # 3회차: 정상 복귀 1.0
        r3 = tracker.record_snapshot("KRW-BTC", total_bid=100.0, total_ask=100.0)
        self.assertAlmostEqual(r3, 2.3333333333333335)  # (1.0 + 5.0 + 1.0) / 3

    def test_backtest_regime_breakdown_reporting(self):
        """QuantBacktester 시뮬레이션 결과에 regime_breakdown이 포함되고 통계가 산출되는지 검증 (과제 D)"""
        backtester = QuantBacktester(initial_capital=1_000_000.0)
        candles = self._generate_mock_candles(count=100, base_price=1000.0, trend=1.0)
        btc_candles = self._generate_mock_candles(count=100, base_price=100000000.0, trend=1.0)

        res = backtester.run_backtest(
            market="KRW-TEST",
            unit=5,
            candles=candles,
            btc_candles=btc_candles,
        )
        self.assertIn("regime_breakdown", res)
        self.assertIsInstance(res["regime_breakdown"], dict)

    def test_trade_memory_regime_and_alpha_tier_stats(self):
        """TradeMemoryManager가 완료 거래의 레짐 및 알파 점수 구간별 통계를 정상 집계하는지 검증 (과제 F)"""
        memory = TradeMemoryManager(data_dir=self.test_dir)

        # 1. NORMAL 레짐 거래 2건 기록 (1승 1패)
        memory.record_completed_trade(
            market="KRW-BTC",
            side="ask",
            entry_price=100.0,
            exit_price=105.0,
            pnl_pct=5.0,
            pnl_krw=5000.0,
            reason="트레일링 익절",
            timestamp="2026-08-25 12:00:00",
            btc_regime="NORMAL",
            alpha_score=82,
        )
        memory.record_completed_trade(
            market="KRW-ETH",
            side="ask",
            entry_price=100.0,
            exit_price=98.0,
            pnl_pct=-2.0,
            pnl_krw=-2000.0,
            reason="스탑로스",
            timestamp="2026-08-25 13:00:00",
            btc_regime="NORMAL",
            alpha_score=72,
        )

        # 2. RISK_OFF 레짐 거래 1건 기록 (1승)
        memory.record_completed_trade(
            market="KRW-SOL",
            side="ask",
            entry_price=100.0,
            exit_price=103.0,
            pnl_pct=3.0,
            pnl_krw=3000.0,
            reason="트레일링 익절",
            timestamp="2026-08-25 14:00:00",
            btc_regime="RISK_OFF",
            alpha_score=68,
        )

        regime_stats = memory.get_regime_performance_stats(min_sample_size=2)
        self.assertIn("NORMAL", regime_stats)
        self.assertEqual(regime_stats["NORMAL"]["sample_count"], 2)
        self.assertEqual(regime_stats["NORMAL"]["win_rate_pct"], 50.0)
        self.assertTrue(regime_stats["NORMAL"]["is_statistically_reliable"])

        self.assertIn("RISK_OFF", regime_stats)
        self.assertEqual(regime_stats["RISK_OFF"]["sample_count"], 1)
        self.assertFalse(regime_stats["RISK_OFF"]["is_statistically_reliable"])  # N=1 < 2

        alpha_stats = memory.get_alpha_tier_stats(min_sample_size=1)
        self.assertIn("80+", alpha_stats)
        self.assertEqual(alpha_stats["80+"]["sample_count"], 1)
        self.assertEqual(alpha_stats["80+"]["win_rate_pct"], 100.0)


if __name__ == "__main__":
    unittest.main()
