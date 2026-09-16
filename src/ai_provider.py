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
from datetime import datetime, timedelta
from typing import Any, Protocol, ClassVar
from zoneinfo import ZoneInfo

import requests

from gemini_telemetry import (
    GeminiTelemetry,
    quota_bucket_for_model,
    quota_limit_for_bucket,
)

logger = logging.getLogger(__name__)

# 신규 BUY 전역 게이트(entry_safety)와 분리하는 Gemini 컨텍스트 — 로컬 퀀트·캐시 폴백이 있는 보조 분석
ENTRY_SAFETY_AUXILIARY_CONTEXTS = frozenset({"macro_regime", "market_briefing", "screener_rank"})


def _is_auxiliary_entry_safety_context(context: str) -> bool:
    """거시·브리핑·스크리너 랭킹 실패는 종목별 trading 분석 fail-closed와 분리한다."""
    ctx = str(context or "").strip().lower()
    if ctx in ENTRY_SAFETY_AUXILIARY_CONTEXTS:
        return True
    return "briefing" in ctx


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

    def models_for(self, purpose: str, for_emergency_exit: bool = False) -> list[str]: ...

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
    # 원인 API 실패와 종목별 파생 BUY 차단·모델 폴백을 분리 집계한다.
    _observability: dict[str, dict[str, Any]] = {}
    _last_root_incident_signature: dict[str, str] = {}

    @classmethod
    def _today_kst(cls) -> str:
        """대시보드 기록 날짜 표기는 운영 기준인 한국 시간을 사용한다."""
        return datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d")

    @staticmethod
    def _empty_observability() -> dict[str, Any]:
        return {
            "root_api_failures": 0,
            "derived_buy_blocks": 0,
            "model_fallbacks": 0,
            "cache_hits": 0,
            "last_root_failure": {},
            "last_event_kst": "",
        }

    @classmethod
    def _obs_locked(cls, exchange: str) -> dict[str, Any]:
        ex = str(exchange or "").strip().lower() or "bithumb"
        state = cls._observability.get(ex)
        if not isinstance(state, dict):
            state = cls._empty_observability()
            cls._observability[ex] = state
        return state

    @classmethod
    def _check_kst_daily_rollover_locked(cls) -> None:
        """KST 거래일이 바뀌면 저장 date와 이벤트 집계가 어긋나지 않도록 운영 일자를 맞춘다."""
        today = cls._today_kst()
        if not cls._current_date:
            cls._current_date = today
            return
        if today == cls._current_date:
            return
        # Provider 쿼터 창이 살아 있으면 Google PT 기준 누적은 유지하고 표시용 KST 일자만 갱신한다.
        if cls._reset_at > time.time():
            cls._current_date = today
            return
        cls._current_date = today
        cls._stats = {}
        cls._persisted_stats = {}
        cls._observability = {}
        cls._last_root_incident_signature = {}
        cls._force_new_window = True
        cls._save_state_locked()

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
            cls._observability = {}
            cls._last_root_incident_signature = {}
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
            stored_date = str(payload.get("date", "") or "")
            today_kst = cls._today_kst()
            if stored_date and stored_date != today_kst and cls._reset_at <= time.time():
                cls._current_date = today_kst
                cls._stats = {}
                cls._persisted_stats = {}
            else:
                cls._current_date = stored_date or today_kst
            obs = payload.get("observability", {})
            if isinstance(obs, dict):
                cls._observability = {
                    str(ex): cls._empty_observability() | dict(val)
                    for ex, val in obs.items()
                    if isinstance(val, dict) and str(ex).strip()
                }
            cls._check_header_reset_locked()
            cls._check_kst_daily_rollover_locked()
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
                        "date": cls._today_kst(),
                        "reset_at": reset_at,
                        "reset_remaining_raw": reset_raw,
                        "reset_header_at": reset_header_at,
                        "entry_safety": merged_entry_safety,
                        "observability": {
                            ex: dict(val) for ex, val in cls._observability.items()
                        },
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
            cls._check_kst_daily_rollover_locked()
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
            cls._check_kst_daily_rollover_locked()
            stat = cls._stats.setdefault(key, {"calls": 0, "success": 0, "rate_limited": 0, "errors": 0,
                                               "cache_hits": 0, "latency_total_ms": 0.0, "last_event": "", "last_event_at": 0.0})
            stat["cache_hits"] += 1
            obs = cls._obs_locked(exchange)
            obs["cache_hits"] = int(obs.get("cache_hits", 0)) + 1
            stat["last_event"] = f"{context} {model} CACHE"
            stat["last_event_at"] = time.time()
            cls._save_state_locked()

    @classmethod
    def record_root_api_failure(
        cls,
        exchange: str,
        *,
        reason: str,
        context: str = "",
        model: str = "",
        status_code: int | None = None,
        error_code: str = "",
    ) -> None:
        """Gemini HTTP/스키마 등 원인 API 실패 1건을 집계·단일 WARNING 로그로 남긴다."""
        with cls._lock:
            cls._ensure_configured_locked()
            cls._check_kst_daily_rollover_locked()
            ex = str(exchange or "").strip().lower() or "bithumb"
            obs = cls._obs_locked(ex)
            obs["root_api_failures"] = int(obs.get("root_api_failures", 0)) + 1
            now_ts = time.time()
            obs["last_root_failure"] = {
                "reason": str(reason)[:120],
                "context": str(context)[:80],
                "model": str(model)[:120],
                "http_status": status_code,
                "error_code": str(error_code)[:80],
                "at": now_ts,
                "at_kst": datetime.fromtimestamp(now_ts, ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S"),
            }
            obs["last_event_kst"] = obs["last_root_failure"]["at_kst"]
            signature = f"{reason}|{context}|{model}|{status_code}|{error_code}"
            if cls._last_root_incident_signature.get(ex) != signature:
                cls._last_root_incident_signature[ex] = signature
                label = "업비트" if ex == "upbit" else "빗썸"
                logger.warning(
                    "[Gemini 원인 API 장애] 거래소=%s reason=%s context=%s model=%s http=%s code=%s",
                    label,
                    reason or "unknown",
                    context or "-",
                    model or "-",
                    status_code if status_code is not None else "-",
                    error_code or "-",
                )
            cls._save_state_locked()

    @classmethod
    def record_derived_buy_block(cls, exchange: str) -> None:
        """종목별 파생 BUY 차단 1건(로그 없이 카운터만 증가)."""
        with cls._lock:
            cls._ensure_configured_locked()
            cls._check_kst_daily_rollover_locked()
            ex = str(exchange or "").strip().lower() or "bithumb"
            obs = cls._obs_locked(ex)
            obs["derived_buy_blocks"] = int(obs.get("derived_buy_blocks", 0)) + 1
            cls._save_state_locked()

    @classmethod
    def record_model_fallback(
        cls, exchange: str, from_model: str, to_model: str, context: str = "",
    ) -> None:
        """차순위 모델로 성공한 폴백 결과를 거래소별로 기록한다."""
        with cls._lock:
            cls._ensure_configured_locked()
            cls._check_kst_daily_rollover_locked()
            ex = str(exchange or "").strip().lower() or "bithumb"
            obs = cls._obs_locked(ex)
            obs["model_fallbacks"] = int(obs.get("model_fallbacks", 0)) + 1
            now_ts = time.time()
            obs["last_fallback"] = {
                "from_model": str(from_model)[:120],
                "to_model": str(to_model)[:120],
                "context": str(context)[:80],
                "at_kst": datetime.fromtimestamp(now_ts, ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S"),
            }
            cls._save_state_locked()

    @classmethod
    def _effective_entry_safety_state(cls, state: dict[str, Any]) -> dict[str, Any]:
        """디스크에 남은 레거시 screener_rank 차단은 로컬 폴백 정책에 맞게 해제 상태로 노출한다."""
        normalized = cls._normalize_entry_safety(state)
        if normalized.get("entry_blocked") and str(normalized.get("context", "")).strip().lower() == "screener_rank":
            cleared = dict(normalized)
            cleared.update({"entry_blocked": False, "status": "NORMAL", "reason": ""})
            return cleared
        return normalized

    @classmethod
    def _reconcile_legacy_screener_entry_block_locked(cls, exchange: str) -> None:
        """screener_rank만으로 닫힌 entry_safety를 영속 저장소에서 정리한다."""
        state = cls._entry_safety.get(exchange, {})
        effective = cls._effective_entry_safety_state(state)
        if state.get("entry_blocked") and not effective.get("entry_blocked"):
            cls._entry_safety[exchange] = effective
            cls._save_state_locked()

    @classmethod
    def record_entry_safety(
        cls, exchange: str, *, blocked: bool, reason: str = "", context: str = "",
        model: str = "", status_code: int | None = None, error_code: str = "",
    ) -> None:
        """FAST 분석 결과를 신규 BUY 공통 차단 상태로 원자 저장한다."""
        if blocked and _is_auxiliary_entry_safety_context(context):
            return
        with cls._lock:
            cls._ensure_configured_locked()
            cls._check_kst_daily_rollover_locked()
            previous = cls._entry_safety.get(exchange, {})
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
            if blocked and (
                not previous.get("entry_blocked")
                or str(previous.get("reason")) != str(reason)
            ):
                cls.record_root_api_failure(
                    exchange,
                    reason=reason or "provider_failure",
                    context=context,
                    model=model,
                    status_code=status_code,
                    error_code=error_code,
                )
            cls._save_state_locked()

    @classmethod
    def get_entry_block_reason(cls, exchange: str, max_age_sec: float | None = None) -> str:
        """기존 포지션에는 영향 없이 모든 신규 BUY 경로가 공통으로 읽는 차단 사유다."""
        with cls._lock:
            cls._ensure_configured_locked()
            cls._reconcile_legacy_screener_entry_block_locked(exchange)
            state = cls._effective_entry_safety_state(cls._entry_safety.get(exchange, {}))
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
    def get_model_stat(cls, provider: str, exchange: str, model: str) -> dict[str, Any]:
        """특정 프로바이더/거래소/모델의 통계를 반환합니다."""
        with cls._lock:
            cls._ensure_configured_locked()
            stat = cls._stats.get((provider, exchange, model))
            return dict(stat) if stat else {"calls": 0, "success": 0, "rate_limited": 0, "errors": 0}

    @classmethod
    def snapshot(cls, exchange: str) -> dict[str, Any]:
        """대시보드 확장용 읽기 전용 집계를 반환합니다."""
        with cls._lock:
            cls._ensure_configured_locked()
            cls._check_header_reset_locked()
            cls._reconcile_legacy_screener_entry_block_locked(exchange)
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
                "entry_safety": cls._effective_entry_safety_state(cls._entry_safety.get(exchange, {})),
                "observability": dict(cls._obs_locked(exchange)),
            }

    @classmethod
    def get_daily_quota_budget(cls, exchange: str = "bithumb") -> dict[str, Any]:
        """거래소별 당일 AI 시도량 및 남은 쿼터 단계 정보 반환 (1,000회 기준)"""
        snap = cls.snapshot(exchange)
        calls = int(snap.get("total", {}).get("api_calls", 0))
        limit = int(snap.get("quota_limit", 1000) or 1000)
        return {
            "api_calls": calls,
            "quota_limit": limit,
            "is_tight": calls >= int(limit * 0.70),
            "is_critical": calls >= int(limit * 0.90),
            "is_exhausted": calls >= int(limit * 0.98),
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


class BaseGeminiProvider:
    """빗썸과 업비트가 공통으로 사용하는 Gemini API 통신 및 모델 라우팅 베이스 클래스입니다."""

    name = "gemini"
    exchange = "base"
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

    SYSTEM_INSTRUCTION: str = ""
    BRIEFING_SYSTEM_INSTRUCTION: str = ""

    # 모델별 쿨다운(429/할당량 초과) 만료 시점 캐시 (거래소별 격리)
    _COOLDOWNS_BY_EXCHANGE: ClassVar[dict[str, dict[str, float]]] = {}
    _COOLDOWN_LOCK: ClassVar[threading.RLock] = threading.RLock()

    @classmethod
    def clear_cooldowns(cls, exchange: str | None = None) -> None:
        """단위 테스트 및 세션 재시작을 위한 모델 쿨다운 초기화"""
        with cls._COOLDOWN_LOCK:
            if exchange:
                cls._COOLDOWNS_BY_EXCHANGE.pop(exchange, None)
            else:
                cls._COOLDOWNS_BY_EXCHANGE.clear()

    def is_model_cooling_down(self, model: str) -> bool:
        """모델의 쿨다운 만료 여부를 확인하고 만료 시 자동 해제합니다."""
        now = time.time()
        with self._COOLDOWN_LOCK:
            exchange_map = self._COOLDOWNS_BY_EXCHANGE.setdefault(self.exchange, {})
            exp = exchange_map.get(model, 0.0)
            if exp <= now:
                if model in exchange_map:
                    exchange_map.pop(model, None)
                return False
            return True

    def set_model_cooldown(
        self,
        model: str,
        duration_sec: float | None = None,
        until_pt_midnight: bool = False,
        reason: str = "429 Rate Limit",
    ) -> None:
        """모델별 쿨다운을 거래소 격리 캐시에 등록하고 GeminiAnalyzer와 동기화합니다."""
        now = time.time()
        if until_pt_midnight:
            # 미국 태평양 표준시(PT) 기준 다음 자정 계산 (한국 시간 16:00 KST 리셋)
            now_pt = datetime.now(ZoneInfo("America/Los_Angeles"))
            next_pt = (now_pt + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            expire_at = next_pt.timestamp()
        elif duration_sec is not None:
            expire_at = now + max(0.01, duration_sec)
        else:
            expire_at = now + 300.0  # 기본 5분(300초)

        with self._COOLDOWN_LOCK:
            exchange_map = self._COOLDOWNS_BY_EXCHANGE.setdefault(self.exchange, {})
            exchange_map[model] = expire_at

        # GeminiAnalyzer 전역 캐시와도 동기화 (분석기 라우터 연동)
        try:
            from gemini_analyzer import GeminiAnalyzer
            with GeminiAnalyzer._CLASS_LOCK:
                GeminiAnalyzer._MODEL_COOLDOWNS[model] = expire_at
        except Exception:
            pass

        remaining = max(0, int(expire_at - now))
        logger.warning(
            "[%s] %s 쿨다운 등록 (%s) -> %d초 후 만료 (차순위 모델 우선 호출)",
            self.exchange, model, reason, remaining,
        )

    def can_call_model_safety(self, model: str, for_emergency_exit: bool = False) -> bool:
        """
        호출 전 쿨다운 상태 및 거래소별 일일 쿼터 잔여 여부를 사전 검증합니다:
        - 쿨다운 중인 모델은 즉시 False
        - 업비트: GeminiTelemetry.can_call_model 검증 (소진 시 PT 자정까지 쿨다운 자동 등록)
        - 빗썸: AIProviderTelemetry 기반 Flash-Lite 쿼터 안전선(85%) 검증
        """
        if self.is_model_cooling_down(model):
            return False

        if self.exchange == "upbit":
            if hasattr(GeminiTelemetry, "can_call_model"):
                if not GeminiTelemetry.can_call_model(model, for_emergency_exit=for_emergency_exit):
                    self.set_model_cooldown(
                        model, until_pt_midnight=True, reason="업비트 Gemini 일일 쿼터(RPD) 한도 도달",
                    )
                    return False
        elif self.exchange == "bithumb":
            stat = AIProviderTelemetry.get_model_stat(self.name, self.exchange, model)
            calls = stat.get("calls", 0)
            limit = 500 if "flash-lite" in model else (20 if "-flash" in model else 0)
            if limit > 0:
                threshold = int(limit * 0.95) if for_emergency_exit else int(limit * 0.85)
                if calls >= threshold:
                    self.set_model_cooldown(
                        model, until_pt_midnight=True, reason="빗썸 Gemini 일일 쿼터(RPD) 한도 도달",
                    )
                    return False

        return True

    def __init__(self, api_key: str):
        self.api_key = (api_key or "").strip()
        self._models: list[str] | None = None
        self._macro_models: list[str] | None = None
        self._briefing_models: list[str] | None = None

    @property
    def is_configured(self) -> bool:
        """거래소 전용 Gemini 키가 있을 때만 모델 탐색과 분석을 허용한다."""
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

    @staticmethod
    def _extract_text(payload: dict[str, Any]) -> str | None:
        """Gemini API 응답 구조에서 텍스트를 안전하게 추출한다."""
        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            return None
        parts = candidates[0].get("content", {}).get("parts", []) if isinstance(candidates[0], dict) else []
        if not isinstance(parts, list) or not parts or not isinstance(parts[0], dict):
            return None
        text = parts[0].get("text")
        return str(text).strip() if text else None

    def _record_http_attempt(
        self, model: str, context: str, endpoint: str, status_code: int | None,
        duration_ms: float = 0.0, error_kind: str = "",
    ) -> None:
        """거래소별 텔레메트리 격리 기록 훅 (하위 클래스 오버라이드)"""
        pass

    def _record_entry_safety(self, result: ProviderResult, context: str) -> ProviderResult:
        """신규 BUY 진입 fail-closed 안전 상태 갱신 훅 (하위 클래스 오버라이드)"""
        return result

    def _discover_models(self) -> list[str]:
        """신규 BUY용 구체 Flash-Lite 모델이 있을 때만 분석을 허용한다."""
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
            self._record_http_attempt("list_models", "list_models", "list_models", status_code,
                                      (time.monotonic() - started) * 1000.0, error_kind)
        self._record_entry_safety(ProviderResult(None, "", error_kind or "exception", status_code, error_code), "list_models")
        return []

    def _discover_macro_models(self) -> list[str]:
        """거래소 전용 키로 거시 진단용 Flash 모델을 탐색하고, 우선순위대로 반환한다."""
        if not self.is_configured:
            return []
        try:
            response = requests.get(
                f"https://generativelanguage.googleapis.com/v1beta/models?key={self.api_key}", timeout=10.0,
            )
            if response.status_code != 200:
                return list(self.MACRO_FALLBACK_MODELS)
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
            return list(self.MACRO_FALLBACK_MODELS)
        except Exception:
            return list(self.MACRO_FALLBACK_MODELS)

    def _discover_briefing_models(self) -> list[str]:
        """거래소 전용 키로 브리핑용 Flash 모델을 탐색하고, 우선순위대로 반환한다."""
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

    def models_for(self, purpose: str, for_emergency_exit: bool = False) -> list[str]:
        """
        목적별 허용 모델을 반환하며, 쿨다운 또는 쿼터 소진 모델은 사전에 제외하고
        가용한 차순위 모델(예: 3.5 소진 시 3.1)을 1순위로 즉시 승격하여 반환한다.
        """
        if purpose == "macro":
            if self._macro_models is None:
                self._macro_models = self._discover_macro_models()
            base_list = list(self._macro_models) if self._macro_models else [self.TRADING_MODEL]
            available = [m for m in base_list if self.can_call_model_safety(m, for_emergency_exit=for_emergency_exit)]
            return available if available else [m for m in base_list if not self.is_model_cooling_down(m)]

        if purpose == "briefing":
            if self._briefing_models is None:
                self._briefing_models = self._discover_briefing_models()
            base_list = list(self._briefing_models) if self._briefing_models else list(self.BRIEFING_FALLBACK_MODELS)
            available = [m for m in base_list if self.can_call_model_safety(m, for_emergency_exit=for_emergency_exit)]
            return available if available else [m for m in base_list if not self.is_model_cooling_down(m)]

        if purpose == "trading":
            if self._models is None:
                self._models = self._discover_models()
            base_list = list(self._models) if self._models is not None else []
            # 쿨다운 및 쿼터 안전선을 통과한 가용 모델만 선별
            available = [m for m in base_list if self.can_call_model_safety(m, for_emergency_exit=for_emergency_exit)]
            return available
        return []

    def complete_json(
        self, prompt: str, models: list[str], schema: dict[str, Any], *, context: str, timeout: float, max_tokens: int,
        schema_name: str = "bithumb_trading_result", strict: bool = True,
    ) -> ProviderResult:
        """Gemini generateContent 응답을 로컬 JSON 스키마 검증 뒤에만 정상 처리하며 순차 폴백을 지원한다."""
        if not self.is_configured or not models:
            return self._record_entry_safety(ProviderResult(None, "", "configuration"), context)
        last_result = ProviderResult(None, models[0], "configuration")
        is_emergency = "holding" in context or "emergency" in context
        failed_models: list[str] = []
        for model in models:
            # 호출 직전 쿨다운/쿼터 안전 검증 (이미 소진된 모델에 대한 불필요한 HTTP 호출 원천 차단)
            if not self.can_call_model_safety(model, for_emergency_exit=is_emergency):
                logger.info("[%s] %s 쿨다운 또는 쿼터 소진으로 호출 건너뜀 (차순위 시도)", self.exchange, model)
                continue

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
                payload: dict[str, Any] = {
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": generation_config,
                }
                if self.SYSTEM_INSTRUCTION:
                    payload["systemInstruction"] = {"parts": [{"text": self.SYSTEM_INSTRUCTION}]}
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
                    if status_code == 429:
                        # 429 Rate Limit 감지 시 즉시 쿨다운 등록
                        retry_after_str = response.headers.get("Retry-After", "").strip() if response is not None else ""
                        retry_sec = float(retry_after_str) if retry_after_str.isdigit() else None
                        res_text = response.text.lower() if response is not None and response.text else ""
                        is_daily = (
                            "resource_exhausted" in error_code.lower()
                            or "quota" in res_text
                            or not self.can_call_model_safety(model, for_emergency_exit=is_emergency)
                        )
                        if is_daily:
                            self.set_model_cooldown(
                                model, until_pt_midnight=True, reason="429 Resource Exhausted (일일 쿼터 소진)",
                            )
                        else:
                            self.set_model_cooldown(
                                model, duration_sec=retry_sec or 300.0,
                                reason=f"429 Rate Limit (일시 초과, Retry-After={retry_after_str or 'None'})",
                            )
                    last_result = ProviderResult(None, model, error_kind, status_code, error_code)
                    failed_models.append(model)
                    continue
                value = _parse_json_text(self._extract_text(response.json()) or "")
                if value is None:
                    last_result = ProviderResult(None, model, "invalid_json", status_code)
                    failed_models.append(model)
                    continue
                if not _validate_schema(value, schema):
                    last_result = ProviderResult(None, model, "schema", status_code)
                    failed_models.append(model)
                    continue
                if failed_models:
                    AIProviderTelemetry.record_model_fallback(
                        self.exchange,
                        failed_models[-1],
                        model,
                        context,
                    )
                res = ProviderResult(value, model, status_code=status_code)
                return self._record_entry_safety(res, context)
            except requests.exceptions.Timeout:
                error_kind = "timeout"
                last_result = ProviderResult(None, model, error_kind, status_code, error_code)
                failed_models.append(model)
                continue
            except (requests.exceptions.RequestException, ValueError, TypeError, KeyError, IndexError):
                error_kind = "exception"
                last_result = ProviderResult(None, model, error_kind, status_code, error_code)
                failed_models.append(model)
                continue
            finally:
                self._record_http_attempt(model, context, "generate_content", status_code,
                                          (time.monotonic() - started) * 1000.0, error_kind)
        return self._record_entry_safety(last_result, context)

    def complete_text(
        self, prompt: str, models: list[str], *, context: str, timeout: float, max_tokens: int,
    ) -> ProviderResult:
        """브리핑은 신규 BUY 게이트와 독립된 텍스트 보조 경로로만 제공하며 순차 폴백을 지원한다."""
        if not self.is_configured or not models:
            return ProviderResult(None, "", "configuration")
        last_error = "no_model"
        last_model = models[0]
        last_status_code: int | None = None
        last_error_code = ""

        system_instruction = self.BRIEFING_SYSTEM_INSTRUCTION if "briefing" in context else self.SYSTEM_INSTRUCTION

        for model in models:
            # 브리핑 등 텍스트 생성도 쿨다운 또는 쿼터 소진 모델은 건너뜀
            if not self.can_call_model_safety(model, for_emergency_exit=True):
                logger.info("[%s] %s 쿨다운 또는 쿼터 소진으로 브리핑 호출 건너뜀 (차순위 시도)", self.exchange, model)
                continue

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
                payload: dict[str, Any] = {
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": generation_config,
                }
                if system_instruction:
                    payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}
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
                    if status_code == 429:
                        retry_after_str = response.headers.get("Retry-After", "").strip() if response is not None else ""
                        retry_sec = float(retry_after_str) if retry_after_str.isdigit() else None
                        res_text = response.text.lower() if response is not None and response.text else ""
                        is_daily = (
                            "resource_exhausted" in error_code.lower()
                            or "quota" in res_text
                            or not self.can_call_model_safety(model, for_emergency_exit=True)
                        )
                        if is_daily:
                            self.set_model_cooldown(
                                model, until_pt_midnight=True, reason="429 Resource Exhausted (일일 쿼터 소진)",
                            )
                        else:
                            self.set_model_cooldown(
                                model, duration_sec=retry_sec or 300.0,
                                reason=f"429 Rate Limit (일시 초과, Retry-After={retry_after_str or 'None'})",
                            )
                    last_error = error_kind
                    last_status_code = status_code
                    last_error_code = error_code
                    continue

                response_data = response.json()
                candidates = response_data.get("candidates", [])
                if candidates and isinstance(candidates[0], dict) and candidates[0].get("finishReason") == "MAX_TOKENS":
                    last_error = "truncated"
                    continue

                text = self._extract_text(response_data)
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
                self._record_http_attempt(
                    model, context, "generate_content", status_code,
                    (time.monotonic() - started) * 1000.0, error_kind,
                )

        return ProviderResult(None, last_model, last_error, last_status_code, last_error_code)


class GeminiProvider(BaseGeminiProvider):
    """업비트 전용 Gemini 경계이며 공용/빗썸 키와 절대로 혼합하지 않습니다."""

    name = "gemini"
    exchange = "upbit"
    is_entry_fail_closed = True

    SYSTEM_INSTRUCTION = """당신은 업비트 전용 Gemini AI 분석 보조자입니다.
업비트에서 제공한 데이터만 사용하고 빗썸·다른 거래소 데이터, API 키, 계좌, 주문 상태를 절대로 혼합하지 마세요. 제공된 수치 외에는 추측하지 마세요.
ACK는 체결이 아닙니다. REST 또는 Private WebSocket의 확정 체결 전에는 포지션·손익·쿨다운·주문 완료를 단정하지 마세요.
불확실하거나 데이터가 누락·모순되면 BUY가 아닌 HOLD를 선택하세요. 주문 실행·취소·체결 확정 권한은 없습니다.
현재 레짐, 후보 경로(SCALP, SWING, MOMENTUM_BREAKOUT, RECOVERY_REBOUND, NEW_LISTING), RS 주도주(RS >= +2.0%) 특례 규정, 신규상장 정책과 안전 차단 조건을 준수하세요. 상투·고점 추격 매수(Chasing the Top)는 절대 금지하며, BUY 승인 시 진입가(ENTRY_PRICE)는 현재가 추격을 지양하고 5분봉 VWAP, MA20 또는 전저점 지지선 부근의 저점 눌림목 지정가(현재가 이하)로 산출하여 안전마진을 확보하세요. MOMENTUM_BREAKOUT의 EXTENDED는 확정 5분봉 고점 돌파·양봉·거래량·RSI·1시간 EMA20을 모두 통과한 경우에만 주간 알파 55점/심야 65점 이상으로 제한 추격 BUY를 검토하며, 최초 비중은 최대 종목 비중의 15%를 넘지 않습니다. 알트코인은 비트코인 급락(CRASH)이 아닌 이상 비트코인의 추세(조정·약세·횡보)에 영향없이 자체 기술적 지표와 수급(체결강도 75% 이상, 호가 갭 0.50% 이하)이 양호하고 손익비(1:1.3 이상)가 확보되면 1시간봉 조정 구간이더라도 5분봉 지지선 안착을 확인하여 독립적으로 매수를 승인하되, 비트코인 대폭락(CRASH)에서는 신규 진입을 제안하지 마세요. 스크리너 단계 신규상장 사전 필터로 `classify_listing_maturity()` + `is_new_listing_eligible()` SSOT에 따라 자격 미충족 NEW_LISTING 후보는 AI 입력 전에 제외됩니다.
API 키, 시크릿, 토큰, 계좌 또는 주문 식별자를 요구·출력·재현하지 마세요. JSON 요청에는 마크다운 없는 유효 JSON만 반환하고, 모든 설명 텍스트는 자연스러운 한국어로만 작성하세요."""

    # 텍스트 시황 브리핑 전용 지침 (거래소 격리·제공 데이터만 사용·ACK 비체결·불확실 신규 BUY 금지·비밀정보 비출력 원칙 준수)
    BRIEFING_SYSTEM_INSTRUCTION = """당신은 업비트 전용 Gemini AI 종합 시황 분석 보조자입니다.
업비트에서 제공한 데이터만 사용하고 빗썸·다른 거래소 데이터를 절대로 혼합하지 마세요. 제공된 수치 외에는 임의로 추측하지 마세요.
ACK는 체결이 아닙니다. REST 또는 Private WebSocket의 확정 체결 전에는 포지션·손익·쿨다운·주문 완료를 단정하지 마세요.
불확실하거나 데이터가 누락·모순되면 신규 BUY를 제안하지 마세요. 주문 실행·취소·체결 확정 권한은 없습니다.
API 키, 시크릿, 토큰, 계좌 또는 주문 식별자를 요구·출력·재현하지 마세요.
요청된 시황 브리핑 지침에 따라 JSON이 아닌 자연스러운 한국어 텍스트로 명확하고 전문적인 3줄 시황 브리핑을 작성하세요."""

    def _record_http_attempt(
        self, model: str, context: str, endpoint: str, status_code: int | None,
        duration_ms: float = 0.0, error_kind: str = "",
    ) -> None:
        """업비트 전용 GeminiTelemetry에 기록한다."""
        GeminiTelemetry.record_http_attempt(model, context, endpoint, status_code, error_kind)
        AIProviderTelemetry.record(self.name, self.exchange, model, context, status_code, duration_ms, error_kind)

    def _record_entry_safety(self, result: ProviderResult, context: str) -> ProviderResult:
        """업비트 fail-closed 안전 상태를 갱신한다."""
        is_entry_context = not _is_auxiliary_entry_safety_context(context)
        if is_entry_context:
            is_success = isinstance(result.value, (dict, list))
            AIProviderTelemetry.record_entry_safety(
                self.exchange,
                blocked=not is_success,
                reason="" if is_success else (result.error_kind or "invalid_response"),
                context=context,
                model=result.model,
                status_code=result.status_code,
                error_code=result.error_code,
            )
        return result


class BithumbGeminiProvider(BaseGeminiProvider):
    """빗썸 전용 Gemini 경계이며 업비트 키·텔레메트리와 절대로 공유하지 않습니다."""

    name = "gemini"
    exchange = "bithumb"
    is_entry_fail_closed = True

    # Flash-Lite만 탐색해 고비용 모델 승격과 암묵적 모델 폴백을 금지한다.
    SYSTEM_INSTRUCTION = """당신은 빗썸 전용 Gemini AI 분석 보조자입니다.
빗썸에서 제공한 데이터만 사용하고 업비트·다른 거래소 데이터, API 키, 계좌, 주문 상태를 절대로 혼합하지 마세요. 제공된 수치 외에는 추측하지 마세요.
ACK는 체결이 아닙니다. REST 또는 Private WebSocket의 확정 체결 전에는 포지션·손익·쿨다운·주문 완료를 단정하지 마세요.
불확실하거나 데이터가 누락·모순되면 BUY가 아닌 HOLD를 선택하세요. 주문 실행·취소·체결 확정 권한은 없습니다.
현재 레짐, 후보 경로(SCALP, SWING, MOMENTUM_BREAKOUT, RECOVERY_REBOUND, NEW_LISTING), RS 주도주(RS >= +2.0%) 특례 규정, 신규상장 정책과 안전 차단 조건을 준수하세요. 상투·고점 추격 매수(Chasing the Top)는 절대 금지하며, BUY 승인 시 진입가(ENTRY_PRICE)는 현재가 추격을 지양하고 5분봉 VWAP, MA20 또는 전저점 지지선 부근의 저점 눌림목 지정가(현재가 이하)로 산출하여 안전마진을 확보하세요. MOMENTUM_BREAKOUT의 EXTENDED는 확정 5분봉 고점 돌파·양봉·거래량·RSI·1시간 EMA20을 모두 통과한 경우에만 주간 알파 55점/심야 65점 이상으로 제한 추격 BUY를 검토하며, 최초 비중은 최대 종목 비중의 15%를 넘지 않습니다. 알트코인은 비트코인 급락(CRASH)이 아닌 이상 비트코인의 추세(조정·약세·횡보)에 영향없이 자체 기술적 지표와 수급(체결강도 75% 이상, 호가 갭 0.50% 이하)이 양호하고 손익비(1:1.3 이상)가 확보되면 1시간봉 조정 구간이더라도 5분봉 지지선 안착을 확인하여 독립적으로 매수를 승인하되, 비트코인 대폭락(CRASH)에서는 신규 진입을 제안하지 마세요. 스크리너 단계 신규상장 사전 필터로 `classify_listing_maturity()` + `is_new_listing_eligible()` SSOT에 따라 자격 미충족 NEW_LISTING 후보는 AI 입력 전에 제외됩니다.
API 키, 시크릿, 토큰, 계좌 또는 주문 식별자를 요구·출력·재현하지 마세요. JSON 요청에는 마크다운 없는 유효 JSON만 반환하고, 모든 설명 텍스트는 자연스러운 한국어로만 작성하세요."""

    # 텍스트 시황 브리핑 전용 지침 (거래소 격리·제공 데이터만 사용·ACK 비체결·불확실 신규 BUY 금지·비밀정보 비출력 원칙 준수)
    BRIEFING_SYSTEM_INSTRUCTION = """당신은 빗썸 전용 Gemini AI 종합 시황 분석 보조자입니다.
빗썸에서 제공한 데이터만 사용하고 업비트·다른 거래소 데이터를 절대로 혼합하지 마세요. 제공된 수치 외에는 임의로 추측하지 마세요.
ACK는 체결이 아닙니다. REST 또는 Private WebSocket의 확정 체결 전에는 포지션·손익·쿨다운·주문 완료를 단정하지 마세요.
불확실하거나 데이터가 누락·모순되면 신규 BUY를 제안하지 마세요. 주문 실행·취소·체결 확정 권한은 없습니다.
API 키, 시크릿, 토큰, 계좌 또는 주문 식별자를 요구·출력·재현하지 마세요.
요청된 시황 브리핑 지침에 따라 JSON이 아닌 자연스러운 한국어 텍스트로 명확하고 전문적인 3줄 시황 브리핑을 작성하세요."""

    def _record_http_attempt(
        self, model: str, context: str, endpoint: str, status_code: int | None,
        duration_ms: float = 0.0, error_kind: str = "",
    ) -> None:
        """빗썸 전용 AIProviderTelemetry에 기록하며 업비트 GeminiTelemetry와 절대 공유하지 않는다."""
        AIProviderTelemetry.record(self.name, self.exchange, model or "unavailable", context, status_code, duration_ms, error_kind)

    def _record_entry_safety(self, result: ProviderResult, context: str) -> ProviderResult:
        """정상 JSON 스키마 검증을 마친 호출만 신규 BUY 차단을 해제한다. 보조 분석은 신규 BUY를 차단하지 않는다."""
        is_entry_context = not _is_auxiliary_entry_safety_context(context)
        if is_entry_context:
            is_success = isinstance(result.value, (dict, list))
            AIProviderTelemetry.record_entry_safety(
                self.exchange,
                blocked=not is_success,
                reason="" if is_success else (result.error_kind or "invalid_response"),
                context=context,
                model=result.model,
                status_code=result.status_code,
                error_code=result.error_code,
            )
        return result

    def models_for(self, purpose: str, for_emergency_exit: bool = False) -> list[str]:
        """목적별 허용 모델을 반환하며, 캐시 히트는 빗썸 전용 텔레메트리에만 기록한다."""
        if purpose == "macro":
            if self._macro_models is None:
                self._macro_models = self._discover_macro_models()
            base_list = list(self._macro_models) if self._macro_models else [self.TRADING_MODEL]
            available = [m for m in base_list if self.can_call_model_safety(m, for_emergency_exit=for_emergency_exit)]
            return available if available else [m for m in base_list if not self.is_model_cooling_down(m)]

        if purpose == "briefing":
            if self._briefing_models is None:
                self._briefing_models = self._discover_briefing_models()
            base_list = list(self._briefing_models) if self._briefing_models else list(self.BRIEFING_FALLBACK_MODELS)
            available = [m for m in base_list if self.can_call_model_safety(m, for_emergency_exit=for_emergency_exit)]
            return available if available else [m for m in base_list if not self.is_model_cooling_down(m)]

        if self._models is None:
            self._models = self._discover_models()
        elif self._models:
            # 프로세스 내 모델 목록 재사용도 빗썸 전용 텔레메트리에만 기록한다.
            AIProviderTelemetry.record_cache_hit(self.name, self.exchange, self._models[0], "list_models")
        base_list = list(self._models) if self._models is not None else []
        available = [m for m in base_list if self.can_call_model_safety(m, for_emergency_exit=for_emergency_exit)]
        return available


