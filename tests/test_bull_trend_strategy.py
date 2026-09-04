import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from strategy_engine import (
    StrategyPolicy,
    classify_btc_regime,
    get_alpha_buy_threshold,
    get_momentum_breakout_alpha_threshold,
    entry_signal,
    is_major_market,
)
from market_screener import MarketScreener
from risk_manager import TrailingStopTracker


class TestBullTrendStrategy(unittest.TestCase):
    def test_classify_btc_regime_bull_trend(self):
        """1H EMA 정배열 및 상승세일 때 BULL_TREND로 정확히 분류되는지 검증"""
        # 5분봉: 최근 15분간 급락 없음
        candles_5m = [
            {"trade_price": 100000.0},
            {"trade_price": 99900.0},
            {"trade_price": 99800.0},
            {"trade_price": 99700.0},
        ]

        # 1시간봉: 30개 캔들, 가격이 과거 90,000원에서 100,000원으로 꾸준히 상승 (EMA20 > EMA50)
        candles_1h = []
        for i in range(30):
            # 최신봉(i=0)일수록 높은 가격
            price = 100000.0 - (i * 300.0)
            candles_1h.append({"trade_price": price})

        result = classify_btc_regime(candles_5m, candles_1h)
        self.assertEqual(result["regime"], "BULL_TREND")
        self.assertIn("BTC 강력 상승 추세", result["reason"])

    def test_alpha_thresholds_bull_trend(self):
        """BULL_TREND 레짐에서 알파 진입 점수가 적절히 유연하게 적용되는지 검증"""
        # 주간 BULL_TREND
        self.assertEqual(get_alpha_buy_threshold("BULL_TREND", is_night=False), 55)
        self.assertEqual(get_momentum_breakout_alpha_threshold("BULL_TREND", is_night=False), 50)

        # 심야 BULL_TREND
        self.assertEqual(get_alpha_buy_threshold("BULL_TREND", is_night=True), 65)
        self.assertEqual(get_momentum_breakout_alpha_threshold("BULL_TREND", is_night=True), 60)

    def test_entry_signal_bull_trend_dynamic_stops(self):
        """BULL_TREND 시 진입 신호에서 손절선(-3.2%) 및 목표가(+4.5%) 버퍼가 확장되는지 검증"""
        # 저점권 반등 캔들 생성
        chronological_prices = [97.0, 98.0, 97.0, 96.0, 97.0] * 5 + [95.0, 96.0, 96.3]
        candles = [
            {
                "trade_price": price,
                "opening_price": price - 0.5,
                "high_price": price + 1.0,
                "low_price": price - 1.0,
                "candle_acc_trade_volume": 100.0,
            }
            for price in reversed(chronological_prices)
        ]

        # NORMAL 레짐에서의 진입
        res_normal = entry_signal(candles, btc_regime="NORMAL", market="KRW-ALT")
        # BULL_TREND 레짐에서의 진입
        res_bull = entry_signal(candles, btc_regime="BULL_TREND", market="KRW-ALT")

        entry_p = res_bull["entry_price"]
        stop_p = res_bull["stop_loss"]
        target_p = res_bull["target_price"]

        # BULL_TREND 손절폭은 최소 -3.2% 반영
        self.assertLessEqual(stop_p, entry_p * (1.0 - StrategyPolicy.BULL_STOP_LOSS_PCT + 1e-4))
        # BULL_TREND 목표 수익률은 최소 +4.5% 반영
        self.assertGreaterEqual(target_p, entry_p * (1.0 + StrategyPolicy.BULL_PARTIAL_TP_1_PCT - 1e-4))

    def test_market_screener_includes_major_in_bull_trend(self):
        """BULL_TREND 레짐에서는 메이저 코인(BTC, ETH, SOL)이 스크리너 후보에 포함되는지 검증"""
        mock_api = MagicMock()
        mock_api.get_all_markets.return_value = [
            {"market": "KRW-BTC"},
            {"market": "KRW-ETH"},
            {"market": "KRW-SOL"},
            {"market": "KRW-ALT1"},
        ]
        mock_api.get_tickers.return_value = [
            {"market": "KRW-BTC", "trade_price": 100000000, "signed_change_rate": 0.05, "acc_trade_price_24h": 50000000000},
            {"market": "KRW-ETH", "trade_price": 3500000, "signed_change_rate": 0.04, "acc_trade_price_24h": 30000000000},
            {"market": "KRW-SOL", "trade_price": 150000, "signed_change_rate": 0.06, "acc_trade_price_24h": 20000000000},
            {"market": "KRW-ALT1", "trade_price": 500, "signed_change_rate": 0.04, "acc_trade_price_24h": 5000000000},
        ]
        mock_api.get_orderbook.return_value = {
            "orderbook_units": [{"ask_price": 100.1, "bid_price": 100.0, "bid_size": 500000.0}]
        }

        screener = MarketScreener(bithumb_api=mock_api, min_trade_value_krw=1000000000)

        # NORMAL 레짐: 메이저 코인은 단타 풀에서 제외됨
        candidates_normal = screener.scan_markets(top_count=5, held_markets=[], btc_regime="NORMAL")
        normal_markets = [c["market"] for c in candidates_normal]
        self.assertNotIn("KRW-BTC", normal_markets)

        # BULL_TREND 레짐: 메이저 코인(BTC, ETH, SOL)이 매매 후보로 포함됨
        candidates_bull = screener.scan_markets(top_count=5, held_markets=[], btc_regime="BULL_TREND")
        bull_markets = [c["market"] for c in candidates_bull]
        self.assertIn("KRW-BTC", bull_markets)
        self.assertIn("KRW-ETH", bull_markets)
        self.assertIn("KRW-SOL", bull_markets)

    def test_trailing_stop_tracker_bull_trend(self):
        """BULL_TREND 레짐에서 트레일링 스탑과 분할 익절이 대세 파동 추종형으로 동작하는지 검증"""
        tracker = TrailingStopTracker(data_dir="data/test_scratch")
        market = "KRW-TESTBULL"
        tracker.clear(market)

        buy_p = 100.0

        # +3.5% 상승 시: 일반 장에서는 1차 익절(+3.5%)이지만, BULL_TREND에서는 1차 익절이 +4.5%이므로 아직 홀딩
        action, _, _, _, _ = tracker.check_position(market, 103.5, buy_p, btc_regime="BULL_TREND")
        self.assertEqual(action, "NONE")

        # +4.6% 상승 시: BULL_TREND 1차 분할 익절 (+4.5% 통과)
        action, _, _, _, _ = tracker.check_position(market, 104.6, buy_p, btc_regime="BULL_TREND")
        self.assertEqual(action, "PARTIAL_TP_1")

        # +8.1% 상승 시: BULL_TREND 2차 분할 익절 (+8.0% 통과)
        action, _, _, _, _ = tracker.check_position(market, 108.1, buy_p, btc_regime="BULL_TREND")
        self.assertEqual(action, "PARTIAL_TP_2")

        # 최고점 110.0원 찍고 2.0% 하락(107.8원): 일반 장(2.0% 드롭)에서는 청산되지만, BULL_TREND(2.5% 드롭)에서는 홀딩
        tracker.check_position(market, 110.0, buy_p, btc_regime="BULL_TREND")
        action, _, _, _, _ = tracker.check_position(market, 107.8, buy_p, btc_regime="BULL_TREND")
        self.assertEqual(action, "NONE")

        # 최고점 110.0원에서 2.6% 하락(107.1원): BULL_TREND 2.5% 드롭 초과로 트레일링 스탑 청산
        action, _, _, _, _ = tracker.check_position(market, 107.1, buy_p, btc_regime="BULL_TREND")
        self.assertEqual(action, "TRAILING_STOP")

        tracker.clear(market)

    def test_time_stop_bull_trend_policy(self):
        """BULL_TREND 시 타임스탑이 6시간(21,600초) 및 최대 8시간(28,800초)으로 연장되는지 검증"""
        self.assertEqual(StrategyPolicy.BULL_TIME_STOP_SECONDS, 21600)
        self.assertEqual(StrategyPolicy.BULL_TIME_STOP_MAX_HOLD_SECONDS, 28800)
        self.assertEqual(StrategyPolicy.BULL_STOP_LOSS_PCT, 0.032)
        self.assertEqual(StrategyPolicy.BULL_PARTIAL_TP_1_PCT, 0.045)
        self.assertEqual(StrategyPolicy.BULL_PARTIAL_TP_2_PCT, 0.080)


if __name__ == "__main__":
    unittest.main()
