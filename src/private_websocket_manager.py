"""Private v2 MyOrder/MyAsset stream with bounded reconnects."""

import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

import jwt
import websocket

logger = logging.getLogger(__name__)


class BithumbPrivateWebSocketClient:
    URL = "wss://ws-api.bithumb.com/websocket/v2/private"

    def __init__(self, access_key: str, secret_key: str, on_order: Callable[[dict[str, Any]], None] | None = None, on_asset: Callable[[dict[str, Any]], None] | None = None):
        self.access_key, self.secret_key = access_key, secret_key
        self.on_order, self.on_asset = on_order, on_asset
        self.is_running = False
        self.ws: websocket.WebSocketApp | None = None

    def _headers(self) -> list[str]:
        token = jwt.encode({"access_key": self.access_key, "nonce": str(uuid.uuid4()), "timestamp": int(time.time() * 1000)}, self.secret_key, algorithm="HS256")
        return [f"Authorization: Bearer {token}"]

    def _on_open(self, ws: Any) -> None:
        ws.send(json.dumps([
            {"ticket": f"quant-private-{uuid.uuid4().hex[:8]}"},
            {"type": "myOrder"},
            {"type": "myAsset"},
            {"format": "DEFAULT"},
        ]))
        logger.info("Private v2 WebSocket 연결 및 MyOrder/MyAsset 구독 완료")

    def _on_message(self, ws: Any, message: Any) -> None:
        try:
            raw = json.loads(message.decode() if isinstance(message, bytes) else message)
            events = raw if isinstance(raw, list) else [raw] if isinstance(raw, dict) else []
            for event in events:
                if not isinstance(event, dict):
                    continue
                event_type = str(event.get("type", "")).lower()
                if event_type == "myorder" and self.on_order:
                    self.on_order(event)
                elif event_type == "myasset" and self.on_asset:
                    self.on_asset(event)
        except (ValueError, TypeError):
            logger.warning("Private WebSocket 메시지 파싱 실패")

    def start(self) -> None:
        if self.is_running or not self.access_key or not self.secret_key:
            return
        self.is_running = True
        def run() -> None:
            delay = 2
            while self.is_running:
                self.ws = websocket.WebSocketApp(self.URL, header=self._headers(), on_open=self._on_open, on_message=self._on_message, on_error=lambda _ws, err: logger.warning("Private WebSocket 오류: %s", err))
                self.ws.run_forever(ping_interval=30, ping_timeout=None)
                if self.is_running:
                    time.sleep(delay)
                    delay = min(delay * 2, 30)
        threading.Thread(target=run, daemon=True, name="BithumbPrivateWebSocket").start()

    def stop(self) -> None:
        self.is_running = False
        if self.ws:
            self.ws.close()
