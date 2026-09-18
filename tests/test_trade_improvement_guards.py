import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from strategy_engine import (
    StrategyPolicy,
    calculate_composite_alpha_score,
    evaluate_swing_trend_entry,
    evaluate_swing_trend_exit,
)
from order_safety import CooldownManager
from gemini_analyzer import GeminiAnalyzer


class TestTradeImprovementGuards(unittest.TestCase):
    def test_strategy_policy_values(self):
        """조정된 StrategyPolicy 파라미터 SSOT 검증"""
        self.assertEqual(StrategyPolicy.COOLDOWN_STOP_LOSS_SEC, 1800.0)
        self.assertEqual(StrategyPolicy.ALPHA_BUY_THRESHOLD_RISK_OFF, 60)
        self.assertEqual(StrategyPolicy.RISK_OFF_ALLOC_RATIO, 1.0)
        self.assertEqual(StrategyPolicy.PCT_B_MAX_RISK_OFF, 0.80)
        self.assertEqual(StrategyPolicy.PULLBACK_PCT_B_MAX_RISK_OFF, 0.80)
        self.assertEqual(StrategyPolicy.TIME_STOP_BREAKEVEN_MIN_PNL_PCT, 0.003)
        self.assertEqual(StrategyPolicy.MOMENTUM_BREAKOUT_RSI_MAX, 78.0)

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
                        "text": '{"STATUS": "ACTIVE", "ACTION": "BUY", "ENTRY_PRICE": 100, "TARGET_PRICE": 105, "STOP_LOSS": 97, "ALLOC_PCT": 0.5, "ALPHA_SCORE": 80, "REASON": "급등 모멘텀 포착"}'
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

    @patch("requests.post")
    def test_gemini_prompt_uses_current_policy_context(self, mock_post):
        """Gemini 요청 프롬프트가 현재 레짐·경로 정책과 JSON 스키마를 함께 전달하는지 검증"""
        analyzer = GeminiAnalyzer(api_key="fake_test_key")
        analyzer.get_candidate_models = MagicMock(return_value=["gemini-3.5-flash-lite"])
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "candidates": [{"content": {"parts": [{
                "text": '{"STATUS":"ACTIVE","ACTION":"HOLD","ENTRY_PRICE":100,"TARGET_PRICE":104,"STOP_LOSS":98,"ALLOC_PCT":0,"ALPHA_SCORE":70,"REASON":"대기"}'
            }]}}]
        }
        mock_post.return_value = mock_response
        candles = [
            {
                "opening_price": 100.0,
                "high_price": 101.0,
                "low_price": 99.0,
                "trade_price": 100.0,
                "candle_acc_trade_volume": 100.0,
                "candle_date_time_utc": "2026-09-06T00:00:00",
            }
            for _ in range(30)
        ]

        analyzer.analyze(
            market="KRW-TEST",
            current_price=100.0,
            candles=candles,
            krw_balance=100000.0,
            coin_balance=0.0,
            avg_buy_price=0.0,
            btc_regime="RISK_OFF",
            is_night=False,
            candidate_type="CONFIRMED",
            entry_policy_mode="STANDARD",
        )

        prompt = mock_post.call_args.kwargs["json"]["contents"][0]["parts"][0]["text"]
        self.assertIn("BTC 레짐: RISK_OFF", prompt)
        self.assertIn("현재 알파 승인 기준: 60점 이상", prompt)
        self.assertIn("AI의 판단은 주문 권한이 아닙니다", prompt)
        self.assertIn('"ALPHA_SCORE": 0', prompt)

        # 같은 확정봉이라도 정책 경로가 달라지면 캐시를 공유하지 않고 최신 기준을 다시 주입해야 한다.
        analyzer.analyze(
            market="KRW-TEST",
            current_price=100.0,
            candles=candles,
            krw_balance=100000.0,
            coin_balance=0.0,
            avg_buy_price=0.0,
            btc_regime="RISK_OFF",
            is_night=False,
            candidate_type="MOMENTUM_BREAKOUT",
            entry_policy_mode="STANDARD",
            momentum_phase="EXTENDED",
        )
        momentum_prompt = mock_post.call_args.kwargs["json"]["contents"][0]["parts"][0]["text"]
        self.assertEqual(mock_post.call_count, 2)
        self.assertIn("후보 유형: MOMENTUM_BREAKOUT", momentum_prompt)
        self.assertIn("현재 알파 승인 기준: 55점 이상", momentum_prompt)
        self.assertIn("모멘텀 단계: EXTENDED", momentum_prompt)
        self.assertIn("최대 종목 비중의 15% 제한 추격 진입", momentum_prompt)

        analyzer.analyze(
            market="KRW-TEST",
            current_price=100.0,
            candles=candles,
            krw_balance=100000.0,
            coin_balance=0.0,
            avg_buy_price=0.0,
            btc_regime="NORMAL",
            is_night=False,
            candidate_type="NEW_LISTING",
            entry_policy_mode="STANDARD",
        )
        new_listing_prompt = mock_post.call_args.kwargs["json"]["contents"][0]["parts"][0]["text"]
        self.assertEqual(mock_post.call_count, 3)
        self.assertIn("후보 유형: NEW_LISTING", new_listing_prompt)
        self.assertIn("현재 알파 승인 기준: 75점 이상", new_listing_prompt)
        self.assertIn("4H/1H MTF 게이트는 면제", new_listing_prompt)

    def test_trade_improvement_plan_constants(self):
        """전일 매매 분석 기반 개선 상수 설정 검증"""
        self.assertEqual(StrategyPolicy.SWING_ENTRY_EMA20_BUFFER_RATIO, 1.005)
        self.assertEqual(StrategyPolicy.SWING_TREND_EXIT_BUFFER_RATIO, 0.985)
        self.assertEqual(StrategyPolicy.RISK_OFF_MAX_ALT_ALLOC_PCT, 0.06)
        self.assertEqual(StrategyPolicy.RISK_OFF_MAX_ALT_BUDGET_KRW, 75000.0)
        self.assertEqual(StrategyPolicy.RISK_OFF_MIN_ORDERBOOK_RATIO, 1.00)

    def test_swing_entry_ema20_buffer_and_exit_margin(self):
        """스윙 진입(+0.5%)과 청산(-1.5%) 간 2.0% 안전 버퍼 검증"""
        candles_4h = [
            {"trade_price": 1000.0, "candle_date_time_kst": f"2026-09-17T{i:02d}:00:00"}
            for i in range(25)
        ]
        # 1004원: 1000원 * 1.005 미달이므로 진입 차단
        ok_1004, reason_1004 = evaluate_swing_trend_entry(candles_4h, 1004.0)
        self.assertFalse(ok_1004)
        self.assertIn("미달 진입 차단", reason_1004)

        # 1006원: 1000원 * 1.005 이상이므로 진입 허용
        ok_1006, reason_1006 = evaluate_swing_trend_entry(candles_4h, 1006.0)
        self.assertTrue(ok_1006)
        self.assertIn("추세 지지 확인", reason_1006)

        # 984원: 1000원 * 0.985 미달이므로 청산
        is_exit, exit_reason = evaluate_swing_trend_exit(candles_4h, 984.0)
        self.assertTrue(is_exit)
        self.assertIn("스윙 추세 이탈 청산", exit_reason)

        # 안전 마진 2.0% 이상 확보 확인
        margin_pct = (1000.0 * StrategyPolicy.SWING_ENTRY_EMA20_BUFFER_RATIO - 1000.0 * StrategyPolicy.SWING_TREND_EXIT_BUFFER_RATIO) / 1000.0 * 100.0
        self.assertAlmostEqual(margin_pct, 2.0, places=5)

    def test_orderbook_scoring_granularity(self):
        """호가 잔량비 점수 세분화 및 매도벽 페널티 검증"""
        candles = [{"trade_price": 1000.0, "opening_price": 1000.0, "candle_acc_trade_volume": 10.0} for _ in range(30)]
        ob_bearish = {"total_bid_size": 70.0, "total_ask_size": 100.0}
        res = calculate_composite_alpha_score(candles, btc_regime="RISK_OFF", orderbook=ob_bearish)
        self.assertEqual(res["factor_breakdown"]["orderbook_raw_ratio"], 0.7)

    def test_alt_position_sizing_risk_off_cap(self):
        """RISK_OFF 약세장에서 알트코인 매수 예산이 6% 및 75,000원 이하로 캡핑되는지 검증"""
        total_equity = 1_200_000.0  # 시드 120만 원
        krw_available = 1_000_000.0
        requested_budget = 167_000.0  # 비중 확대 요청 금액

        # 1. RISK_OFF 레짐 시:
        max_alt_alloc = StrategyPolicy.RISK_OFF_MAX_ALT_ALLOC_PCT  # 0.06
        max_alt_budget = min(
            total_equity * max_alt_alloc,
            StrategyPolicy.RISK_OFF_MAX_ALT_BUDGET_KRW,  # 75,000원
        )
        self.assertEqual(max_alt_budget, 72000.0)  # 120만 * 0.06 = 72,000원
        clamped_risk_off = min(krw_available, max(5000.0, min(requested_budget, max_alt_budget)))
        self.assertEqual(clamped_risk_off, 72000.0)

        # 2. NORMAL 레짐 시:
        max_alt_alloc_normal = StrategyPolicy.MAX_ALT_ALLOC_PCT  # 0.15
        max_alt_budget_normal = total_equity * max_alt_alloc_normal  # 180,000원
        clamped_normal = min(krw_available, max(120000.0, min(requested_budget, max_alt_budget_normal)))
        self.assertEqual(clamped_normal, 167000.0)  # 요청 금액 유지


if __name__ == "__main__":
    unittest.main()
