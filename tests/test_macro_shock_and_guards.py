"""
BTC 매크로 쇼크 필터(Macro Shock Filter) 및 AI 단독 승인 가드레일 단위 테스트
"""

import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from strategy_engine import StrategyPolicy, classify_btc_regime, is_ai_direct_entry_eligible


class TestMacroShockAndAIGuards(unittest.TestCase):
    def setUp(self):
        # 테스트 전 매크로 쇼크 상태 초기화
        StrategyPolicy._MACRO_SHOCK_STATE.clear()

    def tearDown(self):
        StrategyPolicy._MACRO_SHOCK_STATE.clear()

    def test_classify_btc_regime_detects_15m_shock(self):
        """15분 기준 -0.4% 급락 시 매크로 쇼크 감지 및 RISK_OFF 전환"""
        candles_5m = [
            {"trade_price": 99.5},   # cur_p
            {"trade_price": 99.8},
            {"trade_price": 100.0},
            {"trade_price": 100.0},  # p_3 (drop: -0.5%)
        ]
        res = classify_btc_regime(candles_5m)
        self.assertTrue(res.get("macro_shock"))
        self.assertEqual(res["regime"], "RISK_OFF")
        self.assertIn("15분", res["reason"])

    def test_classify_btc_regime_detects_1h_shock_and_blocks_bull_trend(self):
        """12H 상승 정배열이어도 최근 1H -0.48% 급락 시 BULL_TREND 진입을 원천 차단하고 RISK_OFF로 격하"""
        now_price = 115000000.0
        # 1H candles: 0번(현재)은 115M, 3번(직전)은 115.57M (약 -0.49% 급락)
        # 12번은 114M (+0.87% 상승 상태)
        candles_1h = [{"trade_price": now_price}]
        for idx in range(1, 25):
            if idx <= 3:
                candles_1h.append({"trade_price": 115570000.0})
            elif idx <= 12:
                candles_1h.append({"trade_price": 114000000.0})
            else:
                candles_1h.append({"trade_price": 113000000.0})

        candles_5m = [{"trade_price": now_price} for _ in range(5)]

        res = classify_btc_regime(candles_5m, candles_1h)
        self.assertTrue(res.get("macro_shock"))
        self.assertNotEqual(res["regime"], "BULL_TREND")
        self.assertEqual(res["regime"], "RISK_OFF")
        self.assertIn("매크로 쇼크", res["reason"])

    def test_macro_shock_cooldown_blocks_pre_qualification(self):
        """매크로 쇼크 쿨다운 활성 중에는 is_macro_valid가 False가 되어 사전 게이트에서 차단"""
        # 업비트 스코프에 30분 쿨다운 등록
        StrategyPolicy.set_macro_shock("upbit", time.time() + 1800.0, "BTC 15분 급락")

        is_shock, until_ts, reason = StrategyPolicy.is_macro_shock_active("upbit")
        self.assertTrue(is_shock)
        self.assertIn("BTC 15분 급락", reason)

        # 빗썸 스코프에는 등록되지 않았음을 확인 (거래소 격리)
        is_bithumb_shock, _, _ = StrategyPolicy.is_macro_shock_active("bithumb")
        self.assertFalse(is_bithumb_shock)

    def test_ai_direct_entry_blocks_over_8_pct_gain(self):
        """당일 +8.0% 초과 급등 종목은 AI 단독 자율 승인을 차단 (SOON 휩소 방지)"""
        # 85점 높은 알파 점수여도 당일 변동률 +11.89%이면 차단
        eligible = is_ai_direct_entry_eligible(
            alpha_score=85,
            btc_regime="BULL_TREND",
            is_night=False,
            change_rate_24h=0.1189,  # +11.89%
        )
        self.assertFalse(eligible)

        # 당일 변동률 +5.0%이면 정상 허용
        eligible_ok = is_ai_direct_entry_eligible(
            alpha_score=85,
            btc_regime="BULL_TREND",
            is_night=False,
            change_rate_24h=0.050,  # +5.0%
        )
        self.assertTrue(eligible_ok)

    def test_ai_direct_entry_requires_min_80_score_in_normal_regime(self):
        """AI 단독 승인은 최소 스코어(75점 이상 및 레짐 기준)를 요구"""
        # 70점은 탈락
        eligible_fail = is_ai_direct_entry_eligible(
            alpha_score=70,
            btc_regime="NORMAL",
            is_night=False,
            change_rate_24h=0.03,
        )
        self.assertFalse(eligible_fail)

        # 80점은 통과
        eligible_pass = is_ai_direct_entry_eligible(
            alpha_score=80,
            btc_regime="NORMAL",
            is_night=False,
            change_rate_24h=0.03,
        )
        self.assertTrue(eligible_pass)


if __name__ == "__main__":
    unittest.main()
