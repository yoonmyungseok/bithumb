"""매매 사이클 성능·Gemini 장애 관측 회귀 테스트."""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from ai_provider import AIProviderTelemetry, BithumbGeminiProvider
from cycle_observability import (
    CycleGeminiDerivedBlockAggregator,
    assert_cycle_prefix_phase_order,
    get_kst_today_str,
)
from gemini_telemetry import GeminiTelemetry, get_pt_today_str
from trading_runtime import TradingCycleEngine


class CycleObservabilityTests(unittest.TestCase):
    def test_prefix_phase_order_guard(self):
        assert_cycle_prefix_phase_order(["reconcile", "portfolio", "regime", "market_selection"])
        with self.assertRaises(AssertionError):
            assert_cycle_prefix_phase_order(["market_selection", "reconcile"])

    def test_derived_buy_block_aggregation(self):
        logger = logging.getLogger("test_cycle_obs")
        agg = CycleGeminiDerivedBlockAggregator("bithumb", "20260101120000", logger)
        reason = "빗썸 Gemini 분석 장애(http_error, trading)로 신규 BUY를 차단합니다."
        for market in ("KRW-BTC", "KRW-ETH", "KRW-XRP"):
            agg.record_derived_buy_block(market, reason)
        snap = agg.snapshot()
        self.assertEqual(snap["derived_market_count"], 3)
        self.assertEqual(snap["root_incident_count"], 1)
        self.assertEqual(snap["by_incident"]["http_error"]["market_count"], 3)

    def test_gemini_http_failure_blocks_new_buy(self):
        temp_dir = tempfile.mkdtemp()
        try:
            AIProviderTelemetry.configure(data_dir=temp_dir, storage_filename="obs_bithumb.json")
            AIProviderTelemetry.reset(persist=True)
            with patch("ai_provider.requests.post") as mock_post:
                mock_post.return_value = MagicMock(status_code=503)
                provider = BithumbGeminiProvider("bithumb-test-key")
                result = provider.complete_json(
                    "분석",
                    ["gemini-3.5-flash-lite"],
                    {"type": "object", "required": [], "properties": {}},
                    context="KRW-TEST",
                    timeout=1.0,
                    max_tokens=64,
                )
            self.assertIsNone(result.value)
            snap = AIProviderTelemetry.snapshot("bithumb")
            self.assertTrue(snap["entry_safety"]["entry_blocked"])
            self.assertGreaterEqual(snap["observability"]["root_api_failures"], 1)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_holding_exit_path_before_provider_buy_gate(self):
        """청산 경로(process_priority_exits)가 진입 게이트보다 먼저 호출되는지 소스 순서로 검증한다."""
        import inspect

        source = inspect.getsource(TradingCycleEngine.run_market_loop)
        exit_pos = source.find("process_priority_exits")
        entry_pos = source.find("process_entry_gating")
        self.assertGreater(exit_pos, 0)
        self.assertGreater(entry_pos, 0)
        self.assertLess(exit_pos, entry_pos)

    def test_bithumb_upbit_telemetry_isolation(self):
        temp_dir = tempfile.mkdtemp()
        try:
            AIProviderTelemetry.configure(data_dir=temp_dir, storage_filename="obs_bithumb_only.json")
            AIProviderTelemetry.reset(persist=True)
            AIProviderTelemetry.record_derived_buy_block("bithumb")
            AIProviderTelemetry.record_derived_buy_block("bithumb")
            AIProviderTelemetry.record_derived_buy_block("upbit")
            b_snap = AIProviderTelemetry.snapshot("bithumb")["observability"]
            u_snap = AIProviderTelemetry.snapshot("upbit")["observability"]
            self.assertEqual(b_snap["derived_buy_blocks"], 2)
            self.assertEqual(u_snap["derived_buy_blocks"], 1)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_gemini_telemetry_kst_date_fields(self):
        temp_dir = tempfile.mkdtemp()
        try:
            GeminiTelemetry.configure(data_dir=temp_dir)
            GeminiTelemetry.reset(persist=True)
            GeminiTelemetry.record_api_success("gemini-3.5-flash-lite", "KRW-BTC")
            payload = GeminiTelemetry.snapshot().to_dict()
            self.assertEqual(payload["date_kst"], get_kst_today_str())
            self.assertEqual(payload["quota_date_pt"], get_pt_today_str())
            self.assertEqual(payload["date"], get_kst_today_str())
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_performance_metrics_do_not_change_order_contract(self):
        """성능 계측 필드 추가가 주문 실행 경계 시그니처를 바꾸지 않는다."""
        import inspect

        sig_buy = inspect.signature(TradingCycleEngine.process_buy_execution)
        sig_exit = inspect.signature(TradingCycleEngine.process_priority_exits)
        self.assertIn("market_inputs", sig_buy.parameters)
        self.assertIn("market_inputs", sig_exit.parameters)


if __name__ == "__main__":
    unittest.main()
