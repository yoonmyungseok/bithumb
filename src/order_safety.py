"""Durable order intent tracking and pre-trade risk checks.

The exchange is the source of truth for fills.  This module deliberately treats a
network failure after an order submission as *unknown*, never as permission to
submit the same order again.
"""

import json
import logging
import os
import tempfile
import time
import uuid
from typing import Any

import requests

logger = logging.getLogger(__name__)


def write_json_atomically(path: str, payload: Any) -> None:
    """Persist JSON without leaving a partially-written state file after a crash."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temporary_path = tempfile.mkstemp(prefix="state_", suffix=".tmp", dir=directory, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, path)
    except OSError:
        try:
            os.unlink(temporary_path)
        except OSError:
            pass
        raise


class AmbiguousOrderError(RuntimeError):
    """The exchange may have accepted an order, but its response was not received."""


class OrderJournal:
    """Small append-only JSON journal, written atomically for crash recovery."""

    def __init__(self, path: str | None = None):
        data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        os.makedirs(data_dir, exist_ok=True)
        self.path = path or os.path.join(data_dir, "order_journal.json")
        self.orders: list[dict[str, Any]] = self._load()

    def _load(self) -> list[dict[str, Any]]:
        try:
            with open(self.path, "r", encoding="utf-8") as file:
                data = json.load(file)
                return data if isinstance(data, list) else []
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("주문 저널 로드 실패: %s", exc)
            return []

    def _save(self) -> None:
        try:
            write_json_atomically(self.path, self.orders[-500:])
        except OSError as exc:
            logger.error("주문 저널 저장 실패: %s", exc)
            raise

    def record_intent(self, market: str, side: str, volume: float | None, price: float | None, ord_type: str) -> str:
        client_order_id = f"bot-{int(time.time() * 1000)}-{uuid.uuid4().hex[:10]}"
        self.orders.append({
            "client_order_id": client_order_id,
            "market": market,
            "side": side,
            "volume": volume,
            "price": price,
            "ord_type": ord_type,
            "status": "PENDING_SUBMISSION",
            "created_at": time.time(),
            "exchange_uuid": None,
        })
        self._save()
        return client_order_id

    def mark(self, client_order_id: str, status: str, **fields: Any) -> None:
        for order in reversed(self.orders):
            if order["client_order_id"] == client_order_id:
                order["status"] = status
                order["updated_at"] = time.time()
                order.update(fields)
                self._save()
                return

    def has_unresolved_market(self, market: str) -> bool:
        return any(
            order.get("market") == market
            and order.get("status") in {"PENDING_SUBMISSION", "UNKNOWN", "ACKNOWLEDGED", "OPEN"}
            for order in self.orders
        )

    def reconcile_exchange_statuses(self, get_order: Any, get_order_by_client_id: Any | None = None) -> int:
        """Update acknowledged orders from the exchange's canonical order endpoint."""
        updated = 0
        state_map = {"wait": "OPEN", "watch": "OPEN", "done": "FILLED", "cancel": "CANCELED"}
        for local in self.orders:
            exchange_uuid = local.get("exchange_uuid")
            if local.get("status") not in {"PENDING_SUBMISSION", "UNKNOWN", "ACKNOWLEDGED", "OPEN"}:
                continue
            try:
                if exchange_uuid:
                    remote = get_order(exchange_uuid)
                elif get_order_by_client_id:
                    remote = get_order_by_client_id(local["client_order_id"])
                else:
                    continue
            except requests.exceptions.RequestException:
                continue
            state = state_map.get(str(remote.get("state", "")).lower())
            if state and state != local.get("status"):
                self.mark(
                    local["client_order_id"],
                    state,
                    exchange_state=remote.get("state"),
                    exchange_uuid=remote.get("uuid", exchange_uuid),
                    executed_volume=remote.get("executed_volume"),
                    remaining_volume=remote.get("remaining_volume"),
                    trades=remote.get("trades", []),
                )
                updated += 1
        return updated

    def reconcile_open_orders(self, open_orders: list[dict[str, Any]]) -> int:
        """Attach exchange UUIDs where an intent can be unambiguously matched.

        An unknown intent that cannot be matched remains blocked for manual review;
        this conservative behavior prevents accidental duplicate orders.
        """
        matched = 0
        for local in self.orders:
            if local.get("status") not in {"PENDING_SUBMISSION", "UNKNOWN"}:
                continue
            for remote in open_orders:
                if remote.get("market") != local.get("market") or remote.get("side") != local.get("side"):
                    continue
                if local.get("ord_type") != remote.get("ord_type"):
                    continue
                remote_uuid = remote.get("uuid")
                if remote_uuid:
                    self.mark(local["client_order_id"], "OPEN", exchange_uuid=remote_uuid)
                    matched += 1
                    break
        return matched

    def apply_private_order_event(self, event: dict[str, Any]) -> bool:
        """Apply a v2 MyOrder event keyed by the exchange client_order_id."""
        client_id = event.get("client_order_id") or event.get("coid")
        state = str(event.get("state") or event.get("s") or "").lower()
        mapped = {"wait": "OPEN", "trade": "PARTIALLY_FILLED", "done": "FILLED", "cancel": "CANCELED"}.get(state)
        if not client_id or not mapped:
            return False
        self.mark(str(client_id), mapped,
            exchange_order_id=event.get("order_id") or event.get("oid"),
            executed_volume=event.get("executed_volume") or event.get("ev"),
            remaining_volume=event.get("remaining_volume") or event.get("rv"),
            last_event_at=time.time(),
        )
        return True


