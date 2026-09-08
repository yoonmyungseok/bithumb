"""신규 상장(NEW_LISTING) 판정·자격·정책 SSOT·청산 단위 테스트 (PR1~PR3)."""

import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from risk_manager import TrailingStopTracker
from strategy_engine import (
    StrategyPolicy,
    classify_listing_maturity,
    get_kst_now,
    get_minimum_raw_5m_candles,
    get_new_listing_alpha_threshold,
    get_oldest_candle_age_hours,
    has_confirmed_swing_trend_candles,
    is_new_listing_eligible,
    is_within_new_listing_age_window,
    should_block_for_minimum_candles,
)


def _make_candles(count: int, price: float = 100.0, unit_minutes: int = 5) -> list[dict]:
    """최신순 캔들 목록을 생성한다. index 0이 가장 최신."""
    now = get_kst_now()
    candles: list[dict] = []
    for idx in range(count):
        ts = now - timedelta(minutes=unit_minutes * idx)
        candles.append({
            "trade_price": price,
            "opening_price": price * 0.99,
            "high_price": price * 1.01,
            "candle_acc_trade_volume": 1000.0 + idx,
            "candle_date_time_kst": ts.strftime("%Y-%m-%dT%H:%M:%S"),
        })
    return candles


def _make_4h_candles(count: int, age_hours_from_now: float = 1.0) -> list[dict]:
    now = get_kst_now()
    candles: list[dict] = []
    for idx in range(count):
        ts = now - timedelta(hours=age_hours_from_now + (4 * idx))
        candles.append({
            "trade_price": 300.0,
            "candle_date_time_kst": ts.strftime("%Y-%m-%dT%H:%M:%S"),
        })
    return candles


class NewListingPolicyTests(unittest.TestCase):
    """StrategyPolicy 신규상장 상수와 환경 변수 헬퍼를 검증한다."""

    def test_new_listing_alpha_threshold_by_session(self):
        self.assertEqual(get_new_listing_alpha_threshold("NORMAL", is_night=False), 75)
        self.assertEqual(get_new_listing_alpha_threshold("NORMAL", is_night=True), 80)
        self.assertEqual(get_new_listing_alpha_threshold("RISK_OFF", is_night=False), 80)
        self.assertEqual(get_new_listing_alpha_threshold("RISK_OFF", is_night=True), 85)

    def test_is_new_listing_enabled_respects_env(self):
        with patch.dict(os.environ, {"NEW_LISTING_ENABLED": "false"}, clear=False):
            self.assertFalse(StrategyPolicy.is_new_listing_enabled())
        with patch.dict(os.environ, {"NEW_LISTING_ENABLED": "true"}, clear=False):
            self.assertTrue(StrategyPolicy.is_new_listing_enabled())

    def test_default_is_observation_and_exchange_override_has_priority(self):
        # 기본값은 후보 관찰은 허용하지만 어느 거래소에도 실주문을 허용하지 않는다.
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(StrategyPolicy.is_new_listing_enabled("bithumb"))
            self.assertFalse(StrategyPolicy.is_new_listing_enforcement_enabled("bithumb"))
            self.assertFalse(StrategyPolicy.is_new_listing_enforcement_enabled("upbit"))
        with patch.dict(os.environ, {
            "NEW_LISTING_ENFORCEMENT": "false",
            "BITHUMB_NEW_LISTING_ENFORCEMENT": "true",
            "UPBIT_NEW_LISTING_ENFORCEMENT": "false",
        }, clear=True):
            self.assertTrue(StrategyPolicy.is_new_listing_enforcement_enabled("bithumb"))
            self.assertFalse(StrategyPolicy.is_new_listing_enforcement_enabled("upbit"))

    def test_exit_and_alloc_policy_defaults(self):
        self.assertEqual(StrategyPolicy.NEW_LISTING_ALLOC_RATIO, 0.15)
        self.assertEqual(StrategyPolicy.NEW_LISTING_STOP_LOSS_PCT, 0.025)
        self.assertEqual(StrategyPolicy.NEW_LISTING_HARD_STOP_PCT, 0.040)
        self.assertEqual(StrategyPolicy.NEW_LISTING_PARTIAL_TP_PCT, 0.030)
        self.assertEqual(StrategyPolicy.NEW_LISTING_PARTIAL_TP_RATIO, 0.50)
        self.assertEqual(StrategyPolicy.NEW_LISTING_TRAILING_START_PCT, 0.040)
        self.assertEqual(StrategyPolicy.NEW_LISTING_TRAILING_DROP_PCT, 0.020)
        self.assertEqual(StrategyPolicy.NEW_LISTING_TIME_STOP_SECONDS, 3600)
        self.assertEqual(StrategyPolicy.NEW_LISTING_EARLY_EXIT_SECONDS, 1800)
        self.assertEqual(StrategyPolicy.NEW_LISTING_EARLY_EXIT_MIN_PNL_PCT, -0.50)
        self.assertEqual(StrategyPolicy.NEW_LISTING_EARLY_EXIT_MAX_PNL_PCT, 0.30)
        self.assertEqual(StrategyPolicy.NEW_LISTING_REENTRY_COOLDOWN_SEC, 3600.0)


