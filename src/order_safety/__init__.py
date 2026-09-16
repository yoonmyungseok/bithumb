"""Durable order intent tracking and pre-trade risk checks.

The exchange is the source of truth for fills. This package deliberately treats a
network failure after an order submission as *unknown*, never as permission to
submit the same order again.
"""

from order_safety.cooldown import CooldownManager
from order_safety.executor import SafeOrderExecutor
from order_safety.fill_processor import OrderFillProcessor
from order_safety.journal import OrderJournal
from order_safety.markets import get_excluded_markets_set
from order_safety.orderbook import evaluate_buy_orderbook_impact
from order_safety.position_sizing import calculate_partial_take_profit_volume
from order_safety.pre_buy_gate import AckReconcileScheduler, evaluate_pre_buy_submit_gate
from order_safety.risk_off_loss_reentry import RiskOffLossReentryGuard
from order_safety.tick_utils import adjust_price_to_tick, round_price_to_tick
from order_safety.types import AmbiguousOrderError, OrderStatus
from risk_controls import RiskGuard, calculate_risk_position_size, get_dynamic_portfolio_tiers
from state_store import load_json_with_backup_recovery, write_json_atomically

__all__ = [
    "AckReconcileScheduler",
    "AmbiguousOrderError",
    "CooldownManager",
    "OrderFillProcessor",
    "OrderJournal",
    "OrderStatus",
    "RiskGuard",
    "RiskOffLossReentryGuard",
    "SafeOrderExecutor",
    "adjust_price_to_tick",
    "round_price_to_tick",
    "calculate_risk_position_size",
    "evaluate_buy_orderbook_impact",
    "evaluate_pre_buy_submit_gate",
    "calculate_partial_take_profit_volume",
    "get_dynamic_portfolio_tiers",
    "get_excluded_markets_set",
    "load_json_with_backup_recovery",
    "write_json_atomically",
]
