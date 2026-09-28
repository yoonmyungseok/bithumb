"""봇 관리 포지션·수동 격리 종목 판별 — 5분 사이클·실시간 청산 공통 SSOT."""

from __future__ import annotations

import threading
from typing import Any

from market_policy import is_protected_market


def is_bot_managed_position(
    order_journal: Any,
    trailing_tracker: Any,
    market: str,
    *,
    default_if_journal_missing: bool = False,
) -> bool:
    """주문 저널·트레일링 추적기로 봇이 매수한 관리 포지션인지 판별한다."""
    if is_protected_market(market):
        return False

    # 1. 트레일링 추적기에 진입 시각 또는 전략 모드가 활성화되어 있다면 봇 관리 포지션
    if trailing_tracker:
        try:
            raw_entry_t = getattr(trailing_tracker, "get_entry_time", lambda m: 0.0)(market)
            if isinstance(raw_entry_t, (int, float)) and not isinstance(raw_entry_t, bool) and raw_entry_t > 0:
                return True
            strategy_mode = getattr(trailing_tracker, "get_strategy_mode", lambda m: "")(market)
            if isinstance(strategy_mode, str) and strategy_mode in ("SWING", "SCALP", "NEW_LISTING", "MOMENTUM_BREAKOUT"):
                # entry_time이 누락되었더라도 전략 모드가 명시된 경우 관리 포지션으로 인정
                return True
        except Exception:
            pass

    if not order_journal or not hasattr(order_journal, "orders"):
        return default_if_journal_missing

    lock = getattr(order_journal, "_lock", threading.Lock())
    has_prior_bot_buy = False
    with lock:
        for order in reversed(order_journal.orders):
            if order.get("market") != market:
                continue
            side = str(order.get("side", "")).lower()
            status = str(order.get("status", "")).upper()
            if side in ("bid", "buy"):
                executed = float(
                    order.get("executed_volume")
                    or order.get("filled_volume")
                    or order.get("processed_executed_volume")
                    or 0.0
                )
                if status in ("FILLED", "PARTIALLY_FILLED", "OPEN", "ACKNOWLEDGED", "DONE") or executed > 0:
                    return True
                if executed <= 0 and status in ("CANCELED", "CANCELLED", "REJECTED", "EXPIRED"):
                    continue
                return False
            if side in ("ask", "sell"):
                exit_reason = str(order.get("exit_reason", "")).upper()
                if "PARTIAL" in exit_reason:
                    return True
                if status in ("FILLED", "DONE"):
                    has_prior_bot_buy = True

        # 최근 주문이 ask FILLED였더라도, 저널 상에 과거 봇 매수 체결 이력이 존재한다면
        # 분할 청산 잔여분이거나 재동기화된 포지션이므로 봇 관리 포지션으로 인정
        if has_prior_bot_buy:
            for order in order_journal.orders:
                if order.get("market") != market:
                    continue
                side = str(order.get("side", "")).lower()
                status = str(order.get("status", "")).upper()
                executed = float(
                    order.get("executed_volume")
                    or order.get("filled_volume")
                    or order.get("processed_executed_volume")
                    or 0.0
                )
                if side in ("bid", "buy") and (status in ("FILLED", "PARTIALLY_FILLED", "DONE") or executed > 0):
                    return True

    return False


def is_exit_allowed(
    market: str,
    order_journal: Any,
    trailing_tracker: Any,
) -> bool:
    """자동 청산(손절·익절·트레일링) 허용 여부. 수동·격리 종목은 fail-closed."""
    if is_protected_market(market):
        return False
    return is_bot_managed_position(
        order_journal,
        trailing_tracker,
        market,
        default_if_journal_missing=False,
    )
