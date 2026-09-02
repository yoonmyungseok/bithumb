import os
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from exchange_adapter import BithumbAdapter
from trading_orchestrator import TradingOrchestrator
from trading_runtime import (
    ExchangeBuyProfile,
    ExchangeCycleProfile,
    ExchangeEntryProfile,
    ExchangeExitProfile,
    MarketBuyInputs,
    MarketEntryInputs,
    MarketExitInputs,
    MarketStopLossInputs,
    TradingCycleEngine,
    TradingRuntimeConfig,
    TradingRuntimeContext,
    format_krw_display,
)


class FakeExchangeClient:
    def get_balances(self):
        return {
            "KRW": {"balance": 1_000_000.0, "locked": 0.0},
            "BTC": {"balance": 0.01, "locked": 0.0, "avg_buy_price": 90_000_000.0},
        }

    def get_candles(self, unit=5, count=30, market="KRW-BTC", to=None):
        price = 95_000_000.0 if market == "KRW-BTC" else 100.0
        return [{"market": market, "trade_price": price} for _ in range(max(count, 5))]

    def get_orderbook(self, market="KRW-BTC"):
        return {"market": market}

    def get_current_price(self, market="KRW-BTC"):
        return 95_000_000.0 if market == "KRW-BTC" else 100.0

    def get_korean_name(self, market="KRW-BTC"):
        return "비트코인" if market == "KRW-BTC" else market

    def get_open_orders(self, market=None):
        return []


class TradingRuntimePrefixTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.ws_subscriptions: list[list[str]] = []
        self.purged: list[tuple[str, float]] = []
        self.dashboard_calls = 0
        self.reconcile_labels: list[str] = []
        self.recorded_decisions: list[dict] = []
        self.submitted_orders: list[dict] = []
        outer = self

        class OrderExecutor:
            def submit(self, exchange, **kwargs):
                outer.submitted_orders.append(kwargs)
                return {"uuid": "exit-order", "client_order_id": "exit-client"}

        class Journal:
            def reconcile_exchange_statuses(self, **kwargs):
                outer.reconcile_labels.append(kwargs.get("get_order"))
                return 0

            def complete_reconciliation_if_safe(self):
                return None

            def has_active_exit_order(self, market):
                return False

            def has_unresolved_market(self, market):
                return False

        class DecisionDb:
            def purge_strategy_decisions(self, exchange, cutoff_ts):
                outer.purged.append((exchange, cutoff_ts))

            def record_strategy_decision(self, **kwargs):
                outer.recorded_decisions.append(kwargs)

            def has_recovery_entry_since(self, exchange, cutoff_ts):
                return False

        class BotController:
            def get_dashboard_data(self):
                outer.dashboard_calls += 1
                return {}

        class WsClient:
            def update_subscriptions(self, markets):
                outer.ws_subscriptions.append(list(markets))

        self.exchange = BithumbAdapter(FakeExchangeClient(), data_dir=self.tmp_dir.name, web_port=7979)
        self.journal = Journal()
        self.order_executor = OrderExecutor()
        self.decision_db = DecisionDb()
        self.bot_controller = BotController()
        self.ws_client = WsClient()
        self.orchestrator = TradingOrchestrator(__import__("logging").getLogger("test_trading_runtime"))

        def create_screener(exchange):
            class Screener:
                def scan_markets(self, **kwargs):
                    return [{"market": "KRW-XRP", "candidate_type": "CONFIRMED"}]

            return Screener()

        self.create_screener = create_screener

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _build_engine(
        self,
        profile: ExchangeCycleProfile,
        *,
        env_file: str | None = None,
        exit_profile: ExchangeExitProfile | None = None,
        entry_profile: ExchangeEntryProfile | None = None,
        buy_profile: ExchangeBuyProfile | None = None,
        trailing_tracker=None,
        risk_guard=None,
    ) -> TradingCycleEngine:
        if trailing_tracker is None:
            trailing_tracker = types.SimpleNamespace(
                start_profit_pct=0.0,
                trailing_drop_pct=0.0,
                set_macro_defensive_mode=lambda *_args, **_kwargs: None,
                reconcile_markets=lambda held_markets: 0,
                check_position=lambda market, current_price, avg_buy_price: (None, 0.0, 0.0, 0.0, 0.0),
                acquire_exit_lock=lambda market: True,
                release_exit_lock=lambda market: None,
                get_entry_time=lambda market: time.time() - 7200,
                set_entry_time=lambda market, ts: None,
            )
        risk_manager = types.SimpleNamespace(
            max_loss_pct=0.0,
            cooldown_until_ts=0.0,
            update_daily_equity=lambda total_equity, now: (False, 0.0),
            is_cooling_down=lambda: (False, 0),
        )
        realtime_engine = types.SimpleNamespace(
            clean_stale_orders=lambda **kwargs: 0,
            requote_pending_orders=lambda: 0,
        )
        risk_guard = risk_guard or types.SimpleNamespace(
            validate_buy=lambda **kwargs: (True, ""),
            update_limits=lambda **kwargs: None,
        )
        latest_strategies: dict[str, dict] = {}
        cooldown_manager = types.SimpleNamespace(
            check_reentry_allowed=lambda market, current_price: (True, ""),
        )
        trade_memory = types.SimpleNamespace(get_feedback_context=lambda: "")
        strategy_cache_manager = types.SimpleNamespace(save_cache=lambda data: None)

        context = TradingRuntimeContext(
            logger=__import__("logging").getLogger("test_trading_runtime"),
            orchestrator=self.orchestrator,
            create_exchange_client=lambda: self.exchange,
            order_journal=self.journal,
            fill_processor=object(),
            trailing_tracker=trailing_tracker,
            realtime_engine=realtime_engine,
            risk_manager=risk_manager,
            risk_guard=risk_guard,
            bot_controller=self.bot_controller,
            ws_client=self.ws_client,
            decision_db=self.decision_db,
            calculate_total_equity=lambda balances, exchange: 2_000_000.0,
            get_held_markets=lambda balances, exchange: ["KRW-BTC"],
            get_portfolio_tiers=lambda equity: (3, 0.35, 3),
            order_executor=self.order_executor,
            chart_renderer=types.SimpleNamespace(render_trade_chart=lambda **kwargs: "chart"),
            cancel_bot_open_orders=lambda exchange, market=None: 0,
            cooldown_manager=cooldown_manager,
            trade_memory=trade_memory,
            latest_strategies=latest_strategies,
            strategy_cache_manager=strategy_cache_manager,
        )
        config = TradingRuntimeConfig(
            profile=profile,
            exit_profile=exit_profile or ExchangeExitProfile(),
            entry_profile=entry_profile or ExchangeEntryProfile(
                signal_exchange=profile.exchange_key,
                recovery_db_exchange=profile.decision_exchange,
            ),
            buy_profile=buy_profile or ExchangeBuyProfile(exchange_name=profile.exchange_key),
            env_file=env_file,
            interval_minutes=5,
            gemini_api_key="",
            is_bot_paused=lambda: False,
            min_order_krw=5000.0,
            orderbook_slippage_enforcement=False,
        )
        return TradingCycleEngine(config, context)

    @patch("trading_runtime.get_fear_and_greed_index", return_value={"desc": "중립"})
    @patch("trading_runtime.load_runtime_risk_settings")
    def test_bithumb_prefix_sets_audit_and_ws_subscription(self, mock_risk_settings, _mock_fng):
        mock_risk_settings.return_value = types.SimpleNamespace(
            btc_crash_threshold_pct=-0.03,
            max_daily_loss_pct=0.05,
            trailing_start_pct=0.02,
            trailing_stop_pct=0.01,
        )
        profile = ExchangeCycleProfile(
            exchange_key="bithumb",
            reconcile_label="",
            decision_exchange="bithumb",
            log_prefix="",
            extra_excluded_markets=frozenset(),
            create_screener=self.create_screener,
            cycle_start_label="5분 AI 퀀트 트레이딩",
            tier_label="스마트 자산 티어",
            tier_top_wording="스크리닝 상위",
            summary_label="자산 요약",
            btc_crash_label="비트코인 급락 위험 감지",
        )
        engine = self._build_engine(profile)
        prefix = engine.run_cycle_prefix()

        self.assertEqual(prefix.exchange.key, "bithumb")
        self.assertEqual(prefix.target_markets, ["KRW-BTC", "KRW-XRP"])
        self.assertEqual(self.ws_subscriptions[-1], ["KRW-BTC", "KRW-XRP"])
        self.assertEqual(prefix.excluded_markets, frozenset())
        self.assertEqual(self.purged[-1][0], "bithumb")
        self.assertEqual(self.dashboard_calls, 1)

        prefix.audit_decision("KRW-XRP", "BLOCKED", "TEST", ["reason"], {"ok": True})
        self.assertEqual(self.recorded_decisions[-1]["exchange"], "bithumb")
        self.assertEqual(self.recorded_decisions[-1]["market"], "KRW-XRP")

    @patch("trading_runtime.get_fear_and_greed_index", return_value={"desc": "중립"})
    @patch("trading_runtime.load_runtime_risk_settings")
    def test_upbit_prefix_uses_profile_labels_and_excluded_markets(self, mock_risk_settings, _mock_fng):
        mock_risk_settings.return_value = types.SimpleNamespace(
            btc_crash_threshold_pct=-0.03,
            max_daily_loss_pct=0.05,
            trailing_start_pct=0.02,
            trailing_stop_pct=0.01,
        )

        def excluded_factory():
            return frozenset({"KRW-HOLO", "HOLO"})

        profile = ExchangeCycleProfile(
            exchange_key="upbit",
            reconcile_label="업비트 ",
            decision_exchange="upbit",
            log_prefix="업비트 ",
            extra_excluded_markets=excluded_factory,
            create_screener=self.create_screener,
            cycle_start_label="업비트 5분 AI 퀀트 트레이딩",
            tier_label="업비트 스마트 자산 티어",
            tier_top_wording="상위",
            summary_label="업비트 자산 요약",
            btc_crash_label="업비트 비트코인 급락 위험 감지",
            markets_log_prefix="업비트 ",
            stale_orders_log_prefix="업비트 ",
        )
        engine = self._build_engine(profile)
        prefix = engine.run_cycle_prefix()

        self.assertEqual(prefix.excluded_markets, frozenset({"KRW-HOLO", "HOLO"}))
        self.assertEqual(self.purged[-1][0], "upbit")
        prefix.audit_decision("KRW-ETH", "OBSERVED", "TEST", [], {})
        self.assertEqual(self.recorded_decisions[-1]["exchange"], "upbit")

    def test_format_krw_display_preserves_small_balance_decimals(self):
        self.assertEqual(format_krw_display(50.5), "50.50원")
        self.assertEqual(format_krw_display(1234567.0), "1,234,567원")

    def test_trailing_stop_requests_market_loop_continue(self):
        profile = ExchangeCycleProfile(
            exchange_key="bithumb",
            reconcile_label="",
            decision_exchange="bithumb",
            log_prefix="",
            extra_excluded_markets=frozenset(),
            create_screener=self.create_screener,
            cycle_start_label="5분 AI 퀀트 트레이딩",
            tier_label="스마트 자산 티어",
            tier_top_wording="스크리닝 상위",
            summary_label="자산 요약",
            btc_crash_label="비트코인 급락 위험 감지",
        )
        trailing_tracker = types.SimpleNamespace(
            check_position=lambda market, current_price, avg_buy_price: (
                "TRAILING_STOP", 110.0, 105.0, 5.0, 3.0,
            ),
            acquire_exit_lock=lambda market: True,
            release_exit_lock=lambda market: None,
            get_entry_time=lambda market: time.time(),
            set_entry_time=lambda market, ts: None,
        )
        engine = self._build_engine(profile, trailing_tracker=trailing_tracker)
        should_continue = engine.process_priority_exits(MarketExitInputs(
            exchange=self.exchange,
            market="KRW-XRP",
            korean_name="리플",
            coin_available=100.0,
            avg_buy_price=100.0,
            current_price=103.0,
            coin_value=10300.0,
            candles_5m=[{"trade_price": 100.0} for _ in range(20)],
            btc_regime="NORMAL",
            now_str="2026-09-02 16:00:00",
        ))

        self.assertTrue(should_continue)
        self.assertEqual(self.submitted_orders[-1]["exit_reason"], "TRAILING_STOP")
        self.assertEqual(self.submitted_orders[-1]["volume"], 100.0)

    @patch("trading_runtime.entry_signal", return_value={"allow_buy": False, "reason": "관망", "entry_price": 100.0, "target_price": 103.0, "stop_loss": 98.0})
    @patch("trading_runtime.recovery_rebound_signal", return_value={"allow_buy": False})
    @patch("trading_runtime.select_completed_candles")
    def test_upbit_entry_profile_blocks_when_entry_not_ready(self, mock_select_candles, _mock_recovery, _mock_entry):
        mock_select_candles.side_effect = lambda candles, minimum_count=25: candles or []
        profile = ExchangeCycleProfile(
            exchange_key="upbit",
            reconcile_label="업비트 ",
            decision_exchange="upbit",
            log_prefix="업비트 ",
            extra_excluded_markets=frozenset(),
            create_screener=self.create_screener,
            cycle_start_label="업비트 5분 AI 퀀트 트레이딩",
            tier_label="업비트 스마트 자산 티어",
            tier_top_wording="상위",
            summary_label="업비트 자산 요약",
            btc_crash_label="업비트 비트코인 급락 위험 감지",
        )
        entry_profile = ExchangeEntryProfile(
            signal_exchange="upbit",
            recovery_db_exchange="upbit",
            enforce_pre_entry_safety_gates=True,
            block_on_reentry_denied=True,
            require_minimum_candles=True,
        )
        engine = self._build_engine(profile, entry_profile=entry_profile)
        result = engine.process_entry_gating(MarketEntryInputs(
            exchange=self.exchange,
            market="KRW-XRP",
            korean_name="리플",
            candidate_type="CONFIRMED",
            candidate_metadata={},
            analyzer=None,
            coin_available=0.0,
            avg_buy_price=0.0,
            current_price=100.0,
            coin_value=0.0,
            krw_available=1_000_000.0,
            candles_5m=[{"trade_price": 100.0} for _ in range(25)],
            candles_1h=[{"trade_price": 100.0} for _ in range(20)],
            orderbook={"market": "KRW-XRP"},
            btc_regime="NORMAL",
            btc_status_msg="정상",
            is_btc_crashing=False,
            is_cooldown=False,
            is_extreme_fear=False,
            is_bot_paused=False,
            is_kill_switch=False,
            is_entry_ready=False,
            dyn_max_pos_pct=0.35,
            now_str="2026-09-02 16:00:00",
            audit_decision=lambda *args, **kwargs: None,
        ))

        self.assertTrue(result.should_continue)

    @patch("trading_runtime.entry_signal", return_value={"allow_buy": False, "reason": "관망", "entry_price": 100.0, "target_price": 103.0, "stop_loss": 98.0})
    @patch("trading_runtime.recovery_rebound_signal", return_value={"allow_buy": False})
    @patch("trading_runtime.select_completed_candles")
    def test_entry_gating_records_hold_strategy(self, mock_select_candles, _mock_recovery, _mock_entry):
        mock_select_candles.side_effect = lambda candles, minimum_count=25: candles or []
        profile = ExchangeCycleProfile(
            exchange_key="bithumb",
            reconcile_label="",
            decision_exchange="bithumb",
            log_prefix="",
            extra_excluded_markets=frozenset(),
            create_screener=self.create_screener,
            cycle_start_label="5분 AI 퀀트 트레이딩",
            tier_label="스마트 자산 티어",
            tier_top_wording="스크리닝 상위",
            summary_label="자산 요약",
            btc_crash_label="비트코인 급락 위험 감지",
        )
        latest_strategies: dict[str, dict] = {}
        engine = self._build_engine(profile)
        engine.context.latest_strategies = latest_strategies
        audit_calls: list[tuple] = []

        def audit_decision(*args, **kwargs):
            audit_calls.append(args)

        result = engine.process_entry_gating(MarketEntryInputs(
            exchange=self.exchange,
            market="KRW-XRP",
            korean_name="리플",
            candidate_type="CONFIRMED",
            candidate_metadata={"candidate_type": "CONFIRMED"},
            analyzer=None,
            coin_available=0.0,
            avg_buy_price=0.0,
            current_price=100.0,
            coin_value=0.0,
            krw_available=1_000_000.0,
            candles_5m=[{"trade_price": 100.0} for _ in range(25)],
            candles_1h=[{"trade_price": 100.0} for _ in range(20)],
            orderbook={"market": "KRW-XRP"},
            btc_regime="NORMAL",
            btc_status_msg="정상",
            is_btc_crashing=False,
            is_cooldown=False,
            is_extreme_fear=False,
            is_bot_paused=False,
            is_kill_switch=False,
            is_entry_ready=True,
            dyn_max_pos_pct=0.35,
            now_str="2026-09-02 16:00:00",
            audit_decision=audit_decision,
        ))

        self.assertFalse(result.should_continue)
        self.assertEqual(result.action, "HOLD")
        self.assertIn("KRW-XRP", latest_strategies)
        self.assertEqual(latest_strategies["KRW-XRP"]["action"], "HOLD")
        self.assertEqual(audit_calls[-1][1], "HOLD")

    def test_cycle_stop_loss_requests_market_loop_continue(self):
        profile = ExchangeCycleProfile(
            exchange_key="bithumb",
            reconcile_label="",
            decision_exchange="bithumb",
            log_prefix="",
            extra_excluded_markets=frozenset(),
            create_screener=self.create_screener,
            cycle_start_label="5분 AI 퀀트 트레이딩",
            tier_label="스마트 자산 티어",
            tier_top_wording="스크리닝 상위",
            summary_label="자산 요약",
            btc_crash_label="비트코인 급락 위험 감지",
        )
        trailing_tracker = types.SimpleNamespace(
            acquire_exit_lock=lambda market: True,
            release_exit_lock=lambda market: None,
            clear=lambda market: None,
        )
        buy_profile = ExchangeBuyProfile(
            exchange_name="bithumb",
            enable_cycle_stop_loss=True,
            render_stop_loss_chart=False,
        )
        engine = self._build_engine(profile, buy_profile=buy_profile, trailing_tracker=trailing_tracker)
        should_continue = engine.process_cycle_stop_loss(MarketStopLossInputs(
            exchange=self.exchange,
            market="KRW-XRP",
            korean_name="리플",
            coin_available=100.0,
            avg_buy_price=100.0,
            current_price=95.0,
            coin_value=9500.0,
            stop_loss=98.0,
            target_price=110.0,
            reason="손절 테스트",
            candles_5m=[{"trade_price": 95.0} for _ in range(20)],
        ))

        self.assertTrue(should_continue)
        self.assertEqual(self.submitted_orders[-1]["exit_reason"], "STOP_LOSS")
        self.assertEqual(self.submitted_orders[-1]["volume"], 100.0)

    def test_buy_execution_blocks_unresolved_market(self):
        profile = ExchangeCycleProfile(
            exchange_key="bithumb",
            reconcile_label="",
            decision_exchange="bithumb",
            log_prefix="",
            extra_excluded_markets=frozenset(),
            create_screener=self.create_screener,
            cycle_start_label="5분 AI 퀀트 트레이딩",
            tier_label="스마트 자산 티어",
            tier_top_wording="스크리닝 상위",
            summary_label="자산 요약",
            btc_crash_label="비트코인 급락 위험 감지",
        )

        class UnresolvedJournal:
            def has_unresolved_market(self, market):
                return True

            def has_active_exit_order(self, market):
                return False

        self.journal = UnresolvedJournal()
        buy_profile = ExchangeBuyProfile(
            exchange_name="bithumb",
            block_unresolved_market=True,
        )
        engine = self._build_engine(profile, buy_profile=buy_profile)
        should_continue = engine.process_buy_execution(MarketBuyInputs(
            exchange=self.exchange,
            market="KRW-XRP",
            korean_name="리플",
            candidate_type="CONFIRMED",
            entry_price=100.0,
            target_price=110.0,
            stop_loss=95.0,
            alloc_pct=0.35,
            reason="테스트",
            use_recovery_rebound=False,
            selected_entry={},
            coin_available=0.0,
            coin_value=0.0,
            krw_available=1_000_000.0,
            current_price=100.0,
            orderbook={"market": "KRW-XRP"},
            candles_5m=[{"trade_price": 100.0} for _ in range(20)],
            is_bot_paused=False,
            is_kill_switch=False,
            is_entry_ready=True,
            is_btc_crashing=False,
            btc_status_msg="정상",
            current_total_equity=2_000_000.0,
            held_markets=["KRW-BTC"],
            dyn_max_positions=3,
            dyn_max_pos_pct=0.35,
            now_str="2026-09-02 16:00:00",
            audit_decision=lambda *args, **kwargs: None,
        ))

        self.assertTrue(should_continue)
        self.assertEqual(self.submitted_orders, [])

    def test_run_cycle_suffix_prunes_stale_strategies(self):
        profile = ExchangeCycleProfile(
            exchange_key="bithumb",
            reconcile_label="",
            decision_exchange="bithumb",
            log_prefix="",
            extra_excluded_markets=frozenset(),
            create_screener=self.create_screener,
            cycle_start_label="5분 AI 퀀트 트레이딩",
            tier_label="스마트 자산 티어",
            tier_top_wording="스크리닝 상위",
            summary_label="자산 요약",
            btc_crash_label="비트코인 급락 위험 감지",
        )
        latest_strategies = {"KRW-XRP": {"action": "HOLD"}, "KRW-OLD": {"action": "HOLD"}}
        saved_payloads: list[dict] = []

        class CacheManager:
            def save_cache(self, data):
                saved_payloads.append(dict(data))

        engine = self._build_engine(profile)
        engine.context.latest_strategies = latest_strategies
        engine.context.strategy_cache_manager = CacheManager()
        prefix = types.SimpleNamespace(
            target_markets=["KRW-XRP"],
            held_markets=["KRW-BTC"],
        )
        engine.run_cycle_suffix(prefix)

        self.assertIn("KRW-XRP", latest_strategies)
        self.assertNotIn("KRW-OLD", latest_strategies)
        self.assertNotIn("KRW-OLD", saved_payloads[0])

    def test_should_skip_market_honors_excluded_set(self):
        profile = ExchangeCycleProfile(
            exchange_key="upbit",
            reconcile_label="업비트 ",
            decision_exchange="upbit",
            log_prefix="업비트 ",
            extra_excluded_markets=frozenset({"KRW-HOLO", "HOLO"}),
            create_screener=self.create_screener,
            cycle_start_label="업비트 5분 AI 퀀트 트레이딩",
            tier_label="업비트 스마트 자산 티어",
            tier_top_wording="상위",
            summary_label="업비트 자산 요약",
            btc_crash_label="업비트 비트코인 급락 위험 감지",
            skip_excluded_markets_in_loop=True,
        )
        engine = self._build_engine(profile)

        self.assertTrue(engine._should_skip_market("KRW-HOLO", frozenset({"KRW-HOLO", "HOLO"})))
        self.assertTrue(engine._should_skip_market("HOLO", frozenset({"KRW-HOLO", "HOLO"})))
        self.assertFalse(engine._should_skip_market("KRW-XRP", frozenset({"KRW-HOLO", "HOLO"})))


if __name__ == "__main__":
    unittest.main()
