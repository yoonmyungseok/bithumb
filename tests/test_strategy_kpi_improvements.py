import os
import sys
import time
import types
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

from ai_provider import BithumbGeminiProvider, GeminiProvider
from strategy_engine import StrategyPolicy, entry_signal
from trading_runtime import MarketExitInputs, TradingCycleEngine


def _make_candles_5m(count=30, base=1000.0, step=4.0, atr_spread=6.0):
    """최신순(index 0이 가장 최근) 5분봉 확정 캔들 생성."""
    candles = []
    for i in range(count):
        close = base - i * step
        open_p = close - step * 0.6
        high_p = max(open_p, close) + atr_spread
        low_p = min(open_p, close) - atr_spread
        candles.append({
            "opening_price": open_p,
            "high_price": high_p,
            "low_price": low_p,
            "trade_price": close,
            "candle_acc_trade_volume": 1000.0 if i > 0 else 1800.0,
        })
    return candles


def _make_candles_1h(count=25, base=1000.0, step=2.0):
    candles = []
    for i in range(count):
        p = base - i * step
        candles.append({
            "opening_price": p - 1.0,
            "high_price": p + 2.0,
            "low_price": p - 2.0,
            "trade_price": p,
            "candle_acc_trade_volume": 5000.0,
        })
    return candles


def _make_orderbook(bid_size: float, ask_size: float, price: float = 1000.0):
    return {
        "total_bid_size": bid_size,
        "total_ask_size": ask_size,
        "orderbook_units": [
            {"bid_price": price, "bid_size": bid_size, "ask_price": price * 1.001, "ask_size": ask_size}
        ],
    }


def _make_cycle_engine(hold_sec: float):
    submitted = []
    entry_ts = time.time() - hold_sec

    class DummyExecutor:
        def submit(self, *args, **kwargs):
            submitted.append(kwargs)
            return types.SimpleNamespace(accepted=True)

    trailing_tracker = types.SimpleNamespace(
        check_position=lambda *args, **kwargs: ("NONE", 0.0, 0.0, 0.0, 0.0),
        is_swing_position=lambda m: False,
        is_new_listing_position=lambda m: False,
        is_breakeven_active=lambda m, avg_buy_price=0.0: False,
        activate_breakeven=lambda m: None,
        acquire_exit_lock=lambda m: True,
        release_exit_lock=lambda m: None,
        get_entry_time=lambda m: entry_ts,
        set_entry_time=lambda m, ts: None,
        get_dynamic_stop_loss=lambda m: None,
    )

    ctx = types.SimpleNamespace(
        logger=types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None, debug=lambda *a, **k: None),
        trailing_tracker=trailing_tracker,
        order_journal=types.SimpleNamespace(has_active_exit_order=lambda m: False),
        cancel_bot_open_orders=lambda *a, **k: None,
        order_executor=DummyExecutor(),
        chart_renderer=types.SimpleNamespace(render_trade_chart=lambda **k: None),
    )

    engine = TradingCycleEngine.__new__(TradingCycleEngine)
    engine.context = ctx
    engine.exit_profile = types.SimpleNamespace(
        partial_tp_stage2_ratio=0.5,
        partial_tp_stage1_name="1차",
        partial_tp_stage2_name="2차",
        partial_tp_log_prefix="",
        render_trailing_chart=False,
        time_stop_recheck_active_exit=False,
        time_stop_log_prefix="",
        time_stop_close_suffix="시장가 청산",
        entry_time_missing_log_template="{market} entry time missing at {now_str}",
    )
    engine.config = types.SimpleNamespace(min_order_krw=5000.0)
    engine._is_bot_managed_position = lambda _m: True
    return engine, submitted


