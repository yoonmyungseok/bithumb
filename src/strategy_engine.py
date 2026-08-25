import math
import os
from typing import Any


class StrategyPolicy:
    """실거래 및 백테스트 공통 전략 파라미터 및 단일 실행 정책 (Single Source of Truth, P1)"""
    TIME_STOP_SECONDS: int = 3600      # 60분 타임스탑 (실거래 초 단위)
    TIME_STOP_BARS_5M: int = 12       # 5분봉 12개 = 60분 (백테스트 캔들 단위)
    PARTIAL_TP_PCT: float = 0.025     # +2.5% 1차 50% 분할익절
    TRAILING_START_PCT: float = 0.020 # +2.0% 트레일링 스탑 활성화
    TRAILING_DROP_PCT: float = 0.012  # 최고점 대비 1.2% 하락 시 시장가 청산
    STOP_LOSS_PCT: float = 0.025      # 기본 손절 -2.5%
    ATR_STOP_MULTIPLIER: float = 1.5  # Chandelier / ATR 손절 배수
    ATR_TARGET_MULTIPLIER: float = 2.5 # ATR 목표가 배수
    FEE_RATE: float = 0.0004          # 편도 수수료 0.04%
    SLIPPAGE_RATE: float = 0.001      # 편도 슬리피지 0.10%
    MIN_ORDER_KRW: float = 5000.0     # 최소 주문금액
    MAX_DAILY_LOSS_PCT: float = 0.05  # 일일 손실 한도 5%
    COOLDOWN_STOP_LOSS_SEC: float = 2700.0  # 손절 후 쿨다운 45분
    COOLDOWN_TP_SEC: float = 900.0          # 익절 후 쿨다운 15분
    BTC_CRASH_THRESHOLD_PCT: float = 0.015  # BTC 15분 -1.5% 급락 시 차단



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


def calculate_vwap(candles: list[dict[str, Any]]) -> dict[str, Any]:
    """Calculate Volume Weighted Average Price (VWAP) from recent candles."""
    if not candles:
        return {"vwap": 0.0, "disparity_pct": 0.0, "is_above": False}

    cum_pv = 0.0
    cum_vol = 0.0
    for c in candles[:min(len(candles), 30)]:
        h = float(c.get("high_price", 0.0))
        l = float(c.get("low_price", 0.0))
        close_p = float(c.get("trade_price", 0.0))
        vol = float(c.get("candle_acc_trade_volume", 0.0))
        typical_p = (h + l + close_p) / 3.0 if (h > 0 and l > 0 and close_p > 0) else close_p
        cum_pv += typical_p * vol
        cum_vol += vol

    current_price = float(candles[0].get("trade_price", 0.0))
    if cum_vol <= 0 or cum_pv <= 0:
        return {"vwap": current_price, "disparity_pct": 0.0, "is_above": True}

    vwap_val = cum_pv / cum_vol
    disparity_pct = ((current_price - vwap_val) / vwap_val * 100.0) if vwap_val > 0 else 0.0
    is_above = current_price >= (vwap_val * 0.998)

    return {
        "vwap": round(vwap_val, 2),
        "disparity_pct": round(disparity_pct, 2),
        "is_above": is_above,
    }


