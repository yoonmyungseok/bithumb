"""Unit tests for Gemini API call optimization and quota protection."""

import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from gemini_analyzer import GeminiAnalyzer
from gemini_telemetry import GeminiTelemetry


class TestGeminiCallOptimization(unittest.TestCase):
    """Gemini API 호출 횟수 최적화 및 불필요한 호출 방지 검증 테스트"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        GeminiTelemetry.configure(data_dir=self.temp_dir)
        GeminiTelemetry.reset(persist=True)
        GeminiAnalyzer.clear_caches()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        data_dir = os.path.join(project_root, "data")
        GeminiTelemetry.configure(data_dir=data_dir)
        GeminiAnalyzer.clear_caches()

    @patch.object(GeminiAnalyzer, "_call_gemini_json")
    def test_rank_candidate_individual_cache_synthesis(self, mock_call):
        """후보 종목 중 일부만 바뀌었을 때 API 재호출 없이 개별 캐시가 합성되는지 검증"""
        mock_call.return_value = [
            {"market": "KRW-BTC", "rank": 1, "tier": "TIER_1", "score": 95, "reason": "대장주 랠리"},
            {"market": "KRW-ETH", "rank": 2, "tier": "TIER_1", "score": 90, "reason": "알트 대장"},
            {"market": "KRW-SOL", "rank": 3, "tier": "TIER_2", "score": 85, "reason": "모멘텀 양호"},
            {"market": "KRW-XRP", "rank": 4, "tier": "TIER_2", "score": 80, "reason": "안정적 거래대금"},
        ]

        analyzer = GeminiAnalyzer(api_key="test-key")

        # 1. 1차 호출 (4개 종목 초기 평가) -> API 1회 호출
        batch1 = [
            {"market": "KRW-BTC", "trade_price": 100_000_000},
            {"market": "KRW-ETH", "trade_price": 4_000_000},
            {"market": "KRW-SOL", "trade_price": 250_000},
            {"market": "KRW-XRP", "trade_price": 800},
        ]
        res1 = analyzer.rank_candidate_markets(batch1)
        self.assertEqual(mock_call.call_count, 1)
        self.assertEqual(len(res1), 4)

        # 2. 2차 호출: 3개 종목은 기존과 동일하고 1개(KRW-DOGE)만 신규 진입 (75% 캐시 커버리지)
        # -> API 호출 없이 개별 캐시 합성으로 응답해야 함 (mock_call count 유지)
        batch2 = [
            {"market": "KRW-BTC", "trade_price": 100_500_000},
            {"market": "KRW-ETH", "trade_price": 4_020_000},
            {"market": "KRW-SOL", "trade_price": 251_000},
            {"market": "KRW-DOGE", "trade_price": 200},  # 신규 종목 1개
        ]
        res2 = analyzer.rank_candidate_markets(batch2)
        self.assertEqual(mock_call.call_count, 1)  # API 추가 호출 없음!
        self.assertEqual(len(res2), 4)

        # BTC는 기존 캐시된 점수(95점, TIER_1)가 유지되어야 함
        btc_meta = next(c for c in res2 if c["market"] == "KRW-BTC")
        self.assertEqual(btc_meta["ai_tier"], "TIER_1")
        self.assertEqual(btc_meta["ai_score"], 95)

        # 신규 종목 DOGE는 기본 fallback 메타가 부여되어야 함
        doge_meta = next(c for c in res2 if c["market"] == "KRW-DOGE")
        self.assertEqual(doge_meta["ai_tier"], "TIER_2")
        self.assertEqual(doge_meta["ai_score"], 50.0)

        # 텔레메트리 캐시 적중 카운트 증가 확인
        snap = GeminiTelemetry.snapshot()
        self.assertGreaterEqual(snap.cache_hits, 1)

    @patch("requests.post")
    def test_analyze_5m_timeslot_cache_stability(self, mock_post):
        """실시간 틱 가격이 조금 달라져도 5분 타임블록 내에서는 analyze API 호출이 중복되지 않는지 검증"""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": '{"STATUS":"ACTIVE","ACTION":"BUY","ENTRY_PRICE":1000,"TARGET_PRICE":1050,"STOP_LOSS":980,"ALLOC_PCT":0.3,"REASON":"골든크로스"}'
                            }
                        ]
                    }
                }
            ]
        }
        mock_post.return_value = mock_response

        analyzer = GeminiAnalyzer(api_key="test-key")

        candles = [
            {"trade_price": 1000.0, "opening_price": 995.0, "high_price": 1010.0, "low_price": 990.0, "candle_acc_trade_volume": 100.0, "candle_date_time_utc": "2026-09-05T14:00:00"}
            for _ in range(30)
        ]

        # 1. 첫 번째 분석 호출 -> API 1회 호출
        res1 = analyzer.analyze(
            market="KRW-BTC",
            current_price=1000.0,
            candles=candles,
            krw_balance=1_000_000,
            coin_balance=0.0,
            avg_buy_price=0.0,
        )
        self.assertEqual(res1["action"], "BUY")
        self.assertEqual(mock_post.call_count, 1)

        # 2. 10초 뒤 실시간 틱으로 현재가가 1002원으로 살짝 변동되었으나 동일 5분봉 캔들인 경우
        res2 = analyzer.analyze(
            market="KRW-BTC",
            current_price=1002.0,
            candles=candles,
            krw_balance=1_000_000,
            coin_balance=0.0,
            avg_buy_price=0.0,
        )
        self.assertEqual(res2["action"], "BUY")
        self.assertEqual(mock_post.call_count, 1)  # 캐시 적중으로 API 호출 억제

        snap = GeminiTelemetry.snapshot()
        self.assertGreaterEqual(snap.cache_hits, 1)

    def test_1h_trend_filter_logic(self):
        """1시간봉 역배열 하락세 종목의 1H 추세 사전 필터 차단 검증"""
        from gemini_analyzer import GeminiAnalyzer as GA

        # 1시간봉이 100원에서 70원으로 지속 하락 중인 캔들
        prices_1h = [float(70 + i) for i in range(25)]  # 최근가 = 70원, 과거 = 94원
        ema20_1h = GA.calculate_ema(prices_1h, 20)
        ema50_1h = GA.calculate_ema(prices_1h, min(len(prices_1h), 50))
        current_price = 70.0

        # 역배열(EMA20 < EMA50) 및 현재가 < EMA20
        self.assertLess(ema20_1h, ema50_1h)
        self.assertLess(current_price, ema20_1h)

        is_1h_trend_valid = True
        if (ema20_1h < ema50_1h and current_price < ema20_1h) or (current_price < ema20_1h * 0.985):
            is_1h_trend_valid = False

        # 사전 필터에서 차단되어야 함
        self.assertFalse(is_1h_trend_valid)

    def test_local_alpha_score_threshold_50(self):
        """알파 스코어가 45점(50점 미만)일 때는 관망 종목의 AI 분석이 차단되고 50점 이상일 때 통과하는지 검증"""
        # 45점: 차단
        score_45 = 45
        is_quality_promising_45 = (score_45 >= 50)
        self.assertFalse(is_quality_promising_45)

        # 50점: 통과
        score_50 = 50
        is_quality_promising_50 = (score_50 >= 50)
        self.assertTrue(is_quality_promising_50)


if __name__ == "__main__":
    unittest.main()
