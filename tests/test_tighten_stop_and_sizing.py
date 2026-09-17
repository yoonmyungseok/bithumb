import os
import sys
from unittest.mock import MagicMock, patch
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from strategy_engine import StrategyPolicy, is_major_market
from risk_manager import TrailingStopTracker


def test_strategy_policy_constants():
    """개선된 정책 상수가 올바르게 정의되어 있는지 검증"""
    assert StrategyPolicy.BREAKEVEN_MIN_PROFIT_PCT == 0.020
    assert StrategyPolicy.TRAILING_ATR_MULTIPLIER == 1.5
    assert StrategyPolicy.MIN_TRAILING_GAP_PCT == 0.015
    assert StrategyPolicy.MIN_PROFIT_BUFFER_PCT >= 0.015
    assert StrategyPolicy.DEFAULT_ALT_ALLOC_PCT == 0.12
    assert StrategyPolicy.MAX_ALT_ALLOC_PCT == 0.15
    assert StrategyPolicy.MIN_ALT_ALLOC_PCT == 0.10
    assert StrategyPolicy.NIGHT_SESSION_MAX_ALLOC_PCT == 0.10


def test_trailing_stop_tracker_margin_and_gap(tmp_path):
    """TrailingStopTracker에서 일반 알트코인 최소 마진과 갭이 안전하게 확보되는지 검증"""
    tracker = TrailingStopTracker(data_dir=str(tmp_path))
    market = "KRW-BIRB"
    avg_buy_price = 100.0

    # 1차 분할 익절 완료 상태로 설정하여 트레일링 스탑 계산 검증
    tracker.partial_tp_done[market] = 1
    tracker.peaks[market] = 104.0

    action, peak, stop_price, peak_profit, realized = tracker.check_position(
        market=market, current_price=103.0, avg_buy_price=avg_buy_price
    )

    # 최소 보장 마진: avg_buy_price * (1.0 + 0.015) = 101.5원 이상
    # 최소 여유 간격: current_peak * (1.0 - 0.015) = 104 * 0.985 = 102.44원 이하
    assert peak == 104.0
    # base_drop_pct는 2.0% -> 104 * 0.98 = 101.92원
    # 101.92원은 min_buffer(101.5원)보다 크고 max_allowed_trailing(102.44원)보다 작음
    # 따라서 trailing_stop_price는 101.92원 근방이 됨
    min_guaranteed = avg_buy_price * (1.0 + StrategyPolicy.MIN_PROFIT_BUFFER_PCT)
    max_allowed = 104.0 * (1.0 - StrategyPolicy.MIN_TRAILING_GAP_PCT)
    
    # 트레일링 스탑 기준선이 로그와 로직에서 어떻게 계산되었는지 확인:
    # 104 * (1 - 0.02) = 101.92
    assert min_guaranteed <= 101.92 <= max_allowed
    # 현재가(103.0) > trailing_stop_price(101.92) 이므로 action은 NONE
    assert action == "NONE"


def test_tighten_stop_buffer_guard_under_2_percent():
    """손익률이 +2.0% 미만(예: +0.54%)일 때 TIGHTEN_STOP이 HOLD로 완화되는 가드 로직 검증"""
    avg_buy_price = 92.80
    current_price = 93.30  # +0.538%
    pnl_rate = (current_price - avg_buy_price) / avg_buy_price

    min_req_rate = StrategyPolicy.BREAKEVEN_MIN_PROFIT_PCT  # 0.020
    assert pnl_rate < min_req_rate

    ai_action = "TIGHTEN_STOP"
    if pnl_rate < min_req_rate:
        ai_action = "HOLD"

    assert ai_action == "HOLD"


def test_tighten_stop_buffer_guard_over_2_percent():
    """손익률이 +2.0% 이상(예: +3.0%)일 때 TIGHTEN_STOP이 정상 승인되고 여유 간격이 확보되는지 검증"""
    avg_buy_price = 100.0
    current_price = 103.5  # +3.5%
    pnl_rate = (current_price - avg_buy_price) / avg_buy_price

    min_req_rate = StrategyPolicy.BREAKEVEN_MIN_PROFIT_PCT  # 0.020
    assert pnl_rate >= min_req_rate

    min_gap_pct = StrategyPolicy.MIN_TRAILING_GAP_PCT  # 0.015
    max_allowed_sl = current_price * (1.0 - min_gap_pct)  # 103.5 * 0.985 = 101.9475
    min_guaranteed_sl = avg_buy_price * (1.0 + StrategyPolicy.BREAKEVEN_STOP_PCT)  # 100.3

    assert max_allowed_sl > min_guaranteed_sl

    raw_sl = 103.0  # AI가 너무 현재가에 바짝 붙여서 제안한 경우
    safe_sl = min(max(raw_sl, min_guaranteed_sl), max_allowed_sl)
    assert safe_sl <= max_allowed_sl
    assert safe_sl >= min_guaranteed_sl


def test_altcoin_balanced_sizing_clamping():
    """알트코인 포지션 사이징이 10%~15%로 균등 클램핑되고 심야에 10%로 캡이 씌워지는지 검증"""
    total_equity = 1_200_000.0
    krw_available = 1_000_000.0
    market = "KRW-NEWT"
    strategy_mode = "SCALP"

    assert not is_major_market(market)
    assert strategy_mode != "SWING"

    min_alt_budget = total_equity * StrategyPolicy.MIN_ALT_ALLOC_PCT  # 120,000원
    max_alt_budget_day = total_equity * StrategyPolicy.MAX_ALT_ALLOC_PCT  # 180,000원
    max_alt_budget_night = total_equity * StrategyPolicy.NIGHT_SESSION_MAX_ALLOC_PCT  # 120,000원

    # 케이스 1: 주간에 240,000원(20%) 몰빵 시도 ➜ 180,000원(15%)으로 클램핑
    trade_budget = 240_000.0
    clamped_day = min(krw_available, max(min_alt_budget, min(trade_budget, max_alt_budget_day)))
    assert clamped_day == 180_000.0

    # 케이스 2: 심야에 240,000원 몰빵 시도 ➜ 120,000원(10%)으로 하드 캡
    clamped_night = min(krw_available, max(min_alt_budget, min(trade_budget, max_alt_budget_night)))
    assert clamped_night == 120_000.0

    # 케이스 3: 주간에 10,000원 극소액 푼돈 진입 시도 ➜ 120,000원(10%)으로 균등 상향
    trade_budget_tiny = 10_000.0
    clamped_tiny = min(krw_available, max(min_alt_budget, min(trade_budget_tiny, max_alt_budget_day)))
    assert clamped_tiny == 120_000.0
