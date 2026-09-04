"""Thread-safe Gemini API usage telemetry for operations monitoring."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any

KST = timezone(timedelta(hours=9))


def get_kst_today_str() -> str:
    """현재 KST 날짜 문자열 (YYYY-MM-DD) 반환"""
    return datetime.now(KST).strftime("%Y-%m-%d")


@dataclass(frozen=True)
class GeminiTelemetrySnapshot:
    """Point-in-time Gemini usage counters."""

    date: str
    api_calls: int
    api_success: int
    rate_limited: int
    local_fallback: int
    cache_hits: int
    last_event_at: float
    last_event: str
    quota_limit: int = 1500

    def to_dict(self) -> dict[str, Any]:
        used_pct = round((self.api_calls / max(1, self.quota_limit)) * 100.0, 1)
        return {
            "date": self.date,
            "api_calls": self.api_calls,
            "api_success": self.api_success,
            "rate_limited": self.rate_limited,
            "local_fallback": self.local_fallback,
            "cache_hits": self.cache_hits,
            "quota_limit": self.quota_limit,
            "quota_used_pct": used_pct,
            "success_rate_pct": round((self.api_success / self.api_calls) * 100.0, 1) if self.api_calls else 0.0,
            "fallback_rate_pct": round((self.local_fallback / max(1, self.api_calls)) * 100.0, 1),
            "last_event": self.last_event,
            "last_event_at": self.last_event_at,
        }


class GeminiTelemetry:
    """Process-wide Gemini call counters shared across analyzer instances with disk persistence."""

    _lock = threading.Lock()
    _current_date = get_kst_today_str()
    _api_calls = 0
    _api_success = 0
    _rate_limited = 0
    _local_fallback = 0
    _cache_hits = 0
    _last_event_at = 0.0
    _last_event = ""
    _quota_limit = int(os.getenv("GEMINI_DAILY_QUOTA_LIMIT", "500"))
    _storage_path: str | None = None
    _configured = False

    @classmethod
    def can_make_api_call(cls, for_emergency_exit: bool = False) -> bool:
        """
        일일 쿼터(500 RPD) 예산 가드:
        - 신규 매수 분석: 당일 호출수 < 450회 (90%) 일 때만 허용
        - 긴급 탈출/비상 대응: 당일 호출수 < 490회 (98%) 일 때까지 허용
        """
        with cls._lock:
            cls._ensure_configured_locked()
            cls._check_and_rollover()
            threshold = 490 if for_emergency_exit else 450
            return cls._api_calls < threshold

    @classmethod
    def get_daily_quota_budget(cls) -> dict[str, Any]:
        """당일 호출량 및 남은 쿼터 단계 정보 반환"""
        with cls._lock:
            cls._ensure_configured_locked()
            cls._check_and_rollover()
            calls = cls._api_calls
            limit = cls._quota_limit
            return {
                "api_calls": calls,
                "quota_limit": limit,
                "is_tight": calls >= 350,       # 70% 이상 시 사이클당 1개 종목으로 축소
                "is_critical": calls >= 450,    # 90% 이상 시 신규 매수 AI 전면 차단 (100% 로컬 퀀트)
                "is_exhausted": calls >= 490,   # 98% 이상 시 전면 차단 (Fail-soft)
            }

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
                cls._local_fallback = int(data.get("local_fallback", 0))
                cls._cache_hits = int(data.get("cache_hits", 0))
                cls._last_event_at = float(data.get("last_event_at", 0.0))
                cls._last_event = str(data.get("last_event", ""))
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
                "local_fallback": cls._local_fallback,
                "cache_hits": cls._cache_hits,
                "quota_limit": cls._quota_limit,
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
        """KST 자정 날짜 변경 시 일일 카운터 자동 초기화 (락 내부 호출)"""
        cls._ensure_configured_locked()
        today = now_date or get_kst_today_str()
        if today != cls._current_date:
            cls._current_date = today
            cls._api_calls = 0
            cls._api_success = 0
            cls._rate_limited = 0
            cls._local_fallback = 0
            cls._cache_hits = 0
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
    def record_api_success(cls, model: str, market: str) -> None:
        with cls._lock:
            cls._check_and_rollover()
            cls._api_calls += 1
            cls._api_success += 1
            cls._last_event_at = time.time()
            cls._last_event = f"{market} success via {model}"
            cls._save_state_locked()

    @classmethod
    def record_rate_limited(cls, model: str, market: str) -> None:
        with cls._lock:
            cls._check_and_rollover()
            cls._api_calls += 1
            cls._rate_limited += 1
            cls._last_event_at = time.time()
            cls._last_event = f"{market} rate limited on {model}"
            cls._save_state_locked()

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
                local_fallback=cls._local_fallback,
                cache_hits=cls._cache_hits,
                last_event_at=cls._last_event_at,
                last_event=cls._last_event,
                quota_limit=cls._quota_limit,
            )

    @classmethod
    def reset(cls, persist: bool = False) -> None:
        """메모리 카운터 리셋. persist=True일 때만 디스크 저장(운영 환경), 테스트는 기본값 persist=False로 실데이터 파일 보존"""
        with cls._lock:
            cls._current_date = get_kst_today_str()
            cls._api_calls = 0
            cls._api_success = 0
            cls._rate_limited = 0
            cls._local_fallback = 0
            cls._cache_hits = 0
            cls._last_event_at = 0.0
            cls._last_event = ""
            if persist:
                cls._ensure_configured_locked()
                cls._save_state_locked()
