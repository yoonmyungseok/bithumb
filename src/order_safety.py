"""Durable order intent tracking and pre-trade risk checks.

The exchange is the source of truth for fills.  This module deliberately treats a
network failure after an order submission as *unknown*, never as permission to
submit the same order again.
"""

import json
import logging
import os
import tempfile
import threading
import time
import uuid
from typing import Any

import requests

from db_manager import get_db_manager
from market_policy import get_excluded_markets
from risk_controls import RiskGuard as _RiskGuard
from risk_controls import calculate_risk_position_size as _calculate_risk_position_size
from risk_controls import get_dynamic_portfolio_tiers as _get_dynamic_portfolio_tiers
from state_store import load_json_with_backup_recovery as _load_json_with_backup_recovery
from state_store import write_json_atomically as _write_json_atomically

logger = logging.getLogger(__name__)


_FILE_WRITE_LOCK = threading.RLock()


def _legacy_write_json_atomically(path: str, payload: Any) -> None:
    """Persist JSON without leaving a partially-written state file after a crash,
    with robust retry and fallback logic for Windows file-locking quirks (WinError 5 / 32).
    Also maintains a .bak file for self-healing crash recovery."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    backup_path = f"{path}.bak"

    with _FILE_WRITE_LOCK:
        fd, temporary_path = tempfile.mkstemp(prefix="state_", suffix=".tmp", dir=directory, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(payload, file, ensure_ascii=False, indent=2)
                file.flush()
                os.fsync(file.fileno())

            # Windows 파일 잠금/안티바이러스 스캔 경합 해결을 위한 재시도 루프 (최대 5회)
            replaced = False
            for attempt in range(5):
                try:
                    os.replace(temporary_path, path)
                    replaced = True
                    break
                except (PermissionError, OSError):
                    time.sleep(0.02 * (2**attempt))

            if not replaced:
                # 최후의 수단: 직접 파일 덮어쓰기
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)

            # 성공적으로 주 파일 작성 후 자가 치유용 .bak 백업 파일 동기화
            try:
                with open(backup_path, "w", encoding="utf-8") as bf:
                    json.dump(payload, bf, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.debug("백업 파일(.bak) 저장 예외 (무시 가능): %s", e)

        finally:
            if os.path.exists(temporary_path):
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass


def _legacy_load_json_with_backup_recovery(path: str, default: Any = None) -> Any:
    """Load JSON from path with automatic fallback to .bak on corruption or decode failure."""
    backup_path = f"{path}.bak"
    if not os.path.exists(path):
        if os.path.exists(backup_path):
            try:
                with open(backup_path, "r", encoding="utf-8") as bf:
                    data = json.load(bf)
                    logger.warning("주 파일(%s) 부재로 백업본(%s)에서 자동 복구", path, backup_path)
                    # 주 파일 복원 시도
                    try:
                        _write_json_atomically(path, data)
                    except Exception:
                        pass
                    return data
            except Exception as e:
                logger.error("백업 파일(%s) 로드 실패: %s", backup_path, e)
        return default

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("⚠️ 주 파일(%s) 손상 감지 (%s) ➜ 백업 파일(.bak) 자가 복구 시도", path, exc)
        if os.path.exists(backup_path):
            try:
                with open(backup_path, "r", encoding="utf-8") as bf:
                    recovered_data = json.load(bf)
                    logger.info("✅ 백업 파일(%s)로부터 데이터 자가 복구 성공! 주 파일 복원", backup_path)
                    try:
                        _write_json_atomically(path, recovered_data)
                    except Exception:
                        pass
                    return recovered_data
            except Exception as backup_exc:
                logger.error("❌ 백업 파일(.bak) 복구마저 실패: %s", backup_exc)
        return default



def get_excluded_markets_set() -> set[str]:
    """수동 매매 보호 종목 집합 반환 (P3-1 단일 진실의 원천)"""
    return get_excluded_markets()


# Compatibility exports.  State persistence now belongs to state_store.py;
# existing consumers retain these import paths during the migration.
write_json_atomically = _write_json_atomically
load_json_with_backup_recovery = _load_json_with_backup_recovery


class OrderStatus:
    """주문 생애주기 명시적 상태 정의"""
    PENDING_SUBMISSION = "PENDING_SUBMISSION"
    UNKNOWN = "UNKNOWN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    # Private WebSocket 이벤트만으로는 실제 평균 체결가를 확정하지 않는다.
    RECONCILIATION_PENDING = "RECONCILIATION_PENDING"


class AmbiguousOrderError(RuntimeError):
    """The exchange may have accepted an order, but its response was not received."""


class OrderJournal:
    """Small append-only JSON journal, written atomically for crash recovery (Schema v2)."""

    SCHEMA_VERSION = 4

    def __init__(self, path: str | None = None, data_dir: str | None = None, exchange_scope: str = ""):
        self._lock = threading.RLock()
        d_dir = data_dir or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        os.makedirs(d_dir, exist_ok=True)
        self.path = path or os.path.join(d_dir, "order_journal.json")
        self.db = get_db_manager(os.path.join(os.path.dirname(d_dir) if "upbit" in d_dir.lower() else d_dir, "trading.db"))
        # 저장소 간 주문/학습 데이터 혼입을 막기 위한 명시적 거래소 범위다.
        self.exchange_scope = exchange_scope.strip().lower()
        self.reconciliation_state = "PENDING"
        self._last_reconcile_failed_count = 0
        self.orders: list[dict[str, Any]] = self._load()

    def _load(self) -> list[dict[str, Any]]:
        data = load_json_with_backup_recovery(self.path, default=[])
        stored_scope = ""
        if isinstance(data, dict):
            # Schema v2 object wrapper 호환
            stored_scope = str(data.get("exchange_scope", "")).lower()
            self.reconciliation_state = str(data.get("reconciliation_state", "PENDING"))
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

    def _save(self) -> None:
        try:
            payload = {
                "schema_version": self.SCHEMA_VERSION,
                "updated_at": time.time(),
                "exchange_scope": self.exchange_scope,
                "reconciliation_state": self.reconciliation_state,
                "orders": self.orders[-500:],
            }
            write_json_atomically(self.path, payload)
        except Exception as exc:
            logger.warning("주문 저널 저장 경고: %s", exc)
        try:
            ex = self.exchange_scope or "bithumb"
            for order in self.orders[-5:]:
                self.db.upsert_order(ex, order)
        except Exception as exc:
            logger.debug("SQLite 주문 동기화 예외: %s", exc)

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

    def complete_reconciliation_if_safe(self) -> bool:
        """모든 미완료 주문의 REST 대사가 성공한 경우에만 신규 진입을 재개한다."""
        with self._lock:
            unresolved = any(order.get("status") in {
                OrderStatus.PENDING_SUBMISSION, OrderStatus.UNKNOWN, OrderStatus.ACKNOWLEDGED,
                OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED, OrderStatus.RECONCILIATION_PENDING,
            } for order in self.orders)
            if self._last_reconcile_failed_count or unresolved:
                return False
            if self.reconciliation_state != "READY":
                self.reconciliation_state = "READY"
                self._save()
                logger.warning("✅ REST 주문 대사가 완료되어 신규 매수 안전 모드를 해제했습니다.")
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


class OrderFillProcessor:
    """
    공통 체결 처리기 (Execution Reconciler / OrderFillProcessor)
    - “거래소의 주문 접수 ACK는 체결이 아니다. 실제 체결이 확인된 수량(fill_delta)만 포지션과 손익에 반영한다.”
    - Private WebSocket, REST reconcile, 실시간 리스크 엔진, 5분 루프 공통 단일 진입점
    """

    def __init__(
        self,
        order_journal: OrderJournal,
        risk_manager: Any = None,
        trade_memory: Any = None,
        trailing_tracker: Any = None,
        telegram: Any = None,
        send_fill_alerts: bool = False,
    ):
        self.order_journal = order_journal
        self.risk_manager = risk_manager
        self.trade_memory = trade_memory
        self.trailing_tracker = trailing_tracker
        self.telegram = telegram
        self.send_fill_alerts = send_fill_alerts

    def process_order_fill(
        self,
        order_identifier: str,
        status: str,
        executed_volume: float,
        avg_price: float = 0.0,
        fee: float = 0.0,
        remaining_volume: float = 0.0,
        exchange_uuid: str | None = None,
        exchange_state: str | None = None,
        exit_reason: str | None = None,
        avg_buy_price: float | None = None,
        korean_name: str | None = None,
        timestamp_str: str | None = None,
        expected_price: float | None = None,
        chart_img: bytes | None = None,
    ) -> dict[str, Any]:
        """
        체결 내역을 멱등(Idempotent)하고 단조적(Monotonic)으로 반영하며 슬리피지를 추적하는 단일 체결 처리기 (P0-1, P0-3)
        """
        with self.order_journal._lock:
            order = None
            for o in self.order_journal.orders:
                if (
                    o.get("client_order_id") == order_identifier
                    or (exchange_uuid and o.get("exchange_uuid") == exchange_uuid)
                    or o.get("exchange_order_id") == order_identifier
                ):
                    order = o
                    break

            if not order:
                logger.debug("체결 처리 대상 주문 미발견: %s", order_identifier)
                return {"processed": False, "fill_delta": 0.0, "pnl_krw": 0.0, "status": status}

            client_order_id = order["client_order_id"]
            market = order["market"]
            side = str(order.get("side", "")).lower()
            is_buy = side in ("bid", "buy")
            position_id = order.get("position_id") or market
            stored_exit_reason = order.get("exit_reason") or exit_reason or "MANUAL_EXIT"

            prev_processed_vol = float(order.get("processed_executed_volume", 0.0) or 0.0)
            prev_processed_fee = float(order.get("processed_fee", 0.0) or 0.0)

            fill_delta = max(0.0, float(executed_volume) - prev_processed_vol)
            fee_delta = max(0.0, float(fee) - prev_processed_fee)

            pnl_krw = 0.0
            pnl_pct = 0.0
            effective_price = avg_price if avg_price > 0 else float(order.get("price", 0.0) or 0.0)
            now_str = timestamp_str or time.strftime("%Y-%m-%d %H:%M:%S")

            # 실시간 체결 슬리피지(Slippage Bps) 연산
            exp_p = expected_price or float(order.get("expected_price", 0.0) or order.get("price", 0.0) or 0.0)
            slippage_bps = 0.0
            if exp_p > 0 and effective_price > 0:
                if is_buy:
                    slippage_bps = ((effective_price - exp_p) / exp_p) * 10000.0
                else:
                    slippage_bps = ((exp_p - effective_price) / exp_p) * 10000.0

            if abs(slippage_bps) >= 30.0:
                logger.warning(
                    f"⚠️ [{market}] 슬리피지 편차 감지: {slippage_bps:+.1f} bps (체결단가: {effective_price:,.2f} vs 목표단가: {exp_p:,.2f})"
                )

            # 1. 체결 증가분(fill_delta > 0)에 대해서만 포지션 및 손익 반영
            if fill_delta > 0:
                if is_buy:
                    # 매수 첫 체결 시점에만 진입시간 생성 (P0-1)
                    if prev_processed_vol == 0.0 and self.trailing_tracker:
                        self.trailing_tracker.set_entry_time(market)
                    logger.info(
                        "🛒 [%s] 실제 매수 체결 확인: 증가분=%.6f, 체결단가=%.2f, 수수료=%.2f, 슬리피지=%+.1fbps",
                        market, fill_delta, effective_price, fee_delta, slippage_bps,
                    )

                    # 텔레그램 매수 체결 알림 발송 (체결 기반 실시간 알림)
                    entry_snapshot = dict(order.get("entry_strategy_snapshot") or {})
                    k_name = korean_name or entry_snapshot.get("korean_name") or order.get("korean_name") or market.replace("KRW-", "")
                    tp = entry_snapshot.get("target_price") or order.get("target_price")
                    sl = entry_snapshot.get("stop_loss") or order.get("stop_loss")
                    alpha = entry_snapshot.get("alpha_score")
                    raw_exchange = str(entry_snapshot.get("exchange") or order.get("exchange_name") or "").upper()
                    exchange_tag = "업비트" if "UPBIT" in raw_exchange else "빗썸"
                    entry_reason = entry_snapshot.get("entry_reason") or order.get("entry_reason") or ""
                    currency = market.replace("KRW-", "")
                    total_fill_krw = int(fill_delta * effective_price)

                    is_fully_filled = (status == OrderStatus.FILLED) or (remaining_volume <= 1e-8)
                    status_label = "체결 완료" if is_fully_filled else f"부분 체결 (누적 {prev_processed_vol + fill_delta:.6f})"

                    tp_str = f"• 목표가: {float(tp):,.2f} KRW | 손절가: {float(sl):,.2f} KRW\n" if tp and sl else ""
                    alpha_str = f"• AI 알파 스코어: <b>{alpha}점</b>\n" if alpha is not None else ""
                    reason_str = f"• 진입 사유: <i>{entry_reason}</i>\n" if entry_reason else ""
                    slip_str = f" ({slippage_bps:+.1f} bps)" if abs(slippage_bps) >= 0.1 else ""

                    caption = (
                        f"🛒 <b>[{exchange_tag} {k_name}({market}) 매수 {status_label}!]</b>\n"
                        f"• 실체결 단가: <b>{effective_price:,.2f} KRW</b>{slip_str}\n"
                        f"• 체결 수량: {fill_delta:.6f} {currency}\n"
                        f"• 체결 금액: <b>{total_fill_krw:,d} KRW</b>\n"
                        f"• 지불 수수료: {fee_delta:,.2f} KRW\n"
                        f"{tp_str}"
                        f"{alpha_str}"
                        f"{reason_str}"
                        f"• 주문 ID: <code>{order.get('exchange_uuid') or order.get('exchange_order_id') or exchange_uuid or client_order_id}</code>\n"
                        f"• 체결 일시: {now_str}"
                    )

                    if self.send_fill_alerts and self.telegram:
                        try:
                            if chart_img:
                                self.telegram.send_photo(chart_img, caption=caption)
                            else:
                                self.telegram.send_message(caption)
                        except Exception as te:
                            logger.warning("매수 체결 텔레그램 알림 발송 실패: %s", te)


                else:
                    # 매도 체결: 실제 체결 증가분만 실현 손익 계산 (P0-1, P0-3)
                    effective_avg_buy_price = avg_buy_price or float(order.get("avg_buy_price", 0.0) or 0.0)
                    if effective_price <= 0 or effective_avg_buy_price <= 0:
                        # 0원 체결/진입가는 손익·쿨다운·학습 데이터에 절대 기록하지 않는다.
                        self.order_journal.mark(
                            client_order_id,
                            OrderStatus.RECONCILIATION_PENDING,
                            reconciliation_reason="매도 체결가 또는 진입가가 0 이하",
                            last_event_at=time.time(),
                        )
                        logger.error("🛑 [%s] 0원 체결 데이터로 매도 손익 반영을 차단했습니다.", market)
                        return {"processed": False, "fill_delta": 0.0, "pnl_krw": 0.0, "status": OrderStatus.RECONCILIATION_PENDING}
                    proceeds = (effective_price * fill_delta) - fee_delta
                    cost_basis = effective_avg_buy_price * fill_delta
                    pnl_krw = proceeds - cost_basis
                    pnl_pct = ((effective_price - effective_avg_buy_price) / effective_avg_buy_price * 100.0) if effective_avg_buy_price > 0 else 0.0
                    # 실현 손익 기반 청산 사유 레이블 직관화
                    refined_exit_reason = stored_exit_reason
                    is_win = pnl_krw > 0
                    stored_upper = str(stored_exit_reason).upper()
                    if "TRAILING" in stored_upper or "트레일링" in str(stored_exit_reason):
                        if pnl_krw > 0:
                            refined_exit_reason = "트레일링 익절"
                        elif pnl_pct >= -0.5:
                            refined_exit_reason = "트레일링 본전방어"
                        else:
                            refined_exit_reason = "트레일링 방어매도"
                    elif "MOMENTUM_EARLY_EXIT" in stored_upper or "모멘텀" in str(stored_exit_reason):
                        if pnl_krw >= 0:
                            refined_exit_reason = "모멘텀 조기 본전탈출"
                        else:
                            refined_exit_reason = "모멘텀 소멸 방어탈출"
                    elif "TIME_STOP" in stored_upper or "타임스탑" in str(stored_exit_reason):
                        if pnl_krw > 0:
                            refined_exit_reason = "타임스탑 본전익절"
                        elif pnl_pct >= -0.5:
                            refined_exit_reason = "타임스탑 횡보청산"
                        else:
                            refined_exit_reason = "타임스탑 추세이탈청산"
                    elif "PARTIAL_TP" in stored_upper or "분할익절" in str(stored_exit_reason):
                        refined_exit_reason = "1차 분할익절"
                    elif "STOP_LOSS" in stored_upper or "HARD_STOP" in stored_upper or "손절" in str(stored_exit_reason):
                        refined_exit_reason = "손절 방어"
                    if self.risk_manager:
                        self.risk_manager.add_realized_trade(pnl_krw, is_win=is_win)

                    entry_order = self.order_journal.get_entry_order_for_exit(order)
                    entry_snapshot = dict((entry_order or {}).get("entry_strategy_snapshot") or {})
                    # 매도 주문이 구 position_id를 쓰더라도 진입 주문의 고유 ID로 포지션 관계를 보존한다.
                    position_id = (entry_order or {}).get("position_id") or position_id
                    if self.trade_memory:
                        self.trade_memory.record_completed_trade(
                            market=market,
                            side=refined_exit_reason,
                            entry_price=effective_avg_buy_price,
                            exit_price=effective_price,
                            filled_volume=fill_delta,
                            fee=fee_delta,
                            slippage=slippage_bps / 10000.0,
                            pnl_pct=pnl_pct,
                            pnl_krw=pnl_krw,
                            reason=refined_exit_reason,
                            timestamp=now_str,
                            position_id=position_id,
                            order_status=status,
                            exchange=(
                                entry_snapshot.get("exchange")
                                or (entry_order or {}).get("exchange")
                                or getattr(self.trade_memory, "exchange_scope", "")
                                or getattr(self.order_journal, "exchange_scope", "")
                                or "bithumb"
                            ),
                            btc_regime=entry_snapshot.get("entry_btc_regime", "UNKNOWN"),
                            alpha_score=entry_snapshot.get("alpha_score"),
                            indicators=entry_snapshot.get("indicators", {}),
                            entry_btc_regime=entry_snapshot.get("entry_btc_regime", "UNKNOWN"),
                            exit_btc_regime=entry_snapshot.get("exit_btc_regime", "UNKNOWN"),
                            entry_reason=entry_snapshot.get("entry_reason", ""),
                            entry_decision_at=entry_snapshot.get("entry_decision_at", ""),
                            target_price=entry_snapshot.get("target_price"),
                            stop_loss=entry_snapshot.get("stop_loss"),
                        )
                    logger.info(
                        "🎉 [%s] 실제 매도 체결 확인 (%s): 증가분=%.6f, 체결단가=%.2f, 손익=%+.0f원(%.2f%%), 슬리피지=%+.1fbps",
                        market, refined_exit_reason, fill_delta, effective_price, pnl_krw, pnl_pct, slippage_bps,
                    )

                    # [알림 최적화] 거래소 앱 자체 알림 활용을 위해 텔레그램 매도 체결 알림 비활성화
                    pass

            # 2. 완전 청산 상태 도달 시 트레일링 스탑 초기화
            if not is_buy and status in (OrderStatus.FILLED, OrderStatus.CANCELED) and remaining_volume == 0.0:
                if self.trailing_tracker:
                    self.trailing_tracker.clear(market)

            # 3. 저널 상태 영속 업데이트
            update_fields: dict[str, Any] = {
                "executed_volume": executed_volume,
                "processed_executed_volume": executed_volume,
                "remaining_volume": remaining_volume,
                "avg_price": effective_price,
                "fee": fee,
                "processed_fee": fee,
                "slippage_bps": round(slippage_bps, 1),
                "last_event_at": time.time(),
            }
            if exchange_uuid:
                update_fields["exchange_uuid"] = exchange_uuid
            self.order_journal.mark(client_order_id, status, **update_fields)

            return {
                "processed": (fill_delta > 0),
                "fill_delta": fill_delta,
                "fee_delta": fee_delta,
                "pnl_krw": pnl_krw,
                "pnl_pct": pnl_pct,
                "slippage_bps": round(slippage_bps, 1),
                "status": status,
                "order": order,
            }


def get_dynamic_portfolio_tiers(total_equity: float, custom_max_positions: int | None = None) -> tuple[int, float, int]:
    """
    총 평가 자산 규모에 따른 동적 포트폴리오 슬롯 및 비중 한도 자동 스케일링 (Auto-Scaling)
    - 30만 원 미만 (소액): 최대 3종목 (종목당 최대 35%, 스크리닝 상위 10개 정밀 검토)
    - 30만 원 ~ 100만 원 (중소액): 최대 5종목 (종목당 최대 25%, 스크리닝 상위 12개 정밀 검토)
    - 100만 원 이상 (본격 운용): 최대 6종목 (종목당 최대 20%, 스크리닝 상위 15개 정밀 검토)
    Returns: (max_open_positions: int, max_position_pct: float, top_screener_count: int)
    """
    if custom_max_positions and custom_max_positions > 0:
        max_pos = custom_max_positions
        max_pct = round(min(0.50, max(0.15, 1.0 / max_pos + 0.05)), 2)
        top_count = max(10, min(20, max_pos * 3))
        return max_pos, max_pct, top_count

    if total_equity < 300_000.0:
        return 3, 0.35, 10
    elif total_equity < 1_000_000.0:
        return 5, 0.25, 12
    else:
        return 6, 0.20, 15


class RiskGuard:
    """Single decision point for new BUY orders.  SELL orders are never blocked here."""

    def __init__(self, min_order_krw: float, max_open_positions: int, max_position_pct: float, max_total_exposure_pct: float, max_order_krw: float):
        self.min_order_krw = min_order_krw
        self.max_open_positions = max_open_positions
        self.max_position_pct = max_position_pct
        self.max_total_exposure_pct = max_total_exposure_pct
        self.max_order_krw = max_order_krw

    def update_limits(self, max_open_positions: int | None = None, max_position_pct: float | None = None, max_total_exposure_pct: float | None = None) -> None:
        """자산 규모 변동에 따라 동적으로 리스크 한도 갱신"""
        if max_open_positions is not None:
            self.max_open_positions = max_open_positions
        if max_position_pct is not None:
            self.max_position_pct = max_position_pct
        if max_total_exposure_pct is not None:
            self.max_total_exposure_pct = max_total_exposure_pct

    def validate_buy(self, market: str, order_krw: float, available_krw: float, total_equity: float, held_markets: list[str]) -> tuple[bool, str]:
        # 수동 관리 격리 종목(HOLO 등) 매수 원천 차단
        raw_excluded = os.getenv("EXCLUDED_MANUAL_HOLDINGS", "KRW-HOLO,HOLO") + "," + os.getenv("UPBIT_EXCLUDED_MARKETS", "KRW-HOLO,HOLO")
        excluded_items = {x.strip().upper() for x in raw_excluded.split(",") if x.strip()}
        excluded_items.update({"KRW-HOLO", "HOLO"})
        m_upper = market.upper()
        if m_upper in excluded_items or m_upper.replace("KRW-", "") in excluded_items:
            return False, f"수동 관리 격리 종목 ({market}) 매수 불가"

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


class CooldownManager:
    """Tracks post-exit cooldown periods and price gaps to prevent rapid whipsaw re-entries, with disk persistence."""

    def __init__(
        self,
        default_sl_cooldown: float = 2700.0,
        default_tp_cooldown: float = 1800.0,
        default_time_stop_cooldown: float = 2700.0,
        state_file: str | None = None,
        data_dir: str | None = None,
    ):
        self._lock = threading.RLock()
        self.default_sl_cooldown = default_sl_cooldown  # 45 minutes
        self.default_tp_cooldown = default_tp_cooldown  # 30 minutes
        self.default_time_stop_cooldown = default_time_stop_cooldown  # 45 minutes
        d_dir = data_dir or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        os.makedirs(d_dir, exist_ok=True)
        self.state_file = state_file or os.path.join(d_dir, "cooldown_state.json")
        self._records: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        data = load_json_with_backup_recovery(self.state_file, default={})
        records: dict[str, dict[str, Any]] = {}
        if isinstance(data, dict):
            now = time.time()
            for k, v in data.items():
                m_key = str(k).upper()
                if isinstance(v, (int, float)):
                    if float(v) > (now - 7200.0):
                        records[m_key] = {
                            "expire_at": float(v),
                            "exit_type": "STOP_LOSS",
                            "exit_price": 0.0,
                            "timestamp": float(v) - self.default_sl_cooldown,
                        }
                elif isinstance(v, dict):
                    exp = float(v.get("expire_at", 0.0))
                    ts = float(v.get("timestamp", exp - self.default_sl_cooldown))
                    if exp > now or (now - ts < 7200.0):
                        records[m_key] = {
                            "expire_at": exp,
                            "exit_type": str(v.get("exit_type", "UNKNOWN")),
                            "exit_price": float(v.get("exit_price", 0.0)),
                            "timestamp": ts,
                        }
        return records

    def _save(self) -> None:
        try:
            write_json_atomically(self.state_file, self._records)
        except Exception as exc:
            logger.warning(f"쿨다운 상태 파일 저장 실패: {exc}")

    def record_exit(self, market: str, exit_type: str, exit_price: float = 0.0) -> None:
        etype_upper = exit_type.upper()
        if "TIME" in etype_upper:
            duration = self.default_time_stop_cooldown
        elif "STOP" in etype_upper:
            duration = self.default_sl_cooldown
        else:
            duration = self.default_tp_cooldown

        now = time.time()
        expire_at = now + duration
        m_key = market.upper()
        with self._lock:
            self._records[m_key] = {
                "expire_at": expire_at,
                "exit_type": exit_type,
                "exit_price": float(exit_price),
                "timestamp": now,
            }
            self._save()
        price_str = f" (청산가: {exit_price:,.2f}원)" if exit_price > 0 else ""
        logger.info(f"⏳ [{market}] {exit_type} 발생으로 {duration/60:.0f}분간 재진입 쿨다운 적용{price_str} (영속 저장)")

    def is_in_cooldown(self, market: str) -> tuple[bool, float]:
        now = time.time()
        m_key = market.upper()
        with self._lock:
            rec = self._records.get(m_key)
            if not rec:
                return False, 0.0
            expire_at = float(rec.get("expire_at", 0.0))
            if expire_at > now:
                return True, expire_at - now
            ts = float(rec.get("timestamp", 0.0))
            if now - ts >= 7200.0:
                del self._records[m_key]
                self._save()
        return False, 0.0

    def get_last_exit_info(self, market: str) -> dict[str, Any] | None:
        m_key = market.upper()
        with self._lock:
            return self._records.get(m_key)

    def check_reentry_allowed(
        self,
        market: str,
        current_price: float,
        min_gap_pct: float = 0.015,
        expiry_sec: float = 7200.0,
    ) -> tuple[bool, str]:
        """
        쿨다운 타이머 및 직전 청산가 갭 필터를 검증하여 재진입 허용 여부를 결정한다.
        - 1차: 활성 쿨다운 잔여 시간 확인
        - 2차: 타임스탑 청산가 대비 ±1.5% 박스권 횡보 구간 재진입 차단
        - 3차: 손절가 대비 상방 돌파(+1.5%) 미달 시 추격 차단
        - 4차: 트레일링 익절 후 고점 근처(0 ~ +1.5%) 휩쏘 추격 방지
        """
        now = time.time()
        m_key = market.upper()
        with self._lock:
            rec = self._records.get(m_key)
            if not rec:
                return True, "OK"

            expire_at = float(rec.get("expire_at", 0.0))
            if expire_at > now:
                cd_rem = expire_at - now
                return False, f"⏳ 쿨다운 대기 중 ({cd_rem/60:.1f}분 남음)"

            ts = float(rec.get("timestamp", 0.0))
            exit_price = float(rec.get("exit_price", 0.0))
            exit_type = str(rec.get("exit_type", "")).upper()

            if (now - ts) < expiry_sec and exit_price > 0 and current_price > 0:
                gap_pct = (current_price - exit_price) / exit_price
                if "TIME" in exit_type:
                    if abs(gap_pct) < min_gap_pct:
                        return (
                            False,
                            f"직전 타임스탑 청산가({exit_price:,.2f}원) 대비 박스권 횡보 구간(현재 {current_price:,.2f}원, 갭 {gap_pct*100:+.2f}%)으로 휩쏘 재진입 방지",
                        )
                elif "STOP" in exit_type and "TIME" not in exit_type:
                    if gap_pct < min_gap_pct:
                        return (
                            False,
                            f"직전 손절가({exit_price:,.2f}원) 대비 유의미한 상방 돌파(+{min_gap_pct*100:.1f}%) 미도달(현재 {current_price:,.2f}원, 갭 {gap_pct*100:+.2f}%)",
                        )
                elif "TRAILING" in exit_type or "TP" in exit_type:
                    if 0.0 <= gap_pct < min_gap_pct:
                        return (
                            False,
                            f"직전 트레일링 익절가({exit_price:,.2f}원) 인근 휩쏘 고점 재진입 방지(현재 {current_price:,.2f}원, 갭 {gap_pct*100:+.2f}%)",
                        )

            if now - ts >= expiry_sec:
                del self._records[m_key]
                self._save()

        return True, "OK"



def calculate_risk_position_size(
    total_equity: float,
    entry_price: float,
    stop_loss: float,
    risk_fraction: float = 0.01,
    fee_rate: float = 0.0004,
    slippage_rate: float = 0.001,
    max_position_pct: float = 0.35,
    min_order_krw: float = 5000.0,
    available_krw: float | None = None,
    open_slots: int = 3,
    risk_scale_factor: float = 1.0,
) -> float:
    """Calculate position size in KRW such that maximum loss is fixed at risk_fraction with dynamic slot budget & capital de-scaling."""
    if total_equity <= 0 or entry_price <= 0:
        return 0.0

    scale = max(0.1, min(float(risk_scale_factor), 1.0))
    risk_capital = total_equity * risk_fraction * scale
    stop_dist_pct = abs(entry_price - stop_loss) / entry_price if entry_price > 0 else 0.02
    friction = (2.0 * fee_rate) + slippage_rate
    effective_loss_pct = max(0.008, stop_dist_pct + friction)

    raw_position_krw = risk_capital / effective_loss_pct
    slot_budget = (total_equity / max(1, open_slots)) * scale
    max_allowed_krw = min(total_equity * max_position_pct * scale, slot_budget)

    if available_krw is not None and available_krw > 0:
        max_allowed_krw = min(max_allowed_krw, available_krw)

    final_krw = min(raw_position_krw, max_allowed_krw)
    return round(final_krw, 2) if final_krw >= min_order_krw else 0.0


# Public compatibility names.  New code imports risk_controls directly; this
# facade avoids a breaking migration for existing callers.
RiskGuard = _RiskGuard
get_dynamic_portfolio_tiers = _get_dynamic_portfolio_tiers
calculate_risk_position_size = _calculate_risk_position_size

