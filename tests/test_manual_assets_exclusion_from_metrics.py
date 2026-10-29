"""수동 매수 종목이 총 평가 자산, 금일 자산 변동, 금일 확정 실현 손익 및 포지션 목록에서
완전히 배제되는지 종합 검증하는 단위 테스트.
"""

import datetime
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from bot_controller import BotController
from dashboard_server import UnifiedDashboardServer
from order_safety.fill_processor import OrderFillProcessor
from order_safety.types import OrderStatus
from position_guard import calculate_bot_equity, calculate_bot_unrealized_pnl
from risk_manager import DailyRiskManager, build_positions_data, get_held_markets


class FakeOrderJournal:
    def __init__(self, bot_markets: list[str]):
        self.orders = []
        self.path = "/tmp/test_journal/order_journal.json"
        for m in bot_markets:
            self.orders.append({
                "client_order_id": f"bot-buy-{m}",
                "market": m,
                "side": "bid",
                "status": "FILLED",
                "executed_volume": 1.0,
                "volume": 1.0,
                "price": 50000.0,
                "avg_price": 50000.0,
                "created_at": 1000.0,
            })

    def get(self, client_order_id: str) -> dict | None:
        for o in self.orders:
            if o.get("client_order_id") == client_order_id:
                return o
        return None

    def has_active_bot_position(self, market: str) -> bool:
        return any(o["market"] == market and o["side"] == "bid" for o in self.orders)

    def get_recent_orders(self, limit: int = 10) -> list[dict]:
        return self.orders[:limit]

    def has_active_exit_order(self, market: str) -> bool:
        return False

    def mark(self, *args, **kwargs):
        pass

    def is_entry_ready(self) -> tuple[bool, list[str]]:
        return True, []

    def get_order_status_counts(self) -> dict:
        return {}

    def get_entry_blocking_markets(self) -> list[str]:
        return []

    def get_entry_order_for_exit(self, order: dict) -> dict | None:
        return None


