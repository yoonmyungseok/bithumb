"""Pre-trade orderbook impact checks for new buy orders."""

from __future__ import annotations

from typing import Any


def evaluate_buy_orderbook_impact(
    orderbook: dict[str, Any] | None,
    order_krw: float,
    reference_price: float,
    max_slippage_bps: float = 100.0,
) -> tuple[bool, str, dict[str, float]]:
    """신규 매수 전 매도 호가 잔량으로 예상 체결단가와 가격 충격을 보수적으로 검증한다."""
    details = {
        "estimated_vwap": 0.0,
        "estimated_slippage_bps": 0.0,
        "available_ask_krw": 0.0,
    }
    if order_krw <= 0 or reference_price <= 0:
        return False, "주문 금액 또는 기준가가 유효하지 않아 신규 매수를 차단합니다.", details

    units = (orderbook or {}).get("orderbook_units", [])
    if not isinstance(units, list) or not units:
        return False, "호가창이 비어 있어 예상 슬리피지를 검증할 수 없으므로 신규 매수를 차단합니다.", details

    remaining_krw = float(order_krw)
    acquired_volume = 0.0
    spent_krw = 0.0
    available_ask_krw = 0.0
    for unit in units:
        if not isinstance(unit, dict):
            continue
        ask_price = float(unit.get("ask_price", 0.0) or 0.0)
        ask_size = float(unit.get("ask_size", 0.0) or 0.0)
        level_krw = ask_price * ask_size
        if ask_price <= 0 or ask_size <= 0 or level_krw <= 0:
            continue
        available_ask_krw += level_krw
        take_krw = min(remaining_krw, level_krw)
        acquired_volume += take_krw / ask_price
        spent_krw += take_krw
        remaining_krw -= take_krw
        if remaining_krw <= 1e-6:
            break

    details["available_ask_krw"] = round(available_ask_krw, 2)
    if remaining_krw > 1e-6 or acquired_volume <= 0:
        return False, "매도 호가 잔량이 주문 금액보다 부족하여 신규 매수를 차단합니다.", details

    estimated_vwap = spent_krw / acquired_volume
    slippage_bps = ((estimated_vwap - reference_price) / reference_price) * 10000.0
    details["estimated_vwap"] = round(estimated_vwap, 8)
    details["estimated_slippage_bps"] = round(slippage_bps, 1)
    if slippage_bps > max_slippage_bps:
        return (
            False,
            f"예상 매수 슬리피지 {slippage_bps:+.1f}bps가 한도 {max_slippage_bps:.0f}bps를 초과하여 신규 매수를 차단합니다.",
            details,
        )
    return True, "OK", details
