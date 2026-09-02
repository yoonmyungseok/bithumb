"""Journal-backed order submission with fail-closed guards."""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

from order_safety.journal import OrderJournal
from order_safety.markets import get_excluded_markets_set
from order_safety.types import AmbiguousOrderError, OrderStatus

logger = logging.getLogger(__name__)

class SafeOrderExecutor:
    def __init__(self, journal: OrderJournal):
        self.journal = journal

    def submit(
        self,
        exchange: Any,
        market: str,
        side: str,
        volume: float | None = None,
        price: float | None = None,
        ord_type: str = "limit",
        position_id: str | None = None,
        exit_reason: str | None = None,
        avg_buy_price: float | None = None,
        expected_price: float | None = None,
        entry_strategy_snapshot: dict[str, Any] | None = None,
        exchange_name: str = "",
    ) -> dict[str, Any]:
        # HOLO 및 수동 격리 종목 원천 차단 (P3-1)
        m_upper = market.upper()
        excluded_set = get_excluded_markets_set()
        if m_upper in excluded_set or m_upper.replace("KRW-", "") in excluded_set:
            logger.critical(f"🛑 [SafeOrderExecutor] 수동 관리 격리 종목 ({market}) 주문 시도 원천 차단")
            raise ValueError(f"수동 관리 격리 종목 ({market})은 주문할 수 없습니다.")

        # UNKNOWN 미해결 주문 존재 시 동일 종목 중복 주문 차단 (P0-1)
        if self.journal.has_unknown_market(market):
            logger.error(f"🛑 [{market}] 미해결 UNKNOWN 주문이 존재하여 신규 주문 제출 차단")
            raise RuntimeError(f"{market}에 확인되지 않은 이전 주문(UNKNOWN)이 존재합니다. REST 재조정 전까지 주문이 차단됩니다.")

        if side.lower() in ("ask", "sell") and self.journal.has_active_exit_order(market):
            # 대사 대기 청산을 중복 제출하면 기존 보유분을 초과 매도할 수 있어 반드시 차단한다.
            raise RuntimeError(f"{market}에 체결 대기 중인 청산 주문이 있어 중복 매도를 차단합니다.")

        if side.lower() in ("bid", "buy") and expected_price is not None:
            # 신규 매수 직전에는 캐시 가격을 신뢰하지 않고 거래소 최신가를 다시 확인한다.
            # 최신가를 확인할 수 없으면 기존 포지션은 건드리지 않되 신규 매수만 fail-closed 한다.
            try:
                latest_price = float(exchange.get_current_price(market) or 0.0)
            except Exception as exc:
                raise RuntimeError(f"{market} 주문 직전 최신가 조회 실패로 신규 매수를 차단합니다.") from exc
            if latest_price <= 0:
                raise RuntimeError(f"{market} 주문 직전 최신가가 유효하지 않아 신규 매수를 차단합니다.")
            expected_price = latest_price

        client_order_id = self.journal.record_intent(
            market,
            side,
            volume,
            price,
            ord_type,
            position_id=position_id,
            exit_reason=exit_reason,
            avg_buy_price=avg_buy_price,
            expected_price=expected_price,
            entry_strategy_snapshot=entry_strategy_snapshot,
            exchange=exchange_name,
        )
        try:
            response = exchange.create_order(
                market, side, volume, price, ord_type, client_order_id=client_order_id
            )
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            self.journal.mark(client_order_id, OrderStatus.UNKNOWN, error=str(exc))
            raise AmbiguousOrderError(
                f"{market} 주문 응답이 유실되었습니다. 자동 재주문을 차단했으며 order_journal.json에서 확인하세요."
            ) from exc
        except Exception as exc:
            self.journal.mark(client_order_id, OrderStatus.FAILED, error=str(exc))
            raise

        exchange_uuid = response.get("uuid") if isinstance(response, dict) else None
        exchange_order_id = response.get("order_id") if isinstance(response, dict) else None
        # ACK는 주문 수락일 뿐 체결 증빙이 아니다. 아래 상태는 접수 단계만 뜻하며,
        # Private WS 알림과 주기적 REST 대사만 체결량·평균가·수수료를 확정할 수 있다.
        self.journal.mark(
            client_order_id,
            OrderStatus.ACKNOWLEDGED,
            exchange_uuid=exchange_uuid,
            exchange_order_id=exchange_order_id,
        )
        logger.info("주문 접수 확인 (ACKNOWLEDGED): client_order_id=%s exchange_id=%s", client_order_id, exchange_uuid or exchange_order_id)
        if isinstance(response, dict):
            response["client_order_id"] = client_order_id
            if "status" not in response:
                response["status"] = "ACKNOWLEDGED"
        return response

    def execute_twap(self, bithumb: Any, market: str, side: str, volume: float, price: float, splits: int = 3, interval_seconds: float = 2.0) -> list[dict[str, Any]]:
        """Submit each slice through the journal rather than bypassing order safety."""
        results = []
        for index in range(max(1, splits)):
            if index:
                time.sleep(interval_seconds)
            results.append(self.submit(
                bithumb, market, side, volume=volume / max(1, splits), price=price, ord_type="limit"
            ))
        return results
