"""Operational readiness reports for slippage enforcement and quality gates."""

from __future__ import annotations

import datetime
import json
import os
from dataclasses import dataclass
from typing import Any

from risk_manager import get_kst_now


@dataclass(frozen=True)
class SlippageEnforcementReadiness:
    """5거래일 호가 슬리피지 관찰 결과."""

    exchange: str
    trading_days_observed: int
    min_trading_days_required: int
    observed_count: int
    blocked_count: int
    avg_estimated_slippage_bps: float
    buy_fill_count: int
    avg_actual_slippage_bps: float
    enforcement_enabled: bool
    ready_for_enforcement: bool
    recommendation: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "exchange": self.exchange,
            "trading_days_observed": self.trading_days_observed,
            "min_trading_days_required": self.min_trading_days_required,
            "observed_count": self.observed_count,
            "blocked_count": self.blocked_count,
            "avg_estimated_slippage_bps": self.avg_estimated_slippage_bps,
            "buy_fill_count": self.buy_fill_count,
            "avg_actual_slippage_bps": self.avg_actual_slippage_bps,
            "enforcement_enabled": self.enforcement_enabled,
            "ready_for_enforcement": self.ready_for_enforcement,
            "recommendation": self.recommendation,
        }


def _kst_trading_day(ts: float) -> str:
    kst = datetime.timezone(datetime.timedelta(hours=9))
    return datetime.datetime.fromtimestamp(ts, tz=kst).strftime("%Y-%m-%d")


def _load_order_journal_slippage(data_dir: str) -> list[float]:
    journal_path = os.path.join(data_dir, "order_journal.json")
    if not os.path.exists(journal_path):
        return []
    try:
        with open(journal_path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return []

    orders = raw.get("orders", raw) if isinstance(raw, dict) else raw
    if not isinstance(orders, list):
        return []

    slips: list[float] = []
    for order in orders:
        if not isinstance(order, dict):
            continue
        side = str(order.get("side", "")).lower()
        if side not in {"bid", "buy"}:
            continue
        executed = float(order.get("executed_volume", 0.0) or 0.0)
        if executed <= 0:
            continue
        slip = float(order.get("slippage_bps", 0.0) or 0.0)
        slips.append(abs(slip))
    return slips


def build_slippage_enforcement_readiness(
    db_manager: Any,
    *,
    exchange: str,
    data_dir: str,
    lookback_days: int = 14,
    min_trading_days: int = 5,
    enforcement_enabled: bool | None = None,
) -> SlippageEnforcementReadiness:
    """호가 슬리피지 관찰 이력과 실제 체결 슬리피지를 비교해 활성화 준비도를 산출한다."""
    now = get_kst_now()
    since_ts = (now - datetime.timedelta(days=lookback_days)).timestamp()
    decisions = db_manager.get_orderbook_slippage_decisions(exchange, since_ts)

    trading_days = {_kst_trading_day(row["decision_ts"]) for row in decisions}
    observed_count = sum(1 for row in decisions if row["action"] == "OBSERVED")
    blocked_count = sum(1 for row in decisions if row["action"] == "BLOCKED")

    estimated_values = [
        float(row["payload"].get("estimated_slippage_bps", 0.0) or 0.0)
        for row in decisions
        if isinstance(row.get("payload"), dict)
    ]
    avg_estimated = round(sum(estimated_values) / len(estimated_values), 1) if estimated_values else 0.0

    actual_values = _load_order_journal_slippage(data_dir)
    avg_actual = round(sum(actual_values) / len(actual_values), 1) if actual_values else 0.0

    if enforcement_enabled is None:
        enforcement_enabled = os.getenv("ORDERBOOK_SLIPPAGE_ENFORCEMENT", "false").strip().lower() in {
            "1", "true", "yes", "on",
        }

    ready = len(trading_days) >= min_trading_days and observed_count >= min_trading_days
    if enforcement_enabled:
        recommendation = "ORDERBOOK_SLIPPAGE_ENFORCEMENT=true 상태입니다. 관찰/차단 이력을 계속 모니터링하세요."
    elif ready:
        recommendation = (
            f"최근 {len(trading_days)}거래일·관찰 {observed_count}건 축적됨. "
            "실거래 차단 활성화 전 운영자가 ORDERBOOK_SLIPPAGE_ENFORCEMENT=true 전환을 검토할 수 있습니다."
        )
    else:
        recommendation = (
            f"관찰 거래일 {len(trading_days)}/{min_trading_days}, 관찰 건수 {observed_count}건. "
            "5거래일 누적 검증 전까지 차단 모드 활성화를 권장하지 않습니다."
        )

    return SlippageEnforcementReadiness(
        exchange=exchange,
        trading_days_observed=len(trading_days),
        min_trading_days_required=min_trading_days,
        observed_count=observed_count,
        blocked_count=blocked_count,
        avg_estimated_slippage_bps=avg_estimated,
        buy_fill_count=len(actual_values),
        avg_actual_slippage_bps=avg_actual,
        enforcement_enabled=enforcement_enabled,
        ready_for_enforcement=ready and not enforcement_enabled,
        recommendation=recommendation,
    )
