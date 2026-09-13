"""Private v2 MyOrder/MyAsset stream with bounded reconnects."""

import json
import logging
import queue
import threading
import time
import uuid
import warnings
from collections.abc import Callable
from typing import Any

import jwt
import websocket

try:
    from jwt.warnings import InsecureKeyLengthWarning
    warnings.filterwarnings("ignore", category=InsecureKeyLengthWarning)
except ImportError:
    pass

logger = logging.getLogger(__name__)


from base_websocket import BasePrivateWebSocketClient


class BithumbPrivateWebSocketClient(BasePrivateWebSocketClient):
    """빗썸 Private v2 WebSocket 클라이언트 (MyOrder, MyAsset)"""

    URL = "wss://ws-api.bithumb.com/websocket/v2/private"
    CLIENT_NAME = "빗썸 Private WebSocket"

    def __init__(
        self,
        access_key: str,
        secret_key: str,
        on_order: Callable[[dict[str, Any]], None] | None = None,
        on_asset: Callable[[dict[str, Any]], None] | None = None,
        on_queue_overflow: Callable[[], None] | None = None,
    ):
        super().__init__(
            access_key=access_key,
            secret_key=secret_key,
            on_order=on_order,
            on_asset=on_asset,
            on_queue_overflow=on_queue_overflow,
            ws_url=self.URL,
        )

    def _headers(self) -> list[str]:
        token = jwt.encode(
            {"access_key": self.access_key, "nonce": str(uuid.uuid4()), "timestamp": int(time.time() * 1000)},
            self.secret_key,
            algorithm="HS256",
        )
        return [f"Authorization: Bearer {token}"]

    def _on_open(self, ws: Any) -> None:
        self._reconnect_delay = 2
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
                    try:
                        self._order_event_queue.put_nowait(event)
                    except queue.Full:
                        self._handle_order_queue_overflow()
                elif event_type == "myasset" and self.on_asset:
                    self.on_asset(event)
        except (ValueError, TypeError):
            logger.warning("Private WebSocket 메시지 파싱 실패")

