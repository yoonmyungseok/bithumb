"""신규 BUY 주문 제출 직전 fail-closed 안전 게이트 (빗썸·업비트 공통)."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any

from base_websocket import WebSocketHealthState
from order_safety.journal import OrderJournal
logger = logging.getLogger(__name__)

# 시세 스트림·구독·처리 지연 상태: 신규 BUY만 차단하고 기존 포지션 청산은 별도 경로에서 유지한다.
_WS_BLOCKING_STATUSES = frozenset({
    WebSocketHealthState.STALE,
    WebSocketHealthState.DISCONNECTED,
    WebSocketHealthState.PROCESSING_DELAY,
    WebSocketHealthState.SUBSCRIPTION_FAILED,
    WebSocketHealthState.DATA_UNAVAILABLE,
})

def evaluate_pre_buy_submit_gate(
    *,
    market: str,
    exchange_name: str,
    order_journal: OrderJournal,
    current_price: float,
    ws_client: Any | None = None,
    cooldown_manager: Any | None = None,
    check_cooldown: bool = True,
) -> tuple[bool, str, str, dict[str, Any]]:
    """
    주문 API 호출 직전 신규 BUY 허용 여부를 판정한다.

    Returns:
        (allowed, block_code, block_reason_ko, details)
    """
    details: dict[str, Any] = {
        "exchange": exchange_name,
        "market": market,
        "gate": "pre_submit_final",
    }

    if order_journal.has_entry_blocking_market(market):
        # 종목별 체결 대사 대기·UNKNOWN은 중복 제출과 손익 오판을 막기 위해 fail-closed 한다.
        reason = f"{market} 주문이 체결 대사 대기(RECONCILIATION_PENDING 등) 또는 UNKNOWN 상태입니다."
        details["blocking_status"] = "RECONCILIATION_PENDING"
        return False, "PRE_BUY_RECONCILIATION_PENDING", reason, details

    # 전역 REST 대사·저널 영속화가 READY가 아니거나 해당 종목이 차단 상태면 신규 BUY를 차단한다.
    if not order_journal.is_entry_ready(market):
        state = str(getattr(order_journal, "reconciliation_state", "PENDING"))
        suspend_reason = str(
            (getattr(order_journal, "reconciliation_metrics", {}) or {}).get("last_suspend_reason", "")
        )
        reason = (
            f"REST 주문 대사 미완료(저널 상태={state})"
            + (f": {suspend_reason}" if suspend_reason else "")
        )
        details["reconciliation_state"] = state
        return False, "PRE_BUY_GLOBAL_RECONCILIATION", reason, details

    if ws_client is not None and hasattr(ws_client, "get_health_status"):
        ws_health = ws_client.get_health_status(market=market)
        ws_status = str(ws_health.get("status", ""))
        details["ws_status"] = ws_status
        details["ws_latency_seconds"] = ws_health.get("latency_seconds")
        if ws_status in _WS_BLOCKING_STATUSES or not ws_health.get("is_healthy", True):
            reason = f"{market} WebSocket 시세 상태 비정상({ws_status})"
            code = f"PRE_BUY_WS_{ws_status}" if ws_status else "PRE_BUY_WS_UNHEALTHY"
            return False, code, reason, details

    if check_cooldown and cooldown_manager is not None:
        in_cd, remaining = cooldown_manager.is_in_cooldown(market)
        if in_cd:
            reason = f"{market} 쿨다운 대기 중(약 {remaining:.0f}초 남음)"
            details["cooldown_remaining_sec"] = remaining
            return False, "PRE_BUY_MARKET_COOLDOWN", reason, details
        allowed, cd_reason = cooldown_manager.check_reentry_allowed(market, current_price)
        if not allowed:
            details["reentry_reason"] = cd_reason
            return False, "PRE_BUY_MARKET_COOLDOWN", cd_reason, details

    return True, "OK", "OK", details


class AckReconcileScheduler:
    """ACK 직후 단건 REST 대사를 제한된 속도로 예약한다(중복 주문 없음)."""

    def __init__(self, min_interval_sec: float = 1.5, max_queue: int = 32) -> None:
        self._queue: deque[str] = deque(maxlen=max_queue)
        self._lock = threading.Lock()
        self._last_run_monotonic = 0.0
        self._min_interval_sec = min_interval_sec

    def schedule(self, client_order_id: str) -> None:
        if not client_order_id:
            return
        with self._lock:
            if client_order_id not in self._queue:
                self._queue.append(client_order_id)

    def reconcile_next(
        self,
        journal: OrderJournal,
        exchange: Any,
        fill_processor: Any,
    ) -> bool:
        """대기열에서 1건만 REST 대사한다. 주기 대사와 병행해도 멱등 처리된다."""
        now = time.monotonic()
        if now - self._last_run_monotonic < self._min_interval_sec:
            return False
        with self._lock:
            if not self._queue:
                return False
            client_id = self._queue.popleft()
        self._last_run_monotonic = now
        updated = journal.reconcile_client_order(
            client_id,
            get_order=exchange.get_order,
            get_order_by_client_id=getattr(exchange, "get_order_by_client_id", None),
            fill_processor=fill_processor,
        )
        if updated:
            journal.complete_reconciliation_if_safe()
        return updated
