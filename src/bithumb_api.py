import hashlib
import logging
import os
import time
import urllib.parse
import uuid
from typing import Any

import jwt
import requests

logger = logging.getLogger(__name__)


class BithumbAPI:
    """
    빗썸 API 2.0 (Open API) 통신 클라이언트
    - JWT 인증 (UUID 기반 nonce, SHA-512 기반 query_hash)
    - 계좌 잔고, 시세, 미체결 조회, 주문 생성/취소 기능 제공
    """

    API_ROOT = "https://api.bithumb.com"

    def __init__(self, access_key: str = "", secret_key: str = ""):
        self.access_key = (access_key or os.getenv("BITHUMB_ACCESS_KEY", "")).strip()
        self.secret_key = (secret_key or os.getenv("BITHUMB_SECRET_KEY", "")).strip()

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
                    response = requests.get(url, headers=headers, params=params, timeout=10)
                elif method.upper() == "POST":
                    response = requests.post(url, headers=headers, json=data, timeout=10)
                elif method.upper() == "DELETE":
                    response = requests.delete(url, headers=headers, params=params, timeout=10)
                else:
                    raise ValueError(f"지원하지 않는 HTTP 메서드: {method}")

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
                logger.error(f"HTTP Request Failed: {e}")
                raise

        if last_exception:
            raise last_exception

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

    def get_all_markets(self) -> list[dict[str, Any]]:
        """
        빗썸에서 거래 지원하는 전체 마켓 목록 조회
        """
        data = self._request("GET", "/market/all")
        return data if isinstance(data, list) else []

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
        여러 마켓의 Ticker 시세 일괄 조회
        """
        if not markets:
            return []
        
        # 빗썸 API 규격에 맞춰 쉼표로 연결
        markets_str = ",".join(markets)
        params = {"markets": markets_str}
        data = self._request("GET", "/ticker", params=params)
        return data if isinstance(data, list) else []

    def get_ticker(self, market: str = "KRW-BTC") -> dict[str, Any]:
        """
        단일 코인 시세 조회
        """
        res = self.get_tickers([market])
        return res[0] if res else {}

    def get_candles(self, unit: int = 5, count: int = 30, market: str = "KRW-BTC") -> list[dict[str, Any]]:
        """
        분봉 캔들 데이터 조회 (최신 순으로 정렬되어 반환됨)
        - unit: 1, 3, 5, 15, 30, 60 등 분 단위
        - count: 조회할 캔들 개수 (최대 200)
        """
        endpoint = f"/candles/minutes/{unit}"
        params = {
            "market": market,
            "count": count,
        }
        try:
            data = self._request("GET", endpoint, params=params)
            return data if isinstance(data, list) else []
        except (requests.exceptions.RequestException, KeyError, ValueError):
            return []

    def get_orderbook(self, market: str = "KRW-BTC") -> dict[str, Any]:
        """
        실시간 호가창 정보 조회 (매수/매도 호가 잔량 및 체결강도 분석용)
        """
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
    def round_price_to_tick(price: float) -> float:
        """
        빗썸 공식 KRW 마켓 호가 단위(Tick Size)에 맞게 가격 자동 반올림 보정
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
        if price <= 0:
            return price
        if price >= 2_000_000:
            return float(round(price / 1000) * 1000)
        elif price >= 1_000_000:
            return float(round(price / 500) * 500)
        elif price >= 500_000:
            return float(round(price / 100) * 100)
        elif price >= 100_000:
            return float(round(price / 50) * 50)
        elif price >= 10_000:
            return float(round(price / 10) * 10)
        elif price >= 1_000:
            return float(round(price))
        elif price >= 100:
            return round(price, 1)
        elif price >= 10:
            return round(price, 2)
        elif price >= 1:
            return round(price, 3)
        else:
            return round(price, 4)

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
        
        # v2 endpoint with fallback to v1
        params = {"order_id": uuid_str} if uuid_str else {"client_order_id": client_order_id}
        try:
            data = self._request("GET", "/order", params=params, api_version="v2")
        except (requests.exceptions.RequestException, KeyError, ValueError):
            v1_params = {"uuid": uuid_str} if uuid_str else {"client_order_id": client_order_id}
            data = self._request("GET", "/order", params=v1_params, api_version="v1")

        if isinstance(data, dict):
            # 필드 정규화
            data.setdefault("uuid", data.get("order_id", ""))
            data.setdefault("state", data.get("status", ""))
            return data
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
            
            # 호가 단위 자동 반올림 보정
            adjusted_price = self.round_price_to_tick(price)
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
        - uuid_str: 취소할 주문의 UUID
        """
        if not uuid_str and not client_order_id:
            raise ValueError("취소할 order_id 또는 client_order_id가 필요합니다.")
        params = {"order_id": uuid_str} if uuid_str else {"client_order_id": client_order_id}
        logger.info("v2 주문 취소 요청: %s", uuid_str or client_order_id)
        return self._request("DELETE", "/order", params=params, api_version="v2")

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
