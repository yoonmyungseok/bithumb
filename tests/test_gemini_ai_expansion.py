import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# src 디렉터리 경로 등록
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from gemini_analyzer import GeminiAnalyzer
from risk_manager import TrailingStopTracker


class TestGeminiAIExpansion(unittest.TestCase):
    """Gemini AI 활용 고도화 1, 2, 3순위 기능 검증 테스트"""

    def setUp(self):
        self.analyzer = GeminiAnalyzer(api_key="test-api-key")
        self.tracker = TrailingStopTracker()

    def test_trailing_tracker_dynamic_exit(self):
        """TrailingStopTracker의 동적 손절가, 목표가 및 러너 모드 관리 검증"""
        market = "KRW-BTC"
        self.assertFalse(self.tracker.is_runner_mode(market))
        self.assertIsNone(self.tracker.get_dynamic_stop_loss(market))

        # 손절선 상향 및 러너 모드 활성화
        self.tracker.update_dynamic_exit(market, target_price=105_000_000.0, stop_loss=99_000_000.0, runner_mode=True)
        self.assertTrue(self.tracker.is_runner_mode(market))
        self.assertEqual(self.tracker.get_dynamic_target_price(market), 105_000_000.0)
        self.assertEqual(self.tracker.get_dynamic_stop_loss(market), 99_000_000.0)

        # 손절선은 오직 상향(Tighten)만 허용되어야 함 (하향 시도 무시)
        self.tracker.update_dynamic_exit(market, stop_loss=98_000_000.0)
        self.assertEqual(self.tracker.get_dynamic_stop_loss(market), 99_000_000.0)

        # 포지션 정리 시 dynamic 상태 정리
        self.tracker.clear(market)
        self.assertFalse(self.tracker.is_runner_mode(market))
        self.assertIsNone(self.tracker.get_dynamic_stop_loss(market))

    @patch.object(GeminiAnalyzer, "_call_gemini_json")
    def test_evaluate_holding_position_emergency_exit(self, mock_call):
        """AI 포지션 평가 - 세력 덤핑 감지 시 EMERGENCY_EXIT 반환 검증"""
        mock_call.return_value = {
            "ACTION": "EMERGENCY_EXIT",
            "REASON": "거래량 실린 장대음봉 및 VWAP 하향 이탈",
            "ADJUSTED_TARGET_PRICE": 0,
            "ADJUSTED_STOP_LOSS": 0,
            "CONFIDENCE": 90,
        }
        candles = [
            {"candle_date_time_utc": "2026-09-04T03:00:00", "trade_price": 95000, "opening_price": 100000, "candle_acc_trade_volume": 500}
        ]
        res = self.analyzer.evaluate_holding_position(
            market="KRW-ETH",
            current_price=95000,
            avg_buy_price=100000,
            candles=candles,
        )
        self.assertEqual(res["action"], "EMERGENCY_EXIT")
        self.assertIn("장대음봉", res["reason"])

    @patch.object(GeminiAnalyzer, "_call_gemini_json")
    def test_evaluate_holding_position_runner_and_tighten(self, mock_call):
        """AI 포지션 평가 - 러너 모드 및 목표가 상향 검증"""
        mock_call.return_value = {
            "ACTION": "RUNNER_HOLD",
            "REASON": "신고가 돌파 랠리 지속, 추가 상승 여력",
            "ADJUSTED_TARGET_PRICE": 115000,
            "ADJUSTED_STOP_LOSS": 102000,
            "CONFIDENCE": 85,
        }
        candles = [
            {"candle_date_time_utc": "2026-09-04T03:05:00", "trade_price": 108000, "opening_price": 105000, "candle_acc_trade_volume": 1000}
        ]
        res = self.analyzer.evaluate_holding_position(
            market="KRW-SOL",
            current_price=108000,
            avg_buy_price=100000,
            candles=candles,
        )
        self.assertEqual(res["action"], "RUNNER_HOLD")
        self.assertEqual(res["adjusted_target_price"], 115000)
        self.assertEqual(res["adjusted_stop_loss"], 102000)

    @patch.object(GeminiAnalyzer, "_call_gemini_json")
    def test_rank_candidate_markets_batch(self, mock_call):
        """스크리너 후보 종목 1회 배치 AI 랭킹 및 티어 정렬 검증"""
        candidates = [
            {"market": "KRW-AAA", "trade_price": 1000, "change_rate": 0.05, "acc_trade_price_24h": 50_000_000_000, "relative_strength": 0.02},
            {"market": "KRW-BBB", "trade_price": 2000, "change_rate": 0.08, "acc_trade_price_24h": 100_000_000_000, "relative_strength": 0.04},
            {"market": "KRW-CCC", "trade_price": 3000, "change_rate": 0.12, "acc_trade_price_24h": 10_000_000_000, "relative_strength": -0.01},
        ]
        mock_call.return_value = [
            {"market": "KRW-BBB", "rank": 1, "tier": "TIER_1", "score": 95, "reason": "거래대금 압도적 주도주"},
            {"market": "KRW-AAA", "rank": 2, "tier": "TIER_2", "score": 80, "reason": "안정적 추세"},
            {"market": "KRW-CCC", "rank": 9, "tier": "REJECT", "score": 30, "reason": "설거지 의심"},
        ]

        ranked = self.analyzer.rank_candidate_markets(candidates, btc_regime="NORMAL", btc_change_rate=0.01)
        self.assertEqual(len(ranked), 3)
        self.assertEqual(ranked[0]["market"], "KRW-BBB")
        self.assertEqual(ranked[0]["ai_tier"], "TIER_1")
        self.assertEqual(ranked[1]["market"], "KRW-AAA")
        self.assertEqual(ranked[2]["market"], "KRW-CCC")
        self.assertEqual(ranked[2]["ai_tier"], "REJECT")

    @patch.object(GeminiAnalyzer, "_call_gemini_json")
    def test_diagnose_macro_regime(self, mock_call):
        """BTC 매크로 레짐 AI 정밀 진단 검증"""
        mock_call.return_value = {
            "regime": "BULL_TREND",
            "risk_score": 25,
            "recommended_cash_ratio": 0.2,
            "summary": "BTC 1시간봉 EMA 정배열 및 지속 상승세",
            "action_guideline": "알트코인 적극 분할 매수",
        }
        btc_candles_1h = [{"trade_price": 90000000 + i * 100000} for i in range(25)]
        diag = self.analyzer.diagnose_macro_regime(btc_candles_1h)
        self.assertEqual(diag["regime"], "BULL_TREND")
        self.assertEqual(diag["risk_score"], 25)
        self.assertIn("BULL_TREND", diag["regime"])

    def test_fallback_when_no_api_key(self):
        """API 키 없을 때 안전하게 로컬 룰로 폴백하는지 검증"""
        no_key_analyzer = GeminiAnalyzer(api_key="")
        holding_res = no_key_analyzer.evaluate_holding_position(
            market="KRW-BTC", current_price=1000, avg_buy_price=900, candles=[]
        )
        self.assertEqual(holding_res["action"], "HOLD")

        candidates = [{"market": "KRW-BTC", "trade_price": 1000}]
        ranked = no_key_analyzer.rank_candidate_markets(candidates)
        self.assertEqual(ranked, candidates)


if __name__ == "__main__":
    unittest.main()
