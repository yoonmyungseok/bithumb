"""
거래소 REST API 호출량 및 쿼터 계측 모듈 (Exchange API Telemetry)
- 빗썸 및 업비트 REST API 호출 횟수, 상태 코드, Rate limit(429), 잔여 쿼터 계측
- KST(한국 표준시) 자정 기준 일일 카운터 자동 롤오버
- 멀티스레드 환경에서 안전한 락(threading.Lock) 기반 동기화
"""

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
class ExchangeApiTelemetrySnapshot:
    """특정 시점의 거래소 API 호출 텔레메트리 스냅샷"""

    exchange: str
    date: str
    total_calls: int
    by_method: dict[str, int]
    by_group: dict[str, int]
    status_codes: dict[int, int]
    rate_limited_429: int
    errors: int
    remaining_sec: int | None
    remaining_min: int | None
    last_call_at: float
    last_endpoint: str
    last_status: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "exchange": self.exchange,
            "date": self.date,
            "total_calls": self.total_calls,
            "by_method": dict(self.by_method),
            "by_group": dict(self.by_group),
            "status_codes": {str(k): v for k, v in self.status_codes.items()},
            "rate_limited_429": self.rate_limited_429,
            "errors": self.errors,
            "remaining_sec": self.remaining_sec,
            "remaining_min": self.remaining_min,
            "last_call_at": self.last_call_at,
            "last_endpoint": self.last_endpoint,
            "last_status": self.last_status,
        }


