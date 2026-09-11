import os
import sys
import time
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from exchange_adapter import BithumbAdapter
from trading_orchestrator import (
    DEFAULT_STRATEGY_INPUT_PREFETCH_TTL_SEC,
    TradingOrchestrator,
    resolve_strategy_input_prefetch_ttl,
)


class CountingExchangeClient:
    def __init__(self):
        self.candle_calls = 0
        self.orderbook_calls = 0
        self.price_calls = 0

    def get_balances(self):
        return {
            "KRW": {"balance": 1_000_000.0, "locked": 0.0},
            "XRP": {"balance": 0.0, "locked": 0.0, "avg_buy_price": 0.0},
        }

    def get_candles(self, unit=5, count=30, market="KRW-BTC", to=None):
        self.candle_calls += 1
        return [{"market": market, "trade_price": 100.0, "unit": unit} for _ in range(max(count, 5))]

    def get_orderbook(self, market="KRW-BTC"):
        self.orderbook_calls += 1
        return {"market": market, "orderbook_units": [{"ask_price": 101.0, "bid_price": 99.0}]}

    def get_current_price(self, market="KRW-BTC"):
        self.price_calls += 1
        return 100.0

    def get_korean_name(self, market="KRW-BTC"):
        return market

    def get_tickers(self, markets):
        return [{"market": market, "trade_price": 100.0} for market in markets]

    def get_orderbooks(self, markets):
        return [{"market": market, "orderbook_units": []} for market in markets]


class TradingOrchestratorPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.client = CountingExchangeClient()
        self.adapter = BithumbAdapter(self.client)
        self.orchestrator = TradingOrchestrator(__import__("logging").getLogger("test_trading_orchestrator"))

    def _fresh_prefetch(self, market: str = "KRW-XRP") -> dict:
        return {
            "price": 123.0,
            "orderbook": {"market": market, "prefetched": True, "orderbook_units": []},
            "observed_at": time.monotonic(),
        }

    def test_slow_cycle_detail_logs_phase_and_safe_market_timing(self):
        """목표 초과 시에만 단계·종목 시간과 실제 주기 여유를 함께 기록한다."""
        logger = MagicMock()
        orchestrator = TradingOrchestrator(logger)

        logged = orchestrator.log_slow_cycle_detail(
            cycle_id="2026-09-09 19:44:37",
            total_seconds=16.5,
            interval_seconds=300.0,
            timings={"주문대사": 0.1, "마켓선정": 3.2, "마켓루프": 7.4},
            slow_markets=[("KRW-XRP", 2.4), ("KRW-LIT", 3.1)],
        )

        self.assertTrue(logged)
        self.assertTrue(logger.info.called)
        log_args = logger.info.call_args.args
        self.assertIn("slack=", log_args[0])
        self.assertIn("마켓선정=", log_args[5])
        self.assertIn("느린마켓=", log_args[0])

        # 슬랙 여유 부족(<=20%) 시 WARNING 기록 검증
        logger.reset_mock()
        orchestrator.log_slow_cycle_detail(
            cycle_id="2026-09-09 19:44:37",
            total_seconds=250.0,
            interval_seconds=300.0,
            timings={"마켓루프": 240.0},
            slow_markets=[],
        )
        self.assertTrue(logger.warning.called)

    def test_normal_cycle_does_not_write_detail_log(self):
        """정상 사이클은 상세 성능 로그를 남기지 않아 운영 로그 폭증을 막는다."""
        logger = MagicMock()
        orchestrator = TradingOrchestrator(logger)

        logged = orchestrator.log_slow_cycle_detail(
            cycle_id="2026-09-09 19:44:37",
            total_seconds=14.9,
            interval_seconds=300.0,
            timings={},
            slow_markets=[],
        )

        self.assertFalse(logged)
        logger.warning.assert_not_called()

    def test_record_latency_only_logs_every_20_calls(self):
        """40회 버퍼 포화 후에도 매 사이클 찍히지 않고 20회 주기로만 요약/경고가 출력되는지 검증."""
        logger = MagicMock()
        orchestrator = TradingOrchestrator(logger)

        # 1~19회 호출: 로그가 전혀 발생하지 않아야 함
        for _ in range(19):
            orchestrator.record_latency("full_cycle", 50.0)
        self.assertEqual(logger.info.call_count, 0)
        self.assertEqual(logger.warning.call_count, 0)

        # 20회째 호출: 20회 요약 및 p95 > 45 경고 1회 발생
        orchestrator.record_latency("full_cycle", 50.0)
        self.assertEqual(logger.info.call_count, 1)
        self.assertEqual(logger.warning.call_count, 1)

        # 21~39회 호출: 로그 추가 발생 없어야 함
        for _ in range(19):
            orchestrator.record_latency("full_cycle", 50.0)
        self.assertEqual(logger.info.call_count, 1)
        self.assertEqual(logger.warning.call_count, 1)

        # 40회째 호출: 2번째 요약 및 경고 발생
        orchestrator.record_latency("full_cycle", 50.0)
        self.assertEqual(logger.info.call_count, 2)
        self.assertEqual(logger.warning.call_count, 2)

        # 41~59회 호출: maxlen=40 버퍼가 꽉 찬 상태에서도 매 사이클 찍히지 않고 침묵 유지해야 함
        for _ in range(19):
            orchestrator.record_latency("full_cycle", 50.0)
        self.assertEqual(logger.info.call_count, 2)
        self.assertEqual(logger.warning.call_count, 2)

        # 60회째 호출: 3번째 요약 및 경고 발생
        orchestrator.record_latency("full_cycle", 16.0)
        self.assertEqual(logger.info.call_count, 3)
        self.assertEqual(logger.warning.call_count, 3)

    def test_priority_eval_snapshot_uses_prefetch_and_skips_balance(self):
        prefetched = self._fresh_prefetch()
        snap = self.orchestrator.load_priority_eval_snapshot(
            self.adapter, "KRW-XRP", 5, prefetched,
        )

        self.assertTrue(snap.is_priority_eval_only)
        self.assertEqual(snap.current_price, 123.0)
        self.assertTrue(snap.orderbook.get("prefetched"))
        self.assertEqual(snap.listing_maturity, "MATURE")
        self.assertEqual(snap.candles_5m[0]["market"], "KRW-XRP")
        self.assertEqual(snap.candles_1h[0]["unit"], 60)
        self.assertEqual(self.client.orderbook_calls, 0)
        self.assertEqual(self.client.price_calls, 0)
        self.assertGreaterEqual(self.client.candle_calls, 3)

    def test_full_snapshot_reuses_priority_snapshot_candles(self):
        prefetched = self._fresh_prefetch()
        candle_cache = {
            "KRW-XRP": {
                "candles_5m": [{"market": "KRW-XRP", "trade_price": 50.0, "unit": 5}] * 30,
                "candles_1h": [{"market": "KRW-XRP", "trade_price": 50.0, "unit": 60}] * 50,
                "candles_4h": [{"market": "KRW-XRP", "trade_price": 50.0, "unit": 240}] * 25,
                "four_hour_history_status": "AVAILABLE",
            }
        }
        priority = self.orchestrator.load_priority_eval_snapshot(
            self.adapter, "KRW-XRP", 5, prefetched, candle_cache=candle_cache,
        )
        before_candles = self.client.candle_calls

        full = self.orchestrator.load_market_snapshot(
            self.adapter,
            "KRW-XRP",
            5,
            prefetched,
            candle_cache=candle_cache,
            priority_snapshot=priority,
        )

        self.assertFalse(full.is_priority_eval_only)
        self.assertEqual(full.candles_5m, priority.candles_5m)
        self.assertEqual(full.candles_1h, priority.candles_1h)
        self.assertEqual(full.candles_4h, priority.candles_4h)
        self.assertEqual(full.listing_maturity, priority.listing_maturity)
        self.assertEqual(full.korean_name, "KRW-XRP")
        self.assertEqual(self.client.candle_calls, before_candles)

    def test_prefetch_market_inputs_reuses_ticker_seed(self):
        seed = {"KRW-XRP": {"market": "KRW-XRP", "trade_price": 777.0}}
        result = self.orchestrator.prefetch_market_inputs(
            self.adapter, ["KRW-XRP"], ticker_seed=seed,
        )

        self.assertEqual(result["KRW-XRP"]["price"], 777.0)
        self.assertIn("orderbook", result["KRW-XRP"])

    def test_prefetch_cycle_candles_populates_cache(self):
        cache = self.orchestrator.prefetch_cycle_candles(
            self.adapter, ["KRW-A", "KRW-B", "KRW-C"], 5, max_workers=2,
        )

        self.assertEqual(set(cache.keys()), {"KRW-A", "KRW-B", "KRW-C"})
        for payload in cache.values():
            self.assertIn("candles_5m", payload)
            self.assertIn("candles_1h", payload)
            self.assertIn("candles_4h", payload)

    def test_prefetch_cycle_candles_without_4h_defers_history(self):
        before = self.client.candle_calls
        cache = self.orchestrator.prefetch_cycle_candles(
            self.adapter, ["KRW-A"], 5, max_workers=1, prefetch_4h=False,
        )

        payload = cache["KRW-A"]
        self.assertNotIn("candles_4h", payload)
        self.assertEqual(payload.get("four_hour_history_status"), "DEFERRED")
        # 5분+1시간만 조회 (종목당 2회)
        self.assertEqual(self.client.candle_calls - before, 2)

    def test_resolve_cached_candles_load_4h_false_skips_rest(self):
        candle_cache = {
            "KRW-XRP": {
                "candles_5m": [{"market": "KRW-XRP", "trade_price": 50.0}] * 30,
                "candles_1h": [{"market": "KRW-XRP", "trade_price": 50.0, "unit": 60}] * 50,
                "four_hour_history_status": "DEFERRED",
            }
        }
        before = self.client.candle_calls
        candles_5m, candles_1h, four_hour = TradingOrchestrator._resolve_cached_candles(
            "KRW-XRP", 5, self.adapter, candle_cache, load_4h=False,
        )

        self.assertEqual(len(candles_5m), 30)
        self.assertEqual(len(candles_1h), 50)
        self.assertEqual(four_hour.status, "DEFERRED")
        self.assertEqual(self.client.candle_calls, before)

    def test_needs_four_hour_candles_conditions(self):
        self.assertTrue(
            TradingOrchestrator.needs_four_hour_candles(
                "KRW-A", is_held_swing=True, candidate_metadata={},
            )
        )
        self.assertTrue(
            TradingOrchestrator.needs_four_hour_candles(
                "KRW-A",
                is_held_swing=False,
                candidate_metadata={"strategy_mode": "SWING"},
            )
        )
        self.assertTrue(
            TradingOrchestrator.needs_four_hour_candles(
                "KRW-A",
                is_held_swing=False,
                candidate_metadata={"candidate_type": "SWING"},
            )
        )
        self.assertTrue(
            TradingOrchestrator.needs_four_hour_candles(
                "KRW-A",
                is_held_swing=False,
                candidate_metadata={"new_listing_screener_verified": True},
            )
        )
        self.assertFalse(
            TradingOrchestrator.needs_four_hour_candles(
                "KRW-A",
                is_held_swing=False,
                candidate_metadata={"candidate_type": "CONFIRMED"},
            )
        )

    def test_cap_cycle_target_markets_preserves_held_and_order(self):
        target = ["KRW-A", "KRW-B", "KRW-C", "KRW-D", "KRW-E", "KRW-F", "KRW-G", "KRW-H"]
        held = ["KRW-B", "KRW-F"]
        capped = TradingOrchestrator.cap_cycle_target_markets(target, held, 6)
        self.assertEqual(capped, ["KRW-B", "KRW-F", "KRW-A", "KRW-C", "KRW-D", "KRW-E"])

    def test_strategy_input_prefetch_ttl_defaults_and_caps_by_interval(self):
        self.assertEqual(resolve_strategy_input_prefetch_ttl(5), DEFAULT_STRATEGY_INPUT_PREFETCH_TTL_SEC)
        self.assertEqual(resolve_strategy_input_prefetch_ttl(5, configured_ttl=200.0), 150.0)

    def test_prefetch_reused_after_candle_prefetch_delay(self):
        """캔들 사전조회(~1.6s) 이후 마켓루프에서도 prefetch가 유효해야 한다."""
        self.orchestrator.configure_strategy_input_prefetch_ttl(5)
        prefetched = self._fresh_prefetch()
        prefetched["observed_at"] = time.monotonic() - 2.5

        snap = self.orchestrator.load_market_snapshot(self.adapter, "KRW-XRP", 5, prefetched)

        self.assertEqual(snap.current_price, 123.0)
        self.assertEqual(self.client.price_calls, 0)
        self.assertEqual(self.client.orderbook_calls, 0)

    def test_resolve_cycle_btc_candles_reuses_prefetch_cache(self):
        candle_cache = {
            "KRW-BTC": {
                "candles_5m": [{"market": "KRW-BTC", "trade_price": 100.0}] * 15,
            }
        }
        before = self.client.candle_calls
        candles = TradingOrchestrator.resolve_cycle_btc_candles_5m(self.adapter, candle_cache, 5)

        self.assertEqual(len(candles), 15)
        self.assertEqual(self.client.candle_calls, before)

    def test_resolve_cycle_btc_candles_fetches_when_cache_missing(self):
        before = self.client.candle_calls
        candles = TradingOrchestrator.resolve_cycle_btc_candles_5m(self.adapter, {}, 5)

        self.assertGreaterEqual(len(candles), 10)
        self.assertEqual(self.client.candle_calls, before + 1)

    def test_classify_market_regime_passes_background_true(self):
        """classify_market_regime이 analyzer의 diagnose_macro_regime을 background=True로 호출하는지 검증"""
        analyzer = MagicMock()
        analyzer.diagnose_macro_regime.return_value = {
            "regime": "CAUTION_PULLBACK",
            "risk_score": 65,
            "summary": "AI 눌림목 경보",
        }

        is_crashing, regime, reason = self.orchestrator.classify_market_regime(
            self.adapter,
            interval_minutes=5,
            crash_threshold_pct=0.015,
            analyzer=analyzer,
            fng_index={"desc": "탐욕"},
        )

        self.assertFalse(is_crashing)
        self.assertEqual(regime, "RISK_OFF")
        self.assertIn("AI: AI 눌림목 경보", reason)
        analyzer.diagnose_macro_regime.assert_called_once()
        self.assertTrue(analyzer.diagnose_macro_regime.call_args.kwargs.get("background"))

    def test_classify_market_regime_legacy_analyzer_type_error_fallback(self):
        """background 인자를 지원하지 않는 구버전 analyzer에서 TypeError 발생 시 자동 폴백하는지 검증"""
        analyzer = MagicMock()

        def mock_legacy_diagnose(btc_candles_1h, fng_index=None, **kwargs):
            if "background" in kwargs:
                raise TypeError("diagnose_macro_regime() got an unexpected keyword argument 'background'")
            return {"regime": "CAUTION_PULLBACK", "summary": "레거시 폴백"}

        analyzer.diagnose_macro_regime.side_effect = mock_legacy_diagnose

        is_crashing, regime, reason = self.orchestrator.classify_market_regime(
            self.adapter,
            interval_minutes=5,
            crash_threshold_pct=0.015,
            analyzer=analyzer,
        )

        self.assertFalse(is_crashing)
        self.assertEqual(regime, "RISK_OFF")
        self.assertIn("레거시 폴백", reason)
        self.assertEqual(analyzer.diagnose_macro_regime.call_count, 2)

    def test_slow_cycle_detail_logs_market_selection_breakdown(self):
        """15초 초과 사이클 상세 로그에 마켓선정 세부 항목이 정상 포맷팅되어 노출되는지 검증."""
        logger = MagicMock()
        orchestrator = TradingOrchestrator(logger)

        logged = orchestrator.log_slow_cycle_detail(
            cycle_id="2026-09-10 10:45:00",
            total_seconds=16.0,
            interval_seconds=300.0,
            timings={
                "주문대사": 0.05,
                "마켓선정": 10.806,
                "후보스캔": 2.941,
                "AI후보랭킹": 3.339,
                "스윙스캔": 4.421,
                "기타": 0.105,
                "마켓루프": 4.5,
            },
            slow_markets=[],
        )

        self.assertTrue(logged)
        self.assertTrue(logger.info.called)
        log_args = logger.info.call_args.args
        phase_str = log_args[5]
        expected_part = "마켓선정=10.806s (후보스캔=2.941s AI후보랭킹=3.339s 스윙스캔=4.421s 기타=0.105s)"
        self.assertIn(expected_part, phase_str)

    def test_select_target_markets_collects_breakdown_metrics_and_preserves_candidates(self):
        """AI 랭킹 유무, 스윙 스캔 지원 시 마켓 후보 결과 및 세부 계측 수집 검증."""
        mock_screener = MagicMock()
        mock_screener.scan_markets.return_value = [
            {"market": "KRW-BTC", "is_held": True},
            {"market": "KRW-ETH", "candidate_type": "CONFIRMED"},
        ]
        mock_screener.last_scan_tickers = [{"market": "KRW-BTC"}, {"market": "KRW-ETH"}]
        mock_screener.last_scan_metrics = {
            "candidate_scan": 1.234,
            "ai_ranking": 0.567,
        }
        mock_screener.scan_swing_markets.return_value = [
            {"market": "KRW-SOL", "candidate_type": "SWING"},
        ]

        metrics = {}
        screened_meta = []
        result = self.orchestrator.select_target_markets(
            self.adapter,
            held_markets=["KRW-BTC"],
            is_auto_mode=True,
            raw_markets="",
            max_positions=3,
            top_count=2,
            create_screener=lambda: mock_screener,
            btc_regime="NORMAL",
            on_screened_candidates=screened_meta.extend,
            metrics=metrics,
        )

        self.assertEqual(result, ["KRW-BTC", "KRW-ETH", "KRW-SOL"])
        self.assertEqual(metrics["후보스캔"], 1.234)
        self.assertEqual(metrics["AI후보랭킹"], 0.567)
        self.assertGreaterEqual(metrics["스윙스캔"], 0.0)
        self.assertEqual(len(screened_meta), 3)

    def test_select_target_markets_fallback_without_metrics_and_swing_exception(self):
        """세부 계측이 없는 구버전 스크리너나 스윙 스캔 예외 시에도 후보 결과가 보존되고 폴백하는지 검증."""
        class LegacyScreener:
            def scan_markets(self, top_count=3, held_markets=None, btc_regime="NORMAL"):
                return [{"market": "KRW-BTC"}, {"market": "KRW-XRP"}]

            def scan_swing_markets(self, **kwargs):
                raise RuntimeError("스윙 스캔 장애")

        metrics = {}
        result = self.orchestrator.select_target_markets(
            self.adapter,
            held_markets=["KRW-BTC"],
            is_auto_mode=True,
            raw_markets="",
            max_positions=3,
            top_count=2,
            create_screener=lambda: LegacyScreener(),
            btc_regime="NORMAL",
            metrics=metrics,
        )

        self.assertEqual(result, ["KRW-BTC", "KRW-XRP"])
        self.assertEqual(metrics["후보스캔"], 0.0)
        self.assertEqual(metrics["AI후보랭킹"], 0.0)
        self.assertGreaterEqual(metrics["스윙스캔"], 0.0)


if __name__ == "__main__":
    unittest.main()
