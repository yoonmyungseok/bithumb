"""Dual-Track 스윙(SWING) 전략 검증 테스트 스위트."""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from market_screener import MarketScreener
from risk_controls import RiskGuard, calculate_risk_position_size
from risk_manager import TrailingStopTracker
from strategy_engine import StrategyPolicy, evaluate_swing_trend_exit


class FakeExchangeAPI:
    def get_all_markets(self, is_details=True):
        return [
            {"market": "KRW-BTC", "korean_name": "비트코인"},
            {"market": "KRW-ETH", "korean_name": "이더리움"},
            {"market": "KRW-SOL", "korean_name": "솔라나"},
            {"market": "KRW-DOGE", "korean_name": "도지코인"},
        ]

    def get_tickers(self, markets):
        return self.get_ticker(markets)

    def get_ticker(self, markets):
        return [
            {"market": "KRW-BTC", "trade_price": 100_000_000.0, "signed_change_rate": 0.02, "acc_trade_price_24h": 200_000_000_000.0},
            {"market": "KRW-ETH", "trade_price": 5_000_000.0, "signed_change_rate": 0.03, "acc_trade_price_24h": 100_000_000_000.0},
            {"market": "KRW-SOL", "trade_price": 200_000.0, "signed_change_rate": 0.05, "acc_trade_price_24h": 80_000_000_000.0},
            {"market": "KRW-DOGE", "trade_price": 200.0, "signed_change_rate": 0.01, "acc_trade_price_24h": 10_000_000_000.0},
        ]


