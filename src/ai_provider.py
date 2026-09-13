"""빗썸/업비트 AI 통신을 분석 로직에서 분리하는 Provider 경계입니다."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import requests

from gemini_telemetry import (
    GeminiTelemetry,
    quota_bucket_for_model,
    quota_limit_for_bucket,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderResult:
    """키·원문 응답을 보관하지 않는 Provider 호출 결과입니다."""

    value: dict[str, Any] | list[Any] | str | None
    model: str
    error_kind: str = ""
    # 오류를 안전 상태와 연결하기 위한 HTTP 상태이며 응답 본문은 보관하지 않는다.
    status_code: int | None = None
    # 외부 응답의 코드/타입만 허용하며 메시지 원문은 저장하지 않는다.
    error_code: str = ""


class AIProvider(Protocol):
    """분석기가 의존하는 최소 AI 호출 계약입니다."""

    name: str
    exchange: str
    is_entry_fail_closed: bool

    def models_for(self, purpose: str) -> list[str]: ...

    def complete_json(
        self, prompt: str, models: list[str], schema: dict[str, Any], *, context: str, timeout: float, max_tokens: int,
        schema_name: str = "bithumb_trading_result", strict: bool = True,
    ) -> ProviderResult: ...

    def complete_text(
        self, prompt: str, models: list[str], *, context: str, timeout: float, max_tokens: int
    ) -> ProviderResult: ...


class AIProviderTelemetry:
    """Provider·모델·거래소별 호출량과 Provider가 반환한 쿼터 리셋 시각을 영속화합니다."""

    _lock = threading.RLock()
    _stats: dict[tuple[str, str, str], dict[str, Any]] = {}
    # 마지막 디스크 동기화 기준값으로, 다른 프로세스의 호출을 중복 합산하지 않는다.
    _persisted_stats: dict[tuple[str, str, str], dict[str, Any]] = {}
    _storage_path = ""
    _configured = False
    _current_date = ""
    _reset_at = 0.0
    _reset_remaining_raw = ""
    _reset_header_at = 0.0
    _force_new_window = False
    # 빗썸 FAST 분석 실패는 모든 신규 진입 경로를 닫아야 하므로 호출량과 별도로 영속한다.
    _entry_safety: dict[str, dict[str, Any]] = {}

    @classmethod
    def _today_kst(cls) -> str:
        """대시보드 기록 날짜 표기는 운영 기준인 한국 시간을 사용한다."""
        return datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d")

    @classmethod
    def configure(cls, data_dir: str | None = None, storage_filename: str = "gemini_bithumb_telemetry.json") -> None:
        """빗썸 전용 데이터 경로를 바인딩하고 Provider 사용량·안전 상태를 복원한다."""
        with cls._lock:
            if data_dir:
                cls._storage_path = os.path.join(data_dir, storage_filename)
            else:
                project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
                cls._storage_path = os.path.join(project_root, "data", "gemini_bithumb_telemetry.json")
            cls._configured = True
            cls._current_date = cls._today_kst()
            cls._reset_at = 0.0
            cls._reset_remaining_raw = ""
            cls._reset_header_at = 0.0
            cls._force_new_window = False
            cls._entry_safety = {}
            # 재시작 복원과 테스트 격리에서 이전 메모리 값을 섞지 않는다.
            cls._stats = {}
            cls._persisted_stats = {}
            cls._load_state_locked()
            cls._persisted_stats = {
                key: dict(value) for key, value in cls._stats.items()
            }

    @staticmethod
    def _normalize_entry_safety(value: Any) -> dict[str, Any]:
        """대시보드와 주문 게이트에 노출할 안전한 오류 메타데이터만 복원한다."""
        source = value if isinstance(value, dict) else {}
        return {
            "entry_blocked": bool(source.get("entry_blocked", False)),
            "status": str(source.get("status", "NORMAL"))[:32] or "NORMAL",
            "reason": str(source.get("reason", ""))[:120],
            "context": str(source.get("context", ""))[:80],
            "model": str(source.get("model", ""))[:120],
            "http_status": source.get("http_status") if isinstance(source.get("http_status"), int) else None,
            "error_code": str(source.get("error_code", ""))[:80],
            "updated_at": max(0.0, float(source.get("updated_at", 0.0) or 0.0)),
        }

    @classmethod
    def _ensure_configured_locked(cls) -> None:
        """직접 호출 경로도 기본 빗썸 저장소를 사용하도록 보장한다."""
        if not cls._configured:
            cls.configure()

    @classmethod
    def _load_state_locked(cls) -> None:
        """Provider가 알려 준 쿼터 창 또는 Gemini PT 자정 기준 기록을 복원한다."""
        if not cls._storage_path or not os.path.exists(cls._storage_path):
            return
        try:
            with open(cls._storage_path, "r", encoding="utf-8") as file:
                payload = json.load(file)
            restored: dict[tuple[str, str, str], dict[str, Any]] = {}
            for item in payload.get("stats", []):
                if not isinstance(item, dict):
                    continue
                provider = str(item.get("provider", "")).strip()
                exchange = str(item.get("exchange", "")).strip()
                model = str(item.get("model", "")).strip()
                if not provider or not exchange or not model:
                    continue
                restored[(provider, exchange, model)] = {
                    "calls": max(0, int(item.get("calls", 0))),
                    "success": max(0, int(item.get("success", 0))),
                    "rate_limited": max(0, int(item.get("rate_limited", 0))),
                    "errors": max(0, int(item.get("errors", 0))),
                    "cache_hits": max(0, int(item.get("cache_hits", 0))),
                    "latency_total_ms": max(0.0, float(item.get("latency_total_ms", 0.0))),
                    "last_event": str(item.get("last_event", ""))[:200],
                    "last_event_at": max(0.0, float(item.get("last_event_at", 0.0))),
                }
            cls._stats = restored
            entry_safety = payload.get("entry_safety", {})
            if isinstance(entry_safety, dict):
                cls._entry_safety = {
                    str(exchange): cls._normalize_entry_safety(state)
                    for exchange, state in entry_safety.items()
                    if str(exchange).strip()
                }
            # 이전 버전이 남긴 최신 4xx/5xx도 재시작 직후 신규 BUY를 열지 않도록 보수적으로 승격한다.
            if "bithumb" not in cls._entry_safety:
                latest = max(restored.values(), key=lambda item: float(item.get("last_event_at", 0.0)), default={})
                last_event = str(latest.get("last_event", ""))
                matched = re.search(r"HTTP (4\d\d|5\d\d)", last_event)
                if matched:
                    cls._entry_safety["bithumb"] = cls._normalize_entry_safety({
                        "entry_blocked": True, "status": "BLOCKED", "reason": f"legacy_http_{matched.group(1)}",
                        "context": last_event.split(" HTTP ", 1)[0], "http_status": int(matched.group(1)),
                        "updated_at": float(latest.get("last_event_at", 0.0)),
                    })
            cls._reset_at = max(0.0, float(payload.get("reset_at", 0.0)))
            cls._reset_remaining_raw = str(payload.get("reset_remaining_raw", ""))[:64]
            cls._reset_header_at = max(0.0, float(payload.get("reset_header_at", 0.0)))
            cls._check_header_reset_locked()
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            # 계측 파일 손상은 주문 흐름에 영향을 주지 않고 빈 관측값으로 안전하게 시작한다.
            cls._stats = {}

    @classmethod
    def _save_state_locked(cls) -> None:
        """프로세스 간 누적값을 병합해 재시작·동시 실행에도 호출량을 보존한다."""
        if not cls._storage_path:
            return
        try:
            os.makedirs(os.path.dirname(cls._storage_path), exist_ok=True)
            with cls._storage_file_lock_locked():
                disk_stats, disk_payload = ({}, {}) if cls._force_new_window else cls._read_disk_stats_locked()
                merged = cls._merge_new_stats_locked(disk_stats)
                disk_entry_safety = disk_payload.get("entry_safety", {}) if isinstance(disk_payload, dict) else {}
                merged_entry_safety = {
                    str(exchange): cls._normalize_entry_safety(state)
                    for exchange, state in disk_entry_safety.items()
                    if str(exchange).strip()
                }
                for exchange, state in cls._entry_safety.items():
                    current = merged_entry_safety.get(exchange, {})
                    if float(state.get("updated_at", 0.0)) >= float(current.get("updated_at", 0.0)):
                        merged_entry_safety[exchange] = cls._normalize_entry_safety(state)
                reset_header_at = max(cls._reset_header_at, float(disk_payload.get("reset_header_at", 0.0) or 0.0))
                if reset_header_at == cls._reset_header_at:
                    reset_at = cls._reset_at
                    reset_raw = cls._reset_remaining_raw
                else:
                    reset_at = max(0.0, float(disk_payload.get("reset_at", 0.0) or 0.0))
                    reset_raw = str(disk_payload.get("reset_remaining_raw", ""))[:64]
                stats = [
                    {
                        "provider": provider, "exchange": exchange, "model": model,
                        "calls": stat["calls"], "success": stat["success"],
                        "rate_limited": stat["rate_limited"], "errors": stat["errors"],
                        "cache_hits": stat.get("cache_hits", 0),
                        "latency_total_ms": stat["latency_total_ms"],
                        "last_event": stat["last_event"], "last_event_at": stat["last_event_at"],
                    }
                    for (provider, exchange, model), stat in merged.items()
                ]
                temporary_path = f"{cls._storage_path}.tmp"
                with open(temporary_path, "w", encoding="utf-8") as file:
                    json.dump({
                        "date": cls._current_date,
                        "reset_at": reset_at,
                        "reset_remaining_raw": reset_raw,
                        "reset_header_at": reset_header_at,
                        "entry_safety": merged_entry_safety,
                        "stats": stats,
                    }, file, ensure_ascii=False, indent=2)
                os.replace(temporary_path, cls._storage_path)
                cls._stats = merged
                cls._persisted_stats = {key: dict(value) for key, value in merged.items()}
                cls._reset_at = reset_at
                cls._reset_remaining_raw = reset_raw
                cls._reset_header_at = reset_header_at
                cls._entry_safety = merged_entry_safety
                cls._force_new_window = False
        except OSError:
            # 사용량 관측 저장 실패는 AI 호출이나 거래 주문을 막지 않는다.
            logger.debug("AI Provider 텔레메트리 저장 실패: %s", cls._storage_path, exc_info=True)

    @classmethod
    @contextmanager
    def _storage_file_lock_locked(cls):
        """Windows와 POSIX에서 계측 파일의 읽기·병합·교체 구간을 하나로 잠근다."""
        lock_path = f"{cls._storage_path}.lock"
        with open(lock_path, "a+b") as lock_file:
            lock_file.seek(0)
            lock_file.write(b"0")
            lock_file.flush()
            lock_file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @classmethod
    def _read_disk_stats_locked(cls) -> tuple[dict[tuple[str, str, str], dict[str, Any]], dict[str, Any]]:
        """잠금 안에서 최신 파일을 읽어 다른 프로세스의 직전 저장값을 기준으로 삼는다."""
        try:
            with open(cls._storage_path, "r", encoding="utf-8") as file:
                payload = json.load(file)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}, {}
        stats: dict[tuple[str, str, str], dict[str, Any]] = {}
        for item in payload.get("stats", []):
            if not isinstance(item, dict):
                continue
            key = tuple(str(item.get(name, "")).strip() for name in ("provider", "exchange", "model"))
            if not all(key):
                continue
            stats[key] = {
                "calls": max(0, int(item.get("calls", 0))), "success": max(0, int(item.get("success", 0))),
                "rate_limited": max(0, int(item.get("rate_limited", 0))), "errors": max(0, int(item.get("errors", 0))),
                "cache_hits": max(0, int(item.get("cache_hits", 0))),
                "latency_total_ms": max(0.0, float(item.get("latency_total_ms", 0.0))),
                "last_event": str(item.get("last_event", ""))[:200],
                "last_event_at": max(0.0, float(item.get("last_event_at", 0.0))),
            }
        return stats, payload

    @classmethod
    def _merge_new_stats_locked(cls, disk_stats: dict[tuple[str, str, str], dict[str, Any]]) -> dict[tuple[str, str, str], dict[str, Any]]:
        """현재 프로세스가 마지막 동기화 뒤 추가한 값만 최신 디스크 통계에 더한다."""
        merged = {key: dict(value) for key, value in disk_stats.items()}
        numeric_fields = ("calls", "success", "rate_limited", "errors", "cache_hits", "latency_total_ms")
        for key, stat in cls._stats.items():
            current = merged.setdefault(key, {"calls": 0, "success": 0, "rate_limited": 0, "errors": 0, "cache_hits": 0, "latency_total_ms": 0.0, "last_event": "", "last_event_at": 0.0})
            baseline = cls._persisted_stats.get(key, {})
            for field in numeric_fields:
                delta = float(stat.get(field, 0.0)) - float(baseline.get(field, 0.0))
                current[field] += max(0.0, delta)
            if float(stat.get("last_event_at", 0.0)) >= float(current.get("last_event_at", 0.0)):
                current["last_event"] = str(stat.get("last_event", ""))[:200]
                current["last_event_at"] = max(0.0, float(stat.get("last_event_at", 0.0)))
            for field in ("calls", "success", "rate_limited", "errors", "cache_hits"):
                current[field] = int(round(current[field]))
        return merged

    @classmethod
    def _check_header_reset_locked(cls) -> None:
        """Provider 쿼터 창 또는 Gemini PT 자정이 지난 경우 누적 관측값을 초기화한다."""
        if cls._reset_at > 0.0 and time.time() >= cls._reset_at:
            cls._current_date = cls._today_kst()
            cls._stats = {}
            cls._reset_at = 0.0
            cls._reset_remaining_raw = ""
            cls._reset_header_at = 0.0
            # 새 쿼터 창에서는 이전 창의 디스크 통계를 다시 합산하지 않는다.
            cls._persisted_stats = {}
            cls._force_new_window = True
            cls._save_state_locked()

    @staticmethod
    def _parse_reset_seconds(value: str) -> float | None:
        """API 헤더의 `1h2m3.45s` 형식을 초 단위로 보수적으로 변환한다."""
        text = (value or "").strip().lower()
        if not text:
            return None
        matches = re.findall(r"(\d+(?:\.\d+)?)([dhms])", text)
        if not matches or "".join(f"{amount}{unit}" for amount, unit in matches) != text:
            return None
        multipliers = {"d": 86400.0, "h": 3600.0, "m": 60.0, "s": 1.0}
        return sum(float(amount) * multipliers[unit] for amount, unit in matches)

    @classmethod
    def reset(cls, persist: bool = False) -> None:
        """단위 테스트 전용 초기화이며 운영 호출 경로에서는 사용하지 않는다."""
        with cls._lock:
            cls._ensure_configured_locked()
            cls._stats = {}
            cls._entry_safety = {}
            if persist:
                # 테스트 전용 영구 초기화는 이전 창의 통계를 다시 병합하지 않아야 한다.
                cls._persisted_stats = {}
                cls._force_new_window = True
                cls._save_state_locked()

    @classmethod
    def record(
        cls, provider: str, exchange: str, model: str, context: str, status_code: int | None,
        latency_ms: float, error_kind: str = "", reset_requests: str = "",
    ) -> None:
        """응답 본문이나 인증 정보 없이 호출 결과만 계측합니다."""
        key = (provider, exchange, model)
        with cls._lock:
            cls._ensure_configured_locked()
            cls._check_header_reset_locked()
            stat = cls._stats.setdefault(key, {
                "calls": 0, "success": 0, "rate_limited": 0, "errors": 0,
                "cache_hits": 0,
                "latency_total_ms": 0.0, "last_event": "", "last_event_at": 0.0,
            })
            stat["calls"] += 1
            stat["latency_total_ms"] += max(0.0, latency_ms)
            if status_code == 200:
                stat["success"] += 1
            elif status_code == 429:
                stat["rate_limited"] += 1
            else:
                stat["errors"] += 1
            status_label = str(status_code) if status_code is not None else (error_kind or "error")
            stat["last_event"] = f"{context} {model} HTTP {status_label}"
            stat["last_event_at"] = time.time()
            reset_seconds = cls._parse_reset_seconds(reset_requests)
            if reset_seconds is not None:
                # 서버가 반환한 잔여 시간에서 계산하므로 KST/UTC 추정 리셋을 사용하지 않는다.
                cls._reset_at = time.time() + reset_seconds
                cls._reset_remaining_raw = reset_requests[:64]
                cls._reset_header_at = time.time()
            elif provider == "gemini" and exchange == "bithumb":
                # Gemini 무료 쿼터는 PT 날짜를 기준으로 관리하므로, 응답 헤더 추측 없이 다음 PT 자정을 표시한다.
                now_pt = datetime.now(ZoneInfo("America/Los_Angeles"))
                next_pt = now_pt.replace(hour=0, minute=0, second=0, microsecond=0)
                if next_pt <= now_pt:
                    from datetime import timedelta
                    next_pt += timedelta(days=1)
                cls._reset_at = next_pt.timestamp()
                cls._reset_remaining_raw = "PT_MIDNIGHT"
                cls._reset_header_at = time.time()
            cls._save_state_locked()

    @classmethod
    def record_cache_hit(cls, provider: str, exchange: str, model: str, context: str) -> None:
        """외부 호출 없이 재사용한 모델 목록 캐시도 거래소별로 분리 계측한다."""
        key = (provider, exchange, model)
        with cls._lock:
            cls._ensure_configured_locked()
            stat = cls._stats.setdefault(key, {"calls": 0, "success": 0, "rate_limited": 0, "errors": 0,
                                               "cache_hits": 0, "latency_total_ms": 0.0, "last_event": "", "last_event_at": 0.0})
            stat["cache_hits"] += 1
            stat["last_event"] = f"{context} {model} CACHE"
            stat["last_event_at"] = time.time()
            cls._save_state_locked()

    @classmethod
    def record_entry_safety(
        cls, exchange: str, *, blocked: bool, reason: str = "", context: str = "",
        model: str = "", status_code: int | None = None, error_code: str = "",
    ) -> None:
        """FAST 분석 결과를 신규 BUY 공통 차단 상태로 원자 저장한다."""
        with cls._lock:
            cls._ensure_configured_locked()
            cls._entry_safety[exchange] = cls._normalize_entry_safety({
                "entry_blocked": blocked,
                "status": "BLOCKED" if blocked else "NORMAL",
                "reason": reason if blocked else "",
                "context": context,
                "model": model,
                "http_status": status_code,
                "error_code": error_code,
                "updated_at": time.time(),
            })
            cls._save_state_locked()

    @classmethod
    def get_entry_block_reason(cls, exchange: str, max_age_sec: float | None = None) -> str:
        """기존 포지션에는 영향 없이 모든 신규 BUY 경로가 공통으로 읽는 차단 사유다."""
        with cls._lock:
            cls._ensure_configured_locked()
            state = cls._entry_safety.get(exchange, {})
            if not state.get("entry_blocked"):
                return ""
            if max_age_sec is not None and max_age_sec > 0:
                blocked_at = float(state.get("updated_at", 0.0) or state.get("timestamp", 0.0) or 0.0)
                if (time.time() - blocked_at) >= max_age_sec:
                    state["entry_blocked"] = False
                    state["status"] = "NORMAL"
                    state["reason"] = ""
                    cls._save_state_locked()
                    return ""
            reason = str(state.get("reason", "provider_failure"))
            context = str(state.get("context", ""))
            exchange_label = "업비트" if str(exchange).lower() == "upbit" else "빗썸"
            return f"{exchange_label} Gemini 분석 장애({reason}{f', {context}' if context else ''})로 신규 BUY를 차단합니다."

    @classmethod
    def snapshot(cls, exchange: str) -> dict[str, Any]:
        """대시보드 확장용 읽기 전용 집계를 반환합니다."""
        with cls._lock:
            cls._ensure_configured_locked()
            cls._check_header_reset_locked()
            models: dict[str, dict[str, Any]] = {}
            total = {"api_calls": 0, "api_success": 0, "rate_limited": 0, "http_errors": 0, "cache_hits": 0, "latency_total_ms": 0.0}
            providers: set[str] = set()
            last_event = ""
            last_event_at = 0.0
            for (provider, stat_exchange, model), stat in list(cls._stats.items()):
                if stat_exchange != exchange:
                    continue
                if model.endswith("-latest") or "latest" in model.lower():
                    cls._stats.pop((provider, stat_exchange, model), None)
                    continue
                providers.add(provider)
                m_calls = int(stat["calls"])
                bucket = quota_bucket_for_model(model)
                m_limit = quota_limit_for_bucket(bucket) if model != "list_models" else 0
                m_used_pct = round((m_calls / m_limit) * 100.0, 1) if m_limit > 0 else 0.0
                models[model] = {
                    "provider": provider, "calls": m_calls, "success": int(stat["success"]),
                    "rate_limited": int(stat["rate_limited"]), "errors": int(stat["errors"]),
                    "cache_hits": int(stat.get("cache_hits", 0)),
                    "avg_latency_ms": round(float(stat["latency_total_ms"]) / m_calls, 1) if m_calls else 0.0,
                    "quota_bucket": bucket,
                    "quota_limit": m_limit,
                    "quota_used_pct": m_used_pct,
                }
                total["api_calls"] += m_calls
                total["api_success"] += int(stat["success"])
                total["rate_limited"] += int(stat["rate_limited"])
                total["http_errors"] += int(stat["errors"])
                total["cache_hits"] += int(stat.get("cache_hits", 0))
                total["latency_total_ms"] += float(stat["latency_total_ms"])
                if float(stat["last_event_at"]) >= last_event_at:
                    last_event_at = float(stat["last_event_at"])
                    last_event = str(stat["last_event"])
            calls = total["api_calls"]
            remaining_seconds = max(0, int(round(cls._reset_at - time.time()))) if cls._reset_at else 0
            is_gemini = "gemini" in providers or exchange == "bithumb"
            reset_time_kst = (
                datetime.fromtimestamp(cls._reset_at, ZoneInfo("Asia/Seoul")).strftime("%H:%M KST" if is_gemini else "%Y-%m-%d %H:%M:%S KST")
                if cls._reset_at else ""
            )
            hours = remaining_seconds // 3600
            minutes = (remaining_seconds % 3600) // 60
            remaining_str = f"{hours}시간 {minutes:02d}분 후 리셋" if remaining_seconds > 0 else ""
            quota_limit = 1000 if is_gemini else 0
            quota_used_pct = round((calls / quota_limit) * 100.0, 1) if quota_limit > 0 else 0.0

            return {
                "date": cls._current_date,
                "provider": ",".join(sorted(providers)) or "gemini", "exchange": exchange,
                "api_calls": calls, "api_success": total["api_success"], "rate_limited": total["rate_limited"],
                "http_errors": total["http_errors"], "avg_latency_ms": round(total["latency_total_ms"] / calls, 1) if calls else 0.0,
                "local_fallback": 0,
                "cache_hits": total["cache_hits"],
                "quota_limit": quota_limit,
                "quota_used_pct": quota_used_pct,
                "models": models,
                "models_by_id": dict(models),
                "last_event": last_event, "last_event_at": last_event_at,
                "reset_info": {
                    "source": ("pt_midnight" if cls._reset_remaining_raw == "PT_MIDNIGHT" else "x-ratelimit-reset-requests") if cls._reset_at else "",
                    "raw": cls._reset_remaining_raw,
                    "reset_at": cls._reset_at,
                    "reset_time_kst": reset_time_kst,
                    "remaining_seconds": remaining_seconds,
                    "remaining_str": remaining_str,
                },
                "entry_safety": cls._normalize_entry_safety(cls._entry_safety.get(exchange, {})),
            }


def _parse_json_text(raw: str) -> dict[str, Any] | list[Any] | None:
    """Provider 공통으로 마크다운 없는 JSON만 보수적으로 수용합니다."""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, (dict, list)) else None


def _validate_schema(value: Any, schema: dict[str, Any]) -> bool:
    """Provider 스키마 응답의 핵심 타입·필수 필드를 로컬에서 재검증합니다."""
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            return False
        if any(key not in value for key in schema.get("required", [])):
            return False
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False and any(key not in properties for key in value):
            return False
        return all(_validate_schema(value[key], properties[key]) for key in value if key in properties)
    if expected == "array":
        return isinstance(value, list) and all(_validate_schema(item, schema.get("items", {})) for item in value)
    if expected == "string":
        return isinstance(value, str) and (not schema.get("enum") or value in schema["enum"])
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    return True


class GeminiProvider:
    """업비트 Gemini 장애 시 신규 BUY를 닫는 Provider입니다."""

    name = "gemini"
    exchange = "upbit"
    # AI 확인이 필수인 신규 진입에서 통신 실패를 로컬 BUY 승인으로 대체하지 않는다.
    is_entry_fail_closed = True

    def __init__(self, api_key: str):
        self.api_key = (api_key or "").strip()

    def models_for(self, purpose: str) -> list[str]:
        """기존 동적 모델 라우터가 후보를 전달하므로 여기서는 선택하지 않습니다."""
        return []

    @staticmethod
    def _extract_text(payload: dict[str, Any]) -> str | None:
        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            return None
        parts = candidates[0].get("content", {}).get("parts", []) if isinstance(candidates[0], dict) else []
        if not isinstance(parts, list) or not parts or not isinstance(parts[0], dict):
            return None
        text = parts[0].get("text")
        return str(text).strip() if text else None

    def complete_json(
        self, prompt: str, models: list[str], schema: dict[str, Any], *, context: str, timeout: float, max_tokens: int,
        schema_name: str = "bithumb_trading_result", strict: bool = True,
    ) -> ProviderResult:
        """정상 JSON 스키마 응답만 신규 BUY 차단을 해제하며, 실패 시 다음 가용 모델로 순차 폴백한다."""
        last_error = "no_model"
        last_model = ""
        last_status_code: int | None = None
        is_entry_context = context not in ("macro_regime", "market_briefing") and "briefing" not in context
        for model in models:
            last_model = model
            try:
                # 추론 모델(gemini-3.7-flash 등)의 불필요한 Thinking 토큰 소모 및 타임아웃 방지
                generation_config: dict[str, Any] = {
                    "temperature": 0.1, "topP": 0.8, "maxOutputTokens": max_tokens, "responseMimeType": "application/json",
                }
                if any(marker in model.lower() for marker in ("3.7", "thinking", "2.5")):
                    generation_config["thinkingConfig"] = {"thinkingBudget": 0}
                payload = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": generation_config}
                response = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.api_key}",
                    json=payload, timeout=timeout,
                )
                last_status_code = response.status_code
                GeminiTelemetry.record_http_attempt(model, context, "generate_content", response.status_code)
                if response.status_code == 400 and "thinkingConfig" in generation_config:
                    retry_config = dict(generation_config)
                    retry_config.pop("thinkingConfig", None)
                    payload["generationConfig"] = retry_config
                    response = requests.post(
                        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.api_key}",
                        json=payload, timeout=timeout,
                    )
                    last_status_code = response.status_code
                    GeminiTelemetry.record_http_attempt(model, context, "generate_content", response.status_code)
                if response.status_code == 200:
                    value = _parse_json_text(self._extract_text(response.json()) or "")
                    if value is not None and _validate_schema(value, schema):
                        if is_entry_context:
                            AIProviderTelemetry.record_entry_safety(
                                self.exchange, blocked=False, context=context, model=model, status_code=response.status_code,
                            )
                        return ProviderResult(value, model)
                    last_error = "invalid_json" if value is None else "schema"
                    continue
                last_error = "rate_limited" if response.status_code == 429 else "http_error"
                continue
            except requests.exceptions.Timeout:
                GeminiTelemetry.record_http_attempt(model, context, "generate_content", None, "timeout")
                last_error = "timeout"
                continue
            except (requests.exceptions.RequestException, ValueError, KeyError, IndexError):
                GeminiTelemetry.record_http_attempt(model, context, "generate_content", None, "exception")
                last_error = "exception"
                continue

        # 거시 레짐/브리핑 등 보조 분석은 로컬 규칙으로 방어하되, 직접 진입 분석 실패 시에만 신규 BUY를 차단한다.
        if is_entry_context:
            AIProviderTelemetry.record_entry_safety(
                self.exchange, blocked=True, reason=last_error, context=context, model=last_model,
                status_code=last_status_code,
            )
        return ProviderResult(None, last_model, last_error)

    def complete_text(self, prompt: str, models: list[str], *, context: str, timeout: float, max_tokens: int) -> ProviderResult:
        """업비트 브리핑 호환 경로입니다."""
        last_error = "no_model"
        for model in models:
            try:
                # 기존 Gemini 추론 모델의 Thinking 토큰 소모 방지와 400 재시도 계약을 보존한다.
                generation_config: dict[str, Any] = {"temperature": 0.2, "maxOutputTokens": max_tokens}
                if any(marker in model.lower() for marker in ("3.7", "thinking", "2.5")):
                    generation_config["thinkingConfig"] = {"thinkingBudget": 0}
                payload = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": generation_config}
                response = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.api_key}",
                    json=payload,
                    timeout=timeout,
                )
                GeminiTelemetry.record_http_attempt(model, context, "generate_content", response.status_code)
                if response.status_code == 400 and "thinkingConfig" in generation_config:
                    retry_config = dict(generation_config)
                    retry_config.pop("thinkingConfig", None)
                    payload["generationConfig"] = retry_config
                    response = requests.post(
                        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.api_key}",
                        json=payload, timeout=timeout,
                    )
                    GeminiTelemetry.record_http_attempt(model, f"{context}_retry", "generate_content", response.status_code)
                if response.status_code == 200:
                    response_data = response.json()
                    candidates = response_data.get("candidates", [])
                    if candidates and isinstance(candidates[0], dict) and candidates[0].get("finishReason") == "MAX_TOKENS":
                        last_error = "truncated"
                        continue
                    text = self._extract_text(response_data)
                    if text:
                        return ProviderResult(text, model)
                    last_error = "invalid_response"
                    continue
                last_error = "rate_limited" if response.status_code == 429 else "http_error"
            except requests.exceptions.Timeout:
                GeminiTelemetry.record_http_attempt(model, context, "generate_content", None, "timeout")
                last_error = "timeout"
            except (requests.exceptions.RequestException, ValueError, KeyError, IndexError):
                GeminiTelemetry.record_http_attempt(model, context, "generate_content", None, "exception")
                last_error = "exception"
        return ProviderResult(None, "", last_error)


class BithumbGeminiProvider:
    """빗썸 전용 Gemini 경계이며 업비트 키·텔레메트리와 절대로 공유하지 않습니다."""

    name = "gemini"
    exchange = "bithumb"
    is_entry_fail_closed = True
    # 신규 BUY 진입은 Flash-Lite 계열(3.5 우선 후 3.1 순차 폴백)만 허용해 안정성과 복원력을 유지한다.
    TRADING_MODELS = [
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
    ]
    TRADING_MODEL = TRADING_MODELS[0]
    MACRO_FALLBACK_MODELS = [
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
    ]
    BRIEFING_FALLBACK_MODELS = [
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
    ]
    # Flash-Lite만 탐색해 고비용 모델 승격과 암묵적 모델 폴백을 금지한다.
    SYSTEM_INSTRUCTION = """당신은 빗썸 전용 Gemini AI 분석 보조자입니다.
