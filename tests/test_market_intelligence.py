import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from groq_provider import GroqResult
from market_intelligence import MarketIntelligenceService
from trading_orchestrator import TradingOrchestrator


class TestMarketIntelligence(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        MarketIntelligenceService._instances.clear()

    def tearDown(self):
        MarketIntelligenceService._instances.clear()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_ema_calculation(self):
        # 최신값이 index 0
        values = [100.0, 90.0, 80.0, 70.0, 60.0]
        ema = MarketIntelligenceService._calculate_ema(values, 3)
        self.assertGreater(ema, 80.0)
        self.assertLess(ema, 100.0)

    def test_update_and_cache_flow(self):
        service = MarketIntelligenceService(exchange_scope="bithumb", data_dir=self.temp_dir)
        service.groq_provider = MagicMock()
        service.groq_provider.is_available = True
        service.groq_provider.complete_json.return_value = GroqResult(
            value={
                "regime": "CAUTION_PULLBACK",
                "risk_score": 65,
                "recommended_cash_ratio": 0.5,
                "market_summary": "단기 급등 후 과열 구간",
                "action_guideline": "보수적 운용",
            },
            model="llama-3.3-70b-versatile",
            success=True,
            latency_sec=0.25,
            status_code=200,
        )

        dummy_candles_1h = [{"trade_price": 95000000.0} for _ in range(25)]
        dummy_fng = {"value": "75", "desc": "75점 (탐욕)"}

        result = service.update_intelligence(
            btc_candles_1h=dummy_candles_1h,
            fng_index=dummy_fng,
            background=False,
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["regime"], "CAUTION_PULLBACK")
        self.assertEqual(result["risk_score"], 65)
        self.assertEqual(result["latency_sec"], 0.25)

        # 디스크 파일 생성 확인
        self.assertTrue(os.path.exists(service.storage_path))
        with open(service.storage_path, "r", encoding="utf-8") as f:
            disk_data = json.load(f)
            self.assertEqual(disk_data["regime"], "CAUTION_PULLBACK")

        # 캐시 조회 확인
        cached = service.get_latest_intelligence(max_age_sec=300.0)
        self.assertIsNotNone(cached)
        self.assertEqual(cached["regime"], "CAUTION_PULLBACK")

        # 만료 확인
        expired = service.get_latest_intelligence(max_age_sec=0.0)
        self.assertIsNone(expired)

    def test_orchestrator_market_state_integration(self):
        import logging
        orchestrator = TradingOrchestrator(logger=logging.getLogger("test"))
        mock_exchange = MagicMock()
        mock_exchange.exchange_name = "bithumb"
        mock_exchange.get_candles.side_effect = [
            # 5m candles (20개)
            [{"trade_price": 95000000.0, "opening_price": 95000000.0, "high_price": 95100000.0, "low_price": 94900000.0} for _ in range(20)],
            # 1h candles (50개)
            [{"trade_price": 95000000.0, "opening_price": 95000000.0, "high_price": 95100000.0, "low_price": 94900000.0} for _ in range(50)],
        ]

        # Groq 시장 분석에서 CRASH 판정 시뮬레이션
        service = MarketIntelligenceService.get_instance(exchange_scope="bithumb")
        service._cached_data = {
            "regime": "CRASH",
            "risk_score": 90,
            "market_summary": "BTC 연쇄 청산 급락",
            "analyzed_at": time.time(),
        }

        blocked, regime, reason = orchestrator.classify_market_regime(
            mock_exchange,
            interval_minutes=5,
            crash_threshold_pct=1.0,
            analyzer=None,
            fng_index=None,
        )

        self.assertTrue(blocked)
        self.assertEqual(regime, "CRASH")
        self.assertIn("Groq 거시 위기 경보", reason)

        # Groq 시장 분석에서 CAUTION_PULLBACK 판정 시뮬레이션
        service._cached_data = {
            "regime": "CAUTION_PULLBACK",
            "risk_score": 60,
            "market_summary": "단기 숨고르기",
            "analyzed_at": time.time(),
        }

        mock_exchange.get_candles.side_effect = [
            [{"trade_price": 95000000.0, "opening_price": 95000000.0, "high_price": 95100000.0, "low_price": 94900000.0} for _ in range(20)],
            [{"trade_price": 95000000.0, "opening_price": 95000000.0, "high_price": 95100000.0, "low_price": 94900000.0} for _ in range(50)],
        ]

        blocked2, regime2, reason2 = orchestrator.classify_market_regime(
            mock_exchange,
            interval_minutes=5,
            crash_threshold_pct=1.0,
            analyzer=None,
            fng_index=None,
        )

        self.assertFalse(blocked2)
        self.assertEqual(regime2, "RISK_OFF")
        self.assertIn("Groq: 단기 숨고르기", reason2)


if __name__ == "__main__":
    unittest.main()
