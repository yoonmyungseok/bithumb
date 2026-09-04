"""Shared five-minute cycle orchestration for dual-exchange entry points.

The cycle prefix, priority exit, entry gating, stop-loss, buy execution, and full
`run_cycle()` orchestration are centralized here.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from dotenv import load_dotenv

from exchange_adapter import ExchangeAdapter
from gemini_analyzer import GeminiAnalyzer
from order_safety import calculate_risk_position_size, evaluate_buy_orderbook_impact
from risk_manager import get_fear_and_greed_index, get_kst_now, get_kst_now_str
from runtime_config import load_runtime_risk_settings
from strategy_engine import (
    StrategyPolicy,
    calculate_relative_strength,
    calculate_vwap,
    entry_signal,
    is_night_session,
    recovery_rebound_signal,
    select_completed_candles,
)
from trading_orchestrator import TradingOrchestrator


def format_krw_display(amount: float) -> str:
    """Preserve the legacy small-balance decimal formatting used in cycle logs."""
    if (0 < amount < 100 or amount % 1 != 0) and amount < 1000:
        return f"{amount:,.2f}원"
    return f"{amount:,.0f}원"


@dataclass(frozen=True)
class ExchangeCycleProfile:
    """Exchange-specific labels and factories that must not be mixed."""

    exchange_key: str
    reconcile_label: str
    decision_exchange: str
    log_prefix: str
    extra_excluded_markets: frozenset[str] | Callable[[], frozenset[str]]
    create_screener: Callable[[ExchangeAdapter], Any]
    cycle_start_label: str
    tier_label: str
    tier_top_wording: str
    summary_label: str
    btc_crash_label: str
    markets_log_prefix: str = ""
    stale_orders_log_prefix: str = ""
    market_analysis_log_label: str = "AI 퀀트 분석 시작"
    skip_excluded_markets_in_loop: bool = False
    cycle_error_log_prefix: str = ""


@dataclass(frozen=True)
class ExchangeExitProfile:
    """Exchange-specific exit labels and ratio overrides."""

    partial_tp_log_prefix: str = ""
    partial_tp_stage1_name: str = "1차 30%"
    partial_tp_stage2_name: str = "2차 30%"
    partial_tp_stage2_ratio: float = 30.0 / 70.0
    render_trailing_chart: bool = True
    time_stop_log_prefix: str = ""
    time_stop_close_suffix: str = "신규 기회를 위해 시장가 전량 청산"
    time_stop_recheck_active_exit: bool = False
    entry_time_missing_log_template: str = (
        "⏱️ [{market}] 진입 시점 미등록 포지션 감지 ➜ 현재 시간으로 보정 등록 ({now_str})"
    )


@dataclass(frozen=True)
class ExchangeEntryProfile:
    """Exchange-specific entry gating labels and pre-check behavior."""

    signal_exchange: str
    recovery_db_exchange: str
    ws_unhealthy_label: str = "웹소켓"
    hold_reason_fallback: str = ""
    static_default_alloc_pct: float = 0.3
    use_dynamic_default_alloc: bool = False
    enforce_pre_entry_safety_gates: bool = False
    block_on_reentry_denied: bool = False
    require_minimum_candles: bool = False
    include_candidate_metadata_in_latest: bool = True
    whale_flow_requires_capability: bool = False
    continue_on_inactive_status: bool = False
    use_hold_price_fallbacks: bool = False


@dataclass(frozen=True)
class ExchangeBuyProfile:
    """Exchange-specific stop-loss and buy execution behavior."""

    exchange_name: str
    enable_cycle_stop_loss: bool = False
    render_stop_loss_chart: bool = True
    render_buy_chart: bool = True
    enforce_pre_buy_safety_gates: bool = False
    block_unresolved_market: bool = False
    block_duplicate_position: bool = False
    block_alt_on_btc_crash: bool = False
    use_slot_based_budget: bool = False
    use_entry_price_guard: bool = False
    use_simple_buy_log: bool = False
    buy_log_prefix: str = ""
    risk_guard_log_label: str = "리스크 가드"
    entry_label_recovery: str = "반등 전용 축소"
    entry_label_momentum: str = "모멘텀 돌파 소액"
    entry_label_default: str = "AI 승인"
    send_btc_crash_block_alert: Callable[[str, str, str], None] | None = None


@dataclass(frozen=True)
class TradingRuntimeConfig:
    profile: ExchangeCycleProfile
    exit_profile: ExchangeExitProfile
    entry_profile: ExchangeEntryProfile
    buy_profile: ExchangeBuyProfile
    env_file: str | None
    interval_minutes: int
    gemini_api_key: str
    is_bot_paused: Callable[[], bool]
    min_order_krw: float
    orderbook_slippage_enforcement: bool = False


@dataclass
class TradingRuntimeContext:
    logger: Any
    orchestrator: TradingOrchestrator
    create_exchange_client: Callable[[], ExchangeAdapter]
    order_journal: Any
    fill_processor: Any
    trailing_tracker: Any
    realtime_engine: Any
    risk_manager: Any
    risk_guard: Any
    bot_controller: Any
    ws_client: Any
    decision_db: Any
    calculate_total_equity: Callable[[dict[str, Any], Any], float]
    get_held_markets: Callable[[dict[str, Any], Any], list[str]]
    get_portfolio_tiers: Callable[[float], tuple[int, float, int]]
    order_executor: Any
    chart_renderer: Any
    cancel_bot_open_orders: Callable[[ExchangeAdapter, str | None], int]
    cooldown_manager: Any
    trade_memory: Any
    latest_strategies: dict[str, dict[str, Any]]
    strategy_cache_manager: Any


@dataclass
class MarketExitInputs:
    exchange: ExchangeAdapter
    market: str
    korean_name: str
    coin_available: float
    avg_buy_price: float
    current_price: float
    coin_value: float
    candles_5m: list[dict[str, Any]]
    btc_regime: str
    now_str: str
    candles_1h: list[dict[str, Any]] | None = None
    orderbook: dict[str, Any] | None = None
    analyzer: GeminiAnalyzer | None = None


@dataclass
class MarketEntryInputs:
    exchange: ExchangeAdapter
    market: str
    korean_name: str
    candidate_type: str
    candidate_metadata: dict[str, Any]
    analyzer: GeminiAnalyzer | None
    coin_available: float
    avg_buy_price: float
    current_price: float
    coin_value: float
    krw_available: float
    candles_5m: list[dict[str, Any]]
    candles_1h: list[dict[str, Any]]
    orderbook: dict[str, Any]
    btc_regime: str
    btc_status_msg: str
    is_btc_crashing: bool
    is_cooldown: bool
    is_extreme_fear: bool
    is_bot_paused: bool
    is_kill_switch: bool
    is_entry_ready: bool
    dyn_max_pos_pct: float
    now_str: str
    audit_decision: Callable[[str, str, str, list[str], dict[str, Any]], None]


@dataclass
class EntryGatingResult:
    should_continue: bool
    status: str = "ACTIVE"
    action: str = "HOLD"
    entry_price: float = 0.0
    target_price: float = 0.0
    stop_loss: float = 0.0
    alloc_pct: float = 0.0
    reason: str = ""
    use_recovery_rebound: bool = False
    use_momentum_breakout: bool = False
    selected_entry: dict[str, Any] = field(default_factory=dict)
    recovery_entry: dict[str, Any] = field(default_factory=dict)
    local_entry: dict[str, Any] = field(default_factory=dict)
    reentry_allowed: bool = True
    reentry_reason: str = ""
    is_holding: bool = False
    latest_strategy_record: dict[str, Any] = field(default_factory=dict)


@dataclass
class MarketStopLossInputs:
    exchange: ExchangeAdapter
    market: str
    korean_name: str
    coin_available: float
    avg_buy_price: float
    current_price: float
    coin_value: float
    stop_loss: float
    target_price: float
    reason: str
    candles_5m: list[dict[str, Any]]


@dataclass
class MarketBuyInputs:
    exchange: ExchangeAdapter
    market: str
    korean_name: str
    candidate_type: str
    entry_price: float
    target_price: float
    stop_loss: float
    alloc_pct: float
    reason: str
    use_recovery_rebound: bool
    selected_entry: dict[str, Any]
    coin_available: float
    coin_value: float
    krw_available: float
    current_price: float
    orderbook: dict[str, Any]
    candles_5m: list[dict[str, Any]]
    is_bot_paused: bool
    is_kill_switch: bool
    is_entry_ready: bool
    is_btc_crashing: bool
    btc_status_msg: str
    current_total_equity: float
    held_markets: list[str]
    dyn_max_positions: int
    dyn_max_pos_pct: float
    now_str: str
    audit_decision: Callable[[str, str, str, list[str], dict[str, Any]], None]


@dataclass
class CyclePrefixResult:
    exchange: ExchangeAdapter
    analyzer: GeminiAnalyzer | None
    now_dt: Any
    now_str: str
    raw_markets: str
    is_auto_mode: bool
    min_trade_val: float
    min_change: float
    max_change: float
    btc_crash_pct: float
    is_btc_crashing: bool
    btc_regime: str
    btc_status_msg: str
    balances: dict[str, Any]
    krw_available: float
    current_total_equity: float
    held_markets: list[str]
    is_kill_switch: bool
    daily_pnl: float
    is_cooldown: bool
    cd_minutes: int
    dyn_max_positions: int
    dyn_max_pos_pct: float
    dyn_top_count: int
    target_markets: list[str]
    screened_candidate_metadata: dict[str, dict[str, Any]]
    excluded_markets: frozenset[str]
    cycle_id: str
    audit_decision: Callable[[str, str, str, list[str], dict[str, Any]], None]
    bot_state_badge: str = field(default="")
    is_extreme_fear: bool = False


class TradingCycleEngine:
    """Runs shared prefix and priority exit stages of each five-minute cycle."""

    def __init__(self, config: TradingRuntimeConfig, context: TradingRuntimeContext):
        self.config = config
        self.context = context
        self.profile = config.profile
        self.exit_profile = config.exit_profile
        self.entry_profile = config.entry_profile
        self.buy_profile = config.buy_profile

    def _load_cycle_environment(self) -> None:
        env_file = self.config.env_file
        if env_file and os.path.exists(env_file):
            load_dotenv(env_file, override=True)
        else:
            load_dotenv(override=True)

    def run_cycle_prefix(self) -> CyclePrefixResult:
        """환경 로드 -> REST 대사 -> 포트폴리오 -> BTC 레짐 -> 스크리닝 -> WS -> audit."""
        ctx = self.context
        profile = self.profile
        logger = ctx.logger

        self._load_cycle_environment()

        raw_markets = os.getenv("MARKETS", "AUTO").strip()
        is_auto_mode = raw_markets.upper() == "AUTO"
        min_trade_val = float(os.getenv("MIN_TRADE_VALUE", "1000000000"))
        min_change = float(os.getenv("MIN_CHANGE_RATE", "0.005"))
        max_change = float(os.getenv("MAX_CHANGE_RATE", "0.25"))
        risk_settings = load_runtime_risk_settings()
        btc_crash_pct = risk_settings.btc_crash_threshold_pct

        ctx.trailing_tracker.start_profit_pct = risk_settings.trailing_start_pct
        ctx.trailing_tracker.trailing_drop_pct = risk_settings.trailing_stop_pct
        ctx.risk_manager.max_loss_pct = risk_settings.max_daily_loss_pct

        now_dt = get_kst_now()
        now_str = get_kst_now_str()
        logger.info("============================================================")
        logger.info(f"🚀 [{profile.cycle_start_label} 사이클 가동: {now_str}]")
        logger.info("============================================================")

        exchange = ctx.create_exchange_client()
        analyzer = GeminiAnalyzer(api_key=self.config.gemini_api_key) if self.config.gemini_api_key else None

        ctx.orchestrator.reconcile_orders(
            exchange, ctx.order_journal, ctx.fill_processor, label=profile.reconcile_label,
        )

        snapshot = ctx.orchestrator.refresh_portfolio(
            exchange,
            calculate_total_equity=ctx.calculate_total_equity,
            get_held_markets=ctx.get_held_markets,
            trailing_tracker=ctx.trailing_tracker,
            realtime_engine=ctx.realtime_engine,
            risk_manager=ctx.risk_manager,
            risk_guard=ctx.risk_guard,
            get_portfolio_tiers=ctx.get_portfolio_tiers,
            now=now_dt,
        )

        balances = snapshot.balances
        krw_available = snapshot.krw_available
        current_total_equity = snapshot.total_equity
        held_markets = snapshot.held_markets

        if snapshot.stale_states:
            logger.info("보유 잔고와 불일치하는 과거 트레일링 상태 %d건 자동 정리", snapshot.stale_states)
        if snapshot.canceled_stale > 0 or snapshot.requoted > 0:
            logger.info(
                "%s미체결 주문 정리/정정: 취소 %d건, 정정 %d건",
                profile.stale_orders_log_prefix,
                snapshot.canceled_stale,
                snapshot.requoted,
            )

        is_kill_switch = snapshot.is_kill_switch
        daily_pnl = snapshot.daily_pnl
        is_cooldown = snapshot.is_cooldown
        cd_minutes = snapshot.cooldown_minutes
        dyn_max_positions = snapshot.max_positions
        dyn_max_pos_pct = snapshot.max_position_pct
        dyn_top_count = snapshot.top_count

        tot_disp = format_krw_display(current_total_equity)
        logger.info(
            f"📊 [{profile.tier_label}] 총 자산 {tot_disp} ➜ 최대 {dyn_max_positions}종목 분할 "
            f"(종목당 {dyn_max_pos_pct * 100:.0f}% 한도, {profile.tier_top_wording} {dyn_top_count}개)"
        )

        fng = get_fear_and_greed_index()
        is_extreme_fear = bool(fng.get("is_extreme_fear", False))

        is_btc_crashing, btc_regime, btc_status_msg = ctx.orchestrator.classify_market_regime(
            exchange,
            interval_minutes=self.config.interval_minutes,
            crash_threshold_pct=btc_crash_pct,
            analyzer=analyzer,
            fng_index=fng,
        )
        ctx.trailing_tracker.set_macro_defensive_mode(is_btc_crashing)
        if is_btc_crashing:
            logger.warning(
                f"⚠️ [{profile.btc_crash_label}] 레짐: {btc_regime} ({btc_status_msg}) "
                f"➜ 보유 알트코인 비상 방어 모드 가동"
            )
        krw_disp = format_krw_display(krw_available)
        logger.info(
            f"📊 [{profile.summary_label}] 총 자산: {tot_disp} | 원화: {krw_disp} | "
            f"당일 손익: {daily_pnl * 100:+.2f}% | 공포탐욕: {fng['desc']}"
        )

        is_paused = self.config.is_bot_paused()
        if is_paused:
            bot_state_badge = "⏸️ 일시정지 중"
        elif is_kill_switch:
            bot_state_badge = "🛑 킬스위치 발동"
        elif is_cooldown:
            bot_state_badge = "❄️ 쿨다운 대기"
        else:
            bot_state_badge = "🟢 정상 가동 중"

        ctx.bot_controller.get_dashboard_data()

        screened_candidate_metadata: dict[str, dict[str, Any]] = {}

        def capture_screened_candidates(candidates: list[dict[str, Any]]) -> None:
            screened_candidate_metadata.clear()
            for candidate in candidates:
                market_code = str(candidate.get("market", "")).upper()
                if market_code:
                    screened_candidate_metadata[market_code] = dict(candidate)

        target_markets = ctx.orchestrator.select_target_markets(
            exchange,
            held_markets=held_markets,
            is_auto_mode=is_auto_mode,
            raw_markets=raw_markets,
            max_positions=dyn_max_positions,
            top_count=dyn_top_count,
            create_screener=lambda: profile.create_screener(exchange),
            btc_regime=btc_regime,
            analyzer=analyzer,
            on_screened_candidates=capture_screened_candidates,
        )
        logger.info(
            f"{profile.markets_log_prefix}이번 사이클 최종 분석 대상 마켓 "
            f"({len(target_markets)}개): {target_markets}"
        )

        ctx.ws_client.update_subscriptions(list(dict.fromkeys(target_markets + held_markets + ["KRW-BTC"])))

        ctx.decision_db.purge_strategy_decisions(
            profile.decision_exchange, time.time() - 30 * 24 * 60 * 60,
        )
        cycle_id = now_dt.strftime("%Y%m%d%H%M%S")
        decision_exchange = profile.decision_exchange

        def audit_decision(
            market: str, action: str, policy_mode: str, reasons: list[str], payload: dict[str, Any],
        ) -> None:
            try:
                ctx.decision_db.record_strategy_decision(
                    exchange=decision_exchange,
                    cycle_id=cycle_id,
                    market=market,
                    action=action,
                    policy_mode=policy_mode,
                    block_reasons=reasons,
                    payload=payload,
                )
            except Exception as exc:
                logger.warning("[%s] 전략 판단 이력 저장 실패: %s", market, exc)

        excluded_source = profile.extra_excluded_markets
        excluded_markets = excluded_source() if callable(excluded_source) else excluded_source

        return CyclePrefixResult(
            exchange=exchange,
            analyzer=analyzer,
            now_dt=now_dt,
            now_str=now_str,
            raw_markets=raw_markets,
            is_auto_mode=is_auto_mode,
            min_trade_val=min_trade_val,
            min_change=min_change,
            max_change=max_change,
            btc_crash_pct=btc_crash_pct,
            is_btc_crashing=is_btc_crashing,
            btc_regime=btc_regime,
            btc_status_msg=btc_status_msg,
            balances=balances,
            krw_available=krw_available,
            current_total_equity=current_total_equity,
            held_markets=held_markets,
            is_kill_switch=is_kill_switch,
            daily_pnl=daily_pnl,
            is_cooldown=is_cooldown,
            cd_minutes=cd_minutes,
            dyn_max_positions=dyn_max_positions,
            dyn_max_pos_pct=dyn_max_pos_pct,
            dyn_top_count=dyn_top_count,
            target_markets=target_markets,
            screened_candidate_metadata=screened_candidate_metadata,
            excluded_markets=excluded_markets,
            cycle_id=cycle_id,
            audit_decision=audit_decision,
            bot_state_badge=bot_state_badge,
            is_extreme_fear=is_extreme_fear,
        )

    def process_priority_exits(self, market_inputs: MarketExitInputs) -> bool:
        """최우선 청산(분할익절·트레일링·타임스탑) 처리. True면 마켓 루프 continue."""
        ctx = self.context
        exit_profile = self.exit_profile
        logger = ctx.logger
        min_order_krw = self.config.min_order_krw

        exchange = market_inputs.exchange
        market = market_inputs.market
        korean_name = market_inputs.korean_name
        coin_available = market_inputs.coin_available
        avg_buy_price = market_inputs.avg_buy_price
        current_price = market_inputs.current_price
        coin_value = market_inputs.coin_value
        candles_5m = market_inputs.candles_5m
        btc_regime = market_inputs.btc_regime
        now_str = market_inputs.now_str

        if not (coin_value >= min_order_krw and avg_buy_price > 0 and not ctx.order_journal.has_active_exit_order(market)):
            return False

        action_type, peak_p, _trigger_p, peak_profit_pct, realized_profit_pct = (
            ctx.trailing_tracker.check_position(market, current_price, avg_buy_price)
        )

        if action_type in ("PARTIAL_TP", "PARTIAL_TP_1", "PARTIAL_TP_2"):
            is_stage2 = action_type == "PARTIAL_TP_2"
            sell_ratio = exit_profile.partial_tp_stage2_ratio if is_stage2 else StrategyPolicy.PARTIAL_TP_1_RATIO
            sell_vol = coin_available * sell_ratio
            sell_val = sell_vol * current_price
            stage_name = exit_profile.partial_tp_stage2_name if is_stage2 else exit_profile.partial_tp_stage1_name
            if sell_val >= min_order_krw and ctx.trailing_tracker.acquire_exit_lock(market):
                try:
                    logger.info(
                        f"🎉 [{korean_name} / {market} {exit_profile.partial_tp_log_prefix}{stage_name} 분할익절 발동] "
                        f"현재가 {current_price:,.2f}원(+{realized_profit_pct:.2f}%). {stage_name} 시장가 익절!"
                    )
                    ctx.cancel_bot_open_orders(exchange, market)
                    ctx.order_executor.submit(
                        exchange,
                        market=market,
                        side="ask",
                        volume=sell_vol,
                        ord_type="market",
                        position_id=market,
                        exit_reason=f"PARTIAL_TP_{2 if is_stage2 else 1}",
                        avg_buy_price=avg_buy_price,
                    )
                finally:
                    ctx.trailing_tracker.release_exit_lock(market)
            return False

        if action_type == "TRAILING_STOP":
            if ctx.trailing_tracker.acquire_exit_lock(market):
                try:
                    logger.info(
                        f"🎯 [{korean_name} / {market} 트레일링 스탑 익절 발동] "
                        f"최고점 {peak_p:,.2f}원(+{peak_profit_pct:.2f}%) ➜ "
                        f"현재 {current_price:,.2f}원(+{realized_profit_pct:.2f}%). 잔여 전량 시장가 익절!"
                    )
                    ctx.cancel_bot_open_orders(exchange, market)
                    ctx.order_executor.submit(
                        exchange,
                        market=market,
                        side="ask",
                        volume=coin_available,
                        ord_type="market",
                        position_id=market,
                        exit_reason="TRAILING_STOP",
                        avg_buy_price=avg_buy_price,
                    )
                    if exit_profile.render_trailing_chart:
                        ctx.chart_renderer.render_trade_chart(
                            market=market,
                            korean_name=korean_name,
                            candles=candles_5m,
                            entry_price=avg_buy_price,
                            target_price=peak_p,
                            stop_loss=avg_buy_price,
                            action="SELL",
                        )
                finally:
                    ctx.trailing_tracker.release_exit_lock(market)
            return True

        if not (coin_value >= min_order_krw and avg_buy_price > 0 and not ctx.order_journal.has_active_exit_order(market)):
            return False

        entry_ts = ctx.trailing_tracker.get_entry_time(market)
        if entry_ts <= 0:
            entry_ts = time.time()
            ctx.trailing_tracker.set_entry_time(market, entry_ts)
            logger.info(
                exit_profile.entry_time_missing_log_template.format(market=market, now_str=now_str)
            )

        hold_duration_sec = time.time() - entry_ts
        pnl_pct_current = ((current_price - avg_buy_price) / avg_buy_price) * 100.0

        # [1순위] Gemini AI 기보유 포지션 동적 관리 (긴급 탈출 / 러너 추세 추종 / 손절선 상향)
        analyzer = getattr(market_inputs, "analyzer", None)
        if analyzer is not None and hasattr(analyzer, "evaluate_holding_position") and candles_5m:
            try:
                ai_eval = analyzer.evaluate_holding_position(
                    market=market,
                    current_price=current_price,
                    avg_buy_price=avg_buy_price,
                    candles=candles_5m,
                    candles_1h=getattr(market_inputs, "candles_1h", None),
                    orderbook=getattr(market_inputs, "orderbook", None),
                    hold_duration_sec=hold_duration_sec,
                    btc_context=btc_regime,
                )
                ai_action = ai_eval.get("action", "HOLD")
                if ai_action == "EMERGENCY_EXIT" and ctx.trailing_tracker.acquire_exit_lock(market):
                    try:
                        logger.warning(
                            f"🚨 [{korean_name} / {market} AI 긴급 전량 탈출 발동] "
                            f"현재가 {current_price:,.2f}원({pnl_pct_current:+.2f}%). 사유: {ai_eval.get('reason')}"
                        )
                        ctx.cancel_bot_open_orders(exchange, market)
                        ctx.order_executor.submit(
                            exchange,
                            market=market,
                            side="ask",
                            volume=coin_available,
                            ord_type="market",
                            position_id=market,
                            exit_reason="AI_EMERGENCY_EXIT",
                            avg_buy_price=avg_buy_price,
                        )
                        if exit_profile.render_trailing_chart:
                            ctx.chart_renderer.render_trade_chart(
                                market=market,
                                korean_name=korean_name,
                                candles=candles_5m,
                                entry_price=avg_buy_price,
                                target_price=current_price,
                                stop_loss=current_price,
                                action="SELL",
                            )
                        return True
                    finally:
                        ctx.trailing_tracker.release_exit_lock(market)
                elif ai_action in ("RUNNER_HOLD", "TIGHTEN_STOP"):
                    ctx.trailing_tracker.update_dynamic_exit(
                        market,
                        target_price=ai_eval.get("adjusted_target_price"),
                        stop_loss=ai_eval.get("adjusted_stop_loss"),
                        runner_mode=(ai_action == "RUNNER_HOLD"),
                    )
            except Exception as e:
                logger.debug(f"[{market}] AI 포지션 평가 예외 무시 (로컬 룰 유지): {e}")

        # AI 동적 손절선(Tightened Stop) 도달 여부 검사
        dynamic_sl = ctx.trailing_tracker.get_dynamic_stop_loss(market)
        if dynamic_sl and current_price <= dynamic_sl and ctx.trailing_tracker.acquire_exit_lock(market):
            try:
                logger.info(
                    f"🛡️ [{korean_name} / {market} AI 상향 손절선 도달 안착 청산] "
                    f"현재가 {current_price:,.2f}원 <= 상향 손절가 {dynamic_sl:,.2f}원. 시장가 보호 청산!"
                )
                ctx.cancel_bot_open_orders(exchange, market)
                ctx.order_executor.submit(
                    exchange,
                    market=market,
                    side="ask",
                    volume=coin_available,
                    ord_type="market",
                    position_id=market,
                    exit_reason="AI_TIGHTENED_STOP",
                    avg_buy_price=avg_buy_price,
                )
                return True
            finally:
                ctx.trailing_tracker.release_exit_lock(market)

        # 최신 BTC 레짐을 트레일링 트래커에 동기화
        if hasattr(ctx.trailing_tracker, "set_btc_regime"):
            ctx.trailing_tracker.set_btc_regime(btc_regime)

        if btc_regime == "RISK_OFF":
            effective_time_stop = StrategyPolicy.TIME_STOP_SECONDS_RISK_OFF
            effective_max_hold = StrategyPolicy.TIME_STOP_MAX_HOLD_SECONDS
        elif btc_regime == "BULL_TREND":
            effective_time_stop = StrategyPolicy.BULL_TIME_STOP_SECONDS
            effective_max_hold = StrategyPolicy.BULL_TIME_STOP_MAX_HOLD_SECONDS
        elif is_night_session():
            effective_time_stop = StrategyPolicy.NIGHT_TIME_STOP_SECONDS
            effective_max_hold = StrategyPolicy.TIME_STOP_MAX_HOLD_SECONDS
        else:
            effective_time_stop = StrategyPolicy.TIME_STOP_SECONDS_NORMAL
            effective_max_hold = StrategyPolicy.TIME_STOP_MAX_HOLD_SECONDS

        is_holding_support = False
        is_trend_broken = False
        is_above_vwap = True
        if candles_5m:
            vwap_data = calculate_vwap(candles_5m)
            is_above_vwap = vwap_data.get("is_above", True)
            if len(candles_5m) >= 20:
                prices_5m = [float(c.get("trade_price", 0.0)) for c in candles_5m]
                ma20_5m = sum(prices_5m[:20]) / 20.0
                ma5_5m = sum(prices_5m[:5]) / 5.0
                is_holding_support = (current_price >= ma20_5m) or is_above_vwap
                is_trend_broken = (ma5_5m < ma20_5m * 0.995) and (not is_above_vwap)

        be_threshold_pct = StrategyPolicy.TIME_STOP_BREAKEVEN_MIN_PNL_PCT * 100.0
        is_breakeven_or_profit = pnl_pct_current >= be_threshold_pct
        is_time_stop_profit_trigger = (
            hold_duration_sec >= effective_time_stop
            and is_breakeven_or_profit
            and (not is_holding_support or hold_duration_sec >= effective_max_hold)
        )
        is_time_stop_loss_trigger = (
            (hold_duration_sec >= effective_max_hold or (hold_duration_sec >= effective_time_stop and is_trend_broken))
            and (pnl_pct_current < be_threshold_pct)
        )
        # 상승장(BULL_TREND)에서는 단기 횡보 숨고르기가 정상적이므로 조기 모멘텀 탈출을 비활성화하여 털림 방지
        is_early_momentum_exit = (
            (btc_regime != "BULL_TREND")
            and hold_duration_sec >= StrategyPolicy.MOMENTUM_EARLY_EXIT_SECONDS
            and (-0.50 <= pnl_pct_current <= 0.30)
            and (not is_above_vwap)
            and is_trend_broken
        )

        is_time_stop_trigger = is_time_stop_profit_trigger or is_time_stop_loss_trigger or is_early_momentum_exit
        if exit_profile.time_stop_recheck_active_exit:
            is_time_stop_trigger = is_time_stop_trigger and not ctx.order_journal.has_active_exit_order(market)

        if not is_time_stop_trigger:
            return False

        exit_reason_label = "MOMENTUM_EARLY_EXIT" if is_early_momentum_exit else "TIME_STOP"
        if ctx.trailing_tracker.acquire_exit_lock(market):
            try:
                exit_desc = (
                    "15분 모멘텀 소멸 조기 본전 탈출"
                    if is_early_momentum_exit
                    else f"{effective_time_stop / 60:.0f}분 횡보 타임스탑"
                )
                logger.info(
                    f"⏳ [{korean_name} / {market}] {exit_profile.time_stop_log_prefix}{exit_desc} 발동! "
                    f"(레짐: {btc_regime}, 손익률: {pnl_pct_current:+.2f}%, 보유시간: {hold_duration_sec / 60:.0f}분) "
                    f"➜ {exit_profile.time_stop_close_suffix}"
                )
                ctx.cancel_bot_open_orders(exchange, market)
                ctx.order_executor.submit(
                    exchange,
                    market=market,
                    side="ask",
                    volume=coin_available,
                    ord_type="market",
                    position_id=market,
                    exit_reason=exit_reason_label,
                    avg_buy_price=avg_buy_price,
                )
            finally:
                ctx.trailing_tracker.release_exit_lock(market)
        return True

    def process_entry_gating(self, market_inputs: MarketEntryInputs) -> EntryGatingResult:
        """1차 퀀트 게이트 -> 반등/모멘텀/AI 전략 산출 및 audit 기록."""
        ctx = self.context
        entry_profile = self.entry_profile
        logger = ctx.logger
        min_order_krw = self.config.min_order_krw

        market = market_inputs.market
        korean_name = market_inputs.korean_name
        candidate_type = market_inputs.candidate_type
        candidate_metadata = market_inputs.candidate_metadata
        analyzer = market_inputs.analyzer
        exchange = market_inputs.exchange
        coin_available = market_inputs.coin_available
        avg_buy_price = market_inputs.avg_buy_price
        current_price = market_inputs.current_price
        coin_value = market_inputs.coin_value
        krw_available = market_inputs.krw_available
        candles_5m = market_inputs.candles_5m
        candles_1h = market_inputs.candles_1h
        orderbook = market_inputs.orderbook
        btc_regime = market_inputs.btc_regime
        btc_status_msg = market_inputs.btc_status_msg
        is_btc_crashing = market_inputs.is_btc_crashing
        is_cooldown = market_inputs.is_cooldown
        is_extreme_fear = market_inputs.is_extreme_fear
        dyn_max_pos_pct = market_inputs.dyn_max_pos_pct
        now_str = market_inputs.now_str
        audit_decision = market_inputs.audit_decision

        if entry_profile.enforce_pre_entry_safety_gates:
            if market_inputs.is_bot_paused or market_inputs.is_kill_switch or not market_inputs.is_entry_ready:
                if not market_inputs.is_entry_ready:
                    logger.warning("[%s] REST 주문 대사 미완료로 신규 매수를 차단합니다.", market)
                logger.info(f"[{market}] 봇 일시정지 또는 킬스위치 상태로 신규 매수 생략")
                audit_decision(
                    market, "BLOCKED", "SAFETY",
                    ["봇 일시정지, 킬스위치 또는 REST 주문 대사 미완료"],
                    {"btc_regime": btc_regime},
                )
                return EntryGatingResult(should_continue=True)

        reentry_allowed = True
        reentry_reason = ""
        if entry_profile.block_on_reentry_denied:
            reentry_allowed, reentry_reason = ctx.cooldown_manager.check_reentry_allowed(market, current_price)
            if not reentry_allowed:
                logger.info(f"[{market}] {reentry_reason}으로 신규 매수 생략")
                audit_decision(
                    market, "BLOCKED", "MARKET_COOLDOWN", [reentry_reason],
                    {"btc_regime": btc_regime, "current_price": current_price},
                )
                return EntryGatingResult(should_continue=True)

        if entry_profile.require_minimum_candles and (not candles_5m or len(candles_5m) < 20):
            logger.warning(f"[{market}] 캔들 데이터 부족으로 진입 생략")
            return EntryGatingResult(should_continue=True)

        completed_candles_5m = select_completed_candles(candles_5m, minimum_count=25)
        completed_candles_1h = select_completed_candles(candles_1h, minimum_count=20)
        if not completed_candles_5m or not completed_candles_1h:
            logger.warning("[%s] 5분/1시간 확정봉 데이터가 부족하거나 불일치하여 신규 매수 차단", market)
            return EntryGatingResult(should_continue=True)

        night_session_active = is_night_session()
        local_entry = entry_signal(
            candles=completed_candles_5m,
            candles_1h=completed_candles_1h,
            btc_regime=btc_regime,
            orderbook=orderbook,
            market=market,
            exchange=entry_profile.signal_exchange,
            entry_type=candidate_type,
            is_night=night_session_active,
        )

        if not entry_profile.block_on_reentry_denied:
            reentry_allowed, reentry_reason = ctx.cooldown_manager.check_reentry_allowed(market, current_price)

        is_holding = coin_value >= min_order_krw and avg_buy_price > 0
        ws_health = (
            ctx.ws_client.get_health_status(market=market)
            if hasattr(ctx.ws_client, "get_health_status")
            else {"is_healthy": True, "status": "OK"}
        )
        ws_healthy = ws_health.get("is_healthy", True)

        recovery_entry = recovery_rebound_signal(
            candles=completed_candles_5m,
            candles_1h=completed_candles_1h,
            btc_regime=btc_regime,
            orderbook=orderbook,
            market=market,
            exchange=entry_profile.signal_exchange,
            relative_strength=float(candidate_metadata.get("relative_strength", 0.0) or 0.0),
            candidate_trade_value=float(candidate_metadata.get("acc_trade_price_24h", 0.0) or 0.0),
            is_night=night_session_active,
        )
        recovery_window_start = max(0.0, float(ctx.risk_manager.cooldown_until_ts) - 1800.0)
        recovery_slot_available = not ctx.decision_db.has_recovery_entry_since(
            entry_profile.recovery_db_exchange, recovery_window_start,
        )
        use_recovery_rebound = (
            StrategyPolicy.RECOVERY_REBOUND_LIVE_ENABLED
            and is_cooldown and not is_btc_crashing and ws_healthy
            and candidate_type != "MOMENTUM_BREAKOUT"
            and not local_entry.get("allow_buy", False)
            and recovery_entry.get("allow_buy", False) and recovery_slot_available
        )
        selected_entry = recovery_entry if use_recovery_rebound else local_entry
        use_momentum_breakout = (
            candidate_type == "MOMENTUM_BREAKOUT"
            and not is_holding and reentry_allowed and not is_btc_crashing and ws_healthy
            and local_entry.get("allow_buy", False)
        )
        should_call_ai = (
            analyzer is not None
            and not is_holding
            and reentry_allowed
            and selected_entry.get("allow_buy", False)
            and not is_btc_crashing
            and ws_healthy
        )

        if use_recovery_rebound:
            strategy = {
                "status": "ACTIVE", "action": "BUY",
                "entry_price": selected_entry.get("entry_price", current_price),
                "target_price": selected_entry.get("target_price", current_price * 1.03),
                "stop_loss": selected_entry.get("stop_loss", current_price * 0.98),
                "alloc_pct": dyn_max_pos_pct * StrategyPolicy.RECOVERY_REBOUND_ALLOC_RATIO,
                "reason": f"[급락 후 반등 전용·축소 비중] {selected_entry.get('reason', '')}",
            }
        elif use_momentum_breakout:
            strategy = {
                "status": "ACTIVE", "action": "BUY",
                "entry_price": local_entry.get("entry_price", current_price),
                "target_price": local_entry.get("target_price", current_price * 1.03),
                "stop_loss": local_entry.get("stop_loss", current_price * 0.98),
                "alloc_pct": dyn_max_pos_pct * StrategyPolicy.MOMENTUM_BREAKOUT_ALLOC_RATIO,
                "reason": f"[확정봉 모멘텀 돌파·최초 소액] {local_entry.get('reason', '')}",
            }
        elif should_call_ai and analyzer is not None:
            feedback_context = ctx.trade_memory.get_feedback_context()
            if entry_profile.whale_flow_requires_capability and hasattr(ctx.ws_client, "get_whale_flow_summary"):
                whale_flow_context = ctx.ws_client.get_whale_flow_summary(market)
            elif entry_profile.whale_flow_requires_capability:
                whale_flow_context = ""
            else:
                whale_flow_context = ctx.ws_client.get_whale_flow_summary(market)
            btc_candles_5m = exchange.get_candles(
                unit=self.config.interval_minutes, count=30, market="KRW-BTC",
            )
            rs_info = calculate_relative_strength(candles_5m, btc_candles_5m)
            strategy = analyzer.analyze(
                market=market,
                current_price=current_price,
                candles=candles_5m,
                krw_balance=krw_available,
                coin_balance=coin_available,
                avg_buy_price=avg_buy_price,
                candles_1h=candles_1h,
                orderbook=orderbook,
                trade_memory_context=feedback_context,
                btc_context=f"{btc_status_msg} ({'급락 위험 감지' if is_btc_crashing else '정상 안정세'})",
                whale_context=whale_flow_context,
                rs_context=rs_info.get("desc", ""),
            )
        else:
            if entry_profile.use_hold_price_fallbacks:
                hold_entry_price = selected_entry.get("entry_price", current_price)
                hold_target_price = selected_entry.get("target_price", current_price * 1.03)
                hold_stop_loss = selected_entry.get("stop_loss", current_price * 0.98)
            else:
                hold_entry_price = selected_entry["entry_price"]
                hold_target_price = selected_entry["target_price"]
                hold_stop_loss = selected_entry["stop_loss"]

            hold_reason_tail = selected_entry.get("reason", entry_profile.hold_reason_fallback)
            quant_reason = (
                f"기보유 포지션 퀀트 감시 ({hold_reason_tail})"
                if is_holding
                else (
                    f"{reentry_reason}"
                    if not reentry_allowed
                    else (
                        f"BTC 레짐 경보 ({btc_status_msg})"
                        if is_btc_crashing
                        else (
                            f"{entry_profile.ws_unhealthy_label} 데이터 불안정 ({ws_health.get('status', 'UNHEALTHY')}) 진입 차단"
                            if not ws_healthy
                            else f"1차 퀀트 관망 대기: {hold_reason_tail}"
                        )
                    )
                )
            )
            strategy = {
                "status": "ACTIVE",
                "action": "HOLD",
                "entry_price": hold_entry_price,
                "target_price": hold_target_price,
                "stop_loss": hold_stop_loss,
                "alloc_pct": 0.0,
                "reason": quant_reason,
            }

        default_alloc = dyn_max_pos_pct if entry_profile.use_dynamic_default_alloc else entry_profile.static_default_alloc_pct
        status = strategy.get("status", "ACTIVE")
        action = strategy.get("action", "HOLD")
        entry_price = strategy.get("entry_price", current_price)
        if entry_profile.use_hold_price_fallbacks:
            target_price = strategy.get("target_price", selected_entry.get("target_price", current_price * 1.03))
            stop_loss = strategy.get("stop_loss", selected_entry.get("stop_loss", current_price * 0.98))
        else:
            target_price = strategy.get("target_price", selected_entry["target_price"])
            stop_loss = strategy.get("stop_loss", selected_entry["stop_loss"])
        alloc_pct = strategy.get("alloc_pct", default_alloc)
        reason = strategy.get("reason", "자동 분석")

        if not reentry_allowed and action == "BUY":
            action = "HOLD"
            reason = f"{reentry_reason} | {reason}"

        if action == "BUY" and not selected_entry.get("allow_buy", False):
            action = "HOLD"
            reason = f"정량 공통 진입 게이트 차단: {selected_entry.get('reason', '')} | {reason}"
        elif action == "BUY" and selected_entry.get("allow_buy", False):
            if entry_profile.use_hold_price_fallbacks:
                target_price = selected_entry.get("target_price", target_price)
                stop_loss = selected_entry.get("stop_loss", stop_loss)
            else:
                target_price = selected_entry["target_price"]
                stop_loss = selected_entry["stop_loss"]

        if is_holding:
            entry_price = avg_buy_price
            stop_loss = avg_buy_price * (1.0 - StrategyPolicy.STOP_LOSS_PCT)
            if ctx.trailing_tracker.is_breakeven_active(market):
                stop_loss = max(stop_loss, avg_buy_price * (1.0 + StrategyPolicy.BREAKEVEN_STOP_PCT))
            target_price = avg_buy_price * (
                1.0 + getattr(StrategyPolicy, "PROFIT_TARGET_PCT", StrategyPolicy.PARTIAL_TP_1_PCT)
            )

        alpha_val = selected_entry.get("alpha_score", 70)
        if alpha_val >= 85 and btc_regime != "RISK_OFF" and action == "BUY":
            alloc_pct = min(dyn_max_pos_pct * 1.3, 0.65)
            reason = f"[🔥알파 {alpha_val}점 A+ 특급 셋업 비중 확대(65%)] {reason}"
        elif alpha_val < 75 and btc_regime != "RISK_OFF" and action == "BUY":
            alloc_pct = dyn_max_pos_pct * 0.7

        if btc_regime == "RISK_OFF" and action == "BUY":
            alloc_ratio = StrategyPolicy.RISK_OFF_ALLOC_RATIO
            if alpha_val >= 80:
                alloc_ratio = min(0.8, alloc_ratio * 1.3)
            alloc_pct = alloc_pct * alloc_ratio
            reason = f"[BTC 약세 레짐 비중 {int(alloc_ratio * 100)}% 적용 & 알파 {alpha_val}점 엄선] {reason}"

        if is_extreme_fear and action == "BUY":
            alloc_pct = min(alloc_pct, 0.4)

        if candidate_type == "MOMENTUM_BREAKOUT" and action == "BUY":
            alloc_pct = min(alloc_pct, dyn_max_pos_pct * StrategyPolicy.MOMENTUM_BREAKOUT_ALLOC_RATIO)
            reason = f"[⚡모멘텀 돌파 최초 소액] {reason}"

        if is_night_session() and action == "BUY":
            alloc_pct = alloc_pct * StrategyPolicy.NIGHT_SESSION_ALLOC_RATIO
            reason = f"[🌙심야 세션 비중 {int(StrategyPolicy.NIGHT_SESSION_ALLOC_RATIO * 100)}% 적용] {reason}"

        logger.info(
            f"[{market}] 전략: ACTION={action}, 진입가={entry_price:,.2f}, "
            f"목표가={target_price:,.2f}, 손절가={stop_loss:,.2f}, 비중={alloc_pct * 100:.0f}%"
        )
        logger.info(f"[{market}] 근거: {reason}")

        alpha_score_val = int(strategy.get("alpha_score") or selected_entry.get("alpha_score", 0) or 0)
        factor_breakdown = (
            selected_entry.get("factor_breakdown")
            or selected_entry.get("checklist_details", {}).get("factor_breakdown")
            or selected_entry.get("checklist", {}).get("factor_breakdown")
            or selected_entry.get("checklist", {})
            or {}
        )
        allow_buy_val = bool(selected_entry.get("allow_buy", False))
        target_pct_val = (
            ((target_price - current_price) / current_price * 100)
            if current_price > 0 and target_price > 0 else 0.0
        )
        stop_pct_val = (
            ((stop_loss - current_price) / current_price * 100)
            if current_price > 0 and stop_loss > 0 else 0.0
        )
        rr_denom_val = max(1e-6, current_price - stop_loss)
        rr_ratio_val = (
            ((target_price - current_price) / rr_denom_val)
            if current_price > stop_loss > 0 and target_price > current_price else 0.0
        )

        latest_strategy_record: dict[str, Any] = {
            "market": market,
            "korean_name": korean_name,
            "status": status,
            "action": action,
            "current_price": current_price,
            "entry_price": entry_price,
            "target_price": target_price,
            "stop_loss": stop_loss,
            "alloc_pct": alloc_pct,
            "reason": reason,
            "alpha_score": alpha_score_val,
            "policy_mode": "RECOVERY_REBOUND" if use_recovery_rebound else "STANDARD",
            "allow_buy": allow_buy_val,
            "factor_breakdown": factor_breakdown,
            "target_pct": round(target_pct_val, 2),
            "stop_pct": round(stop_pct_val, 2),
            "risk_reward_ratio": round(rr_ratio_val, 2),
            "updated_at": now_str,
            "ACTION": action,
            "TARGET_PRICE": target_price,
            "STOP_LOSS": stop_loss,
            "REASON": reason,
        }
        if entry_profile.include_candidate_metadata_in_latest:
            latest_strategy_record["candidate_type"] = candidate_type
            latest_strategy_record["momentum_breakout"] = selected_entry.get("momentum_breakout", {})

        ctx.latest_strategies[market] = latest_strategy_record

        audit_decision(
            market, "BUY_APPROVED" if action == "BUY" else "HOLD",
            "RECOVERY_REBOUND" if use_recovery_rebound else "STANDARD",
            [] if action == "BUY" else [reason],
            {
                "current_price": current_price,
                "btc_regime": btc_regime,
                "is_loss_recovery_mode": is_cooldown,
                "candidate": candidate_metadata,
                "local_checklist": selected_entry.get("checklist_details", {}),
                "recovery_checklist": recovery_entry.get("recovery_checklist", {}),
            },
        )

        if entry_profile.continue_on_inactive_status and status != "ACTIVE":
            return EntryGatingResult(should_continue=True)

        return EntryGatingResult(
            should_continue=False,
            status=status,
            action=action,
            entry_price=entry_price,
            target_price=target_price,
            stop_loss=stop_loss,
            alloc_pct=alloc_pct,
            reason=reason,
            use_recovery_rebound=use_recovery_rebound,
            use_momentum_breakout=use_momentum_breakout,
            selected_entry=selected_entry,
            recovery_entry=recovery_entry,
            local_entry=local_entry,
            reentry_allowed=reentry_allowed,
            reentry_reason=reentry_reason,
            is_holding=is_holding,
            latest_strategy_record=latest_strategy_record,
        )

    def process_cycle_stop_loss(self, market_inputs: MarketStopLossInputs) -> bool:
        """5분 사이클 손절 검사. True면 마켓 루프 continue."""
        buy_profile = self.buy_profile
        if not buy_profile.enable_cycle_stop_loss:
            return False

        ctx = self.context
        logger = ctx.logger
        min_order_krw = self.config.min_order_krw

        market = market_inputs.market
        korean_name = market_inputs.korean_name
        exchange = market_inputs.exchange
        coin_available = market_inputs.coin_available
        avg_buy_price = market_inputs.avg_buy_price
        current_price = market_inputs.current_price
        coin_value = market_inputs.coin_value
        stop_loss = market_inputs.stop_loss

        if not (
            coin_value >= min_order_krw
            and stop_loss > 0
            and current_price <= stop_loss
            and not ctx.order_journal.has_active_exit_order(market)
        ):
            return False

        if ctx.trailing_tracker.acquire_exit_lock(market):
            try:
                logger.warning(
                    f"🚨 [{market} 손절 발생] 현재가({current_price:,.2f}원) <= 손절가({stop_loss:,.2f}원). 전량 시장가 매도!"
                )
                ctx.trailing_tracker.clear(market)
                ctx.cancel_bot_open_orders(exchange, market)
                ctx.order_executor.submit(
                    exchange,
                    market=market,
                    side="ask",
                    volume=coin_available,
                    ord_type="market",
                    position_id=market,
                    exit_reason="STOP_LOSS",
                    avg_buy_price=avg_buy_price,
                    expected_price=current_price,
                )
                # 손절 주문도 REST 대사 전에는 쿨다운·손익을 기록하지 않는다.
            finally:
                ctx.trailing_tracker.release_exit_lock(market)

        if buy_profile.render_stop_loss_chart:
            ctx.chart_renderer.render_trade_chart(
                market=market,
                korean_name=korean_name,
                candles=market_inputs.candles_5m,
                entry_price=avg_buy_price,
                target_price=market_inputs.target_price,
                stop_loss=stop_loss,
                action="SELL",
                reason=market_inputs.reason,
            )
        return True

    def process_buy_execution(self, market_inputs: MarketBuyInputs) -> bool:
        """신규 매수 실행. True면 마켓 루프 continue(차단 또는 완료 후 다음 종목)."""
        ctx = self.context
        buy_profile = self.buy_profile
        logger = ctx.logger
        min_order_krw = self.config.min_order_krw
        safe_order_krw = 5500.0

        market = market_inputs.market
        korean_name = market_inputs.korean_name
        exchange = market_inputs.exchange
        audit_decision = market_inputs.audit_decision

        if buy_profile.block_unresolved_market and ctx.order_journal.has_unresolved_market(market):
            logger.warning(
                "[%s] 이전 주문의 거래소 결과가 확정되지 않아 신규 매수를 차단합니다. data/order_journal.json을 확인하세요.",
                market,
            )
            return True

        if buy_profile.enforce_pre_buy_safety_gates:
            if market_inputs.is_bot_paused or not market_inputs.is_entry_ready:
                if not market_inputs.is_entry_ready:
                    logger.warning("[%s] REST 주문 대사 미완료로 신규 매수를 차단합니다.", market)
                logger.info(f"[{korean_name} / {market}] 봇이 일시정지 상태이므로 신규 매수를 건너뜁니다.")
                return True
            if market_inputs.is_kill_switch:
                logger.info(f"[{korean_name} / {market}] 킬 스위치 발동 상태이므로 신규 매수를 건너뜁니다.")
                return True

        if buy_profile.block_duplicate_position and market_inputs.coin_value >= min_order_krw:
            logger.info(
                f"[{korean_name} / {market}] 이미 보유 중인 포지션({market_inputs.coin_value:,.0f}원)이므로 중복 매수를 건너뜁니다 (보유 유지)."
            )
            return True

        if (
            buy_profile.block_alt_on_btc_crash
            and market != "KRW-BTC"
            and market_inputs.is_btc_crashing
        ):
            logger.warning(
                f"[{korean_name} / {market}] 비트코인 급락세({market_inputs.btc_status_msg})로 인해 알트코인 매수를 방어적으로 차단합니다."
            )
            if buy_profile.send_btc_crash_block_alert is not None:
                buy_profile.send_btc_crash_block_alert(market, korean_name, market_inputs.btc_status_msg)
            return True

        alloc_pct = market_inputs.alloc_pct
        dyn_max_pos_pct = market_inputs.dyn_max_pos_pct
        dyn_max_positions = market_inputs.dyn_max_positions
        krw_available = market_inputs.krw_available
        current_total_equity = market_inputs.current_total_equity
        entry_price = market_inputs.entry_price
        stop_loss = market_inputs.stop_loss
        current_price = market_inputs.current_price

        if buy_profile.use_slot_based_budget:
            effective_capital = (
                current_total_equity
                if current_total_equity > 0
                else (krw_available + max(0.0, ctx.risk_manager.realized_pnl_krw))
            )
            max_slot_budget = effective_capital / max(1, dyn_max_positions)
            slot_budget = min(
                krw_available,
                max_slot_budget * (alloc_pct / dyn_max_pos_pct if alloc_pct < dyn_max_pos_pct else 1.0),
            )
            if buy_profile.use_entry_price_guard:
                order_price = entry_price if (0 < entry_price <= current_price * 1.002) else current_price
            else:
                order_price = entry_price or current_price
            order_price = exchange.adjust_price_to_tick(order_price, side="bid")
            risk_scale = ctx.risk_manager.get_risk_scale_factor()
            risk_based_budget = calculate_risk_position_size(
                total_equity=effective_capital,
                entry_price=order_price,
                stop_loss=stop_loss,
                risk_fraction=0.01,
                fee_rate=0.0004,
                slippage_rate=0.001,
                max_position_pct=dyn_max_pos_pct,
                min_order_krw=min_order_krw,
                risk_scale_factor=risk_scale,
            )
            effective_risk_budget = risk_based_budget if risk_based_budget > 0 else slot_budget
            trade_budget = min(krw_available, slot_budget, effective_risk_budget)
        else:
            order_price = exchange.adjust_price_to_tick(entry_price or current_price, side="bid")
            alloc_pct = alloc_pct or dyn_max_pos_pct
            max_slot_budget = current_total_equity * alloc_pct
            risk_scale = ctx.risk_manager.get_risk_scale_factor()
            calculated_size = calculate_risk_position_size(
                total_equity=current_total_equity,
                entry_price=order_price,
                stop_loss=stop_loss,
                max_position_pct=alloc_pct,
                min_order_krw=min_order_krw,
                risk_scale_factor=risk_scale,
            )
            trade_budget = min(krw_available, max_slot_budget, calculated_size)

        if trade_budget < safe_order_krw and krw_available >= safe_order_krw:
            trade_budget = min(krw_available, max(max_slot_budget, safe_order_krw))

        if trade_budget < safe_order_krw or (trade_budget * 0.995) < min_order_krw:
            if buy_profile.use_slot_based_budget:
                logger.warning(
                    f"[{korean_name} / {market}] 매수 예산 부족: "
                    f"요청금액 {trade_budget:,.0f}원 < 안전최소주문금액 {safe_order_krw:,.0f}원"
                )
            else:
                logger.warning(
                    f"[{korean_name} / {market}] 매수 예산 부족: "
                    f"요청 {trade_budget:,.0f}원 < 최소 {safe_order_krw:,.0f}원"
                )
            audit_decision(market, "BLOCKED", "BUDGET", ["매수 예산 부족"], {"trade_budget": trade_budget})
            return True

        impact_ok, impact_reason, impact_details = evaluate_buy_orderbook_impact(
            orderbook=market_inputs.orderbook,
            order_krw=trade_budget,
            reference_price=current_price,
        )
        if not impact_ok:
            if self.config.orderbook_slippage_enforcement:
                logger.warning("[%s] %s", market, impact_reason)
                audit_decision(market, "BLOCKED", "ORDERBOOK_SLIPPAGE", [impact_reason], impact_details)
                return True
            logger.info("[%s] [관찰] %s", market, impact_reason)
            audit_decision(market, "OBSERVED", "ORDERBOOK_SLIPPAGE", [impact_reason], impact_details)

        is_safe, rejection_reason = ctx.risk_guard.validate_buy(
            market=market,
            order_krw=trade_budget,
            available_krw=krw_available,
            total_equity=current_total_equity,
            held_markets=market_inputs.held_markets,
        )
        if not is_safe:
            logger.warning(
                f"[{market}] {buy_profile.risk_guard_log_label}로 매수 차단: {rejection_reason}"
            )
            audit_decision(market, "BLOCKED", "RISK_GUARD", [rejection_reason], {"trade_budget": trade_budget})
            return True

        order_volume = (trade_budget * 0.995) / order_price
        formatted_volume = exchange.round_volume(market, order_volume)
        if formatted_volume <= 0:
            if buy_profile.use_slot_based_budget:
                logger.warning(f"[{market}] 계산된 수량이 너무 작아 주문 취소: {formatted_volume}")
            else:
                logger.warning(f"[{market}] 주문 수량 오류: {formatted_volume}")
            audit_decision(market, "BLOCKED", "ORDER_VALIDATION", ["주문 수량 오류"], {"volume": formatted_volume})
            return True

        candidate_type = market_inputs.candidate_type
        use_recovery_rebound = market_inputs.use_recovery_rebound
        if buy_profile.use_simple_buy_log:
            logger.info(
                f"🛒 [{buy_profile.buy_log_prefix}{korean_name} / {market} 신규 매수 실행] "
                f"주문가={order_price:,.2f}원, 수량={formatted_volume:.6f}, 투입금액={int(trade_budget):,d}원"
            )
        else:
            entry_label = (
                buy_profile.entry_label_recovery
                if use_recovery_rebound
                else (
                    buy_profile.entry_label_momentum
                    if candidate_type == "MOMENTUM_BREAKOUT"
                    else buy_profile.entry_label_default
                )
            )
            logger.info(
                f"🛒 [{korean_name} / {market} {entry_label} 매수 실행] "
                f"주문가={order_price:,.2f}원, 수량={formatted_volume:.6f}, 투입금액={int(trade_budget):,d}원"
            )

        ctx.cancel_bot_open_orders(exchange, market)

        selected_entry = market_inputs.selected_entry
        entry_snapshot = dict(selected_entry.get("strategy_snapshot", {}))
        entry_snapshot.update({
            "exchange": buy_profile.exchange_name,
            "market": market,
            "entry_decision_at": market_inputs.now_str,
            "entry_reason": market_inputs.reason,
            "target_price": market_inputs.target_price,
            "stop_loss": stop_loss,
        })
        position_id = f"{buy_profile.exchange_name}:{market}:{int(time.time() * 1000)}"
        ctx.order_executor.submit(
            exchange,
            market=market,
            side="bid",
            price=order_price,
            volume=formatted_volume,
            ord_type="limit",
            position_id=position_id,
            expected_price=order_price,
            entry_strategy_snapshot=entry_snapshot,
            exchange_name=buy_profile.exchange_name,
        )
        audit_decision(
            market,
            "BUY_SUBMITTED",
            "RECOVERY_REBOUND" if use_recovery_rebound else "STANDARD",
            [],
            {"order_price": order_price, "volume": formatted_volume, "trade_budget": trade_budget},
        )

        if buy_profile.render_buy_chart:
            ctx.chart_renderer.render_trade_chart(
                market=market,
                korean_name=korean_name,
                candles=market_inputs.candles_5m,
                entry_price=order_price,
                target_price=market_inputs.target_price,
                stop_loss=stop_loss,
                action="BUY",
                reason=market_inputs.reason,
            )
        return False

    def _should_skip_market(self, market: str, excluded_markets: frozenset[str]) -> bool:
        """업비트 등 루프 선행 제외 종목 스킵 여부."""
        if not self.profile.skip_excluded_markets_in_loop:
            return False
        return market in excluded_markets or market.replace("KRW-", "") in excluded_markets

    def run_market_loop(self, prefix: CyclePrefixResult) -> None:
        """마켓별 스냅샷 로드 -> 청산 -> 진입 -> 손절 -> 매수 실행."""
        ctx = self.context
        profile = self.profile
        logger = ctx.logger
        interval_minutes = self.config.interval_minutes

        exchange = prefix.exchange
        analyzer = prefix.analyzer
        target_markets = prefix.target_markets
        screened_candidate_metadata = prefix.screened_candidate_metadata
        excluded_markets = prefix.excluded_markets
        audit_decision = prefix.audit_decision
        btc_regime = prefix.btc_regime
        btc_status_msg = prefix.btc_status_msg
        is_btc_crashing = prefix.is_btc_crashing
        is_kill_switch = prefix.is_kill_switch
        is_cooldown = prefix.is_cooldown
        is_extreme_fear = prefix.is_extreme_fear
        dyn_max_positions = prefix.dyn_max_positions
        dyn_max_pos_pct = prefix.dyn_max_pos_pct
        held_markets = prefix.held_markets
        current_total_equity = prefix.current_total_equity
        now_str = prefix.now_str
        is_bot_paused = self.config.is_bot_paused()
        is_entry_ready = ctx.order_journal.is_entry_ready()

        for market in target_markets:
            if self._should_skip_market(market, excluded_markets):
                logger.warning(f"🛑 [보호 규칙 작동] 관리 제외 종목 ({market}) 분석 건너뜀")
                continue

            try:
                candidate_metadata = screened_candidate_metadata.get(market, {})
                candidate_type = str(candidate_metadata.get("candidate_type", "CONFIRMED")).upper()
                market_snapshot = ctx.orchestrator.load_market_snapshot(exchange, market, interval_minutes)
                korean_name = market_snapshot.korean_name
                logger.info(
                    f"--- [{korean_name} / {market} {profile.market_analysis_log_label}] ---"
                )
                krw_available = market_snapshot.krw_available
                coin_available = market_snapshot.coin_available
                avg_buy_price = market_snapshot.avg_buy_price
                current_price = market_snapshot.current_price
                coin_value = coin_available * current_price
                candles_5m = market_snapshot.candles_5m
                candles_1h = market_snapshot.candles_1h
                orderbook = market_snapshot.orderbook

                if self.process_priority_exits(MarketExitInputs(
                    exchange=exchange,
                    market=market,
                    korean_name=korean_name,
                    coin_available=coin_available,
                    avg_buy_price=avg_buy_price,
                    current_price=current_price,
                    coin_value=coin_value,
                    candles_5m=candles_5m,
                    btc_regime=btc_regime,
                    now_str=now_str,
                    candles_1h=candles_1h,
                    orderbook=orderbook,
                    analyzer=analyzer,
                )):
                    continue

                entry = self.process_entry_gating(MarketEntryInputs(
                    exchange=exchange,
                    market=market,
                    korean_name=korean_name,
                    candidate_type=candidate_type,
                    candidate_metadata=candidate_metadata,
                    analyzer=analyzer,
                    coin_available=coin_available,
                    avg_buy_price=avg_buy_price,
                    current_price=current_price,
                    coin_value=coin_value,
                    krw_available=krw_available,
                    candles_5m=candles_5m,
                    candles_1h=candles_1h,
                    orderbook=orderbook,
                    btc_regime=btc_regime,
                    btc_status_msg=btc_status_msg,
                    is_btc_crashing=is_btc_crashing,
                    is_cooldown=is_cooldown,
                    is_extreme_fear=is_extreme_fear,
                    is_bot_paused=is_bot_paused,
                    is_kill_switch=is_kill_switch,
                    is_entry_ready=is_entry_ready,
                    dyn_max_pos_pct=dyn_max_pos_pct,
                    now_str=now_str,
                    audit_decision=audit_decision,
                ))
                if entry.should_continue:
                    continue

                if self.process_cycle_stop_loss(MarketStopLossInputs(
                    exchange=exchange,
                    market=market,
                    korean_name=korean_name,
                    coin_available=coin_available,
                    avg_buy_price=avg_buy_price,
                    current_price=current_price,
                    coin_value=coin_value,
                    stop_loss=entry.stop_loss,
                    target_price=entry.target_price,
                    reason=entry.reason,
                    candles_5m=candles_5m,
                )):
                    continue

                if entry.action == "BUY" and self.process_buy_execution(MarketBuyInputs(
                    exchange=exchange,
                    market=market,
                    korean_name=korean_name,
                    candidate_type=candidate_type,
                    entry_price=entry.entry_price,
                    target_price=entry.target_price,
                    stop_loss=entry.stop_loss,
                    alloc_pct=entry.alloc_pct,
                    reason=entry.reason,
                    use_recovery_rebound=entry.use_recovery_rebound,
                    selected_entry=entry.selected_entry,
                    coin_available=coin_available,
                    coin_value=coin_value,
                    krw_available=krw_available,
                    current_price=current_price,
                    orderbook=orderbook,
                    candles_5m=candles_5m,
                    is_bot_paused=is_bot_paused,
                    is_kill_switch=is_kill_switch,
                    is_entry_ready=is_entry_ready,
                    is_btc_crashing=is_btc_crashing,
                    btc_status_msg=btc_status_msg,
                    current_total_equity=current_total_equity,
                    held_markets=held_markets,
                    dyn_max_positions=dyn_max_positions,
                    dyn_max_pos_pct=dyn_max_pos_pct,
                    now_str=now_str,
                    audit_decision=audit_decision,
                )):
                    continue
            except Exception as exc:
                logger.error(f"[{market}] 매매 사이클 오류 발생: {exc}", exc_info=True)

    def run_cycle_suffix(self, prefix: CyclePrefixResult) -> None:
        """이번 사이클 대상 외 전략 캐시 정리 및 디스크 저장."""
        current_valid_markets = set(prefix.target_markets).union(prefix.held_markets)
        latest_strategies = self.context.latest_strategies
        for old_market in list(latest_strategies.keys()):
            if old_market not in current_valid_markets:
                latest_strategies.pop(old_market, None)
        self.context.strategy_cache_manager.save_cache(latest_strategies)

    def run_cycle(self) -> None:
        """5분 사이클 전체(prefix -> market loop -> suffix) 실행."""
        logger = self.context.logger
        error_prefix = self.profile.cycle_error_log_prefix
        try:
            prefix = self.run_cycle_prefix()
            self.run_market_loop(prefix)
            self.run_cycle_suffix(prefix)
        except Exception as exc:
            logger.error(
                f"{error_prefix}전체 트레이딩 사이클 예외 발생: {exc}",
                exc_info=True,
            )