빗썸에서 제공한 데이터만 사용하고 업비트·다른 거래소 데이터, API 키, 계좌, 주문 상태를 절대로 혼합하지 마세요. 제공된 수치 외에는 추측하지 마세요.
ACK는 체결이 아닙니다. REST 또는 Private WebSocket의 확정 체결 전에는 포지션·손익·쿨다운·주문 완료를 단정하지 마세요.
불확실하거나 데이터가 누락·모순되면 BUY가 아닌 HOLD를 선택하세요. 주문 실행·취소·체결 확정 권한은 없습니다.
현재 레짐, 후보 경로(SCALP, SWING, MOMENTUM_BREAKOUT, RECOVERY_REBOUND, NEW_LISTING), RS 주도주(RS >= +3.0%) 특례 규정, 신규상장 정책과 안전 차단 조건을 준수하세요. MOMENTUM_BREAKOUT의 EXTENDED는 확정 5분봉 고점 돌파·양봉·거래량·RSI·1시간 EMA20을 모두 통과한 경우에만 주간 알파 55점/심야 65점 이상으로 제한 추격 BUY를 검토하며, 최초 비중은 최대 종목 비중의 15%를 넘지 않습니다. 알트코인은 비트코인 급락(CRASH)이 아닌 이상 비트코인의 추세(조정·약세·횡보)에 영향없이 자체 기술적 지표와 수급이 양호하면 독립적으로 매수를 승인하되, 비트코인 대폭락(CRASH)에서는 신규 진입을 제안하지 마세요. 스크리너 단계 신규상장 사전 필터로 `classify_listing_maturity()` + `is_new_listing_eligible()` SSOT에 따라 자격 미충족 NEW_LISTING 후보는 AI 입력 전에 제외됩니다.
API 키, 시크릿, 토큰, 계좌 또는 주문 식별자를 요구·출력·재현하지 마세요. JSON 요청에는 마크다운 없는 유효 JSON만 반환하고, 모든 설명 텍스트는 자연스러운 한국어로만 작성하세요."""

    def __init__(self, api_key: str):
        # 호출자에서 전달받은 전용 키만 보관하며 환경 변수 fallback을 의도적으로 두지 않는다.
        self.api_key = (api_key or "").strip()
        self._models: list[str] | None = None
        self._macro_models: list[str] | None = None
        self._briefing_models: list[str] | None = None

    @property
    def is_configured(self) -> bool:
        """빗썸 Gemini 전용 키가 있을 때만 모델 탐색과 분석을 허용한다."""
        return bool(self.api_key)

    @staticmethod
    def _safe_error_code(response: requests.Response) -> str:
        """외부 오류에서 식별자 형식 코드만 추출해 원문 노출을 막는다."""
        try:
            payload = response.json()
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            return re.sub(r"[^A-Za-z0-9_.:-]", "", str(error.get("status") or error.get("code") or ""))[:80]
        except (ValueError, TypeError, AttributeError):
            return ""

    def _record_entry_safety(self, result: ProviderResult, context: str) -> ProviderResult:
        """정상 JSON 스키마 검증을 마친 호출만 신규 BUY 차단을 해제한다. 보조 분석은 신규 BUY를 차단하지 않는다."""
        is_entry_context = context not in ("macro_regime", "market_briefing") and "briefing" not in context
        if is_entry_context:
            AIProviderTelemetry.record_entry_safety(
                self.exchange, blocked=not isinstance(result.value, (dict, list)),
                reason=result.error_kind or "invalid_response", context=context,
                model=result.model, status_code=result.status_code, error_code=result.error_code,
            )
        return result

    def _discover_models(self) -> list[str]:
        """업비트와 같은 구체 Flash-Lite 모델이 있을 때만 분석을 허용한다."""
        if not self.is_configured:
            self._record_entry_safety(ProviderResult(None, "", "configuration"), "list_models")
            return []
        started = time.monotonic()
        status_code: int | None = None
        error_kind = ""
        error_code = ""
        try:
            response = requests.get(
                f"https://generativelanguage.googleapis.com/v1beta/models?key={self.api_key}", timeout=10.0,
            )
            status_code = response.status_code
            if status_code != 200:
                error_kind = "rate_limited" if status_code == 429 else "http_error"
                error_code = self._safe_error_code(response)
                self._record_entry_safety(ProviderResult(None, "", error_kind, status_code, error_code), "list_models")
                return []
            payload = response.json()
            models = [
                str(item.get("name", "")).replace("models/", "").strip()
                for item in payload.get("models", []) if isinstance(item, dict)
                and "generateContent" in item.get("supportedGenerationMethods", [])
                and "flash-lite" in str(item.get("name", "")).lower()
            ]
            if not models:
                self._record_entry_safety(ProviderResult(None, "", "no_flash_lite", status_code), "list_models")
                return []
            available_trading = [m for m in self.TRADING_MODELS if m in models]
            if not available_trading:
                # 허용된 구체 Flash-Lite 모델이 전혀 없으면 신규 BUY를 차단한다.
                self._record_entry_safety(
                    ProviderResult(None, "", "required_model_unavailable", status_code), "list_models"
                )
                return []
            return available_trading
        except requests.exceptions.Timeout:
            error_kind = "timeout"
        except (requests.exceptions.RequestException, ValueError, TypeError, KeyError, IndexError):
            error_kind = "exception"
        finally:
            AIProviderTelemetry.record(self.name, self.exchange, "list_models", "list_models", status_code,
                                       (time.monotonic() - started) * 1000.0, error_kind)
        self._record_entry_safety(ProviderResult(None, "", error_kind or "exception", status_code, error_code), "list_models")
        return []

    def _discover_macro_models(self) -> list[str]:
        """빗썸 전용 키로 거시 진단용 Flash 모델을 탐색하고, 우선순위대로 반환한다."""
        if not self.is_configured:
            return []
        try:
            response = requests.get(
                f"https://generativelanguage.googleapis.com/v1beta/models?key={self.api_key}", timeout=10.0,
            )
            if response.status_code != 200:
                return [self.TRADING_MODEL]
            payload = response.json()
            available = [
                str(item.get("name", "")).replace("models/", "").strip()
                for item in payload.get("models", []) if isinstance(item, dict)
                and "generateContent" in item.get("supportedGenerationMethods", [])
                and "pro" not in str(item.get("name", "")).lower()
            ]
            candidates = [m for m in self.MACRO_FALLBACK_MODELS if m in available]
            if candidates:
                return candidates
            return [self.TRADING_MODEL]
        except Exception:
            return [self.TRADING_MODEL]

    def _discover_briefing_models(self) -> list[str]:
        """빗썸 전용 키로 브리핑용 Flash 모델을 탐색하고, 우선순위대로 반환한다."""
        if not self.is_configured:
            return []
        try:
            response = requests.get(
                f"https://generativelanguage.googleapis.com/v1beta/models?key={self.api_key}", timeout=10.0,
            )
            if response.status_code != 200:
                return list(self.BRIEFING_FALLBACK_MODELS)
            payload = response.json()
            available = [
                str(item.get("name", "")).replace("models/", "").strip()
                for item in payload.get("models", []) if isinstance(item, dict)
                and "generateContent" in item.get("supportedGenerationMethods", [])
                and "pro" not in str(item.get("name", "")).lower()
            ]
            candidates = [m for m in self.BRIEFING_FALLBACK_MODELS if m in available]
            if candidates:
                return candidates
            return list(self.BRIEFING_FALLBACK_MODELS)
        except Exception:
            return list(self.BRIEFING_FALLBACK_MODELS)

    def models_for(self, purpose: str) -> list[str]:
        """목적별 허용 모델을 반환하며, 신규 BUY(trading)는 Flash-Lite 계열 순차 폴백 목록을 반환한다."""
        if purpose == "macro":
            if self._macro_models is None:
                self._macro_models = self._discover_macro_models()
            return list(self._macro_models) if self._macro_models else [self.TRADING_MODEL]

        if purpose == "briefing":
            if self._briefing_models is None:
                self._briefing_models = self._discover_briefing_models()
            return list(self._briefing_models) if self._briefing_models else list(self.BRIEFING_FALLBACK_MODELS)

        if self._models is None:
            self._models = self._discover_models()
        elif self._models:
            # 프로세스 내 모델 목록 재사용도 빗썸 전용 텔레메트리에만 기록한다.
            AIProviderTelemetry.record_cache_hit(self.name, self.exchange, self._models[0], "list_models")
        return list(self._models) if self._models else []

    def complete_json(self, prompt: str, models: list[str], schema: dict[str, Any], *, context: str,
                      timeout: float, max_tokens: int, schema_name: str = "bithumb_trading_result",
                      strict: bool = True) -> ProviderResult:
        """Gemini generateContent 응답을 로컬 JSON 스키마 검증 뒤에만 정상 처리한다."""
        if not self.is_configured or not models:
            return self._record_entry_safety(ProviderResult(None, "", "configuration"), context)
        last_result = ProviderResult(None, models[0], "configuration")
        for model in models:
            started = time.monotonic()
            status_code: int | None = None
            error_kind = ""
            error_code = ""
            try:
                generation_config: dict[str, Any] = {
                    "temperature": 0.1, "topP": 0.8, "maxOutputTokens": max_tokens, "responseMimeType": "application/json",
                }
                if any(marker in model.lower() for marker in ("3.7", "thinking", "2.5")):
                    generation_config["thinkingConfig"] = {"thinkingBudget": 0}
                payload = {
                    "systemInstruction": {"parts": [{"text": self.SYSTEM_INSTRUCTION}]},
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": generation_config,
                }
                response = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.api_key}",
                    json=payload, timeout=timeout,
                )
                status_code = response.status_code
                if status_code == 400 and "thinkingConfig" in generation_config:
                    retry_config = dict(generation_config)
                    retry_config.pop("thinkingConfig", None)
                    payload["generationConfig"] = retry_config
                    response = requests.post(
                        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.api_key}",
                        json=payload, timeout=timeout,
                    )
                    status_code = response.status_code
                if status_code != 200:
                    error_kind = "rate_limited" if status_code == 429 else "http_error"
                    error_code = self._safe_error_code(response)
                    last_result = ProviderResult(None, model, error_kind, status_code, error_code)
                    continue
                value = _parse_json_text(GeminiProvider._extract_text(response.json()) or "")
                if value is None:
                    last_result = ProviderResult(None, model, "invalid_json", status_code)
                    continue
                if not _validate_schema(value, schema):
                    last_result = ProviderResult(None, model, "schema", status_code)
                    continue
                return self._record_entry_safety(ProviderResult(value, model, status_code=status_code), context)
            except requests.exceptions.Timeout:
                error_kind = "timeout"
                last_result = ProviderResult(None, model, error_kind, status_code, error_code)
                continue
            except (requests.exceptions.RequestException, ValueError, TypeError, KeyError, IndexError):
                error_kind = "exception"
                last_result = ProviderResult(None, model, error_kind, status_code, error_code)
                continue
            finally:
                AIProviderTelemetry.record(self.name, self.exchange, model or "unavailable", context, status_code,
                                           (time.monotonic() - started) * 1000.0, error_kind)
        return self._record_entry_safety(last_result, context)

    def complete_text(self, prompt: str, models: list[str], *, context: str, timeout: float, max_tokens: int) -> ProviderResult:
        """브리핑은 신규 BUY 게이트와 독립된 텍스트 보조 경로로만 제공하며 순차 폴백을 지원한다."""
        if not self.is_configured or not models:
            return ProviderResult(None, "", "configuration")
        last_error = "no_model"
        last_model = models[0]
        last_status_code: int | None = None
        last_error_code = ""

        for model in models:
            started = time.monotonic()
            status_code: int | None = None
            error_kind = ""
            error_code = ""
            last_model = model
            try:
                generation_config: dict[str, Any] = {
                    "temperature": 0.2,
                    "maxOutputTokens": max_tokens,
                }
                if any(marker in model.lower() for marker in ("3.7", "thinking", "2.5")):
                    generation_config["thinkingConfig"] = {"thinkingBudget": 0}
                payload = {
                    "systemInstruction": {"parts": [{"text": self.SYSTEM_INSTRUCTION}]},
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": generation_config,
                }
                response = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.api_key}",
                    json=payload,
                    timeout=timeout,
                )
                status_code = response.status_code
                if status_code == 400 and "thinkingConfig" in generation_config:
                    retry_config = dict(generation_config)
                    retry_config.pop("thinkingConfig", None)
                    payload["generationConfig"] = retry_config
                    response = requests.post(
                        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.api_key}",
                        json=payload,
                        timeout=timeout,
                    )
                    status_code = response.status_code

                if status_code != 200:
                    error_kind = "rate_limited" if status_code == 429 else "http_error"
                    error_code = self._safe_error_code(response)
                    last_error = error_kind
                    last_status_code = status_code
                    last_error_code = error_code
                    continue

                response_data = response.json()
                candidates = response_data.get("candidates", [])
                if candidates and isinstance(candidates[0], dict) and candidates[0].get("finishReason") == "MAX_TOKENS":
                    last_error = "truncated"
                    continue

                text = GeminiProvider._extract_text(response_data)
                if text:
                    return ProviderResult(text, model, status_code=status_code)
                last_error = "invalid_response"
            except requests.exceptions.Timeout:
                error_kind = "timeout"
                last_error = "timeout"
            except (requests.exceptions.RequestException, ValueError, TypeError, KeyError, IndexError):
                error_kind = "exception"
                last_error = "exception"
            finally:
                AIProviderTelemetry.record(
                    self.name, self.exchange, model, context, status_code,
                    (time.monotonic() - started) * 1000.0, error_kind
                )

        return ProviderResult(None, last_model, last_error, last_status_code, last_error_code)

