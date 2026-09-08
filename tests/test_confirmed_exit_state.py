"""확정 체결 전 포지션 보호 상태 보존 회귀 테스트."""

import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from order_safety import OrderFillProcessor, OrderStatus


class _MemoryJournal:
    """파일/SQLite 없이 fill_processor의 체결 경계만 검증하는 저널 대역."""

    def __init__(self, exit_reason):
        self._lock = threading.RLock()
        self.orders = [{
            "client_order_id": "exit-1", "market": "KRW-TEST", "side": "ask",
            "position_id": "bithumb:KRW-TEST:confirmed", "avg_buy_price": 100.0,
            "expected_price": 110.0, "processed_executed_volume": 0.0,
            "processed_fee": 0.0, "remaining_volume": 4.0,
            "exit_reason": exit_reason,
        }]

    def mark(self, _client_order_id, status, **fields):
        self.orders[0]["status"] = status
        self.orders[0].update(fields)

    def get_entry_order_for_exit(self, _order):
        return None


class _TrackerSpy:
    """확정 체결에서만 변경되어야 하는 포지션 상태를 최소 모델로 검증한다."""

    def __init__(self):
        self.stage = 0
        self.mode = "SWING"
        self.peaks = {"KRW-TEST": 120.0}

    def mark_partial_take_profit_filled(self, _market, stage, fill_delta):
        if fill_delta > 0.0 and stage > self.stage:
            self.stage = stage
            return True
        return False

    def clear(self, market):
        self.peaks.pop(market, None)
        self.mode = "SCALP"


class ConfirmedExitStateTests(unittest.TestCase):
    """ACK·OPEN·UNKNOWN·부분 체결에서 익절/트레일링 상태 유실을 막는다."""

    def setUp(self):
        self.market = "KRW-TEST"

    def _record_exit(self, reason="PARTIAL_TP_1"):
        self.tracker = _TrackerSpy()
        self.journal = _MemoryJournal(reason)
        self.processor = OrderFillProcessor(self.journal, trailing_tracker=self.tracker)
        return "exit-1"

    def test_ack_open_unknown_and_rejection_do_not_advance_partial_take_profit(self):
        """체결 수량 0인 모든 미확정 상태는 본전 보호와 스윙 모드를 보존한다."""
        order_id = self._record_exit()
        for status in (OrderStatus.ACKNOWLEDGED, OrderStatus.OPEN, OrderStatus.UNKNOWN, OrderStatus.REJECTED):
            self.processor.process_order_fill(order_id, status, 0.0, avg_price=110.0, remaining_volume=4.0)

        self.assertEqual(self.tracker.stage, 0)
        self.assertEqual(self.tracker.mode, "SWING")
        self.assertEqual(self.tracker.peaks[self.market], 120.0)

    def test_confirmed_partial_fill_advances_stage_without_clearing_position(self):
        """부분 체결 델타는 1차 단계만 확정하고 트레일링·전략 상태는 유지한다."""
        order_id = self._record_exit()
        result = self.processor.process_order_fill(
            order_id, OrderStatus.PARTIALLY_FILLED, 1.0, avg_price=110.0, remaining_volume=3.0,
        )

        self.assertTrue(result["processed"])
        self.assertEqual(self.tracker.stage, 1)
        self.assertEqual(self.tracker.mode, "SWING")
        self.assertEqual(self.tracker.peaks[self.market], 120.0)

    def test_trailing_ack_and_partial_fill_do_not_clear_until_confirmed_full_exit(self):
        """트레일링 주문은 ACK와 부분 체결에서 보존하고 FILLED에서만 정리한다."""
        order_id = self._record_exit("TRAILING_STOP")
        self.processor.process_order_fill(order_id, OrderStatus.ACKNOWLEDGED, 0.0, avg_price=105.0, remaining_volume=4.0)
        self.processor.process_order_fill(order_id, OrderStatus.PARTIALLY_FILLED, 1.0, avg_price=105.0, remaining_volume=3.0)
        self.assertEqual(self.tracker.mode, "SWING")
        self.assertEqual(self.tracker.peaks[self.market], 120.0)

        self.processor.process_order_fill(order_id, OrderStatus.FILLED, 4.0, avg_price=105.0, remaining_volume=0.0)
        self.assertEqual(self.tracker.mode, "SCALP")
        self.assertNotIn(self.market, self.tracker.peaks)


if __name__ == "__main__":
    unittest.main()
