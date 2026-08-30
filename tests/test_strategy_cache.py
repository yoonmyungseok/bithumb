import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from risk_manager import StrategyCacheManager
from gemini_analyzer import GeminiAnalyzer


class TestStrategyCacheManager(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_save_and_load_cache(self):
        mgr = StrategyCacheManager(data_dir=self.test_dir, exchange_name="bithumb")
        strats = {
            "KRW-BTC": {
                "market": "KRW-BTC",
                "action": "BUY",
                "alpha_score": 85,
                "target_price": 100000000.0,
                "stop_loss": 95000000.0,
            }
        }
        mgr.save_cache(strats)

        loaded_strats, elapsed, is_valid = mgr.get_valid_strategies(ttl=300.0)
        self.assertTrue(is_valid)
        self.assertIn("KRW-BTC", loaded_strats)
        self.assertEqual(loaded_strats["KRW-BTC"]["alpha_score"], 85)
        self.assertLess(elapsed, 5.0)

    def test_cache_ttl_expiration(self):
        mgr = StrategyCacheManager(data_dir=self.test_dir, exchange_name="bithumb")
        strats = {"KRW-ETH": {"market": "KRW-ETH", "action": "HOLD"}}
        mgr.save_cache(strats)

        # TTL이 0.001초인 경우 만료 검증
        time.sleep(0.02)
        loaded_strats, elapsed, is_valid = mgr.get_valid_strategies(ttl=0.01)
        self.assertFalse(is_valid)

    def test_exchange_isolation(self):
        bithumb_mgr = StrategyCacheManager(data_dir=self.test_dir, exchange_name="bithumb")
        upbit_mgr = StrategyCacheManager(data_dir=self.test_dir, exchange_name="upbit")

        bithumb_mgr.save_cache({"KRW-XRP": {"market": "KRW-XRP", "exchange": "bithumb"}})
        upbit_mgr.save_cache({"KRW-SOL": {"market": "KRW-SOL", "exchange": "upbit"}})

        b_strats, _, b_valid = bithumb_mgr.get_valid_strategies()
        u_strats, _, u_valid = upbit_mgr.get_valid_strategies()

        self.assertTrue(b_valid)
        self.assertTrue(u_valid)
        self.assertIn("KRW-XRP", b_strats)
        self.assertNotIn("KRW-SOL", b_strats)
        self.assertIn("KRW-SOL", u_strats)
        self.assertNotIn("KRW-XRP", u_strats)

    def test_gemini_analyzer_candle_cache(self):
        analyzer = GeminiAnalyzer(api_key="test_dummy_key")
        candles = [
            {
                "candle_date_time_utc": "2026-08-30T09:35:00",
                "opening_price": 1000.0,
                "high_price": 1050.0,
                "low_price": 990.0,
                "trade_price": 1020.0,
                "candle_acc_trade_volume": 500.0,
            }
            for _ in range(30)
        ]

        # 1차 분석 실행 (로컬 퀀트 폴백)
        res1 = analyzer.analyze(
            market="KRW-TEST",
            current_price=1020.0,
            candles=candles,
            krw_balance=100000.0,
            coin_balance=0.0,
            avg_buy_price=0.0,
        )
        self.assertIn("status", res1)

        # 2차 분석 실행 (동일 캔들 캐시 히트)
        res2 = analyzer.analyze(
            market="KRW-TEST",
            current_price=1020.0,
            candles=candles,
            krw_balance=100000.0,
            coin_balance=0.0,
            avg_buy_price=0.0,
        )
        self.assertEqual(res1["alpha_score"], res2["alpha_score"])
        self.assertEqual(res1["action"], res2["action"])


if __name__ == "__main__":
    unittest.main()
