"""업비트 RISK_OFF 모멘텀 돌파 당일 손실 재진입 차단 회귀 테스트."""

import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from order_safety import OrderFillProcessor, OrderJournal, OrderStatus
from order_safety.risk_off_loss_reentry import (
    RiskOffLossReentryGuard,
    get_kst_date_str,
    kst_midnight_after_date,
    qualifies_risk_off_loss_reentry_record,
)
from risk_manager import DailyRiskManager, TrailingStopTracker

KST = timezone(timedelta(hours=9))


class UpbitRiskOffLossReentryBlockTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_upbit_risk_off_reentry_")
        self.guard = RiskOffLossReentryGuard(data_dir=self.test_dir)
        self.journal = OrderJournal(data_dir=self.test_dir, exchange_scope="upbit")
        self.cooldown = MagicMock()
        self.processor = OrderFillProcessor(
            self.journal,
            DailyRiskManager(data_dir=self.test_dir),
            trade_memory=None,
            trailing_tracker=TrailingStopTracker(data_dir=self.test_dir),
            cooldown_manager=self.cooldown,
            risk_off_loss_reentry_guard=self.guard,
        )

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_time_stop_loss_blocks_same_day_reentry(self):
        self.guard.record_confirmed_loss_exit(
            exchange="upbit",
            market="KRW-ALT",
            exit_reason="TIME_STOP",
            net_pnl_krw=-1200.0,
        )
        blocked, info = self.guard.check_reentry_blocked("KRW-ALT")
        self.assertTrue(blocked)
        self.assertEqual(info["exit_reason"], "TIME_STOP")
        self.assertEqual(info["exchange"], "upbit")
        self.assertTrue(self.guard.applies_to_entry_path("RISK_OFF", "MOMENTUM_BREAKOUT"))
        self.assertFalse(self.guard.applies_to_entry_path("NORMAL", "MOMENTUM_BREAKOUT"))

    def test_profitable_exit_does_not_record_block(self):
        self.guard.record_confirmed_loss_exit(
            exchange="upbit",
            market="KRW-ALT",
            exit_reason="TIME_STOP",
            net_pnl_krw=500.0,
        )
        blocked, _ = self.guard.check_reentry_blocked("KRW-ALT")
        self.assertFalse(blocked)

    def test_next_kst_day_unblocks(self):
        yesterday = (datetime.now(KST) - timedelta(days=1)).strftime("%Y-%m-%d")
        self.guard._records["KRW-ALT"] = {
            "kst_date": yesterday,
            "exit_ts": datetime.now(KST).timestamp(),
            "exit_reason": "STOP_LOSS",
            "confirmed_net_pnl_krw": -800.0,
            "exchange": "upbit",
        }
        blocked, _ = self.guard.check_reentry_blocked("KRW-ALT")
        self.assertFalse(blocked)
        self.assertNotIn("KRW-ALT", self.guard._records)

    def test_ack_does_not_record_loss_block(self):
        client_id = self.journal.record_intent(
            "KRW-ALT", "ask", 10.0, None, "market", avg_buy_price=100.0, exchange="upbit",
        )
        self.journal.mark(client_id, OrderStatus.ACKNOWLEDGED, exit_reason="TIME_STOP")
        self.processor.process_order_fill(
            client_id,
            OrderStatus.ACKNOWLEDGED,
            executed_volume=0.0,
            avg_price=99.0,
            remaining_volume=10.0,
        )
        blocked, _ = self.guard.check_reentry_blocked("KRW-ALT")
        self.assertFalse(blocked)
        self.cooldown.record_exit.assert_not_called()

    def test_partial_sell_loss_with_fee_records_block(self):
        client_id = self.journal.record_intent(
            "KRW-ALT", "ask", 10.0, None, "market", avg_buy_price=100.0, exchange="upbit",
        )
        self.journal.mark(client_id, OrderStatus.OPEN, exit_reason="TIME_STOP")
        # 체결가 100, 수수료로 순손익 음수
        self.processor.process_order_fill(
            client_id,
            OrderStatus.FILLED,
            executed_volume=5.0,
            avg_price=100.0,
            fee=50.0,
            remaining_volume=5.0,
        )
        blocked, info = self.guard.check_reentry_blocked("KRW-ALT")
        self.assertTrue(blocked)
        self.assertLess(info["confirmed_net_pnl_krw"], 0)

    def test_bithumb_exchange_does_not_record(self):
        self.guard.record_confirmed_loss_exit(
            exchange="bithumb",
            market="KRW-ALT",
            exit_reason="TIME_STOP",
            net_pnl_krw=-1000.0,
        )
        blocked, _ = self.guard.check_reentry_blocked("KRW-ALT")
        self.assertFalse(blocked)

    def test_qualifies_exit_reason_matrix(self):
        self.assertTrue(qualifies_risk_off_loss_reentry_record("STOP_LOSS", -1.0))
        self.assertTrue(qualifies_risk_off_loss_reentry_record("AI_TIGHTENED_STOP", -1.0))
        self.assertFalse(qualifies_risk_off_loss_reentry_record("AI_TIGHTENED_STOP", 1.0))
        self.assertFalse(qualifies_risk_off_loss_reentry_record("TRAILING_STOP", -1.0))

    def test_next_allowed_at_is_kst_midnight(self):
        today = get_kst_date_str()
        ts = kst_midnight_after_date(today)
        next_str = datetime.fromtimestamp(ts, tz=KST).strftime("%Y-%m-%d %H:%M:%S")
        self.assertTrue(next_str.endswith("00:00:00"))

    def test_normal_regime_path_not_subject_to_guard(self):
        self.guard.record_confirmed_loss_exit(
            exchange="upbit",
            market="KRW-ALT",
            exit_reason="TIME_STOP",
            net_pnl_krw=-900.0,
        )
        self.assertFalse(self.guard.applies_to_entry_path("NORMAL", "MOMENTUM_BREAKOUT"))


if __name__ == "__main__":
    unittest.main()
