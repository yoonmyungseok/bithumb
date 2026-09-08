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
) -> float:
    """분할익절 주문 수량을 반환하며, 보유 잔고보다 크게 주문하지 않는다."""
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
        return min(available, original * ratio)

    # 단타는 거래소별 기존 잔여 수량 비율 계약을 그대로 유지한다.
    return available * max(0.0, float(scalp_stage_ratio or 0.0))
