import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

import websocket

logger = logging.getLogger(__name__)


class BithumbWebSocketClient:
    """
    빗썸 2.0 공식 실시간 웹소켓(WebSocket) 스트리밍 클라이언트
    - wss://ws-api.bithumb.com/websocket/v1 상시 연결
    - 0.1초 단위 실시간 체결가(ticker) 및 대량 체결(whale transaction) 스트리밍
    - 네트워크 단절 시 자동 재연결(Auto-Reconnect) 및 재구독 지원
    - 보유 코인의 실시간 트레일링 스탑 / 긴급 손절 즉시 감시
    - 3,000만 원 이상 고래 대량 시장가 매수 실시간 포착
    """

    WS_URL = "wss://ws-api.bithumb.com/websocket/v1"

    def __init__(
        self,
        initial_markets: list[str] | None = None,
        on_price_callback: Callable[[str, float], None] | None = None,
        on_whale_callback: Callable[[str, float, float, str], None] | None = None,
    ):
        self.subscribed_markets: list[str] = initial_markets or ["KRW-BTC"]
        self.on_price_callback = on_price_callback
        self.on_whale_callback = on_whale_callback
        self.latest_prices: dict[str, float] = {}
        self.is_running = False
        self.ws: websocket.WebSocketApp | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_whale_time: dict[str, float] = {}
        self._whale_trades: list[dict[str, Any]] = []

    def get_latest_price(self, market: str) -> float:
        """실시간 캐시된 최신 체결가 반환 (없으면 0.0)"""
        with self._lock:
            return self.latest_prices.get(market, 0.0)

    def get_whale_flow_summary(self, market: str, window_seconds: int = 300) -> str:
        """
        최근 window_seconds(기본 5분) 동안의 고래 대량 체결 수급 집계
        """
        now_ts = time.time()
        cutoff_ts = now_ts - window_seconds
        
        with self._lock:
            # 10분 이전 과거 데이터 정리
            self._whale_trades = [t for t in self._whale_trades if t["ts"] >= (now_ts - 600)]
            recent_trades = [t for t in self._whale_trades if t["market"] == market and t["ts"] >= cutoff_ts]

        if not recent_trades:
            return "최근 5분간 3,000만 원 이상 고래 대량 체결 없음 (수급 평온)"

        buy_krw = sum(t["val_krw"] for t in recent_trades if t["side"] == "매수")
        sell_krw = sum(t["val_krw"] for t in recent_trades if t["side"] == "매도")
        net_krw = buy_krw - sell_krw

        buy_100m = buy_krw / 100_000_000.0
        sell_100m = sell_krw / 100_000_000.0
        net_100m = net_krw / 100_000_000.0

        if net_krw > 0:
            return f"🟢 최근 5분 고래 순매수 우위 (+{net_100m:.2f}억 원 | 매수: {buy_100m:.2f}억, 매도: {sell_100m:.2f}억)"
        elif net_krw < 0:
            return f"🔴 최근 5분 고래 순매도 우위 ({net_100m:.2f}억 원 | 매도: {sell_100m:.2f}억, 매수: {buy_100m:.2f}억)"
        else:
            return f"⚪ 최근 5분 고래 매수/매도 균형 (총 {buy_100m + sell_100m:.2f}억 원)"

    def update_subscriptions(self, markets: list[str]):
        """감시 대상 마켓 목록 동적 갱신 및 재구독"""
        clean_markets = list(dict.fromkeys([m.strip() for m in markets if m.strip()]))
        if not clean_markets:
            return

        with self._lock:
            if set(self.subscribed_markets) == set(clean_markets):
                return
            self.subscribed_markets = clean_markets

        logger.info(f"⚡ [웹소켓 구독 갱신] 총 {len(clean_markets)}개 마켓: {clean_markets}")
        self._send_subscription()

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

            # 1. 실시간 시세 (Ticker) 수신
            if msg_type == "ticker":
                price = float(data.get("trade_price", 0.0))
                if code and price > 0:
                    with self._lock:
                        self.latest_prices[code] = price

                    if self.on_price_callback:
                        self.on_price_callback(code, price)

            # 2. 실시간 체결 (Trade) 수신 ➜ 고래 체결 탐지
            elif msg_type == "trade":
                price = float(data.get("trade_price", 0.0))
                qty = float(data.get("trade_volume", 0.0))
                val_krw = price * qty
                ask_bid = data.get("ask_bid", "BID")
                side = "매수" if str(ask_bid).upper() in ("BID", "BUY", "1") else "매도"

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
                            self.on_whale_callback(code, price, val_krw, side)

        except (json.JSONDecodeError, ValueError, KeyError):
            pass

    def _on_open(self, ws: Any):
        logger.info("⚡ [빗썸 웹소켓 연결 성공] 0.1초 실시간 시세 & 고래 체결 스트리밍 활성화")
        self._send_subscription()

    def _on_error(self, ws: Any, error: Any):
        logger.warning(f"웹소켓 에러 발생: {error}")

    def _on_close(self, ws: Any, close_status_code: Any, close_msg: Any):
        logger.info("웹소켓 연결 종료, 재연결을 대기합니다.")

    def start(self):
        """백그라운드 스레드에서 웹소켓 클라이언트 가동"""
        if self.is_running:
            return

        self.is_running = True

        def _run_loop():
            while self.is_running:
                try:
                    self.ws = websocket.WebSocketApp(
                        self.WS_URL,
                        on_open=self._on_open,
                        on_message=self._on_message,
                        on_error=self._on_error,
                        on_close=self._on_close,
                    )
                    self.ws.run_forever(ping_interval=30, ping_timeout=10)
                except (websocket.WebSocketException, OSError) as e:
                    logger.warning(f"웹소켓 루프 예외: {e}")
                time.sleep(3)

        self._thread = threading.Thread(target=_run_loop, daemon=True, name="BithumbWebSocket")
        self._thread.start()
        logger.info("👀 빗썸 실시간 웹소켓(WebSocket) 감시 엔진 시작 완료")

    def stop(self):
        self.is_running = False
        if self.ws:
            self.ws.close()
