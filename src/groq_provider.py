"""Groq API를 활용한 초저지연 거시 시장 분석 Provider 경계입니다.

- 외부 추가 라이브러리 없이 requests를 통해 Groq의 OpenAI 호환 엔드포인트를 호출합니다.
- API 키 및 비밀정보는 로그와 결과 객체에 절대 노출하지 않습니다.
- JSON 모드와 스키마 검증, 그리고 모델 순차 폴백(Llama 3.3 70B -> Llama 3.1 8B)을 지원합니다.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import requests

logger = logging.getLogger(__name__)

DEFAULT_GROQ_MODELS = [
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "qwen/qwen3.8-27b",
]


@dataclass(frozen=True)
class GroqResult:
    """API 키와 원문 전체를 노출하지 않는 안전한 Groq 호출 결과입니다."""

    value: dict[str, Any] | str | None
    model: str
    success: bool
    latency_sec: float
    error_kind: str = ""
    status_code: int | None = None


class GroqProvider:
    """OpenAI 호환 규격의 Groq API를 호출하는 경량 Provider입니다."""

    API_URL = "https://api.groq.com/openai/v1/chat/completions"

    def __init__(
        self,
        api_key: str | None = None,
        exchange_scope: str = "bithumb",
        models: list[str] | None = None,
        timeout: float = 8.0,
    ) -> None:
        self.exchange_scope = str(exchange_scope).lower()
        self.timeout = float(timeout)
        
        custom_model = os.getenv("GROQ_MODEL", "").strip()
        if models:
            self.models = list(models)
        elif custom_model:
            self.models = [custom_model] + [m for m in DEFAULT_GROQ_MODELS if m != custom_model]
        else:
            self.models = list(DEFAULT_GROQ_MODELS)

        # 거래소별 전용 키 엄격 조회 (공용 키 폴백 금지 - 키 격리 준수)
        resolved_key = (api_key or "").strip()
        if not resolved_key:
            if self.exchange_scope == "upbit":
                resolved_key = os.getenv("UPBIT_GROQ_API_KEY", "").strip()
            else:
                resolved_key = os.getenv("BITHUMB_GROQ_API_KEY", "").strip()
        self._api_key = resolved_key

        # 운영 통계 (메모리)
        self.total_calls = 0
        self.success_calls = 0
        self.failed_calls = 0
        self.last_call_ts = 0.0
        self.last_latency_sec = 0.0
        self.last_error = ""

    @property
    def is_available(self) -> bool:
        """API 키가 설정되어 호출 가능한 상태인지 확인합니다."""
        return bool(self._api_key)

    def complete_json(
        self,
        prompt: str,
        *,
        system_instruction: str = "You are a professional crypto quant analyst. Always respond in valid JSON.",
        models: list[str] | None = None,
        schema: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> GroqResult:
        """JSON 모드로 Groq를 호출하고 파싱된 dict를 반환합니다."""
        if not self.is_available:
            return GroqResult(
                value=None,
                model="",
                success=False,
                latency_sec=0.0,
                error_kind="API_KEY_MISSING",
            )

        candidate_models = models or self.models
        req_timeout = float(timeout if timeout is not None else self.timeout)
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        last_status: int | None = None
        last_error_kind = ""

        for model in candidate_models:
            start_ts = time.time()
            self.total_calls += 1
            self.last_call_ts = start_ts

            payload: dict[str, Any] = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": prompt},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.2,
                "max_tokens": 1024,
            }

            try:
                resp = requests.post(
                    self.API_URL,
                    headers=headers,
                    json=payload,
                    timeout=req_timeout,
                )
                latency = time.time() - start_ts
                self.last_latency_sec = latency
                last_status = resp.status_code

                if resp.status_code == 200:
                    data = resp.json()
                    choices = data.get("choices", [])
                    if not choices:
                        last_error_kind = "EMPTY_CHOICES"
                        self.failed_calls += 1
                        continue

                    raw_content = choices[0].get("message", {}).get("content", "")
                    try:
                        parsed_json = json.loads(raw_content)
                    except json.JSONDecodeError:
                        last_error_kind = "JSON_PARSE_ERROR"
                        self.failed_calls += 1
                        continue

                    # 스키마 필수 필드 검증 (제공된 경우)
                    if schema and isinstance(parsed_json, dict):
                        required_fields = schema.get("required", [])
                        if not all(field in parsed_json for field in required_fields):
                            last_error_kind = "SCHEMA_VALIDATION_FAILED"
                            self.failed_calls += 1
                            continue

                    self.success_calls += 1
                    self.last_error = ""
                    return GroqResult(
                        value=parsed_json,
                        model=model,
                        success=True,
                        latency_sec=latency,
                        status_code=resp.status_code,
                    )
                elif resp.status_code == 404:
                    last_error_kind = "MODEL_NOT_FOUND"
                    logger.warning(f"Groq API 404 Model Not Found (모델 '{model}' 미지원/종료) -> 다음 모델 폴백")
                elif resp.status_code == 429:
                    last_error_kind = "RATE_LIMIT_EXCEEDED"
                    logger.warning(f"Groq API 429 Rate Limit (모델: {model}) -> 다음 모델 폴백")
                else:
                    last_error_kind = f"HTTP_{resp.status_code}"
                    logger.warning(f"Groq API 오류 HTTP {resp.status_code} (모델: {model})")

                self.failed_calls += 1

            except requests.Timeout:
                last_error_kind = "TIMEOUT"
                self.failed_calls += 1
                logger.warning(f"Groq API 타임아웃 ({req_timeout}s, 모델: {model})")
            except Exception as exc:
                last_error_kind = "NETWORK_ERROR"
                self.failed_calls += 1
                logger.warning(f"Groq API 호출 예외 ({model}): {exc}")

        self.last_error = last_error_kind
        return GroqResult(
            value=None,
            model=candidate_models[-1] if candidate_models else "",
            success=False,
            latency_sec=time.time() - self.last_call_ts,
            error_kind=last_error_kind,
            status_code=last_status,
        )

    def get_stats(self) -> dict[str, Any]:
        """외부 모니터링을 위한 호출 통계를 반환합니다."""
        return {
            "available": self.is_available,
            "total_calls": self.total_calls,
            "success_calls": self.success_calls,
            "failed_calls": self.failed_calls,
            "last_call_ts": self.last_call_ts,
            "last_latency_sec": round(self.last_latency_sec, 3),
            "last_error": self.last_error,
        }
