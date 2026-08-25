import os
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from bot_controller import BotController
from market_screener import MarketScreener
from order_safety import OrderJournal, RiskGuard, SafeOrderExecutor
from risk_manager import (
    DailyRiskManager,
    TrailingStopTracker,
    build_positions_data,
    calculate_total_equity,
    get_excluded_manual_holdings,
    get_held_markets,
)
from upbit_api import UpbitAPI, get_upbit_excluded_markets


class UpbitHoloGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.data_dir = self.tmp_dir.name
        self.journal = OrderJournal(data_dir=self.data_dir)
        self.executor = SafeOrderExecutor(self.journal)
        self.risk_guard = RiskGuard(
            min_order_krw=5000.0,
            max_open_positions=3,
            max_position_pct=0.35,
            max_total_exposure_pct=0.95,
            max_order_krw=0,
        )

        # Mock Upbit with BTC and HOLO in balances
        self.mock_upbit = types.SimpleNamespace(
            get_balances=lambda: {
                "KRW": {"balance": 1_000_000.0, "locked": 0.0, "avg_buy_price": 0.0},
                "BTC": {"balance": 0.01, "locked": 0.0, "avg_buy_price": 90_000_000.0},
                "HOLO": {"balance": 100_000.0, "locked": 0.0, "avg_buy_price": 500.0},  # 5,000만 원 상당 수동 보유분!
            },
            get_current_price=lambda m: 100_000_000.0 if m == "KRW-BTC" else (600.0 if m == "KRW-HOLO" else 1000.0),
            get_korean_name=lambda m: "홀로월드에이아이" if m == "KRW-HOLO" else ("비트코인" if m == "KRW-BTC" else m),
            get_all_markets=lambda: [{"market": "KRW-BTC"}, {"market": "KRW-HOLO"}, {"market": "KRW-ETH"}],
            get_tickers=lambda markets: [
                {"market": "KRW-BTC", "trade_price": "100000000", "signed_change_rate": "0.03", "acc_trade_price_24h": "50000000000"},
                {"market": "KRW-HOLO", "trade_price": "600", "signed_change_rate": "0.15", "acc_trade_price_24h": "90000000000"},  # 대량 거래대금 급등
                {"market": "KRW-ETH", "trade_price": "4000000", "signed_change_rate": "0.02", "acc_trade_price_24h": "30000000000"},
            ],
            get_orderbook=lambda m: {
                "orderbook_units": [
                    {"ask_price": 601.0 if m == "KRW-HOLO" else 100100.0, "bid_price": 600.0 if m == "KRW-HOLO" else 100000.0, "bid_size": 100000.0}
                ]
            },
            get_open_orders=lambda market=None: [],
            cancel_order=lambda uuid_str="", client_order_id="": {"status": "ok"},
            create_order=UpbitAPI.create_order.__get__(UpbitAPI("test", "test")),
        )

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_excluded_holdings_always_contains_holo(self):
        excluded = get_excluded_manual_holdings()
        self.assertIn("KRW-HOLO", excluded)
        self.assertIn("HOLO", excluded)

        upbit_excluded = get_upbit_excluded_markets()
        self.assertIn("KRW-HOLO", upbit_excluded)
        self.assertIn("HOLO", upbit_excluded)

    def test_calculate_total_equity_completely_ignores_holo_balance(self):
        balances = self.mock_upbit.get_balances()
        # KRW = 1,000,000 / BTC = 0.01 * 100,000,000 = 1,000,000 / HOLO = 100,000 * 600 = 60,000,000
        # Total equity should be ONLY 2,000,000 KRW (HOLO 6,000만 원 완전 배제)
        total_equity = calculate_total_equity(balances, self.mock_upbit)
        self.assertEqual(total_equity, 2_000_000.0)

    def test_get_held_markets_excludes_holo(self):
        balances = self.mock_upbit.get_balances()
        held = get_held_markets(balances, self.mock_upbit)
        self.assertIn("KRW-BTC", held)
        self.assertNotIn("KRW-HOLO", held)
        self.assertNotIn("HOLO", held)

    def test_build_positions_data_excludes_holo(self):
        balances = self.mock_upbit.get_balances()
        positions = build_positions_data(balances, self.mock_upbit)
        pos_markets = [p["market"] for p in positions]
        self.assertIn("KRW-BTC", pos_markets)
        self.assertNotIn("KRW-HOLO", pos_markets)

    def test_market_screener_strictly_filters_out_holo(self):
        screener = MarketScreener(self.mock_upbit, min_trade_value_krw=1_000_000_000.0)
        candidates = screener.scan_markets(top_count=5)
        candidate_markets = [c["market"] for c in candidates]
        self.assertIn("KRW-BTC", candidate_markets)
        self.assertNotIn("KRW-HOLO", candidate_markets)

    def test_risk_guard_validate_buy_rejects_holo(self):
        is_safe, reason = self.risk_guard.validate_buy(
            market="KRW-HOLO",
            order_krw=50000.0,
            available_krw=1_000_000.0,
            total_equity=2_000_000.0,
            held_markets=["KRW-BTC"],
        )
        self.assertFalse(is_safe)
        self.assertIn("격리 종목", reason)

    def test_safe_order_executor_rejects_holo(self):
        with self.assertRaises(ValueError) as ctx:
            self.executor.submit(
                self.mock_upbit,
                market="KRW-HOLO",
                side="bid",
                price=600.0,
                volume=100.0,
            )
        self.assertIn("격리 종목", str(ctx.exception))

    def test_panic_sell_never_sells_holo(self):
        submitted_orders = []

        def mock_submit(exchange, market, side, volume, ord_type="market"):
            submitted_orders.append({"market": market, "side": side, "volume": volume})
            return {"uuid": "ord-123"}

        mock_executor = types.SimpleNamespace(submit=mock_submit)
        mock_journal = types.SimpleNamespace(is_managed_order=lambda o: True, mark_by_uuid=lambda *a: None)
        rm = DailyRiskManager(data_dir=self.data_dir)
        tst = TrailingStopTracker(data_dir=self.data_dir)
        tg = types.SimpleNamespace(send_message=lambda msg: None)
        paused = [False]

        controller = BotController(
            exchange_factory=lambda: self.mock_upbit,
            order_executor=mock_executor,
            order_journal=mock_journal,
            risk_manager=rm,
            trailing_tracker=tst,
            trade_memory=types.SimpleNamespace(),
            telegram=tg,
            get_is_paused=lambda: paused[0],
            set_is_paused=lambda v: paused.__setitem__(0, v),
            exchange_name="업비트",
        )

        res_msg = controller.execute_panic_sell()
        self.assertTrue(paused[0])
        self.assertIn("긴급 전량 매도", res_msg)

        # Ensure BTC was sold but HOLO was NEVER touched
        sold_markets = [o["market"] for o in submitted_orders]
        self.assertIn("KRW-BTC", sold_markets)
        self.assertNotIn("KRW-HOLO", sold_markets)
        self.assertNotIn("HOLO", sold_markets)


if __name__ == "__main__":
    unittest.main()