class NewListingCooldownTests(unittest.TestCase):
    """NEW_LISTING 청산 후 재진입 쿨다운(3600초)을 검증한다."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _cooldown(self):
        from order_safety.cooldown import CooldownManager

        return CooldownManager(
            default_sl_cooldown=1800.0,
            default_tp_cooldown=1800.0,
            default_time_stop_cooldown=600.0,
            data_dir=self.temp_dir,
        )

    def test_new_listing_time_stop_uses_sixty_minute_reentry_cooldown(self):
        cd = self._cooldown()
        cd.record_exit("KRW-NEW", "NEW_LISTING_TIME_STOP", exit_price=1000.0)
        is_cd, rem = cd.is_in_cooldown("KRW-NEW")
        self.assertTrue(is_cd)
        self.assertGreater(rem, 3500.0)
        allowed, reason = cd.check_reentry_allowed("KRW-NEW", 1005.0)
        self.assertFalse(allowed)
        self.assertIn("신규상장 재진입 쿨다운", reason)

    def test_new_listing_early_exit_uses_sixty_minute_reentry_cooldown(self):
        cd = self._cooldown()
        cd.record_exit("KRW-NEW", "NEW_LISTING_EARLY_EXIT", exit_price=1002.0)
        is_cd, rem = cd.is_in_cooldown("KRW-NEW")
        self.assertTrue(is_cd)
        self.assertGreater(rem, 3500.0)
        allowed, reason = cd.check_reentry_allowed("KRW-NEW", 1000.0)
        self.assertFalse(allowed)
        self.assertIn("신규상장 재진입 쿨다운", reason)


class NewListingSlotIsolationTests(unittest.TestCase):
    """RiskGuard Triple-Track 신규상장 슬롯 격리를 검증한다."""

    def test_new_listing_alloc_ratio_caps_position_pct(self):
        from risk_controls import RiskGuard

        guard = RiskGuard(
            min_order_krw=5_000.0,
            max_open_positions=3,
            max_position_pct=0.35,
            max_total_exposure_pct=0.90,
            max_order_krw=0.0,
            max_swing_positions=1,
            max_new_listing_positions=1,
        )
        # 15% 비중 한도 = 0.35 * 0.15 = 5.25% → 60,000원 주문은 차단
        allowed, reason = guard.validate_buy(
            "KRW-NEW",
            order_krw=60_000.0,
            available_krw=500_000.0,
            total_equity=1_000_000.0,
            held_markets=[],
            strategy_mode="NEW_LISTING",
            held_swing_markets=[],
            held_new_listing_markets=[],
        )
        self.assertFalse(allowed)
        self.assertIn("종목당 비중 한도 초과", reason)

class NewListingExitTests(unittest.TestCase):
    """TrailingStopTracker NEW_LISTING 전용 청산 파라미터를 검증한다."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.tracker = TrailingStopTracker(data_dir=self.temp_dir)
        self.market = "KRW-NEW"
        self.avg_buy_price = 1000.0

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_new_listing_strategy_mode_helpers(self):
        self.tracker.set_strategy_mode(self.market, "NEW_LISTING")
        self.assertTrue(self.tracker.is_new_listing_position(self.market))
        self.assertFalse(self.tracker.is_swing_position(self.market))
        self.assertEqual(self.tracker.get_strategy_mode(self.market), "NEW_LISTING")
        self.assertIn(self.market, self.tracker.get_new_listing_markets())

    def test_new_listing_partial_take_profit_at_three_percent(self):
        self.tracker.set_strategy_mode(self.market, "NEW_LISTING")
        action, _, _, _, _ = self.tracker.check_position(
            self.market, 1030.0, self.avg_buy_price,
        )
        self.assertEqual(action, "PARTIAL_TP_1")

    def test_new_listing_no_second_partial_take_profit(self):
        self.tracker.set_strategy_mode(self.market, "NEW_LISTING")
        self.tracker.mark_partial_take_profit_filled(self.market, 1, 0.5)
        action, _, _, _, _ = self.tracker.check_position(
            self.market, 1060.0, self.avg_buy_price,
        )
        self.assertNotEqual(action, "PARTIAL_TP_2")

    def test_new_listing_trailing_stop_after_four_percent_peak(self):
        self.tracker.set_strategy_mode(self.market, "NEW_LISTING")
        self.tracker.peaks[self.market] = 1045.0  # +4.5%
        action, _, _, _, _ = self.tracker.check_position(
            self.market, 1020.0, self.avg_buy_price,  # 1045 * 0.98 = 1024.1 이하
        )
        self.assertEqual(action, "TRAILING_STOP")

    def test_new_listing_below_partial_tp_threshold_holds(self):
        self.tracker.set_strategy_mode(self.market, "NEW_LISTING")
        action, _, _, _, _ = self.tracker.check_position(
            self.market, 1025.0, self.avg_buy_price,
        )
        self.assertEqual(action, "NONE")


