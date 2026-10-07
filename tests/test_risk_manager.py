import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from risk_manager import (
    TrailingStopTracker,
    build_positions_data,
    calculate_total_equity,
    get_held_markets,
)


class TrailingStopTrackerReconcileTests(unittest.TestCase):
    def test_reconcile_markets_adopts_orphan_positions_without_journal_backward_compat(self):
        """order_journal 매개변수가 없는 경우 하위 호환으로 자동 입양."""
        tracker = TrailingStopTracker(data_dir=".test-tmp-current")
        tracker.entry_times.clear()
        tracker.strategy_modes.clear()

        held_markets = ["KRW-BTC", "KRW-SOLV"]
        tracker.reconcile_markets(held_markets)

        self.assertGreater(tracker.get_entry_time("KRW-BTC"), 0.0)
        self.assertEqual(tracker.get_strategy_mode("KRW-BTC"), "SWING")
        self.assertGreater(tracker.get_entry_time("KRW-SOLV"), 0.0)
        self.assertEqual(tracker.get_strategy_mode("KRW-SOLV"), "SCALP")

    def test_reconcile_markets_protects_manual_positions_with_journal(self):
        """주문 저널에 매수 이력이 없는 수동 포지션은 절대 입양하지 않고 격리 보호."""
        tracker = TrailingStopTracker(data_dir=".test-tmp-current")
        tracker.entry_times.clear()
        tracker.strategy_modes.clear()

        mock_journal = MagicMock()
        # KRW-BTC는 봇 매수 활성 포지션, KRW-CAP은 수동 매수 종목(저널 이력 없음)
        mock_journal.has_active_bot_position.side_effect = lambda m: m.upper() == "KRW-BTC"

        held_markets = ["KRW-BTC", "KRW-CAP"]
        tracker.reconcile_markets(held_markets, order_journal=mock_journal)

        # 봇 매수 종목인 KRW-BTC는 복구되어 트래커에 등록됨
        self.assertGreater(tracker.get_entry_time("KRW-BTC"), 0.0)
        self.assertEqual(tracker.get_strategy_mode("KRW-BTC"), "SWING")

        # 수동 매수 종목인 KRW-CAP은 절대 등록되지 않음 (수동 격리 보호)
        self.assertEqual(tracker.get_entry_time("KRW-CAP"), 0.0)
        self.assertNotIn("KRW-CAP", tracker.strategy_modes)


class RiskManagerPortfolioBatchTests(unittest.TestCase):
    def setUp(self):
        self.balances = {
            "KRW": {"balance": 1_000_000.0, "locked": 0.0, "avg_buy_price": 0.0},
            "BTC": {"balance": 0.01, "locked": 0.0, "avg_buy_price": 90_000_000.0},
            "ETH": {"balance": 0.5, "locked": 0.0, "avg_buy_price": 4_000_000.0},
        }
        self.mock_api = MagicMock()
        self.mock_api.get_tickers.return_value = [
            {"market": "KRW-BTC", "trade_price": 100_000_000.0},
            {"market": "KRW-ETH", "trade_price": 5_000_000.0},
        ]
        self.mock_api.get_korean_name.side_effect = lambda m: m.split("-")[-1]

    def test_portfolio_helpers_share_single_get_tickers_call(self):
        calculate_total_equity(self.balances, self.mock_api)
        get_held_markets(self.balances, self.mock_api)
        build_positions_data(self.balances, self.mock_api, strategies={})

        self.assertEqual(self.mock_api.get_tickers.call_count, 3)
        self.mock_api.get_current_price.assert_not_called()

    def test_shared_price_map_limits_get_tickers_to_one_call(self):
        price_map = {
            "KRW-BTC": 100_000_000.0,
            "KRW-ETH": 5_000_000.0,
        }

        calculate_total_equity(self.balances, self.mock_api, price_map=price_map)
        get_held_markets(self.balances, self.mock_api, price_map=price_map)
        build_positions_data(self.balances, self.mock_api, strategies={}, price_map=price_map)

        self.mock_api.get_tickers.assert_not_called()
        self.mock_api.get_current_price.assert_not_called()


if __name__ == "__main__":
    unittest.main()
