import os
import sys
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from strategy_engine import (
    StrategyPolicy,
    is_night_session,
    calculate_composite_alpha_score,
    entry_signal,
    get_alpha_buy_threshold,
    KST,
)
from market_screener import (
    MarketScreener,
    EXCLUDED_MAJOR_SCALPING_MARKETS,
    EXCLUDED_STABLE_MARKETS,
)


class TestNightSessionAndMajorFilter(unittest.TestCase):
    @staticmethod
    def _make_confirmed_pullback_candles():
        """하드 게이트를 통과하는 저점권 확정 반등 캔들을 만든다."""
        chronological_prices = [97.0, 98.0, 97.0, 96.0, 97.0] * 5 + [95.0, 96.0, 96.3]
        return [
            {
                "trade_price": price,
                "opening_price": price - 0.5,
                "high_price": price + 1.0,
                "low_price": price - 1.0,
                "candle_acc_trade_volume": 100.0,
            }
            for price in reversed(chronological_prices)
        ]

    def test_night_session_detection(self):
        """Test KST night session time detection (00:00 ~ 07:00)."""
        # 03:30 KST (Night)
        dt_night = datetime(2026, 9, 1, 3, 30, 0, tzinfo=KST)
        self.assertTrue(is_night_session(dt_night))

        # 00:00 KST (Night start)
        dt_night_start = datetime(2026, 9, 1, 0, 0, 0, tzinfo=KST)
        self.assertTrue(is_night_session(dt_night_start))

        # 06:59 KST (Night end boundary)
        dt_night_end = datetime(2026, 9, 1, 6, 59, 59, tzinfo=KST)
        self.assertTrue(is_night_session(dt_night_end))

        # 07:00 KST (Daytime)
        dt_day = datetime(2026, 9, 1, 7, 0, 0, tzinfo=KST)
        self.assertFalse(is_night_session(dt_day))

        # 14:00 KST (Daytime)
        dt_day2 = datetime(2026, 9, 1, 14, 0, 0, tzinfo=KST)
        self.assertFalse(is_night_session(dt_day2))

    def test_night_session_alpha_thresholds(self):
        """Test that alpha threshold is stricter during night session."""
        # Create synthetic candles
        candles = []
        base_price = 100.0
        for i in range(30):
            candles.append({
                "trade_price": base_price + (i * 0.1),
                "opening_price": base_price,
                "high_price": base_price + 1.0,
                "low_price": base_price - 0.5,
                "candle_acc_trade_volume": 1000.0,
            })
        candles_1h = [{"trade_price": base_price + 1.0} for _ in range(30)]

        # Alpha score evaluated during daytime
        res_day = calculate_composite_alpha_score(
            candles=candles,
            candles_1h=candles_1h,
            btc_regime="NORMAL",
            is_night=False,
        )

        # Alpha score evaluated during night
        res_night = calculate_composite_alpha_score(
            candles=candles,
            candles_1h=candles_1h,
            btc_regime="NORMAL",
            is_night=True,
        )

        self.assertEqual(res_day["total_score"], res_night["total_score"])
        self.assertFalse(res_day["factor_breakdown"]["is_night"])
        self.assertTrue(res_night["factor_breakdown"]["is_night"])

        # If score is 65, day allows buy (threshold 60), but night blocks (threshold 75)
        if 60 <= res_day["total_score"] < 75:
            self.assertTrue(res_day["allow_buy"])
            self.assertFalse(res_night["allow_buy"])

    def test_alpha_threshold_ssot_has_four_session_boundaries(self):
        """주간·심야와 NORMAL·RISK_OFF 조합은 단일 알파 기준을 반환해야 한다."""
        self.assertEqual(get_alpha_buy_threshold("NORMAL", is_night=False), 60)
        self.assertEqual(get_alpha_buy_threshold("RISK_OFF", is_night=False), 70)
        self.assertEqual(get_alpha_buy_threshold("NORMAL", is_night=True), 75)
        self.assertEqual(get_alpha_buy_threshold("RISK_OFF", is_night=True), 80)

    def test_entry_signal_enforces_night_alpha_boundaries(self):
        """최종 진입 게이트도 심야 74/75점과 79/80점 경계값을 그대로 적용해야 한다."""
        candles = self._make_confirmed_pullback_candles()

        for btc_regime, blocked_score, approved_score in (
            ("NORMAL", 74, 75),
            ("RISK_OFF", 79, 80),
        ):
            with self.subTest(btc_regime=btc_regime, alpha_score=blocked_score):
                with patch(
                    "strategy_engine.calculate_composite_alpha_score",
                    return_value={"total_score": blocked_score, "allow_buy": False, "factor_breakdown": {}},
                ):
                    blocked = entry_signal(candles, btc_regime=btc_regime, is_night=True)
                self.assertFalse(blocked["allow_buy"])
                self.assertEqual(blocked["checklist_details"]["alpha_threshold"], approved_score)

            with self.subTest(btc_regime=btc_regime, alpha_score=approved_score):
                with patch(
                    "strategy_engine.calculate_composite_alpha_score",
                    return_value={"total_score": approved_score, "allow_buy": True, "factor_breakdown": {}},
                ):
                    approved = entry_signal(candles, btc_regime=btc_regime, is_night=True)
                self.assertTrue(approved["allow_buy"])

    def test_market_screener_excludes_major_scalping(self):
        """Test that MarketScreener excludes BTC, ETH, SOL, XRP from new candidates unless held."""
        mock_api = MagicMock()
        mock_api.get_all_markets.return_value = [
            {"market": "KRW-BTC"},
            {"market": "KRW-ETH"},
            {"market": "KRW-SOL"},
            {"market": "KRW-XRP"},
            {"market": "KRW-ALT1"},
            {"market": "KRW-ALT2"},
        ]
        mock_api.get_tickers.return_value = [
            {"market": "KRW-BTC", "trade_price": 100000000, "signed_change_rate": 0.03, "acc_trade_price_24h": 50000000000},
            {"market": "KRW-ETH", "trade_price": 3500000, "signed_change_rate": 0.04, "acc_trade_price_24h": 30000000000},
            {"market": "KRW-SOL", "trade_price": 150000, "signed_change_rate": 0.05, "acc_trade_price_24h": 20000000000},
            {"market": "KRW-XRP", "trade_price": 2000, "signed_change_rate": 0.03, "acc_trade_price_24h": 40000000000},
            {"market": "KRW-ALT1", "trade_price": 500, "signed_change_rate": 0.04, "acc_trade_price_24h": 5000000000},
            {"market": "KRW-ALT2", "trade_price": 1000, "signed_change_rate": 0.05, "acc_trade_price_24h": 8000000000},
        ]
        mock_api.get_orderbook.return_value = {
            "orderbook_units": [
                {"ask_price": 501.0, "bid_price": 500.0, "bid_size": 100000.0}  # Spread 0.2%, Depth 50,000,000 (> 20M)
            ]
        }

        screener = MarketScreener(bithumb_api=mock_api, min_trade_value_krw=1000000000)

        # 1. No held positions -> BTC, ETH, SOL, XRP must NOT be in scanned candidates
        candidates = screener.scan_markets(top_count=5, held_markets=[])
        scanned_markets = [c["market"] for c in candidates]
        self.assertNotIn("KRW-BTC", scanned_markets)
        self.assertNotIn("KRW-ETH", scanned_markets)
        self.assertNotIn("KRW-SOL", scanned_markets)
        self.assertNotIn("KRW-XRP", scanned_markets)
        self.assertIn("KRW-ALT1", scanned_markets)
        self.assertIn("KRW-ALT2", scanned_markets)

        # 2. If user holds KRW-BTC, it must be included as held candidate
        held_candidates = screener.scan_markets(top_count=5, held_markets=["KRW-BTC"])
        held_markets = [c["market"] for c in held_candidates if c.get("is_held")]
        self.assertIn("KRW-BTC", held_markets)


if __name__ == "__main__":
    unittest.main()
