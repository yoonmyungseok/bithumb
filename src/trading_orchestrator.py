"""Shared, behavior-preserving operations for dual-exchange trading cycles.

This is intentionally small at first: only operations with identical safety
semantics are moved here.  Exchange-specific execution remains in each entry
point until its contract is covered by a cross-exchange test.
"""

from __future__ import annotations

import datetime
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from exchange_adapter import ExchangeAdapter
from strategy_engine import classify_btc_regime


class TradingOrchestrator:
    """Common orchestration operations used by both five-minute cycles."""

    def __init__(self, logger: logging.Logger):
        self.logger = logger
        # 한 사이클의 반복 잔고 조회를 줄이되, 주문 직전에는 force_refresh로 우회한다.
        self._balance_snapshot: dict[str, Any] = {}
        self._balance_snapshot_at = 0.0
        self._balance_snapshot_exchange_id: int | None = None

    def get_balance_snapshot(
        self, exchange: ExchangeAdapter, *, force_refresh: bool = False, ttl_seconds: float = 2.0,
    ) -> dict[str, Any]:
        """짧은 TTL의 잔고 스냅샷을 재사용하고, 주문 경계에서는 강제 최신 조회를 허용한다."""
        now_ts = time.monotonic()
        same_exchange = self._balance_snapshot_exchange_id == id(exchange)
        if (
            not force_refresh
            and same_exchange
            and self._balance_snapshot
            and now_ts - self._balance_snapshot_at < ttl_seconds
        ):
            return self._balance_snapshot
        balances = exchange.get_balances()
        # 실패/빈 응답은 캐시하지 않아 오래된 잔고가 신규 매수 승인에 쓰이지 않게 한다.
        if not isinstance(balances, dict):
            raise RuntimeError("거래소 잔고 스냅샷이 유효하지 않습니다.")
        self._balance_snapshot = balances
        self._balance_snapshot_at = now_ts
        self._balance_snapshot_exchange_id = id(exchange)
        return balances

    def reconcile_orders(
        self,
        exchange: ExchangeAdapter,
        order_journal: Any,
        fill_processor: Any,
        *,
        label: str = "",
    ) -> int:
        """Reconcile pending orders and only reopen entries after a safe result."""
        try:
            reconciled = order_journal.reconcile_exchange_statuses(
                get_order=exchange.get_order,
                get_order_by_client_id=getattr(exchange, "get_order_by_client_id", None),
                fill_processor=fill_processor,
            )
            if reconciled:
                self.logger.info("🔄 [%sREST 체결 재조정] 미완료 주문 %d건 체결 상태 최신화 완료", label, reconciled)
            order_journal.complete_reconciliation_if_safe()
            return reconciled
        except Exception as exc:
            self.logger.debug("%s주기적 REST 주문 상태 재조정 예외: %s", label, exc)
            return 0

    def refresh_portfolio(
        self,
        exchange: ExchangeAdapter,
        *,
        calculate_total_equity: Callable[[dict[str, Any], Any], float],
        get_held_markets: Callable[[dict[str, Any], Any], list[str]],
        trailing_tracker: Any,
        realtime_engine: Any,
        risk_manager: Any,
        risk_guard: Any,
        get_portfolio_tiers: Callable[[float], tuple[int, float, int]],
        now: datetime.datetime,
    ) -> "PortfolioSnapshot":
        """Refresh the shared account/risk snapshot without changing trade rules."""
        # 포트폴리오·킬스위치 계산은 매 사이클 첫 조회를 강제 최신 상태로 시작한다.
        balances = self.get_balance_snapshot(exchange, force_refresh=True)
        total_equity = calculate_total_equity(balances, exchange)
        held_markets = get_held_markets(balances, exchange)
        stale_states = trailing_tracker.reconcile_markets(held_markets)

        canceled_stale = realtime_engine.clean_stale_orders(max_age_seconds=180)
        requoted = realtime_engine.requote_pending_orders()
        if canceled_stale or requoted:
            # 취소/재호가 직후에는 잠긴 잔고가 달라질 수 있으므로 다시 강제 조회한다.
            balances = self.get_balance_snapshot(exchange, force_refresh=True)

        krw_available = float(balances.get("KRW", {}).get("balance", 0.0))
        is_kill_switch, daily_pnl = risk_manager.update_daily_equity(total_equity, now)
        is_cooldown, cooldown_minutes = risk_manager.is_cooling_down()
        max_positions, max_position_pct, top_count = get_portfolio_tiers(total_equity)
        risk_guard.update_limits(max_open_positions=max_positions, max_position_pct=max_position_pct)
        return PortfolioSnapshot(
            balances=balances,
            total_equity=total_equity,
            held_markets=held_markets,
            krw_available=krw_available,
            stale_states=stale_states,
            canceled_stale=canceled_stale,
            requoted=requoted,
            is_kill_switch=is_kill_switch,
            daily_pnl=daily_pnl,
            is_cooldown=is_cooldown,
            cooldown_minutes=cooldown_minutes,
            max_positions=max_positions,
            max_position_pct=max_position_pct,
            top_count=top_count,
        )

    def select_target_markets(
        self,
        exchange: ExchangeAdapter,
        *,
        held_markets: list[str],
        is_auto_mode: bool,
        raw_markets: str,
        max_positions: int,
        top_count: int,
        create_screener: Callable[[], Any],
        btc_regime: str = "NORMAL",
        analyzer: Any | None = None,
        on_screened_candidates: Callable[[list[dict[str, Any]]], None] | None = None,
    ) -> list[str]:
        """Select markets through one policy while retaining exchange exclusions."""
        held = [market for market in held_markets if exchange.is_tradeable_market(market)]
        if is_auto_mode:
            screener = create_screener()
            try:
                screened = screener.scan_markets(top_count=top_count, held_markets=held, btc_regime=btc_regime, analyzer=analyzer)
            except TypeError:
                screened = screener.scan_markets(top_count=top_count, held_markets=held, btc_regime=btc_regime)
            # 호출자가 후보 유형 등 선별 메타데이터를 주문 기록에 보존할 수 있게 전달한다.
            if on_screened_candidates is not None:
                on_screened_candidates(screened)
            candidates = [item.get("market", "") for item in screened if isinstance(item, dict)]
        else:
            # 수동 종목 목록에는 스크리너 메타데이터가 없으므로 이전 사이클 정보를 비운다.
            if on_screened_candidates is not None:
                on_screened_candidates([])
            candidates = [market.strip().upper() for market in raw_markets.split(",") if market.strip()]

        return list(dict.fromkeys(
            market for market in [*held, *candidates]
            if market and exchange.is_tradeable_market(market)
        ))

    def load_market_snapshot(self, exchange: ExchangeAdapter, market: str, interval_minutes: int) -> "MarketSnapshot":
        """Load the common per-market inputs used by strategy and exit logic."""
        currency = market.split("-")[-1] if "-" in market else market
        # 시장별 분석은 짧은 TTL 잔고 스냅샷을 공유해 종목 수만큼 REST를 반복하지 않는다.
        balances = self.get_balance_snapshot(exchange)
        coin = balances.get(currency, {"balance": 0.0, "locked": 0.0, "avg_buy_price": 0.0})
        return MarketSnapshot(
            market=market,
            currency=currency,
            korean_name=exchange.get_korean_name(market),
            balances=balances,
            krw_available=float(balances.get("KRW", {}).get("balance", 0.0)),
            coin_available=float(coin.get("balance", 0.0)),
            avg_buy_price=float(coin.get("avg_buy_price", 0.0)),
            current_price=exchange.get_current_price(market),
            candles_5m=exchange.get_candles(unit=interval_minutes, count=30, market=market),
            candles_1h=exchange.get_candles(unit=60, count=50, market=market),
            orderbook=exchange.get_orderbook(market),
        )

    def classify_market_regime(
        self,
        exchange: ExchangeAdapter,
        *,
        interval_minutes: int,
        crash_threshold_pct: float,
        analyzer: Any | None = None,
        fng_index: dict[str, Any] | None = None,
    ) -> tuple[bool, str, str]:
        """Use one fail-closed BTC regime decision for every exchange cycle."""
        try:
            candles_5m = exchange.get_candles(unit=interval_minutes, count=20, market="KRW-BTC")
            candles_1h = exchange.get_candles(unit=60, count=50, market="KRW-BTC")
            if len(candles_5m) < 5:
                return True, "CRASH", "BTC 데이터 부족 (Fail-Closed: 안전 관망)"
            result = classify_btc_regime(candles_5m, candles_1h, crash_threshold_pct=crash_threshold_pct)
            regime = str(result.get("regime", "CRASH"))
            reason = str(result.get("reason", "BTC 정상 안정세"))

            # 1차 로컬 판별에서 이미 급락이면 즉시 차단
            if regime == "CRASH":
                return True, "CRASH", reason

            # [3순위] AI 매크로 정밀 진단 결합 (30분 캐시)
            if analyzer is not None and hasattr(analyzer, "diagnose_macro_regime") and candles_1h:
                try:
                    macro_diag = analyzer.diagnose_macro_regime(
                        btc_candles_1h=candles_1h,
                        fng_index=fng_index,
                    )
                    ai_regime = str(macro_diag.get("regime", "")).upper()
                    if ai_regime == "CRASH":
                        return True, "CRASH", f"AI 거시 위기 경보: {macro_diag.get('summary')}"
                    elif ai_regime in ("BEAR_REGIME", "CAUTION_PULLBACK") and regime != "CRASH":
                        regime = "RISK_OFF"
                        reason = f"{reason} | AI: {macro_diag.get('summary')}"
                    elif ai_regime == "BULL_TREND" and regime == "NORMAL":
                        regime = "BULL_TREND"
                        reason = f"{reason} | AI: {macro_diag.get('summary')}"
                except Exception as exc:
                    self.logger.debug("AI 매크로 진단 폴백: %s", exc)

            return regime == "CRASH", regime, reason
        except Exception as exc:
            self.logger.warning("BTC market-state lookup failed; blocking entries: %s", exc)
            return True, "CRASH", f"BTC 조회 실패 (Fail-Closed: {exc})"


@dataclass(frozen=True)
class PortfolioSnapshot:
    balances: dict[str, Any]
    total_equity: float
    held_markets: list[str]
    krw_available: float
    stale_states: int
    canceled_stale: int
    requoted: int
    is_kill_switch: bool
    daily_pnl: float
    is_cooldown: bool
    cooldown_minutes: int
    max_positions: int
    max_position_pct: float
    top_count: int


@dataclass(frozen=True)
class MarketSnapshot:
    market: str
    currency: str
    korean_name: str
    balances: dict[str, Any]
    krw_available: float
    coin_available: float
    avg_buy_price: float
    current_price: float
    candles_5m: list[dict[str, Any]]
    candles_1h: list[dict[str, Any]]
    orderbook: dict[str, Any]
