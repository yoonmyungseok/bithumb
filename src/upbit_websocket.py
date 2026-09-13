"""
업비트(Upbit) 실시간 Public 웹소켓(WebSocket) 스트리밍 클라이언트
- wss://api.upbit.com/websocket/v1 엔드포인트 상시 연결
- 0.1초 단위 실시간 시세(ticker) 및 대량 체결(trade / whale transactions) 스트리밍
- 네트워크 단절 시 지연 백오프 자동 재연결(Auto-Reconnect) 및 동적 구독 복구
- 3,000만 원 이상 고래 대량 시장가 매수/매도 실시간 포착
"""

import json
import logging
import os
import queue
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any
"""
업비트(Upbit) 실시간 Public 웹소켓(WebSocket) 스트리밍 클라이언트
- wss://api.upbit.com/websocket/v1 엔드포인트 상시 연결
- 0.1초 단위 실시간 시세(ticker) 및 대량 체결(trade / whale transactions) 스트리밍
- 네트워크 단절 시 지연 백오프 자동 재연결(Auto-Reconnect) 및 동적 구독 복구
- 3,000만 원 이상 고래 대량 시장가 매수/매도 실시간 포착
"""

import json
import logging
import os
import queue
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

import websocket

logger = logging.getLogger(__name__)


from base_websocket import BasePublicWebSocketClient, WebSocketHealthState


class UpbitWebSocketClient(BasePublicWebSocketClient):
    """업비트 실시간 Public WebSocket 클라이언트"""

    WS_URL = "wss://api.upbit.com/websocket/v1"
    CLIENT_NAME = "업비트 웹소켓"

    def __init__(
        self,
        initial_markets: list[str] | None = None,
        on_price_callback: Callable[[str, float], None] | None = None,
        on_whale_callback: Callable[[str, float, float, str], None] | None = None,
    ):
        configured_url = os.getenv("UPBIT_WEBSOCKET_URL", self.WS_URL).strip()
        super().__init__(
            initial_markets=initial_markets,
            on_price_callback=on_price_callback,
            on_whale_callback=on_whale_callback,
            ws_url=configured_url,
        )

    def _send_subscription(self):
        if not self.ws or not self.ws.sock or not self.ws.sock.connected:
            return

        with self._lock:
            codes = list(self.subscribed_markets)

        sub_payload = [
            {"ticket": f"upbit_quant_{uuid.uuid4().hex[:8]}"},
            {"type": "ticker", "codes": codes},
            {"type": "trade", "codes": codes},
            {"format": "DEFAULT"},
        ]

        try:
            self.ws.send(json.dumps(sub_payload))
            logger.debug(f"업비트 웹소켓 구독 요청 전송 완료: {codes}")
        except Exception as e:
            logger.warning(f"업비트 웹소켓 구독 전송 실패: {e}")

    def _on_message(self, ws: Any, message: Any):
        try:
            raw = message.decode("utf-8") if isinstance(message, bytes) else str(message)
            data = json.loads(raw)
            if not isinstance(data, dict):
                return

            msg_type = data.get("type", "")
            code = data.get("code", "")

            now_ts = time.time()
            # 1. 실시간 시세 (Ticker) 수신
            if msg_type == "ticker":
                price = float(data.get("trade_price", 0.0))
                if code and price > 0:
                    with self._lock:
                        self.latest_prices[code] = price
                        self.last_tick_time = now_ts
                        self.last_tick_time_by_market[code] = now_ts
                        self.confirmed_markets.add(code)
                        self.is_connected = True

                    should_enqueue = False
                    if self.on_price_callback:
                        with self._lock:
                            last_p = self._last_callback_prices.get(code)
                            if last_p is None or abs(price - last_p) > 1e-9:
                                self._last_callback_prices[code] = price
                                should_enqueue = True
                        if should_enqueue:
                            self._enqueue_callback("price", (code, price))

            # 2. 실시간 체결 (Trade) 수신 ➜ 고래 대량 체결 탐지
            elif msg_type == "trade":
                price = float(data.get("trade_price", 0.0))
                qty = float(data.get("trade_volume", 0.0))
                val_krw = price * qty
                ask_bid = str(data.get("ask_bid", "BID")).upper()
                side = "매수" if ask_bid in ("BID", "BUY", "1") else "매도"

                with self._lock:
                    self.last_tick_time = now_ts
                    if code:
                        self.last_tick_time_by_market[code] = now_ts
                        self.confirmed_markets.add(code)
                    self.is_connected = True

                # 3,000만 원 이상 대량 체결 포착
                if val_krw >= 30_000_000 and price > 0:
                    now_ts = time.time()
                    with self._lock:
                        self._whale_trades.append({
                            "ts": now_ts,
                            "market": code,
                            "side": side,
                            "val_krw": val_krw,
                            "price": price,
                            "qty": qty,
                        })

                    # 빗썸과 같은 30초 종목별 쿨다운으로 고래 이벤트 폭주를 막는다.
                    last_t = self._last_whale_time.get(code, 0.0)
                    if now_ts - last_t >= 30:
                        self._last_whale_time[code] = now_ts
                        logger.info("🐋 [업비트 고래 대량 체결 감지] %s %s 체결: %s원", code, side, f"{val_krw:,.0f}")
                        if self.on_whale_callback:
                            self._enqueue_callback("whale", (code, price, val_krw, side))

        except (json.JSONDecodeError, ValueError, TypeError, KeyError) as exc:
            # 비정상 수신은 주문으로 이어지지 않으며, 원인 추적용 최소 정보만 남긴다.
            logger.warning(
                "업비트 WebSocket 메시지 해석 실패: type=%s, code=%s, error=%s",
                msg_type if "msg_type" in locals() else "unknown",
                code if "code" in locals() else "unknown",
                type(exc).__name__,
            )
