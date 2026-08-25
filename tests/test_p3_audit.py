import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from backtest import QuantBacktester
from order_safety import OrderJournal, SafeOrderExecutor, get_excluded_markets_set
from risk_manager import calculate_total_equity, get_excluded_manual_holdings, get_held_markets


class TestP3Audit(unittest.TestCase):
    """P3-1 (격리 종목 단일 원천), P3-2 (백테스트 리포트 자동화), P3-3 (시스템 통합 감사) (완전 격리)"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.d_dir = self.temp_dir.name
        self.journal = OrderJournal(path=os.path.join(self.d_dir, "order_journal.json"))
        self.executor = SafeOrderExecutor(journal=self.journal)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_01_dynamic_excluded_markets_isolation(self):
        """1. 환경변수를 통한 동적 격리 종목 설정 시 모든 모듈에서 100% 차단/제외 검증 (P3-1)"""
        with patch.dict(os.environ, {"EXCLUDED_MANUAL_HOLDINGS": "KRW-HOLO,HOLO,KRW-SECRET,CUSTOM"}):
            excluded = get_excluded_markets_set()
            self.assertIn("KRW-HOLO", excluded)
            self.assertIn("HOLO", excluded)
            self.assertIn("KRW-SECRET", excluded)
            self.assertIn("SECRET", excluded)
            self.assertIn("KRW-CUSTOM", excluded)
            self.assertIn("CUSTOM", excluded)

            # 주문 제출 시 즉시 ValueError 발생 검증
            fake_exchange = MagicMock()
            with self.assertRaises(ValueError) as cm:
                self.executor.submit(fake_exchange, "KRW-SECRET", "bid", volume=10.0, price=1000.0)
            self.assertIn("수동 관리 격리 종목", str(cm.exception))

            # 자산 계산 및 보유 목록에서 제외 검증
            balances = {
                "KRW": {"balance": 100_000.0, "locked": 0.0},
                "SECRET": {"balance": 1000.0, "locked": 0.0},
                "BTC": {"balance": 0.01, "locked": 0.0},
            }
            fake_exchange.get_current_price.side_effect = lambda m: 100_000_000.0 if "BTC" in m else 5000.0
            
            held = get_held_markets(balances, fake_exchange)
            self.assertIn("KRW-BTC", held)
            self.assertNotIn("KRW-SECRET", held)

            total_eq = calculate_total_equity(balances, fake_exchange)
            # 100,000(KRW) + 1,000,000(BTC 0.01) = 1,100,000 (SECRET 5,000,000원은 배제되어야 함)
            self.assertEqual(total_eq, 1_100_000.0)

    def test_02_backtest_report_export_json_and_md(self):
        """2. 백테스트 결과의 JSON 및 Markdown 리포트 내보내기 무결성 검증 (P3-2)"""
        dummy_result = {
            "market": "KRW-BTC",
            "candles_tested": 500,
            "initial_capital": 1_000_000.0,
            "final_capital": 1_150_000.0,
            "total_return_pct": 15.0,
            "max_drawdown_pct": 2.5,
            "total_trades": 10,
            "win_trades": 8,
            "loss_trades": 2,
            "win_rate": 80.0,
            "profit_factor": 4.5,
            "expectancy_pct": 1.2,
            "fee_rate": 0.0004,
            "slippage_rate": 0.001,
            "timestop_bars": 12,
        }

        json_path = os.path.join(self.d_dir, "report.json")
        md_path = os.path.join(self.d_dir, "report.md")

        QuantBacktester.save_report_json(dummy_result, json_path)
        QuantBacktester.save_report_markdown(dummy_result, md_path)

        self.assertTrue(os.path.exists(json_path))
        self.assertTrue(os.path.exists(md_path))

        with open(json_path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
            self.assertEqual(loaded["market"], "KRW-BTC")
            self.assertEqual(loaded["total_return_pct"], 15.0)

        with open(md_path, "r", encoding="utf-8") as f:
            md_text = f.read()
            self.assertIn("# 📊 퀀트 백테스팅 결과 리포트: KRW-BTC", md_text)
            self.assertIn("+15.00%", md_text)


if __name__ == "__main__":
    unittest.main()
