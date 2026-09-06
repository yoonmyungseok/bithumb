import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from strategy_engine import StrategyPolicy
from order_safety import CooldownManager
from gemini_analyzer import GeminiAnalyzer


class TestTradeImprovementGuards(unittest.TestCase):
    def test_strategy_policy_values(self):
        """조정된 StrategyPolicy 파라미터 SSOT 검증"""
        self.assertEqual(StrategyPolicy.COOLDOWN_STOP_LOSS_SEC, 1800.0)
        self.assertEqual(StrategyPolicy.ALPHA_BUY_THRESHOLD_RISK_OFF, 70)
        self.assertEqual(StrategyPolicy.RISK_OFF_ALLOC_RATIO, 0.4)
        self.assertEqual(StrategyPolicy.PCT_B_MAX_RISK_OFF, 0.75)
        self.assertEqual(StrategyPolicy.PULLBACK_PCT_B_MAX_RISK_OFF, 0.75)
        self.assertEqual(StrategyPolicy.TIME_STOP_BREAKEVEN_MIN_PNL_PCT, 0.003)
        self.assertEqual(StrategyPolicy.MOMENTUM_BREAKOUT_RSI_MAX, 72.0)

    def test_cooldown_knife_catch_and_daily_limit(self):
        """CooldownManager의 떨어지는 칼날 잡기 방지 및 당일 2회 차단 검증"""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            cd = CooldownManager(
                default_sl_cooldown=1800.0,
                max_daily_losses_per_market=2,
                data_dir=td,
            )
            # 1회 손절
            cd.record_exit("KRW-TEST", "STOP_LOSS", exit_price=100.0)
            self.assertEqual(cd.get_daily_loss_count("KRW-TEST"), 1)

            # 쿨다운 중 차단
            allowed, reason = cd.check_reentry_allowed("KRW-TEST", 99.0)
            self.assertFalse(allowed)
            self.assertIn("쿨다운 대기 중", reason)

            # 쿨다운을 강제 만료 처리하여 갭 필터 검증 (최근 1분 전 청산 상태로 모킹)
            with cd._lock:
                cd._records["KRW-TEST"]["expire_at"] = 0.0
                cd._records["KRW-TEST"]["timestamp"] = time.time() - 60.0

            # 100원 대비 95원(-5% 폭락 중) -> 칼날 잡기 방지 차단
            allowed_knife, reason_knife = cd.check_reentry_allowed("KRW-TEST", 95.0)
            self.assertFalse(allowed_knife)
            self.assertIn("칼날 잡기 방지", reason_knife)

            # 100원 대비 100.5원(+0.5% 회복/반등) -> 재진입 허용
            allowed_ok, reason_ok = cd.check_reentry_allowed("KRW-TEST", 100.5)
            self.assertTrue(allowed_ok)

            # 2회 손절 발생
            cd.record_exit("KRW-TEST", "AI 긴급 비상탈출", exit_price=98.0)
            self.assertEqual(cd.get_daily_loss_count("KRW-TEST"), 2)

            # 당일 완전 차단
            allowed_block, reason_block = cd.check_reentry_allowed("KRW-TEST", 105.0)
            self.assertFalse(allowed_block)
            self.assertIn("당일 손절 2회 누적", reason_block)

    @patch("requests.post")
    def test_gemini_overheat_guardrail_overrides_buy(self, mock_post):
        """AI가 BUY를 응답해도 지표 과열 시 HOLD로 오버라이드되는지 검증"""
        analyzer = GeminiAnalyzer(api_key="fake_test_key")
        analyzer.get_candidate_models = MagicMock(return_value=["gemini-3.5-flash-lite"])

        # AI 응답 모킹: BUY 권고
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "candidates": [{
                "content": {
                    "parts": [{
                        "text": '{"STATUS": "ACTIVE", "ACTION": "BUY", "ENTRY_PRICE": 100, "TARGET_PRICE": 105, "STOP_LOSS": 97, "ALLOC_PCT": 0.5, "REASON": "급등 모멘텀 포착"}'
                    }]
                }
            }]
        }
        mock_post.return_value = mock_response

        # 가상 캔들: RSI 과열을 유발하는 지속 상승 캔들 (RSI > 65)
        candles_overheat = []
        for p in range(100, 200, 5):
            candles_overheat.append({
                "opening_price": p - 2,
                "high_price": p + 2,
                "low_price": p - 2,
                "trade_price": p,
                "candle_acc_trade_volume": 100.0,
            })
        candles_overheat.reverse()

        res = analyzer.analyze(
            market="KRW-HOT",
            current_price=200.0,
            candles=candles_overheat,
            krw_balance=100000.0,
            coin_balance=0.0,
            avg_buy_price=0.0,
        )

        # 과열 가드레일이 작동하여 HOLD로 강제 전환되었는지 확인
        self.assertEqual(res["action"], "HOLD")
        self.assertEqual(res["alloc_pct"], 0.0)
        self.assertIn("과열 가드레일 작동", res["reason"])


if __name__ == "__main__":
    unittest.main()