class TestStrategyKpiImprovements(unittest.TestCase):
    def test_get_min_orderbook_ratio_by_regime(self):
        self.assertEqual(StrategyPolicy.get_min_orderbook_ratio("RISK_OFF"), 1.00)
        self.assertEqual(StrategyPolicy.get_min_orderbook_ratio("BULL_TREND"), 0.95)
        self.assertEqual(StrategyPolicy.get_min_orderbook_ratio("NORMAL"), 0.95)

    def test_orderbook_gate_blocks_weak_bid_in_bull_and_normal(self):
        candles_5m = _make_candles_5m(count=30, base=1000.0, step=4.0, atr_spread=6.0)
        candles_1h = _make_candles_1h(count=25, base=1000.0, step=2.0)

        for regime in ("BULL_TREND", "NORMAL"):
            weak_ob = _make_orderbook(bid_size=85.0, ask_size=100.0)
            res = entry_signal(
                candles=candles_5m,
                candles_1h=candles_1h,
                orderbook=weak_ob,
                btc_regime=regime,
                market=f"KRW-OBTEST-{regime}",
                is_night=False,
            )
            self.assertFalse(res["allow_buy"], f"{regime}에서 ob_ratio=0.85는 차단되어야 함")
            self.assertLess(res.get("factor_breakdown", {}).get("orderbook_smoothed_ratio", 1.0), 0.95)
            self.assertIn("하드게이트 차단", res.get("reason", ""))

    def test_atr_gate_blocks_ultra_low_volatility_alt_but_allows_major(self):
        flat_5m = _make_candles_5m(count=30, base=1000.0, step=0.2, atr_spread=0.5)
        candles_1h = _make_candles_1h(count=25, base=1000.0, step=1.0)
        strong_ob = _make_orderbook(bid_size=130.0, ask_size=100.0)

        alt_res = entry_signal(
            candles=flat_5m,
            candles_1h=candles_1h,
            orderbook=strong_ob,
            btc_regime="BULL_TREND",
            market="KRW-ZKC",
            is_night=False,
        )
        self.assertFalse(alt_res["allow_buy"], "비메이저 알트 극저변동성(ATR% < 0.25%)은 차단되어야 함")

        btc_res = entry_signal(
            candles=flat_5m,
            candles_1h=candles_1h,
            orderbook=strong_ob,
            btc_regime="BULL_TREND",
            market="KRW-BTC",
            is_night=False,
        )
        self.assertNotIn("ATR", str(btc_res.get("reason", "")))

    def test_time_stop_bull_trend_protects_normal_pullback_when_1h_alive(self):
        candles_5m = []
        for i in range(25):
            p = 985.0 if i < 5 else 1005.0
            candles_5m.append({
                "opening_price": p, "high_price": p + 2.0, "low_price": p - 2.0,
                "trade_price": p, "candle_acc_trade_volume": 100.0,
            })
        candles_1h = _make_candles_1h(count=25, base=1010.0, step=0.5)

        engine, submitted = _make_cycle_engine(hold_sec=StrategyPolicy.BULL_TIME_STOP_SECONDS + 60)
        inputs = MarketExitInputs(
            exchange=types.SimpleNamespace(),
            market="KRW-APT",
            korean_name="앱토스",
            coin_available=10.0,
            avg_buy_price=1000.0,
            current_price=990.0,
            coin_value=9900.0,
            candles_5m=candles_5m,
            btc_regime="BULL_TREND",
            now_str="2026-11-25 00:40:00",
            candles_1h=candles_1h,
        )
        exited = engine.process_priority_exits(inputs)
        self.assertFalse(exited, "BULL_TREND에서 1H 추세 생존 + -1.0% 정상 눌림목은 타임스탑 손절 보호되어야 함")
        self.assertEqual(len(submitted), 0)

    def test_time_stop_normal_exits_early_when_5m_and_1h_both_broken(self):
        candles_5m = []
        for i in range(25):
            p = 980.0 if i < 5 else 1005.0
            candles_5m.append({
                "opening_price": p, "high_price": p + 2.0, "low_price": p - 2.0,
                "trade_price": p, "candle_acc_trade_volume": 100.0,
            })
        candles_1h = []
        for i in range(25):
            p = 950.0 if i == 0 else 1000.0
            candles_1h.append({
                "opening_price": p, "high_price": p + 2.0, "low_price": p - 2.0,
                "trade_price": p, "candle_acc_trade_volume": 1000.0,
            })

        engine, submitted = _make_cycle_engine(hold_sec=StrategyPolicy.TIME_STOP_SECONDS_NORMAL * 0.6)
        inputs = MarketExitInputs(
            exchange=types.SimpleNamespace(),
            market="KRW-ZKC",
            korean_name="ZKC",
            coin_available=10.0,
            avg_buy_price=1000.0,
            current_price=992.0,
            coin_value=9920.0,
            candles_5m=candles_5m,
            btc_regime="NORMAL",
            now_str="2026-11-25 00:40:00",
            candles_1h=candles_1h,
        )
        exited = engine.process_priority_exits(inputs)
        self.assertTrue(exited, "NORMAL에서 5m+1H 추세 동시 훼손 시 절반 시간 경과 후 조기 타임스탑 청산되어야 함")
        self.assertEqual(len(submitted), 1)
        self.assertEqual(submitted[0].get("exit_reason"), "TIME_STOP")

    def test_ai_system_instructions_include_orderbook_and_atr_rules(self):
        self.assertIn("0.95", GeminiProvider.SYSTEM_INSTRUCTION)
        self.assertIn("0.25%", GeminiProvider.SYSTEM_INSTRUCTION)
        self.assertIn("0.95", BithumbGeminiProvider.SYSTEM_INSTRUCTION)
        self.assertIn("0.25%", BithumbGeminiProvider.SYSTEM_INSTRUCTION)


if __name__ == "__main__":
    unittest.main()
