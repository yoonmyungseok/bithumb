"""A durable paper-trading adapter using live public Bithumb market data.

It deliberately never invokes a private exchange endpoint.  Limit orders are
filled immediately at their requested price so results are optimistic unless
backtest slippage is configured separately.
"""

import os
import time
import uuid
from typing import Any

from order_safety import write_json_atomically


class PaperBroker:
    def __init__(self, public_api: Any, initial_krw: float, fee_rate: float = 0.0):
        self.public_api = public_api
        self.fee_rate = max(0.0, fee_rate)
        data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        self.state_path = os.path.join(data_dir, "paper_account.json")
        self.balances = self._load(initial_krw)

    def _load(self, initial_krw: float) -> dict[str, dict[str, float]]:
        try:
            import json
            with open(self.state_path, "r", encoding="utf-8") as file:
                saved = json.load(file)
            if isinstance(saved, dict) and "KRW" in saved:
                return saved
        except (FileNotFoundError, OSError, ValueError):
            pass
        return {"KRW": {"balance": float(initial_krw), "locked": 0.0, "avg_buy_price": 0.0}}

    def _save(self) -> None:
        write_json_atomically(self.state_path, self.balances)

    def get_balances(self) -> dict[str, dict[str, float]]:
        return {currency: values.copy() for currency, values in self.balances.items()}

    def get_open_orders(self, market: str | None = None) -> list[dict[str, Any]]:
        return []

    def get_order(self, uuid_str: str) -> dict[str, Any]:
        return {"uuid": uuid_str, "state": "done"}

    def cancel_order(self, uuid_str: str) -> dict[str, Any]:
        return {"uuid": uuid_str, "state": "cancel"}

    def create_order(self, market: str, side: str, volume: float | None = None, price: float | None = None, ord_type: str = "limit", client_order_id: str = "") -> dict[str, Any]:
        currency = market.split("-")[-1]
        fill_price = float(price or self.get_current_price(market))
        if fill_price <= 0:
            raise ValueError("페이퍼 주문 가격이 유효하지 않습니다.")
        krw = self.balances["KRW"]
        coin = self.balances.setdefault(currency, {"balance": 0.0, "locked": 0.0, "avg_buy_price": 0.0})

        if side == "bid":
            spend = float(price) if ord_type == "price" and price else float(volume or 0.0) * fill_price
            if spend <= 0 or spend > krw["balance"]:
                raise ValueError("페이퍼 매수 가용 KRW 부족")
            filled_volume = spend * (1.0 - self.fee_rate) / fill_price
            prior_value = coin["balance"] * coin["avg_buy_price"]
            coin["balance"] += filled_volume
            coin["avg_buy_price"] = (prior_value + spend) / coin["balance"] if coin["balance"] else 0.0
            krw["balance"] -= spend
        elif side == "ask":
            filled_volume = float(volume or 0.0)
            if filled_volume <= 0 or filled_volume > coin["balance"] + 1e-12:
                raise ValueError("페이퍼 매도 보유수량 부족")
            coin["balance"] -= filled_volume
            krw["balance"] += filled_volume * fill_price * (1.0 - self.fee_rate)
        else:
            raise ValueError(f"지원하지 않는 주문 방향: {side}")

        self._save()
        return {"uuid": f"paper-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}", "client_order_id": client_order_id, "state": "done", "market": market}

    def __getattr__(self, name: str) -> Any:
        return getattr(self.public_api, name)