class NewListingRuntimeExitTests(unittest.TestCase):
    """trading_runtime.process_priority_exits NEW_LISTING 타임스탑 분기를 검증한다."""

    def test_new_listing_time_stop_triggers_at_sixty_minutes(self):
        import types
        from trading_runtime import MarketExitInputs, TradingCycleEngine

        submitted = []
        entry_ts = time.time() - 3700.0

        class DummyExecutor:
            def submit(self, *args, **kwargs):
                submitted.append(kwargs)
                return {"uuid": "test"}

        trailing_tracker = types.SimpleNamespace(
            check_position=lambda *args, **kwargs: ("NONE", 0.0, 0.0, 0.0, 0.0),
            is_swing_position=lambda m: False,
            is_new_listing_position=lambda m: True,
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
            time_stop_recheck_active_exit=True,
            time_stop_log_prefix="",
            time_stop_close_suffix="시장가 청산",
            entry_time_missing_log_template="{market} entry time missing at {now_str}",
        )
        engine.config = types.SimpleNamespace(min_order_krw=5000.0)

        def bot_managed(_m):
            return True

        engine._is_bot_managed_position = bot_managed

        should_continue = engine.process_priority_exits(MarketExitInputs(
            exchange=types.SimpleNamespace(),
            market="KRW-NEW",
            korean_name="신규코인",
            coin_available=100.0,
            avg_buy_price=1000.0,
            current_price=1005.0,
            coin_value=100500.0,
            candles_5m=[{"trade_price": 1000.0} for _ in range(20)],
            btc_regime="NORMAL",
            now_str="2026-09-08 16:00:00",
        ))

        self.assertTrue(should_continue)
        self.assertEqual(submitted[-1]["exit_reason"], "NEW_LISTING_TIME_STOP")

    def test_new_listing_early_exit_triggers_in_flat_pnl_band(self):
        import types
        from trading_runtime import MarketExitInputs, TradingCycleEngine

        submitted = []
        entry_ts = time.time() - 1900.0

        trailing_tracker = types.SimpleNamespace(
            check_position=lambda *args, **kwargs: ("NONE", 0.0, 0.0, 0.0, 0.0),
            is_swing_position=lambda m: False,
            is_new_listing_position=lambda m: True,
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
            order_executor=types.SimpleNamespace(submit=lambda *a, **k: submitted.append(k) or {"uuid": "test"}),
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
            time_stop_recheck_active_exit=True,
            time_stop_log_prefix="",
            time_stop_close_suffix="시장가 청산",
            entry_time_missing_log_template="{market} entry time missing at {now_str}",
        )
        engine.config = types.SimpleNamespace(min_order_krw=5000.0)
        engine._is_bot_managed_position = lambda _m: True

        engine.process_priority_exits(MarketExitInputs(
            exchange=types.SimpleNamespace(),
            market="KRW-NEW",
            korean_name="신규코인",
            coin_available=100.0,
            avg_buy_price=1000.0,
            current_price=1002.0,
            coin_value=100200.0,
            candles_5m=[{"trade_price": 1000.0} for _ in range(20)],
            btc_regime="NORMAL",
            now_str="2026-09-08 16:00:00",
        ))

        self.assertEqual(submitted[-1]["exit_reason"], "NEW_LISTING_EARLY_EXIT")


