"""대시보드 일일 자산변동 및 성과 이력 표시 기능 단위 테스트."""

from datetime import datetime, timezone
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from bot_controller import BotController
from dashboard_server import UnifiedDashboardServer
from db_manager import DatabaseManager


class TestDailyStatsDashboard(unittest.TestCase):
    """일일 자산변동 DB 조회, 봇 컨트롤러 주입, 대시보드 집계 테스트."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_trading.db")
        self.db = DatabaseManager(db_path=self.db_path)

    def tearDown(self):
        if hasattr(self.db, "close"):
            self.db.close()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_get_daily_stats_history_calculation(self):
        """DB에서 일자별 통계가 역순으로 조회되고 pnl_pct 및 win_rate가 정확히 계산되는지 확인."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO daily_stats (
                    exchange, date, start_equity, realized_pnl_krw,
                    total_trades, win_trades, consecutive_losses,
                    kill_switch_active, updated_at
                ) VALUES
                ('bithumb', '2026-08-30', 1000000.0, 50000.0, 10, 7, 0, 0, '2026-08-30 23:59:59'),
                ('bithumb', '2026-08-31', 1050000.0, -21000.0, 5, 2, 2, 0, '2026-08-31 23:59:59'),
                ('bithumb', '2026-09-01', 1029000.0, 0.0, 0, 0, 0, 1, '2026-09-01 23:59:59')
                """
            )
            conn.commit()

        history = self.db.get_daily_stats_history(exchange="bithumb", limit=10)
        self.assertEqual(len(history), 3)

        self.assertEqual(history[0]["date"], "2026-09-01")
        self.assertEqual(history[0]["kill_switch_active"], True)
        self.assertEqual(history[0]["pnl_pct"], 0.0)
        self.assertEqual(history[0]["win_rate"], 0.0)

        self.assertEqual(history[1]["date"], "2026-08-31")
        self.assertEqual(history[1]["realized_pnl_krw"], -21000)
        self.assertEqual(history[1]["pnl_pct"], -2.0)
        self.assertEqual(history[1]["win_rate"], 40.0)

        self.assertEqual(history[2]["date"], "2026-08-30")
        self.assertEqual(history[2]["realized_pnl_krw"], 50000)
        self.assertEqual(history[2]["pnl_pct"], 5.0)
        self.assertEqual(history[2]["win_rate"], 70.0)

    def test_bot_controller_dashboard_data_includes_daily_history(self):
        """BotController.get_dashboard_data에 daily_stats_history가 포함되고 오늘자 데이터가 실시간 동기화되는지 확인."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO daily_stats (
                    exchange, date, start_equity, realized_pnl_krw,
                    total_trades, win_trades, consecutive_losses,
                    kill_switch_active, updated_at
                ) VALUES
                ('bithumb', '2026-08-31', 1000000.0, 30000.0, 4, 3, 0, 0, '2026-08-31 23:59:59')
                """
            )
            conn.commit()

        bot = BotController.__new__(BotController)
        bot.exchange_name = "bithumb"
        bot.db = self.db
        bot._last_dashboard_fetch_ts = 0.0
        bot.latest_dashboard_data = {}
        bot._dashboard_cache_lock = threading.Lock()
        bot.risk_manager = MagicMock()
        bot.risk_manager.db = self.db
        bot.risk_manager.exchange = "bithumb"
        bot.risk_manager.current_date_str = today
        bot.risk_manager.daily_start_equity = 1030000.0
        bot.risk_manager.realized_pnl_krw = 15000.0
        bot.risk_manager.daily_realized_pnl = 15000.0
        bot.risk_manager.total_trades_today = 3
        bot.risk_manager.win_trades_today = 2
        bot.risk_manager.kill_switch_active = False
        bot.risk_manager.daily_kill_switch_active = False
        bot.risk_manager.kill_switch_latched_date = ""

        mock_ex = MagicMock()
        mock_ex.get_balances = MagicMock(return_value={"KRW": {"balance": 1030000.0, "locked": 0.0}})
        bot.get_exchange = MagicMock(return_value=mock_ex)
        bot.get_is_paused = MagicMock(return_value=False)
        bot.restore_missing_position_strategies = MagicMock()
        bot.latest_strategies = {}
        bot.trailing_tracker = MagicMock()
        bot.trailing_tracker.get_position_dashboard_state = MagicMock(return_value={})
        bot.trade_memory = MagicMock()
        bot.trade_memory.get_recent_trades = MagicMock(return_value=[])
        bot.trade_memory.get_position_level_stats = MagicMock(return_value={})

        bot.order_journal = MagicMock()
        bot.order_journal.orders = []
        bot.order_journal.get_recent_orders = MagicMock(return_value=[])

        bot.order_safety = MagicMock()
        bot.order_safety.get_recent_orders = MagicMock(return_value=[])
        bot.order_safety.get_safety_summary = MagicMock(return_value={
            "entry_ready": True,
            "entry_block_reasons": [],
            "order_status_counts": {},
            "feed": {"is_healthy": True, "status": "DATA_AVAILABLE"},
        })
        bot._build_safety_data = MagicMock(return_value={
            "entry_ready": True,
            "entry_block_reasons": [],
            "order_status_counts": {},
            "feed": {"is_healthy": True, "status": "DATA_AVAILABLE"},
        })
        bot.active_policy = {}

        data = bot.get_dashboard_data()
        self.assertIn("daily_stats_history", data)
        history = data["daily_stats_history"]
        self.assertGreaterEqual(len(history), 2)

        today_entry = history[0]
        self.assertEqual(today_entry["date"], today)
        self.assertEqual(today_entry["start_equity"], 1030000)
        self.assertEqual(today_entry["realized_pnl_krw"], 15000)
        self.assertEqual(today_entry["total_trades"], 3)
        self.assertEqual(today_entry["win_trades"], 2)

    def test_unified_dashboard_aggregation(self):
        """UnifiedDashboardServer가 빗썸과 업비트의 일별 통계를 combined에 정확히 합산하는지 검증."""
        server = UnifiedDashboardServer()

        bithumb_sample = {
            "online": True,
            "total_equity": 1500000.0,
            "krw_available": 500000.0,
            "daily_start_equity": 1400000.0,
            "daily_pnl_krw": 100000.0,
            "realized_pnl_krw": 80000.0,
            "total_trades": 5,
            "win_trades": 4,
            "positions": [],
            "candidates": [],
            "recent_trades": [],
            "recent_orders": [],
            "daily_stats_history": [
                {
                    "date": "2026-09-02",
                    "start_equity": 1400000,
                    "realized_pnl_krw": 80000,
                    "total_trades": 5,
                    "win_trades": 4,
                    "kill_switch_active": False,
                },
                {
                    "date": "2026-09-01",
                    "start_equity": 1300000,
                    "realized_pnl_krw": 100000,
                    "total_trades": 8,
                    "win_trades": 6,
                    "kill_switch_active": False,
                },
            ],
        }

        upbit_sample = {
            "online": True,
            "total_equity": 2500000.0,
            "krw_available": 1000000.0,
            "daily_start_equity": 2600000.0,
            "daily_pnl_krw": -100000.0,
            "realized_pnl_krw": -50000.0,
            "total_trades": 3,
            "win_trades": 1,
            "positions": [],
            "candidates": [],
            "recent_trades": [],
            "recent_orders": [],
            "daily_stats_history": [
                {
                    "date": "2026-09-02",
                    "start_equity": 2600000,
                    "realized_pnl_krw": -50000,
                    "total_trades": 3,
                    "win_trades": 1,
                    "kill_switch_active": False,
                },
                {
                    "date": "2026-08-31",
                    "start_equity": 2500000,
                    "realized_pnl_krw": 50000,
                    "total_trades": 4,
                    "win_trades": 3,
                    "kill_switch_active": False,
                },
            ],
        }

        with patch.object(server, "fetch_exchange_status") as mock_fetch:
            mock_fetch.side_effect = lambda url, name: bithumb_sample if name == "bithumb" else upbit_sample
            status = server.get_aggregated_status()

            combined = status.get("combined", {})
            self.assertIn("daily_stats_history", combined)
            comb_hist = combined["daily_stats_history"]

            self.assertEqual(len(comb_hist), 3)
            self.assertEqual(comb_hist[0]["date"], "2026-09-02")
            self.assertEqual(comb_hist[1]["date"], "2026-09-01")
            self.assertEqual(comb_hist[2]["date"], "2026-08-31")

            day_today = comb_hist[0]
            self.assertEqual(day_today["start_equity"], 4000000)
            self.assertEqual(day_today["realized_pnl_krw"], 30000)
            self.assertEqual(day_today["total_trades"], 8)
            self.assertEqual(day_today["win_trades"], 5)
            self.assertAlmostEqual(day_today["pnl_pct"], 0.75, places=2)
            self.assertAlmostEqual(day_today["win_rate"], 62.5, places=1)


if __name__ == "__main__":
    unittest.main()