class SafeOrderExecutor:
    def __init__(self, journal: OrderJournal):
        self.journal = journal

    def submit(self, bithumb: Any, market: str, side: str, volume: float | None = None, price: float | None = None, ord_type: str = "limit") -> dict[str, Any]:
        client_order_id = self.journal.record_intent(market, side, volume, price, ord_type)
        try:
            response = bithumb.create_order(
                market, side, volume, price, ord_type, client_order_id=client_order_id
            )
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            self.journal.mark(client_order_id, "UNKNOWN", error=str(exc))
            raise AmbiguousOrderError(
                f"{market} 주문 응답이 유실되었습니다. 자동 재주문을 차단했으며 order_journal.json에서 확인하세요."
            ) from exc
        except Exception as exc:
            self.journal.mark(client_order_id, "FAILED", error=str(exc))
            raise

        exchange_uuid = response.get("uuid") if isinstance(response, dict) else None
        exchange_order_id = response.get("order_id") if isinstance(response, dict) else None
        self.journal.mark(
            client_order_id,
            "ACKNOWLEDGED",
            exchange_uuid=exchange_uuid,
            exchange_order_id=exchange_order_id,
        )
        logger.info("주문 접수 확인: client_order_id=%s exchange_id=%s", client_order_id, exchange_uuid or exchange_order_id)
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


class RiskGuard:
    """Single decision point for new BUY orders.  SELL orders are never blocked here."""

    def __init__(self, min_order_krw: float, max_open_positions: int, max_position_pct: float, max_total_exposure_pct: float, max_order_krw: float):
        self.min_order_krw = min_order_krw
        self.max_open_positions = max_open_positions
        self.max_position_pct = max_position_pct
        self.max_total_exposure_pct = max_total_exposure_pct
        self.max_order_krw = max_order_krw

    def validate_buy(self, market: str, order_krw: float, available_krw: float, total_equity: float, held_markets: list[str]) -> tuple[bool, str]:
        if order_krw < self.min_order_krw:
            return False, "최소 주문금액 미달"
        if order_krw > available_krw:
            return False, "가용 KRW 초과"
        if self.max_order_krw > 0 and order_krw > self.max_order_krw:
            return False, "건당 주문 한도 초과"
        if total_equity <= 0:
            return False, "총 자산 평가 실패"
        if order_krw / total_equity > self.max_position_pct:
            return False, "종목당 비중 한도 초과"
        if len(held_markets) >= self.max_open_positions and market not in held_markets:
            return False, "동시 보유 종목 수 한도 초과"
        # Available KRW is the uninvested portion; this prevents spending beyond the
        # configured total exposure even when several targets are evaluated in a cycle.
        projected_exposure = 1.0 - max(0.0, available_krw - order_krw) / total_equity
        if projected_exposure > self.max_total_exposure_pct:
            return False, "총 투자 비중 한도 초과"
        return True, "OK"
