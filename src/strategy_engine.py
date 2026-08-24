"""Deterministic entry gate shared by live trading and backtesting.

LLM output may rank and explain candidates, but this gate is the final common
minimum for a new long position. Candles are expected newest-first.
"""

import math
from typing import Any


def calculate_rsi(prices: list[float], period: int = 14) -> float:
    if len(prices) < period + 1:
        return 50.0
    chronological = prices[::-1]
    changes = [chronological[i] - chronological[i - 1] for i in range(1, len(chronological))]
    gains = [max(change, 0.0) for change in changes[-period:]]
    losses = [max(-change, 0.0) for change in changes[-period:]]
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    return round(100.0 - 100.0 / (1.0 + (sum(gains) / period) / avg_loss), 2)


def bollinger(prices: list[float], period: int = 20) -> tuple[float, float]:
    subset = prices[:period]
    middle = sum(subset) / len(subset)
    std = math.sqrt(sum((price - middle) ** 2 for price in subset) / len(subset))
    lower, upper = middle - 2 * std, middle + 2 * std
    pct_b = (prices[0] - lower) / (upper - lower) if upper > lower else 0.5
    return middle, pct_b


def atr(candles: list[dict[str, Any]], period: int = 14) -> float:
    ranges = []
    for index in range(min(len(candles) - 1, period)):
        candle, previous = candles[index], candles[index + 1]
        high, low = float(candle.get("high_price", 0)), float(candle.get("low_price", 0))
        prior_close = float(previous.get("trade_price", 0))
        ranges.append(max(high - low, abs(high - prior_close), abs(low - prior_close)))
    return sum(ranges) / len(ranges) if ranges else 0.0


def entry_signal(candles: list[dict[str, Any]]) -> dict[str, Any]:
    if len(candles) < 25:
        return {"allow_buy": False, "reason": "캔들 데이터 부족"}
    prices = [float(c.get("trade_price", 0.0)) for c in candles]
    current = prices[0]
    ma5 = sum(prices[:5]) / 5
    ma20, pct_b = bollinger(prices)
    rsi = calculate_rsi(prices)
    allowed = ma5 > ma20 and 45.0 <= rsi <= 65.0 and 0.30 <= pct_b <= 0.75
    volatility = atr(candles)
    return {
        "allow_buy": allowed,
        "reason": f"MA5 {'>' if ma5 > ma20 else '<='} MA20, RSI {rsi:.1f}, %B {pct_b:.2f}",
        "entry_price": current,
        "target_price": current + max(current * 0.025, volatility * 1.5),
        "stop_loss": current - max(current * 0.015, volatility),
        "rsi": rsi,
        "pct_b": pct_b,
    }
