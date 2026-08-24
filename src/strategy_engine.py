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


def calculate_ema_series(prices: list[float], period: int) -> list[float]:
    """Calculate full EMA series for chronological prices (oldest-first)."""
    if not prices:
        return []
    if len(prices) < period:
        # Fallback: simple expanding mean
        result = []
        acc = 0.0
        for i, p in enumerate(prices, 1):
            acc += p
            result.append(acc / i)
        return result

    k = 2.0 / (period + 1)
    ema = sum(prices[:period]) / period
    result = [prices[i] for i in range(period - 1)] + [ema]
    for price in prices[period:]:
        ema = (price * k) + (ema * (1.0 - k))
        result.append(ema)
    return result


def calculate_macd(prices: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict[str, Any]:
    """Calculate MACD Line, Signal Line, Histogram and Trend using standard exponential series."""
    if len(prices) < slow + signal:
        return {"macd": 0.0, "signal": 0.0, "hist": 0.0, "trend": "NEUTRAL"}

    chronological = prices[::-1]
    ema_fast_series = calculate_ema_series(chronological, fast)
    ema_slow_series = calculate_ema_series(chronological, slow)

    # Compute MACD series for available overlap
    macd_series = [f - s for f, s in zip(ema_fast_series, ema_slow_series)]
    signal_series = calculate_ema_series(macd_series, signal)

    macd_line = macd_series[-1]
    signal_line = signal_series[-1]
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


def calculate_chandelier_exit(candles: list[dict[str, Any]], period: int = 14, multiplier: float = 1.5) -> float:
    """Calculate Chandelier Exit trailing stop price (Highest High - multiplier * ATR)."""
    if not candles:
        return 0.0
    subset = candles[:min(len(candles), period)]
    highest_high = max(float(c.get("high_price", 0.0)) for c in subset)
    atr_val = atr(candles, period=period)
    return round(highest_high - (multiplier * atr_val), 2)


def classify_btc_regime(
    candles_5m: list[dict[str, Any]],
    candles_1h: list[dict[str, Any]] | None = None,
    crash_threshold_pct: float = 0.015,
) -> dict[str, Any]:
    """Classify BTC market regime into NORMAL, RISK_OFF, or CRASH.

    - CRASH: Recent 5m/15m drop >= crash_threshold_pct (1.5%) -> Stop all new buys
    - RISK_OFF: 1H Close < 1H EMA50 or 1H drop >= 1.0% -> Stricter gates & 50% sizing
    - NORMAL: Healthy uptrend/stable state
    """
    if not candles_5m or len(candles_5m) < 3:
        return {"regime": "NORMAL", "reason": "BTC 데이터 부족"}

    cur_p = float(candles_5m[0].get("trade_price", 0.0))
    p_3 = float(candles_5m[min(len(candles_5m) - 1, 3)].get("trade_price", cur_p))
    recent_drop = (cur_p - p_3) / p_3 if p_3 > 0 else 0.0

    if recent_drop <= -crash_threshold_pct:
        return {
            "regime": "CRASH",
            "drop_pct": round(recent_drop * 100.0, 2),
            "reason": f"BTC 15분 급락 경보 ({recent_drop*100.0:.2f}%)",
        }

    # 1H Check
    if candles_1h and len(candles_1h) >= 20:
        prices_1h = [float(c.get("trade_price", 0.0)) for c in candles_1h]
        ema50_1h = calculate_ema(prices_1h, min(len(prices_1h), 50))
        cur_1h = prices_1h[0]
        p_1h_prev = prices_1h[min(len(prices_1h) - 1, 3)]
        drop_1h = (cur_1h - p_1h_prev) / p_1h_prev if p_1h_prev > 0 else 0.0

        if cur_1h < ema50_1h or drop_1h <= -0.010:
            sub_reason = "1H EMA50 하회" if cur_1h < ema50_1h else f"1H {drop_1h*100.0:.1f}% 하락"
            return {
                "regime": "RISK_OFF",
                "drop_pct": round(drop_1h * 100.0, 2),
                "reason": f"BTC 약세/조정 ({sub_reason})",
            }

    return {"regime": "NORMAL", "drop_pct": round(recent_drop * 100.0, 2), "reason": "BTC 정상 안정세"}


def entry_signal(
    candles: list[dict[str, Any]],
    candles_1h: list[dict[str, Any]] | None = None,
    btc_regime: str = "NORMAL",
) -> dict[str, Any]:
    """Deterministic entry rule with MTF trend alignment, 3-tier BTC regime, and dynamic ATR risk bounds.

    - 5M Signal: MA5 > MA20, RSI 45~65 (RISK_OFF: 48~58), Bollinger %B 0.35~0.75 (RISK_OFF: 0.40~0.65)
    - 1H MTF Filter: 1H Close > 1H EMA20 (if 1H candles provided)
    - Regime Filter: Reject new entries if btc_regime == 'CRASH'
    """
    if len(candles) < 25:
        return {"allow_buy": False, "reason": "캔들 데이터 부족"}

    regime_upper = btc_regime.upper()
    if regime_upper in ("CRASH", "BEAR_VOLATILE"):
        return {"allow_buy": False, "reason": f"BTC 시장 레짐 경보 ({btc_regime})"}

    prices = [float(c.get("trade_price", 0.0)) for c in candles]
    current = prices[0]
    ma5 = sum(prices[:5]) / 5.0
    bands = calculate_bollinger_bands(prices, period=20)
    ma20 = bands["middle"]
    pct_b = bands["pct_b"]
    rsi = calculate_rsi(prices)

    # 1. 5분봉 정량 조건 (RISK_OFF 상태에서는 기준 강화)
    rsi_min, rsi_max = (48.0, 58.0) if regime_upper == "RISK_OFF" else (45.0, 65.0)
    pct_b_min, pct_b_max = (0.40, 0.65) if regime_upper == "RISK_OFF" else (0.35, 0.75)

    signal_5m = ma5 > ma20 and (rsi_min <= rsi <= rsi_max) and (pct_b_min <= pct_b <= pct_b_max)

    # 2. 1시간봉 MTF 추세 필터 (옵션/제공 시)
    mtf_allowed = True
    mtf_reason = "1H MTF 미제공"
    if candles_1h and len(candles_1h) >= 20:
        prices_1h = [float(c.get("trade_price", 0.0)) for c in candles_1h]
        ema20_1h = calculate_ema(prices_1h, 20)
        current_1h = prices_1h[0]
        mtf_allowed = current_1h >= (ema20_1h * 0.995)  # 1H EMA20 지지 또는 상단
        mtf_reason = f"1H {current_1h:.1f} {'>=' if mtf_allowed else '<'} EMA20 {ema20_1h:.1f}"

    allowed = signal_5m and mtf_allowed

    # 3. ATR 기반 동적 손익비 산출
    atr_data = calculate_atr(candles, period=14)
    volatility = atr_data["atr"]
    atr_pct = atr_data["atr_pct"]

    # 목표가: 최소 +2.0% 또는 ATR 2.0배
    target_offset = max(current * 0.020, volatility * 2.0)
    target_price = current + target_offset

    # 손절가: 최소 -1.2% 또는 ATR 1.3배 (손익비 >= 1.54 보장)
    stop_offset = max(current * 0.012, volatility * 1.3)
    stop_loss = current - stop_offset

    reasons = [
        f"MA5 {'>' if ma5 > ma20 else '<='} MA20",
        f"RSI {rsi:.1f}",
        f"%B {pct_b:.2f}",
        mtf_reason,
    ]

    return {
        "allow_buy": allowed,
        "reason": ", ".join(reasons),
        "entry_price": current,
        "target_price": round(target_price, 2),
        "stop_loss": round(stop_loss, 2),
        "atr": volatility,
        "atr_pct": atr_pct,
        "rsi": rsi,
        "pct_b": pct_b,
        "risk_reward_ratio": round(target_offset / stop_offset, 2) if stop_offset > 0 else 1.5,
    }
