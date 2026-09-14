"""position_guard SSOT 단위 테스트."""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from position_guard import is_bot_managed_position, is_exit_allowed


class PositionGuardTests(unittest.TestCase):
    def test_manual_position_not_exit_allowed(self):
        journal = MagicMock()
        journal.orders = []
        tracker = MagicMock()
        tracker.get_entry_time.return_value = 0.0

        self.assertFalse(is_bot_managed_position(journal, tracker, "KRW-NEW"))
        self.assertFalse(is_exit_allowed("KRW-NEW", journal, tracker))

    def test_bot_filled_position_is_exit_allowed(self):
        journal = MagicMock()
        journal.orders = [{"market": "KRW-BTC", "side": "bid", "status": "FILLED"}]
        tracker = MagicMock()
        tracker.get_entry_time.return_value = 0.0

        self.assertTrue(is_bot_managed_position(journal, tracker, "KRW-BTC"))
        self.assertTrue(is_exit_allowed("KRW-BTC", journal, tracker))

    def test_protected_market_always_blocked(self):
        journal = MagicMock()
        journal.orders = [{"market": "KRW-HOLO", "side": "bid", "status": "FILLED"}]
        tracker = MagicMock()

        self.assertFalse(is_exit_allowed("KRW-HOLO", journal, tracker))

    def test_journal_missing_defaults_to_not_managed_for_exit(self):
        tracker = MagicMock()
        self.assertFalse(is_bot_managed_position(None, tracker, "KRW-ETH", default_if_journal_missing=False))
        self.assertTrue(is_bot_managed_position(None, tracker, "KRW-ETH", default_if_journal_missing=True))

    def test_partially_filled_then_canceled_is_bot_managed(self):
        """부분 체결 후 미체결 잔량이 취소되어 CANCELED 상태여도 체결 수량이 있으면 봇 관리 포지션으로 인정."""
        journal = MagicMock()
        journal.orders = [
            {
                "market": "KRW-SOLV",
                "side": "bid",
                "status": "CANCELED",
                "executed_volume": 2700.0,
                "volume": 16376.76,
            }
        ]
        tracker = MagicMock()
        tracker.get_entry_time.return_value = 0.0

        self.assertTrue(is_bot_managed_position(journal, tracker, "KRW-SOLV"))
        self.assertTrue(is_exit_allowed("KRW-SOLV", journal, tracker))

    def test_unfilled_canceled_with_prior_filled_is_bot_managed(self):
        """미체결 취소된 신규 주문(체결량 0)이 있더라도 이전 매수 체결 건이 있으면 봇 관리 포지션 유지."""
        journal = MagicMock()
        journal.orders = [
            {"market": "KRW-SOLV", "side": "bid", "status": "FILLED", "executed_volume": 1000.0},
            {"market": "KRW-SOLV", "side": "bid", "status": "CANCELED", "executed_volume": 0.0},
        ]
        tracker = MagicMock()
        tracker.get_entry_time.return_value = 0.0

        self.assertTrue(is_bot_managed_position(journal, tracker, "KRW-SOLV"))

    def test_unfilled_canceled_without_prior_filled_is_not_managed(self):
        """체결된 적 없이 전량 미체결 취소된 주문만 있는 경우 봇 관리 포지션이 아님."""
        journal = MagicMock()
        journal.orders = [
            {"market": "KRW-SOLV", "side": "bid", "status": "CANCELED", "executed_volume": 0.0},
        ]
        tracker = MagicMock()
        tracker.get_entry_time.return_value = 0.0

        self.assertFalse(is_bot_managed_position(journal, tracker, "KRW-SOLV"))
        self.assertFalse(is_exit_allowed("KRW-SOLV", journal, tracker))


if __name__ == "__main__":
    unittest.main()
