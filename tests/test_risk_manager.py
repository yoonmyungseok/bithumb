import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from risk_manager import (
    build_positions_data,
    calculate_total_equity,
    get_held_markets,
)


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
