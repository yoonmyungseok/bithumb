"""Single source of truth for markets excluded from automated trading.

This policy is deliberately dependency-free so screening, order submission,
portfolio valuation, and presentation all enforce exactly the same rule.
"""

from __future__ import annotations

import os


def get_excluded_markets() -> set[str]:
    """Return protected market and asset symbols, always including HOLO."""
    raw = ",".join((
        os.getenv("EXCLUDED_MANUAL_HOLDINGS", "KRW-HOLO,HOLO"),
        os.getenv("UPBIT_EXCLUDED_MARKETS", "KRW-HOLO,HOLO"),
    ))
    markets = {"KRW-HOLO", "HOLO"}
    for item in raw.split(","):
        symbol = item.strip().upper()
        if not symbol:
            continue
        markets.add(symbol)
        markets.add(symbol.removeprefix("KRW-"))
        markets.add(symbol if symbol.startswith("KRW-") else f"KRW-{symbol}")
    return markets


def is_protected_market(market: str) -> bool:
    """Whether a market code or base-asset symbol is manually protected."""
    normalized = market.strip().upper()
    return normalized in get_excluded_markets() or normalized.removeprefix("KRW-") in get_excluded_markets()
