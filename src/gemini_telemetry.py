"""Thread-safe Gemini API usage telemetry for operations monitoring with PT midnight reset and per-model quotas."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any
from zoneinfo import ZoneInfo

# 미국 태평양 표준시 (PT, PDT/PST 일광절약시간 자동 계산) & 한국 표준시 (KST)
PT_TZ = ZoneInfo("America/Los_Angeles")
KST_TZ = timezone(timedelta(hours=9))

LIST_MODELS_KEY = "list_models"


def get_pt_today_str() -> str:
    """미국 태평양 표준시(PT) 기준 현재 날짜 문자열 (YYYY-MM-DD) 반환"""
    return datetime.now(PT_TZ).strftime("%Y-%m-%d")


def get_pt_reset_info() -> dict[str, Any]:
    """
    PT 자정(00:00:00) 일괄 쿼터 리셋 정보 반환:
    - 서머타임(PDT, 3월~11월): 한국 시간 16:00 KST 리셋
    - 서머타임 해제(PST, 11월~3월): 한국 시간 17:00 KST 리셋
    """
    now_pt = datetime.now(PT_TZ)
    # 다음 PT 자정 계산
    next_midnight_pt = (now_pt + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    remaining_sec = max(0, int((next_midnight_pt - now_pt).total_seconds()))
    hours = remaining_sec // 3600
    minutes = (remaining_sec % 3600) // 60

    # 해당 자정을 KST로 변환
    next_midnight_kst = next_midnight_pt.astimezone(KST_TZ)
    reset_kst_str = next_midnight_kst.strftime("%H:%M KST")

    tz_name = now_pt.tzname() or "PT"
    return {
        "reset_time_kst": reset_kst_str,
        "remaining_sec": remaining_sec,
        "remaining_str": f"{hours}시간 {minutes:02d}분 후 리셋",
        "timezone": f"America/Los_Angeles ({tz_name})",
    }


def canonical_model_name(model: str) -> str:
    """쿼터 가드용 Flash-Lite 계열 표준 명칭 (하위 호환)"""
    return quota_bucket_for_model(model)


def quota_bucket_for_model(model: str) -> str:
    """Google AI Studio 무료 티어 쿼터 그룹 키 (실제 모델 ID 기반)"""
    if model == LIST_MODELS_KEY:
        return LIST_MODELS_KEY

    lower = (model or "").lower().strip()
    if not lower:
        return "unknown"

    lite_match = re.search(r"gemini-(\d+(?:\.\d+)?)-flash-lite", lower)
    if lite_match or ("flash-lite" in lower or "flash_lite" in lower):
        if "3.1" in lower:
            return "gemini-3.1-flash-lite"
        version = lite_match.group(1) if lite_match else None
        if version:
            return f"gemini-{version}-flash-lite"
        if "latest" in lower or "2.5" in lower:
            return "gemini-3.5-flash-lite"
        return "gemini-3.5-flash-lite"

    flash_match = re.search(r"gemini-(\d+(?:\.\d+)?)-flash", lower)
    if flash_match and "lite" not in lower:
        return f"gemini-{flash_match.group(1)}-flash"

    return lower


def quota_limit_for_bucket(bucket: str) -> int:
    """Google AI Studio 무료 티어 모델별 일일 한도 (RPD)"""
    if bucket == LIST_MODELS_KEY:
        return 0
    if "flash-lite" in bucket:
        return 500
    if bucket.endswith("-flash"):
        return 20
    return 0


def _empty_model_stat() -> dict[str, int]:
    return {"calls": 0, "success": 0, "rate_limited": 0, "errors": 0}


def _normalize_model_stats(raw: dict[str, Any] | None) -> dict[str, dict[str, int]]:
    """디스크/레거시 포맷을 실제 모델 ID 기반 통계로 정규화"""
    if not raw:
        return {}
    normalized: dict[str, dict[str, int]] = {}
    for model_id, stat in raw.items():
        if not isinstance(stat, dict):
            continue
        normalized[str(model_id)] = {
            "calls": int(stat.get("calls", 0)),
            "success": int(stat.get("success", 0)),
            "rate_limited": int(stat.get("rate_limited", 0)),
            "errors": int(stat.get("errors", 0)),
        }
    return normalized


def _aggregate_quota_buckets(by_model: dict[str, dict[str, int]]) -> dict[str, dict[str, Any]]:
    """실제 모델 ID 통계를 Google 쿼터 그룹별로 합산"""
    buckets: dict[str, dict[str, Any]] = {}
    for model_id, stat in by_model.items():
        if model_id == LIST_MODELS_KEY:
            continue
        bucket = quota_bucket_for_model(model_id)
        if bucket not in buckets:
            limit = quota_limit_for_bucket(bucket)
            buckets[bucket] = {
                "calls": 0,
                "success": 0,
                "rate_limited": 0,
                "errors": 0,
                "quota_limit": limit,
                "quota_used_pct": 0.0,
            }
        for key in ("calls", "success", "rate_limited", "errors"):
            buckets[bucket][key] += int(stat.get(key, 0))
        limit = int(buckets[bucket]["quota_limit"] or 0)
        calls = int(buckets[bucket]["calls"])
        buckets[bucket]["quota_used_pct"] = round((calls / limit) * 100.0, 1) if limit > 0 else 0.0
    return buckets


@dataclass(frozen=True)
class GeminiTelemetrySnapshot:
    """Point-in-time Gemini usage counters with per-model breakdown."""

    date: str
    api_calls: int
    api_success: int
    rate_limited: int
    http_errors: int
    list_models_calls: int
    local_fallback: int
    cache_hits: int
    last_event_at: float
    last_event: str
    quota_limit: int = 1000
    by_model: dict[str, dict[str, int]] | None = None
    reset_info: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        used_pct = round((self.api_calls / max(1, self.quota_limit)) * 100.0, 1)
        by_model = self.by_model or {}
        quota_buckets = _aggregate_quota_buckets(by_model)

        # 대시보드 하위 호환: Flash-Lite 2개 그룹은 항상 노출
        models_dict: dict[str, Any] = {}
        for m_name in ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite"):
            m_stat = quota_buckets.get(m_name, {})
            m_calls = int(m_stat.get("calls", 0))
            m_limit = quota_limit_for_bucket(m_name)
            models_dict[m_name] = {
                "calls": m_calls,
                "success": int(m_stat.get("success", 0)),
                "rate_limited": int(m_stat.get("rate_limited", 0)),
                "errors": int(m_stat.get("errors", 0)),
                "quota_limit": m_limit,
                "quota_used_pct": round((m_calls / m_limit) * 100.0, 1) if m_limit > 0 else 0.0,
            }

        # Google 대시보드 대조용: 실제 모델 ID별 상세 통계
        models_by_id: dict[str, Any] = {}
        for model_id, stat in sorted(by_model.items(), key=lambda item: (-item[1].get("calls", 0), item[0])):
            if int(stat.get("calls", 0)) <= 0:
                continue
            bucket = quota_bucket_for_model(model_id)
            limit = quota_limit_for_bucket(bucket) if model_id != LIST_MODELS_KEY else 0
            calls = int(stat.get("calls", 0))
            models_by_id[model_id] = {
                "calls": calls,
                "success": int(stat.get("success", 0)),
                "rate_limited": int(stat.get("rate_limited", 0)),
                "errors": int(stat.get("errors", 0)),
                "quota_bucket": bucket,
                "quota_limit": limit,
                "quota_used_pct": round((calls / limit) * 100.0, 1) if limit > 0 else 0.0,
            }

        return {
            "date": self.date,
            "api_calls": self.api_calls,
            "api_success": self.api_success,
            "rate_limited": self.rate_limited,
            "http_errors": self.http_errors,
            "list_models_calls": self.list_models_calls,
            "local_fallback": self.local_fallback,
            "cache_hits": self.cache_hits,
            "quota_limit": self.quota_limit,
            "quota_used_pct": used_pct,
            "models": models_dict,
            "models_by_id": models_by_id,
            "quota_buckets": quota_buckets,
            "reset_info": self.reset_info or get_pt_reset_info(),
            "success_rate_pct": round((self.api_success / self.api_calls) * 100.0, 1) if self.api_calls else 0.0,
            "fallback_rate_pct": round((self.local_fallback / max(1, self.api_calls)) * 100.0, 1),
            "last_event": self.last_event,
            "last_event_at": self.last_event_at,
        }


class GeminiTelemetry:
    """Process-wide Gemini call counters with PT midnight reset and per-model quotas."""

    _lock = threading.Lock()
    _current_date = get_pt_today_str()
    _api_calls = 0
    _api_success = 0
    _rate_limited = 0
    _http_errors = 0
    _list_models_calls = 0
    _local_fallback = 0
    _cache_hits = 0
    _last_event_at = 0.0
    _last_event = ""
    _quota_limit = int(os.getenv("GEMINI_DAILY_QUOTA_LIMIT", "1000"))
    _storage_path: str | None = None
    _configured = False
    _by_model: dict[str, dict[str, int]] = {}

    @classmethod
    def _ensure_model_stat_locked(cls, model_key: str) -> dict[str, int]:
        if model_key not in cls._by_model:
            cls._by_model[model_key] = _empty_model_stat()
        return cls._by_model[model_key]

    @classmethod
    def _bucket_call_count_locked(cls, bucket: str) -> int:
        total = 0
        for model_id, stat in cls._by_model.items():
            if model_id == LIST_MODELS_KEY:
                continue
            if quota_bucket_for_model(model_id) == bucket:
                total += int(stat.get("calls", 0))
        return total

    @classmethod
    def can_call_model(cls, model: str, for_emergency_exit: bool = False) -> bool:
        """
        특정 모델 쿼터 그룹의 일일 HTTP 시도 횟수 기반 가드:
        - 모델별 일일 한도(limit) 대비 안전 마진(평시 85%, 긴급 95%) 초과 시 호출 차단
        - Flash-Lite (500 RPD): 평시 425회, 긴급 475회
        - 일반 Flash (20 RPD): 평시 17회, 긴급 19회
        """
        with cls._lock:
            cls._ensure_configured_locked()
            cls._check_and_rollover()
            bucket = quota_bucket_for_model(model)
            limit = quota_limit_for_bucket(bucket)
            if limit <= 0:
                return True
            m_calls = cls._bucket_call_count_locked(bucket)
            threshold = int(limit * 0.95) if for_emergency_exit else int(limit * 0.85)
            return m_calls < threshold

    @classmethod
    def can_make_api_call(cls, for_emergency_exit: bool = False) -> bool:
        """
        일일 쿼터(Flash-Lite 2개 모델 각 500회 = 총 1,000 RPD) 예산 가드:
        - 신규 매수 분석: 당일 총 HTTP 시도 < 850회 (85%) 일 때 허용 (Google 429 선제 차단)
        - 긴급 탈출/비상 대응: 당일 총 HTTP 시도 < 950회 (95%) 일 때 허용
        """
        with cls._lock:
            cls._ensure_configured_locked()
            cls._check_and_rollover()
            threshold = int(cls._quota_limit * 0.95) if for_emergency_exit else int(cls._quota_limit * 0.85)
            return cls._api_calls < threshold

    @classmethod
    def get_daily_quota_budget(cls) -> dict[str, Any]:
        """당일 HTTP 시도량 및 남은 쿼터 단계 정보 반환 (1,000회 기준)"""
        with cls._lock:
            cls._ensure_configured_locked()
            cls._check_and_rollover()
            calls = cls._api_calls
            limit = cls._quota_limit
            return {
                "api_calls": calls,
                "quota_limit": limit,
                "is_tight": calls >= int(limit * 0.70),
                "is_critical": calls >= int(limit * 0.90),
                "is_exhausted": calls >= int(limit * 0.98),
            }

    @classmethod
    def reset_for_test(cls) -> None:
        """단위 테스트 격리를 위한 프로세스 내 카운터 초기화 및 디스크 상태 격리"""
        import tempfile
        with cls._lock:
            cls._storage_path = os.path.join(tempfile.mkdtemp(), "gemini_telemetry_test.json")
            cls._configured = True
            cls._by_model.clear()
            cls._api_calls = 0
            cls._api_success = 0
            cls._rate_limited = 0
            cls._http_errors = 0
            cls._list_models_calls = 0
            cls._local_fallback = 0
            cls._cache_hits = 0
            cls._last_event_at = 0.0
            cls._last_event = ""

    @classmethod
    def configure(cls, data_dir: str | None = None) -> None:
        """데이터 디렉토리를 바인딩하고 당일 저장된 텔레메트리 복원"""
        with cls._lock:
            if data_dir:
                cls._storage_path = os.path.join(data_dir, "gemini_telemetry.json")
            else:
                project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
                cls._storage_path = os.path.join(project_root, "data", "gemini_telemetry.json")
            cls._configured = True
            cls._load_state_locked()

    @classmethod
    def _ensure_configured_locked(cls) -> None:
        if not cls._configured:
            project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
            cls._storage_path = os.path.join(project_root, "data", "gemini_telemetry.json")
            cls._configured = True
            cls._load_state_locked()

    @classmethod
    def _load_state_locked(cls) -> None:
        if not cls._storage_path or not os.path.exists(cls._storage_path):
            return
        try:
            with open(cls._storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("date") == cls._current_date:
                cls._api_calls = int(data.get("api_calls", 0))
                cls._api_success = int(data.get("api_success", 0))
                cls._rate_limited = int(data.get("rate_limited", 0))
                cls._http_errors = int(data.get("http_errors", 0))
                cls._list_models_calls = int(data.get("list_models_calls", 0))
                cls._local_fallback = int(data.get("local_fallback", 0))
                cls._cache_hits = int(data.get("cache_hits", 0))
                cls._last_event_at = float(data.get("last_event_at", 0.0))
                cls._last_event = str(data.get("last_event", ""))
                cls._by_model = _normalize_model_stats(data.get("by_model") or data.get("models") or {})
        except Exception:
            pass

    @classmethod
    def _save_state_locked(cls) -> None:
        if not cls._storage_path:
            return
        try:
            os.makedirs(os.path.dirname(cls._storage_path), exist_ok=True)
            payload = {
                "date": cls._current_date,
                "api_calls": cls._api_calls,
                "api_success": cls._api_success,
                "rate_limited": cls._rate_limited,
                "http_errors": cls._http_errors,
                "list_models_calls": cls._list_models_calls,
                "local_fallback": cls._local_fallback,
                "cache_hits": cls._cache_hits,
                "quota_limit": cls._quota_limit,
                "by_model": cls._by_model,
                "last_event_at": cls._last_event_at,
                "last_event": cls._last_event,
            }
            tmp_path = f"{cls._storage_path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, cls._storage_path)
        except Exception:
            pass

    @classmethod
    def _check_and_rollover(cls, now_date: str | None = None) -> None:
        """미국 태평양 표준시(PT) 자정(한국 16:00/17:00 KST) 날짜 변경 시 일일 카운터 자동 초기화 (락 내부 호출)"""
        cls._ensure_configured_locked()
        today = now_date or get_pt_today_str()
        if today != cls._current_date:
            cls._current_date = today
            cls._api_calls = 0
            cls._api_success = 0
            cls._rate_limited = 0
            cls._http_errors = 0
            cls._list_models_calls = 0
            cls._local_fallback = 0
            cls._cache_hits = 0
            cls._by_model = {}
            cls._save_state_locked()

    @classmethod
    def record_cache_hit(cls, market: str) -> None:
        with cls._lock:
            cls._check_and_rollover()
            cls._cache_hits += 1
            cls._last_event_at = time.time()
            cls._last_event = f"{market} cache hit"
            cls._save_state_locked()

    @classmethod
    def record_http_attempt(
        cls,
        model: str,
        context: str,
        endpoint: str = "generate_content",
        status_code: int | None = None,
        error_kind: str = "",
    ) -> None:
        """Google AI Studio와 동일하게 모든 HTTP 시도를 실제 모델 ID별로 집계"""
        with cls._lock:
            cls._check_and_rollover()
            cls._api_calls += 1

            if endpoint == "list_models":
                cls._list_models_calls += 1
                model_key = LIST_MODELS_KEY
            else:
                model_key = (model or "unknown").strip() or "unknown"

            stat = cls._ensure_model_stat_locked(model_key)
            stat["calls"] += 1

            if status_code == 200:
                cls._api_success += 1
                stat["success"] += 1
            elif status_code == 429:
                cls._rate_limited += 1
                stat["rate_limited"] += 1
            else:
                cls._http_errors += 1
                stat["errors"] += 1

            cls._last_event_at = time.time()
            if endpoint == "list_models":
                status_label = str(status_code) if status_code is not None else (error_kind or "error")
                cls._last_event = f"{context} list_models HTTP {status_label}"
            else:
                status_label = str(status_code) if status_code is not None else (error_kind or "error")
                cls._last_event = f"{context} {model_key} HTTP {status_label}"
            cls._save_state_locked()

    @classmethod
    def record_api_success(cls, model: str, market: str) -> None:
        """하위 호환 래퍼 (성공 HTTP 1회)"""
        cls.record_http_attempt(model, market, "generate_content", 200)

    @classmethod
    def record_rate_limited(cls, model: str, market: str) -> None:
        """하위 호환 래퍼 (429 HTTP 1회)"""
        cls.record_http_attempt(model, market, "generate_content", 429)

    @classmethod
    def record_local_fallback(cls, market: str, reason: str) -> None:
        with cls._lock:
            cls._check_and_rollover()
            cls._local_fallback += 1
            cls._last_event_at = time.time()
            cls._last_event = f"{market} local fallback: {reason[:120]}"
            cls._save_state_locked()

    @classmethod
    def snapshot(cls) -> GeminiTelemetrySnapshot:
        with cls._lock:
            cls._ensure_configured_locked()
            cls._check_and_rollover()
            cls._load_state_locked()
            return GeminiTelemetrySnapshot(
                date=cls._current_date,
                api_calls=cls._api_calls,
                api_success=cls._api_success,
                rate_limited=cls._rate_limited,
                http_errors=cls._http_errors,
                list_models_calls=cls._list_models_calls,
                local_fallback=cls._local_fallback,
                cache_hits=cls._cache_hits,
                last_event_at=cls._last_event_at,
                last_event=cls._last_event,
                quota_limit=cls._quota_limit,
                by_model=dict(cls._by_model),
                reset_info=get_pt_reset_info(),
            )

    @classmethod
    def reset(cls, persist: bool = False) -> None:
        """메모리 카운터 리셋. persist=True일 때만 디스크 저장(운영 환경), 테스트는 기본값 persist=False로 실데이터 파일 보존"""
        with cls._lock:
            cls._current_date = get_pt_today_str()
            cls._api_calls = 0
            cls._api_success = 0
            cls._rate_limited = 0
            cls._http_errors = 0
            cls._list_models_calls = 0
            cls._local_fallback = 0
            cls._cache_hits = 0
            cls._last_event_at = 0.0
            cls._last_event = ""
            cls._by_model = {}
            if persist:
                cls._ensure_configured_locked()
                cls._save_state_locked()
