"""
업비트(Upbit) Private 실시간 웹소켓(WebSocket) 스트리밍 클라이언트
- wss://api.upbit.com/websocket/v1/private 엔드포인트 상시 연결
- JWT Authorization 헤더를 통한 공식 인증
- 실시간 내 주문(myOrder) 및 내 자산(myAsset) 변동 이벤트 수신
- OrderJournal과 직접 연동하여 0.1초 체결 상태 즉각 반영
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

import jwt
import websocket

logger = logging.getLogger(__name__)


class UpbitPrivateWebSocketClient:
    """
    업비트 실시간 Private WebSocket 클라이언트 (myOrder, myAsset)
    """

    URL = "wss://api.upbit.com/websocket/v1/private"

    def __init__(
        self,
        access_key: str = "",
        secret_key: str = "",
        on_order: Callable[[dict[str, Any]], None] | None = None,
        on_asset: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.access_key = (access_key or os.getenv("UPBIT_ACCESS_KEY", "")).strip()
        self.secret_key = (secret_key or os.getenv("UPBIT_SECRET_KEY", "")).strip()
        self.ws_url = os.getenv("UPBIT_PRIVATE_WEBSOCKET_URL", self.URL).strip()
        self.on_order = on_order
        self.on_asset = on_asset
        self.is_running = False
        self.ws: websocket.WebSocketApp | None = None
        self._thread: threading.Thread | None = None
        # Private WebSocket 수신 스레드에서는 영속화·손익 계산을 하지 않는다.
        self._order_event_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1000)

    def _headers(self) -> list[str]:
        """업비트 Private WebSocket용 JWT 토큰 헤더 생성"""
        payload = {
            "access_key": self.access_key,
            "nonce": str(uuid.uuid4()),
        }
        token = jwt.encode(payload, self.secret_key, algorithm="HS512")
        jwt_str = token if isinstance(token, str) else token.decode("utf-8")
        return [f"Authorization: Bearer {jwt_str}"]

    def _on_open(self, ws: Any) -> None:
        sub_payload = [
            {"ticket": f"upbit-private-{uuid.uuid4().hex[:8]}"},
            {"type": "myOrder"},
            {"type": "myAsset"},
            {"format": "DEFAULT"},
        ]
        ws.send(json.dumps(sub_payload))
        logger.info("⚡ [업비트 Private WebSocket 연결 및 myOrder/myAsset 구독 완료]")

    def _on_message(self, ws: Any, message: Any) -> None:
        try:
            raw = json.loads(message.decode("utf-8") if isinstance(message, bytes) else str(message))
            events = raw if isinstance(raw, list) else [raw] if isinstance(raw, dict) else []
            for event in events:
                if not isinstance(event, dict):
                    continue
                event_type = str(event.get("type", "")).lower()
                if event_type == "myorder" and self.on_order:
                    # 업비트 필드 정규화
                    if "identifier" in event and "client_order_id" not in event:
                        event["client_order_id"] = event["identifier"]
                    try:
                        self._order_event_queue.put_nowait(event)
                    except queue.Full:
                        logger.error("Private WebSocket 주문 이벤트 큐 포화: REST 대사 전까지 신규 진입을 차단해야 합니다.")
                elif event_type == "myasset" and self.on_asset:
                    self.on_asset(event)
        except Exception as e:
            logger.debug(f"업비트 Private WebSocket 메시지 파싱 예외: {e}")

    def drain_order_events(self, limit: int = 200) -> int:
        """메인 스레드에서 주문 이벤트를 순차 반영해 파일·손익 갱신 경합을 방지한다."""
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
                logger.warning("Private WebSocket 주문 이벤트 후속 처리 실패: %s", exc)
            drained += 1
        return drained

    def start(self) -> None:
        if self.is_running or not self.access_key or not self.secret_key:
            return
        self.is_running = True

        def run() -> None:
            delay = 2
            while self.is_running:
                try:
                    self.ws = websocket.WebSocketApp(
                        self.ws_url,
                        header=self._headers(),
                        on_open=self._on_open,
                        on_message=self._on_message,
                        on_error=lambda _ws, err: logger.warning(f"업비트 Private WebSocket 오류: {err}"),
                    )
                    self.ws.run_forever(ping_interval=30, ping_timeout=20)
                except Exception as e:
                    logger.warning(f"업비트 Private WebSocket 예외: {e}")

                if self.is_running:
                    time.sleep(delay)
                    delay = min(delay * 2, 30)

        self._thread = threading.Thread(target=run, daemon=True, name="UpbitPrivateWebSocket")
        self._thread.start()
        logger.info("업비트 Private WebSocket 클라이언트 스레드 가동")

    def stop(self) -> None:
        self.is_running = False
        if self.ws:
            self.ws.close()
        logger.info("업비트 Private WebSocket 클라이언트 종료 완료")
