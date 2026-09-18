"""거래소별 DB 격리와 확인 체결 경계를 외부 파일 없이 검증한다."""

import os
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import db_manager
from order_safety import OrderFillProcessor, OrderStatus


class _InMemoryJournal:
    """파일 I/O 없이 체결 처리기의 상태 전이를 검증하는 최소 저널이다."""

    def __init__(self):
        self._lock = threading.RLock()
        self.orders = [{
            "client_order_id": "sell-1", "market": "KRW-BTC", "side": "ask",
            "position_id": "position-1", "avg_buy_price": 100.0, "expected_price": 110.0,
            "processed_executed_volume": 0.0, "processed_fee": 0.0,
            "executed_volume": 0.0, "remaining_volume": 1.0, "exit_reason": "STOP_LOSS",
        }]

    def mark(self, client_order_id, status, **fields):
        # 실제 저널과 같이 체결 처리 후 상태와 누적 필드를 갱신한다.
        order = self.orders[0]
        order["status"] = status
        order.update(fields)

    def get_entry_order_for_exit(self, _order):
        return None


class StorageAndFillBoundaryTests(unittest.TestCase):
    def test_database_manager_is_cached_per_normalized_path(self):
        """빗썸/업비트 DB 요청은 서로 다른 인스턴스이며 같은 경로만 재사용한다."""
        created_paths = []
        self.addCleanup(db_manager._DB_MANAGER_BY_PATH.clear)

        class FakeDatabaseManager:
            def __init__(self, path):
                created_paths.append(path)
                self.db_path = path

        with patch.object(db_manager, "DatabaseManager", FakeDatabaseManager):
            db_manager._DB_MANAGER_BY_PATH.clear()
            bithumb_path = os.path.join("C:\\bot", "data", "trading.db")
            upbit_path = os.path.join("C:\\bot", "data", "upbit", "trading.db")
            first = db_manager.get_db_manager(bithumb_path)
            same = db_manager.get_db_manager(os.path.join("C:\\bot", "data", ".", "trading.db"))
            other = db_manager.get_db_manager(upbit_path)

        self.assertIs(first, same)
        self.assertIsNot(first, other)
        self.assertEqual(len(created_paths), 2)

    def test_exit_cooldown_is_recorded_only_after_confirmed_fill_delta(self):
        """ACK가 아닌 REST 확정 매도 체결 증가분만 쿨다운과 실현손익을 갱신한다."""
        journal = _InMemoryJournal()
        cooldown = MagicMock()
        risk = MagicMock()
        processor = OrderFillProcessor(journal, risk_manager=risk, cooldown_manager=cooldown)

        # 체결량이 0이면 어떤 청산 부수상태도 갱신하지 않는다.
        processor.process_order_fill("sell-1", OrderStatus.ACKNOWLEDGED, 0.0, avg_price=110.0)
        cooldown.record_exit.assert_not_called()
        risk.add_realized_trade.assert_not_called()

        # REST 대사가 확인한 체결 증가분에서만 상태를 갱신한다.
        processor.process_order_fill("sell-1", OrderStatus.FILLED, 1.0, avg_price=110.0, remaining_volume=0.0)
        cooldown.record_exit.assert_called_once_with("KRW-BTC", "STOP_LOSS", exit_price=110.0)
        risk.add_realized_trade.assert_called_once()

    def test_ai_emergency_exit_reason_refined_to_korean(self):
        """AI_EMERGENCY_EXIT 사유가 확정 체결 시 쿨다운에는 원본 코드로, 거래 메모리에는 한글 레이블로 반영된다."""
        journal = _InMemoryJournal()
        journal.orders[0]["exit_reason"] = "AI_EMERGENCY_EXIT"
        journal.orders[0]["avg_buy_price"] = 100.0
        cooldown = MagicMock()
        risk = MagicMock()
        trade_mem = MagicMock()
        processor = OrderFillProcessor(journal, risk_manager=risk, cooldown_manager=cooldown, trade_memory=trade_mem)

        # 손실 상태의 AI 비상탈출 (90원에 체결)
        processor.process_order_fill("sell-1", OrderStatus.FILLED, 1.0, avg_price=90.0, remaining_volume=0.0)
        cooldown.record_exit.assert_called_once_with("KRW-BTC", "AI_EMERGENCY_EXIT", exit_price=90.0)
        self.assertEqual(trade_mem.record_completed_trade.call_args.kwargs["reason"], "AI 긴급 비상탈출")

        # 이익 상태의 AI 비상탈출 (110원에 체결)
        cooldown.reset_mock()
        trade_mem.reset_mock()
        journal.orders[0]["processed_executed_volume"] = 0.0
        processor.process_order_fill("sell-1", OrderStatus.FILLED, 1.0, avg_price=110.0, remaining_volume=0.0)
        cooldown.record_exit.assert_called_once_with("KRW-BTC", "AI_EMERGENCY_EXIT", exit_price=110.0)
        self.assertEqual(trade_mem.record_completed_trade.call_args.kwargs["reason"], "AI 긴급 익절탈출")

    def test_reconcile_idempotent_filled_order_does_not_warn(self):
        """이미 FILLED 상태인 주문의 후속 대사는 경고 없이 FILLED를 유지하고, 미체결 FILLED는 거부한다."""
        journal = _InMemoryJournal()
        processor = OrderFillProcessor(journal)

        # 1. 0체결 상태에서 FILLED 요구 -> 거부 및 RECONCILIATION_PENDING
        with self.assertLogs("order_safety.fill_processor", level="WARNING") as cm:
            res = processor.process_order_fill("sell-1", OrderStatus.FILLED, 0.0, avg_price=110.0)
            self.assertIn("체결 증가분 없이 FILLED 상태를 거부했습니다", cm.output[0])
            self.assertEqual(res["status"], OrderStatus.RECONCILIATION_PENDING)

        # 2. 정상 전량 체결로 FILLED 승격
        res_fill = processor.process_order_fill("sell-1", OrderStatus.FILLED, 1.0, avg_price=110.0, remaining_volume=0.0)
        self.assertEqual(res_fill["status"], OrderStatus.FILLED)
        self.assertEqual(res_fill["fill_delta"], 1.0)

        # 3. 이미 FILLED된 주문에 대해 후속 REST 대사가 다시 동일 수량으로 들어올 때: WARNING 없이 통과
        with self.assertLogs("order_safety.fill_processor", level="DEBUG") as cm_debug:
            res_dup = processor.process_order_fill("sell-1", OrderStatus.FILLED, 1.0, avg_price=110.0, remaining_volume=0.0)
            self.assertEqual(res_dup["status"], OrderStatus.FILLED)
            self.assertEqual(res_dup["fill_delta"], 0.0)
            self.assertTrue(any("이미 FILLED 완료된 주문" in o for o in cm_debug.output))


    def test_insert_trade_canonical_field_mapping(self):
        """insert_trade 호출 시 레거시 키(reason, btc_regime, bars_held)가 표준 컬럼으로 매핑된다."""
        import tempfile
        import sqlite3
        with tempfile.TemporaryDirectory() as tmpdir:
            test_db = os.path.join(tmpdir, "test_trading.db")
            mgr = db_manager.get_db_manager(test_db)
            trade_sample = {
                "market": "KRW-ETH",
                "timestamp": "2026-09-06 12:00:00",
                "entry_price": 3000000.0,
                "exit_price": 3090000.0,
                "pnl_krw": 90000.0,
                "pnl_pct": 3.0,
                "side": "트레일링 익절",
                "reason": "1차 목표 도달 트레일링",
                "btc_regime": "BULL_TREND",
                "bars_held": 6,
            }
            row_id = mgr.insert_trade("upbit", trade_sample)
            self.assertGreater(row_id, 0)

            trades = mgr.get_trades("upbit", "KRW-ETH")
            self.assertEqual(len(trades), 1)
            row = trades[0]
            self.assertEqual(row["exchange"], "upbit")
            self.assertEqual(row["market"], "KRW-ETH")
            self.assertIn("트레일링", row["exit_reason"])
            self.assertEqual(row["market_regime"], "BULL_TREND")
            self.assertEqual(row["hold_duration_min"], 30)  # 6봉 * 5분 = 30분
            self.assertEqual(row["exit_time"], "2026-09-06 12:00:00")


if __name__ == "__main__":
    unittest.main()
