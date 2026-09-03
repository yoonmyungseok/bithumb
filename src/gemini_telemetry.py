"""Thread-safe Gemini API usage telemetry for operations monitoring."""

from __future__ import annotations

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
    """Process-wide Gemini call counters shared across analyzer instances."""

    _lock = threading.Lock()
    _current_date = get_kst_today_str()
    _api_calls = 0
    _api_success = 0
    _rate_limited = 0
    _local_fallback = 0
    _cache_hits = 0
    _last_event_at = 0.0
    _last_event = ""
    _quota_limit = int(os.getenv("GEMINI_DAILY_QUOTA_LIMIT", "1500"))

    @classmethod
    def _check_and_rollover(cls, now_date: str | None = None) -> None:
        """KST 자정 날짜 변경 시 일일 카운터 자동 초기화 (락 내부 호출)"""
        today = now_date or get_kst_today_str()
        if today != cls._current_date:
            cls._current_date = today
            cls._api_calls = 0
            cls._api_success = 0
            cls._rate_limited = 0
            cls._local_fallback = 0
            cls._cache_hits = 0

    @classmethod
    def record_cache_hit(cls, market: str) -> None:
        with cls._lock:
            cls._check_and_rollover()
            cls._cache_hits += 1
            cls._last_event_at = time.time()
            cls._last_event = f"{market} cache hit"

    @classmethod
    def record_api_success(cls, model: str, market: str) -> None:
        with cls._lock:
            cls._check_and_rollover()
            cls._api_calls += 1
            cls._api_success += 1
            cls._last_event_at = time.time()
            cls._last_event = f"{market} success via {model}"

    @classmethod
    def record_rate_limited(cls, model: str, market: str) -> None:
        with cls._lock:
            cls._check_and_rollover()
            cls._api_calls += 1
            cls._rate_limited += 1
            cls._last_event_at = time.time()
            cls._last_event = f"{market} rate limited on {model}"

    @classmethod
    def record_local_fallback(cls, market: str, reason: str) -> None:
        with cls._lock:
            cls._check_and_rollover()
            cls._local_fallback += 1
            cls._last_event_at = time.time()
            cls._last_event = f"{market} local fallback: {reason[:120]}"

    @classmethod
    def snapshot(cls) -> GeminiTelemetrySnapshot:
        with cls._lock:
            cls._check_and_rollover()
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
    def reset(cls) -> None:
        with cls._lock:
            cls._current_date = get_kst_today_str()
            cls._api_calls = 0
            cls._api_success = 0
            cls._rate_limited = 0
            cls._local_fallback = 0
            cls._cache_hits = 0
            cls._last_event_at = 0.0
            cls._last_event = ""
