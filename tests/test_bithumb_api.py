import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from bithumb_api import BithumbAPI


class BithumbAPITests(unittest.TestCase):
    def setUp(self):
        self.api = BithumbAPI("test-access-key-12345", "test-secret-key-67890")

    def test_get_tickers_reuses_short_lived_cache_and_force_refresh_bypasses_it(self):
        # 시장 스캔으로 받은 시세는 짧은 시간 동안 개별 현재가 조회에 재사용한다.
        self.api._valid_markets_cache = {"KRW-BTC"}
        with patch.object(self.api, "_request") as mock_request:
            mock_request.return_value = [{"market": "KRW-BTC", "trade_price": 100.0}]

            self.assertEqual(self.api.get_current_price("KRW-BTC"), 100.0)
            self.assertEqual(self.api.get_current_price("KRW-BTC"), 100.0)
            self.assertEqual(mock_request.call_count, 1)

            self.assertEqual(self.api.get_current_price("KRW-BTC", force_refresh=True), 100.0)
            self.assertEqual(mock_request.call_count, 2)


if __name__ == "__main__":
    unittest.main()