class ListingMaturityTests(unittest.TestCase):
    """classify_listing_maturity 판정 경계를 검증한다."""

    def test_minimum_raw_candle_requirements_by_maturity(self):
        self.assertEqual(get_minimum_raw_5m_candles("NEW_LISTING"), 6)
        self.assertEqual(get_minimum_raw_5m_candles("MATURE"), 20)
        self.assertEqual(get_minimum_raw_5m_candles("INSUFFICIENT"), 0)

    def test_should_block_for_minimum_candles_respects_new_listing_exception(self):
        candles_11 = _make_candles(11)
        self.assertFalse(
            should_block_for_minimum_candles(True, "NEW_LISTING", candles_11),
        )
        self.assertTrue(
            should_block_for_minimum_candles(True, "MATURE", candles_11),
        )
        self.assertFalse(
            should_block_for_minimum_candles(True, "INSUFFICIENT", candles_11),
        )
        self.assertFalse(
            should_block_for_minimum_candles(False, "MATURE", candles_11),
        )

    def test_mature_when_four_hour_confirmed_candles_exist(self):
        candles_4h = [{"trade_price": 100.0} for _ in range(21)]
        candles_5m = _make_candles(6)
        self.assertEqual(
            classify_listing_maturity(candles_4h, None, candles_5m),
            "MATURE",
        )

    def test_new_listing_when_four_hour_sparse_and_five_minute_enough(self):
        """USELESS 유형: 4H 1봉 + 5분 6봉 이상 → NEW_LISTING."""
        candles_4h = _make_4h_candles(1)
        candles_5m = _make_candles(6)
        self.assertFalse(has_confirmed_swing_trend_candles(candles_4h))
        self.assertEqual(
            classify_listing_maturity(candles_4h, None, candles_5m),
            "NEW_LISTING",
        )

    def test_insufficient_when_five_minute_completed_below_minimum(self):
        candles_4h = _make_4h_candles(1)
        candles_5m = _make_candles(5)  # 확정 4개만 확보
        self.assertEqual(
            classify_listing_maturity(candles_4h, None, candles_5m),
            "INSUFFICIENT",
        )

    def test_empty_or_unavailable_four_hour_history_is_not_new_listing(self):
        """4H 조회 장애와 5분 정상 수신이 함께 발생해도 신규상장 우회 매수를 허용하지 않는다."""
        self.assertEqual(
            classify_listing_maturity([], None, _make_candles(8), "UNAVAILABLE"),
            "INSUFFICIENT",
        )
        self.assertEqual(
            classify_listing_maturity([], None, _make_candles(8), "AVAILABLE"),
            "INSUFFICIENT",
        )

    def test_insufficient_when_listing_age_exceeds_window(self):
        candles_4h = _make_4h_candles(3, age_hours_from_now=80.0)
        candles_5m = _make_candles(8)
        self.assertFalse(is_within_new_listing_age_window(candles_4h, max_age_hours=72))
        self.assertEqual(
            classify_listing_maturity(candles_4h, None, candles_5m),
            "INSUFFICIENT",
        )

    def test_oldest_candle_age_hours_parses_kst_timestamp(self):
        candles = _make_4h_candles(2, age_hours_from_now=10.0)
        age = get_oldest_candle_age_hours(candles)
        self.assertIsNotNone(age)
        self.assertGreaterEqual(age, 14.0)
        self.assertLess(age, 20.0)


