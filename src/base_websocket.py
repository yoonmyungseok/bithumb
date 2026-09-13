"""공통 웹소켓 베이스 모듈 (빗썸/업비트 Public 및 Private 웹소켓 공통화)

- BasePublicWebSocketClient: 시세/고래 체결 콜백 큐, 상태 머신, 지수 백오프 자동 재연결
- BasePrivateWebSocketClient: myOrder/myAsset 이벤트 큐, 오버플로우 핸들링, 순차 드레인
"""

from __future__ import annotations

import logging
import queue
import threading
import time
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
    PROCESSING_DELAY = "PROCESSING_DELAY"


class BasePublicWebSocketClient:
    """빗썸 및 업비트 Public 웹소켓 공통 기본 클래스.

    - 상태 머신 및 헬스 체크 SSOT
    - 콜백 큐 (최대 2000) 및 메인 스레드 직렬 드레인
    - 지수 백오프(2s -> 60s) 자동 재연결 루프
    """

    WS_URL: str = ""
    CLIENT_NAME: str = "BaseWebSocket"

    def __init__(
        self,
        initial_markets: list[str] | None = None,
        on_price_callback: Callable[[str, float], None] | None = None,
        on_whale_callback: Callable[[str, float, float, str], None] | None = None,
        ws_url: str | None = None,
    ):
        self.ws_url = (ws_url or self.WS_URL).strip()
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
        # 수신 스레드와 후속 주문 판단 스레드를 분리해 ping/pong 처리를 방어한다.
        self._callback_queue: queue.Queue[tuple[str, tuple[Any, ...], float]] = queue.Queue(maxsize=2000)
        self._callback_dropped_count = 0
        self._pending_price_markets: set[str] = set()
        self._callback_processed_count = 0
        self._callback_total_execution_seconds = 0.0
        self._callback_max_execution_seconds = 0.0
        self._last_callback_delay_seconds = 0.0
        self._max_callback_delay_seconds = 0.0
        self._last_callback_prices: dict[str, float] = {}
        self.last_tick_time: float = 0.0
        self.last_tick_time_by_market: dict[str, float] = {}
        self.market_subscription_time: dict[str, float] = {}
        self.confirmed_markets: set[str] = set()
        self.subscription_error: bool = False
        self.reconnect_count: int = 0
        self.is_connected: bool = False

    def get_health_status(self, market: str | None = None, max_stale_seconds: float = 45.0) -> dict[str, Any]:
        """웹소켓 데이터 건강상태 검사 (시장별 개별 상태 판정 지원, P1-2)"""
        now = time.time()
        effective_stale_sec = 15.0 if market and "BTC" in market.upper() else max_stale_seconds

        with self._lock:
            connected = self.is_connected
            reconnects = self.reconnect_count
            sub_count = len(self.subscribed_markets)
            sub_markets = list(self.subscribed_markets)

            if market:
                market_upper = market.upper()
                last_tick = self.last_tick_time_by_market.get(market_upper, 0.0)
                is_sub = market_upper in [m.upper() for m in sub_markets]
                sub_time = self.market_subscription_time.get(market_upper, 0.0)
            else:
                last_tick = self.last_tick_time
                is_sub = True
                sub_time = 0.0

        latency = (now - last_tick) if last_tick > 0 else 9999.0
        time_since_sub = (now - sub_time) if sub_time > 0 else 9999.0

        queue_depth = self._callback_queue.qsize()
        with self._lock:
            callback_delay = self._last_callback_delay_seconds
            callback_drops = self._callback_dropped_count
            callback_processed = self._callback_processed_count
            callback_total_execution = self._callback_total_execution_seconds
            callback_max_execution = self._callback_max_execution_seconds

        if not connected:
            state = WebSocketHealthState.DISCONNECTED
            is_healthy = False
        elif market and not is_sub:
            state = WebSocketHealthState.SUBSCRIPTION_FAILED
            is_healthy = False
        elif last_tick <= 0:
            # 신규 구독 후 60초 이내 유예(Grace Period) 적용: 아직 틱 미수신이어도 웹소켓 정상 연결 시 유효 간주
            if market and is_sub and time_since_sub <= 60.0:
                state = WebSocketHealthState.DATA_AVAILABLE
                is_healthy = True
            else:
                state = WebSocketHealthState.DATA_UNAVAILABLE
                is_healthy = False
        elif latency > effective_stale_sec:
            state = WebSocketHealthState.STALE
            is_healthy = False
        elif queue_depth > 100 or (callback_delay > 2.0 and queue_depth > 20):
            state = WebSocketHealthState.PROCESSING_DELAY
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
            "callback_queue_depth": queue_depth,
            "callback_delay_seconds": round(callback_delay, 3),
            "callback_dropped_count": callback_drops,
            "callback_processed_count": callback_processed,
            "callback_avg_execution_seconds": round(callback_total_execution / callback_processed, 4) if callback_processed else 0.0,
            "callback_max_execution_seconds": round(callback_max_execution, 4),
        }

    def get_latest_price(self, market: str) -> float:
        """실시간 캐시된 최신 체결가 반환 (없으면 0.0)"""
        with self._lock:
            return self.latest_prices.get(market, 0.0)

    def get_whale_flow_summary(self, market: str, window_seconds: int = 300) -> str:
        """최근 window_seconds 동안의 고래 대량 체결 수급 집계"""
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

        now_ts = time.time()
        with self._lock:
            for m in clean_markets:
                m_upper = m.upper()
                if m_upper not in self.market_subscription_time:
                    self.market_subscription_time[m_upper] = now_ts

            if set(self.subscribed_markets) == set(clean_markets):
                return
            self.subscribed_markets = clean_markets

        logger.info(f"⚡ [{self.CLIENT_NAME} 구독 갱신] 총 {len(clean_markets)}개 마켓: {clean_markets}")
        self._send_subscription()

    def drain_callbacks(self, limit: int = 200) -> int:
        """메인 스레드에서 실시간 후속 작업을 직렬 실행한다."""
        drained = 0
        while drained < limit:
            try:
                kind, args, enqueued_at = self._callback_queue.get_nowait()
            except queue.Empty:
                break
            try:
                callback_started_at = time.monotonic()
                if kind == "price" and self.on_price_callback:
                    market, queued_price = args
                    with self._lock:
                        current_price = self.latest_prices.get(market, queued_price)
                        self._pending_price_markets.discard(market)
                    self.on_price_callback(market, current_price)
                elif kind == "whale" and self.on_whale_callback:
                    self.on_whale_callback(*args)
            except Exception as exc:
                logger.warning(f"{self.CLIENT_NAME} 후속 콜백 처리 실패: {exc}")
            finally:
                completed_at = time.monotonic()
                delay = max(0.0, completed_at - enqueued_at)
                execution_seconds = max(0.0, completed_at - callback_started_at)
                with self._lock:
                    self._last_callback_delay_seconds = delay
                    self._max_callback_delay_seconds = max(self._max_callback_delay_seconds, delay)
                    self._callback_processed_count += 1
                    self._callback_total_execution_seconds += execution_seconds
                    self._callback_max_execution_seconds = max(self._callback_max_execution_seconds, execution_seconds)
            drained += 1

        if self._callback_queue.empty():
            with self._lock:
                self._last_callback_delay_seconds = 0.0

        return drained

    def _enqueue_callback(self, kind: str, args: tuple[Any, ...]) -> None:
        """큐 포화 시 오래된 이벤트를 버려 연결 유지와 최신 가격 처리를 우선한다."""
        if kind == "price" and args:
            market = str(args[0])
            with self._lock:
                if market in self._pending_price_markets:
                    return
                self._pending_price_markets.add(market)
        try:
            self._callback_queue.put_nowait((kind, args, time.monotonic()))
        except queue.Full:
            try:
                dropped_item = self._callback_queue.get_nowait()
                dropped_kind = dropped_item[0]
                dropped_args = dropped_item[1]
                if dropped_kind == "price" and dropped_args:
                    with self._lock:
                        self._pending_price_markets.discard(str(dropped_args[0]))
                self._callback_queue.put_nowait((kind, args, time.monotonic()))
                with self._lock:
                    self._callback_dropped_count += 1
            except queue.Empty:
                if kind == "price" and args:
                    with self._lock:
                        self._pending_price_markets.discard(str(args[0]))

    def _send_subscription(self):
        """거래소별 구독 페이로드 전송 (하위 클래스 구현)"""
        raise NotImplementedError

    def _on_message(self, ws: Any, message: Any):
        """거래소별 메시지 파싱 (하위 클래스 구현)"""
        raise NotImplementedError

    def _on_open(self, ws: Any):
        with self._lock:
            self.is_connected = True
        logger.info(f"⚡ [{self.CLIENT_NAME} 연결 성공] 실시간 시세 스트리밍 활성화")
        self._send_subscription()

    def _on_error(self, ws: Any, error: Any):
        with self._lock:
            self.is_connected = False
        err_str = str(error)
        if "10054" in err_str or "ConnectionReset" in err_str:
            logger.debug(f"{self.CLIENT_NAME} 세션 만료 감지 (자동 재연결 대기): {error}")
        else:
            logger.warning(f"{self.CLIENT_NAME} 에러 발생: {error}")

    def _on_close(self, ws: Any, close_status_code: Any, close_msg: Any):
        with self._lock:
            self.is_connected = False
        logger.info(f"{self.CLIENT_NAME} 연결 종료, 재연결을 대기합니다. (코드: {close_status_code})")

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
                logger.warning(f"{self.CLIENT_NAME} 루프 예외: {e}")

            if self.is_running:
                logger.info(f"{self.CLIENT_NAME} {retry_delay}초 후 재연결 시도...")
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)
            else:
                break

    def start(self):
        """백그라운드 스레드에서 웹소켓 클라이언트 가동"""
        if self.is_running:
            return
        self.is_running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name=self.CLIENT_NAME)
        self._thread.start()
        logger.info(f"👀 {self.CLIENT_NAME} 감시 엔진 시작 완료")

    def stop(self):
        """웹소켓 안전 종료"""
        self.is_running = False
        with self._lock:
            self.is_connected = False
        if self.ws:
            self.ws.close()
        logger.info(f"{self.CLIENT_NAME} 종료 완료")


