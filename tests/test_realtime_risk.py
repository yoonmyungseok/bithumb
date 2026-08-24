import os
import sys
import types
import unittest

try:
    import requests  # noqa: F401
except ModuleNotFoundError:
    module = types.ModuleType("requests")
    module.exceptions = types.SimpleNamespace(
        RequestException=Exception,
        Timeout=TimeoutError,
        ConnectionError=ConnectionError,
    )
    sys.modules["requests"] = module

if "jwt" not in sys.modules:
    sys.modules["jwt"] = types.SimpleNamespace(encode=lambda *args, **kwargs: "test-token")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from strategy_engine import (
    calculate_atr,
    calculate_bollinger_bands,
    calculate_ema,
    calculate_macd,
    calculate_rsi,
    entry_signal,
)
from gemini_analyzer import GeminiAnalyzer


class RealtimeRiskAndIndicatorTests(unittest.TestCase):
    def test_indicator_consistency_between_engine_and_analyzer(self):
        prices = [100.0 + i * 0.5 for i in range(30)]
        
        # RSI consistency
        engine_rsi = calculate_rsi(prices, 14)
        analyzer_rsi = GeminiAnalyzer.calculate_rsi(prices, 14)
        self.assertEqual(engine_rsi, analyzer_rsi)

        # Bollinger Bands consistency
        engine_bb = calculate_bollinger_bands(prices, 20)
        analyzer_bb = GeminiAnalyzer.calculate_bollinger_bands(prices, 20)
        self.assertEqual(engine_bb, analyzer_bb)

        # EMA consistency
        engine_ema = calculate_ema(prices, 12)
        analyzer_ema = GeminiAnalyzer.calculate_ema(prices, 12)
        self.assertEqual(engine_ema, analyzer_ema)

        # MACD consistency
        engine_macd = calculate_macd(prices)
        analyzer = GeminiAnalyzer(api_key="dummy")
        analyzer_macd = analyzer.calculate_macd(prices)
        self.assertEqual(engine_macd, analyzer_macd)

    def test_entry_signal_output_keys(self):
        candles = []
        for i in range(30):
            p = 1000 + i * 2
            candles.append({
                "trade_price": p,
                "high_price": p + 5,
                "low_price": p - 5,
                "opening_price": p - 1,
            })
        signal = entry_signal(candles)
        self.assertIn("allow_buy", signal)
        self.assertIn("reason", signal)
        self.assertIn("entry_price", signal)
        self.assertIn("target_price", signal)
        self.assertIn("stop_loss", signal)
        self.assertIn("rsi", signal)
        self.assertIn("pct_b", signal)

    def test_private_websocket_list_message_handling(self):
        import json
        from private_websocket_manager import BithumbPrivateWebSocketClient
        
        received_orders = []
        received_assets = []
        
        client = BithumbPrivateWebSocketClient(
            access_key="test-key",
            secret_key="test-secret",
            on_order=lambda ev: received_orders.append(ev),
            on_asset=lambda ev: received_assets.append(ev),
        )
        
        # Test handling a list of events
        payload = json.dumps([
            {"type": "myOrder", "order_id": "ord-1", "state": "trade"},
            {"type": "myAsset", "currency": "KRW", "balance": "1000000"},
        ])
        client._on_message(None, payload)
        
        self.assertEqual(len(received_orders), 1)
        self.assertEqual(received_orders[0]["order_id"], "ord-1")
        self.assertEqual(len(received_assets), 1)
        self.assertEqual(received_assets[0]["currency"], "KRW")

    def test_bithumb_api_cancel_order_fallback_to_v1(self):
        from bithumb_api import BithumbAPI
        
        api = BithumbAPI(access_key="test-key", secret_key="test-secret")
        calls = []
        
        def mock_request(method, endpoint, params=None, data=None, api_version="v2"):
            calls.append({"method": method, "endpoint": endpoint, "api_version": api_version, "params": params})
            if api_version == "v2":
                raise requests.exceptions.RequestException("v2 404 error")
            return {"status": "0000", "uuid": params.get("uuid")}
        
        api._request = mock_request
        res = api.cancel_order(uuid_str="uuid-123")
        
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["api_version"], "v2")
        self.assertEqual(calls[1]["api_version"], "v1")
        self.assertEqual(res["status"], "0000")

    def test_telegram_debouncing(self):
        from telegram_alert import TelegramAlert
        telegram = TelegramAlert(bot_token="test-token", chat_id="12345")
        
        sent_messages = []
        def mock_send(text, parse_mode="HTML", reply_markup=None):
            sent_messages.append(text)
            return True
        telegram.send_message = mock_send

        # 첫 번째 발송: 성공
        res1 = telegram.send_debounced_message("test_cat", "경보 1", min_interval_sec=10.0)
        self.assertTrue(res1)
        self.assertEqual(len(sent_messages), 1)

        # 10초 이내 두 번째 발송: 디바운싱 차단 (False)
        res2 = telegram.send_debounced_message("test_cat", "경보 2", min_interval_sec=10.0)
        self.assertFalse(res2)
        self.assertEqual(len(sent_messages), 1)

        # 다른 카테고리 발송: 성공
        res3 = telegram.send_debounced_message("other_cat", "경보 3", min_interval_sec=10.0)
        self.assertTrue(res3)
        self.assertEqual(len(sent_messages), 2)

    def test_bithumb_api_session_pooling(self):
        from bithumb_api import BithumbAPI
        api = BithumbAPI(access_key="test-key", secret_key="test-secret")
        self.assertIsNotNone(api.session)
        self.assertTrue(hasattr(api.session, "get"))
        self.assertTrue(hasattr(api.session, "post"))


if __name__ == "__main__":
    unittest.main()
