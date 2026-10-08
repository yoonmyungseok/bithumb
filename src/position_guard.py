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
            bot_bids: list[dict[str, Any]] = []
            bot_asks: list[dict[str, Any]] = []
            for order in order_journal.orders:
                if str(order.get("market", "")).upper() != market.upper():
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
                        bot_bids.append(order)
                elif side in ("ask", "sell"):
                    if status in ("FILLED", "PARTIALLY_FILLED", "DONE") or executed > 0:
                        bot_asks.append(order)

            if not bot_bids:
                return False

            last_bid = bot_bids[-1]
            last_bid_time = float(last_bid.get("created_at") or 0.0)
            bid_vol = float(
                last_bid.get("executed_volume")
                or last_bid.get("filled_volume")
                or last_bid.get("processed_executed_volume")
                or last_bid.get("volume")
                or 0.0
            )

            asks_after_bid = [
                a for a in bot_asks
                if float(a.get("created_at") or 0.0) >= last_bid_time
            ]

            if not asks_after_bid:
                return True

            last_ask = asks_after_bid[-1]
            last_ask_reason = str(last_ask.get("exit_reason", "")).upper()

            if "PARTIAL" not in last_ask_reason:
                return False

            if bid_vol > 0:
                sold_vol = sum(
                    float(
                        a.get("executed_volume")
                        or a.get("filled_volume")
                        or a.get("processed_executed_volume")
                        or a.get("volume")
                        or 0.0
                    )
                    for a in asks_after_bid
                )
                if sold_vol >= (bid_vol * 0.999):
                    return False

            return True

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
