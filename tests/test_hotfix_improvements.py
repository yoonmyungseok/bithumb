import os
import sys
import time
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from websocket_manager import BithumbWebSocketClient, WebSocketHealthState
from upbit_websocket import UpbitWebSocketClient, WebSocketHealthState as UpbitWebSocketHealthState
from strategy_engine import entry_signal


class TestHotfixImprovements(unittest.TestCase):
    def test_bithumb_websocket_grace_period_and_stale_tolerance(self):
        client = BithumbWebSocketClient(initial_markets=["KRW-BTC"])
        client.is_connected = True
        client.last_tick_time = time.time()

        # 1. 신규 구독 종목 (KRW-CYS) Grace Period 검증
        client.update_subscriptions(["KRW-CYS", "KRW-BTC"])
        health_new = client.get_health_status(market="KRW-CYS")
        self.assertTrue(health_new["is_healthy"])
        self.assertEqual(health_new["status"], WebSocketHealthState.DATA_AVAILABLE)

        # 2. 30초 전 틱을 받은 알트코인은 45초 기본 임계치 내에서 정상이어야 함
        client.last_tick_time_by_market["KRW-CYS"] = time.time() - 30.0
        client.market_subscription_time["KRW-CYS"] = time.time() - 100.0  # grace period 지남
        health_alt = client.get_health_status(market="KRW-CYS")
        self.assertTrue(health_alt["is_healthy"])
        self.assertEqual(health_alt["status"], WebSocketHealthState.DATA_AVAILABLE)

        # 3. 50초 전 틱은 STALE이어야 함
        client.last_tick_time_by_market["KRW-CYS"] = time.time() - 50.0
        health_stale = client.get_health_status(market="KRW-CYS")
        self.assertFalse(health_stale["is_healthy"])
        self.assertEqual(health_stale["status"], WebSocketHealthState.STALE)

    def test_bithumb_websocket_callback_drain_resets_delay(self):
        client = BithumbWebSocketClient(initial_markets=["KRW-BTC"])
        client.is_connected = True
        client.last_tick_time = time.time()

        # 콜백 큐에 이전 사이클 작업 인큐
        client._enqueue_callback("price", ("KRW-BTC", 100000000.0))
        time.sleep(0.01)
        client.drain_callbacks()

        # 드레인 후 큐가 비어있으므로 _last_callback_delay_seconds가 0으로 리셋되어야 함
        self.assertEqual(client._last_callback_delay_seconds, 0.0)
        health = client.get_health_status()
        self.assertTrue(health["is_healthy"])

    def test_upbit_websocket_grace_period_and_stale_tolerance(self):
        client = UpbitWebSocketClient(initial_markets=["KRW-BTC"])
        client.is_connected = True
        client.last_tick_time = time.time()

        # 1. 신규 구독 종목 Grace Period
        client.update_subscriptions(["KRW-MIRA", "KRW-BTC"])
        health_new = client.get_health_status(market="KRW-MIRA")
        self.assertTrue(health_new["is_healthy"])

        # 2. 25초 전 틱 알트코인은 정상
        client.last_tick_time_by_market["KRW-MIRA"] = time.time() - 25.0
        client.market_subscription_time["KRW-MIRA"] = time.time() - 120.0
        health_alt = client.get_health_status(market="KRW-MIRA")
        self.assertTrue(health_alt["is_healthy"])
        self.assertEqual(health_alt["status"], UpbitWebSocketHealthState.DATA_AVAILABLE)

    def test_strategy_engine_risk_off_high_alpha_gating(self):
        # 30개 5분봉 캔들 생성 (우상향 및 안정적 변동)
        base_price = 1000.0
        candles_5m = []
        for i in range(30):
            p = base_price + (30 - i) * 1.5
            candles_5m.append({
                "trade_price": p,
                "opening_price": p - 0.5,
                "high_price": p + 1.0,
                "low_price": p - 0.5,
                "candle_acc_trade_volume": 1000.0,
            })

        # 25개 1시간봉 캔들 (EMA20 인근 지지)
        candles_1h = []
        for i in range(25):
            p = base_price + (25 - i) * 2.0
            candles_1h.append({
                "trade_price": p,
                "opening_price": p - 1.0,
                "high_price": p + 2.0,
                "low_price": p - 1.0,
                "candle_acc_trade_volume": 50000.0,
            })

        # RISK_OFF 레짐에서 진입 신호 생성
        sig = entry_signal(
            candles=candles_5m,
            candles_1h=candles_1h,
            btc_regime="RISK_OFF",
            market="KRW-TEST",
            exchange="bithumb",
        )

        # 캔들과 가격이 정상이므로 오류 없이 결과 반환 확인
        self.assertIn("allow_buy", sig)
        self.assertIn("target_price", sig)
        self.assertIn("stop_loss", sig)
        self.assertGreater(sig["target_price"], sig["entry_price"])
        self.assertLess(sig["stop_loss"], sig["entry_price"])


if __name__ == "__main__":
    unittest.main()