def calculate_macd_acceleration(
    prices: list[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> dict[str, Any]:
    """Calculate MACD Histogram Slope & Momentum Acceleration."""
    if len(prices) < slow + signal + 3:
        macd_base = calculate_macd(prices, fast, slow, signal)
        return {
            "macd": macd_base.get("macd", 0.0),
            "signal": macd_base.get("signal", 0.0),
            "hist": macd_base.get("hist", 0.0),
            "slope": 0.0,
            "is_accelerating": False,
            "momentum_state": "NEUTRAL",
        }

    chronological = prices[::-1]
    ema_fast_series = calculate_ema_series(chronological, fast)
    ema_slow_series = calculate_ema_series(chronological, slow)

    macd_series = [f - s for f, s in zip(ema_fast_series, ema_slow_series)]
    signal_series = calculate_ema_series(macd_series, signal)

    hist_series = [m - s for m, s in zip(macd_series, signal_series)]
    hist_now = hist_series[-1]
    hist_prev1 = hist_series[-2] if len(hist_series) >= 2 else hist_now
    hist_prev2 = hist_series[-3] if len(hist_series) >= 3 else hist_prev1

    slope = hist_now - hist_prev1
    is_accelerating = (slope > 0) and (hist_now > hist_prev1 >= hist_prev2 or hist_now > 0)

    if hist_now > 0 and is_accelerating:
        state = "ACCELERATING_BULL"
    elif hist_now > 0 and not is_accelerating:
        state = "DECELERATING_BULL"
    elif is_accelerating:
        state = "RECOVERING"
    else:
        state = "BEARISH"

    return {
        "macd": round(macd_series[-1], 2),
        "signal": round(signal_series[-1], 2),
        "hist": round(hist_now, 2),
        "slope": round(slope, 4),
        "is_accelerating": is_accelerating,
        "momentum_state": state,
    }


def calculate_composite_alpha_score(
    candles: list[dict[str, Any]],
    candles_1h: list[dict[str, Any]] | None = None,
    orderbook: dict[str, Any] | None = None,
    btc_regime: str = "NORMAL",
) -> dict[str, Any]:
    """Calculate 7-Factor Composite Quantitative Alpha Score (0 ~ 100 points)."""
    if not candles or len(candles) < 20:
        return {"total_score": 0, "allow_buy": False, "factor_breakdown": {}, "reason": "데이터 부족"}

    prices = [float(c.get("trade_price", 0.0)) for c in candles]
    current = prices[0]

    # 1. MTF 1H 추세 (15점)
    score_mtf = 0
    mtf_reason = "1H 미제공"
    if candles_1h and len(candles_1h) >= 20:
        prices_1h = [float(c.get("trade_price", 0.0)) for c in candles_1h]
        ema20_1h = calculate_ema(prices_1h, 20)
        if prices_1h[0] >= ema20_1h:
            score_mtf = 15
            mtf_reason = "1H 정배열 강세"
        elif prices_1h[0] >= (ema20_1h * 0.990):
            score_mtf = 10
            mtf_reason = "1H 지지선 유지"
        else:
            score_mtf = 0
            mtf_reason = "1H 역배열 약세"
    else:
        score_mtf = 10

    # 2. VWAP 지지/돌파 (15점)
    vwap_data = calculate_vwap(candles)
    score_vwap = 15 if vwap_data["is_above"] and vwap_data["disparity_pct"] <= 3.5 else (8 if vwap_data["is_above"] else 0)

    # 3. MACD 히스토그램 가속도 (15점)
    macd_acc = calculate_macd_acceleration(prices)
    if macd_acc["momentum_state"] == "ACCELERATING_BULL":
        score_macd = 15
    elif macd_acc["is_accelerating"]:
        score_macd = 12
    elif macd_acc["hist"] > 0:
        score_macd = 8
    else:
        score_macd = 0

    # 4. RSI 골든존 (15점)
    rsi_val = calculate_rsi(prices, 14)
    if 45.0 <= rsi_val <= 65.0:
        score_rsi = 15
    elif 38.0 <= rsi_val <= 72.0:
        score_rsi = 10
    else:
        score_rsi = 0

    # 5. 볼린저 밴드 중심선 돌파 및 밴드 확장 (15점)
    bb = calculate_bollinger_bands(prices, 20, 2.0)
    ma5 = sum(prices[:5]) / 5.0
    if ma5 > bb["middle"] and (0.30 <= bb["pct_b"] <= 0.85):
        score_bb = 15
    elif ma5 >= bb["middle"] * 0.995:
        score_bb = 10
    else:
        score_bb = 0

    # 6. 수급 / 체결강도 및 호가창 잔량비 (15점)
    score_orderflow = 10  # 기본
    if orderbook:
        total_ask = float(orderbook.get("total_ask_size", 1.0))
        total_bid = float(orderbook.get("total_bid_size", 1.0))
        ratio = total_bid / total_ask if total_ask > 0 else 1.0
        if ratio >= 1.4:
            score_orderflow = 15
        elif ratio < 0.6:
            score_orderflow = 3

    # 7. 볼륨 스파이크 (10점)
    vols = [float(c.get("candle_acc_trade_volume", 0.0)) for c in candles]
    avg_vol_20 = (sum(vols[1:21]) / 20.0) if len(vols) >= 21 else (vols[0] if vols else 1.0)
    current_vol = vols[0] if vols else 0.0
    vol_ratio = (current_vol / avg_vol_20) if avg_vol_20 > 0 else 1.0
    if vol_ratio >= 2.0 and current >= float(candles[0].get("opening_price", current)):
        score_vol = 10
    elif vol_ratio >= 1.2:
        score_vol = 7
    else:
        score_vol = 4

    total_score = score_mtf + score_vwap + score_macd + score_rsi + score_bb + score_orderflow + score_vol
    allow_buy = (total_score >= 65) and (btc_regime.upper() not in ("CRASH", "BEAR_VOLATILE"))

    breakdown = {
        "mtf_score": score_mtf,
        "vwap_score": score_vwap,
        "macd_score": score_macd,
        "rsi_score": score_rsi,
        "bollinger_score": score_bb,
        "orderflow_score": score_orderflow,
        "volume_score": score_vol,
    }

    return {
        "total_score": total_score,
        "allow_buy": allow_buy,
        "factor_breakdown": breakdown,
        "vwap": vwap_data["vwap"],
        "macd_state": macd_acc["momentum_state"],
        "rsi": rsi_val,
        "pct_b": bb["pct_b"],
        "reason": f"알파 스코어 {total_score}/100점 ({'🟢 승인' if allow_buy else '⚪ 미달'}) | MTF:{score_mtf} VWAP:{score_vwap} MACD:{score_macd} RSI:{score_rsi} BB:{score_bb}",
    }


def entry_signal(
    candles: list[dict[str, Any]],
    candles_1h: list[dict[str, Any]] | None = None,
    btc_regime: str = "NORMAL",
    orderbook: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Deterministic entry rule with MTF trend alignment, 7-factor alpha score, and dynamic ATR risk bounds."""
    if len(candles) < 25:
        return {"allow_buy": False, "reason": "캔들 데이터 부족"}

    regime_upper = btc_regime.upper()
    if regime_upper in ("CRASH", "BEAR_VOLATILE"):
        return {"allow_buy": False, "reason": f"BTC 시장 레짐 경보 ({btc_regime})"}

    alpha_res = calculate_composite_alpha_score(
        candles=candles,
        candles_1h=candles_1h,
        orderbook=orderbook,
        btc_regime=btc_regime,
    )

    prices = [float(c.get("trade_price", 0.0)) for c in candles]
    current = prices[0]
    ma5 = sum(prices[:5]) / 5.0
    bands = calculate_bollinger_bands(prices, period=20)
    ma20 = bands["middle"]
    pct_b = bands["pct_b"]
    rsi = calculate_rsi(prices)

    # 1. 5분봉 정량 조건
    rsi_min, rsi_max = (42.0, 65.0) if regime_upper == "RISK_OFF" else (38.0, 72.0)
    pct_b_min, pct_b_max = (0.30, 0.75) if regime_upper == "RISK_OFF" else (0.20, 0.88)

    signal_5m = ma5 > ma20 and (rsi_min <= rsi <= rsi_max) and (pct_b_min <= pct_b <= pct_b_max)

    # 2. 1시간봉 MTF 추세 필터
    mtf_allowed = True
    mtf_reason = "1H MTF 미제공"
    if candles_1h and len(candles_1h) >= 20:
        prices_1h = [float(c.get("trade_price", 0.0)) for c in candles_1h]
        ema20_1h = calculate_ema(prices_1h, 20)
        current_1h = prices_1h[0]
        mtf_allowed = current_1h >= (ema20_1h * 0.990)
        mtf_reason = f"1H {current_1h:.1f} {'>=' if mtf_allowed else '<'} EMA20 {ema20_1h:.1f}"

    allowed = (signal_5m and mtf_allowed) or (alpha_res["allow_buy"] and mtf_allowed)

    # 3. ATR 기반 동적 손익비 산출
    atr_data = calculate_atr(candles, period=14)
    volatility = atr_data["atr"]
    atr_pct = atr_data["atr_pct"]

    target_offset = max(current * 0.020, volatility * 1.8)
    target_price = current + target_offset

    stop_offset = max(current * 0.015, volatility * 1.2)
    stop_loss = current - stop_offset

    checklist_details = {
        "alpha_score": alpha_res["total_score"],
        "factor_breakdown": alpha_res["factor_breakdown"],
        "ma_alignment": {"pass": ma5 > ma20, "ma5": round(ma5, 2), "ma20": round(ma20, 2)},
        "rsi_range": {"pass": (rsi_min <= rsi <= rsi_max), "value": rsi, "min": rsi_min, "max": rsi_max},
        "bollinger_pct_b": {"pass": (pct_b_min <= pct_b <= pct_b_max), "value": round(pct_b, 3), "min": pct_b_min, "max": pct_b_max},
        "mtf_1h_trend": {"pass": mtf_allowed, "detail": mtf_reason},
        "btc_regime": {"pass": regime_upper not in ("CRASH", "BEAR_VOLATILE"), "regime": btc_regime},
    }

    reasons = [
        f"알파스코어 {alpha_res['total_score']}점",
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
        "alpha_score": alpha_res["total_score"],
        "risk_reward_ratio": round(target_offset / stop_offset, 2) if stop_offset > 0 else 1.5,
        "checklist_details": checklist_details,
    }
