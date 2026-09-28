import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from strategy_engine import (
    StrategyPolicy,
    is_ai_direct_entry_eligible,
    evaluate_shakeout_sweep_setup,
    get_shakeout_sweep_alpha_threshold,
)
from trading_runtime import validate_emergency_exit_safety
from runtime_config import load_runtime_risk_settings


class TestOptionATradingImprovements(unittest.TestCase):
    def test_ssot_parameters(self):
        """1. SSOT 파라미터 무결성 검증"""
        # 약세장 개미털기 스윕 알파 임계치 70점 확인
        self.assertEqual(StrategyPolicy.SHAKEOUT_SWEEP_ALPHA_THRESHOLD_RISK_OFF, 70)
        self.assertEqual(StrategyPolicy.SHAKEOUT_SWEEP_VOLUME_RATIO_MIN_RISK_OFF, 1.8)
        self.assertEqual(StrategyPolicy.SHAKEOUT_SWEEP_MIN_LOWER_SHADOW_RATIO_RISK_OFF, 0.60)
        
        # 런타임 리스크 설정 트레일링 시작점 3.5% 확인
        settings = load_runtime_risk_settings()
        self.assertEqual(settings.trailing_start_pct, 0.035)

    def test_ai_direct_entry_option_a_weak_market_block(self):
        """2. 옵션 A: 약세장(RISK_OFF / CRASH / BEAR_VOLATILE)에서 AI 단독 진입 전면 차단 검증"""
        # 약세장(RISK_OFF)에서는 알파 점수가 90점 만점이어도 차단되어야 함
        self.assertFalse(is_ai_direct_entry_eligible(alpha_score=90, btc_regime="RISK_OFF"))
        self.assertFalse(is_ai_direct_entry_eligible(alpha_score=85, btc_regime="CRASH"))
        self.assertFalse(is_ai_direct_entry_eligible(alpha_score=80, btc_regime="BEAR_VOLATILE"))

        # 정상장(NORMAL) 및 강세장(BULL_TREND)에서는 고알파 시 허용되어야 함
        self.assertTrue(is_ai_direct_entry_eligible(alpha_score=80, btc_regime="NORMAL", change_rate_24h=0.03))
        self.assertTrue(is_ai_direct_entry_eligible(alpha_score=75, btc_regime="BULL_TREND", change_rate_24h=0.03))

    def test_swing_position_emergency_exit_protection(self):
        """3. 스윙(SWING) 포지션의 단기 AI 비상탈출 차단 검증"""
        ai_eval = {"confidence": 95, "action": "EMERGENCY_EXIT", "reason": "5분봉 음봉 전환"}
        
        # 스윙 포지션인 경우 단기 AI 비상탈출이 차단되고 HOLD로 방어되어야 함
        is_safe, guard_reason, fallback = validate_emergency_exit_safety(
            market="KRW-SOON",
            korean_name="쑨",
            current_price=420.0,
            avg_buy_price=433.0,
            pnl_pct_current=-3.0,
            hold_duration_sec=1800.0,
            ai_eval=ai_eval,
            candles_5m=[{"opening_price": 425.0, "trade_price": 420.0}],
            is_btc_crashing=False,
            is_bot_managed=True,
            is_swing=True,
        )
        self.assertFalse(is_safe)
        self.assertEqual(fallback, "HOLD")
        self.assertIn("스윙 포지션 보호", guard_reason)

        # 스윙이 아닌 일반 단타 포지션이고 조건을 충족하면 긴급 탈출 승인 가능
        is_safe_scalp, _, action_scalp = validate_emergency_exit_safety(
            market="KRW-SCALP",
            korean_name="단타코인",
            current_price=420.0,
            avg_buy_price=433.0,
            pnl_pct_current=-3.0,
            hold_duration_sec=1800.0,
            ai_eval=ai_eval,
            candles_5m=[{"opening_price": 425.0, "trade_price": 420.0}],
            is_btc_crashing=False,
            is_bot_managed=True,
            is_swing=False,
        )
        self.assertTrue(is_safe_scalp)
        self.assertEqual(action_scalp, "EMERGENCY_EXIT")

    def test_shakeout_sweep_weak_market_stricter_filter(self):
        """4. 약세장 개미털기 스윕 거래량(1.8배) 및 아랫꼬리(60%) 강화 검증"""
        # 가상 캔들 구성: 25봉
        # 1~12봉: 저점 100
        # 0봉: 저점 95(스윕), 종가 102(재탈환), 고점 104, 시가 101, 거래량 150 (직전평균 100 대비 1.5배)
        candles = []
        c0 = {
            "high_price": 104.0,
            "opening_price": 101.0,
            "trade_price": 102.0,
            "low_price": 95.0,  # 아랫꼬리 = 101 - 95 = 6. 진폭 = 9. 6/9 = 66.7% (60% 통과)
            "candle_acc_trade_volume": 150.0,  # 1.5배
        }
        candles.append(c0)
        for _ in range(24):
            candles.append({
                "high_price": 103.0,
                "opening_price": 101.0,
                "trade_price": 101.5,
                "low_price": 100.0,
                "candle_acc_trade_volume": 100.0,
            })

        # NORMAL 레짐에서는 거래량 1.5배(기준 1.3배 이상)이므로 통과
        passed_norm, reason_norm, _, _ = evaluate_shakeout_sweep_setup(
            candles=candles, current=102.0, rsi=35.0, pct_b=0.15, btc_regime="NORMAL"
        )
        self.assertTrue(passed_norm, f"NORMAL 실패: {reason_norm}")

        # RISK_OFF 레짐에서는 거래량 1.5배는 기준 1.8배 미달이므로 차단되어야 함
        passed_risk_off, reason_risk_off, _, _ = evaluate_shakeout_sweep_setup(
            candles=candles, current=102.0, rsi=35.0, pct_b=0.15, btc_regime="RISK_OFF"
        )
        self.assertFalse(passed_risk_off, f"RISK_OFF 통과오류: {reason_risk_off}")
        self.assertIn("기준 1.80배", reason_risk_off)


if __name__ == '__main__':
    unittest.main()
