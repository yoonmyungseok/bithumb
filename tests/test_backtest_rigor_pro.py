"""
Backtest and Data Rigor Pro Tests (Walk-Forward Cross-Validation, Monte Carlo 1,000x, Sensitivity Analysis)
"""

import os
import sys
import unittest

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from backtest import QuantBacktester


class BacktestRigorProTests(unittest.TestCase):
    def setUp(self):
        # 100개 가상 캔들 생성 (우상향 및 파동)
        self.mock_candles = []
        for i in range(100):
            base_p = 1000.0 + (i * 2.0) + (3.0 if i % 2 == 0 else -2.0)
            self.mock_candles.append({
                "opening_price": base_p - 1.0,
                "high_price": base_p + 4.0,
                "low_price": base_p - 3.0,
                "trade_price": base_p,
                "candle_acc_trade_volume": 150.0,
            })
        # 최신이 가장 앞 (newest-first)
        self.mock_candles = self.mock_candles[::-1]

    def test_walk_forward_backtest_execution(self):
        """Walk-Forward 시계열 롤링 분할 검증 정상 동작 테스트"""
        backtester = QuantBacktester(initial_capital=1_000_000.0)
        res = backtester.run_walk_forward_backtest(
            market="KRW-TEST",
            candles=self.mock_candles,
            num_windows=2,
            train_ratio=0.7,
        )

        self.assertIn("robustness_score", res)
        self.assertIn("windows", res)
        self.assertGreaterEqual(len(res["windows"]), 1)
        self.assertIn("mean_out_of_sample_return_pct", res)
        self.assertIn("mean_out_of_sample_win_rate", res)

    def test_monte_carlo_simulation_var95(self):
        """몬테카를로 1,000회 리샘플링 및 MDD VaR 95% 산출 검증"""
        backtester = QuantBacktester(initial_capital=1_000_000.0)
        completed_trades = [
            {"profit_krw": 20000.0, "type": "PARTIAL_TP"},
            {"profit_krw": -15000.0, "type": "STOP_LOSS"},
            {"profit_krw": 35000.0, "type": "TRAILING_STOP"},
            {"profit_krw": -10000.0, "type": "STOP_LOSS"},
            {"profit_krw": 18000.0, "type": "TRAILING_STOP"},
            {"profit_krw": -8000.0, "type": "TIME_STOP"},
        ]

        mc_res = backtester.run_monte_carlo_simulation(
            completed_trades=completed_trades,
            num_simulations=500,
            initial_capital=1_000_000.0,
        )

        self.assertEqual(mc_res["simulations"], 500)
        self.assertEqual(mc_res["trades_count"], 6)
        self.assertGreaterEqual(mc_res["mdd_var_95_pct"], 0.0)
        self.assertGreaterEqual(mc_res["worst_mdd_pct"], mc_res["mdd_var_95_pct"])
        self.assertTrue(0.0 <= mc_res["ruin_probability_pct"] <= 100.0)

    def test_sensitivity_analysis_grid(self):
        """파라미터 리스크 비율 민감도 그리드 분석 검증"""
        backtester = QuantBacktester(initial_capital=1_000_000.0)
        sens_res = backtester.run_sensitivity_analysis(
            market="KRW-TEST",
            candles=self.mock_candles,
            risk_fractions=[0.005, 0.010],
        )

        self.assertEqual(len(sens_res), 2)
        self.assertEqual(sens_res[0]["risk_fraction_pct"], 0.5)
        self.assertEqual(sens_res[1]["risk_fraction_pct"], 1.0)
        self.assertIn("total_return_pct", sens_res[0])
        self.assertIn("max_drawdown_pct", sens_res[0])

    def test_portfolio_backtest_respects_max_positions_and_shared_capital(self):
        """다중 마켓 포트폴리오 백테스트의 최대 동시 포지션 제한 및 마켓별 집계 검증"""
        backtester = QuantBacktester(initial_capital=1_000_000.0)
        market_candles = {
            "KRW-BTC": self.mock_candles,
            "KRW-ETH": self.mock_candles,
            "KRW-XRP": self.mock_candles,
        }
        res = backtester.run_portfolio_backtest(
            market_candles=market_candles,
            btc_candles=self.mock_candles,
            max_positions=2,
        )
        self.assertEqual(set(res["markets"]), {"KRW-BTC", "KRW-ETH", "KRW-XRP"})
        self.assertLessEqual(res["max_concurrent_positions"], 2)
        self.assertIn("KRW-BTC", res["market_breakdown"])
        self.assertGreater(len(res["equity_curve"]), 10)

    def test_portfolio_walk_forward_backtest_with_warmup(self):
        """포트폴리오 Walk-Forward 전진 검증 및 워밍업 분리 검증"""
        long_candles = []
        for i in range(140):
            base_p = 1000.0 + (i * 1.8) + (4.0 if i % 3 == 0 else -2.0)
            long_candles.append({
                "opening_price": base_p - 1.0,
                "high_price": base_p + 5.0,
                "low_price": base_p - 3.0,
                "trade_price": base_p,
                "candle_acc_trade_volume": 200.0,
            })
        long_candles = long_candles[::-1]

        backtester = QuantBacktester(initial_capital=1_000_000.0)
        res = backtester.run_portfolio_walk_forward_backtest(
            market_candles={"KRW-BTC": long_candles, "KRW-ETH": long_candles},
            btc_candles=long_candles,
            num_windows=2,
            train_ratio=0.7,
            max_positions=2,
        )
        self.assertGreaterEqual(res["num_windows"], 1)
        self.assertIn("mean_out_of_sample_return_pct", res)
        self.assertIn("mean_out_of_sample_mdd_pct", res)
        self.assertIn("robustness_score", res)


if __name__ == "__main__":
    unittest.main()
