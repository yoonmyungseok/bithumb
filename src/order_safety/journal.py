"""Durable order intent journal with REST reconciliation support."""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from typing import Any

from db_manager import get_db_manager, get_exchange_db_path
from order_safety.types import OrderStatus
from state_store import load_json_with_backup_recovery, write_json_atomically

logger = logging.getLogger(__name__)

# 재시작 시 저널 대사 시각의 오래됨을 판정하는 임계값(초).
# 일반적인 REST 주문 대사 주기는 5분(300초)이며, 2주기(10분=600초)를 초과하여 대사가 실행되지 않은 상태에서
# 재시작되면 직전 메모리의 READY 상태를 신뢰할 수 없으므로, PENDING 상태로 시작하여 최초 REST 대사 및
# 영속화 검증이 완료될 때까지 신규 BUY 주문을 fail-closed로 차단한다.
MAX_RECONCILIATION_STALENESS_SEC = 600.0

class OrderJournal:
    """Small append-only JSON journal, written atomically for crash recovery (Schema v2)."""

    SCHEMA_VERSION = 4

    def __init__(self, path: str | None = None, data_dir: str | None = None, exchange_scope: str = ""):
        self._lock = threading.RLock()
        # journal.py가 src/order_safety/에 위치하므로 3단계 상위가 프로젝트 루트이다.
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        d_dir = data_dir or os.path.join(project_root, "data")
        os.makedirs(d_dir, exist_ok=True)
        self.path = path or os.path.join(d_dir, "order_journal.json")
        # 주문 저널은 상위 data/가 아니라 해당 거래소의 data_dir DB를 사용한다.
        self.db = get_db_manager(get_exchange_db_path(d_dir))
        # 저장소 간 주문/학습 데이터 혼입을 막기 위한 명시적 거래소 범위다.
        self.exchange_scope = exchange_scope.strip().lower()
        self.reconciliation_state = "PENDING"
        self._last_reconcile_failed_count = 0
        # 대사 백필 지연과 실패를 대시보드/로그에서 추적할 수 있게 보존한다.
        self.reconciliation_metrics: dict[str, Any] = {
            "last_started_at": 0.0, "last_completed_at": 0.0,
            "last_updated_count": 0, "last_failed_count": 0,
        }
        self.orders: list[dict[str, Any]] = self._load()

    def _load(self) -> list[dict[str, Any]]:
        data = load_json_with_backup_recovery(self.path, default=[])
        stored_scope = ""
        if isinstance(data, dict):
            # Schema v2 object wrapper 호환
            stored_scope = str(data.get("exchange_scope", "")).lower()
            self.reconciliation_state = str(data.get("reconciliation_state", "PENDING"))
            saved_metrics = data.get("reconciliation_metrics", {})
            if isinstance(saved_metrics, dict):
                self.reconciliation_metrics.update(saved_metrics)
            orders = data.get("orders", [])
        elif isinstance(data, list):
            # Schema v1 raw list 호환
            orders = data
        else:
            return []

        if not isinstance(orders, list):
            return []
        if self.exchange_scope and stored_scope and stored_scope != self.exchange_scope:
            logger.warning("주문 저널 거래소 범위 불일치(%s != %s): 신규 매수를 차단합니다.", stored_scope, self.exchange_scope)
            self.reconciliation_state = "PENDING"

        # 재시작 안전장치: 마지막 대사 완료 시각이 0이거나 오래된(10분 초과) 경우
        # 저장된 파일의 reconciliation_state가 READY여도 신뢰하지 않고 PENDING으로 강제하여
        # 재시작 후 첫 REST 대사 및 디스크 영속화 검증이 완료되기 전까지 신규 BUY를 차단한다.
        now = time.time()
        last_completed = float(self.reconciliation_metrics.get("last_completed_at", 0.0) or 0.0)
        if last_completed <= 0.0 or (now - last_completed) > MAX_RECONCILIATION_STALENESS_SEC:
            if self.reconciliation_state == "READY":
                logger.warning(
                    "주문 저널의 마지막 대사 시각이 오래되었거나(경과: %.1f초, 기준: %.1f초) 미완료 상태여서 재시작 시 신규 BUY를 안전하게 차단(PENDING)합니다.",
                    max(0.0, now - last_completed) if last_completed > 0 else -1.0,
                    MAX_RECONCILIATION_STALENESS_SEC,
                )
            self.reconciliation_state = "PENDING"

        if not self.exchange_scope:
            return orders

        active, quarantined = [], []
        for order in orders:
            order_exchange = str(order.get("exchange", "")).lower()
            if order_exchange and order_exchange != self.exchange_scope:
                quarantined.append(order)
            else:
                active.append(order)
        if quarantined:
            # 다른 거래소 기록은 삭제하지 않고 별도 감사 파일에 보존한다.
            audit_path = f"{self.path}.{self.exchange_scope}.foreign-orders.audit.json"
            write_json_atomically(audit_path, {"schema_version": self.SCHEMA_VERSION, "orders": quarantined})
            logger.warning("다른 거래소 주문 %d건을 감사 파일로 격리했습니다.", len(quarantined))
            self.reconciliation_state = "PENDING"
        return active

    def _save(self) -> bool:
        """주문 저널을 원자적으로 디스크에 저장하고, 저장 결과를 재읽기하여 검증한다.

        Returns:
            bool: 원자 저장 및 디스크 재읽기 검증 성공 시 True, 실패 시 False
        """
        now = time.time()
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "updated_at": now,
            "exchange_scope": self.exchange_scope,
            "reconciliation_state": self.reconciliation_state,
            "reconciliation_metrics": dict(self.reconciliation_metrics),
            "orders": self.orders[-500:],
        }
        try:
            write_json_atomically(self.path, payload)
        except Exception as exc:
            logger.error("주문 저널 원자 저장 실패 (경로: %s): %s", os.path.abspath(self.path), exc)
            return False

        # 저장 직후 디스크의 JSON을 재읽기하여 영속화 정합성을 검증한다.
        try:
            verified = load_json_with_backup_recovery(self.path, default=None)
            if not isinstance(verified, dict):
                logger.error("주문 저널 영속화 검증 실패 (경로: %s): 저장된 데이터가 유효한 딕셔너리가 아닙니다.", os.path.abspath(self.path))
                return False

            if verified.get("schema_version") != self.SCHEMA_VERSION:
                logger.error(
                    "주문 저널 영속화 검증 실패 (경로: %s): 스키마 버전 불일치 (기대: %s, 실제: %s)",
                    os.path.abspath(self.path), self.SCHEMA_VERSION, verified.get("schema_version"),
                )
                return False

            saved_scope = str(verified.get("exchange_scope", "")).lower()
            if self.exchange_scope and saved_scope != self.exchange_scope:
                logger.error(
                    "주문 저널 영속화 검증 실패 (경로: %s): 거래소 범위 불일치 (기대: %s, 실제: %s)",
                    os.path.abspath(self.path), self.exchange_scope, saved_scope,
                )
                return False

            saved_updated_at = float(verified.get("updated_at", 0.0) or 0.0)
            if abs(saved_updated_at - now) > 1e-4:
                logger.error(
                    "주문 저널 영속화 검증 실패 (경로: %s): updated_at 미반영 (기대: %f, 실제: %f)",
                    os.path.abspath(self.path), now, saved_updated_at,
                )
                return False

            saved_metrics = verified.get("reconciliation_metrics", {})
            if isinstance(saved_metrics, dict):
                exp_completed = float(self.reconciliation_metrics.get("last_completed_at", 0.0) or 0.0)
                saved_completed = float(saved_metrics.get("last_completed_at", 0.0) or 0.0)
                if abs(saved_completed - exp_completed) > 1e-4:
                    logger.error(
                        "주문 저널 영속화 검증 실패 (경로: %s): last_completed_at 미반영 (기대: %f, 실제: %f)",
                        os.path.abspath(self.path), exp_completed, saved_completed,
                    )
                    return False
        except Exception as exc:
            logger.error("주문 저널 재읽기 검증 중 예외 발생 (경로: %s): %s", os.path.abspath(self.path), exc)
            return False

        # SQLite 백업 동기화 (실패해도 JSON 영속화가 성공했으면 크리티컬 실패로 취급하지 않음)
        try:
            ex = self.exchange_scope or "bithumb"
            for order in self.orders[-5:]:
                self.db.upsert_order(ex, order)
        except Exception as exc:
            logger.debug("SQLite 주문 동기화 예외: %s", exc)

        return True

    def record_intent(
        self,
        market: str,
        side: str,
        volume: float | None,
        price: float | None,
        ord_type: str,
        position_id: str | None = None,
        exit_reason: str | None = None,
        avg_buy_price: float | None = None,
        expected_price: float | None = None,
        entry_strategy_snapshot: dict[str, Any] | None = None,
        exchange: str = "",
    ) -> str:
        client_order_id = f"bot-{int(time.time() * 1000)}-{uuid.uuid4().hex[:10]}"
        with self._lock:
            self.orders.append({
                "client_order_id": client_order_id,
                "position_id": position_id or client_order_id,
                "market": market,
                "side": side,
                "volume": volume,
                "requested_volume": volume or 0.0,
                "price": price,
                "expected_price": expected_price or price or 0.0,
                "slippage_bps": 0.0,
                "ord_type": ord_type,
                "status": OrderStatus.PENDING_SUBMISSION,
                "created_at": time.time(),
                "exchange_uuid": None,
                "executed_volume": 0.0,
                "processed_executed_volume": 0.0,
                "remaining_volume": volume or 0.0,
                "avg_price": 0.0,
                "fee": 0.0,
                "processed_fee": 0.0,
                "exit_reason": exit_reason or "",
                "avg_buy_price": avg_buy_price or 0.0,
                # ACK와 무관한 매수 승인 근거로, 실제 성과 기록 시에만 참조한다.
                "entry_strategy_snapshot": dict(entry_strategy_snapshot or {}),
                "exchange": exchange or "",
            })
            self._save()
        return client_order_id

    def mark(self, client_order_id: str, status: str, **fields: Any) -> None:
        terminal_states = {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.FAILED, OrderStatus.REJECTED}
        with self._lock:
            for order in reversed(self.orders):
                if order["client_order_id"] == client_order_id:
                    # 단조성(Monotonicity) 가드: 이미 최종 상태(FILLED/CANCELED)인 경우 하위 상태(OPEN/ACK)로 역행 방지
                    curr_status = order.get("status")
                    if curr_status in terminal_states and status not in terminal_states:
                        logger.debug("상태 역행 방지: %s -> %s (무시됨)", curr_status, status)
                        return
                    order["status"] = status
                    order["updated_at"] = time.time()
                    order.update(fields)
                    self._save()
                    return

    def get_recent_orders(self, limit: int = 10) -> list[dict[str, Any]]:
        """Return the most recent orders (newest first)."""
        with self._lock:
            return list(reversed(self.orders[-limit:]))

    def is_managed_order(self, order_uuid_or_client_id: str) -> bool:
        """Check if an order UUID or client_order_id originated from this bot."""
        if not order_uuid_or_client_id:
            return False
        with self._lock:
            return any(
                order.get("client_order_id") == order_uuid_or_client_id
                or order.get("exchange_uuid") == order_uuid_or_client_id
                or order.get("exchange_order_id") == order_uuid_or_client_id
                for order in self.orders
            )

    def mark_by_uuid(self, exchange_uuid: str, status: str, **fields: Any) -> None:
        """Update order status using exchange UUID."""
        with self._lock:
            for order in reversed(self.orders):
                if order.get("exchange_uuid") == exchange_uuid or order.get("exchange_order_id") == exchange_uuid:
                    order["status"] = status
                    order["updated_at"] = time.time()
                    order.update(fields)
                    self._save()
                    return

    def get_order_by_uuid(self, exchange_uuid: str) -> dict[str, Any] | None:
        """Lookup order by exchange UUID."""
        with self._lock:
            for order in reversed(self.orders):
                if order.get("exchange_uuid") == exchange_uuid or order.get("exchange_order_id") == exchange_uuid:
                    return dict(order)
        return None

    def get_order_by_client_id(self, client_order_id: str) -> dict[str, Any] | None:
        """Lookup order by client_order_id."""
        with self._lock:
            for order in reversed(self.orders):
                if order.get("client_order_id") == client_order_id:
                    return dict(order)
        return None

    def get_entry_order_for_exit(self, order: dict[str, Any]) -> dict[str, Any] | None:
        """매도 주문에 연결된 최신 실제 매수 주문을 찾아 진입 메타데이터를 복원한다."""
        market = order.get("market")
        position_id = order.get("position_id")
        with self._lock:
            for candidate in reversed(self.orders):
                if str(candidate.get("side", "")).lower() not in ("bid", "buy"):
                    continue
                if position_id and candidate.get("position_id") == position_id:
                    return dict(candidate)
                # 구 스키마의 market 기반 position_id도 재시작 후 읽을 수 있게 최후에만 호환한다.
                if candidate.get("market") == market and float(candidate.get("executed_volume", 0.0) or 0.0) > 0:
                    return dict(candidate)
        return None

    def get_latest_confirmed_entry_volume(self, market: str) -> float:
        """해당 거래소 저널에서 최근 포지션의 최초 확정 매수 누적 수량을 반환한다."""
        with self._lock:
            for candidate in reversed(self.orders):
                if candidate.get("market") != market:
                    continue
                if str(candidate.get("side", "")).lower() not in ("bid", "buy"):
                    continue
                filled = float(candidate.get("processed_executed_volume", 0.0) or 0.0)
                if filled > 0.0:
                    return filled
        return 0.0

    def has_unresolved_market(self, market: str) -> bool:
        with self._lock:
            return any(
                order.get("market") == market
                and order.get("status") in {
                    OrderStatus.PENDING_SUBMISSION,
                    OrderStatus.UNKNOWN,
                    OrderStatus.ACKNOWLEDGED,
                    OrderStatus.OPEN,
                    OrderStatus.PARTIALLY_FILLED,
                    OrderStatus.RECONCILIATION_PENDING,
                }
                for order in self.orders
            )

    def has_unknown_market(self, market: str) -> bool:
        """네트워크 타임아웃 등으로 상태가 UNKNOWN인 미해결 주문이 있는지 확인"""
        with self._lock:
            return any(
                order.get("market") == market and order.get("status") == OrderStatus.UNKNOWN
                for order in self.orders
            )

    def has_active_exit_order(self, market: str) -> bool:
        """해당 종목에 이미 진행 중인 매도/청산 주문이 있는지 확인 (중복 청산 방지)"""
        with self._lock:
            return any(
                order.get("market") == market
                and str(order.get("side", "")).lower() in ("ask", "sell")
                and order.get("status") in {
                    OrderStatus.PENDING_SUBMISSION,
                    OrderStatus.UNKNOWN,
                    OrderStatus.ACKNOWLEDGED,
                    OrderStatus.OPEN,
                    OrderStatus.PARTIALLY_FILLED,
                    OrderStatus.RECONCILIATION_PENDING,
                }
                for order in self.orders
            )

    def reconcile_exchange_statuses(
        self,
        get_order: Any,
        get_order_by_client_id: Any | None = None,
        fill_processor: Any | None = None,
    ) -> int:
        """Update acknowledged orders from the exchange's canonical order endpoint with execution reconciler (P0-1)."""
        updated = 0
        self.reconciliation_metrics["last_started_at"] = time.time()
        # 이번 전체 대사 회차의 실패만으로 진입 재개 여부를 판단한다.
        self._last_reconcile_failed_count = 0
        state_map = {
            "wait": OrderStatus.OPEN,
            "watch": OrderStatus.OPEN,
            "trade": OrderStatus.PARTIALLY_FILLED,
            "done": OrderStatus.FILLED,
            "cancel": OrderStatus.CANCELED,
        }
        with self._lock:
            orders_snapshot = list(self.orders)

        for local in orders_snapshot:
            exchange_uuid = local.get("exchange_uuid") or local.get("exchange_order_id")
            if local.get("status") not in {
                OrderStatus.PENDING_SUBMISSION,
                OrderStatus.UNKNOWN,
                OrderStatus.ACKNOWLEDGED,
                OrderStatus.OPEN,
                OrderStatus.PARTIALLY_FILLED,
                OrderStatus.RECONCILIATION_PENDING,
            }:
                continue
            try:
                if exchange_uuid:
                    remote = get_order(exchange_uuid)
                elif get_order_by_client_id:
                    remote = get_order_by_client_id(local["client_order_id"])
                else:
                    continue
            except Exception as exc:
                # 대사 실패 시 신규 진입을 열지 않도록 실패 횟수를 보존한다.
                self._last_reconcile_failed_count += 1
                logger.warning("주문 REST 대사 실패(%s): %s", local.get("client_order_id"), exc)
                continue

            if not isinstance(remote, dict):
                continue

            raw_state = str(remote.get("state") or remote.get("status") or "").lower()
            state = state_map.get(raw_state, local.get("status"))
            exec_vol = float(remote.get("executed_volume", 0.0) or 0.0)
            rem_vol = float(remote.get("remaining_volume", 0.0) or 0.0)
            paid_fee = float(remote.get("paid_fee", 0.0) or 0.0)
            trades = remote.get("trades", [])
            avg_p = 0.0
            if trades:
                total_funds = sum(float(t.get("price", 0.0)) * float(t.get("volume", 0.0)) for t in trades)
                total_v = sum(float(t.get("volume", 0.0)) for t in trades)
                avg_p = (total_funds / total_v) if total_v > 0 else 0.0
            elif exec_vol > 0 and float(remote.get("price", 0.0) or 0.0) > 0:
                avg_p = float(remote.get("price", 0.0))
            elif float(local.get("price", 0.0) or 0.0) > 0:
                avg_p = float(local.get("price", 0.0))

            # 거래소 응답의 수량·가격이 모순되면 손익을 산출하지 않고 재대사를 요구한다.
            requested = float(local.get("requested_volume", 0.0) or 0.0)
            tolerance = max(1e-8, requested * 1e-6)
            invalid_fill = (
                exec_vol < 0 or rem_vol < 0
                or (requested > 0 and exec_vol > requested + tolerance)
                or (requested > 0 and exec_vol + rem_vol > requested + tolerance)
                or (exec_vol > 0 and avg_p <= 0)
                or (raw_state == "done" and rem_vol > tolerance)
            )
            if invalid_fill:
                self._last_reconcile_failed_count += 1
                self.mark(
                    local["client_order_id"], OrderStatus.RECONCILIATION_PENDING,
                    reconciliation_reason="REST 체결 수량 또는 평균 체결가 검증 실패",
                    exchange_state=raw_state,
                    last_event_at=time.time(),
                )
                logger.error("🛑 [%s] REST 체결 데이터가 모순되어 신규 진입을 차단합니다.", local.get("market"))
                continue

            if fill_processor:
                fill_processor.process_order_fill(
                    order_identifier=local["client_order_id"],
                    status=state,
                    executed_volume=exec_vol,
                    avg_price=avg_p,
                    fee=paid_fee,
                    remaining_volume=rem_vol,
                    exchange_uuid=remote.get("uuid") or remote.get("order_id", exchange_uuid),
                    exchange_state=raw_state,
                )
                updated += 1
            else:
                self.mark(
                    local["client_order_id"],
                    state,
                    exchange_state=raw_state,
                    exchange_uuid=remote.get("uuid") or remote.get("order_id", exchange_uuid),
                    executed_volume=exec_vol,
                    remaining_volume=rem_vol,
                    avg_price=avg_p,
                    fee=paid_fee,
                    trades=trades,
                )
                updated += 1
        self.reconciliation_metrics.update({
            "last_completed_at": time.time(),
            "last_updated_count": updated,
            "last_failed_count": self._last_reconcile_failed_count,
        })
        # 대사 관측값은 신규 매수 차단 판단에 영향을 주므로 즉시 영속화하고 검증한다.
        saved_ok = self._save()
        if not saved_ok:
            self._last_reconcile_failed_count += 1
            self.reconciliation_state = "PENDING"
            logger.error("🛑 주문 저널 영속화 검증 실패로 대사 결과가 디스크에 확정되지 않아 신규 매수를 차단(PENDING)합니다.")
        logger.info(
            "REST 주문 대사 완료: 갱신=%d 실패=%d", updated, self._last_reconcile_failed_count,
        )
        return updated

    def reconcile_open_orders(self, open_orders: list[dict[str, Any]]) -> int:
        """Attach exchange UUIDs where an intent can be unambiguously matched."""
        matched = 0
        with self._lock:
            orders_snapshot = list(self.orders)

        for local in orders_snapshot:
            if local.get("status") not in {OrderStatus.PENDING_SUBMISSION, OrderStatus.UNKNOWN}:
                continue
            for remote in open_orders:
                if remote.get("market") != local.get("market") or remote.get("side") != local.get("side"):
                    continue
                if local.get("ord_type") != remote.get("ord_type"):
                    continue
                remote_uuid = remote.get("uuid") or remote.get("order_id")
                if remote_uuid:
                    self.mark(local["client_order_id"], OrderStatus.OPEN, exchange_uuid=remote_uuid)
                    matched += 1
                    break
        return matched

    def is_entry_ready(self) -> bool:
        """초기 REST 대사 전에는 신규 매수만 차단하고 기존 포지션 보호는 계속 허용한다."""
        return self.reconciliation_state == "READY"

    def suspend_entry_for_reconciliation(self, reason: str) -> None:
        """신규 BUY만 차단하고 기존 포지션 보호·청산 경로는 유지한다."""
        reason = str(reason or "").strip()
        if not reason:
            reason = "unknown"
        now = time.time()
        with self._lock:
            last_reason = str(self.reconciliation_metrics.get("last_suspend_reason", ""))
            last_at = float(self.reconciliation_metrics.get("last_suspend_at", 0.0) or 0.0)
            # 이미 PENDING이면 동일 사유 60초 이내 중복 저장·로그를 생략한다.
            if self.reconciliation_state == "PENDING" and last_reason == reason and now - last_at < 60.0:
                return
            was_ready = self.reconciliation_state == "READY"
            self.reconciliation_state = "PENDING"
            self.reconciliation_metrics["last_suspend_reason"] = reason
            self.reconciliation_metrics["last_suspend_at"] = now
            self._save()
        if was_ready or last_reason != reason:
            logger.warning("🛑 신규 매수 차단: %s", reason)

    def complete_reconciliation_if_safe(self) -> bool:
        """모든 미완료 주문의 REST 대사 및 저널 영속화 검증이 성공한 경우에만 신규 진입을 재개한다."""
        with self._lock:
            unresolved = any(order.get("status") in {
                OrderStatus.PENDING_SUBMISSION, OrderStatus.UNKNOWN, OrderStatus.ACKNOWLEDGED,
                OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED, OrderStatus.RECONCILIATION_PENDING,
            } for order in self.orders)
            if self._last_reconcile_failed_count or unresolved:
                return False
            was_ready = self.reconciliation_state == "READY"
            if not was_ready:
                self.reconciliation_state = "READY"
                if not self._save():
                    self.reconciliation_state = "PENDING"
                    logger.error("🛑 REST 주문 대사 완료 후 저널 영속화 검증에 실패하여 신규 매수 차단(PENDING)을 유지합니다.")
                    return False
                logger.warning("✅ REST 주문 대사 및 저널 영속화가 완료되어 신규 매수 안전 모드를 해제했습니다.")
            return True

    def apply_private_order_event(
        self,
        event: dict[str, Any],
        fill_processor: Any | None = None,
        require_rest_confirmation: bool = False,
    ) -> bool:
        """Private WebSocket 이벤트를 반영하되, 필요 시 REST 대사 전 손익 확정을 금지한다."""
        client_id = event.get("client_order_id") or event.get("coid") or event.get("identifier")
        state = str(event.get("state") or event.get("s") or event.get("status") or "").lower()
        mapped = {
            "wait": OrderStatus.OPEN,
            "trade": OrderStatus.PARTIALLY_FILLED,
            "done": OrderStatus.FILLED,
            "cancel": OrderStatus.CANCELED,
        }.get(state)
        if not client_id or not mapped:
            return False

        # volume은 업비트에서 주문 원수량일 수 있어 체결 누적수량으로 사용하지 않는다.
        exec_vol = float(event.get("executed_volume") or event.get("ev") or 0.0)
        rem_vol = float(event.get("remaining_volume") or event.get("rv") or 0.0)
        price_val = float(event.get("price") or event.get("p") or 0.0)
        fee_val = float(event.get("paid_fee") or event.get("fee") or 0.0)
        oid = event.get("order_id") or event.get("oid") or event.get("uuid")

        local = self.get_order_by_client_id(str(client_id))
        if not local:
            return False
        requested = float(local.get("requested_volume", 0.0) or 0.0)
        tolerance = max(1e-8, requested * 1e-6)
        invalid = (
            exec_vol < 0 or rem_vol < 0
            or (requested > 0 and exec_vol > requested + tolerance)
            or (requested > 0 and exec_vol + rem_vol > requested + tolerance)
        )
        if require_rest_confirmation or invalid:
            # WebSocket은 빠른 알림용이며, 평균 체결가·수수료는 REST가 기준이다.
            self.mark(
                str(client_id), OrderStatus.RECONCILIATION_PENDING,
                exchange_order_id=oid,
                exchange_state=state,
                reconciliation_reason="Private WebSocket 수신 후 REST 체결 대기",
                last_event_at=time.time(),
            )
            return True

        if fill_processor:
            fill_processor.process_order_fill(
                order_identifier=str(client_id),
                status=mapped,
                executed_volume=exec_vol,
                avg_price=price_val,
                fee=fee_val,
                remaining_volume=rem_vol,
                exchange_uuid=oid,
                exchange_state=state,
            )
            return True

        update_kwargs: dict[str, Any] = {
            "exchange_order_id": oid,
            "executed_volume": exec_vol,
            "remaining_volume": rem_vol,
            "last_event_at": time.time(),
        }
        if price_val > 0:
            update_kwargs["avg_price"] = price_val
        if fee_val > 0:
            update_kwargs["fee"] = fee_val

        self.mark(str(client_id), mapped, **update_kwargs)
        return True
