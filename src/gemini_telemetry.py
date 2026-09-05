"""Thread-safe Gemini API usage telemetry for operations monitoring with PT midnight reset and per-model quotas."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any
from zoneinfo import ZoneInfo

# 미국 태평양 표준시 (PT, PDT/PST 일광절약시간 자동 계산) & 한국 표준시 (KST)
PT_TZ = ZoneInfo("America/Los_Angeles")
KST_TZ = timezone(timedelta(hours=9))


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
    """다양한 모델 이름 표기를 3.5 / 3.1 Flash-Lite 표준 명칭으로 정규화"""
    lower = (model or "").lower()
    if "3.5" in lower:
        return "gemini-3.5-flash-lite"
    if "3.1" in lower:
        return "gemini-3.1-flash-lite"
    if "2.5" in lower:
        return "gemini-2.5-flash-lite"
    return "gemini-3.5-flash-lite"


@dataclass(frozen=True)
class GeminiTelemetrySnapshot:
    """Point-in-time Gemini usage counters with per-model breakdown."""

    date: str
    api_calls: int
    api_success: int
    rate_limited: int
    local_fallback: int
    cache_hits: int
    last_event_at: float
    last_event: str
    quota_limit: int = 1000
    by_model: dict[str, dict[str, int]] | None = None
    reset_info: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        used_pct = round((self.api_calls / max(1, self.quota_limit)) * 100.0, 1)

        # 모델별 500회 한도 대비 사용량 산출
        models_dict: dict[str, Any] = {}
        for m_name in ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite"):
            m_stat = (self.by_model or {}).get(m_name, {})
            m_calls = int(m_stat.get("calls", 0))
            m_limit = 500
            models_dict[m_name] = {
                "calls": m_calls,
                "success": int(m_stat.get("success", 0)),
                "rate_limited": int(m_stat.get("rate_limited", 0)),
                "quota_limit": m_limit,
                "quota_used_pct": round((m_calls / m_limit) * 100.0, 1),
            }

        return {
            "date": self.date,
            "api_calls": self.api_calls,
            "api_success": self.api_success,
            "rate_limited": self.rate_limited,
            "local_fallback": self.local_fallback,
            "cache_hits": self.cache_hits,
            "quota_limit": self.quota_limit,
            "quota_used_pct": used_pct,
            "models": models_dict,
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
    _local_fallback = 0
    _cache_hits = 0
    _last_event_at = 0.0
    _last_event = ""
    _quota_limit = int(os.getenv("GEMINI_DAILY_QUOTA_LIMIT", "1000"))
    _storage_path: str | None = None
    _configured = False
    _by_model: dict[str, dict[str, int]] = {
        "gemini-3.5-flash-lite": {"calls": 0, "success": 0, "rate_limited": 0},
        "gemini-3.1-flash-lite": {"calls": 0, "success": 0, "rate_limited": 0},
    }

    @classmethod
    def can_call_model(cls, model: str, for_emergency_exit: bool = False) -> bool:
        """특정 모델의 일일 쿼터(500 RPD) 개별 초과 여부 가드"""
        with cls._lock:
            cls._ensure_configured_locked()
            cls._check_and_rollover()
            c_name = canonical_model_name(model)
            m_stat = cls._by_model.get(c_name, {})
            m_calls = m_stat.get("calls", 0)
            threshold = 490 if for_emergency_exit else 450
            return m_calls < threshold

    @classmethod
    def can_make_api_call(cls, for_emergency_exit: bool = False) -> bool:
        """
        일일 쿼터(Flash-Lite 2개 모델 각 500회 = 총 1,000 RPD) 예산 가드:
        - 신규 매수 분석: 당일 총 호출수 < 900회 (90%) 일 때 허용
        - 긴급 탈출/비상 대응: 당일 총 호출수 < 980회 (98%) 일 때 허용
        """
        with cls._lock:
            cls._ensure_configured_locked()
            cls._check_and_rollover()
            threshold = int(cls._quota_limit * 0.98) if for_emergency_exit else int(cls._quota_limit * 0.90)
            return cls._api_calls < threshold

    @classmethod
    def get_daily_quota_budget(cls) -> dict[str, Any]:
        """당일 호출량 및 남은 쿼터 단계 정보 반환 (1,000회 기준)"""
        with cls._lock:
            cls._ensure_configured_locked()
            cls._check_and_rollover()
            calls = cls._api_calls
            limit = cls._quota_limit
            return {
                "api_calls": calls,
                "quota_limit": limit,
                "is_tight": calls >= int(limit * 0.70),       # 70% (700회) 이상 시 사이클당 1개 종목으로 축소
                "is_critical": calls >= int(limit * 0.90),    # 90% (900회) 이상 시 신규 매수 AI 전면 차단 (100% 로컬 퀀트)
                "is_exhausted": calls >= int(limit * 0.98),   # 98% (980회) 이상 시 전면 차단 (Fail-soft)
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

                # 모델별 데이터 복원
                raw_models = data.get("by_model") or data.get("models") or {}
                cls._by_model = {
                    "gemini-3.5-flash-lite": {
                        "calls": int(raw_models.get("gemini-3.5-flash-lite", {}).get("calls", 0)),
                        "success": int(raw_models.get("gemini-3.5-flash-lite", {}).get("success", 0)),
                        "rate_limited": int(raw_models.get("gemini-3.5-flash-lite", {}).get("rate_limited", 0)),
                    },
                    "gemini-3.1-flash-lite": {
                        "calls": int(raw_models.get("gemini-3.1-flash-lite", {}).get("calls", 0)),
                        "success": int(raw_models.get("gemini-3.1-flash-lite", {}).get("success", 0)),
                        "rate_limited": int(raw_models.get("gemini-3.1-flash-lite", {}).get("rate_limited", 0)),
                    },
                }
                # 만약 총 호출은 있는데 모델별 분할이 비어있는 경우 균등 배분 보정
                total_m_calls = sum(m["calls"] for m in cls._by_model.values())
                if cls._api_calls > 0 and total_m_calls == 0:
                    half = cls._api_calls // 2
                    cls._by_model["gemini-3.5-flash-lite"]["calls"] = cls._api_calls - half
                    cls._by_model["gemini-3.5-flash-lite"]["success"] = cls._api_calls - half
                    cls._by_model["gemini-3.1-flash-lite"]["calls"] = half
                    cls._by_model["gemini-3.1-flash-lite"]["success"] = half
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
            cls._local_fallback = 0
            cls._cache_hits = 0
            cls._by_model = {
                "gemini-3.5-flash-lite": {"calls": 0, "success": 0, "rate_limited": 0},
                "gemini-3.1-flash-lite": {"calls": 0, "success": 0, "rate_limited": 0},
            }
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
            c_name = canonical_model_name(model)
            if c_name not in cls._by_model:
                cls._by_model[c_name] = {"calls": 0, "success": 0, "rate_limited": 0}
            cls._by_model[c_name]["calls"] += 1
            cls._by_model[c_name]["success"] += 1
            cls._last_event_at = time.time()
            cls._last_event = f"{market} success via {model}"
            cls._save_state_locked()

    @classmethod
    def record_rate_limited(cls, model: str, market: str) -> None:
        with cls._lock:
            cls._check_and_rollover()
            cls._api_calls += 1
            cls._rate_limited += 1
            c_name = canonical_model_name(model)
            if c_name not in cls._by_model:
                cls._by_model[c_name] = {"calls": 0, "success": 0, "rate_limited": 0}
            cls._by_model[c_name]["calls"] += 1
            cls._by_model[c_name]["rate_limited"] += 1
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
            cls._local_fallback = 0
            cls._cache_hits = 0
            cls._last_event_at = 0.0
            cls._last_event = ""
            cls._by_model = {
                "gemini-3.5-flash-lite": {"calls": 0, "success": 0, "rate_limited": 0},
                "gemini-3.1-flash-lite": {"calls": 0, "success": 0, "rate_limited": 0},
            }
            if persist:
                cls._ensure_configured_locked()
                cls._save_state_locked()
