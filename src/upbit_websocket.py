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
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

import websocket

logger = logging.getLogger(__name__)


class WebSocketHealthState:
    """웹소켓 데이터 상태 머신 정의 (P1-2)"""
    DATA_AVAILABLE = "DATA_AVAILABLE"
    STALE = "STALE"
    DISCONNECTED = "DISCONNECTED"
    SUBSCRIPTION_FAILED = "SUBSCRIPTION_FAILED"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"


class UpbitWebSocketClient:
    """
    업비트 실시간 Public WebSocket 클라이언트
    """

    WS_URL = "wss://api.upbit.com/websocket/v1"

    def __init__(
        self,
        initial_markets: list[str] | None = None,
        on_price_callback: Callable[[str, float], None] | None = None,
        on_whale_callback: Callable[[str, float, float, str], None] | None = None,
    ):
        self.ws_url = os.getenv("UPBIT_WEBSOCKET_URL", self.WS_URL).strip()
        self.subscribed_markets: list[str] = initial_markets or ["KRW-BTC"]
        self.on_price_callback = on_price_callback
        self.on_whale_callback = on_whale_callback
        self.latest_prices: dict[str, float] = {}
        self.is_running = False
        self.ws: websocket.WebSocketApp | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._whale_trades: list[dict[str, Any]] = []
        self.last_tick_time: float = 0.0
        self.last_tick_time_by_market: dict[str, float] = {}
        self.confirmed_markets: set[str] = set()
        self.subscription_error: bool = False
        self.reconnect_count: int = 0
        self.is_connected: bool = False

    def get_health_status(self, market: str | None = None, max_stale_seconds: float = 15.0) -> dict[str, Any]:
        """업비트 웹소켓 데이터 건강상태 검사 (시장별 개별 상태 판정 지원, P1-2)"""
        now = time.time()
        with self._lock:
            connected = self.is_connected
            reconnects = self.reconnect_count
            sub_count = len(self.subscribed_markets)
            sub_markets = list(self.subscribed_markets)

            if market:
                last_tick = self.last_tick_time_by_market.get(market.upper(), 0.0)
                is_sub = market.upper() in [m.upper() for m in sub_markets]
            else:
                last_tick = self.last_tick_time
                is_sub = True

        latency = (now - last_tick) if last_tick > 0 else 9999.0

        if not connected:
            state = WebSocketHealthState.DISCONNECTED
            is_healthy = False
        elif market and not is_sub:
            state = WebSocketHealthState.SUBSCRIPTION_FAILED
            is_healthy = False
        elif last_tick <= 0:
            state = WebSocketHealthState.DATA_UNAVAILABLE
            is_healthy = False
        elif latency > max_stale_seconds:
            state = WebSocketHealthState.STALE
            is_healthy = False
        else:
            state = WebSocketHealthState.DATA_AVAILABLE
            is_healthy = True

        return {
            "status": state,
            "is_healthy": is_healthy,
            "latency_seconds": round(latency, 2),
            "last_tick_time": last_tick,
            "market": market,
            "reconnect_count": reconnects,
            "subscribed_count": sub_count,
        }

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
        """감시 대상 마켓 목록 동적 갱신 및 재구독 (HOLO는 자동 배제)"""
        raw_excluded = os.getenv("UPBIT_EXCLUDED_MARKETS", "KRW-HOLO,HOLO")
        excluded_set = {x.strip().upper() for x in raw_excluded.split(",") if x.strip()}
        excluded_set.update({"KRW-HOLO", "HOLO"})

        clean_markets = [
            m.strip().upper()
            for m in markets
            if m.strip() and m.strip().upper() not in excluded_set and m.strip().upper().replace("KRW-", "") not in excluded_set
        ]
        clean_markets = list(dict.fromkeys(clean_markets))
        if not clean_markets:
            return

        with self._lock:
            if set(self.subscribed_markets) == set(clean_markets):
                return
            self.subscribed_markets = clean_markets

        logger.info(f"⚡ [업비트 웹소켓 구독 갱신] 총 {len(clean_markets)}개 마켓: {clean_markets}")
        self._send_subscription()

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

                    if self.on_price_callback:
                        self.on_price_callback(code, price)

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

                    if self.on_whale_callback:
                        self.on_whale_callback(code, price, val_krw, side)

        except Exception:
            pass

    def _on_open(self, ws: Any):
        with self._lock:
            self.is_connected = True
        logger.info("⚡ [업비트 실시간 Public WebSocket 연결 완료]")
        self._send_subscription()

    def _on_error(self, ws: Any, error: Any):
        with self._lock:
            self.is_connected = False
        logger.warning(f"업비트 웹소켓 오류: {error}")

    def _on_close(self, ws: Any, close_status_code: Any, close_msg: Any):
        with self._lock:
            self.is_connected = False
        logger.info(f"업비트 웹소켓 연결 종료 (코드: {close_status_code})")

    def _run_loop(self):
        retry_delay = 2
        while self.is_running:
            try:
                with self._lock:
                    self.reconnect_count += 1
                self.ws = websocket.WebSocketApp(
                    self.ws_url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self.ws.run_forever(
                    ping_interval=30,
                    ping_timeout=20,
                )
            except Exception as e:
                logger.warning(f"업비트 웹소켓 루프 예외: {e}")

            if self.is_running:
                logger.info(f"업비트 웹소켓 {retry_delay}초 후 재연결 시도...")
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)
            else:
                break

    def start(self):
        """웹소켓 백그라운드 스레드 가동"""
        if self.is_running:
            return
        self.is_running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="UpbitWebSocket")
        self._thread.start()
        logger.info("업비트 Public WebSocket 클라이언트 스레드 가동")

    def stop(self):
        """웹소켓 안전 종료"""
        self.is_running = False
        with self._lock:
            self.is_connected = False
        if self.ws:
            self.ws.close()
        logger.info("업비트 Public WebSocket 클라이언트 종료 완료")