class ManualAssetsExclusionTests(unittest.TestCase):
    def setUp(self):
        self.data_dir = f"/tmp/test_manual_excl_{os.getpid()}"
        os.makedirs(self.data_dir, exist_ok=True)
        self.risk_manager = DailyRiskManager(data_dir=self.data_dir)
        self.now = datetime.datetime(2026, 10, 25, 10, 0, 0)
        # 봇 시작 기준 자산: 150,000 KRW
        self.risk_manager.daily_start_equity = 150_000.0
        self.risk_manager.current_date_str = "2026-10-25"

    def tearDown(self):
        try:
            import shutil
            shutil.rmtree(self.data_dir, ignore_errors=True)
        except Exception:
            pass

    def test_build_positions_and_get_held_exclude_manual_assets(self):
        """수동 매수 종목(KRW-US)은 build_positions_data 및 get_held_markets에서 제외되어야 한다."""
        balances = {
            "KRW": {"balance": 100_000.0, "locked": 0.0},
            "BTC": {"balance": 1.0, "locked": 0.0, "avg_buy_price": 50_000.0},  # 봇 관리 코인
            "US": {"balance": 100.0, "locked": 0.0, "avg_buy_price": 5_000.0},  # 수동 매수 코인
        }
        price_map = {"KRW-BTC": 50_000.0, "KRW-US": 5_000.0}
        journal = FakeOrderJournal(bot_markets=["KRW-BTC"])
        tracker = MagicMock()
        tracker.get_entry_time = MagicMock(side_effect=lambda m: 1000.0 if m == "KRW-BTC" else 0.0)
        tracker.get_strategy_mode = MagicMock(return_value="SCALP")
        exchange = MagicMock()
        exchange.get_korean_name = MagicMock(side_effect=lambda m: "비트코인" if m == "KRW-BTC" else "탈루스")

        # 1. get_held_markets 검증
        held = get_held_markets(
            balances,
            exchange,
            price_map=price_map,
            order_journal=journal,
            trailing_tracker=tracker,
        )
        self.assertEqual(held, ["KRW-BTC"])
        self.assertNotIn("KRW-US", held)

        # 2. build_positions_data 검증
        positions = build_positions_data(
            balances,
            exchange,
            price_map=price_map,
            order_journal=journal,
            trailing_tracker=tracker,
        )
        markets = [p["market"] for p in positions]
        self.assertEqual(markets, ["KRW-BTC"])
        self.assertNotIn("KRW-US", markets)

    def test_bot_controller_dashboard_data_excludes_manual_assets(self):
        """대시보드 메트릭에서 총 평가 자산 및 금일 자산 변동이 수동 매수 종목을 배제하는지 검증."""
        balances = {
            "KRW": {"balance": 100_000.0, "locked": 0.0},
            "BTC": {"balance": 1.0, "locked": 0.0, "avg_buy_price": 50_000.0},  # 봇 보유 (5만 원)
            "US": {"balance": 100.0, "locked": 0.0, "avg_buy_price": 5_000.0},  # 수동 보유 (50만 원)
        }
        # US 시세가 +100% 폭등(10,000원)하여 계좌 전체 자산이 115만 원이 되었더라도
        price_map = {"KRW-BTC": 52_000.0, "KRW-US": 10_000.0}

        journal = FakeOrderJournal(bot_markets=["KRW-BTC"])
        tracker = MagicMock()
        tracker.get_entry_time = MagicMock(side_effect=lambda m: 1000.0 if m == "KRW-BTC" else 0.0)
        tracker.get_strategy_mode = MagicMock(return_value="SCALP")
        tracker.get_position_dashboard_state = MagicMock(return_value={})
        tracker.get_new_listing_markets = MagicMock(return_value=[])

        exchange = MagicMock()
        exchange.get_balances = MagicMock(return_value=balances)
        exchange.get_korean_name = MagicMock(return_value="테스트")

        controller = BotController(
            exchange_factory=lambda: exchange,
            order_executor=MagicMock(),
            order_journal=journal,
            risk_manager=self.risk_manager,
            trailing_tracker=tracker,
            trade_memory=MagicMock(),
            telegram=MagicMock(),
            get_is_paused=lambda: False,
            set_is_paused=lambda p: None,
            exchange_name="빗썸",
        )

        with patch("bot_controller._fetch_held_prices", return_value=price_map):
            d = controller.get_dashboard_data()

        # 총 평가 자산은 봇 운용 자산(가용 10만 + BTC 5.2만 = 152,000 KRW)이어야 함
        self.assertEqual(d["total_equity"], 152_000)
        self.assertEqual(d["bot_equity"], 152_000)
        # 수동 격리 자산은 1,000,000 KRW (US 100개 * 10,000원)
        self.assertEqual(d["manual_isolated_equity"], 1_000_000)
        # 계좌 전체 자산은 1,152,000 KRW
        self.assertEqual(d["account_total_equity"], 1_152_000)

        # 금일 자산 변동은 봇 시작 자산(150,000) 대비 봇 평가액(152,000)의 변동인 +2,000원이어야 함 (수동 폭등 배제)
        self.assertEqual(d["daily_pnl_krw"], 2_000)
        self.assertAlmostEqual(d["daily_pnl_pct"], round(2_000 / 150_000 * 100, 2))

    def test_status_message_excludes_manual_assets(self):
        """텔레그램 상태 메시지에서도 봇 총 평가 자산 및 금일 자산 변동에서 수동 코인이 배제되는지 검증."""
        balances = {
            "KRW": {"balance": 100_000.0, "locked": 0.0},
            "BTC": {"balance": 1.0, "locked": 0.0, "avg_buy_price": 50_000.0},
            "US": {"balance": 100.0, "locked": 0.0, "avg_buy_price": 5_000.0},
        }
        price_map = {"KRW-BTC": 50_000.0, "KRW-US": 20_000.0}

        journal = FakeOrderJournal(bot_markets=["KRW-BTC"])
        tracker = MagicMock()
        tracker.get_entry_time = MagicMock(side_effect=lambda m: 1000.0 if m == "KRW-BTC" else 0.0)
        tracker.get_strategy_mode = MagicMock(return_value="SCALP")

        exchange = MagicMock()
        exchange.get_balances = MagicMock(return_value=balances)
        exchange.get_korean_name = MagicMock(return_value="비트코인")

        controller = BotController(
            exchange_factory=lambda: exchange,
            order_executor=MagicMock(),
            order_journal=journal,
            risk_manager=self.risk_manager,
            trailing_tracker=tracker,
            trade_memory=MagicMock(),
            telegram=MagicMock(),
            get_is_paused=lambda: False,
            set_is_paused=lambda p: None,
            exchange_name="빗썸",
        )

        with patch("bot_controller._fetch_held_prices", return_value=price_map), \
             patch("bot_controller.get_fear_and_greed_index", return_value={"desc": "중립"}):
            msg = controller.get_status_message()

        self.assertIn("총 평가 자산:</b> 150,000 KRW", msg)
        self.assertIn("금일 자산 변동:</b> +0 KRW", msg)
        self.assertIn("현재 보유 종목:</b> KRW-BTC", msg)
        self.assertNotIn("KRW-US", msg)

    def test_manual_exit_does_not_affect_realized_pnl(self):
        """수동 청산(MANUAL) 매도 체결 시 봇의 realized_pnl_krw에 합산되지 않아야 한다."""
        import threading
        journal = FakeOrderJournal(bot_markets=[])
        journal._lock = threading.Lock()
        processor = OrderFillProcessor(
            order_journal=journal,
            trailing_tracker=MagicMock(),
            risk_manager=self.risk_manager,
            send_fill_alerts=False,
        )

        order = {
            "client_order_id": "manual-sell-1",
            "market": "KRW-US",
            "side": "ask",
            "status": "OPEN",
            "volume": 10.0,
            "executed_volume": 0.0,
            "price": 6000.0,
            "avg_price": 6000.0,
            "exit_reason": "MANUAL",
            "created_at": 1000.0,
            "is_manual": True,
            "avg_buy_price": 5000.0,
        }
        journal.orders.append(order)

        res = processor.process_order_fill(
            order_identifier="manual-sell-1",
            status=OrderStatus.FILLED,
            executed_volume=10.0,
            avg_price=6000.0,
            fee=0.0,
            avg_buy_price=5000.0,
        )

        # 봇의 확정 실현 손익은 0원이어야 함 (수동 청산 건이 누적되지 않음)
        self.assertEqual(self.risk_manager.realized_pnl_krw, 0.0)
        self.assertEqual(self.risk_manager.total_trades_today, 0)

    def test_dashboard_server_combined_excludes_manual_assets(self):
        """대시보드 통합(combined) 응답에서 total_equity가 수동 자산을 배제한 total_bot_equity여야 한다."""
        server = UnifiedDashboardServer()
        bithumb_data = {
            "online": True,
            "total_equity": 150_000,
            "bot_equity": 150_000,
            "manual_isolated_equity": 500_000,
            "krw_available": 100_000,
            "daily_start_equity": 150_000,
            "daily_pnl_krw": 0,
            "daily_pnl_pct": 0.0,
            "realized_pnl_krw": 0,
            "total_trades": 0,
            "win_trades": 0,
            "safety": {"entry_ready": True, "entry_block_reasons": []},
        }
        upbit_data = {
            "online": True,
            "total_equity": 200_000,
            "bot_equity": 200_000,
            "manual_isolated_equity": 300_000,
            "krw_available": 150_000,
            "daily_start_equity": 200_000,
            "daily_pnl_krw": 0,
            "daily_pnl_pct": 0.0,
            "realized_pnl_krw": 0,
            "total_trades": 0,
            "win_trades": 0,
            "safety": {"entry_ready": True, "entry_block_reasons": []},
        }

        with patch.object(server, "fetch_exchange_status", side_effect=[bithumb_data, upbit_data]):
            combined_all = server.get_aggregated_status()

        combined = combined_all["combined"]
        # combined total_equity는 봇 운용 총액 350,000이어야 함 (수동 합산 800,000 제외)
        self.assertEqual(combined["total_equity"], 350_000.0)
        self.assertEqual(combined["bot_equity"], 350_000.0)
        self.assertEqual(combined["manual_isolated_equity"], 800_000.0)
        self.assertEqual(combined["account_total_equity"], 350_000.0)


if __name__ == "__main__":
    unittest.main()
