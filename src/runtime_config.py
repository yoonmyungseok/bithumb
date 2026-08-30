"""Runtime configuration normalization shared by both exchange entry points."""

from __future__ import annotations

import os
from dataclasses import dataclass


def get_fraction_setting(name: str, default: float, *, positive: bool = True) -> float:
    """Read a ratio as a decimal fraction, accepting legacy percentage input safely.

    Values such as ``5`` and ``-5`` are interpreted as 5% for backwards
    compatibility.  The returned value is always positive and less than one;
    invalid safety settings fail at startup rather than silently disabling a
    risk guard.
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        value = float(default)
    else:
        try:
            value = float(raw.strip())
        except ValueError as exc:
            raise ValueError(f"{name} must be a numeric ratio, e.g. 0.05 for 5%") from exc

    value = abs(value)
    if value >= 1.0:
        value /= 100.0
    if (positive and value <= 0.0) or value >= 1.0:
        raise ValueError(f"{name} must be greater than 0 and less than 1 after normalization")
    return value


@dataclass(frozen=True)
class RuntimeRiskSettings:
    """Normalized risk ratios consumed by every live trading cycle."""

    btc_crash_threshold_pct: float
    max_daily_loss_pct: float
    trailing_start_pct: float
    trailing_stop_pct: float


def load_runtime_risk_settings() -> RuntimeRiskSettings:
    """Load all risk ratios through the one compatibility-safe configuration path."""
    return RuntimeRiskSettings(
        btc_crash_threshold_pct=get_fraction_setting("BTC_CRASH_THRESHOLD_PCT", 0.015),
        max_daily_loss_pct=get_fraction_setting("MAX_DAILY_LOSS_PCT", 0.05),
        trailing_start_pct=get_fraction_setting("TRAILING_START_PCT", 0.02),
        trailing_stop_pct=get_fraction_setting("TRAILING_STOP_PCT", 0.012),
    )
