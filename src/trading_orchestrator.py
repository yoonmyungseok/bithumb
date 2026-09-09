"""Shared, behavior-preserving operations for dual-exchange trading cycles.

This is intentionally small at first: only operations with identical safety
semantics are moved here.  Exchange-specific execution remains in each entry
point until its contract is covered by a cross-exchange test.
"""

from __future__ import annotations

import datetime
import logging
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable

from exchange_adapter import ExchangeAdapter
from strategy_engine import classify_btc_regime, classify_listing_maturity


class TradingOrchestrator:
    """Common orchestration operations used by both five-minute cycles."""

    def __init__(self, logger: logging.Logger):
        self.logger = logger
        # 한 사이클의 반복 잔고 조회를 줄이되, 주문 직전에는 force_refresh로 우회한다.
        self._balance_snapshot: dict[str, Any] = {}
        self._balance_snapshot_at = 0.0
        self._balance_snapshot_exchange_id: int | None = None
        # 최근 실행 구간만 보존해 장기 실행 중 메모리 증가 없이 p50/p95를 관찰한다.
        self._latencies: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=40))
        # 스크리너가 이미 조회한 ticker를 같은 사이클 prefetch가 재사용한다.
        self._last_screener_ticker_seed: dict[str, dict[str, Any]] = {}

    def record_latency(self, name: str, elapsed_seconds: float) -> None:
        """성능 측정값을 누적하고 20회마다 운영 로그로 요약한다."""
        samples = self._latencies[name]
        samples.append(max(0.0, float(elapsed_seconds)))
        if len(samples) < 20 or len(samples) % 20 != 0:
            return
        ordered = sorted(samples)
        p50 = ordered[len(ordered) // 2]
        p95 = ordered[min(len(ordered) - 1, int((len(ordered) - 1) * 0.95))]
        self.logger.info(
            "[성능 계측] %s 최근 %d회: p50=%.3fs p95=%.3fs max=%.3fs",
            name, len(ordered), p50, p95, ordered[-1],
        )
        if name == "full_cycle" and p95 > 15.0:
            self.logger.warning(
                "[성능 경고] full_cycle p95=%.3fs > 15s 목표 초과 (coalesce 위험 점검 필요)",
                p95,
            )

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
        self._last_screener_ticker_seed = {}
        if is_auto_mode:
            screener = create_screener()
            try:
                screened = screener.scan_markets(top_count=top_count, held_markets=held, btc_regime=btc_regime, analyzer=analyzer)
            except TypeError:
                screened = screener.scan_markets(top_count=top_count, held_markets=held, btc_regime=btc_regime)

            ticker_seed = getattr(screener, "last_scan_tickers", None) or []
            for ticker in ticker_seed:
                if isinstance(ticker, dict) and isinstance(ticker.get("market"), str):
                    self._last_screener_ticker_seed[ticker["market"]] = ticker

            # [Dual-Track] 스윙 전용 유망 후보군 병합 스캔 (최대 1종목)
            if hasattr(screener, "scan_swing_markets"):
                try:
                    swing_kwargs: dict[str, Any] = {
                        "top_count": 1,
                        "held_markets": held,
                        "btc_regime": btc_regime,
                    }
                    if ticker_seed:
                        swing_kwargs["ticker_seed"] = ticker_seed
                    swing_candidates = screener.scan_swing_markets(**swing_kwargs)
                    if swing_candidates:
                        screened.extend(swing_candidates)
                except TypeError:
                    try:
                        swing_candidates = screener.scan_swing_markets(top_count=1, held_markets=held, btc_regime=btc_regime)
                        if swing_candidates:
                            screened.extend(swing_candidates)
                    except Exception as exc:
                        self.logger.debug("스윙 후보 스크리닝 폴백: %s", exc)
                except Exception as exc:
                    self.logger.debug("스윙 후보 스크리닝 폴백: %s", exc)

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

    def prefetch_market_inputs(
        self,
        exchange: ExchangeAdapter,
        markets: list[str],
        *,
        ticker_seed: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """동일 사이클의 전략 입력만 일괄 조회하고, 주문 직전 검증에는 사용하지 않는다."""
        unique_markets = list(dict.fromkeys(market for market in markets if market))
        if not unique_markets:
            return {}

        started_at = time.monotonic()
        observed_at = time.monotonic()
        result: dict[str, dict[str, Any]] = {}
        seed = ticker_seed if ticker_seed is not None else self._last_screener_ticker_seed
        markets_needing_ticker: list[str] = []
        for market in unique_markets:
            seeded = (seed or {}).get(market)
            if isinstance(seeded, dict):
                try:
                    price = float(seeded.get("trade_price", 0.0) or 0.0)
                except (TypeError, ValueError):
                    markets_needing_ticker.append(market)
                    continue
                if price > 0:
                    result.setdefault(market, {})["price"] = price
                else:
                    markets_needing_ticker.append(market)
            else:
                markets_needing_ticker.append(market)

        try:
            tickers = exchange.get_tickers(markets_needing_ticker) if markets_needing_ticker else []
            orderbooks = exchange.get_orderbooks(unique_markets)
        except Exception as exc:
            # 사전 조회 실패는 캐시를 만들지 않아 이후 단건 조회와 fail-closed 경계를 보존한다.
            self.logger.debug("전략 입력 일괄 사전 조회 실패: %s", exc)
            return {}
        finally:
            self.record_latency("strategy_input_prefetch", time.monotonic() - started_at)

        for ticker in tickers or []:
            if isinstance(ticker, dict) and isinstance(ticker.get("market"), str):
                try:
                    price = float(ticker.get("trade_price", 0.0) or 0.0)
                except (TypeError, ValueError):
                    # 손상된 가격은 사전 조회에서 제외해 단건 조회/fail-closed 경계로 넘긴다.
                    continue
                if price > 0:
                    result.setdefault(ticker["market"], {})["price"] = price
        for orderbook in orderbooks or []:
            if isinstance(orderbook, dict) and isinstance(orderbook.get("market"), str):
                result.setdefault(orderbook["market"], {})["orderbook"] = orderbook
        for payload in result.values():
            payload["observed_at"] = observed_at
        return result

    def prefetch_cycle_candles(
        self,
        exchange: ExchangeAdapter,
        markets: list[str],
        interval_minutes: int,
        *,
        max_workers: int = 5,
    ) -> dict[str, dict[str, Any]]:
        """사이클 내 스냅샷·우선순위 평가가 공유하는 캔들 사전 조회. 예외 시 단건 폴백을 허용한다."""
        unique_markets = list(dict.fromkeys(market for market in markets if market))
        if not unique_markets:
            return {}

        started_at = time.monotonic()
        cache: dict[str, dict[str, Any]] = {}
        worker_count = max(1, min(max_workers, len(unique_markets)))

        def _fetch_market_candles(market: str) -> tuple[str, dict[str, Any] | None]:
            try:
                candles_5m = exchange.get_candles(unit=interval_minutes, count=30, market=market)
                candles_1h = exchange.get_candles(unit=60, count=50, market=market)
                four_hour_history = self._load_swing_candles_safely(exchange, market)
                return market, {
                    "candles_5m": candles_5m,
                    "candles_1h": candles_1h,
                    "candles_4h": four_hour_history.candles,
                    "four_hour_history_status": four_hour_history.status,
                }
            except Exception as exc:
                self.logger.debug("캔들 사전 조회 실패(%s): %s", market, exc)
                return market, None

        try:
            with ThreadPoolExecutor(max_workers=worker_count) as pool:
                futures = {pool.submit(_fetch_market_candles, market): market for market in unique_markets}
                for future in as_completed(futures):
                    market, payload = future.result()
                    if payload:
                        cache[market] = payload
        except Exception as exc:
            self.logger.debug("캔들 사전 조회 병렬 실행 실패: %s", exc)
        finally:
            self.record_latency("candle_prefetch", time.monotonic() - started_at)
        return cache

    @staticmethod
    def _resolve_prefetch_window(prefetched_input: dict[str, Any] | None) -> tuple[bool, float, dict[str, Any] | None]:
        """전략 입력 prefetch TTL(1.0s) 안에서만 ticker·호가를 재사용한다."""
        is_fresh_prefetch = bool(
            prefetched_input
            and time.monotonic() - float(prefetched_input.get("observed_at", 0.0) or 0.0) <= 1.0
        )
        prefetched_price = float((prefetched_input or {}).get("price", 0.0) or 0.0)
        prefetched_orderbook = (prefetched_input or {}).get("orderbook")
        return is_fresh_prefetch, prefetched_price, prefetched_orderbook if isinstance(prefetched_orderbook, dict) else None

    @staticmethod
    def _resolve_cached_candles(
        market: str,
        interval_minutes: int,
        exchange: ExchangeAdapter,
        candle_cache: dict[str, dict[str, Any]] | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], FourHourHistoryResult]:
        """사전 조회 캐시가 없으면 단건 REST로 fail-closed 폴백한다."""
        cached = (candle_cache or {}).get(market, {})
        candles_5m = cached.get("candles_5m")
        candles_1h = cached.get("candles_1h")
        if "candles_4h" in cached:
            four_hour_history = FourHourHistoryResult(
                cached.get("candles_4h") or [],
                str(cached.get("four_hour_history_status", "AVAILABLE")),
            )
        else:
            four_hour_history = None

        if not isinstance(candles_5m, list):
            candles_5m = exchange.get_candles(unit=interval_minutes, count=30, market=market)
        if not isinstance(candles_1h, list):
            candles_1h = exchange.get_candles(unit=60, count=50, market=market)
        if four_hour_history is None:
            four_hour_history = TradingOrchestrator._load_swing_candles_safely(exchange, market)
        return candles_5m, candles_1h, four_hour_history

    def load_priority_eval_snapshot(
        self,
        exchange: ExchangeAdapter,
        market: str,
        interval_minutes: int,
        prefetched_input: dict[str, Any] | None = None,
        *,
        candle_cache: dict[str, dict[str, Any]] | None = None,
    ) -> "MarketSnapshot":
        """AI 우선순위 정렬에 필요한 최소 필드만 조회한다. 주문·청산 경로에는 사용하지 않는다."""
        started_at = time.monotonic()
        currency = market.split("-")[-1] if "-" in market else market
        is_fresh_prefetch, prefetched_price, prefetched_orderbook = self._resolve_prefetch_window(prefetched_input)
        candles_5m, candles_1h, four_hour_history = self._resolve_cached_candles(
            market, interval_minutes, exchange, candle_cache,
        )
        snapshot = MarketSnapshot(
            market=market,
            currency=currency,
            korean_name="",
            balances={},
            krw_available=0.0,
            coin_available=0.0,
            avg_buy_price=0.0,
            current_price=(
                prefetched_price
                if is_fresh_prefetch and prefetched_price > 0
                else exchange.get_current_price(market)
            ),
            candles_5m=candles_5m,
            candles_1h=candles_1h,
            candles_4h=four_hour_history.candles,
            four_hour_history_status=four_hour_history.status,
            orderbook=(
                prefetched_orderbook
                if is_fresh_prefetch and prefetched_orderbook is not None
                else exchange.get_orderbook(market)
            ),
            listing_maturity=classify_listing_maturity(
                four_hour_history.candles, candles_1h, candles_5m, four_hour_history.status,
            ),
            is_priority_eval_only=True,
        )
        self.record_latency("priority_eval_snapshot", time.monotonic() - started_at)
        return snapshot

    def load_market_snapshot(
        self,
        exchange: ExchangeAdapter,
        market: str,
        interval_minutes: int,
        prefetched_input: dict[str, Any] | None = None,
        *,
        candle_cache: dict[str, dict[str, Any]] | None = None,
        priority_snapshot: "MarketSnapshot | None" = None,
    ) -> "MarketSnapshot":
        """Load the common per-market inputs used by strategy and exit logic."""
        started_at = time.monotonic()
        currency = market.split("-")[-1] if "-" in market else market
        # 시장별 분석은 짧은 TTL 잔고 스냅샷을 공유해 종목 수만큼 REST를 반복하지 않는다.
        balances = self.get_balance_snapshot(exchange)
        coin = balances.get(currency, {"balance": 0.0, "locked": 0.0, "avg_buy_price": 0.0})
        is_fresh_prefetch, prefetched_price, prefetched_orderbook = self._resolve_prefetch_window(prefetched_input)
        if (
            priority_snapshot is not None
            and priority_snapshot.market == market
            and priority_snapshot.is_priority_eval_only
        ):
            candles_5m = priority_snapshot.candles_5m
            candles_1h = priority_snapshot.candles_1h
            four_hour_history = FourHourHistoryResult(
                priority_snapshot.candles_4h,
                priority_snapshot.four_hour_history_status,
            )
            listing_maturity = priority_snapshot.listing_maturity
        else:
            candles_5m, candles_1h, four_hour_history = self._resolve_cached_candles(
                market, interval_minutes, exchange, candle_cache,
            )
            listing_maturity = classify_listing_maturity(
                four_hour_history.candles, candles_1h, candles_5m, four_hour_history.status,
            )
        snapshot = MarketSnapshot(
            market=market,
            currency=currency,
            korean_name=exchange.get_korean_name(market),
            balances=balances,
            krw_available=float(balances.get("KRW", {}).get("balance", 0.0)),
            coin_available=float(coin.get("balance", 0.0)),
            avg_buy_price=float(coin.get("avg_buy_price", 0.0)),
            current_price=prefetched_price if is_fresh_prefetch and prefetched_price > 0 else exchange.get_current_price(market),
            candles_5m=candles_5m,
            candles_1h=candles_1h,
            candles_4h=four_hour_history.candles,
            four_hour_history_status=four_hour_history.status,
            orderbook=prefetched_orderbook if is_fresh_prefetch and prefetched_orderbook is not None else exchange.get_orderbook(market),
            listing_maturity=listing_maturity,
            is_priority_eval_only=False,
        )
        self.record_latency("market_snapshot", time.monotonic() - started_at)
        return snapshot

    @staticmethod
    def _load_swing_candles_safely(exchange: ExchangeAdapter, market: str) -> "FourHourHistoryResult":
        """4시간봉 실제 희소 이력과 조회 장애를 분리해 신규 진입 오판을 막는다."""
        try:
            candles = exchange.get_candles(unit=240, count=25, market=market)
            if not isinstance(candles, list) or not candles:
                return FourHourHistoryResult([], "UNAVAILABLE")
            if not all(isinstance(candle, dict) for candle in candles):
                return FourHourHistoryResult([], "UNAVAILABLE")
            return FourHourHistoryResult(candles, "AVAILABLE")
        except Exception:
            return FourHourHistoryResult([], "UNAVAILABLE")

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

            # [3순위] AI 매크로 정밀 진단 결합 (백그라운드 비동기 갱신으로 사이클 지연 제거)
            if analyzer is not None and hasattr(analyzer, "diagnose_macro_regime") and candles_1h:
                try:
                    macro_diag = analyzer.diagnose_macro_regime(
                        btc_candles_1h=candles_1h,
                        fng_index=fng_index,
                        background=True,
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
                except TypeError:
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
class FourHourHistoryResult:
    """4시간봉 응답의 신뢰 상태를 보존한다. 빈 응답은 신규상장 증거가 아니다."""

    candles: list[dict[str, Any]]
    status: str


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
    candles_4h: list[dict[str, Any]]
    orderbook: dict[str, Any]
    four_hour_history_status: str = "UNAVAILABLE"
    listing_maturity: str = "MATURE"
    is_priority_eval_only: bool = False
