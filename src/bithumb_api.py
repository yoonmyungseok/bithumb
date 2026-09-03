import hashlib
import logging
import math
import os
import time
from requests.adapters import HTTPAdapter
import urllib.parse
import uuid
import warnings
from typing import Any

import jwt
import requests

try:
    from jwt.warnings import InsecureKeyLengthWarning
    warnings.filterwarnings("ignore", category=InsecureKeyLengthWarning)
except ImportError:
    pass

from api_telemetry import ExchangeApiTelemetry

logger = logging.getLogger(__name__)


class BithumbAPI:
    """
    빗썸 API 2.0 (Open API) 통신 클라이언트
    - JWT 인증 (UUID 기반 nonce, SHA-512 기반 query_hash)
    - 계좌 잔고, 시세, 미체결 조회, 주문 생성/취소 기능 제공
    - HTTP Keep-Alive 커넥션 풀링을 통한 왕복 지연시간(RTT) 최적화
    """

    API_ROOT = "https://api.bithumb.com"

    def __init__(self, access_key: str = "", secret_key: str = ""):
        self.access_key = (access_key or os.getenv("BITHUMB_ACCESS_KEY", "")).strip()
        self.secret_key = (secret_key or os.getenv("BITHUMB_SECRET_KEY", "")).strip()
        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=1)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.telemetry = ExchangeApiTelemetry("bithumb")

    def _generate_jwt_token(self, params: dict[str, Any] | None = None) -> str:
        """
        빗썸 API 2.0 규격에 맞는 JWT 토큰 생성
        """
        payload = {
            "access_key": self.access_key,
            "nonce": str(uuid.uuid4()),
            "timestamp": int(time.time() * 1000),
        }

        if params:
            query_string = urllib.parse.urlencode(params).encode("utf-8")
            sha512 = hashlib.sha512()
            sha512.update(query_string)
            query_hash = sha512.hexdigest()

            payload["query_hash"] = query_hash
            payload["query_hash_alg"] = "SHA512"

        token = jwt.encode(payload, self.secret_key, algorithm="HS256")
        return token

    def _request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        max_retries: int = 3,
        api_version: str = "v1",
    ) -> Any:
        """
        API 요청 공통 핸들러.

        조회(GET)만 자동 재시도합니다. 주문 POST는 응답 유실 뒤 재시도하면
        중복 주문이 될 수 있으므로 호출자에게 불확실한 결과를 전달합니다.
        """
        url = f"{self.API_ROOT}/{api_version}{endpoint}"
        last_exception = None

        retryable = method.upper() == "GET"
        attempts = max_retries if retryable else 1
        for attempt in range(1, attempts + 1):
            headers = {"Content-Type": "application/json"}
            if self.access_key and self.secret_key:
                query_payload = params if params is not None else data
                jwt_token = self._generate_jwt_token(query_payload)
                headers["Authorization"] = f"Bearer {jwt_token}"

            try:
                if method.upper() == "GET":
                    response = self.session.get(url, headers=headers, params=params, timeout=10)
                elif method.upper() == "POST":
                    response = self.session.post(url, headers=headers, json=data, timeout=10)
                elif method.upper() == "DELETE":
                    response = self.session.delete(url, headers=headers, params=params, timeout=10)
                else:
                    raise ValueError(f"지원하지 않는 HTTP 메서드: {method}")

                # 텔레메트리 계측 기록
                self.telemetry.record_call(
                    method=method,
                    endpoint=endpoint,
                    status_code=response.status_code,
                )

                # 429(Rate Limit) 또는 5xx 서버 일시 에러 시 재시도
                if response.status_code in (429, 500, 502, 503, 504):
                    if retryable and attempt < attempts:
                        sleep_sec = 2 ** (attempt - 1)
                        logger.warning(
                            f"⚠️ Bithumb API [{response.status_code}] {endpoint} 일시 오류. {sleep_sec}초 후 재시도 ({attempt}/{max_retries})"
                        )
                        time.sleep(sleep_sec)
                        continue

                if response.status_code not in (200, 201):
                    logger.error(
                        f"Bithumb API Error [{response.status_code}] {endpoint}: {response.text}"
                    )
                    response.raise_for_status()

                return response.json()

            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                self.telemetry.record_call(method=method, endpoint=endpoint, status_code=0, is_error=True)
                last_exception = e
                if retryable and attempt < attempts:
                    sleep_sec = 2 ** (attempt - 1)
                    logger.warning(
                        f"⚠️ Bithumb API 네트워크 타임아웃/연결 오류: {e}. {sleep_sec}초 후 재시도 ({attempt}/{max_retries})"
                    )
                    time.sleep(sleep_sec)
                else:
                    logger.error(f"HTTP Request Failed after {attempts} attempts: {e}")
                    raise
            except requests.exceptions.RequestException as e:
                self.telemetry.record_call(method=method, endpoint=endpoint, status_code=0, is_error=True)
                logger.error(f"HTTP Request Failed: {e}")
                raise

        if last_exception:
            raise last_exception

    def get_telemetry(self) -> dict[str, Any]:
        """REST API 일일 호출량 및 상태 텔레메트리 반환"""
        return self.telemetry.to_dict()

    def get_balances(self) -> dict[str, dict[str, float]]:
        """
        계좌 전체 잔고 조회
        반환: {'KRW': {'balance': float, 'locked': float, 'avg_buy_price': float}, ...}
        """
        data = self._request("GET", "/accounts")
        balances: dict[str, dict[str, float]] = {}
        for item in data:
            currency = item.get("currency")
            balances[currency] = {
                "balance": float(item.get("balance", 0.0)),
                "locked": float(item.get("locked", 0.0)),
                "avg_buy_price": float(item.get("avg_buy_price", 0.0)),
            }
        return balances

    def get_currency_balance(self, currency: str) -> dict[str, float]:
        """
        특정 통화/코인의 잔고 정보 반환
        """
        balances = self.get_balances()
        return balances.get(
            currency.upper(), {"balance": 0.0, "locked": 0.0, "avg_buy_price": 0.0}
        )

    def _get_valid_markets_set(self) -> set[str]:
        """빗썸에서 거래 지원하는 유효 마켓 코드 세트 캐싱 반환"""
        if not hasattr(self, "_valid_markets_cache") or not self._valid_markets_cache:
            try:
                all_m = self.get_all_markets()
                self._valid_markets_cache = {m["market"] for m in all_m if "market" in m}
            except (requests.exceptions.RequestException, KeyError, ValueError):
                self._valid_markets_cache = set()
        return self._valid_markets_cache

    def get_all_markets(self, is_details: bool = False) -> list[dict[str, Any]]:
        """
        빗썸에서 거래 지원하는 전체 마켓 목록 조회 (1시간 메모리 캐싱)
        """
        now = time.time()
        if not hasattr(self, "_all_markets_cache"):
            self._all_markets_cache = {}
        cached_entry = self._all_markets_cache.get(is_details)
        if cached_entry:
            cached_time, cached_data = cached_entry
            if now - cached_time < 3600.0 and cached_data:
                return cached_data

        params = {"isDetails": "true" if is_details else "false"} if is_details else None
        try:
            data = self._request("GET", "/market/all", params=params)
            if isinstance(data, list) and data:
                self._all_markets_cache[is_details] = (now, data)
                return data
        except Exception as e:
            logger.warning(f"빗썸 전체 마켓 조회 실패: {e}")
            if cached_entry:
                return cached_entry[1]
        return []

    def get_korean_name(self, market: str) -> str:
        """
        마켓 코드(예: KRW-BTC)에 대응하는 한글 종목명(예: 비트코인) 반환
        """
        if not hasattr(self, "_market_name_map") or not self._market_name_map:
            try:
                all_m = self.get_all_markets()
                self._market_name_map = {m["market"]: m.get("korean_name", "") for m in all_m if "market" in m}
            except (requests.exceptions.RequestException, KeyError, ValueError):
                self._market_name_map = {}
        return self._market_name_map.get(market, market.split("-")[-1])

    def get_tickers(self, markets: list[str]) -> list[dict[str, Any]]:
        """
        여러 마켓의 Ticker 시세 일괄 조회 (미상장 마켓 404 방지)
        """
        if not markets:
            return []
        valid_set = self._get_valid_markets_set()
        filtered = [m for m in markets if not valid_set or m in valid_set]
        if not filtered:
            return []
        
        # 빗썸 API 규격에 맞춰 쉼표로 연결
        markets_str = ",".join(filtered)
        params = {"markets": markets_str}
        try:
            data = self._request("GET", "/ticker", params=params)
            return data if isinstance(data, list) else []
        except (requests.exceptions.RequestException, KeyError, ValueError):
            return []

    def get_ticker(self, market: str = "KRW-BTC") -> dict[str, Any]:
        """
        단일 코인 시세 조회
        """
        valid_set = self._get_valid_markets_set()
        if valid_set and market not in valid_set:
            return {}
        res = self.get_tickers([market])
        return res[0] if res else {}

    def get_candles(self, unit: int = 5, count: int = 30, market: str = "KRW-BTC", to: str | None = None) -> list[dict[str, Any]]:
        """
        분봉 캔들 데이터 조회 (최신 순으로 정렬되어 반환됨)
        - unit: 1, 3, 5, 15, 30, 60 등 분 단위
        - count: 조회할 캔들 개수 (최대 200)
        - to: 마지막 캔들 시각 (예: 2026-08-24 12:00:00)
        """
        valid_set = self._get_valid_markets_set()
        if valid_set and market not in valid_set:
            return []
        endpoint = f"/candles/minutes/{unit}"
        params = {
            "market": market,
            "count": count,
        }
        if to:
            params["to"] = to
        try:
            data = self._request("GET", endpoint, params=params)
            return data if isinstance(data, list) else []
        except (requests.exceptions.RequestException, KeyError, ValueError):
            return []

    def get_orderbooks(self, markets: list[str]) -> list[dict[str, Any]]:
        """
        여러 마켓의 실시간 호가창 정보 일괄 조회 (Bithumb batch query /orderbook?markets=...)
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
        except (requests.exceptions.RequestException, KeyError, ValueError):
            return []

    def get_orderbook(self, market: str = "KRW-BTC") -> dict[str, Any]:
        """
        실시간 호가창 정보 조회 (매수/매도 호가 잔량 및 체결강도 분석용)
        """
        valid_set = self._get_valid_markets_set()
        if valid_set and market not in valid_set:
            return {}
        params = {"markets": market}
        try:
            data = self._request("GET", "/orderbook", params=params)
            return data[0] if (isinstance(data, list) and data) else {}
        except (requests.exceptions.RequestException, KeyError, ValueError, IndexError):
            return {}

    def get_current_price(self, market: str = "KRW-BTC") -> float:
        """
        현재 체결가(trade_price) float 조회
        """
        ticker = self.get_ticker(market)
        return float(ticker.get("trade_price", 0.0))

    @staticmethod
    def get_tick_size(price: float) -> float:
        """
        빗썸 공식 KRW 마켓 호가 단위(Tick Size) 반환
        - 2,000,000 이상: 1,000원 단위
        - 1,000,000 ~ 2,000,000: 500원 단위
        - 500,000 ~ 1,000,000: 100원 단위
        - 100,000 ~ 500,000: 50원 단위
        - 10,000 ~ 100,000: 10원 단위
        - 1,000 ~ 10,000: 1원 단위
        - 100 ~ 1,000: 0.1원 단위 (정수형 마켓은 1원)
        - 10 ~ 100: 0.01원 단위
        - 1 ~ 10: 0.001원 단위
        - 1 미만: 0.0001원 단위
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
        elif price >= 1_000:
            return 1.0
        elif price >= 100:
            return 0.1
        elif price >= 10:
            return 0.01
        elif price >= 1:
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

        tick = BithumbAPI.get_tick_size(price)
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
        빗썸 공식 KRW 마켓 호가 단위(Tick Size)에 맞게 가격 자동 반올림 보정 (기존 외부 계약 호환성 유지)
        """
        return BithumbAPI.adjust_price_to_tick(price, mode="round")

    @staticmethod
    def round_volume(market: str, volume: float) -> float:
        """빗썸 마켓별 주문 가능 수량 정밀도 반올림 (기본 소수점 4자리)"""
        if volume <= 0:
            return 0.0
        return round(volume, 4)

    def get_open_orders(self, market: str | None = None) -> list[dict[str, Any]]:
        """v2 대기 주문 목록 조회. 반환 스키마를 기존 호출부와 호환시킨다."""
        params: dict[str, Any] = {}
        if market:
            params["market"] = market
        data = self._request("GET", "/orders/pending", params=params, api_version="v2")
        orders = data if isinstance(data, list) else data.get("orders", data.get("data", [])) if isinstance(data, dict) else []
        normalized = []
        for order in orders:
            if not isinstance(order, dict):
                continue
            item = dict(order)
            item.setdefault("uuid", item.get("order_id", ""))
            item.setdefault("ord_type", item.get("order_type", ""))
            normalized.append(item)
        return normalized

    def get_closed_orders(self, market: str | None = None) -> list[dict[str, Any]]:
        """v2 종료 주문(done/cancel) 목록으로 재시작 복구에 사용한다."""
        params: dict[str, Any] = {}
        if market:
            params["market"] = market
        data = self._request("GET", "/orders/history", params=params, api_version="v2")
        return data if isinstance(data, list) else data.get("orders", data.get("data", [])) if isinstance(data, dict) else []

    def get_order(self, uuid_str: str = "", client_order_id: str = "") -> dict[str, Any]:
        """Fetch one order's current exchange state for crash-recovery reconciliation."""
        if not uuid_str and not client_order_id:
            raise ValueError("주문 UUID 또는 client_order_id가 필요합니다.")
        
        # 빗썸 v1 개별 주문 단건 조회 표준 규격: GET /v1/order?uuid=... or identifier=...
        params: dict[str, Any] = {}
        if uuid_str:
            params["uuid"] = uuid_str
        if client_order_id:
            params["identifier"] = client_order_id

        try:
            data = self._request("GET", "/order", params=params, api_version="v1")
            if isinstance(data, dict):
                data.setdefault("uuid", data.get("order_id", uuid_str))
                data.setdefault("state", data.get("status", ""))
                return data
        except (requests.exceptions.RequestException, KeyError, ValueError) as exc:
            logger.debug("빗썸 단건 주문(v1) 조회 실패 (%s): %s", uuid_str or client_order_id, exc)

        # Fallback: 대기 주문 목록에서 검색
        try:
            open_orders = self.get_open_orders()
            for o in open_orders:
                if (uuid_str and (o.get("uuid") == uuid_str or o.get("order_id") == uuid_str)) or (client_order_id and o.get("client_order_id") == client_order_id):
                    o.setdefault("state", "wait")
                    return o
        except Exception:
            pass

        return {}

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
        주문 생성
        - side: 'bid' (매수), 'ask' (매도)
        - ord_type: 'limit' (지정가), 'price' (시장가 매수), 'market' (시장가 매도)
        - volume: 주문 수량 (limit, market 필수)
        - price: 주문 가격 (limit, price 필수)
        """
        # v2 is used intentionally: it is the documented order endpoint that
        # accepts client_order_id, which is essential for idempotent recovery.
        data: dict[str, Any] = {
            "market": market,
            "side": side,
            "order_type": ord_type,
        }
        if client_order_id:
            data["client_order_id"] = client_order_id

        if ord_type == "limit":
            if volume is None or price is None:
                raise ValueError("지정가(limit) 주문은 volume과 price가 모두 필요합니다.")
            
            # 호가 단위 방향별 자동 보정 (매수: 내림, 매도: 올림)
            adjusted_price = self.adjust_price_to_tick(price, side=side)
            data["volume"] = f"{volume:.8f}".rstrip("0").rstrip(".")
            data["price"] = str(int(adjusted_price) if adjusted_price.is_integer() else adjusted_price)

        elif ord_type == "price":  # 시장가 매수 (price = 원화 총 금액)
            if price is None:
                raise ValueError("시장가 매수(price)는 총 매수금액(price)이 필요합니다.")
            data["price"] = str(int(price))

        elif ord_type == "market":  # 시장가 매도 (volume = 코인 수량)
            if volume is None:
                raise ValueError("시장가 매도(market)는 매도수량(volume)이 필요합니다.")
            data["volume"] = f"{volume:.8f}".rstrip("0").rstrip(".")

        logger.info(f"주문 요청 데이터: {data}")
        return self._request("POST", "/orders", data=data, api_version="v2")

    def cancel_order(self, uuid_str: str = "", client_order_id: str = "") -> dict[str, Any]:
        """
        미체결 주문 취소
        - uuid_str: 취소할 주문의 UUID / order_id
        """
        if not uuid_str and not client_order_id:
            raise ValueError("취소할 order_id 또는 client_order_id가 필요합니다.")
        params = {"order_id": uuid_str} if uuid_str else {"client_order_id": client_order_id}
        logger.info("v2 주문 취소 요청: %s", uuid_str or client_order_id)
        try:
            return self._request("DELETE", "/order", params=params, api_version="v2")
        except (requests.exceptions.RequestException, KeyError, ValueError) as e:
            logger.warning(f"v2 주문 취소 실패 ({e}), v1 엔드포인트로 폴백 시도")
            v1_params = {"uuid": uuid_str} if uuid_str else {"client_order_id": client_order_id}
            return self._request("DELETE", "/order", params=v1_params, api_version="v1")

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
        기관용 TWAP (Time-Weighted Average Price) 시간 분할 주문 집행
        - 대량 주문을 여러 개로 분할하여 슬리피지(Slippage)와 호가 충격을 최소화
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
                    logger.info(f"⚡ [TWAP 분할 주문 {i+1}/{splits} 완료] {market} {side} {slice_vol:.6f}개 @ {price:,.2f}원")
                except (requests.exceptions.RequestException, KeyError, ValueError) as e:
                    logger.warning(f"TWAP {i+1}/{splits} 주문 실패: {e}")

        elif ord_type == "price" and price is not None:
            slice_price = price / splits
            for i in range(splits):
                if i > 0:
                    time.sleep(interval_seconds)
                try:
                    res = self.create_order(market, side, price=slice_price, ord_type=ord_type)
                    results.append(res)
                    logger.info(f"⚡ [TWAP 시장가 매수 {i+1}/{splits} 완료] {market} {slice_price:,.0f}원")
                except (requests.exceptions.RequestException, KeyError, ValueError) as e:
                    logger.warning(f"TWAP 시장가 {i+1}/{splits} 실패: {e}")

        elif ord_type == "market" and volume is not None:
            slice_vol = volume / splits
            for i in range(splits):
                if i > 0:
                    time.sleep(interval_seconds)
                try:
                    res = self.create_order(market, side, volume=slice_vol, ord_type=ord_type)
                    results.append(res)
                    logger.info(f"⚡ [TWAP 시장가 매도 {i+1}/{splits} 완료] {market} {slice_vol:.6f}개")
                except (requests.exceptions.RequestException, KeyError, ValueError) as e:
                    logger.warning(f"TWAP 매도 {i+1}/{splits} 실패: {e}")
        else:
            results.append(self.create_order(market, side, volume, price, ord_type))

        return results
