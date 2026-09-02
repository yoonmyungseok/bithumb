"""Protected-market helpers shared by order submission paths."""

from market_policy import get_excluded_markets


def get_excluded_markets_set() -> set[str]:
    """수동 매매 보호 종목 집합 반환 (P3-1 단일 진실의 원천)"""
    return get_excluded_markets()
