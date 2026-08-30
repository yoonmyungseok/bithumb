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

    def __init__(self, min_order_krw: float, max_open_positions: int, max_position_pct: float, max_total_exposure_pct: float, max_order_krw: float):
        self.min_order_krw = min_order_krw
        self.max_open_positions = max_open_positions
        self.max_position_pct = max_position_pct
        self.max_total_exposure_pct = max_total_exposure_pct
        self.max_order_krw = max_order_krw

    def update_limits(self, max_open_positions: int | None = None, max_position_pct: float | None = None, max_total_exposure_pct: float | None = None) -> None:
        if max_open_positions is not None:
            self.max_open_positions = max_open_positions
        if max_position_pct is not None:
            self.max_position_pct = max_position_pct
        if max_total_exposure_pct is not None:
            self.max_total_exposure_pct = max_total_exposure_pct

    def validate_buy(self, market: str, order_krw: float, available_krw: float, total_equity: float, held_markets: list[str]) -> tuple[bool, str]:
        if is_protected_market(market): return False, f"수동 관리 격리 종목 ({market}) 매수 불가"
        if order_krw < self.min_order_krw: return False, "최소 주문금액 미달"
        if order_krw > available_krw: return False, "가용 KRW 초과"
        if self.max_order_krw > 0 and order_krw > self.max_order_krw: return False, "건당 주문 한도 초과"
        if total_equity <= 0: return False, "총 자산 평가 실패"
        if order_krw / total_equity > self.max_position_pct: return False, "종목당 비중 한도 초과"
        if len(held_markets) >= self.max_open_positions and market not in held_markets: return False, "동시 보유 종목 수 한도 초과"
        projected_exposure = 1.0 - max(0.0, available_krw - order_krw) / total_equity
        return (False, "총 투자 비중 한도 초과") if projected_exposure > self.max_total_exposure_pct else (True, "OK")


def calculate_risk_position_size(total_equity: float, entry_price: float, stop_loss: float, risk_fraction: float = 0.01, fee_rate: float = 0.0004, slippage_rate: float = 0.001, max_position_pct: float = 0.35, min_order_krw: float = 5000.0, available_krw: float | None = None, open_slots: int = 3, risk_scale_factor: float = 1.0) -> float:
    if total_equity <= 0 or entry_price <= 0: return 0.0
    scale = max(0.1, min(float(risk_scale_factor), 1.0))
    risk_capital = total_equity * risk_fraction * scale
    stop_dist_pct = abs(entry_price - stop_loss) / entry_price
    effective_loss_pct = max(0.008, stop_dist_pct + (2.0 * fee_rate) + slippage_rate)
    raw_position_krw = risk_capital / effective_loss_pct
    max_allowed_krw = min(total_equity * max_position_pct * scale, (total_equity / max(1, open_slots)) * scale)
    if available_krw is not None and available_krw > 0: max_allowed_krw = min(max_allowed_krw, available_krw)
    final_krw = min(raw_position_krw, max_allowed_krw)
    return round(final_krw, 2) if final_krw >= min_order_krw else 0.0
