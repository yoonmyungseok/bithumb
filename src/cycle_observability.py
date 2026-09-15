"""매매 사이클 성능 계측과 Gemini 장애·파생 BUY 차단 관측을 분리한다.

주문 경로·API 계약은 변경하지 않고 로그·집계·텔레메트리 메타만 다룬다.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any

KST = timezone(timedelta(hours=9))

# run_cycle_prefix 내부에서 주문 대사·포트폴리오가 후보 스캔보다 먼저 실행되어야 한다.
CYCLE_PREFIX_PHASE_ORDER: tuple[str, ...] = (
    "reconcile",
    "portfolio",
    "regime",
    "market_selection",
)


def get_kst_today_str() -> str:
    """운영 대시보드·일일 집계에 쓰는 KST 거래일(YYYY-MM-DD)."""
    return datetime.now(KST).strftime("%Y-%m-%d")


def kst_datetime_from_epoch(epoch_sec: float) -> str:
    if epoch_sec <= 0:
        return ""
    return datetime.fromtimestamp(epoch_sec, tz=KST).strftime("%Y-%m-%d %H:%M:%S")


def assert_cycle_prefix_phase_order(completed_phases: list[str]) -> None:
    """단위 테스트용: 대사·포트폴리오가 후보 선정 이전에 완료되었는지 검증한다."""
    indices = [CYCLE_PREFIX_PHASE_ORDER.index(name) for name in completed_phases if name in CYCLE_PREFIX_PHASE_ORDER]
    if indices != sorted(indices):
        raise AssertionError(
            f"사이클 prefix 단계 순서 위반: 기대={CYCLE_PREFIX_PHASE_ORDER}, 실제={completed_phases}",
        )


@dataclass
class CyclePerformanceSlice:
    """마켓 루프 내부 AI·주문 처리 시간 누적(초)."""

    ai_analysis_sec: float = 0.0
    order_processing_sec: float = 0.0

    def add_ai(self, elapsed_sec: float) -> None:
        if elapsed_sec > 0:
            self.ai_analysis_sec += elapsed_sec

    def add_order(self, elapsed_sec: float) -> None:
        if elapsed_sec > 0:
            self.order_processing_sec += elapsed_sec


@dataclass
class CycleGeminiDerivedBlockAggregator:
    """동일 Gemini 장애로 인한 종목별 파생 BUY 차단을 사이클 단위로 집계한다."""

    exchange: str
    cycle_id: str
    logger: logging.Logger
    _by_incident: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    _incident_detail: dict[str, str] = field(default_factory=dict)
    _audit_trail: list[dict[str, Any]] = field(default_factory=list)

    def incident_key_from_reason(self, block_reason: str) -> str:
        text = (block_reason or "").strip()
        if not text:
            return "unknown"
        # 장애 유형 괄호 안의 reason만 키로 사용해 동일 원인을 묶는다.
        start = text.find("장애(")
        end = text.find(")", start + 3) if start >= 0 else -1
        if start >= 0 and end > start:
            return text[start + 3 : end].split(",")[0].strip() or "provider_failure"
        return "provider_failure"

    def record_derived_buy_block(self, market: str, block_reason: str) -> None:
        key = self.incident_key_from_reason(block_reason)
        self._by_incident[key].add(market)
        if key not in self._incident_detail:
            self._incident_detail[key] = block_reason[:200]
        self._audit_trail.append({
            "market": market,
            "incident_key": key,
            "at": time.time(),
        })

    @property
    def total_derived_markets(self) -> int:
        markets: set[str] = set()
        for group in self._by_incident.values():
            markets.update(group)
        return len(markets)

    def flush_cycle_summary(self) -> None:
        if not self._by_incident:
            return
        exchange_label = "업비트" if self.exchange.lower() == "upbit" else "빗썸"
        for incident_key, markets in sorted(self._by_incident.items(), key=lambda item: -len(item[1])):
            detail = self._incident_detail.get(incident_key, incident_key)
            sample = ", ".join(sorted(markets)[:5])
            more = len(markets) - 5
            suffix = f" 외 {more}종목" if more > 0 else ""
            self.logger.info(
                "[Gemini 파생 BUY 차단 집계] cycle=%s 거래소=%s 원인=%s 영향종목=%d건 (%s%s) | %s",
                self.cycle_id,
                exchange_label,
                incident_key,
                len(markets),
                sample,
                suffix,
                detail,
            )

    def snapshot(self) -> dict[str, Any]:
        return {
            "cycle_id": self.cycle_id,
            "exchange": self.exchange,
            "root_incident_count": len(self._by_incident),
            "derived_market_count": self.total_derived_markets,
            "by_incident": {
                key: {
                    "markets": sorted(markets),
                    "market_count": len(markets),
                    "detail": self._incident_detail.get(key, ""),
                }
                for key, markets in self._by_incident.items()
            },
            "audit_trail_count": len(self._audit_trail),
        }
