import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from db_manager import get_db_manager, get_exchange_db_path
from gemini_telemetry import GeminiTelemetry
from operational_quality import build_slippage_enforcement_readiness
from risk_manager import get_kst_now


import shutil

class GeminiTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        GeminiTelemetry.configure(data_dir=self.temp_dir)
        GeminiTelemetry.reset(persist=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        data_dir = os.path.join(project_root, "data")
        GeminiTelemetry.configure(data_dir=data_dir)

    def test_snapshot_tracks_api_and_fallback_counters(self):
        GeminiTelemetry.record_api_success("gemini-flash-lite-latest", "KRW-BTC")
        GeminiTelemetry.record_rate_limited("gemini-flash-lite-latest", "KRW-ETH")
        GeminiTelemetry.record_local_fallback("KRW-XRP", "quota exceeded")
        GeminiTelemetry.record_cache_hit("KRW-BTC")

        snap = GeminiTelemetry.snapshot().to_dict()
        self.assertEqual(snap["api_calls"], 2)
        self.assertEqual(snap["api_success"], 1)
        self.assertEqual(snap["rate_limited"], 1)
        self.assertEqual(snap["local_fallback"], 1)
        self.assertEqual(snap["cache_hits"], 1)


class SlippageReadinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.data_dir = self.tmp_dir.name
        self.db = get_db_manager(get_exchange_db_path(self.data_dir))

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_readiness_requires_five_trading_days(self):
        now = get_kst_now().timestamp()
        for day in range(3):
            self.db.record_strategy_decision(
                exchange="bithumb",
                cycle_id=f"cycle-{day}",
                market="KRW-BTC",
                action="OBSERVED",
                policy_mode="ORDERBOOK_SLIPPAGE",
                block_reasons=["예상 슬리피지 관찰"],
                payload={"estimated_slippage_bps": 20.0 + day},
                decision_ts=now - (2 - day) * 86_400,
            )

        report = build_slippage_enforcement_readiness(
            self.db,
            exchange="bithumb",
            data_dir=self.data_dir,
            enforcement_enabled=False,
        )
        self.assertEqual(report.observed_count, 3)
        self.assertFalse(report.ready_for_enforcement)

    def test_readiness_marks_ready_after_five_days(self):
        now = get_kst_now().timestamp()
        for day in range(5):
            self.db.record_strategy_decision(
                exchange="bithumb",
                cycle_id=f"cycle-{day}",
                market="KRW-BTC",
                action="OBSERVED",
                policy_mode="ORDERBOOK_SLIPPAGE",
                block_reasons=["예상 슬리피지 관찰"],
                payload={"estimated_slippage_bps": 15.0},
                decision_ts=now - (4 - day) * 86_400,
            )

        report = build_slippage_enforcement_readiness(
            self.db,
            exchange="bithumb",
            data_dir=self.data_dir,
            enforcement_enabled=False,
        )
        self.assertTrue(report.ready_for_enforcement)
        self.assertEqual(report.trading_days_observed, 5)


if __name__ == "__main__":
    unittest.main()
