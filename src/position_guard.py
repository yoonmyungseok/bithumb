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
    if not order_journal or not hasattr(order_journal, "orders"):
        return default_if_journal_missing

    lock = getattr(order_journal, "_lock", threading.Lock())
    with lock:
        for order in reversed(order_journal.orders):
            if order.get("market") != market:
                continue
            side = str(order.get("side", "")).lower()
            status = str(order.get("status", "")).upper()
            if side in ("bid", "buy"):
                if status in ("FILLED", "PARTIALLY_FILLED", "OPEN", "ACKNOWLEDGED"):
                    return True
                return False
            if side in ("ask", "sell"):
                exit_reason = str(order.get("exit_reason", "")).upper()
                if "PARTIAL" in exit_reason:
                    return True
                if status == "FILLED":
                    return False

    if trailing_tracker and hasattr(trailing_tracker, "get_entry_time"):
        try:
            entry_t = float(trailing_tracker.get_entry_time(market) or 0.0)
            if entry_t > 0:
                return True
        except Exception:
            pass
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
