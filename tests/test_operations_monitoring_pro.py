"""
Operations and Monitoring Pro Tests (Diagnostics Telemetry, Extended Telegram Commands)
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from bot_controller import BotController
from order_safety import OrderJournal, SafeOrderExecutor
from risk_manager import DailyRiskManager, TrailingStopTracker
from telegram_alert import TelegramAlert
from trade_memory import TradeMemoryManager


class OperationsMonitoringProTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_ops_pro_")
        self.mock_exchange = MagicMock()
        self.mock_exchange.get_balances.return_value = {"KRW": {"balance": 1000000.0, "locked": 0.0}}
        self.mock_exchange.get_current_price.return_value = 100000000.0
        self.mock_exchange.get_korean_name.return_value = "비트코인"

        self.journal = OrderJournal(data_dir=self.test_dir)
        self.executor = SafeOrderExecutor(self.journal)
        self.risk_manager = DailyRiskManager(data_dir=self.test_dir)
        self.trailing_tracker = TrailingStopTracker(data_dir=self.test_dir)
        self.trade_memory = TradeMemoryManager(data_dir=self.test_dir)
        self.telegram = MagicMock()

        self.is_paused = False

        self.controller = BotController(
            exchange_factory=lambda: self.mock_exchange,
            order_executor=self.executor,
            order_journal=self.journal,
            risk_manager=self.risk_manager,
            trailing_tracker=self.trailing_tracker,
            trade_memory=self.trade_memory,
            telegram=self.telegram,
            get_is_paused=lambda: self.is_paused,
            set_is_paused=self._set_paused,
            exchange_name="업비트",
            web_port=7980,
        )

    def _set_paused(self, val: bool):
        self.is_paused = val

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_bot_controller_diagnostics_data(self):
        """실시간 진단 텔레메트리 데이터 정확도 검증"""
        diag = self.controller.get_diagnostics_data()
        self.assertEqual(diag["exchange"], "업비트")
        self.assertGreaterEqual(diag["uptime_seconds"], 0)
        self.assertIn("uptime_str", diag)
        self.assertIn("pid", diag)
        self.assertIn("active_threads", diag)
        self.assertFalse(diag["kill_switch_active"])
        self.assertAlmostEqual(diag["risk_scale_factor"], 1.0)
        self.assertEqual(diag["web_port"], 7980)

    def test_bot_controller_diagnostics_message(self):
        """텔레그램 /diag 상세 진단 메시지 포맷팅 검증"""
        msg = self.controller.get_diagnostics_message()
        self.assertIn("업비트 AI 트레이딩 시스템 정밀 진단 리포트", msg)
        self.assertIn("시스템 Uptime", msg)
        self.assertIn("일일 킬스위치", msg)
        self.assertIn("웹 대시보드 포트", msg)
        self.assertIn("http://localhost:7980", msg)

    def test_bot_controller_trades_summary_message(self):
        """텔레그램 /trades 당일 거래 및 슬리피지 요약 메시지 검증"""
        # 거래 기록 추가
        self.trade_memory.record_completed_trade(
            market="KRW-BTC",
            side="TRAILING_STOP",
            entry_price=90000000.0,
            exit_price=93000000.0,
            filled_volume=0.01,
            fee=370.0,
            slippage=0.001,  # 10 bps
            pnl_pct=3.33,
            pnl_krw=29630.0,
            reason="TRAILING_STOP",
            timestamp="2026-08-25 17:30:00",
        )

        msg = self.controller.get_trades_summary_message()
        self.assertIn("최근 매매 및 체결 품질 내역", msg)
        self.assertIn("KRW-BTC", msg)
        self.assertIn("TRAILING_STOP", msg)
        self.assertIn("슬리피지", msg)

    def test_telegram_listener_command_dispatch(self):
        """TelegramAlert 리스너에 diag_callback과 trades_callback 정상 등록 검증"""
        alert = TelegramAlert(bot_token="", chat_id="")
        diag_cb = MagicMock(return_value="진단 완료")
        trades_cb = MagicMock(return_value="거래 완료")

        alert.start_command_listener(
            diag_callback=diag_cb,
            trades_callback=trades_cb,
        )

        self.assertEqual(alert._diag_callback, diag_cb)
        self.assertEqual(alert._trades_callback, trades_cb)


if __name__ == "__main__":
    unittest.main()
