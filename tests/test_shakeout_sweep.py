import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from order_safety.cooldown import CooldownManager
from strategy_engine import (
    StrategyPolicy,
    entry_signal,
    evaluate_shakeout_sweep_setup,
    get_shakeout_sweep_alpha_threshold,
)


class ShakeoutSweepStrategyTests(unittest.TestCase):
    def setUp(self):
        # 30개의 기본 5분봉 생성 (직전 저점 약 100.0원, 정상적인 가격 변동)
        self.base_candles = []
        for i in range(30):
            p = 101.0 + (i % 5) * 0.5
            self.base_candles.append({
                "opening_price": p - 0.2,
                "high_price": p + 1.0,
                "low_price": 100.0 if i > 0 else 97.0,
                "trade_price": 101.5 if i == 0 else p,
                "candle_acc_trade_volume": 2000.0 if i == 0 else 1000.0,
            })

    def test_evaluate_shakeout_sweep_setup_success(self):
        """전저점 이탈 후 재탈환 + 아랫꼬리 50% 이상 + 거래량 폭증 시 스윕 셋업 통과 검증."""
        candles = list(self.base_candles)
        # 캔들 0: low 97.0, high 102.0, open 99.5, close 101.5
        # range = 5.0, lower_shadow = 99.5 - 97.0 = 2.5 (50%), upper_shadow = 102.0 - 101.5 = 0.5 (10%)
        candles[0] = {
            "opening_price": 99.5,
            "high_price": 102.0,
            "low_price": 97.0,
            "trade_price": 101.5,
            "candle_acc_trade_volume": 2000.0,
        }
        passed, reason, sweep_low, details = evaluate_shakeout_sweep_setup(
            candles=candles,
            current=101.5,
            rsi=38.0,
            pct_b=0.25,
            lookback_bars=12,
        )
        self.assertTrue(passed, f"스윕 셋업이 통과해야 합니다: {reason}")
        self.assertEqual(sweep_low, 97.0)
        self.assertGreaterEqual(details["lower_shadow_ratio"], 0.50)
        self.assertLessEqual(details["upper_shadow_ratio"], 0.25)
        self.assertTrue(details["sweep_occurred"])
        self.assertTrue(details["reclaim_passed"])

    def test_evaluate_shakeout_sweep_setup_falling_knife_blocked(self):
        """재탈환 실패 및 장대 음봉(떨어지는 칼날) 시 스윕 차단 검증."""
        candles = list(self.base_candles)
        # 종가가 전저점(100.0) 밑에 머물고 아랫꼬리가 짧은 장대 음봉
        candles[0] = {
            "opening_price": 99.0,
            "high_price": 99.5,
            "low_price": 95.0,
            "trade_price": 95.5,
            "candle_acc_trade_volume": 2500.0,
        }
        passed, reason, sweep_low, details = evaluate_shakeout_sweep_setup(
            candles=candles,
            current=95.5,
            rsi=25.0,
            pct_b=0.05,
            lookback_bars=12,
        )
        self.assertFalse(passed, "떨어지는 칼날 장대음봉은 스윕으로 인정되면 안 됩니다.")
        self.assertIn("미달", reason)

    def test_evaluate_entry_rules_shakeout_sweep_success(self):
        """entry_signal에서 entry_type=SHAKEOUT_SWEEP 정상 승인 및 동적 손절가 산출 검증."""
        candles = list(self.base_candles)
        candles[0] = {
            "opening_price": 99.5,
            "high_price": 102.0,
            "low_price": 97.0,
            "trade_price": 101.5,
            "candle_acc_trade_volume": 2000.0,
        }
        result = entry_signal(
            candles=candles,
            entry_type="SHAKEOUT_SWEEP",
            btc_regime="NORMAL",
            market="KRW-ALT",
            orderbook={
                "orderbook_units": [
                    {"bid_price": 101.0, "bid_size": 3000.0, "ask_price": 101.5, "ask_size": 1000.0},
                    {"bid_price": 100.5, "bid_size": 4000.0, "ask_price": 102.0, "ask_size": 1000.0},
                ],
                "total_bid_size": 7000.0,
                "total_ask_size": 2000.0,
            },
        )
        self.assertTrue(result["allow_buy"], f"개미털기 스윕 진입이 허용되어야 합니다: {result['reason']}")
        self.assertEqual(result["entry_type"], "SHAKEOUT_SWEEP")
        # 손절가는 스윕 저점(97.0 * 0.995 = 96.515) 또는 최대 손절선 범위 내로 산출
        self.assertLess(result["stop_loss"], 101.5)
        self.assertGreater(result["target_price"], 101.5)
        self.assertIn("shakeout_sweep", result["checklist_details"])
        self.assertTrue(result["checklist_details"]["shakeout_sweep"]["pass"])

    def test_evaluate_entry_rules_shakeout_sweep_blocked_on_btc_crash(self):
        """BTC CRASH 레짐에서는 스윕 패턴이 완벽해도 신규 매수가 fail-closed로 차단되어야 함."""
        candles = list(self.base_candles)
        candles[0] = {
            "opening_price": 99.5,
            "high_price": 102.0,
            "low_price": 97.0,
            "trade_price": 101.5,
            "candle_acc_trade_volume": 2000.0,
        }
        result = entry_signal(
            candles=candles,
            entry_type="SHAKEOUT_SWEEP",
            btc_regime="CRASH",
            market="KRW-ALT",
            orderbook={"orderbook_units": []},
        )
        self.assertFalse(result["allow_buy"], "BTC CRASH에서는 개미털기 역매수도 차단되어야 합니다.")

    def test_cooldown_shakeout_reclaim_bypass(self):
        """손절 1회 후 쿨다운 상태에서 손절가 이상으로 재탈환 시 allow_shakeout_reclaim=True로 쿨다운 통과 검증."""
        import tempfile
        temp_dir = tempfile.mkdtemp()
        test_state = os.path.join(temp_dir, "test_shakeout_cd.json")

        cd = CooldownManager(
            default_sl_cooldown=1800.0,
            max_daily_losses_per_market=2,
            data_dir=temp_dir,
            state_file=test_state,
        )
        # 100.0원에 손절 기록 (쿨다운 30분 활성화)
        cd.record_exit("KRW-TEST", "STOP_LOSS", 100.0)

        # 1) 일반 재진입 검사: 쿨다운 중이므로 차단되어야 함
        allowed_normal, reason_normal = cd.check_reentry_allowed("KRW-TEST", 101.0, allow_shakeout_reclaim=False)
        self.assertFalse(allowed_normal)
        self.assertIn("쿨다운 대기 중", reason_normal)

        # 2) 개미털기 재탈환 검사: 현재가(101.0) >= 손절가(100.0) 이므로 쿨다운 통과
        allowed_sweep, reason_sweep = cd.check_reentry_allowed("KRW-TEST", 101.0, allow_shakeout_reclaim=True)
        self.assertTrue(allowed_sweep, f"개미털기 재탈환 시 쿨다운이 통과되어야 합니다: {reason_sweep}")
        self.assertEqual(reason_sweep, "SHAKEOUT_RECLAIM_OK")

        # 3) 현재가가 손절가 미만(98.0)이면 allow_shakeout_reclaim이어도 차단
        allowed_under, _ = cd.check_reentry_allowed("KRW-TEST", 98.0, allow_shakeout_reclaim=True)
        self.assertFalse(allowed_under)

        # 4) 당일 손절 2회 누적 시에는 allow_shakeout_reclaim이어도 일일 한도로 엄격 차단 (Fail-Closed)
        cd.record_exit("KRW-TEST", "STOP_LOSS", 98.0)
        allowed_limit, reason_limit = cd.check_reentry_allowed("KRW-TEST", 102.0, allow_shakeout_reclaim=True)
        self.assertFalse(allowed_limit)
        self.assertIn("당일 손절", reason_limit)


if __name__ == "__main__":
    unittest.main()