class ExchangeApiTelemetry:
    """
    거래소별 REST API 호출 카운터 및 쿼터 추적기.
    자정(KST)이 지나면 자동으로 일일 카운터가 리셋됩니다.
    동일한 exchange_name에 대해 프로세스 내 단일 인스턴스를 공유하며, 디스크에 영속 저장됩니다.
    """

    _instances: dict[str, "ExchangeApiTelemetry"] = {}
    _registry_lock = threading.Lock()

    def __new__(cls, exchange_name: str, data_dir: str | None = None) -> "ExchangeApiTelemetry":
        key = exchange_name.lower()
        with cls._registry_lock:
            if key not in cls._instances:
                inst = super().__new__(cls)
                inst._initialized = False
                cls._instances[key] = inst
            return cls._instances[key]

    def __init__(self, exchange_name: str, data_dir: str | None = None):
        if getattr(self, "_initialized", False):
            if data_dir and not getattr(self, "_custom_data_dir", False):
                with self._lock:
                    self._storage_path = os.path.join(data_dir, "api_telemetry.json")
                    self._custom_data_dir = True
                    self._load_state()
            return
        self.exchange_name = exchange_name.lower()
        self._lock = threading.Lock()
        self._current_date = get_kst_today_str()
        self._storage_path = self._resolve_storage_path(data_dir)
        self._custom_data_dir = bool(data_dir)
        self._total_calls = 0
        self._by_method: dict[str, int] = {"GET": 0, "POST": 0, "DELETE": 0}
        self._by_group: dict[str, int] = {}
        self._status_codes: dict[int, int] = {}
        self._rate_limited_429 = 0
        self._errors = 0
        self._remaining_sec: int | None = None
        self._remaining_min: int | None = None
        self._last_call_at = 0.0
        self._last_endpoint = ""
        self._last_status = 0
        self._load_state()
        self._initialized = True

    def _resolve_storage_path(self, data_dir: str | None = None) -> str:
        if data_dir:
            return os.path.join(data_dir, "api_telemetry.json")
        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        if self.exchange_name == "upbit":
            base = os.path.join(project_root, "data", "upbit")
        else:
            base = os.path.join(project_root, "data")
        return os.path.join(base, "api_telemetry.json")

    def _load_state(self) -> None:
        """디스크에서 당일(KST) 텔레메트리 복원"""
        if not self._storage_path or not os.path.exists(self._storage_path):
            return
        try:
            with open(self._storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            saved_date = data.get("date", "")
            if saved_date == self._current_date:
                self._total_calls = int(data.get("total_calls", 0))
                self._by_method = {str(k): int(v) for k, v in data.get("by_method", {}).items()}
                self._by_group = {str(k): int(v) for k, v in data.get("by_group", {}).items()}
                self._status_codes = {int(k): int(v) for k, v in data.get("status_codes", {}).items() if str(k).isdigit()}
                self._rate_limited_429 = int(data.get("rate_limited_429", 0))
                self._errors = int(data.get("errors", 0))
                self._last_call_at = float(data.get("last_call_at", 0.0))
                self._last_endpoint = str(data.get("last_endpoint", ""))
                self._last_status = int(data.get("last_status", 0))
        except Exception:
            pass

    def _save_state(self) -> None:
        """원자적(atomic) 파일 쓰기로 디스크에 텔레메트리 영속화"""
        if not self._storage_path:
            return
        try:
            os.makedirs(os.path.dirname(self._storage_path), exist_ok=True)
            payload = {
                "exchange": self.exchange_name,
                "date": self._current_date,
                "total_calls": self._total_calls,
                "by_method": self._by_method,
                "by_group": self._by_group,
                "status_codes": self._status_codes,
                "rate_limited_429": self._rate_limited_429,
                "errors": self._errors,
                "remaining_sec": self._remaining_sec,
                "remaining_min": self._remaining_min,
                "last_call_at": self._last_call_at,
                "last_endpoint": self._last_endpoint,
                "last_status": self._last_status,
            }
            tmp_path = f"{self._storage_path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self._storage_path)
        except Exception:
            pass

    def _check_and_rollover(self, now_date: str | None = None) -> None:
        """KST 날짜가 변경되었을 경우 일일 카운터를 0으로 롤오버 (락 내부에서 호출)"""
        today = now_date or get_kst_today_str()
        if today != self._current_date:
            self._current_date = today
            self._total_calls = 0
            self._by_method = {"GET": 0, "POST": 0, "DELETE": 0}
            self._by_group = {}
            self._status_codes = {}
            self._rate_limited_429 = 0
            self._errors = 0
            self._save_state()

    def record_call(
        self,
        method: str,
        endpoint: str,
        status_code: int = 200,
        group: str = "",
        remaining_sec: int | None = None,
        remaining_min: int | None = None,
        is_error: bool = False,
    ) -> None:
        """REST API 호출 결과 기록"""
        method_upper = method.upper()
        now_ts = time.time()

        with self._lock:
            self._check_and_rollover()

            self._total_calls += 1
            self._by_method[method_upper] = self._by_method.get(method_upper, 0) + 1

            if group:
                self._by_group[group] = self._by_group.get(group, 0) + 1

            if status_code > 0:
                self._status_codes[status_code] = self._status_codes.get(status_code, 0) + 1

            if status_code == 429:
                self._rate_limited_429 += 1

            if is_error or (status_code >= 400 and status_code != 429):
                self._errors += 1

            if remaining_sec is not None:
                self._remaining_sec = remaining_sec
            if remaining_min is not None:
                self._remaining_min = remaining_min

            self._last_call_at = now_ts
            self._last_endpoint = endpoint
            self._last_status = status_code
            self._save_state()

    def snapshot(self) -> ExchangeApiTelemetrySnapshot:
        """현재 계측치 불변 스냅샷 반환"""
        with self._lock:
            self._check_and_rollover()
            return ExchangeApiTelemetrySnapshot(
                exchange=self.exchange_name,
                date=self._current_date,
                total_calls=self._total_calls,
                by_method=dict(self._by_method),
                by_group=dict(self._by_group),
                status_codes=dict(self._status_codes),
                rate_limited_429=self._rate_limited_429,
                errors=self._errors,
                remaining_sec=self._remaining_sec,
                remaining_min=self._remaining_min,
                last_call_at=self._last_call_at,
                last_endpoint=self._last_endpoint,
                last_status=self._last_status,
            )

    def to_dict(self) -> dict[str, Any]:
        """스냅샷을 딕셔너리 형태로 반환"""
        return self.snapshot().to_dict()

    def reset(self) -> None:
        """테스트 및 관리자용 수동 초기화"""
        with self._lock:
            self._current_date = get_kst_today_str()
            self._total_calls = 0
            self._by_method = {"GET": 0, "POST": 0, "DELETE": 0}
            self._by_group = {}
            self._status_codes = {}
            self._rate_limited_429 = 0
            self._errors = 0
            self._remaining_sec = None
            self._remaining_min = None
            self._last_call_at = 0.0
            self._last_endpoint = ""
            self._last_status = 0
            self._save_state()
