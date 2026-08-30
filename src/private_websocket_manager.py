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


class BithumbPrivateWebSocketClient:
    URL = "wss://ws-api.bithumb.com/websocket/v2/private"

    def __init__(self, access_key: str, secret_key: str, on_order: Callable[[dict[str, Any]], None] | None = None, on_asset: Callable[[dict[str, Any]], None] | None = None):
        self.access_key, self.secret_key = access_key, secret_key
        self.on_order, self.on_asset = on_order, on_asset
        self.is_running = False
        self.ws: websocket.WebSocketApp | None = None
        # 수신 스레드는 이벤트 큐에만 기록해 체결 파일과 손익 갱신 경합을 막는다.
        self._order_event_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1000)

    def _headers(self) -> list[str]:
        token = jwt.encode({"access_key": self.access_key, "nonce": str(uuid.uuid4()), "timestamp": int(time.time() * 1000)}, self.secret_key, algorithm="HS256")
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
                        logger.error("빗썸 Private WebSocket 주문 이벤트 큐 포화: REST 대사 전까지 신규 진입을 차단해야 합니다.")
                elif event_type == "myasset" and self.on_asset:
                    self.on_asset(event)
        except (ValueError, TypeError):
            logger.warning("Private WebSocket 메시지 파싱 실패")

    def drain_order_events(self, limit: int = 200) -> int:
        """메인 스레드에서 주문 이벤트를 순차 처리한다."""
        drained = 0
        while drained < limit:
            try:
                event = self._order_event_queue.get_nowait()
            except queue.Empty:
                break
            try:
                if self.on_order:
                    self.on_order(event)
            except Exception as exc:
                logger.warning("빗썸 Private WebSocket 주문 이벤트 후속 처리 실패: %s", exc)
            drained += 1
        return drained

    def start(self) -> None:
        if self.is_running or not self.access_key or not self.secret_key:
            return
        self.is_running = True
        self._reconnect_delay = 2
        def run() -> None:
            while self.is_running:
                self.ws = websocket.WebSocketApp(self.URL, header=self._headers(), on_open=self._on_open, on_message=self._on_message, on_error=lambda _ws, err: logger.warning("Private WebSocket 오류: %s", err))
                self.ws.run_forever(ping_interval=30, ping_timeout=None)
                if self.is_running:
                    time.sleep(self._reconnect_delay)
                    self._reconnect_delay = min(self._reconnect_delay * 2, 30)
        threading.Thread(target=run, daemon=True, name="BithumbPrivateWebSocket").start()

    def stop(self) -> None:
        self.is_running = False
        if self.ws:
            self.ws.close()
