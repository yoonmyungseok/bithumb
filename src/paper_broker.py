"""현실적인 호가 유동성·슬리피지·미체결 주문을 반영하는 페이퍼 트레이딩 브로커."""

import os
import time
import uuid
from typing import Any

from order_safety import write_json_atomically


class PaperBroker:
    def __init__(
        self,
        public_api: Any,
        initial_krw: float,
        fee_rate: float = 0.0,
        state_path: str | None = None,
        data_dir: str | None = None,
        slippage_rate: float | None = None,
    ):
        self.public_api = public_api
        self.fee_rate = max(0.0, float(fee_rate))
        if slippage_rate is None:
            slippage_rate = float(os.getenv("PAPER_SLIPPAGE_RATE", "0.0005"))
        self.slippage_rate = max(0.0, float(slippage_rate))
        d_dir = data_dir or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        os.makedirs(d_dir, exist_ok=True)
        self.state_path = state_path or os.path.join(d_dir, "paper_account.json")
        self.balances = self._load(initial_krw)
        self.open_orders: dict[str, dict[str, Any]] = {}
        self.completed_orders: dict[str, dict[str, Any]] = {}

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

    def _fetch_orderbook_units(self, market: str) -> list[dict[str, Any]]:
        getter = getattr(self.public_api, "get_orderbook", None)
        if not callable(getter):
            return []
        try:
            ob = getter(market)
        except Exception:
            return []
        if not isinstance(ob, dict):
            return []
        units = ob.get("orderbook_units")
        return units if isinstance(units, list) else []

    def _estimate_vwap_fill(
        self,
        market: str,
        side: str,
        ord_type: str,
        limit_price: float | None,
        target_krw: float | None = None,
        target_vol: float | None = None,
    ) -> tuple[float | None, bool]:
        """호가창 잔량을 기반으로 현실적인 체결가(VWAP)와 즉시 체결 가능 여부를 계산한다."""
        units = self._fetch_orderbook_units(market)
        if not units:
            spot = float(limit_price or self.get_current_price(market))
            if spot <= 0:
                return None, False
            if ord_type in ("price", "market"):
                adj = (1.0 + self.slippage_rate) if side == "bid" else (1.0 - self.slippage_rate)
                return spot * adj, True
            if side == "bid":
                return min(float(limit_price or spot), spot * (1.0 + self.slippage_rate)), True
            return max(float(limit_price or spot), spot * (1.0 - self.slippage_rate)), True

        if side == "bid":
            levels = sorted(
                (
                    (float(u.get("ask_price", 0.0) or 0.0), float(u.get("ask_size", 0.0) or 0.0))
                    for u in units
                    if float(u.get("ask_price", 0.0) or 0.0) > 0 and float(u.get("ask_size", 0.0) or 0.0) > 0
                ),
                key=lambda x: x[0],
            )
            if not levels:
                return None, False
            best_ask = levels[0][0]
            if ord_type == "limit" and limit_price is not None and limit_price < best_ask:
                return None, False

            filled_cost = 0.0
            filled_vol = 0.0
            rem_krw = float(target_krw or 0.0)
            rem_vol = float(target_vol or 0.0)
            for ask_p, ask_sz in levels:
                if ord_type == "limit" and limit_price is not None and ask_p > limit_price:
                    break
                if target_krw is not None:
                    if rem_krw <= 0:
                        break
                    level_krw = ask_p * ask_sz
                    take_krw = min(rem_krw, level_krw)
                    filled_cost += take_krw
                    filled_vol += take_krw / ask_p
                    rem_krw -= take_krw
                else:
                    if rem_vol <= 0:
                        break
                    take_vol = min(rem_vol, ask_sz)
                    filled_cost += take_vol * ask_p
                    filled_vol += take_vol
                    rem_vol -= take_vol

            if filled_vol <= 0:
                return None, False
            # 호가 잔량 초과분은 최후 체결 호가에 슬리피지를 가산해 보수적으로 반영한다.
            if (target_krw is not None and rem_krw > 1e-6) or (target_vol is not None and rem_vol > 1e-12):
                tail_price = levels[-1][0] * (1.0 + self.slippage_rate)
                if ord_type == "limit" and limit_price is not None:
                    tail_price = min(limit_price, tail_price)
                if target_krw is not None:
                    filled_cost += rem_krw
                    filled_vol += rem_krw / tail_price
                else:
                    filled_cost += rem_vol * tail_price
                    filled_vol += rem_vol
            return filled_cost / filled_vol, True

        levels = sorted(
            (
                (float(u.get("bid_price", 0.0) or 0.0), float(u.get("bid_size", 0.0) or 0.0))
                for u in units
                if float(u.get("bid_price", 0.0) or 0.0) > 0 and float(u.get("bid_size", 0.0) or 0.0) > 0
            ),
            key=lambda x: x[0],
            reverse=True,
        )
        if not levels:
            return None, False
        best_bid = levels[0][0]
        if ord_type == "limit" and limit_price is not None and limit_price > best_bid:
            return None, False

        filled_proceeds = 0.0
        filled_vol = 0.0
        rem_vol = float(target_vol or 0.0)
        for bid_p, bid_sz in levels:
            if ord_type == "limit" and limit_price is not None and bid_p < limit_price:
                break
            if rem_vol <= 0:
                break
            take_vol = min(rem_vol, bid_sz)
            filled_proceeds += take_vol * bid_p
            filled_vol += take_vol
            rem_vol -= take_vol

        if filled_vol <= 0:
            return None, False
        if rem_vol > 1e-12:
            tail_price = levels[-1][0] * (1.0 - self.slippage_rate)
            if ord_type == "limit" and limit_price is not None:
                tail_price = max(limit_price, tail_price)
            filled_proceeds += rem_vol * tail_price
            filled_vol += rem_vol
        return filled_proceeds / filled_vol, True

    def _find_order(self, uuid_str: str = "", client_order_id: str = "") -> dict[str, Any] | None:
        if uuid_str and uuid_str in self.open_orders:
            return self.open_orders[uuid_str]
        if uuid_str and uuid_str in self.completed_orders:
            return self.completed_orders[uuid_str]
        if client_order_id:
            for order in self.open_orders.values():
                if order.get("client_order_id") == client_order_id:
                    return order
            for order in self.completed_orders.values():
                if order.get("client_order_id") == client_order_id:
                    return order
        return None

    def _try_fill_open_orders(self, market: str | None = None) -> None:
        for order_id in list(self.open_orders.keys()):
            order = self.open_orders[order_id]
            if market and order.get("market") != market:
                continue
            side = order["side"]
            limit_price = float(order["price"])
            volume = float(order["volume"])
            fill_price, can_fill = self._estimate_vwap_fill(
                order["market"],
                side,
                "limit",
                limit_price,
                target_vol=volume,
            )
            if not can_fill or fill_price is None:
                continue
            self._settle_open_order(order, fill_price)

    def _settle_open_order(self, order: dict[str, Any], fill_price: float) -> None:
        market = order["market"]
        currency = market.split("-")[-1]
        side = order["side"]
        volume = float(order["volume"])
        reserved_krw = float(order.get("reserved_krw", 0.0))
        krw = self.balances["KRW"]
        coin = self.balances.setdefault(currency, {"balance": 0.0, "locked": 0.0, "avg_buy_price": 0.0})

        if side == "bid":
            spend = min(reserved_krw, volume * fill_price)
            refund = max(0.0, reserved_krw - spend)
            krw["locked"] = max(0.0, float(krw.get("locked", 0.0)) - reserved_krw)
            krw["balance"] += refund
            filled_volume = spend * (1.0 - self.fee_rate) / fill_price
            prior_value = coin["balance"] * coin["avg_buy_price"]
            coin["balance"] += filled_volume
            coin["avg_buy_price"] = (prior_value + spend) / coin["balance"] if coin["balance"] else 0.0
        else:
            coin["locked"] = max(0.0, float(coin.get("locked", 0.0)) - volume)
            krw["balance"] += volume * fill_price * (1.0 - self.fee_rate)

        order["state"] = "done"
        order["status"] = "done"
        order["avg_price"] = fill_price
        order["executed_volume"] = volume
        order["remaining_volume"] = 0.0
        self.open_orders.pop(order["uuid"], None)
        self.completed_orders[order["uuid"]] = order
        self._save()

    def get_open_orders(self, market: str | None = None) -> list[dict[str, Any]]:
        self._try_fill_open_orders(market=market)
        orders = list(self.open_orders.values())
        if market:
            orders = [o for o in orders if o.get("market") == market]
        return [dict(o) for o in orders]

    def get_order(self, uuid_str: str = "", client_order_id: str = "") -> dict[str, Any]:
        self._try_fill_open_orders()
        found = self._find_order(uuid_str=uuid_str, client_order_id=client_order_id)
        if found is not None:
            return dict(found)
        return {
            "uuid": uuid_str or f"paper-{client_order_id}",
            "order_id": uuid_str or f"paper-{client_order_id}",
            "client_order_id": client_order_id,
            "state": "done",
            "status": "done",
        }

    def cancel_order(self, uuid_str: str = "", client_order_id: str = "") -> dict[str, Any]:
        found = self._find_order(uuid_str=uuid_str, client_order_id=client_order_id)
        if found and found.get("uuid") in self.open_orders:
            order = self.open_orders.pop(found["uuid"])
            currency = order["market"].split("-")[-1]
            if order["side"] == "bid":
                reserved = float(order.get("reserved_krw", 0.0))
                self.balances["KRW"]["locked"] = max(0.0, float(self.balances["KRW"].get("locked", 0.0)) - reserved)
                self.balances["KRW"]["balance"] += reserved
            else:
                vol = float(order.get("volume", 0.0))
                coin = self.balances.setdefault(currency, {"balance": 0.0, "locked": 0.0, "avg_buy_price": 0.0})
                coin["locked"] = max(0.0, float(coin.get("locked", 0.0)) - vol)
                coin["balance"] += vol
            order["state"] = "cancel"
            order["status"] = "cancel"
            self.completed_orders[order["uuid"]] = order
            self._save()
            return dict(order)
        return {
            "uuid": uuid_str or f"paper-{client_order_id}",
            "order_id": uuid_str or f"paper-{client_order_id}",
            "client_order_id": client_order_id,
            "state": "cancel",
            "status": "cancel",
        }

    def create_order(self, market: str, side: str, volume: float | None = None, price: float | None = None, ord_type: str = "limit", client_order_id: str = "") -> dict[str, Any]:
        currency = market.split("-")[-1]
        krw = self.balances["KRW"]
        coin = self.balances.setdefault(currency, {"balance": 0.0, "locked": 0.0, "avg_buy_price": 0.0})
        order_uuid = f"paper-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"

        if side == "bid":
            limit_price = float(price) if price is not None else None
            target_krw = float(price) if ord_type == "price" and price else None
            target_vol = float(volume or 0.0) if target_krw is None else None
            if target_krw is not None and target_krw <= 0:
                raise ValueError("페이퍼 매수 금액이 유효하지 않습니다.")
            if target_krw is None and (not target_vol or target_vol <= 0):
                raise ValueError("페이퍼 매수 수량이 유효하지 않습니다.")

            fill_price, can_fill = self._estimate_vwap_fill(
                market,
                "bid",
                ord_type,
                limit_price,
                target_krw=target_krw,
                target_vol=target_vol,
            )
            if not can_fill or fill_price is None:
                if ord_type != "limit" or limit_price is None or limit_price <= 0:
                    raise ValueError("페이퍼 주문 가격이 유효하지 않습니다.")
                reserved = float(volume or 0.0) * limit_price
                if reserved <= 0 or reserved > krw["balance"]:
                    raise ValueError("페이퍼 매수 가용 KRW 부족")
                krw["balance"] -= reserved
                krw["locked"] = float(krw.get("locked", 0.0)) + reserved
                order = {
                    "uuid": order_uuid,
                    "order_id": order_uuid,
                    "client_order_id": client_order_id,
                    "market": market,
                    "side": "bid",
                    "ord_type": ord_type,
                    "price": limit_price,
                    "volume": float(volume or 0.0),
                    "remaining_volume": float(volume or 0.0),
                    "executed_volume": 0.0,
                    "reserved_krw": reserved,
                    "state": "wait",
                    "status": "wait",
                }
                self.open_orders[order_uuid] = order
                self._save()
                return dict(order)

            if fill_price <= 0:
                raise ValueError("페이퍼 주문 가격이 유효하지 않습니다.")
            spend = target_krw if target_krw is not None else float(volume or 0.0) * fill_price
            if spend <= 0 or spend > krw["balance"]:
                raise ValueError("페이퍼 매수 가용 KRW 부족")
            filled_volume = spend * (1.0 - self.fee_rate) / fill_price
            prior_value = coin["balance"] * coin["avg_buy_price"]
            coin["balance"] += filled_volume
            coin["avg_buy_price"] = (prior_value + spend) / coin["balance"] if coin["balance"] else 0.0
            krw["balance"] -= spend
        elif side == "ask":
            req_volume = float(volume or 0.0)
            if req_volume <= 0 or req_volume > coin["balance"] + 1e-12:
                raise ValueError("페이퍼 매도 보유수량 부족")
            limit_price = float(price) if price is not None else None
            fill_price, can_fill = self._estimate_vwap_fill(
                market,
                "ask",
                ord_type,
                limit_price,
                target_vol=req_volume,
            )
            if not can_fill or fill_price is None:
                if ord_type != "limit" or limit_price is None or limit_price <= 0:
                    raise ValueError("페이퍼 주문 가격이 유효하지 않습니다.")
                coin["balance"] -= req_volume
                coin["locked"] = float(coin.get("locked", 0.0)) + req_volume
                order = {
                    "uuid": order_uuid,
                    "order_id": order_uuid,
                    "client_order_id": client_order_id,
                    "market": market,
                    "side": "ask",
                    "ord_type": ord_type,
                    "price": limit_price,
                    "volume": req_volume,
                    "remaining_volume": req_volume,
                    "executed_volume": 0.0,
                    "state": "wait",
                    "status": "wait",
                }
                self.open_orders[order_uuid] = order
                self._save()
                return dict(order)

            if fill_price <= 0:
                raise ValueError("페이퍼 주문 가격이 유효하지 않습니다.")
            coin["balance"] -= req_volume
            krw["balance"] += req_volume * fill_price * (1.0 - self.fee_rate)
            filled_volume = req_volume
        else:
            raise ValueError(f"지원하지 않는 주문 방향: {side}")

        self._save()
        done_order = {
            "uuid": order_uuid,
            "order_id": order_uuid,
            "client_order_id": client_order_id,
            "state": "done",
            "status": "done",
            "market": market,
            "side": side,
            "avg_price": fill_price,
            "executed_volume": filled_volume,
            "remaining_volume": 0.0,
        }
        self.completed_orders[order_uuid] = done_order
        return dict(done_order)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.public_api, name)
