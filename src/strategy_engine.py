"""Deterministic entry gate and standardized technical indicators shared by live trading, AI analyzer, and backtesting.

All candle inputs are expected newest-first (descending chronological order).
"""

import math
from typing import Any


def calculate_rsi(prices: list[float], period: int = 14) -> float:
    """Calculate standard Relative Strength Index (RSI)."""
    if len(prices) < period + 1:
        return 50.0
    chronological = prices[::-1]
    changes = [chronological[i] - chronological[i - 1] for i in range(1, len(chronological))]
    gains = [max(change, 0.0) for change in changes[-period:]]
    losses = [max(-change, 0.0) for change in changes[-period:]]
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = (sum(gains) / period) / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


def calculate_bollinger_bands(prices: list[float], period: int = 20, num_std: float = 2.0) -> dict[str, float]:
    """Calculate Bollinger Bands (upper, middle, lower, width_pct, %B)."""
    if len(prices) < period:
        current = prices[0] if prices else 0.0
        return {"upper": current * 1.02, "middle": current, "lower": current * 0.98, "width_pct": 4.0, "pct_b": 0.5}

    subset = prices[:period]
    middle = sum(subset) / len(subset)
    variance = sum((x - middle) ** 2 for x in subset) / len(subset)
    std = math.sqrt(variance)

    upper = middle + (num_std * std)
    lower = middle - (num_std * std)
    width_pct = ((upper - lower) / middle) * 100.0 if middle > 0 else 0.0
    pct_b = (prices[0] - lower) / (upper - lower) if (upper - lower) > 0 else 0.5

    return {
        "upper": round(upper, 2),
        "middle": round(middle, 2),
        "lower": round(lower, 2),
        "width_pct": round(width_pct, 2),
        "pct_b": round(pct_b, 2),
    }


def bollinger(prices: list[float], period: int = 20) -> tuple[float, float]:
    """Compatibility helper returning (middle_ma20, pct_b)."""
    bands = calculate_bollinger_bands(prices, period=period)
    return bands["middle"], bands["pct_b"]


def calculate_ema(prices: list[float], period: int) -> float:
    """Calculate Exponential Moving Average (EMA)."""
    if len(prices) < period:
        return sum(prices) / len(prices) if prices else 0.0

    chronological = prices[::-1]
    k = 2.0 / (period + 1)
    ema = sum(chronological[:period]) / period
    for price in chronological[period:]:
        ema = (price * k) + (ema * (1.0 - k))
    return ema


def calculate_macd(prices: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict[str, Any]:
    """Calculate MACD Line, Signal Line, Histogram and Trend."""
    if len(prices) < slow:
        return {"macd": 0.0, "signal": 0.0, "hist": 0.0, "trend": "NEUTRAL"}

    ema_fast = calculate_ema(prices, fast)
    ema_slow = calculate_ema(prices, slow)
    macd_line = ema_fast - ema_slow
    signal_line = macd_line * 0.9  # Simplified signal estimate
    hist = macd_line - signal_line

    trend = "BULLISH" if macd_line > signal_line and macd_line > 0 else ("BEARISH" if macd_line < signal_line and macd_line < 0 else "NEUTRAL")

    return {
        "macd": round(macd_line, 2),
        "signal": round(signal_line, 2),
        "hist": round(hist, 2),
        "trend": trend,
    }


def calculate_atr(candles: list[dict[str, Any]], period: int = 14) -> dict[str, float]:
    """Calculate Average True Range (ATR) and percentage."""
    if not candles or len(candles) < 2:
        return {"atr": 0.0, "atr_pct": 2.0}

    ranges = []
    for index in range(min(len(candles) - 1, period)):
        candle, previous = candles[index], candles[index + 1]
        high = float(candle.get("high_price", 0.0))
        low = float(candle.get("low_price", 0.0))
        prior_close = float(previous.get("trade_price", 0.0))
        ranges.append(max(high - low, abs(high - prior_close), abs(low - prior_close)))

    atr_val = sum(ranges) / len(ranges) if ranges else 0.0
    current_price = float(candles[0].get("trade_price", 1.0))
    atr_pct = (atr_val / current_price * 100.0) if current_price > 0 else 2.0

    return {"atr": round(atr_val, 2), "atr_pct": round(atr_pct, 2)}


def atr(candles: list[dict[str, Any]], period: int = 14) -> float:
    """Compatibility helper returning raw ATR value."""
    return calculate_atr(candles, period=period)["atr"]


def entry_signal(candles: list[dict[str, Any]]) -> dict[str, Any]:
    """Deterministic entry rule: MA5 > MA20, RSI 45~65, Bollinger %B 0.30~0.75."""
    if len(candles) < 25:
        return {"allow_buy": False, "reason": "캔들 데이터 부족"}
    prices = [float(c.get("trade_price", 0.0)) for c in candles]
    current = prices[0]
    ma5 = sum(prices[:5]) / 5.0
    bands = calculate_bollinger_bands(prices, period=20)
    ma20 = bands["middle"]
    pct_b = bands["pct_b"]
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
