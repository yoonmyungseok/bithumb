import logging
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from strategy_engine import classify_listing_maturity
from trading_orchestrator import TradingOrchestrator
from trading_runtime import ExchangeBuyProfile, MarketBuyInputs, TradingCycleEngine


class _FakeExchange:
    """실제 거래소 통신 없이 매수 실행 경계만 검증하는 최소 Fake다."""

    def adjust_price_to_tick(self, price, side="bid"):
        return price

    def round_volume(self, market, volume):
        return volume


class RuntimeBuyContractTests(unittest.TestCase):
    """주문 제출 직전 입력 계약과 4H 장애 fail-closed를 검증한다."""

    def _engine(self, submitted):
        engine = TradingCycleEngine.__new__(TradingCycleEngine)
        engine.config = types.SimpleNamespace(min_order_krw=5000.0, orderbook_slippage_enforcement=False)
        engine.buy_profile = ExchangeBuyProfile(exchange_name="bithumb")
        engine.context = types.SimpleNamespace(
            logger=logging.getLogger("test_runtime_buy_contract"),
            risk_manager=types.SimpleNamespace(get_risk_scale_factor=lambda: 1.0, realized_pnl_krw=0.0),
            trailing_tracker=types.SimpleNamespace(get_swing_markets=lambda: [], get_new_listing_markets=lambda: []),
            risk_guard=types.SimpleNamespace(validate_buy=lambda **_kwargs: (True, "")),
            cancel_bot_open_orders=lambda *_args, **_kwargs: 0,
            order_executor=types.SimpleNamespace(submit=lambda _exchange, **kwargs: submitted.append(kwargs)),
            chart_renderer=types.SimpleNamespace(render_trade_chart=lambda **_kwargs: None),
        )
        return engine

    def test_all_buy_paths_submit_explicit_strategy_snapshot_without_metadata(self):
        """CONFIRMED·NEW_LISTING·MOMENTUM_BREAKOUT·SWING이 AttributeError 없이 제출 경계까지 도달한다."""
        submitted = []
        engine = self._engine(submitted)
        for candidate_type, strategy_mode in (
            ("CONFIRMED", "SCALP"), ("NEW_LISTING", "NEW_LISTING"),
            ("MOMENTUM_BREAKOUT", "SCALP"), ("SWING", "SWING"),
        ):
            blocked = engine.process_buy_execution(MarketBuyInputs(
                exchange=_FakeExchange(), market=f"KRW-{candidate_type}", korean_name="테스트",
                candidate_type=candidate_type, momentum_phase="CONFIRMED", entry_price=100.0,
                target_price=105.0, stop_loss=97.0, alloc_pct=0.1, reason="계약 회귀",
                use_recovery_rebound=False, selected_entry={}, coin_available=0.0, coin_value=0.0,
                krw_available=1_000_000.0, current_price=100.0, orderbook={}, candles_5m=[],
                is_bot_paused=False, is_kill_switch=False, is_entry_ready=True, is_btc_crashing=False,
                btc_status_msg="정상", current_total_equity=2_000_000.0, held_markets=[],
                dyn_max_positions=3, dyn_max_pos_pct=0.35, now_str="2026-09-08 18:00:00",
                audit_decision=lambda *_args, **_kwargs: None, strategy_mode=strategy_mode,
            ))
            self.assertFalse(blocked)
            self.assertEqual(submitted[-1]["entry_strategy_snapshot"]["strategy_mode"], strategy_mode)

    def test_four_hour_failure_with_normal_five_minute_data_is_insufficient(self):
        """4H 조회 예외는 정상 5분봉이 있어도 실제 신규상장 이력으로 해석되지 않는다."""
        exchange = types.SimpleNamespace(get_candles=lambda **_kwargs: (_ for _ in ()).throw(TimeoutError("4H timeout")))
        result = TradingOrchestrator(logging.getLogger("test_runtime_buy_contract"))._load_swing_candles_safely(
            exchange, "KRW-TEST",
        )
        self.assertEqual(result.status, "UNAVAILABLE")
        self.assertEqual(
            classify_listing_maturity(result.candles, None, [{"trade_price": 100.0}] * 8, result.status),
            "INSUFFICIENT",
        )


if __name__ == "__main__":
    unittest.main()
