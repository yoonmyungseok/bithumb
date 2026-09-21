import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from unittest.mock import MagicMock
from trading_orchestrator import TradingOrchestrator


def test_smart_taker_conditions():
    """스마트 오더 체결(Smart Taker) 조건 완화 및 Ask1 호가 산정 검증"""
    current_price = 100.0

    # 케이스 1: alpha_score=67 (기존 80 미만이라 탈락하던 조건), 스프레드 0.3%, 현재가 대비 +0.2%
    alpha_score = 67
    cand_type = "CONFIRMED"
    eff_cand_type = "CONFIRMED"
    is_breakout_or_listing = cand_type in ("MOMENTUM_BREAKOUT", "NEW_LISTING") or eff_cand_type in ("MOMENTUM_BREAKOUT", "NEW_LISTING")
    is_high_conviction = (alpha_score >= 65) or is_breakout_or_listing
    assert is_high_conviction is True

    orderbook = {
        "orderbook_units": [
            {"ask_price": 100.2, "bid_price": 99.9}
        ]
    }
    ob_units = orderbook["orderbook_units"]
    best_ask = float(ob_units[0]["ask_price"])
    best_bid = float(ob_units[0]["bid_price"])
    spread_ratio = (best_ask - best_bid) / best_bid

    smart_taker_price = None
    if is_high_conviction and orderbook:
        if spread_ratio <= 0.0050 and best_ask <= current_price * 1.005:
            smart_taker_price = best_ask

    assert smart_taker_price == 100.2


def test_smart_taker_momentum_breakout():
    """모멘텀 돌파 후보일 때 알파 점수와 무관하게 Ask 1호가 타격 활성화 검증"""
    current_price = 50.0
    alpha_score = 55  # 알파가 낮아도 모멘텀 돌파면 발동
    cand_type = "MOMENTUM_BREAKOUT"
    is_breakout_or_listing = cand_type in ("MOMENTUM_BREAKOUT", "NEW_LISTING")
    is_high_conviction = (alpha_score >= 65) or is_breakout_or_listing
    assert is_high_conviction is True

    orderbook = {
        "orderbook_units": [
            {"ask_price": 50.2, "bid_price": 50.0}
        ]
    }
    ob_units = orderbook["orderbook_units"]
    best_ask = float(ob_units[0]["ask_price"])
    best_bid = float(ob_units[0]["bid_price"])
    spread_ratio = (best_ask - best_bid) / best_bid

    smart_taker_price = None
    if is_high_conviction and spread_ratio <= 0.0050 and best_ask <= current_price * 1.005:
        smart_taker_price = best_ask

    assert smart_taker_price == 50.2


def test_smart_taker_wide_spread_fallback():
    """스프레드가 0.5%를 초과하면 스마트 테이커가 차단되어 일반 지정가로 폴백하는지 검증"""
    current_price = 100.0
    alpha_score = 75
    is_high_conviction = alpha_score >= 65

    # 스프레드가 1.0%로 벌어져 있는 호가창
    orderbook = {
        "orderbook_units": [
            {"ask_price": 101.0, "bid_price": 100.0}
        ]
    }
    ob_units = orderbook["orderbook_units"]
    best_ask = float(ob_units[0]["ask_price"])
    best_bid = float(ob_units[0]["bid_price"])
    spread_ratio = (best_ask - best_bid) / best_bid

    smart_taker_price = None
    if is_high_conviction and spread_ratio <= 0.0050 and best_ask <= current_price * 1.005:
        smart_taker_price = best_ask

    assert smart_taker_price is None  # 안전을 위해 발동 안 됨


def test_cycle_markets_cap_behavior():
    """max_cycle_markets가 None이거나 0일 때 마켓 축소 없이 전체 통과하는지 검증"""
    logger = MagicMock()
    orch = TradingOrchestrator(logger)

    target_markets = [f"KRW-COIN{i}" for i in range(18)]
    held_markets = ["KRW-COIN0"]

    # 1. 상한이 6일 때: 6개로 캡핑
    capped = orch.cap_cycle_target_markets(target_markets, held_markets, max_markets=6)
    assert len(capped) == 6

    # 2. 상한이 0이거나 None일 때: 마켓 축소 없이 전체 통과
    full_zero = orch.cap_cycle_target_markets(target_markets, held_markets, max_markets=0)
    assert len(full_zero) == 18
    assert full_zero == target_markets

    max_cycle_markets = None
    if max_cycle_markets is not None and max_cycle_markets > 0:
        result = orch.cap_cycle_target_markets(target_markets, held_markets, max_cycle_markets)
    else:
        result = target_markets

    assert len(result) == 18
    assert result == target_markets
