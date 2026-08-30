import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from bot_controller import BotController
from order_safety import OrderJournal, SafeOrderExecutor
from risk_manager import DailyRiskManager, TrailingStopTracker, build_candidates_data, build_positions_data
from telegram_alert import TelegramAlert
from trade_memory import TradeMemoryManager
from web_server import DashboardWebServer


class TestCandidateStrategiesDashboard(unittest.TestCase):
    def setUp(self):
        self.mock_api = MagicMock()
        self.mock_api.get_current_price.side_effect = lambda m: 100000000.0 if m == "KRW-BTC" else (4000000.0 if m == "KRW-ETH" else 200000.0)
        self.mock_api.get_korean_name.side_effect = lambda m: "비트코인" if m == "KRW-BTC" else ("이더리움" if m == "KRW-ETH" else "솔라나")

    def test_build_candidates_data(self):
        balances = {
            "KRW": {"balance": 1000000.0, "locked": 0.0},
            "BTC": {"balance": 0.01, "locked": 0.0, "avg_buy_price": 95000000.0},
        }
        strategies = {
            "KRW-BTC": {
                "market": "KRW-BTC",
                "korean_name": "비트코인",
                "action": "HOLD",
                "current_price": 100000000.0,
                "target_price": 105000000.0,
                "stop_loss": 97000000.0,
                "alpha_score": 75,
                "allow_buy": False,
                "reason": "기보유 감시 중",
            },
            "KRW-ETH": {
                "market": "KRW-ETH",
                "korean_name": "이더리움",
                "action": "BUY",
                "current_price": 4000000.0,
                "target_price": 4200000.0,
                "stop_loss": 3900000.0,
                "alpha_score": 88,
                "allow_buy": True,
                "factor_breakdown": {"mtf_score": 15, "vwap_score": 15, "macd_score": 15, "rsi_score": 15, "bb_score": 15, "orderflow_score": 13},
                "reason": "🔥알파 88점 A+ 특급 셋업",
            },
            "KRW-SOL": {
                "market": "KRW-SOL",
                "korean_name": "솔라나",
                "action": "HOLD",
                "current_price": 200000.0,
                "target_price": 210000.0,
                "stop_loss": 195000.0,
                "alpha_score": 62,
                "allow_buy": False,
                "factor_breakdown": {"mtf_score": 10, "vwap_score": 8, "macd_score": 8, "rsi_score": 10, "bb_score": 10, "orderflow_score": 10},
                "reason": "1차 퀀트 관망 대기",
            },
        }

        candidates = build_candidates_data(balances, self.mock_api, strategies)
        
        cand_markets = [c["market"] for c in candidates]
        self.assertNotIn("KRW-BTC", cand_markets)
        self.assertIn("KRW-ETH", cand_markets)
        self.assertIn("KRW-SOL", cand_markets)

        self.assertEqual(candidates[0]["market"], "KRW-ETH")
        self.assertTrue(candidates[0]["allow_buy"])
        self.assertEqual(candidates[0]["alpha_score"], 88)
        self.assertGreater(candidates[0]["risk_reward_ratio"], 0)

    def test_build_positions_data_with_rich_metrics(self):
        balances = {
            "KRW": {"balance": 1000000.0, "locked": 0.0},
            "BTC": {"balance": 0.01, "locked": 0.0, "avg_buy_price": 95000000.0},
        }
        strategies = {
            "KRW-BTC": {
                "market": "KRW-BTC",
                "korean_name": "비트코인",
                "action": "HOLD",
                "current_price": 100000000.0,
                "target_price": 105000000.0,
                "stop_loss": 97000000.0,
                "alpha_score": 78,
                "reason": "보유 중 정상 감시",
            },
        }

        positions = build_positions_data(balances, self.mock_api, strategies)
        self.assertEqual(len(positions), 1)
        btc_pos = positions[0]
        self.assertEqual(btc_pos["market"], "KRW-BTC")
        self.assertEqual(btc_pos["alpha_score"], 78)
        self.assertEqual(btc_pos["action"], "HOLD")
        self.assertGreater(btc_pos["pnl_pct"], 0)

    def test_bot_controller_get_dashboard_data_structure(self):
        mock_executor = MagicMock(spec=SafeOrderExecutor)
        mock_journal = MagicMock(spec=OrderJournal)
        mock_journal.orders = []
        mock_journal.get_recent_orders.return_value = []
        
        mock_risk = MagicMock(spec=DailyRiskManager)
        mock_risk.daily_start_equity = 1000000.0
        mock_risk.total_trades_today = 5
        mock_risk.win_trades_today = 4
        mock_risk.realized_pnl_krw = 50000.0
        mock_risk.kill_switch_active = False

        mock_trailing = MagicMock(spec=TrailingStopTracker)
        mock_memory = MagicMock(spec=TradeMemoryManager)
        mock_memory.get_recent_trades.return_value = []
        mock_memory.get_position_level_stats.return_value = {"position_win_rate_pct": 80.0, "total_positions": 5}
        mock_telegram = MagicMock(spec=TelegramAlert)

        self.mock_api.get_balances.return_value = {
            "KRW": {"balance": 1000000.0, "locked": 0.0},
            "BTC": {"balance": 0.01, "locked": 0.0, "avg_buy_price": 95000000.0},
        }

        strategies = {
            "KRW-BTC": {"market": "KRW-BTC", "action": "HOLD", "current_price": 100000000.0, "alpha_score": 75},
            "KRW-ETH": {"market": "KRW-ETH", "action": "BUY", "current_price": 4000000.0, "alpha_score": 85, "allow_buy": True},
        }

        controller = BotController(
            exchange_factory=lambda: self.mock_api,
            order_executor=mock_executor,
            order_journal=mock_journal,
            risk_manager=mock_risk,
            trailing_tracker=mock_trailing,
            trade_memory=mock_memory,
            telegram=mock_telegram,
            get_is_paused=lambda: False,
            set_is_paused=lambda p: None,
            latest_strategies=strategies,
        )

        data = controller.get_dashboard_data()
        self.assertIn("positions", data)
        self.assertIn("candidates", data)
        self.assertIn("btc_regime", data)
        self.assertIn("btc_regime_desc", data)
        self.assertEqual(len(data["positions"]), 1)
        self.assertEqual(len(data["candidates"]), 1)
        self.assertEqual(data["candidates"][0]["market"], "KRW-ETH")

    def test_restore_held_position_strategy_from_filled_buy_snapshot(self):
        mock_journal = MagicMock(spec=OrderJournal)
        mock_journal.orders = [{
            "market": "KRW-GRVT",
            "side": "bid",
            "status": "FILLED",
            "avg_price": 250.0,
            "entry_strategy_snapshot": {
                "target_price": 253.98,
                "stop_loss": 245.26,
                "alpha_score": 77,
                "entry_reason": "저장된 진입 근거",
                "indicators": {"rsi": 50.0},
            },
        }]
        mock_journal._lock = MagicMock()

        controller = BotController(
            exchange_factory=lambda: self.mock_api,
            order_executor=MagicMock(spec=SafeOrderExecutor),
            order_journal=mock_journal,
            risk_manager=MagicMock(spec=DailyRiskManager),
            trailing_tracker=MagicMock(spec=TrailingStopTracker),
            trade_memory=MagicMock(spec=TradeMemoryManager),
            telegram=MagicMock(spec=TelegramAlert),
            get_is_paused=lambda: False,
            set_is_paused=lambda _: None,
            latest_strategies={},
        )

        self.assertEqual(controller.restore_missing_position_strategies(["KRW-GRVT"]), 1)
        strategy = controller.latest_strategies["KRW-GRVT"]
        self.assertEqual(strategy["action"], "HOLD")
        self.assertEqual(strategy["target_price"], 253.98)
        self.assertEqual(strategy["stop_loss"], 245.26)
        self.assertEqual(strategy["alpha_score"], 77)
        self.assertFalse(strategy["allow_buy"])

    def test_web_server_html_rendering(self):
        server = DashboardWebServer(port=7979, title="테스트 퀀트 봇")
        html = server._render_html()
        self.assertIn("신규 스캔 종목 AI 진입 전략 후보군", html)
        self.assertIn("현재 보유 포지션 및 실시간 AI 퀀트 전략", html)
        self.assertIn("btc_regime_badge", html)
        self.assertIn("cand_regime_indicator", html)
        self.assertIn("switchStrategyTab", html)
        self.assertIn("renderAlphaBadge", html)


if __name__ == "__main__":
    unittest.main()
