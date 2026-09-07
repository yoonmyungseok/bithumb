"""빗썸/업비트 AI 통신을 분석 로직에서 분리하는 Provider 경계입니다."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import requests

from gemini_telemetry import GeminiTelemetry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderResult:
    """키·원문 응답을 보관하지 않는 Provider 호출 결과입니다."""

    value: dict[str, Any] | list[Any] | str | None
    model: str
    error_kind: str = ""


class AIProvider(Protocol):
    """분석기가 의존하는 최소 AI 호출 계약입니다."""

    name: str
    exchange: str
    is_entry_fail_closed: bool

    def models_for(self, purpose: str) -> list[str]: ...

    def complete_json(
        self, prompt: str, models: list[str], schema: dict[str, Any], *, context: str, timeout: float, max_tokens: int
    ) -> ProviderResult: ...

    def complete_text(
        self, prompt: str, models: list[str], *, context: str, timeout: float, max_tokens: int
    ) -> ProviderResult: ...


class AIProviderTelemetry:
    """Provider·모델·거래소별 호출량과 Provider가 반환한 쿼터 리셋 시각을 영속화합니다."""

    _lock = threading.RLock()
    _stats: dict[tuple[str, str, str], dict[str, Any]] = {}
    _storage_path = ""
    _configured = False
    _current_date = ""
    _reset_at = 0.0
    _reset_remaining_raw = ""
    _reset_header_at = 0.0

    @classmethod
    def _today_kst(cls) -> str:
        """대시보드 기록 날짜 표기는 운영 기준인 한국 시간을 사용한다."""
        return datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d")

    @classmethod
    def configure(cls, data_dir: str | None = None, storage_filename: str = "groq_telemetry.json") -> None:
        """빗썸 데이터 경로를 바인딩하고 Groq가 준 쿼터 창의 누적 사용량을 복원한다."""
        with cls._lock:
            if data_dir:
                cls._storage_path = os.path.join(data_dir, storage_filename)
            else:
                project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
                cls._storage_path = os.path.join(project_root, "data", "groq_telemetry.json")
            cls._configured = True
            cls._current_date = cls._today_kst()
            cls._reset_at = 0.0
            cls._reset_remaining_raw = ""
            cls._reset_header_at = 0.0
            # 재시작 복원과 테스트 격리에서 이전 메모리 값을 섞지 않는다.
            cls._stats = {}
            cls._load_state_locked()

    @classmethod
    def _ensure_configured_locked(cls) -> None:
        """직접 호출 경로도 기본 빗썸 저장소를 사용하도록 보장한다."""
        if not cls._configured:
            cls.configure()

    @classmethod
    def _load_state_locked(cls) -> None:
        """KST 자정이 아닌 Groq 응답 헤더의 쿼터 창을 기준으로 기록을 복원한다."""
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
                    "latency_total_ms": max(0.0, float(item.get("latency_total_ms", 0.0))),
                    "last_event": str(item.get("last_event", ""))[:200],
                    "last_event_at": max(0.0, float(item.get("last_event_at", 0.0))),
                }
            cls._stats = restored
            cls._reset_at = max(0.0, float(payload.get("reset_at", 0.0)))
            cls._reset_remaining_raw = str(payload.get("reset_remaining_raw", ""))[:64]
            cls._reset_header_at = max(0.0, float(payload.get("reset_header_at", 0.0)))
            cls._check_header_reset_locked()
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            # 계측 파일 손상은 주문 흐름에 영향을 주지 않고 빈 관측값으로 안전하게 시작한다.
            cls._stats = {}

    @classmethod
    def _save_state_locked(cls) -> None:
        """교체 가능한 임시 파일을 사용해 재시작 중에도 계측 파일 손상을 막는다."""
        if not cls._storage_path:
            return
        try:
            os.makedirs(os.path.dirname(cls._storage_path), exist_ok=True)
            stats = [
                {
                    "provider": provider, "exchange": exchange, "model": model,
                    "calls": stat["calls"], "success": stat["success"],
                    "rate_limited": stat["rate_limited"], "errors": stat["errors"],
                    "latency_total_ms": stat["latency_total_ms"],
                    "last_event": stat["last_event"], "last_event_at": stat["last_event_at"],
                }
                for (provider, exchange, model), stat in cls._stats.items()
            ]
            temporary_path = f"{cls._storage_path}.tmp"
            with open(temporary_path, "w", encoding="utf-8") as file:
                json.dump({
                    "date": cls._current_date,
                    "reset_at": cls._reset_at,
                    "reset_remaining_raw": cls._reset_remaining_raw,
                    "reset_header_at": cls._reset_header_at,
                    "stats": stats,
                }, file, ensure_ascii=False, indent=2)
            os.replace(temporary_path, cls._storage_path)
        except OSError:
            # 사용량 관측 저장 실패는 AI 호출이나 거래 주문을 막지 않는다.
            logger.debug("AI Provider 텔레메트리 저장 실패: %s", cls._storage_path, exc_info=True)

    @classmethod
    def _check_header_reset_locked(cls) -> None:
        """Groq가 알려 준 RPD 리셋 시각이 지난 경우에만 누적 관측값을 초기화한다."""
        if cls._reset_at > 0.0 and time.time() >= cls._reset_at:
            cls._current_date = cls._today_kst()
            cls._stats = {}
            cls._reset_at = 0.0
            cls._reset_remaining_raw = ""
            cls._reset_header_at = 0.0
            cls._save_state_locked()

    @staticmethod
    def _parse_reset_seconds(value: str) -> float | None:
        """Groq 헤더의 `1h2m3.45s` 형식을 초 단위로 보수적으로 변환한다."""
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
            if persist:
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
            cls._save_state_locked()

    @classmethod
    def snapshot(cls, exchange: str) -> dict[str, Any]:
        """대시보드 확장용 읽기 전용 집계를 반환합니다."""
        with cls._lock:
            cls._ensure_configured_locked()
            cls._check_header_reset_locked()
            models: dict[str, dict[str, Any]] = {}
            total = {"api_calls": 0, "api_success": 0, "rate_limited": 0, "http_errors": 0, "latency_total_ms": 0.0}
            providers: set[str] = set()
            last_event = ""
            last_event_at = 0.0
            for (provider, stat_exchange, model), stat in cls._stats.items():
                if stat_exchange != exchange:
                    continue
                providers.add(provider)
                calls = int(stat["calls"])
                models[model] = {
                    "provider": provider, "calls": calls, "success": int(stat["success"]),
                    "rate_limited": int(stat["rate_limited"]), "errors": int(stat["errors"]),
                    "avg_latency_ms": round(float(stat["latency_total_ms"]) / calls, 1) if calls else 0.0,
                }
                total["api_calls"] += calls
                total["api_success"] += int(stat["success"])
                total["rate_limited"] += int(stat["rate_limited"])
                total["http_errors"] += int(stat["errors"])
                total["latency_total_ms"] += float(stat["latency_total_ms"])
                if float(stat["last_event_at"]) >= last_event_at:
                    last_event_at = float(stat["last_event_at"])
                    last_event = str(stat["last_event"])
            calls = total["api_calls"]
            remaining_seconds = max(0, int(round(cls._reset_at - time.time()))) if cls._reset_at else 0
            reset_time_kst = (
                datetime.fromtimestamp(cls._reset_at, ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S KST")
                if cls._reset_at else ""
            )
            return {
                "date": cls._current_date,
                "provider": ",".join(sorted(providers)) or "unconfigured", "exchange": exchange,
                "api_calls": calls, "api_success": total["api_success"], "rate_limited": total["rate_limited"],
                "http_errors": total["http_errors"], "avg_latency_ms": round(total["latency_total_ms"] / calls, 1) if calls else 0.0,
                "models": models, "last_event": last_event, "last_event_at": last_event_at,
                "reset_info": {
                    "source": "x-ratelimit-reset-requests" if cls._reset_at else "",
                    "raw": cls._reset_remaining_raw,
                    "reset_at": cls._reset_at,
                    "reset_time_kst": reset_time_kst,
                    "remaining_seconds": remaining_seconds,
                },
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
    """Groq strict schema 응답도 로컬에서 핵심 타입·필수 필드를 재검증합니다."""
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
    """업비트 기존 Gemini 통신 형식을 유지하는 호환 Provider입니다."""

    name = "gemini"
    exchange = "upbit"
    is_entry_fail_closed = False

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

    def complete_json(self, prompt: str, models: list[str], schema: dict[str, Any], *, context: str, timeout: float, max_tokens: int) -> ProviderResult:
        """기존 Google API 요청 구조를 보존해 업비트 호출 계약을 유지합니다."""
        for model in models:
            try:
                response = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.api_key}",
                    json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {
                        "temperature": 0.1, "topP": 0.8, "maxOutputTokens": max_tokens, "responseMimeType": "application/json",
                    }}, timeout=timeout,
                )
                GeminiTelemetry.record_http_attempt(model, context, "generate_content", response.status_code)
                if response.status_code == 200:
                    value = _parse_json_text(self._extract_text(response.json()) or "")
                    if value is not None:
                        return ProviderResult(value, model)
                    return ProviderResult(None, model, "invalid_json")
                return ProviderResult(None, model, "rate_limited" if response.status_code == 429 else "http_error")
            except requests.exceptions.Timeout:
                GeminiTelemetry.record_http_attempt(model, context, "generate_content", None, "timeout")
                return ProviderResult(None, model, "timeout")
            except (requests.exceptions.RequestException, ValueError, KeyError, IndexError):
                GeminiTelemetry.record_http_attempt(model, context, "generate_content", None, "exception")
                return ProviderResult(None, model, "exception")

        return ProviderResult(None, "", "no_model")

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


class GroqProvider:
    """빗썸 전용 Groq Provider; FAST 실패는 신규 매수에 로컬 BUY를 허용하지 않습니다."""

    name = "groq"
    exchange = "bithumb"
    is_entry_fail_closed = True
    FAST_TRADING = "openai/gpt-oss-20b"
    DEEP_BRIEFING = "openai/gpt-oss-120b"

    def __init__(self, api_key: str, fast_model: str, deep_model: str):
        self.api_key = (api_key or "").strip()
        self.fast_model = (fast_model or "").strip()
        self.deep_model = (deep_model or "").strip()

    @property
    def is_configured(self) -> bool:
        """고정 모델 매핑과 자격 증명이 모두 있을 때만 분석을 허용합니다."""
        return bool(self.api_key and self.fast_model == self.FAST_TRADING and self.deep_model == self.DEEP_BRIEFING)

    def models_for(self, purpose: str) -> list[str]:
        return [self.deep_model] if purpose == "briefing" else [self.fast_model]

    def _post(self, model: str, payload: dict[str, Any], context: str, timeout: float) -> ProviderResult:
        started = time.monotonic()
        status_code: int | None = None
        error_kind = ""
        reset_requests = ""
        try:
            response = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json=payload, timeout=timeout,
            )
            status_code = response.status_code
            # Groq 공식 헤더는 RPD 쿼터 창이 리셋되기까지의 실제 남은 시간을 제공한다.
            reset_requests = str(response.headers.get("x-ratelimit-reset-requests", ""))
            if status_code != 200:
                error_kind = "rate_limited" if status_code == 429 else "http_error"
                return ProviderResult(None, model, error_kind)
            data = response.json()
            choices = data.get("choices", [])
            content = choices[0].get("message", {}).get("content") if choices and isinstance(choices[0], dict) else None
            if not isinstance(content, str) or not content.strip():
                error_kind = "invalid_response"
                return ProviderResult(None, model, error_kind)
            return ProviderResult(content.strip(), model)
        except requests.exceptions.Timeout:
            error_kind = "timeout"
            return ProviderResult(None, model, error_kind)
        except (requests.exceptions.RequestException, ValueError, KeyError, IndexError, TypeError):
            error_kind = "exception"
            return ProviderResult(None, model, error_kind)
        finally:
            AIProviderTelemetry.record(
                self.name, self.exchange, model, context, status_code,
                (time.monotonic() - started) * 1000.0, error_kind, reset_requests,
            )

    def complete_json(self, prompt: str, models: list[str], schema: dict[str, Any], *, context: str, timeout: float, max_tokens: int) -> ProviderResult:
        """Groq JSON Schema 강제와 로컬 재검증을 모두 적용합니다."""
        model = self.fast_model
        if not self.is_configured or models != [model]:
            return ProviderResult(None, model, "configuration")
        result = self._post(model, {
            "model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.1,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_schema", "json_schema": {"name": "bithumb_trading_result", "strict": True, "schema": schema}},
        }, context, timeout)
        if not isinstance(result.value, str):
            return result
        value = _parse_json_text(result.value)
        if value is None:
            return ProviderResult(None, model, "invalid_json")
        if not _validate_schema(value, schema):
            return ProviderResult(None, model, "schema")
        return ProviderResult(value, model)

    def complete_text(self, prompt: str, models: list[str], *, context: str, timeout: float, max_tokens: int) -> ProviderResult:
        """120B 브리핑 실패 시에만 20B 요약 브리핑으로 단일 폴백합니다."""
        if not self.is_configured or models != [self.deep_model]:
            return ProviderResult(None, self.deep_model, "configuration")
        deep_result = self._post(self.deep_model, {
            "model": self.deep_model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.2, "max_tokens": max_tokens,
        }, context, timeout)
        if isinstance(deep_result.value, str):
            return deep_result
        return self._post(self.fast_model, {
            "model": self.fast_model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.2, "max_tokens": max_tokens,
        }, f"{context}_fast_fallback", timeout)