class TestSwingStrategy(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_swing_policy_parameters(self):
        """StrategyPolicy에 정의된 스윙 파라미터가 계획서 기준과 일치하는지 검증."""
        self.assertEqual(StrategyPolicy.SWING_STOP_LOSS_PCT, 0.055)
        self.assertEqual(StrategyPolicy.SWING_HARD_STOP_PCT, 0.080)
        self.assertEqual(StrategyPolicy.SWING_PARTIAL_TP_1_PCT, 0.080)
        self.assertEqual(StrategyPolicy.SWING_PARTIAL_TP_1_RATIO, 0.40)
        self.assertEqual(StrategyPolicy.SWING_PARTIAL_TP_2_PCT, 0.150)
        self.assertEqual(StrategyPolicy.SWING_PARTIAL_TP_2_RATIO, 0.30)
        self.assertEqual(StrategyPolicy.SWING_TRAILING_START_PCT, 0.080)
        self.assertEqual(StrategyPolicy.SWING_TRAILING_DROP_PCT, 0.040)
        self.assertEqual(StrategyPolicy.SWING_BREAKEVEN_STOP_PCT, 0.015)
        self.assertFalse(StrategyPolicy.SWING_TIME_STOP_ENABLED)

    def test_evaluate_swing_trend_exit(self):
        """4시간봉 추세 지지선 이탈 기반 청산 판정 검증."""
        # 4H 캔들 25개 생성 (종가 100원 유지 ➜ EMA20은 100원 근방)
        candles_4h = [{"trade_price": 100.0} for _ in range(25)]

        # 현재가 99원: EMA20(100원)의 0.985(98.5원) 이상이므로 추세 유지
        is_exit, reason = evaluate_swing_trend_exit(candles_4h, current_price=99.0)
        self.assertFalse(is_exit)
        self.assertIn("추세 양호", reason)

        # 현재가 97원: 98.5원 미만이므로 추세 이탈 청산
        is_exit, reason = evaluate_swing_trend_exit(candles_4h, current_price=97.0)
        self.assertTrue(is_exit)
        self.assertIn("추세 이탈 청산", reason)

    def test_risk_guard_dual_track_slots(self):
        """RiskGuard에서 단타 슬롯과 스윙 슬롯이 독립적으로 격리되는지 검증."""
        # 총 3슬롯 중 스윙 1슬롯, 단타 2슬롯
        guard = RiskGuard(
            min_order_krw=5000.0,
            max_open_positions=3,
            max_position_pct=0.35,
            max_total_exposure_pct=0.85,
            max_order_krw=1_000_000.0,
            max_swing_positions=1,
        )

        total_equity = 1_000_000.0
        available_krw = 500_000.0

        # 1. 단타 2종목 보유 중인 상태
        held_markets = ["KRW-ALT1", "KRW-ALT2"]
        held_swings = []

        # 단타 3번째 종목 매수 시도 ➜ 단타 한도(2개) 초과로 차단
        ok, msg = guard.validate_buy(
            market="KRW-ALT3",
            order_krw=200_000.0,
            available_krw=available_krw,
            total_equity=total_equity,
            held_markets=held_markets,
            strategy_mode="SCALP",
            held_swing_markets=held_swings,
        )
        self.assertFalse(ok)
        self.assertIn("단타 전용 보유 종목 수 한도", msg)

        # 단타가 꽉 차 있어도 스윙 종목 매수 시도 ➜ 스윙 슬롯(1개) 여유로 승인!
        ok, msg = guard.validate_buy(
            market="KRW-BTC",
            order_krw=250_000.0,
            available_krw=available_krw,
            total_equity=total_equity,
            held_markets=held_markets,
            strategy_mode="SWING",
            held_swing_markets=held_swings,
        )
        self.assertTrue(ok)
        self.assertEqual(msg, "OK")

        # 2. 스윙 1종목이 이미 보유된 상태
        held_swings = ["KRW-BTC"]
        held_markets = ["KRW-ALT1", "KRW-BTC"]

        # 추가 스윙 종목 매수 시도 ➜ 스윙 한도(1개) 초과로 차단
        ok, msg = guard.validate_buy(
            market="KRW-ETH",
            order_krw=200_000.0,
            available_krw=available_krw,
            total_equity=total_equity,
            held_markets=held_markets,
            strategy_mode="SWING",
            held_swing_markets=held_swings,
        )
        self.assertFalse(ok)
        self.assertIn("스윙 전용 보유 종목 수 한도", msg)

        # 대신 단타 2번째 종목 매수 시도 ➜ 단타 슬롯(1개 남음)으로 승인!
        ok, msg = guard.validate_buy(
            market="KRW-ALT2",
            order_krw=200_000.0,
            available_krw=available_krw,
            total_equity=total_equity,
            held_markets=held_markets,
            strategy_mode="SCALP",
            held_swing_markets=held_swings,
        )
        self.assertTrue(ok)
        self.assertEqual(msg, "OK")

    def test_trailing_stop_tracker_swing_mode(self):
        """TrailingStopTracker에서 스윙 모드 설정 및 분할익절/트레일링 동작 검증."""
        tracker = TrailingStopTracker(data_dir=self.temp_dir)
        market = "KRW-BTC"

        # 스윙 모드 설정
        tracker.set_strategy_mode(market, "SWING")
        self.assertTrue(tracker.is_swing_position(market))
        self.assertEqual(tracker.get_strategy_mode(market), "SWING")
        self.assertIn(market, tracker.get_swing_markets())

        avg_buy_price = 100_000_000.0

        # 1. +5% 상승 시: 단타는 1차 익절(+3.5%) 나갔겠지만, 스윙은 +8% 목표이므로 익절 안 나감
        action, peak, trigger, peak_pct, cur_pct = tracker.check_position(
            market=market,
            current_price=105_000_000.0,
            avg_buy_price=avg_buy_price,
        )
        self.assertEqual(action, "NONE")

        # 2. +8.5% 상승 시: 스윙 1차 분할 익절 (+8.0% 이상 도달) 트리거
        action, peak, trigger, peak_pct, cur_pct = tracker.check_position(
            market=market,
            current_price=108_500_000.0,
            avg_buy_price=avg_buy_price,
        )
        self.assertEqual(action, "PARTIAL_TP_1")

        # 3. 브레이크이븐 활성화 확인
        self.assertTrue(tracker.is_breakeven_active(market))

        # 4. 최고점 120_000_000원(+20%) 찍은 후 -4.5% 반락 시 ➜ 스윙 트레일링(드롭 4%) 청산
        tracker.peaks[market] = 120_000_000.0
        action, peak, trigger, peak_pct, cur_pct = tracker.check_position(
            market=market,
            current_price=114_000_000.0,  # 120,000,000 * 0.96 = 115,200,000 이하
            avg_buy_price=avg_buy_price,
        )
        self.assertEqual(action, "TRAILING_STOP")

    def test_market_screener_scan_swing_markets(self):
        """MarketScreener의 scan_swing_markets가 대형 메이저 우선 선별하는지 검증."""
        fake_api = FakeExchangeAPI()
        screener = MarketScreener(bithumb_api=fake_api)

        swing_candidates = screener.scan_swing_markets(top_count=2, btc_regime="NORMAL")
        self.assertTrue(len(swing_candidates) > 0)

        # 메이저 코인(BTC, ETH, SOL)이 우선 선별되어 상위에 오는지 확인
        first = swing_candidates[0]
        self.assertIn(first["market"], ["KRW-BTC", "KRW-ETH", "KRW-SOL"])
        self.assertEqual(first["candidate_type"], "SWING")
        self.assertEqual(first["strategy_mode"], "SWING")


if __name__ == "__main__":
    unittest.main()
