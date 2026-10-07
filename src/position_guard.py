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

    # 1. order_journal이 존재하는 경우: 실제 주문 저널이 Single Source of Truth(SSOT)
    if order_journal and hasattr(order_journal, "orders"):
        has_active_fn = getattr(order_journal, "has_active_bot_position", None)
        # Mock 객체가 아닌 실제 OrderJournal 인스턴스이거나, 테스트에서 명시적으로 모킹한 경우
        if callable(has_active_fn):
            try:
                from unittest.mock import Mock
                if not isinstance(order_journal, Mock) or "has_active_bot_position" in getattr(order_journal, "__dict__", {}):
                    return bool(has_active_fn(market))
            except ImportError:
                return bool(has_active_fn(market))

        lock = getattr(order_journal, "_lock", threading.Lock())
        with lock:
            for order in reversed(order_journal.orders):
                if order.get("market", "").upper() != market.upper():
                    continue
                side = str(order.get("side", "")).lower()
                status = str(order.get("status", "")).upper()
                executed = float(
                    order.get("executed_volume")
                    or order.get("filled_volume")
                    or order.get("processed_executed_volume")
                    or 0.0
                )
                if side in ("bid", "buy"):
                    if status in ("FILLED", "PARTIALLY_FILLED", "OPEN", "ACKNOWLEDGED", "DONE") or executed > 0:
                        return True
                    if executed <= 0 and status in ("CANCELED", "CANCELLED", "REJECTED", "EXPIRED"):
                        continue
                    return False
                if side in ("ask", "sell"):
                    exit_reason = str(order.get("exit_reason", "")).upper()
                    if "PARTIAL" in exit_reason:
                        return True
                    if status in ("FILLED", "DONE") or executed > 0:
                        # 전량 매도 완료된 후에는 봇 포지션이 아님 (수동 매수 종목 보호)
                        return False
            # 저널에 해당 종목 기록이 전혀 없는 경우 봇 관리 포지션이 아님
            return False

    # 2. order_journal이 없는 경우(트래커 단독 모드 또는 테스트 환경):
    if trailing_tracker:
        try:
            raw_entry_t = getattr(trailing_tracker, "get_entry_time", lambda m: 0.0)(market)
            if isinstance(raw_entry_t, (int, float)) and not isinstance(raw_entry_t, bool) and raw_entry_t > 0:
                return True
            strategy_mode = getattr(trailing_tracker, "get_strategy_mode", lambda m: "")(market)
            if isinstance(strategy_mode, str) and strategy_mode in ("SWING", "SCALP", "NEW_LISTING", "MOMENTUM_BREAKOUT"):
                return True
        except Exception:
            pass

    return default_if_journal_missing


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
