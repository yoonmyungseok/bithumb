"""Small, dependency-free heartbeat health checks for watchdog processes."""

from __future__ import annotations

import json
import os
import time


def get_heartbeat_health(path: str, now: float | None = None) -> tuple[bool, str, float | None]:
    """Return whether a heartbeat is readable and its age without masking failures."""
    current = time.time() if now is None else now
    if not os.path.exists(path):
        return False, "하트비트 파일 없음", None
    try:
        with open(path, "r", encoding="utf-8") as file:
            timestamp = float(json.load(file).get("timestamp", 0.0))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return False, f"하트비트 파일 읽기 실패: {exc}", None
    if timestamp <= 0.0:
        return False, "하트비트 timestamp 누락 또는 무효", None
    return True, "OK", max(0.0, current - timestamp)
