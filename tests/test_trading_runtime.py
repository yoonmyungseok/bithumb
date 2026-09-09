import os
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import MagicMock, patch

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
    def __init__(self):
        self.candle_calls = 0
        self.orderbook_calls = 0
        self.price_calls = 0

    def get_balances(self):
        return {
            "KRW": {"balance": 1_000_000.0, "locked": 0.0},
            "BTC": {"balance": 0.01, "locked": 0.0, "avg_buy_price": 90_000_000.0},
        }

    def get_candles(self, unit=5, count=30, market="KRW-BTC", to=None):
        self.candle_calls += 1
        price = 95_000_000.0 if market == "KRW-BTC" else 100.0
        return [{"market": market, "trade_price": price, "unit": unit} for _ in range(max(count, 5))]

    def get_orderbook(self, market="KRW-BTC"):
        self.orderbook_calls += 1
        return {"market": market, "orderbook_units": [{"ask_price": 101.0, "bid_price": 99.0}]}

    def get_current_price(self, market="KRW-BTC"):
        self.price_calls += 1
        return 95_000_000.0 if market == "KRW-BTC" else 100.0

    def get_korean_name(self, market="KRW-BTC"):
        return "비트코인" if market == "KRW-BTC" else market

    def get_open_orders(self, market=None):
        return []

    def get_tickers(self, markets):
        return [{"market": market, "trade_price": 100.0} for market in markets]

    def get_orderbooks(self, markets):
        return [{"market": market, "orderbook_units": []} for market in markets]


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

            def is_entry_ready(self):
                # 기본 fixture는 대사 완료 상태로 두어 일반 사이클 회귀 동작을 보존한다.
                return True

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

            def get_whale_flow_summary(self, market):
                return ""

            def get_health_status(self, market=None):
                return {"is_healthy": True, "status": "OK"}

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
        candles_4h = [{"trade_price": 100.0 + i} for i in range(21)]
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
            candles_4h=candles_4h,
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
            momentum_phase="CONFIRMED",
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

    @patch("trading_runtime.recovery_rebound_signal", return_value={"allow_buy": False})
    @patch("trading_runtime.entry_signal")
    @patch("trading_runtime.select_completed_candles")
    @patch("trading_runtime.StrategyPolicy.is_new_listing_enforcement_enabled", return_value=True)
    def test_new_listing_path_activates_without_four_hour_gate(self, _mock_enforcement, mock_select_candles, mock_entry_signal, _mock_recovery):
        mock_select_candles.side_effect = (
            lambda candles, minimum_count=25: candles[1:1 + minimum_count]
            if len(candles) >= minimum_count + 1 else []
        )
        mock_entry_signal.return_value = {
            "allow_buy": True,
            "reason": "신규상장 5분 돌파 테스트",
            "entry_price": 328.0,
            "target_price": 344.0,
            "stop_loss": 320.0,
            "alpha_score": 82,
            "checklist_details": {"alpha_score": 82},
        }
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
        audit_calls: list[tuple] = []

        class FakeAnalyzer:
            def analyze(self, **kwargs):
                return {
                    "status": "ACTIVE",
                    "action": "BUY",
                    "entry_price": 328.0,
                    "target_price": 344.0,
                    "stop_loss": 320.0,
                    "alloc_pct": 0.15,
                    "reason": "Gemini AI 신규상장 승인",
                    "alpha_score": 82,
                }

        candles_5m = [
            {
                "trade_price": 320.0 + idx,
                "opening_price": 319.0 + idx,
                "high_price": 321.0 + idx,
                "candle_acc_trade_volume": 1000.0 * (idx + 1),
            }
            for idx in range(7)
        ]
        result = engine.process_entry_gating(MarketEntryInputs(
            exchange=self.exchange,
            market="KRW-USELESS",
            korean_name="유쓸리스",
            candidate_type="MOMENTUM_BREAKOUT",
            candidate_metadata={
                "acc_trade_price_24h": 3_100_000_000.0,
                "change_rate": 0.0615,
                "relative_strength": 0.069,
                "momentum_phase": "EARLY",
            },
            analyzer=FakeAnalyzer(),
            coin_available=0.0,
            avg_buy_price=0.0,
            current_price=328.0,
            coin_value=0.0,
            krw_available=1_000_000.0,
            candles_5m=candles_5m,
            candles_1h=[{"trade_price": 300.0}],
            candles_4h=[{"trade_price": 300.0, "candle_date_time_kst": "2026-09-08T12:00:00"}],
            orderbook={"market": "KRW-USELESS", "total_bid_size": 2000.0, "total_ask_size": 1000.0},
            btc_regime="NORMAL",
            btc_status_msg="정상",
            is_btc_crashing=False,
            is_cooldown=False,
            is_extreme_fear=False,
            is_bot_paused=False,
            is_kill_switch=False,
            is_entry_ready=True,
            dyn_max_pos_pct=0.35,
            now_str="2026-09-08 14:40:49",
            audit_decision=lambda *args, **kwargs: audit_calls.append(args),
        ))

        self.assertFalse(result.should_continue)
        self.assertEqual(result.action, "BUY")
        self.assertTrue(result.use_new_listing)
        self.assertEqual(result.effective_candidate_type, "NEW_LISTING")

    @patch("trading_runtime.recovery_rebound_signal", return_value={"allow_buy": False})
    @patch("trading_runtime.entry_signal", return_value={"allow_buy": False, "reason": "관망", "entry_price": 100.0, "target_price": 103.0, "stop_loss": 98.0})
    def test_insufficient_candles_block_before_entry_signal(self, _mock_entry, _mock_recovery):
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
            candles_5m=[{"trade_price": 100.0} for _ in range(3)],
            candles_1h=[{"trade_price": 100.0}],
            candles_4h=[{"trade_price": 100.0}],
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
            now_str="2026-09-08 14:40:49",
            audit_decision=lambda *args, **kwargs: None,
        ))

        self.assertTrue(result.should_continue)
        _mock_entry.assert_not_called()

    @patch("trading_runtime.recovery_rebound_signal", return_value={"allow_buy": False})
    @patch("trading_runtime.entry_signal")
    @patch("trading_runtime.select_completed_candles")
    def test_upbit_new_listing_bypasses_twenty_candle_gate(
        self, mock_select_candles, mock_entry_signal, _mock_recovery,
    ):
        """업비트 require_minimum_candles=True에서도 신규상장(5분 11개)은 20개 게이트를 우회한다."""
        mock_select_candles.side_effect = (
            lambda candles, minimum_count=25: candles[1:1 + minimum_count]
            if len(candles) >= minimum_count + 1 else []
        )
        mock_entry_signal.return_value = {
            "allow_buy": False,
            "reason": "신규상장 관망",
            "entry_price": 28.9,
            "target_price": 30.0,
            "stop_loss": 28.0,
            "alpha_score": 60,
        }
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
        candles_5m = [
            {
                "trade_price": 28.0 + idx * 0.1,
                "opening_price": 27.9 + idx * 0.1,
                "high_price": 28.2 + idx * 0.1,
                "candle_acc_trade_volume": 1000.0 * (idx + 1),
                "candle_date_time_kst": f"2026-09-08T15:{30 - idx:02d}:00",
            }
            for idx in range(11)
        ]
        result = engine.process_entry_gating(MarketEntryInputs(
            exchange=self.exchange,
            market="KRW-CP",
            korean_name="클러스터프로토콜",
            candidate_type="MOMENTUM_BREAKOUT",
            candidate_metadata={
                "acc_trade_price_24h": 33_800_000_000.0,
                "change_rate": 0.050,
                "relative_strength": 0.0239,
                "momentum_phase": "EARLY",
            },
            analyzer=None,
            coin_available=0.0,
            avg_buy_price=0.0,
            current_price=28.9,
            coin_value=0.0,
            krw_available=1_000_000.0,
            candles_5m=candles_5m,
            candles_1h=[{"trade_price": 28.0}],
            candles_4h=[{"trade_price": 28.0, "candle_date_time_kst": "2026-09-08T12:00:00"}],
            orderbook={"market": "KRW-CP", "total_bid_size": 2000.0, "total_ask_size": 1000.0},
            btc_regime="NORMAL",
            btc_status_msg="정상",
            is_btc_crashing=False,
            is_cooldown=False,
            is_extreme_fear=False,
            is_bot_paused=False,
            is_kill_switch=False,
            is_entry_ready=True,
            dyn_max_pos_pct=0.35,
            now_str="2026-09-08 15:26:38",
            audit_decision=lambda *args, **kwargs: None,
        ))

        mock_entry_signal.assert_called_once()
        call_kwargs = mock_entry_signal.call_args.kwargs
        self.assertEqual(call_kwargs.get("entry_type"), "NEW_LISTING")
        self.assertEqual(result.action, "HOLD")


