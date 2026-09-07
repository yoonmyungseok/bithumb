"""Pre-trade portfolio limits and position-size calculations.

Kept independent from order persistence and exchange transport so risk rules can
be tested and evolved without touching order lifecycle code.
"""

from __future__ import annotations

from market_policy import is_protected_market


def get_dynamic_portfolio_tiers(total_equity: float, custom_max_positions: int | None = None) -> tuple[int, float, int]:
    if custom_max_positions and custom_max_positions > 0:
        max_pct = round(min(0.50, max(0.15, 1.0 / custom_max_positions + 0.05)), 2)
        return custom_max_positions, max_pct, max(10, min(20, custom_max_positions * 3))
    if total_equity < 300_000.0:
        return 3, 0.35, 10
    if total_equity < 1_000_000.0:
        return 5, 0.25, 12
    return 6, 0.20, 15


class RiskGuard:
    """Single decision point for new BUY orders; exits are never blocked."""

    def __init__(
        self,
        min_order_krw: float,
        max_open_positions: int,
        max_position_pct: float,
        max_total_exposure_pct: float,
        max_order_krw: float,
        max_swing_positions: int = 0,
    ):
        self.min_order_krw = min_order_krw
        self.max_open_positions = max_open_positions
        self.max_position_pct = max_position_pct
        self.max_total_exposure_pct = max_total_exposure_pct
        self.max_order_krw = max_order_krw
        self.max_swing_positions = max_swing_positions

    def update_limits(
        self,
        max_open_positions: int | None = None,
        max_position_pct: float | None = None,
        max_total_exposure_pct: float | None = None,
        max_swing_positions: int | None = None,
    ) -> None:
        if max_open_positions is not None:
            self.max_open_positions = max_open_positions
        if max_position_pct is not None:
            self.max_position_pct = max_position_pct
        if max_total_exposure_pct is not None:
            self.max_total_exposure_pct = max_total_exposure_pct
        if max_swing_positions is not None:
            self.max_swing_positions = max_swing_positions

    def validate_buy(
        self,
        market: str,
        order_krw: float,
        available_krw: float,
        total_equity: float,
        held_markets: list[str],
        strategy_mode: str = "SCALP",
        held_swing_markets: list[str] | None = None,
    ) -> tuple[bool, str]:
        if is_protected_market(market):
            return False, f"수동 관리 격리 종목 ({market}) 매수 불가"
        if order_krw < self.min_order_krw:
            return False, "최소 주문금액 미달"
        if order_krw > available_krw:
            return False, "가용 KRW 초과"
        if self.max_order_krw > 0 and order_krw > self.max_order_krw:
            return False, "건당 주문 한도 초과"
        if total_equity <= 0:
            return False, "총 자산 평가 실패"

        # Dual-Track 비중 및 슬롯 제어
        is_swing = str(strategy_mode).upper() == "SWING"
        effective_max_pct = max(self.max_position_pct, 0.35) if is_swing else self.max_position_pct
        if order_krw / total_equity > effective_max_pct:
            return False, "종목당 비중 한도 초과"

        swing_held = set(held_swing_markets or [])
        if self.max_swing_positions > 0:
            if is_swing:
                # 스윙 슬롯 독립 검증
                if len(swing_held) >= self.max_swing_positions and market not in swing_held:
                    return False, f"스윙 전용 보유 종목 수 한도({self.max_swing_positions}개) 초과"
            else:
                # 단타 슬롯: 전체 보유 중 스윙을 제외한 단타 보유 종목 수만 계측
                scalp_held = [m for m in held_markets if m not in swing_held]
                max_scalp_positions = max(1, self.max_open_positions - self.max_swing_positions)
                if len(scalp_held) >= max_scalp_positions and market not in scalp_held:
                    return False, f"단타 전용 보유 종목 수 한도({max_scalp_positions}개) 초과"

        # 전체 총 포지션 한도
        if len(held_markets) >= self.max_open_positions and market not in held_markets:
            return False, "동시 보유 종목 수 한도 초과"

        projected_exposure = 1.0 - max(0.0, available_krw - order_krw) / total_equity
        return (False, "총 투자 비중 한도 초과") if projected_exposure > self.max_total_exposure_pct else (True, "OK")


def calculate_risk_position_size(
    total_equity: float,
    entry_price: float,
    stop_loss: float,
    risk_fraction: float = 0.01,
    fee_rate: float = 0.0004,
    slippage_rate: float = 0.001,
    max_position_pct: float = 0.35,
    min_order_krw: float = 5000.0,
    available_krw: float | None = None,
    open_slots: int = 3,
    risk_scale_factor: float = 1.0,
    strategy_mode: str = "SCALP",
) -> float:
    if total_equity <= 0 or entry_price <= 0:
        return 0.0
    scale = max(0.1, min(float(risk_scale_factor), 1.0))
    is_swing = str(strategy_mode).upper() == "SWING"

    # 스윙 모드는 리스크 버퍼를 약간 확대(1.5%)하고 기본 슬롯 2개 기준 적용
    effective_risk_fraction = max(risk_fraction, 0.015) if is_swing else risk_fraction
    risk_capital = total_equity * effective_risk_fraction * scale
    stop_dist_pct = abs(entry_price - stop_loss) / entry_price
    effective_loss_pct = max(0.008, stop_dist_pct + (2.0 * fee_rate) + slippage_rate)
    raw_position_krw = risk_capital / effective_loss_pct

    effective_slots = 2 if is_swing else max(1, open_slots)
    effective_max_pct = max(max_position_pct, 0.35) if is_swing else max_position_pct
    max_allowed_krw = min(total_equity * effective_max_pct * scale, (total_equity / effective_slots) * scale)

    if available_krw is not None:
        max_allowed_krw = min(max_allowed_krw, max(0.0, float(available_krw)))
    final_krw = min(raw_position_krw, max_allowed_krw)
    return round(final_krw, 2) if final_krw >= min_order_krw else 0.0

