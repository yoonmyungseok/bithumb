"""
수동 포지션 스마트 모니터링 알림 (MANUAL_POSITION_OBSERVER) 단위 테스트
"""

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from manual_position_observer import ManualPositionObserver


class TestManualPositionObserver(unittest.TestCase):
    def setUp(self):
        self.mock_ex = MagicMock()
        self.mock_telegram = MagicMock()
        self.mock_logger = MagicMock()
        self.observer = ManualPositionObserver(
            exchange=self.mock_ex,
            telegram=self.mock_telegram,
            logger=self.mock_logger,
            enabled=True,
            drop_threshold_pct=0.08,  # -8%
            cooldown_sec=7200.0,
        )

    def test_observes_drop_from_high_on_manual_position(self):
        """수동 종목이 24시간 고점 대비 -10% 급락했을 때 텔레그램 시황 알림 발송"""
        balances = {
            "KRW": {"balance": 50000.0, "locked": 0.0},
            "US": {"balance": 1000.0, "locked": 0.0},  # 수동 보유 코인
        }
        order_journal = MagicMock()
        order_journal.orders = []  # 봇 매수 기록 없음 -> 수동 코인

        # 캔들 설정: 2일 전 고가 10.0원, 현재가 9.0원 (-10% 급락)
        daily_candles = [
            {"trade_price": 9.0, "high_price": 9.5},
            {"trade_price": 9.8, "high_price": 10.0},
        ] + [{"trade_price": 9.5, "high_price": 9.6} for _ in range(25)]

        self.mock_ex.get_candles.return_value = daily_candles

        events = self.observer.observe_manual_positions(balances, order_journal=order_journal)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["market"], "KRW-US")
        self.assertLessEqual(event["drop_from_high_pct"], -8.0)
        self.assertTrue(self.mock_telegram.send_debounced_message.called)
        sent_msg = self.mock_telegram.send_debounced_message.call_args[1]["message"]
        self.assertIn("수동 포지션 스마트 모니터링 알림", sent_msg)
        self.assertIn("매매 간섭은 일절 발생하지 않습니다", sent_msg)

    def test_ignores_bot_managed_positions(self):
        """봇이 직접 매수하여 관리 중인 포지션은 수동 관측 알림 대상에서 제외"""
        balances = {
            "KRW": {"balance": 50000.0, "locked": 0.0},
            "XRP": {"balance": 100.0, "locked": 0.0},
        }
        order_journal = MagicMock()
        order_journal.orders = [
            {"market": "KRW-XRP", "side": "bid", "status": "FILLED", "volume": 100.0, "created_at": 100.0}
        ]

        events = self.observer.observe_manual_positions(balances, order_journal=order_journal)
        self.assertEqual(len(events), 0)
        self.assertFalse(self.mock_telegram.send_debounced_message.called)

    def test_cooldown_prevents_duplicate_notifications(self):
        """동일 종목에 대해 쿨다운(2시간) 동안 중복 알림이 발송되지 않음"""
        balances = {
            "KRW": {"balance": 50000.0, "locked": 0.0},
            "US": {"balance": 1000.0, "locked": 0.0},
        }
        order_journal = MagicMock()
        order_journal.orders = []

        daily_candles = [
            {"trade_price": 9.0, "high_price": 9.5},
            {"trade_price": 9.8, "high_price": 10.0},
        ] + [{"trade_price": 9.5, "high_price": 9.6} for _ in range(25)]
        self.mock_ex.get_candles.return_value = daily_candles

        # 1차 실행: 알림 발송
        events1 = self.observer.observe_manual_positions(balances, order_journal=order_journal)
        self.assertEqual(len(events1), 1)
        self.assertEqual(self.mock_telegram.send_debounced_message.call_count, 1)

        # 2차 실행: 쿨다운 미도래로 스킵
        events2 = self.observer.observe_manual_positions(balances, order_journal=order_journal)
        self.assertEqual(len(events2), 0)
        self.assertEqual(self.mock_telegram.send_debounced_message.call_count, 1)


if __name__ == "__main__":
    unittest.main()
