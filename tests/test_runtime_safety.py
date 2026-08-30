import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from heartbeat_monitor import get_heartbeat_health
from runtime_config import get_fraction_setting, load_runtime_risk_settings
from upbit_websocket import UpbitWebSocketClient, WebSocketHealthState


class RuntimeSafetyTests(unittest.TestCase):
    def test_legacy_percent_values_are_normalized_to_decimal_fractions(self):
        with patch.dict(os.environ, {
            "MAX_DAILY_LOSS_PCT": "-5",
            "TRAILING_START_PCT": "2",
            "TRAILING_STOP_PCT": "1.2",
            "BTC_CRASH_THRESHOLD_PCT": "-3",
        }, clear=False):
            self.assertEqual(get_fraction_setting("MAX_DAILY_LOSS_PCT", 0.05), 0.05)
            self.assertEqual(get_fraction_setting("TRAILING_START_PCT", 0.02), 0.02)
            self.assertEqual(get_fraction_setting("TRAILING_STOP_PCT", 0.012), 0.012)
            self.assertEqual(get_fraction_setting("BTC_CRASH_THRESHOLD_PCT", 0.03), 0.03)

    def test_runtime_risk_settings_normalize_the_complete_live_configuration(self):
        with patch.dict(os.environ, {
            "MAX_DAILY_LOSS_PCT": "-5",
            "TRAILING_START_PCT": "2",
            "TRAILING_STOP_PCT": "1.2",
            "BTC_CRASH_THRESHOLD_PCT": "-3",
        }, clear=False):
            settings = load_runtime_risk_settings()
        self.assertEqual(settings.max_daily_loss_pct, 0.05)
        self.assertEqual(settings.trailing_start_pct, 0.02)
        self.assertEqual(settings.trailing_stop_pct, 0.012)
        self.assertEqual(settings.btc_crash_threshold_pct, 0.03)

    def test_invalid_risk_fraction_fails_fast(self):
        with patch.dict(os.environ, {"MAX_DAILY_LOSS_PCT": "100"}, clear=False):
            with self.assertRaises(ValueError):
                get_fraction_setting("MAX_DAILY_LOSS_PCT", 0.05)

    def test_missing_corrupt_stale_and_fresh_heartbeat_are_distinguished(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, ".heartbeat")
            healthy, _, age = get_heartbeat_health(path, now=1000.0)
            self.assertFalse(healthy)
            self.assertIsNone(age)

            with open(path, "w", encoding="utf-8") as file:
                file.write("{invalid")
            healthy, reason, age = get_heartbeat_health(path, now=1000.0)
            self.assertFalse(healthy)
            self.assertIn("읽기 실패", reason)
            self.assertIsNone(age)

            with open(path, "w", encoding="utf-8") as file:
                json.dump({"timestamp": 900.0}, file)
            healthy, _, age = get_heartbeat_health(path, now=1000.0)
            self.assertTrue(healthy)
            self.assertEqual(age, 100.0)

    def test_callback_backlog_is_visible_as_unhealthy(self):
        client = UpbitWebSocketClient(initial_markets=["KRW-BTC"])
        client.is_connected = True
        client.last_tick_time = time.time()
        client.last_tick_time_by_market["KRW-BTC"] = client.last_tick_time
        for _ in range(101):
            client._enqueue_callback("price", ("KRW-BTC", 100.0))

        health = client.get_health_status("KRW-BTC")
        self.assertEqual(health["status"], WebSocketHealthState.PROCESSING_DELAY)
        self.assertFalse(health["is_healthy"])
        self.assertEqual(health["callback_queue_depth"], 101)


if __name__ == "__main__":
    unittest.main()