class BasePrivateWebSocketClient:
    """빗썸 및 업비트 Private 웹소켓 공통 기본 클래스.

    - myOrder/myAsset 이벤트 큐 (최대 1000)
    - 큐 오버플로우 안전 차단 훅
    - 순차 이벤트 드레인 (drain_order_events)
    - 지수 백오프 자동 재연결
    """

    URL: str = ""
    CLIENT_NAME: str = "BasePrivateWebSocket"

    def __init__(
        self,
        access_key: str = "",
        secret_key: str = "",
        on_order: Callable[[dict[str, Any]], None] | None = None,
        on_asset: Callable[[dict[str, Any]], None] | None = None,
        on_queue_overflow: Callable[[], None] | None = None,
        ws_url: str | None = None,
    ):
        self.access_key = access_key.strip()
        self.secret_key = secret_key.strip()
        self.ws_url = (ws_url or self.URL).strip()
        self.on_order = on_order
        self.on_asset = on_asset
        self.on_queue_overflow = on_queue_overflow
        self.is_running = False
        self.ws: websocket.WebSocketApp | None = None
        self._thread: threading.Thread | None = None
        self._order_event_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1000)
        self._reconnect_delay = 2

    def pending_order_event_count(self) -> int:
        """메인 스레드가 아직 처리하지 않은 주문 이벤트 수."""
        return self._order_event_queue.qsize()

    def _handle_order_queue_overflow(self) -> None:
        logger.warning(
            f"{self.CLIENT_NAME} 주문 이벤트 큐 포화(대기 {self.pending_order_event_count()}건): REST 대사 전까지 신규 진입을 차단합니다.",
        )
        if self.on_queue_overflow:
            try:
                self.on_queue_overflow()
            except Exception as exc:
                logger.warning(f"{self.CLIENT_NAME} 큐 포화 콜백 실패: {exc}")

    def _headers(self) -> list[str]:
        raise NotImplementedError

    def _on_open(self, ws: Any) -> None:
        raise NotImplementedError

    def _on_message(self, ws: Any, message: Any) -> None:
        raise NotImplementedError

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
                logger.warning(f"{self.CLIENT_NAME} 주문 이벤트 후속 처리 실패: {exc}")
            drained += 1
        return drained

    def start(self) -> None:
        if self.is_running or not self.access_key or not self.secret_key:
            return
        self.is_running = True
        self._reconnect_delay = 2

        def run() -> None:
            while self.is_running:
                try:
                    self.ws = websocket.WebSocketApp(
                        self.ws_url,
                        header=self._headers(),
                        on_open=self._on_open,
                        on_message=self._on_message,
                        on_error=lambda _ws, err: logger.warning(f"{self.CLIENT_NAME} 오류: {err}"),
                        on_close=lambda _ws, code, msg: logger.info(f"{self.CLIENT_NAME} 연결 종료 (code: {code})"),
                    )
                    self.ws.run_forever(ping_interval=30, ping_timeout=20)
                except Exception as exc:
                    logger.warning(f"{self.CLIENT_NAME} 루프 예외: {exc}")
                if not self.is_running:
                    break
                time.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 60)

        self._thread = threading.Thread(target=run, daemon=True, name=self.CLIENT_NAME)
        self._thread.start()
        logger.info(f"{self.CLIENT_NAME} 스레드 가동 완료")

    def stop(self) -> None:
        self.is_running = False
        if self.ws:
            self.ws.close()
        logger.info(f"{self.CLIENT_NAME} 종료 완료")
