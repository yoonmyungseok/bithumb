"""
업비트(Upbit) Open API v1 통신 클라이언트
- 공식 JWT 인증 (UUID 기반 nonce, HS512 알고리즘, 비인코딩 query string의 SHA-512 query_hash)
- 계좌 잔고, 마켓 목록, Ticker, 캔들, 호가창(Orderbook), 주문가능 정보 조회
- 지정가/시장가 매수/매도 주문 생성, 주문 취소, 단건/목록 주문 조회
- identifier 고유 식별자 지원으로 네트워크 장애 시 중복 주문 방지 및 멱등성 보장
- KRW-HOLO(홀로월드에이아이) 수동 매매 보호 종목 자동 주문 원천 차단
- HTTP Keep-Alive 커넥션 풀링 및 Rate Limit (초당 호출 한도) 백오프 핸들링
"""

import hashlib
import logging
import math
import os
import threading
import time
import urllib.parse
import uuid
import warnings
from typing import Any
from requests.adapters import HTTPAdapter

import jwt
import requests

from market_policy import get_excluded_markets

try:
    from jwt.warnings import InsecureKeyLengthWarning
    warnings.filterwarnings("ignore", category=InsecureKeyLengthWarning)
except ImportError:
    pass

logger = logging.getLogger(__name__)

# 수동 매매 전용 관리 제외 종목 (기본값: KRW-HOLO, HOLO)
DEFAULT_UPBIT_EXCLUDED: set[str] = {"KRW-HOLO", "HOLO"}


def get_upbit_excluded_markets() -> set[str]:
    """업비트 자동 매매에서 엄격히 격리할 종목 목록 반환 (HOLO 필수 포함)"""
    return get_excluded_markets()


