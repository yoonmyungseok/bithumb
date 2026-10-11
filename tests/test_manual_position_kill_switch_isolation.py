"""수동 격리 포지션의 미실현 평가손익이 봇 킬 스위치를 침범하지 않음을 엄격히 검증한다."""

import datetime
import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from position_guard import calculate_bot_equity, calculate_bot_unrealized_pnl
from risk_manager import DailyRiskManager
from trading_orchestrator import PortfolioSnapshot, TradingOrchestrator


class FakeOrderJournal:
    def __init__(self, bot_markets: list[str]):
        self.orders = []
        for m in bot_markets:
            self.orders.append({
                "market": m,
                "side": "bid",
                "status": "FILLED",
                "executed_volume": 10.0,
                "price": 1000.0,
                "created_at": 1000.0,
            })

    def has_active_bot_position(self, market: str) -> bool:
        return any(o["market"] == market and o["side"] == "bid" for o in self.orders)


class ManualPositionKillSwitchIsolationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.datetime(2026, 10, 11, 9, 0, 0)
        self.risk_manager = DailyRiskManager(max_loss_pct=0.05, data_dir="/tmp/test_risk_p0")
        # 시작 자산 1,000,000 KRW로 초기화
        self.risk_manager.update_daily_equity(1_000_000.0, self.now)

    def tearDown(self):
        try:
            import shutil
            shutil.rmtree("/tmp/test_risk_p0", ignore_errors=True)
        except Exception:
            pass

    def test_manual_position_crash_does_not_trigger_bot_kill_switch(self):
        """
        사용자가 수동 매수한 종목(KRW-US)이 -50% 폭락하여 계좌 총 평가액이 -40% 급감했더라도,
        봇 관리 포지션이 없고 봇 실현손익이 정상이면 봇 킬스위치는 발동되지 않아야 한다.
        """
        balances = {
            "KRW": {"balance": 100_000.0, "locked": 0.0},
            "US": {"balance": 20000.0, "locked": 0.0, "avg_buy_price": 45.0},  # 수동 매수: 매수금 90만 원
        }
        price_map = {
            "KRW-US": 22.5,  # -50% 폭락 (평가액 45만 원, 계좌 총액 55만 원으로 -45% 급감)
        }
        journal = FakeOrderJournal(bot_markets=[])  # KRW-US는 봇 주문 저널에 없음 (수동 매수)

        # 1. 봇 미실현 손익 산출: 수동 종목이므로 0.0원이어야 함
        bot_unrealized_pnl = calculate_bot_unrealized_pnl(balances, price_map, order_journal=journal)
        self.assertEqual(bot_unrealized_pnl, 0.0)

        # 2. 봇 유효 자산 산출: KRW 100,000원만 집계
        bot_equity = calculate_bot_equity(balances, price_map, order_journal=journal)
        self.assertEqual(bot_equity, 100_000.0)

        # 3. 킬스위치 판정: 계좌 총액은 55만 원이지만 bot_unrealized_pnl이 0원이므로 킬스위치는 미발동
        total_account_equity = 550_000.0
        is_kill_switch, daily_pnl = self.risk_manager.update_daily_equity(
            total_account_equity,
            self.now,
            bot_unrealized_pnl_krw=bot_unrealized_pnl,
            bot_equity=bot_equity,
        )
        self.assertFalse(is_kill_switch)
        self.assertGreaterEqual(daily_pnl, 0.0)

    def test_bot_managed_position_loss_triggers_kill_switch(self):
        """
        봇이 직접 매수한 포지션(KRW-BTC)의 미실현 손실이 한도(-5%)를 초과하면
        킬스위치가 정상적으로 Latch 발동되어야 한다.
        """
        balances = {
            "KRW": {"balance": 500_000.0, "locked": 0.0},
            "BTC": {"balance": 0.005, "locked": 0.0, "avg_buy_price": 100_000_000.0},  # 투입 50만 원
        }
        price_map = {
            "KRW-BTC": 88_000_000.0,  # -12% 하락 -> 미실현 손실 -60,000원 (-6.0% 손실률)
        }
        journal = FakeOrderJournal(bot_markets=["KRW-BTC"])  # 봇 매수 포지션

        bot_unrealized_pnl = calculate_bot_unrealized_pnl(balances, price_map, order_journal=journal)
        self.assertAlmostEqual(bot_unrealized_pnl, -60_000.0)

        is_kill_switch, daily_pnl = self.risk_manager.update_daily_equity(
            940_000.0,
            self.now,
            bot_unrealized_pnl_krw=bot_unrealized_pnl,
        )
        self.assertTrue(is_kill_switch)
        self.assertEqual(daily_pnl, -0.06)

    def test_bot_realized_loss_triggers_kill_switch(self):
        """
        봇의 누적 실현 손실이 한도(-5%)를 초과하면 킬스위치가 정상 발동되어야 한다.
        """
        self.risk_manager.add_realized_trade(-60_000.0, is_win=False)
        is_kill_switch, daily_pnl = self.risk_manager.update_daily_equity(
            940_000.0,
            self.now,
            bot_unrealized_pnl_krw=0.0,
        )
        self.assertTrue(is_kill_switch)
        self.assertLessEqual(daily_pnl, -0.05)

    def test_legacy_call_backward_compatibility(self):
        """
        bot_unrealized_pnl_krw 인자가 없는 구형 호출 방식도 100% 정상 호환되어야 한다.
        """
        is_kill, pnl = self.risk_manager.update_daily_equity(930_000.0, self.now)
        self.assertTrue(is_kill)
        self.assertAlmostEqual(pnl, -0.07)

    def test_orchestrator_refresh_portfolio_manual_position_isolation(self):
        """
        TradingOrchestrator.refresh_portfolio 실행 시 수동 포지션 급락이
        봇 킬스위치를 트리거하지 않고 PortfolioSnapshot에 정확히 반영되는지 검증.
        """
        import logging
        orchestrator = TradingOrchestrator(logger=logging.getLogger("test"))
        balances = {
            "KRW": {"balance": 100_000.0, "locked": 0.0},
            "US": {"balance": 20000.0, "locked": 0.0, "avg_buy_price": 45.0},  # 수동 매수
        }
        exchange_mock = MagicMock()
        exchange_mock.get_balances.return_value = balances
        exchange_mock.get_tickers.return_value = [{"market": "KRW-US", "trade_price": 22.5}]
        exchange_mock.is_tradeable_market.return_value = True

        trailing_tracker = MagicMock()
        trailing_tracker.reconcile_markets.return_value = 0
        realtime_engine = MagicMock()
        realtime_engine.clean_stale_orders.return_value = 0
        realtime_engine.requote_pending_orders.return_value = 0
        risk_guard = MagicMock()
        journal = FakeOrderJournal(bot_markets=[])

        snapshot = orchestrator.refresh_portfolio(
            exchange_mock,
            calculate_total_equity=lambda b, e, **kwargs: 550_000.0,
            get_held_markets=lambda b, e, **kwargs: ["KRW-US"],
            trailing_tracker=trailing_tracker,
            realtime_engine=realtime_engine,
            risk_manager=self.risk_manager,
            risk_guard=risk_guard,
            get_portfolio_tiers=lambda eq: (3, 0.35, 10),
            now=self.now,
            order_journal=journal,
        )

        self.assertFalse(snapshot.is_kill_switch)
        self.assertEqual(snapshot.bot_equity, 100_000.0)
        self.assertEqual(snapshot.total_equity, 550_000.0)


if __name__ == "__main__":
    unittest.main()

