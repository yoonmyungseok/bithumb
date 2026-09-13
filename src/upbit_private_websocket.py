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
import warnings

import jwt
import websocket

try:
    from jwt.warnings import InsecureKeyLengthWarning
    warnings.filterwarnings("ignore", category=InsecureKeyLengthWarning)
except ImportError:
    pass

logger = logging.getLogger(__name__)


from base_websocket import BasePrivateWebSocketClient


class UpbitPrivateWebSocketClient(BasePrivateWebSocketClient):
    """업비트 실시간 Private WebSocket 클라이언트 (myOrder, myAsset)"""

    URL = "wss://api.upbit.com/websocket/v1/private"
    CLIENT_NAME = "업비트 Private WebSocket"

    def __init__(
        self,
        access_key: str = "",
        secret_key: str = "",
        on_order: Callable[[dict[str, Any]], None] | None = None,
        on_asset: Callable[[dict[str, Any]], None] | None = None,
        on_queue_overflow: Callable[[], None] | None = None,
    ):
        eff_access = (access_key or os.getenv("UPBIT_ACCESS_KEY", "")).strip()
        eff_secret = (secret_key or os.getenv("UPBIT_SECRET_KEY", "")).strip()
        configured_url = os.getenv("UPBIT_PRIVATE_WEBSOCKET_URL", self.URL).strip()
        super().__init__(
            access_key=eff_access,
            secret_key=eff_secret,
            on_order=on_order,
            on_asset=on_asset,
            on_queue_overflow=on_queue_overflow,
            ws_url=configured_url,
        )

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
        self._reconnect_delay = 2
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
                        self._handle_order_queue_overflow()
                elif event_type == "myasset" and self.on_asset:
                    self.on_asset(event)
        except Exception as e:
            logger.debug(f"업비트 Private WebSocket 메시지 파싱 예외: {e}")

