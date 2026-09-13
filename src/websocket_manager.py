import json
import logging
import queue
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

import websocket

logger = logging.getLogger(__name__)


from base_websocket import BasePublicWebSocketClient, WebSocketHealthState


class BithumbWebSocketClient(BasePublicWebSocketClient):
    """빗썸 2.0 공식 실시간 웹소켓(WebSocket) 스트리밍 클라이언트

    - wss://ws-api.bithumb.com/websocket/v1 상시 연결
    - 0.1초 단위 실시간 체결가(ticker) 및 대량 체결(whale transaction) 스트리밍
    - 네트워크 단절 시 자동 재연결(Auto-Reconnect) 및 재구독 지원
    - 보유 코인의 실시간 트레일링 스탑 / 긴급 손절 즉시 감시
    - 3,000만 원 이상 고래 대량 시장가 매수 실시간 포착
    """

    WS_URL = "wss://ws-api.bithumb.com/websocket/v1"
    CLIENT_NAME = "빗썸 웹소켓"

    def __init__(
        self,
        initial_markets: list[str] | None = None,
        on_price_callback: Callable[[str, float], None] | None = None,
        on_whale_callback: Callable[[str, float, float, str], None] | None = None,
    ):
        super().__init__(
            initial_markets=initial_markets,
            on_price_callback=on_price_callback,
            on_whale_callback=on_whale_callback,
            ws_url=self.WS_URL,
        )

    def _send_subscription(self):
        if not self.ws or not self.ws.sock or not self.ws.sock.connected:
            return

        with self._lock:
            codes = list(self.subscribed_markets)

        sub_payload = [
            {"ticket": f"bithumb_quant_{uuid.uuid4().hex[:8]}"},
            {"type": "ticker", "codes": codes},
            {"type": "trade", "codes": codes},
            {"format": "DEFAULT"},
        ]

        try:
            self.ws.send(json.dumps(sub_payload))
            logger.debug(f"웹소켓 구독 요청 전송 완료: {codes}")
        except (websocket.WebSocketException, OSError) as e:
            logger.warning(f"웹소켓 구독 전송 실패: {e}")

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

            # 2. 실시간 체결 (Trade) 수신 ➜ 고래 체결 탐지
            elif msg_type == "trade":
                price = float(data.get("trade_price", 0.0))
                qty = float(data.get("trade_volume", 0.0))
                val_krw = price * qty
                ask_bid = data.get("ask_bid", "BID")
                side = "매수" if str(ask_bid).upper() in ("BID", "BUY", "1") else "매도"

                with self._lock:
                    self.last_tick_time = now_ts
                    if code:
                        self.last_tick_time_by_market[code] = now_ts
                        self.confirmed_markets.add(code)
                    self.is_connected = True

                # 3,000만 원 이상 대량 체결 포착 및 이력 누적
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

                    last_t = self._last_whale_time.get(code, 0.0)
                    if now_ts - last_t >= 30:  # 30초 쿨다운
                        self._last_whale_time[code] = now_ts
                        logger.info(
                            f"🐋 [고래 대량 체결 감지] {code} {side} 체결: {val_krw:,.0f}원 ({qty:,.4f}개 @ {price:,.2f}원)"
                        )
                        if self.on_whale_callback:
                            self._enqueue_callback("whale", (code, price, val_krw, side))

        except (json.JSONDecodeError, ValueError, TypeError, KeyError) as exc:
            logger.warning(
                "빗썸 WebSocket 메시지 해석 실패: type=%s, code=%s, error=%s",
                msg_type if "msg_type" in locals() else "unknown",
                code if "code" in locals() else "unknown",
                type(exc).__name__,
            )

