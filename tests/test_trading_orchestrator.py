import os
import sys
import time
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from exchange_adapter import BithumbAdapter
from trading_orchestrator import TradingOrchestrator


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
        self.assertTrue(logger.warning.called)
        log_args = logger.warning.call_args.args
        self.assertIn("slack=", log_args[0])
        self.assertIn("마켓선정=", log_args[5])
        self.assertIn("느린마켓=", log_args[0])

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


if __name__ == "__main__":
    unittest.main()
