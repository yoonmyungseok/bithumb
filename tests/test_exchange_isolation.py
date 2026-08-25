import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from order_safety import CooldownManager, OrderJournal
from paper_broker import PaperBroker
from process_manager import find_bot_processes
from risk_manager import DailyRiskManager, TrailingStopTracker
from trade_memory import TradeMemoryManager


class ExchangeIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.bithumb_data_dir = os.path.join(self.tmp_dir.name, "bithumb")
        self.upbit_data_dir = os.path.join(self.tmp_dir.name, "upbit")
        os.makedirs(self.bithumb_data_dir, exist_ok=True)
        os.makedirs(self.upbit_data_dir, exist_ok=True)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_order_journal_isolation(self):
        b_journal = OrderJournal(data_dir=self.bithumb_data_dir)
        u_journal = OrderJournal(data_dir=self.upbit_data_dir)

        b_journal.record_intent("KRW-BTC", "bid", 0.01, 100000000.0, "limit")
        u_journal.record_intent("KRW-ETH", "bid", 0.1, 4000000.0, "limit")

        self.assertEqual(len(b_journal.orders), 1)
        self.assertEqual(b_journal.orders[0]["market"], "KRW-BTC")

        self.assertEqual(len(u_journal.orders), 1)
        self.assertEqual(u_journal.orders[0]["market"], "KRW-ETH")

        # Reload from disk and verify file separation
        b_reloaded = OrderJournal(data_dir=self.bithumb_data_dir)
        u_reloaded = OrderJournal(data_dir=self.upbit_data_dir)

        self.assertEqual(b_reloaded.orders[0]["market"], "KRW-BTC")
        self.assertEqual(u_reloaded.orders[0]["market"], "KRW-ETH")

    def test_daily_stats_isolation(self):
        b_rm = DailyRiskManager(data_dir=self.bithumb_data_dir)
        u_rm = DailyRiskManager(data_dir=self.upbit_data_dir)

        b_rm.add_realized_trade(50000.0, is_win=True)
        u_rm.add_realized_trade(-20000.0, is_win=False)

        self.assertEqual(b_rm.realized_pnl_krw, 50000.0)
        self.assertEqual(b_rm.win_trades_today, 1)

        self.assertEqual(u_rm.realized_pnl_krw, -20000.0)
        self.assertEqual(u_rm.win_trades_today, 0)
        self.assertEqual(u_rm.consecutive_losses, 1)

    def test_paper_account_isolation(self):
        dummy_api = types.SimpleNamespace(get_current_price=lambda m: 100000000.0 if m == "KRW-BTC" else 4000000.0)

        b_paper = PaperBroker(dummy_api, initial_krw=1_000_000.0, data_dir=self.bithumb_data_dir)
        u_paper = PaperBroker(dummy_api, initial_krw=5_000_000.0, data_dir=self.upbit_data_dir)

        b_paper.create_order("KRW-BTC", "bid", volume=0.005, price=100000000.0, ord_type="limit")
        u_paper.create_order("KRW-ETH", "bid", volume=0.5, price=4000000.0, ord_type="limit")

        b_balances = b_paper.get_balances()
        u_balances = u_paper.get_balances()

        self.assertIn("BTC", b_balances)
        self.assertNotIn("ETH", b_balances)

        self.assertIn("ETH", u_balances)
        self.assertNotIn("BTC", u_balances)


if __name__ == "__main__":
    unittest.main()
