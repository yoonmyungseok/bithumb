"""거래소 공통 호가 단위(Tick Size) 수학적 가격 보정 순수 함수."""

from __future__ import annotations

import math


def adjust_price_to_tick(
    price: float,
    tick: float,
    side: str = "bid",
    mode: str | None = None,
) -> float:
    """주문 방향에 따른 지정가 호가 단위 보정 (P0-1 안전 수칙).

    - 매수 지정가 (bid/buy/floor): 호가 단위 내림(floor)으로 예산 초과 및 불리한 체결 방지
    - 매도 지정가 (ask/sell/ceil): 호가 단위 올림(ceil)으로 불리한 슬리피지 방지
    - round: 단순 반올림
    """
    if price <= 0 or tick <= 0:
        return price

    if tick >= 1.0:
        precision = 0
    else:
        tick_str = str(tick)
        precision = len(tick_str.split(".")[1]) if "." in tick_str else 0

    if mode:
        m = mode.lower()
    else:
        s = str(side).lower()
        if s in ("bid", "buy"):
            m = "floor"
        elif s in ("ask", "sell"):
            m = "ceil"
        else:
            m = "round"

    if m == "floor":
        units = math.floor(round(price / tick, 8))
        res = units * tick
    elif m == "ceil":
        units = math.ceil(round(price / tick, 8))
        res = units * tick
    else:
        units = round(price / tick)
        res = units * tick

    return round(res, precision) if precision > 0 else float(int(round(res)))


def round_price_to_tick(price: float, tick: float) -> float:
    """공식 호가 단위에 맞게 가격을 단순 반올림 보정한다."""
    return adjust_price_to_tick(price, tick, mode="round")
