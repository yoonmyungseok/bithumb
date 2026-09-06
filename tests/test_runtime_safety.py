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
from websocket_manager import BithumbWebSocketClient


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
        # 가격 병합은 같은 종목에만 적용되므로 서로 다른 종목을 적재해 실제 적체 상태를 만든다.
        for index in range(101):
            client._enqueue_callback("price", (f"KRW-TEST{index}", 100.0))

        health = client.get_health_status("KRW-BTC")
        self.assertEqual(health["status"], WebSocketHealthState.PROCESSING_DELAY)
        self.assertFalse(health["is_healthy"])
        self.assertEqual(health["callback_queue_depth"], 101)

    def test_duplicate_price_ticks_skip_callback_enqueue(self):
        # 빗썸 중복 틱 필터링 검증
        received_bithumb = []
        bithumb_ws = BithumbWebSocketClient(
            initial_markets=["KRW-BTC"],
            on_price_callback=lambda m, p: received_bithumb.append((m, p)),
        )
        # 동일 가격 틱 5회 연속 수신 시뮬레이션
        msg = json.dumps({"type": "ticker", "code": "KRW-BTC", "trade_price": 50000000.0})
        for _ in range(5):
            bithumb_ws._on_message(None, msg)

        self.assertEqual(bithumb_ws._callback_queue.qsize(), 1, "동일 가격 틱은 최초 1회만 큐에 인큐되어야 함")

        # 가격 변동은 이미 대기 중인 종목의 오래된 이벤트를 늘리지 않고 최신 캐시로 병합한다.
        msg_changed = json.dumps({"type": "ticker", "code": "KRW-BTC", "trade_price": 50100000.0})
        bithumb_ws._on_message(None, msg_changed)
        self.assertEqual(bithumb_ws._callback_queue.qsize(), 1, "같은 종목 가격 틱은 하나만 대기해야 함")
        bithumb_ws.drain_callbacks()
        self.assertEqual(received_bithumb, [("KRW-BTC", 50100000.0)], "처리 시에는 최신 캐시 가격을 사용해야 함")

        # 업비트 중복 틱 필터링 검증
        received_upbit = []
        upbit_ws = UpbitWebSocketClient(
            initial_markets=["KRW-BTC"],
            on_price_callback=lambda m, p: received_upbit.append((m, p)),
        )
        msg_upbit = json.dumps({"type": "ticker", "code": "KRW-BTC", "trade_price": 50000000.0})
        for _ in range(5):
            upbit_ws._on_message(None, msg_upbit)

        self.assertEqual(upbit_ws._callback_queue.qsize(), 1, "업비트에서도 동일 가격 틱은 최초 1회만 큐에 인큐되어야 함")
        msg_upbit_changed = json.dumps({"type": "ticker", "code": "KRW-BTC", "trade_price": 50100000.0})
        upbit_ws._on_message(None, msg_upbit_changed)
        self.assertEqual(upbit_ws._callback_queue.qsize(), 1, "업비트도 같은 종목 가격 틱을 병합해야 함")
        upbit_ws.drain_callbacks()
        self.assertEqual(received_upbit, [("KRW-BTC", 50100000.0)], "업비트 처리 시에도 최신 캐시 가격을 사용해야 함")

    def test_upbit_whale_callback_is_rate_limited_and_invalid_message_is_observable(self):
        """업비트 고래 이벤트는 폭주를 막고 비정상 수신은 주문 없이 경고로 남겨야 한다."""
        received_whales = []
        client = UpbitWebSocketClient(
            initial_markets=["KRW-BTC"],
            on_whale_callback=lambda *args: received_whales.append(args),
        )
        whale_message = json.dumps({
            "type": "trade", "code": "KRW-BTC", "trade_price": 100000000.0,
            "trade_volume": 1.0, "ask_bid": "BID",
        })
        client._on_message(None, whale_message)
        client._on_message(None, whale_message)
        self.assertEqual(client._callback_queue.qsize(), 1, "같은 종목 고래 이벤트는 30초 내 한 번만 대기해야 함")
        client.drain_callbacks()
        self.assertEqual(len(received_whales), 1)

        with self.assertLogs("upbit_websocket", level="WARNING") as logs:
            client._on_message(None, "{invalid")
        self.assertIn("업비트 WebSocket 메시지 해석 실패", logs.output[0])


if __name__ == "__main__":
    unittest.main()