class UpbitAPI:
    """
    업비트 REST API 클라이언트 (v1)
    """

    API_ROOT = "https://api.upbit.com/v1"

    def __init__(self, access_key: str = "", secret_key: str = ""):
        self.access_key = (access_key or os.getenv("UPBIT_ACCESS_KEY", "")).strip()
        self.secret_key = (secret_key or os.getenv("UPBIT_SECRET_KEY", "")).strip()
        self.api_root = os.getenv("UPBIT_API_BASE_URL", self.API_ROOT).strip().rstrip("/")
        
        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=1)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self._market_name_map: dict[str, str] = {}
        self._lock = threading.RLock()
        # 업비트 응답 헤더의 Rate Limit 그룹별 예약 시각과 차단 시각을 관리한다.
        self._rate_limit_next_at: dict[str, float] = {}
        self._rate_limit_blocked_until: dict[str, float] = {}
        self._rate_limit_remaining: dict[str, int] = {}
        # 시장 스캔 결과를 재사용해 분석·대시보드의 중복 /ticker 요청을 줄인다.
        self._ticker_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._ticker_cache_ttl = 1.5

    @staticmethod
    def _get_rate_limit_group(method: str, endpoint: str) -> str:
        """요청 전에도 안전한 간격을 적용할 수 있도록 API 그룹을 판별한다."""
        if endpoint == "/ticker":
            return "ticker"
        if endpoint == "/orderbook":
            return "orderbook"
        if endpoint == "/market/all":
            return "market"
        if endpoint.startswith("/candles/"):
            return "candle"
        if endpoint == "/trades/ticks":
            return "trade"
        if method.upper() == "POST" and endpoint == "/orders":
            return "order"
        return "default"

    @staticmethod
    def _get_rate_limit_interval(group: str) -> float:
        """공식 초당 한도보다 여유를 둔 그룹별 최소 요청 간격을 반환한다."""
        # 시세 그룹은 초당 10회보다 낮은 약 7.7회, 주문은 초당 10회로 제한한다.
        if group in {"market", "candle", "trade", "ticker", "orderbook"}:
            return 0.13
        if group == "order":
            return 0.10
        # 거래·자산 기본 그룹의 공식 한도(초당 30회)보다 여유를 둔다.
        return 0.04

    @staticmethod
    def _seconds_until_next_boundary() -> float:
        """429 또는 sec=0 이후 다음 초 경계까지의 안전 대기 시간을 계산한다."""
        return max(0.02, 1.02 - (time.time() % 1.0))

    def _throttle(self, group: str) -> None:
        """그룹별 요청 예약으로 동시 호출에도 초당 한도를 선제적으로 지킨다."""
        with self._lock:
            now = time.monotonic()
            reserved_at = max(
                now,
                self._rate_limit_next_at.get(group, now),
                self._rate_limit_blocked_until.get(group, now),
            )
            self._rate_limit_next_at[group] = reserved_at + self._get_rate_limit_interval(group)

        wait_sec = reserved_at - time.monotonic()
        if wait_sec > 0:
            time.sleep(wait_sec)

    def _update_rate_limit_from_response(self, response: requests.Response, fallback_group: str) -> tuple[str, int | None]:
        """Remaining-Req 헤더를 반영하고, 한도 소진 그룹은 다음 초까지 차단한다."""
        # requests의 CaseInsensitiveDict와 테스트용 dict 모두 get()을 지원한다.
        header = response.headers.get("Remaining-Req", "")
        if not isinstance(header, str):
            return fallback_group, None

        parts: dict[str, str] = {}
        for item in header.split(";"):
            key, separator, value = item.strip().partition("=")
            if separator:
                parts[key.strip()] = value.strip()

        group = parts.get("group", fallback_group)
        try:
            remaining_sec = int(parts["sec"])
        except (KeyError, TypeError, ValueError):
            return group, None

        with self._lock:
            self._rate_limit_remaining[group] = remaining_sec
            if remaining_sec <= 0:
                blocked_until = time.monotonic() + self._seconds_until_next_boundary()
                self._rate_limit_blocked_until[group] = max(
                    self._rate_limit_blocked_until.get(group, 0.0), blocked_until
                )
        return group, remaining_sec

    def _block_rate_limit_group(self, group: str) -> float:
        """429를 받은 그룹만 다음 초 경계까지 차단하고 실제 대기 시간을 반환한다."""
        wait_sec = self._seconds_until_next_boundary()
        with self._lock:
            blocked_until = time.monotonic() + wait_sec
            self._rate_limit_blocked_until[group] = max(
                self._rate_limit_blocked_until.get(group, 0.0), blocked_until
            )
        return wait_sec

    def _generate_jwt_token(self, params: dict[str, Any] | None = None) -> str:
        """
        업비트 공식 규격에 맞는 JWT 토큰 생성
        - 알고리즘: HS512
        - 파라미터가 있는 경우: unquote(urlencode(params, doseq=True))의 SHA-512 해시 생성
        """
        payload: dict[str, Any] = {
            "access_key": self.access_key,
            "nonce": str(uuid.uuid4()),
        }

        if params:
            # 업비트 공식: urlencode 후 unquote한 비인코딩 쿼리 스트링의 SHA-512 해시
            query_string = urllib.parse.unquote(urllib.parse.urlencode(params, doseq=True)).encode("utf-8")
            sha512 = hashlib.sha512()
            sha512.update(query_string)
            query_hash = sha512.hexdigest()

            payload["query_hash"] = query_hash
            payload["query_hash_alg"] = "SHA512"

        token = jwt.encode(payload, self.secret_key, algorithm="HS512")
        return token if isinstance(token, str) else token.decode("utf-8")

    def _request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        max_retries: int = 3,
    ) -> Any:
        """
        업비트 API 요청 공통 핸들러
        - 조회(GET) 요청만 네트워크/일시 오류 시 자동 재시도
        - 주문(POST) 요청은 중복 주문 방지를 위해 즉시 예외 전달 (호출자가 멱등성 저널을 통해 처리)
        """
        url = f"{self.api_root}{endpoint}"
        last_exception = None

        method_upper = method.upper()
        retryable = method_upper == "GET"
        attempts = max_retries if retryable else 1
        request_group = self._get_rate_limit_group(method_upper, endpoint)

        for attempt in range(1, attempts + 1):
            self._throttle(request_group)
            headers = {"Content-Type": "application/json"}
            if self.access_key and self.secret_key:
                query_payload = params if params is not None else data
                jwt_token = self._generate_jwt_token(query_payload)
                headers["Authorization"] = f"Bearer {jwt_token}"

            try:
                if method_upper == "GET":
                    response = self.session.get(url, headers=headers, params=params, timeout=10)
                elif method_upper == "POST":
                    response = self.session.post(url, headers=headers, json=data, timeout=10)
                elif method_upper == "DELETE":
                    response = self.session.delete(url, headers=headers, params=params, timeout=10)
                else:
                    raise ValueError(f"지원하지 않는 HTTP 메서드: {method}")

                response_group, remaining_sec = self._update_rate_limit_from_response(
                    response, request_group
                )

                # Rate Limit (429) 또는 서버 일시 장애 (5xx) 시 지수 백오프 재시도
                if response.status_code in (429, 500, 502, 503, 504):
                    if retryable and attempt < attempts:
                        if response.status_code == 429:
                            # 429는 지수 백오프보다 다음 초 경계 대기가 우선이다.
                            sleep_sec = self._block_rate_limit_group(response_group)
                            remaining_text = "미확인" if remaining_sec is None else str(remaining_sec)
                            logger.warning(
                                f"⚠️ Upbit API [429] {endpoint} 요청 제한. "
                                f"그룹={response_group}, 잔여초당요청={remaining_text}, "
                                f"{sleep_sec:.2f}초 후 재시도 ({attempt}/{max_retries})"
                            )
                        else:
                            sleep_sec = 2 ** (attempt - 1)
                            logger.warning(
                                f"⚠️ Upbit API [{response.status_code}] {endpoint} 일시 오류. "
                                f"{sleep_sec}초 후 재시도 ({attempt}/{max_retries})"
                            )
                        time.sleep(sleep_sec)
                        continue

                if response.status_code not in (200, 201):
                    logger.error(
                        f"Upbit API Error [{response.status_code}] {endpoint}: {response.text}"
                    )
                    response.raise_for_status()

                return response.json()

            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                last_exception = e
                if retryable and attempt < attempts:
                    sleep_sec = 2 ** (attempt - 1)
                    logger.warning(
                        f"⚠️ Upbit API 네트워크 타임아웃/연결 오류: {e}. {sleep_sec}초 후 재시도 ({attempt}/{max_retries})"
                    )
                    time.sleep(sleep_sec)
                else:
                    logger.error(f"HTTP Request Failed after {attempts} attempts: {e}")
                    raise
            except requests.exceptions.RequestException as e:
                logger.error(f"HTTP Request Failed: {e}")
                raise

        if last_exception:
            raise last_exception

    def get_balances(self) -> dict[str, dict[str, float]]:
        """
        계좌 전체 잔고 조회
        반환 형식: {'KRW': {'balance': float, 'locked': float, 'avg_buy_price': float}, ...}
        """
        data = self._request("GET", "/accounts")
        balances: dict[str, dict[str, float]] = {}
        for item in data:
            currency = item.get("currency")
            if not currency:
                continue
            balances[currency] = {
                "balance": float(item.get("balance", 0.0)),
                "locked": float(item.get("locked", 0.0)),
                "avg_buy_price": float(item.get("avg_buy_price", 0.0)),
            }
        return balances

    def get_currency_balance(self, currency: str) -> dict[str, float]:
        """특정 통화/코인의 잔고 정보 반환"""
        balances = self.get_balances()
        return balances.get(
            currency.upper(), {"balance": 0.0, "locked": 0.0, "avg_buy_price": 0.0}
        )

    def get_all_markets(self, is_details: bool = False) -> list[dict[str, Any]]:
        """
        업비트에서 거래 지원하는 전체 마켓 목록 조회 (1시간 메모리 캐싱으로 429 Rate Limit 방어)
        """
        now = time.time()
        if not hasattr(self, "_all_markets_cache"):
            self._all_markets_cache = {}
        cached_entry = self._all_markets_cache.get(is_details)
        if cached_entry:
            cached_time, cached_data = cached_entry
            if now - cached_time < 3600.0 and cached_data:
                return cached_data

        params = {"isDetails": "true" if is_details else "false"}
        try:
            data = self._request("GET", "/market/all", params=params)
            if isinstance(data, list) and data:
                self._all_markets_cache[is_details] = (now, data)
                return data
        except Exception as e:
            logger.warning(f"업비트 전체 마켓 조회 실패: {e}")
            if cached_entry:
                return cached_entry[1]
        return []

    def _get_valid_markets_set(self) -> set[str]:
        """업비트에서 거래 지원하는 유효 마켓 코드 세트 캐싱 반환"""
        if not hasattr(self, "_valid_markets_cache") or not self._valid_markets_cache:
            try:
                all_m = self.get_all_markets()
                self._valid_markets_cache = {m["market"] for m in all_m if "market" in m}
            except Exception:
                self._valid_markets_cache = set()
        return self._valid_markets_cache

    def get_korean_name(self, market: str) -> str:
        """
        마켓 코드(예: KRW-BTC)에 대응하는 한글 종목명(예: 비트코인) 반환
        """
        if not self._market_name_map:
            try:
                all_m = self.get_all_markets()
                self._market_name_map = {m["market"]: m.get("korean_name", "") for m in all_m if "market" in m}
            except Exception:
                self._market_name_map = {}
        return self._market_name_map.get(market, market.split("-")[-1])

    def get_tickers(self, markets: list[str], force_refresh: bool = False) -> list[dict[str, Any]]:
        """
        여러 마켓의 Ticker 시세 일괄 조회 (존재하지 않는 마켓 404 방지)
        - 기본값은 1.5초 캐시를 사용해 시장 스캔 직후의 중복 호출을 방지한다.
        - 주문 직전처럼 최신 시세가 필요한 호출자는 force_refresh=True를 사용할 수 있다.
        """
        if not markets:
            return []
        valid_set = self._get_valid_markets_set()
        filtered = [m for m in markets if not valid_set or m in valid_set]
        if not filtered:
            return []

        now = time.monotonic()
        cached_by_market: dict[str, dict[str, Any]] = {}
        if not force_refresh:
            with self._lock:
                for market in filtered:
                    cached_entry = self._ticker_cache.get(market)
                    if cached_entry and now - cached_entry[0] < self._ticker_cache_ttl:
                        cached_by_market[market] = dict(cached_entry[1])

        missing_markets = [market for market in filtered if market not in cached_by_market]
        if not missing_markets:
            return [cached_by_market[market] for market in filtered if market in cached_by_market]

        markets_str = ",".join(missing_markets)
        params = {"markets": markets_str}
        try:
            data = self._request("GET", "/ticker", params=params)
            tickers = data if isinstance(data, list) else []
            fetched_by_market = {
                ticker["market"]: dict(ticker)
                for ticker in tickers
                if isinstance(ticker, dict) and isinstance(ticker.get("market"), str)
            }
            if fetched_by_market:
                cached_at = time.monotonic()
                with self._lock:
                    for market, ticker in fetched_by_market.items():
                        self._ticker_cache[market] = (cached_at, ticker)
            merged = {**cached_by_market, **fetched_by_market}
            return [merged[market] for market in filtered if market in merged]
        except Exception as e:
            logger.debug(f"Ticker 조회 예외: {e}")
            # 이미 확보한 짧은 캐시만 반환해 장애 시 불필요한 빈 시세 전파를 줄인다.
            return [cached_by_market[market] for market in filtered if market in cached_by_market]

    def get_ticker(self, market: str = "KRW-BTC", force_refresh: bool = False) -> dict[str, Any]:
        """단일 코인 시세 조회"""
        valid_set = self._get_valid_markets_set()
        if valid_set and market not in valid_set:
            return {}
        res = self.get_tickers([market], force_refresh=force_refresh)
        return res[0] if res else {}

    def get_candles(self, unit: int = 5, count: int = 30, market: str = "KRW-BTC", to: str | None = None) -> list[dict[str, Any]]:
        """
        분봉 캔들 데이터 조회 (최신 순으로 정렬되어 반환됨)
        - unit: 1, 3, 5, 10, 15, 30, 60, 240
        - count: 조회할 캔들 개수 (최대 200)
        - to: 마지막 캔들 시각 (예: 2026-08-25T12:00:00+09:00 또는 YYYY-MM-DD HH:MM:SS)
        """
        valid_set = self._get_valid_markets_set()
        if valid_set and market not in valid_set:
            return []
        endpoint = f"/candles/minutes/{unit}"
        params: dict[str, Any] = {
            "market": market,
            "count": count,
        }
        if to:
            params["to"] = to
        try:
            data = self._request("GET", endpoint, params=params)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def get_orderbooks(self, markets: list[str]) -> list[dict[str, Any]]:
        """
        여러 마켓의 실시간 호가창 정보 일괄 조회 (Upbit batch query /orderbook?markets=...)
        """
        if not markets:
            return []
        valid_set = self._get_valid_markets_set()
        filtered = [m for m in markets if not valid_set or m in valid_set]
        if not filtered:
            return []
        markets_str = ",".join(filtered)
        params = {"markets": markets_str}
        try:
            data = self._request("GET", "/orderbook", params=params)
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.debug(f"호가창 일괄 조회 예외: {e}")
            return []

    def get_orderbook(self, market: str = "KRW-BTC") -> dict[str, Any]:
        """
        실시간 호가창 정보 조회
        """
        valid_set = self._get_valid_markets_set()
        if valid_set and market not in valid_set:
            return {}
        params = {"markets": market}
        try:
            data = self._request("GET", "/orderbook", params=params)
            return data[0] if (isinstance(data, list) and data) else {}
        except Exception:
            return {}

    def get_chance(self, market: str = "KRW-BTC") -> dict[str, Any]:
        """
        마켓별 주문 가능 정보(최소 주문금액, 수수료 등) 조회
        """
        valid_set = self._get_valid_markets_set()
        if valid_set and market not in valid_set:
            return {}
        params = {"market": market}
        try:
            data = self._request("GET", "/orders/chance", params=params)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def get_current_price(self, market: str = "KRW-BTC", force_refresh: bool = False) -> float:
        """현재 체결가 float 조회. force_refresh=True면 캐시를 우회한다."""
        valid_set = self._get_valid_markets_set()
        if valid_set and market not in valid_set:
            return 0.0
        ticker = self.get_ticker(market, force_refresh=force_refresh)
        return float(ticker.get("trade_price", 0.0))

    @staticmethod
    def get_tick_size(price: float) -> float:
        """
        업비트 공식 KRW 마켓 호가 단위(Tick Size) 반환
        - 2,000,000원 이상: 1,000원 단위
        - 1,000,000 ~ 2,000,000원: 500원 단위
        - 500,000 ~ 1,000,000원: 100원 단위
        - 100,000 ~ 500,000원: 50원 단위
        - 10,000 ~ 100,000원: 10원 단위
        - 1,000 ~ 10,000원: 1원 단위
        - 100 ~ 1,000원: 1원 단위 (업비트 최신 기준)
        - 10 ~ 100원: 0.1원 단위
        - 1 ~ 10원: 0.01원 단위
        - 0.1 ~ 1원: 0.001원 단위
        - 0.1원 미만: 0.0001원 단위
        """
        if price >= 2_000_000:
            return 1000.0
        elif price >= 1_000_000:
            return 500.0
        elif price >= 500_000:
            return 100.0
        elif price >= 100_000:
            return 50.0
        elif price >= 10_000:
            return 10.0
        elif price >= 100:
            return 1.0
        elif price >= 10:
            return 0.1
        elif price >= 1:
            return 0.01
        elif price >= 0.1:
            return 0.001
        else:
            return 0.0001

    @staticmethod
    def adjust_price_to_tick(price: float, side: str = "bid", mode: str | None = None) -> float:
        """
        주문 방향에 따른 지정가 호가 단위 보정 (P0-1 안전 수칙)
        - 매수 지정가 (bid/buy/floor): 호가 단위 내림(floor)으로 예산 초과 및 불리한 체결 방지
        - 매도 지정가 (ask/sell/ceil): 호가 단위 올림(ceil)으로 불리한 슬리피지 방지
        - round (기존 호환): 단순 반올림
        """
        if price <= 0:
            return price

        tick = UpbitAPI.get_tick_size(price)
        if tick >= 1.0:
            precision = 0
        else:
            precision = len(str(tick).split(".")[1])

        if mode:
            m = mode.lower()
        else:
            s = str(side).lower()
            if s in ("bid", "buy"):
                m = "floor"
            elif s in ("ask", "sell"):
                m = "ceil"
            else:
                m = "round"

        if m == "floor":
            units = math.floor(round(price / tick, 8))
            res = units * tick
        elif m == "ceil":
            units = math.ceil(round(price / tick, 8))
            res = units * tick
        else:
            units = round(price / tick)
            res = units * tick

        return round(res, precision) if precision > 0 else float(int(round(res)))

    @staticmethod
    def round_price_to_tick(price: float) -> float:
        """
        업비트 공식 KRW 마켓 호가 단위(Tick Size)에 맞게 가격 자동 반올림 보정 (기존 외부 계약 호환성 유지)
        """
        return UpbitAPI.adjust_price_to_tick(price, mode="round")

    @staticmethod
    def round_volume(market: str, volume: float) -> float:
        """업비트 주문 가능 수량 정밀도 반올림 (소수점 8자리)"""
        if volume <= 0:
            return 0.0
        return round(volume, 8)

    def get_open_orders(self, market: str | None = None) -> list[dict[str, Any]]:
        """대기(미체결) 주문 목록 조회 (state=wait)"""
        params: dict[str, Any] = {
            "state": "wait",
        }
        if market:
            params["market"] = market
        try:
            data = self._request("GET", "/orders", params=params)
            orders = data if isinstance(data, list) else []
            normalized = []
            for order in orders:
                if isinstance(order, dict):
                    item = dict(order)
                    item.setdefault("order_id", item.get("uuid", ""))
                    normalized.append(item)
            return normalized
        except Exception as e:
            logger.warning(f"업비트 미체결 주문 조회 실패: {e}")
            return []

    def get_closed_orders(self, market: str | None = None) -> list[dict[str, Any]]:
        """종료(done/cancel) 주문 목록 조회"""
        params: dict[str, Any] = {
            "state": "done",
        }
        if market:
            params["market"] = market
        try:
            data = self._request("GET", "/orders", params=params)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def get_order(self, uuid_str: str = "", client_order_id: str = "") -> dict[str, Any]:
        """
        단건 주문 조회
        - uuid_str 또는 client_order_id(identifier)로 조회
        """
        if not uuid_str and not client_order_id:
            raise ValueError("주문 UUID 또는 identifier(client_order_id)가 필요합니다.")
        
        params: dict[str, Any] = {}
        if uuid_str:
            params["uuid"] = uuid_str
        elif client_order_id:
            params["identifier"] = client_order_id

        try:
            data = self._request("GET", "/order", params=params)
            if isinstance(data, dict):
                data.setdefault("order_id", data.get("uuid", ""))
                return data
            return {}
        except Exception as e:
            logger.warning(f"업비트 단건 주문 조회 실패: {e}")
            raise

    def create_order(
        self,
        market: str,
        side: str,
        volume: float | None = None,
        price: float | None = None,
        ord_type: str = "limit",
        client_order_id: str = "",
    ) -> dict[str, Any]:
        """
        업비트 주문 생성
        - side: 'bid' (매수), 'ask' (매도)
        - ord_type: 'limit' (지정가), 'price' (시장가 매수), 'market' (시장가 매도)
        - volume: 주문 수량 (limit, market 매도 필수)
        - price: 주문 가격 (limit, price 매수 필수)
        - client_order_id: 업비트 identifier (최대 64자, 멱등성 보장 고유값)

        [절대 보호 원칙] KRW-HOLO 주문 요청 시 즉각 차단 및 ValueError 발생
        """
        m_upper = market.upper()
        excluded = get_upbit_excluded_markets()
        if m_upper in excluded or m_upper.replace("KRW-", "") in excluded:
            logger.critical(f"🛑 [보호 규칙 위반 방어] 수동 매매 보호 종목 ({market}) 주문 시도가 차단되었습니다!")
            raise ValueError(f"KRW-HOLO는 수동 매매 보호 종목으로 자동 주문 생성이 엄격히 금지되어 있습니다: {market}")

        data: dict[str, Any] = {
            "market": market,
            "side": side,
            "ord_type": ord_type,
        }
        if client_order_id:
            # 업비트 API는 고유 식별자 파라미터명이 identifier 임
            data["identifier"] = str(client_order_id)[:64]

        if ord_type == "limit":
            if volume is None or price is None:
                raise ValueError("지정가(limit) 주문은 volume과 price가 모두 필요합니다.")
            adjusted_price = self.adjust_price_to_tick(price, side=side)
            data["volume"] = f"{volume:.8f}".rstrip("0").rstrip(".")
            data["price"] = str(int(adjusted_price) if adjusted_price.is_integer() else adjusted_price)

        elif ord_type == "price":  # 시장가 매수 (price = 총 원화 금액)
            if price is None:
                raise ValueError("시장가 매수(price)는 총 매수금액(price)이 필요합니다.")
            if price < 5000.0:
                raise ValueError(f"최소 주문 금액(5,000 KRW) 미달: {price:,.0f} KRW")
            data["price"] = str(int(price))

        elif ord_type == "market":  # 시장가 매도 (volume = 코인 수량)
            if volume is None:
                raise ValueError("시장가 매도(market)는 매도수량(volume)이 필요합니다.")
            data["volume"] = f"{volume:.8f}".rstrip("0").rstrip(".")

        logger.info(f"업비트 주문 요청 데이터: {data}")
        res = self._request("POST", "/orders", data=data)
        if isinstance(res, dict):
            res.setdefault("order_id", res.get("uuid", ""))
        return res

    def cancel_order(self, uuid_str: str = "", client_order_id: str = "") -> dict[str, Any]:
        """
        미체결 주문 취소
        - uuid_str 또는 identifier(client_order_id)
        """
        if not uuid_str and not client_order_id:
            raise ValueError("취소할 주문의 uuid 또는 identifier가 필요합니다.")
        
        params: dict[str, Any] = {}
        if uuid_str:
            params["uuid"] = uuid_str
        elif client_order_id:
            params["identifier"] = client_order_id

        logger.info(f"업비트 주문 취소 요청: uuid={uuid_str}, identifier={client_order_id}")
        return self._request("DELETE", "/order", params=params)

    def execute_twap_order(
        self,
        market: str,
        side: str,
        volume: float | None = None,
        price: float | None = None,
        ord_type: str = "limit",
        splits: int = 3,
        interval_seconds: float = 2.0,
    ) -> list[dict[str, Any]]:
        """
        시간 분할 주문 (TWAP)
        """
        if splits <= 1:
            return [self.create_order(market, side, volume, price, ord_type)]

        results = []
        if ord_type == "limit" and volume is not None and price is not None:
            slice_vol = volume / splits
            for i in range(splits):
                if i > 0:
                    time.sleep(interval_seconds)
                try:
                    res = self.create_order(market, side, volume=slice_vol, price=price, ord_type=ord_type)
                    results.append(res)
                    logger.info(f"⚡ [업비트 TWAP {i+1}/{splits}] {market} {side} {slice_vol:.6f}개 @ {price:,.2f}원")
                except Exception as e:
                    logger.warning(f"TWAP {i+1}/{splits} 주문 실패: {e}")

        elif ord_type == "price" and price is not None:
            slice_price = price / splits
            for i in range(splits):
                if i > 0:
                    time.sleep(interval_seconds)
                try:
                    res = self.create_order(market, side, price=slice_price, ord_type=ord_type)
                    results.append(res)
                    logger.info(f"⚡ [업비트 TWAP 시장가 매수 {i+1}/{splits}] {market} {slice_price:,.0f}원")
                except Exception as e:
                    logger.warning(f"TWAP 시장가 매수 {i+1}/{splits} 실패: {e}")

        elif ord_type == "market" and volume is not None:
            slice_vol = volume / splits
            for i in range(splits):
                if i > 0:
                    time.sleep(interval_seconds)
                try:
                    res = self.create_order(market, side, volume=slice_vol, ord_type=ord_type)
                    results.append(res)
                    logger.info(f"⚡ [업비트 TWAP 시장가 매도 {i+1}/{splits}] {market} {slice_vol:.6f}개")
                except Exception as e:
                    logger.warning(f"TWAP 매도 {i+1}/{splits} 실패: {e}")
        else:
            results.append(self.create_order(market, side, volume, price, ord_type))

        return results
