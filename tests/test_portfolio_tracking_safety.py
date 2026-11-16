"""잔고 대사에서 시세 누락이 보호 상태를 삭제하지 않는지 검증한다."""
import datetime
import logging
import unittest
from unittest.mock import MagicMock

from trading_orchestrator import TradingOrchestrator
from risk_manager import filter_small_manual_holdings, _fetch_held_prices


class PortfolioTrackingSafetyTests(unittest.TestCase):
    def run_snapshot(self, balances):
        exchange = MagicMock()
        exchange.get_balances.return_value = balances
        exchange.get_tickers.return_value = []
        tracker = MagicMock()
        tracker.reconcile_markets.return_value = 0
        realtime = MagicMock()
        realtime.clean_stale_orders.return_value = 0
        realtime.requote_pending_orders.return_value = 0
        risk = MagicMock()
        risk.update_daily_equity.return_value = (False, 0.0)
        risk.is_cooling_down.return_value = (False, 0)
        TradingOrchestrator(logging.getLogger(__name__)).refresh_portfolio(
            exchange, calculate_total_equity=lambda *args, **kwargs: 10000.0,
            get_held_markets=lambda *args, **kwargs: [], trailing_tracker=tracker,
            realtime_engine=realtime, risk_manager=risk, risk_guard=MagicMock(),
            get_portfolio_tiers=lambda _: (3, 0.3, 3), now=datetime.datetime.now(),
            order_journal=MagicMock(),
        )
        return tracker

    def test_small_manual_cost_is_excluded_but_unknown_and_bot_are_preserved(self):
        """수동 소액만 제외하고 5천 원 경계·평단 누락·봇 보유는 보존한다."""
        journal = MagicMock()
        journal.has_active_bot_position = MagicMock(side_effect=lambda market: market == "KRW-BOT")
        balances = {
            "KRW": {"balance": 10000},
            "DUST": {"balance": 1, "locked": 1, "avg_buy_price": 2000},
            "EDGE": {"balance": 1, "avg_buy_price": 5000},
            "UNKNOWN": {"balance": 1, "avg_buy_price": 0},
            "BOT": {"balance": 1, "avg_buy_price": 100},
        }
        filtered = filter_small_manual_holdings(balances, journal)
        self.assertNotIn("DUST", filtered)
        self.assertEqual(set(filtered), {"KRW", "EDGE", "UNKNOWN", "BOT"})
        exchange = MagicMock()
        exchange.get_tickers.return_value = []
        _fetch_held_prices(filtered, exchange)
        self.assertEqual(set(exchange.get_tickers.call_args.args[0]), {"KRW-EDGE", "KRW-UNKNOWN", "KRW-BOT"})
        self.assertIn("DUST", filter_small_manual_holdings(balances, None))

    def test_bithumb_fixed_exclusion_respects_exchange_and_bot_ownership(self):
        """고정 목록은 빗썸 수동 잔고에만 적용하고 봇 보유와 업비트는 보호한다."""
        balances = {"ETH": {"balance": 1, "avg_buy_price": 10000},
                    "BTG": {"balance": 1, "avg_buy_price": 0},
                    "ETHW": {"balance": 1, "avg_buy_price": 0},
                    "DIS": {"balance": 1, "avg_buy_price": 0}}
        journal = MagicMock()
        journal.exchange_scope = "bithumb"
        journal.has_active_bot_position = MagicMock(side_effect=lambda market: market == "KRW-ETH")
        self.assertEqual(set(filter_small_manual_holdings(balances, journal)), {"ETH"})
        journal.exchange_scope = "upbit"
        self.assertEqual(set(filter_small_manual_holdings(balances, journal)), {"ETH", "BTG", "ETHW", "DIS"})

    def test_small_manual_missing_price_does_not_block_valuation(self):
        """매수원금이 확인된 수동 소액 시세가 없어도 나머지 평가를 완료한다."""
        exchange = MagicMock()
        exchange.get_balances.return_value = {"KRW": {"balance": 10000}, "DUST": {"balance": 1, "avg_buy_price": 100}}
        tracker, realtime, risk, journal = MagicMock(), MagicMock(), MagicMock(), MagicMock()
        journal.has_active_bot_position.return_value = False
        realtime.clean_stale_orders.return_value = realtime.requote_pending_orders.return_value = 0
        risk.update_daily_equity.return_value = (False, 0.0)
        risk.is_cooling_down.return_value = (False, 0)
        TradingOrchestrator(logging.getLogger(__name__)).refresh_portfolio(
            exchange, calculate_total_equity=lambda *a, **kw: 10000.0,
            get_held_markets=lambda *a, **kw: [], trailing_tracker=tracker,
            realtime_engine=realtime, risk_manager=risk, risk_guard=MagicMock(),
            get_portfolio_tiers=lambda _: (3, 0.3, 3), now=datetime.datetime.now(), order_journal=journal,
        )
        exchange.get_tickers.assert_not_called()
        risk.update_daily_equity.assert_called_once()
        self.assertEqual(journal.set_valuation_ready.call_args.args, (True,))
        self.assertEqual(tracker.reconcile_markets.call_args.args[0], ["KRW-DUST"])

    def test_incomplete_valuation_preserves_risk_and_blocks_until_recovery(self):
        """가격 누락 시 위험 갱신을 보류하고 완전한 조회에서만 차단을 해제한다."""
        exchange = MagicMock()
        exchange.get_balances.return_value = {"KRW": {"balance": 10000.0}, "BTC": {"balance": 1.0}}
        exchange.get_tickers.side_effect = [[], [{"market": "KRW-BTC", "trade_price": 100.0}]]
        tracker, realtime, risk, journal = MagicMock(), MagicMock(), MagicMock(), MagicMock()
        realtime.clean_stale_orders.return_value = 0
        realtime.requote_pending_orders.return_value = 0
        risk.kill_switch_active = True
        risk.update_daily_equity.return_value = (True, -0.05)
        risk.is_cooling_down.return_value = (False, 0)
        engine = TradingOrchestrator(logging.getLogger(__name__))
        def refresh():
            return engine.refresh_portfolio(
                exchange, calculate_total_equity=lambda *a, **kw: 10000.0,
                get_held_markets=lambda *a, **kw: [], trailing_tracker=tracker,
                realtime_engine=realtime, risk_manager=risk, risk_guard=MagicMock(),
                get_portfolio_tiers=lambda _: (3, 0.3, 3), now=datetime.datetime.now(),
                order_journal=journal,
            )
        snapshot = refresh()
        self.assertTrue(snapshot.is_kill_switch)
        risk.update_daily_equity.assert_not_called()
        self.assertEqual(journal.set_valuation_ready.call_args.args, (False,))
        refresh()
        risk.update_daily_equity.assert_called_once()
        self.assertEqual(journal.set_valuation_ready.call_args.args, (True,))

    def test_missing_price_and_dust_preserve_held_market(self):
        """시세가 없거나 소액이어도 양수 잔고가 있으면 추적 상태를 보존한다."""
        tracker = self.run_snapshot({"KRW": {"balance": 10000.0}, "BTC": {"balance": 0.000001}})
        self.assertEqual(tracker.reconcile_markets.call_args.args[0], ["KRW-BTC"])

    def test_empty_response_does_not_clear_tracking(self):
        """빈 잔고 응답은 조회 실패 가능성이 있어 상태 정리를 보류한다."""
        tracker = self.run_snapshot({})
        tracker.reconcile_markets.assert_not_called()

    def test_confirmed_absence_allows_cleanup(self):
        """유효한 원화 잔고에서 코인이 사라졌다면 추적 상태 정리를 허용한다."""
        tracker = self.run_snapshot({"KRW": {"balance": 10000.0}})
        self.assertEqual(tracker.reconcile_markets.call_args.args[0], [])


if __name__ == "__main__":
    unittest.main()
