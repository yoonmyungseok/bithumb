import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from order_safety import OrderJournal, OrderStatus, OrderFillProcessor


class TestBuyFillAlert(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.journal_path = os.path.join(self.temp_dir.name, "order_journal.json")
        self.journal = OrderJournal(path=self.journal_path)
        self.telegram = MagicMock()
        self.sheets = MagicMock()
        self.trailing_tracker = MagicMock()
        self.risk_manager = MagicMock()
        self.trade_memory = MagicMock()

        self.processor = OrderFillProcessor(
            order_journal=self.journal,
            risk_manager=self.risk_manager,
            trade_memory=self.trade_memory,
            trailing_tracker=self.trailing_tracker,
            telegram=self.telegram,
            sheets=self.sheets,
            send_fill_alerts=True,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_buy_fill_sends_telegram_alert(self):
        # 1. 매수 주문 등록
        client_id = self.journal.record_intent(
            market="KRW-BTC",
            side="bid",
            ord_type="limit",
            price=100_000_000.0,
            volume=0.01,
            position_id="pos-001",
            expected_price=100_000_000.0,
            entry_strategy_snapshot={
                "korean_name": "비트코인",
                "target_price": 105_000_000.0,
                "stop_loss": 97_000_000.0,
                "alpha_score": 85,
                "exchange": "빗썸",
                "entry_reason": "골든크로스 및 수급 급증",
            },
            exchange="bithumb",
        )

        # 2. 체결 처리 (완전 체결)
        res = self.processor.process_order_fill(
            order_identifier=client_id,
            status=OrderStatus.FILLED,
            executed_volume=0.01,
            avg_price=100_000_000.0,
            fee=400.0,
            remaining_volume=0.0,
            exchange_uuid="uuid-001",
            korean_name="비트코인",
        )

        self.assertTrue(res["processed"])
        self.assertEqual(res["fill_delta"], 0.01)

        # 텔레그램 알림 발송 검증
        self.telegram.send_message.assert_called_once()
        msg = self.telegram.send_message.call_args[0][0]
        self.assertIn("비트코인(KRW-BTC) 매수 체결 완료", msg)
        self.assertIn("100,000,000.00 KRW", msg)
        self.assertIn("1,000,000 KRW", msg)
        self.assertIn("목표가: 105,000,000.00 KRW", msg)
        self.assertIn("손절가: 97,000,000.00 KRW", msg)
        self.assertIn("85점", msg)

        # 주문 저널 영속 저장 검증 (아침 9시 구글 시트 일괄 배치 동기화 소스)
        saved_order = next((o for o in self.journal.orders if o.get("client_order_id") == client_id), None)
        self.assertIsNotNone(saved_order)
        self.assertEqual(saved_order.get("status"), OrderStatus.FILLED)
        self.assertEqual(saved_order.get("executed_volume"), 0.01)

    def test_buy_fill_photo_alert(self):
        # 차트 이미지가 전달된 경우 send_photo 호출 확인
        client_id = self.journal.record_intent(
            market="KRW-ETH",
            side="bid",
            ord_type="limit",
            price=4_000_000.0,
            volume=0.5,
            entry_strategy_snapshot={
                "korean_name": "이더리움",
                "exchange": "업비트",
            },
            exchange="upbit",
        )

        fake_chart = b"fake_chart_bytes"
        self.processor.process_order_fill(
            order_identifier=client_id,
            status=OrderStatus.FILLED,
            executed_volume=0.5,
            avg_price=4_000_000.0,
            fee=800.0,
            remaining_volume=0.0,
            chart_img=fake_chart,
        )

        self.telegram.send_photo.assert_called_once()
        call_args, call_kwargs = self.telegram.send_photo.call_args
        self.assertEqual(call_args[0], fake_chart)
        self.assertIn("이더리움(KRW-ETH) 매수 체결 완료", call_kwargs["caption"])

    def test_buy_fill_idempotent_no_duplicate_alert(self):
        # 중복 체결 호출 시 fill_delta == 0 이므로 중복 알림이 발송되지 않는지 확인
        client_id = self.journal.record_intent(
            market="KRW-SOL",
            side="bid",
            ord_type="limit",
            price=200_000.0,
            volume=1.0,
            exchange="bithumb",
        )

        # 1차 호출 (체결)
        self.processor.process_order_fill(
            order_identifier=client_id,
            status=OrderStatus.FILLED,
            executed_volume=1.0,
            avg_price=200_000.0,
            fee=80.0,
            remaining_volume=0.0,
        )
        self.assertEqual(self.telegram.send_message.call_count, 1)

        # 2차 중복 호출
        res2 = self.processor.process_order_fill(
            order_identifier=client_id,
            status=OrderStatus.FILLED,
            executed_volume=1.0,
            avg_price=200_000.0,
            fee=80.0,
            remaining_volume=0.0,
        )
        self.assertFalse(res2["processed"])
        self.assertEqual(res2["fill_delta"], 0.0)
        # 알림 횟수가 여전히 1회인지 확인
        self.assertEqual(self.telegram.send_message.call_count, 1)


if __name__ == "__main__":
    unittest.main()
