"""확정 체결 수량을 기준으로 한 포지션 청산 수량 계산."""

from __future__ import annotations

from strategy_engine import StrategyPolicy


def calculate_partial_take_profit_volume(
    *,
    is_swing: bool,
    stage: int,
    available_volume: float,
    original_filled_volume: float = 0.0,
    scalp_stage_ratio: float = 0.0,
    current_price: float = 0.0,
    min_order_krw: float = 0.0,
) -> float:
    """분할익절 주문 수량을 반환하며, 소액 포지션에서 최소주문금액 미달·먼지 잔고 발생 시 전량 익절로 전환한다."""
    available = max(0.0, float(available_volume or 0.0))
    if available <= 0.0:
        return 0.0

    if is_swing:
        # 스윙 2차는 잔여분 비율이 아니라 최초 확정 매수 수량의 30%다.
        original = max(0.0, float(original_filled_volume or 0.0))
        if original <= 0.0:
            # 원보유 수량을 대사하지 못하면 수익 실현 주문을 추정 제출하지 않는다.
            return 0.0
        ratio = (
            StrategyPolicy.SWING_PARTIAL_TP_2_RATIO
            if stage == 2
            else StrategyPolicy.SWING_PARTIAL_TP_1_RATIO
        )
        partial_vol = min(available, original * ratio)
    else:
        # 단타는 거래소별 기존 잔여 수량 비율 계약을 그대로 유지한다.
        partial_vol = available * max(0.0, float(scalp_stage_ratio or 0.0))

    if partial_vol <= 0.0:
        return 0.0

    price = max(0.0, float(current_price or 0.0))
    min_krw = max(0.0, float(min_order_krw or 0.0))
    if price > 0.0 and min_krw > 0.0:
        total_val = available * price
        partial_val = partial_vol * price
        remaining_val = max(0.0, available - partial_vol) * price
        # 전체 보유액은 주문 가능하지만 분할 매도액 또는 잔여액이 최소주문금액 미만이면 전량 익절로 전환한다.
        # 시드가 증액되어 양쪽 모두 최소주문금액 이상이 되면 자동으로 분할익절 비율이 적용된다.
        if total_val >= min_krw and (partial_val < min_krw or remaining_val < min_krw):
            return available

    return partial_vol
