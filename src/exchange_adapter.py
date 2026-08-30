"""Exchange-neutral boundary for the shared trading orchestrator.

The adapters deliberately wrap the existing API clients instead of translating
their responses.  That keeps the current order-journal and reconciliation
contracts intact while allowing the orchestration flow to become exchange
independent in a later migration step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ExchangeClient(Protocol):
    """Minimum normalized client surface already shared by both exchanges."""

    def get_balances(self) -> dict[str, dict[str, float]]: ...
    def get_candles(self, unit: int = 5, count: int = 30, market: str = "KRW-BTC", to: str | None = None) -> list[dict[str, Any]]: ...
    def get_orderbook(self, market: str = "KRW-BTC") -> dict[str, Any]: ...
    def get_current_price(self, market: str = "KRW-BTC") -> float: ...
    def adjust_price_to_tick(self, price: float, side: str = "bid", mode: str | None = None) -> float: ...
    def get_open_orders(self, market: str | None = None) -> list[dict[str, Any]]: ...
    def get_order(self, uuid_str: str = "", client_order_id: str = "") -> dict[str, Any]: ...
    def create_order(self, market: str, side: str, volume: float | None = None, price: float | None = None, ord_type: str = "limit", client_order_id: str = "") -> dict[str, Any]: ...
    def cancel_order(self, uuid_str: str = "", client_order_id: str = "") -> dict[str, Any]: ...


@dataclass(frozen=True)
class ExchangeProfile:
    """Exchange-specific values that must not leak into common orchestration."""

    key: str
    display_name: str
    data_dir: str
    web_port: int
    excluded_markets: frozenset[str] = field(default_factory=frozenset)


class ExchangeAdapter:
    """Thin, behavior-preserving wrapper around an existing exchange client."""

    def __init__(self, client: ExchangeClient, profile: ExchangeProfile):
        self.client = client
        self.profile = profile

    @property
    def key(self) -> str:
        return self.profile.key

    def is_tradeable_market(self, market: str) -> bool:
        return market.strip().upper() not in self.profile.excluded_markets

    def ensure_tradeable_market(self, market: str) -> None:
        if not self.is_tradeable_market(market):
            raise ValueError(f"{self.profile.display_name} 보호 종목은 자동매매할 수 없습니다: {market}")

    def get_balances(self) -> dict[str, dict[str, float]]:
        return self.client.get_balances()

    def get_candles(self, unit: int = 5, count: int = 30, market: str = "KRW-BTC", to: str | None = None) -> list[dict[str, Any]]:
        return self.client.get_candles(unit=unit, count=count, market=market, to=to)

    def get_orderbook(self, market: str = "KRW-BTC") -> dict[str, Any]:
        return self.client.get_orderbook(market=market)

    def get_current_price(self, market: str = "KRW-BTC") -> float:
        return self.client.get_current_price(market=market)

    def adjust_price_to_tick(self, price: float, side: str = "bid", mode: str | None = None) -> float:
        return self.client.adjust_price_to_tick(price, side=side, mode=mode)

    def get_open_orders(self, market: str | None = None) -> list[dict[str, Any]]:
        return self.client.get_open_orders(market=market)

    def get_order(self, uuid_str: str = "", client_order_id: str = "") -> dict[str, Any]:
        return self.client.get_order(uuid_str=uuid_str, client_order_id=client_order_id)

    def create_order(self, market: str, side: str, volume: float | None = None, price: float | None = None, ord_type: str = "limit", client_order_id: str = "") -> dict[str, Any]:
        self.ensure_tradeable_market(market)
        return self.client.create_order(
            market=market,
            side=side,
            volume=volume,
            price=price,
            ord_type=ord_type,
            client_order_id=client_order_id,
        )

    def cancel_order(self, uuid_str: str = "", client_order_id: str = "") -> dict[str, Any]:
        return self.client.cancel_order(uuid_str=uuid_str, client_order_id=client_order_id)

    def get_order_by_client_id(self, client_order_id: str) -> dict[str, Any]:
        """Optional exchange capability normalized at the adapter edge."""
        method = getattr(self.client, "get_order_by_client_id", None)
        return method(client_order_id) if callable(method) else {}

    def get_korean_name(self, market: str) -> str:
        """Presentation metadata is explicit instead of escaping via __getattr__."""
        method = getattr(self.client, "get_korean_name", None)
        return str(method(market)) if callable(method) else market.split("-")[-1]

    def round_volume(self, market: str, volume: float) -> float:
        method = getattr(self.client, "round_volume", None)
        return float(method(market, volume)) if callable(method) else float(volume)

    def get_all_markets(self, is_details: bool = False) -> list[dict[str, Any]]:
        """Market catalog capability used by the shared screener."""
        method = getattr(self.client, "get_all_markets", None)
        if not callable(method):
            return []
        try:
            return method(is_details=is_details)
        except TypeError:
            return method()

    def get_tickers(self, markets: list[str]) -> list[dict[str, Any]]:
        method = getattr(self.client, "get_tickers", None)
        return method(markets) if callable(method) else []

    def get_orderbooks(self, markets: list[str]) -> list[dict[str, Any]]:
        method = getattr(self.client, "get_orderbooks", None)
        return method(markets) if callable(method) else []


class BithumbAdapter(ExchangeAdapter):
    def __init__(self, client: ExchangeClient, data_dir: str = "data", web_port: int = 7979):
        super().__init__(client, ExchangeProfile("bithumb", "빗썸", data_dir, web_port))


class UpbitAdapter(ExchangeAdapter):
    def __init__(self, client: ExchangeClient, data_dir: str = "data/upbit", web_port: int = 7980, excluded_markets: frozenset[str] | None = None):
        protected = excluded_markets or frozenset({"KRW-HOLO"})
        super().__init__(client, ExchangeProfile("upbit", "업비트", data_dir, web_port, protected))