class UpbitMarketLoopSnapshotBudgetTests(unittest.TestCase):
    """업비트 12종목 시나리오에서 market_snapshot REST 호출 상한을 검증한다."""

    def test_upbit_market_loop_reduces_repeat_candle_calls(self):
        client = FakeExchangeClient()
        exchange = BithumbAdapter(client, data_dir="data/upbit", web_port=7978)
        orchestrator = TradingOrchestrator(__import__("logging").getLogger("test_upbit_perf"))
        profile = ExchangeCycleProfile(
            exchange_key="upbit",
            reconcile_label="업비트 ",
            decision_exchange="upbit",
            log_prefix="업비트 ",
            extra_excluded_markets=frozenset(),
            create_screener=lambda _exchange: types.SimpleNamespace(),
            cycle_start_label="업비트 5분 AI 퀀트 트레이딩",
            tier_label="업비트 스마트 자산 티어",
            tier_top_wording="상위",
            summary_label="업비트 자산 요약",
            btc_crash_label="업비트 비트코인 급락 위험 감지",
        )
        config = TradingRuntimeConfig(
            profile=profile,
            exit_profile=ExchangeExitProfile(),
            entry_profile=ExchangeEntryProfile(signal_exchange="upbit", recovery_db_exchange="upbit"),
            buy_profile=ExchangeBuyProfile(exchange_name="upbit"),
            env_file=None,
            interval_minutes=5,
            gemini_api_key="",
            is_bot_paused=lambda: False,
            min_order_krw=5000.0,
            orderbook_slippage_enforcement=False,
        )
        context = TradingRuntimeContext(
            logger=__import__("logging").getLogger("test_upbit_perf"),
            orchestrator=orchestrator,
            create_exchange_client=lambda: exchange,
            order_journal=types.SimpleNamespace(is_entry_ready=lambda: True),
            fill_processor=object(),
            trailing_tracker=types.SimpleNamespace(
                reconcile_markets=lambda held_markets: 0,
                check_position=lambda *args, **kwargs: (None, 0.0, 0.0, 0.0, 0.0),
                acquire_exit_lock=lambda market: True,
                release_exit_lock=lambda market: None,
                get_entry_time=lambda market: time.time(),
                set_entry_time=lambda market, ts: None,
            ),
            realtime_engine=types.SimpleNamespace(clean_stale_orders=lambda **kwargs: 0, requote_pending_orders=lambda: 0),
            risk_manager=types.SimpleNamespace(),
            risk_guard=types.SimpleNamespace(),
            bot_controller=types.SimpleNamespace(get_dashboard_data=lambda: {}),
            ws_client=types.SimpleNamespace(update_subscriptions=lambda markets: None, get_whale_flow_summary=lambda market: "", get_health_status=lambda market=None: {"is_healthy": True}),
            decision_db=types.SimpleNamespace(),
            calculate_total_equity=lambda balances, exchange: 1_000_000.0,
            get_held_markets=lambda balances, exchange: [],
            get_portfolio_tiers=lambda equity: (3, 0.35, 3),
            order_executor=types.SimpleNamespace(),
            chart_renderer=types.SimpleNamespace(),
            cancel_bot_open_orders=lambda exchange, market=None: 0,
            cooldown_manager=types.SimpleNamespace(),
            trade_memory=types.SimpleNamespace(),
            latest_strategies={},
            strategy_cache_manager=types.SimpleNamespace(save_cache=lambda data: None),
        )
        engine = TradingCycleEngine(config, context)
        markets = [f"KRW-M{i:02d}" for i in range(12)]
        observed_at = time.monotonic()
        prefetched = {
            market: {
                "price": 100.0,
                "orderbook": {"market": market, "orderbook_units": []},
                "observed_at": observed_at,
            }
            for market in markets
        }
        candle_prefetch_cache = orchestrator.prefetch_cycle_candles(exchange, markets, 5, max_workers=4)
        baseline_candles = client.candle_calls

        prefix = types.SimpleNamespace(
            exchange=exchange,
            analyzer=None,
            target_markets=markets,
            held_markets=[],
            screened_candidate_metadata={},
            prefetched_market_inputs=prefetched,
            candle_prefetch_cache=candle_prefetch_cache,
            excluded_markets=frozenset(),
            is_btc_crashing=False,
            is_kill_switch=False,
            is_cooldown=False,
            is_extreme_fear=False,
            btc_regime="NORMAL",
            btc_status_msg="정상",
            current_total_equity=1_000_000.0,
            now_str="2026-09-08 16:00:00",
            dyn_max_positions=3,
            dyn_max_pos_pct=0.35,
            audit_decision=lambda *args, **kwargs: None,
        )

        with patch("trading_runtime.entry_signal", return_value={"allow_buy": False, "alpha_score": 10, "checklist": {"hard_gates": {"all_passed": False}}}):
            with patch.dict(os.environ, {"MAX_AI_CANDIDATES_PER_CYCLE": "4"}):
                engine.process_priority_exits = MagicMock(return_value=False)
                engine.process_entry_gating = MagicMock(
                    return_value=types.SimpleNamespace(should_continue=True, called_ai=False, action="HOLD"),
                )
                engine.run_market_loop(prefix)

        # 기존 경로(종목당 정렬+메인 2회 캔들 조회) 대비 30% 이상 절감: 12종목×3캔들×2회=72 대신
        # 사전조회 36 + 정렬 0 + 메인 잔고/호가만 추가되는 구조여야 한다.
        incremental_candles = client.candle_calls - baseline_candles
        legacy_estimate = len(markets) * 3 * 2
        self.assertLessEqual(incremental_candles, int(legacy_estimate * 0.7))
        self.assertEqual(client.orderbook_calls, 0)
        self.assertEqual(client.price_calls, 0)

    def test_bithumb_market_loop_uses_candle_prefetch_cache(self):
        """빗썸 마켓 루프에서도 캔들 프리패치 캐시가 적용되어 캔들 중복 호출이 억제되는지 검증"""
        client = FakeExchangeClient()
        exchange = BithumbAdapter(client)
        orchestrator = TradingOrchestrator(__import__("logging").getLogger("test_bithumb_budget"))
        profile = ExchangeCycleProfile(
            exchange_key="bithumb",
            reconcile_label="빗썸",
            decision_exchange="bithumb",
            log_prefix="[빗썸]",
            extra_excluded_markets=frozenset(),
            create_screener=lambda _ex: None,
            cycle_start_label="빗썸",
            tier_label="빗썸 티어",
            tier_top_wording="상위",
            summary_label="빗썸 요약",
            btc_crash_label="빗썸 BTC",
        )
        config = TradingRuntimeConfig(
            profile=profile,
            exit_profile=ExchangeExitProfile(),
            entry_profile=ExchangeEntryProfile(
                signal_exchange="bithumb",
                recovery_db_exchange="bithumb",
            ),
            buy_profile=ExchangeBuyProfile(exchange_name="bithumb"),
            env_file=".env",
            interval_minutes=5,
            gemini_api_key="",
            min_order_krw=5000.0,
            is_bot_paused=lambda: False,
        )
        context = TradingRuntimeContext(
            logger=__import__("logging").getLogger("test_bithumb_budget"),
            orchestrator=orchestrator,
            create_exchange_client=lambda: exchange,
            order_journal=types.SimpleNamespace(
                is_entry_ready=lambda: True,
                has_active_exit_order=lambda _m: False,
                orders=[],
            ),
            fill_processor=types.SimpleNamespace(),
            trailing_tracker=types.SimpleNamespace(
                reconcile_markets=lambda held_markets: 0,
                check_position=lambda *args, **kwargs: (None, 0.0, 0.0, 0.0, 0.0),
                acquire_exit_lock=lambda market: True,
                release_exit_lock=lambda market: None,
                get_entry_time=lambda market: time.time(),
                set_entry_time=lambda market, ts: None,
            ),
            realtime_engine=types.SimpleNamespace(clean_stale_orders=lambda **kwargs: 0, requote_pending_orders=lambda: 0),
            risk_manager=types.SimpleNamespace(),
            risk_guard=types.SimpleNamespace(),
            bot_controller=types.SimpleNamespace(get_dashboard_data=lambda: {}),
            ws_client=types.SimpleNamespace(update_subscriptions=lambda markets: None, get_whale_flow_summary=lambda market: "", get_health_status=lambda market=None: {"is_healthy": True}),
            decision_db=types.SimpleNamespace(),
            calculate_total_equity=lambda balances, exchange: 1_000_000.0,
            get_held_markets=lambda balances, exchange: [],
            get_portfolio_tiers=lambda equity: (3, 0.35, 3),
            order_executor=types.SimpleNamespace(),
            chart_renderer=types.SimpleNamespace(),
            cancel_bot_open_orders=lambda exchange, market=None: 0,
            cooldown_manager=types.SimpleNamespace(),
            trade_memory=types.SimpleNamespace(),
            latest_strategies={},
            strategy_cache_manager=types.SimpleNamespace(save_cache=lambda data: None),
        )
        engine = TradingCycleEngine(config, context)
        markets = [f"KRW-B{i:02d}" for i in range(6)]
        observed_at = time.monotonic()
        prefetched = {
            market: {
                "price": 100.0,
                "orderbook": {"market": market, "orderbook_units": []},
                "observed_at": observed_at,
            }
            for market in markets
        }
        candle_prefetch_cache = orchestrator.prefetch_cycle_candles(exchange, markets, 5, max_workers=3)
        baseline_candles = client.candle_calls

        prefix = types.SimpleNamespace(
            exchange=exchange,
            analyzer=None,
            target_markets=markets,
            held_markets=[],
            screened_candidate_metadata={},
            prefetched_market_inputs=prefetched,
            candle_prefetch_cache=candle_prefetch_cache,
            excluded_markets=frozenset(),
            is_btc_crashing=False,
            is_kill_switch=False,
            is_cooldown=False,
            is_extreme_fear=False,
            btc_regime="NORMAL",
            btc_status_msg="정상",
            current_total_equity=1_000_000.0,
            now_str="2026-09-08 16:00:00",
            dyn_max_positions=3,
            dyn_max_pos_pct=0.35,
            audit_decision=lambda *args, **kwargs: None,
        )

        with patch("trading_runtime.entry_signal", return_value={"allow_buy": False, "alpha_score": 10, "checklist": {"hard_gates": {"all_passed": False}}}):
            with patch.dict(os.environ, {"MAX_AI_CANDIDATES_PER_CYCLE": "4"}):
                engine.process_priority_exits = MagicMock(return_value=False)
                engine.process_entry_gating = MagicMock(
                    return_value=types.SimpleNamespace(should_continue=True, called_ai=False, action="HOLD"),
                )
                engine.run_market_loop(prefix)

        incremental_candles = client.candle_calls - baseline_candles
        # 사전조회된 캐시가 사용되어 루프 내 추가 캔들 호출이 대폭 억제되어야 함
        legacy_estimate = len(markets) * 3 * 2
        self.assertLessEqual(incremental_candles, int(legacy_estimate * 0.7))


if __name__ == "__main__":
    unittest.main()
