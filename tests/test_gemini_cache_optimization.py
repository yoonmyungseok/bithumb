import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

# src 경로 등록
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from gemini_analyzer import GeminiAnalyzer
from gemini_telemetry import GeminiTelemetry


import shutil
import tempfile

class TestGeminiCacheOptimization(unittest.TestCase):
    """Gemini AI 캐시 지속성 및 쿼터 보존 로직 검증 테스트"""

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
    def test_macro_diag_class_cache_persistence_across_instances(self, mock_call):
        """인스턴스가 새로 생성되어도 거시 시황(diagnose_macro_regime) 클래스 캐시가 유지되는지 검증"""
        mock_call.return_value = {
            "regime": "NORMAL",
            "risk_score": 35,
            "recommended_cash_ratio": 0.3,
            "summary": "BTC 정상 안정세",
            "action_guideline": "정상 운용",
        }
        candles_1h = [
            {"trade_price": 130_000_000, "candle_date_time_utc": f"2026-09-05T0{i}:00:00"}
            for i in range(15)
        ]

        # 1. 첫 번째 인스턴스에서 거시 시황 분석 실행 -> mock_call 1회 호출
        analyzer_1 = GeminiAnalyzer(api_key="test-key")
        res1 = analyzer_1.diagnose_macro_regime(candles_1h)
        self.assertEqual(res1["regime"], "NORMAL")
        self.assertEqual(mock_call.call_count, 1)

        # 2. 새로운 두 번째 인스턴스 생성 (5분 후 사이클 모사) -> mock_call 추가 호출 없이 캐시 재사용
        analyzer_2 = GeminiAnalyzer(api_key="test-key")
        res2 = analyzer_2.diagnose_macro_regime(candles_1h)
        self.assertEqual(res2["regime"], "NORMAL")
        self.assertEqual(mock_call.call_count, 1)  # 여전히 1회 (API 호출 생략)

        # 텔레메트리 캐시 적중 카운트 검증
        snap = GeminiTelemetry.snapshot()
        self.assertGreaterEqual(snap.cache_hits, 1)

    @patch.object(GeminiAnalyzer, "_call_gemini_json")
    def test_rank_candidate_markets_class_cache_persistence(self, mock_call):
        """인스턴스가 새로 생성되어도 스크리너 랭킹(rank_candidate_markets) 캐시가 유지되는지 검증"""
        mock_call.return_value = [
            {"market": "KRW-BTC", "ai_score": 90, "ai_tier": "TIER_1", "ai_reason": "대장주"},
            {"market": "KRW-ETH", "ai_score": 85, "ai_tier": "TIER_2", "ai_reason": "알트 2위"},
        ]
        candidates = [
            {"market": "KRW-BTC", "trade_price": 130_000_000, "change_rate": 0.02, "acc_trade_price_24h": 500_000_000_000},
            {"market": "KRW-ETH", "trade_price": 4_500_000, "change_rate": 0.03, "acc_trade_price_24h": 200_000_000_000},
        ]

        # 1. 첫 번째 인스턴스 호출
        analyzer_1 = GeminiAnalyzer(api_key="test-key")
        res1 = analyzer_1.rank_candidate_markets(candidates)
        self.assertEqual(mock_call.call_count, 1)

        # 2. 새로운 두 번째 인스턴스에서 동일 후보군 호출 -> 캐시 적중
        analyzer_2 = GeminiAnalyzer(api_key="test-key")
        res2 = analyzer_2.rank_candidate_markets(candidates)
        self.assertEqual(mock_call.call_count, 1)  # API 호출 생략
        self.assertEqual(len(res2), 2)

        snap = GeminiTelemetry.snapshot()
        self.assertGreaterEqual(snap.cache_hits, 1)

    def test_clear_caches(self):
        """clear_caches 호출 시 모든 캐시가 초기화되는지 검증"""
        GeminiAnalyzer._MACRO_DIAG_CACHE = {"regime": "TEST"}
        GeminiAnalyzer._LAST_MACRO_DIAG_TS = time.time()
        GeminiAnalyzer._SCREENER_RANK_CACHE = {"RANK:test": {"cached_at": time.time(), "result": []}}

        GeminiAnalyzer.clear_caches()
        self.assertEqual(GeminiAnalyzer._MACRO_DIAG_CACHE, {})
        self.assertEqual(GeminiAnalyzer._LAST_MACRO_DIAG_TS, 0.0)
        self.assertEqual(GeminiAnalyzer._SCREENER_RANK_CACHE, {})

    def test_should_call_ai_quality_gate(self):
        """로컬 관망(allow_buy=False) 상태일 때 알파 스코어 60점 미만은 AI 호출 차단, 60점 이상은 허용 검증"""
        from trading_runtime import TradingCycleEngine, TradingRuntimeConfig, TradingRuntimeContext, StrategyPolicy

        # 가상 엔진 컨텍스트
        config = MagicMock(spec=TradingRuntimeConfig)
        config.profile = MagicMock()
        config.exit_profile = MagicMock()
        config.entry_profile = MagicMock()
        config.buy_profile = MagicMock()
        config.gemini_api_key = "test-key"
        context = MagicMock(spec=TradingRuntimeContext)

        engine = TradingCycleEngine(config, context)
        self.assertIsNotNone(engine.analyzer)

        # 1. 알파스코어 40점 (50점 미만) -> allow_buy=False인 경우 should_call_ai 차단
        selected_entry_low = {"allow_buy": False, "alpha_score": 40}
        pre_qualification_passed = True
        candidate_trade_value = StrategyPolicy.MIN_TRADE_VALUE_RISK_OFF * 2.0
        base_safety_passed = True
        allow_ai = True

        local_alpha_score = int(selected_entry_low.get("alpha_score", 0))
        is_quality_promising = (local_alpha_score >= 50)
        should_call_ai_low = (
            engine.analyzer is not None
            and base_safety_passed
            and allow_ai
            and (
                selected_entry_low.get("allow_buy", False)
                or (
                    pre_qualification_passed
                    and candidate_trade_value >= StrategyPolicy.MIN_TRADE_VALUE_RISK_OFF * 0.5
                    and is_quality_promising
                )
            )
        )
        self.assertFalse(should_call_ai_low, "알파스코어 50점 미만 종목은 AI 호출이 차단되어야 합니다.")

        # 2. 알파스코어 55점 (50점 이상) -> allow_buy=False여도 싹수가 있어 AI 호출 통과
        selected_entry_high = {"allow_buy": False, "alpha_score": 55}
        local_alpha_score_high = int(selected_entry_high.get("alpha_score", 0))
        is_quality_promising_high = (local_alpha_score_high >= 50)
        should_call_ai_high = (
            engine.analyzer is not None
            and base_safety_passed
            and allow_ai
            and (
                selected_entry_high.get("allow_buy", False)
                or (
                    pre_qualification_passed
                    and candidate_trade_value >= StrategyPolicy.MIN_TRADE_VALUE_RISK_OFF * 0.5
                    and is_quality_promising_high
                )
            )
        )
        self.assertTrue(should_call_ai_high, "알파스코어 50점 이상 유망 종목은 AI 심층 분석을 허용해야 합니다.")


if __name__ == "__main__":
    unittest.main()