class NewListingEligibilityTests(unittest.TestCase):
    """is_new_listing_eligible 하드 게이트를 검증한다."""

    def _eligible_inputs(self) -> dict:
        return {
            "maturity": "NEW_LISTING",
            "acc_trade_price_24h": 3_100_000_000.0,
            "change_rate": 0.0615,
            "relative_strength": 0.069,
            "btc_regime": "NORMAL",
        }

    def test_eligible_for_typical_new_listing_leader(self):
        ok, reason = is_new_listing_eligible(**self._eligible_inputs())
        self.assertTrue(ok)
        self.assertIn("자격 충족", reason)

    def test_reject_when_maturity_not_new_listing(self):
        ok, reason = is_new_listing_eligible(
            "MATURE",
            acc_trade_price_24h=5_000_000_000.0,
            change_rate=0.05,
            relative_strength=0.02,
            btc_regime="NORMAL",
        )
        self.assertFalse(ok)
        self.assertIn("신규상장 경로 아님", reason)

    def test_reject_risk_off_regime_when_change_rate_below_stricter_floor(self):
        ok, reason = is_new_listing_eligible(
            **{**self._eligible_inputs(), "btc_regime": "RISK_OFF", "change_rate": 0.018},
        )
        self.assertFalse(ok)
        self.assertIn("상승률 부족", reason)

    def test_eligible_in_risk_off_with_stricter_gates(self):
        ok, reason = is_new_listing_eligible(
            **{
                **self._eligible_inputs(),
                "btc_regime": "RISK_OFF",
                "change_rate": 0.035,
                "relative_strength": 0.02,
            },
        )
        self.assertTrue(ok)
        self.assertIn("자격 충족", reason)

    def test_reject_crash_regime(self):
        ok, reason = is_new_listing_eligible(
            **{**self._eligible_inputs(), "btc_regime": "CRASH"},
        )
        self.assertFalse(ok)
        self.assertIn("CRASH", reason)

    def test_reject_low_trade_value(self):
        ok, reason = is_new_listing_eligible(
            **{**self._eligible_inputs(), "acc_trade_price_24h": 1_000_000_000.0},
        )
        self.assertFalse(ok)
        self.assertIn("거래대금 부족", reason)

    def test_reject_overheated_change_rate(self):
        ok, reason = is_new_listing_eligible(
            **{**self._eligible_inputs(), "change_rate": 0.12},
        )
        self.assertFalse(ok)
        self.assertIn("상승률 과열", reason)

    def test_reject_low_relative_strength(self):
        ok, reason = is_new_listing_eligible(
            **{**self._eligible_inputs(), "relative_strength": 0.005},
        )
        self.assertFalse(ok)
        self.assertIn("RS 부족", reason)

    def test_reject_when_feature_disabled(self):
        with patch.dict(os.environ, {"NEW_LISTING_ENABLED": "false"}, clear=False):
            ok, reason = is_new_listing_eligible(**self._eligible_inputs())
        self.assertFalse(ok)
        self.assertIn("비활성화", reason)


class NewListingEntrySignalTests(unittest.TestCase):
    """new_listing_entry_signal 5분봉 돌파 경계를 검증한다."""

    def test_allows_breakout_with_sufficient_alpha(self):
        candles = []
        base = 300.0
        for idx in range(6):
            price = base + idx * 2.0
            candles.append({
                "trade_price": price,
                "opening_price": price - 1.0,
                "high_price": price + 0.5,
                "low_price": price - 1.5,
                "candle_acc_trade_volume": 1000.0 * (idx + 1),
            })
        candles[0]["trade_price"] = base + 20.0
        candles[0]["high_price"] = base + 21.0
        candles[0]["opening_price"] = base + 18.0
        candles[0]["candle_acc_trade_volume"] = 8000.0

        from strategy_engine import new_listing_entry_signal

        signal = new_listing_entry_signal(
            candles=candles,
            btc_regime="NORMAL",
            orderbook={"total_bid_size": 1500.0, "total_ask_size": 1000.0},
            relative_strength=0.02,
        )
        self.assertTrue(signal["allow_buy"])
        self.assertEqual(signal["entry_type"], "NEW_LISTING")


if __name__ == "__main__":
    unittest.main()
