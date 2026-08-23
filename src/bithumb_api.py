import hashlib
import json
import logging
import time
import urllib.parse
import uuid
from typing import Any, Dict, List, Optional

import jwt
import requests

logger = logging.getLogger(__name__)


class BithumbAPI:
    """
    빗썸 API 2.0 (Open API) 통신 클라이언트
    - JWT 인증 (UUID 기반 nonce, SHA-512 기반 query_hash)
    - 계좌 잔고, 시세, 미체결 조회, 주문 생성/취소 기능 제공
    """

    BASE_URL = "https://api.bithumb.com/v1"

    def __init__(self, access_key: str, secret_key: str):
        if not access_key or not secret_key:
            raise ValueError("BITHUMB_ACCESS_KEY 및 BITHUMB_SECRET_KEY가 설정되어야 합니다.")
        self.access_key = access_key
        self.secret_key = secret_key

    def _generate_jwt_token(self, params: Optional[Dict[str, Any]] = None) -> str:
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
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """
        API 요청 공통 핸들러
        """
        url = f"{self.BASE_URL}{endpoint}"
        query_payload = params if params is not None else data
        jwt_token = self._generate_jwt_token(query_payload)

        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Content-Type": "application/json",
        }

        try:
            if method.upper() == "GET":
                response = requests.get(url, headers=headers, params=params, timeout=10)
            elif method.upper() == "POST":
                response = requests.post(url, headers=headers, json=data, timeout=10)
            elif method.upper() == "DELETE":
                response = requests.delete(url, headers=headers, params=params, timeout=10)
            else:
                raise ValueError(f"지원하지 않는 HTTP 메서드: {method}")

            if response.status_code not in (200, 201):
                logger.error(
                    f"Bithumb API Error [{response.status_code}] {endpoint}: {response.text}"
                )
                response.raise_for_status()

            return response.json()

        except requests.exceptions.RequestException as e:
            logger.error(f"HTTP Request Failed: {e}")
            raise

    def get_balances(self) -> Dict[str, Dict[str, float]]:
        """
        계좌 전체 잔고 조회
        반환: {'KRW': {'balance': float, 'locked': float, 'avg_buy_price': float}, ...}
        """
        data = self._request("GET", "/accounts")
        balances: Dict[str, Dict[str, float]] = {}

        for item in data:
            currency = item.get("currency")
            balances[currency] = {
                "balance": float(item.get("balance", 0.0)),
                "locked": float(item.get("locked", 0.0)),
                "avg_buy_price": float(item.get("avg_buy_price", 0.0)),
            }
        return balances

    def get_currency_balance(self, currency: str) -> Dict[str, float]:
        """
        특정 통화/코인의 잔고 정보 반환
        """
        balances = self.get_balances()
        return balances.get(
            currency.upper(), {"balance": 0.0, "locked": 0.0, "avg_buy_price": 0.0}
        )

    def get_ticker(self, market: str = "KRW-BTC") -> Dict[str, Any]:
        """
        현재 코인 시세 조회
        반환: Ticker 정보 딕셔너리 (trade_price, high_price, low_price, etc.)
        """
        params = {"markets": market}
        data = self._request("GET", "/ticker", params=params)
        if isinstance(data, list) and len(data) > 0:
            return data[0]
        return {}

    def get_candles(self, unit: int = 5, count: int = 30, market: str = "KRW-BTC") -> List[Dict[str, Any]]:
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
        data = self._request("GET", endpoint, params=params)
        return data if isinstance(data, list) else []

    def get_current_price(self, market: str = "KRW-BTC") -> float:
        """
        현재 체결가(trade_price) float 조회
        """
        ticker = self.get_ticker(market)
        return float(ticker.get("trade_price", 0.0))

    def get_open_orders(self, market: str = "KRW-BTC") -> List[Dict[str, Any]]:
        """
        현재 미체결 주문 목록 조회 (state='wait')
        """
        params = {
            "market": market,
            "state": "wait",
        }
        data = self._request("GET", "/orders", params=params)
        return data if isinstance(data, list) else []

    def create_order(
        self,
        market: str,
        side: str,
        volume: Optional[float] = None,
        price: Optional[float] = None,
        ord_type: str = "limit",
    ) -> Dict[str, Any]:
        """
        주문 생성
        - side: 'bid' (매수), 'ask' (매도)
        - ord_type: 'limit' (지정가), 'price' (시장가 매수), 'market' (시장가 매도)
        - volume: 주문 수량 (limit, market 필수)
        - price: 주문 가격 (limit, price 필수)
        """
        data: Dict[str, Any] = {
            "market": market,
            "side": side,
            "ord_type": ord_type,
        }

        if ord_type == "limit":
            if volume is None or price is None:
                raise ValueError("지정가(limit) 주문은 volume과 price가 모두 필요합니다.")
            data["volume"] = f"{volume:.8f}".rstrip("0").rstrip(".")
            data["price"] = str(int(price) if price.is_integer() else price)

        elif ord_type == "price":  # 시장가 매수 (price = 원화 총 금액)
            if price is None:
                raise ValueError("시장가 매수(price)는 총 매수금액(price)이 필요합니다.")
            data["price"] = str(int(price))

        elif ord_type == "market":  # 시장가 매도 (volume = 코인 수량)
            if volume is None:
                raise ValueError("시장가 매도(market)는 매도수량(volume)이 필요합니다.")
            data["volume"] = f"{volume:.8f}".rstrip("0").rstrip(".")

        logger.info(f"주문 요청 데이터: {data}")
        return self._request("POST", "/orders", data=data)

    def cancel_order(self, uuid_str: str) -> Dict[str, Any]:
        """
        미체결 주문 취소
        - uuid_str: 취소할 주문의 UUID
        """
        params = {"uuid": uuid_str}
        logger.info(f"주문 취소 요청 (UUID: {uuid_str})")
        return self._request("DELETE", "/order", params=params)
