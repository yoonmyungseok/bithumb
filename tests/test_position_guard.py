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


if __name__ == "__main__":
    unittest.main()
