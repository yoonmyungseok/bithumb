"""
로컬 퀀트 고알파 자율 매수(Local Autonomous Buy) 검증 단위 테스트:
1. StrategyPolicy 환경 변수 제어 및 헬퍼 메서드
2. AI 예산 부재(allow_ai_analysis=False) 시 고알파(70점 이상) 자율 BUY 실행
3. 알파 점수 기준 미달(70점 미만) 시 HOLD 관망 유지
4. 자율 매수 기능 비활성화 시 HOLD 관망 유지
"""

import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import pytest
from strategy_engine import StrategyPolicy
from trading_runtime import MarketEntryInputs, TradingCycleEngine


def test_strategy_policy_local_autonomous_buy_defaults_and_env():
    """StrategyPolicy 자율 매수 기본값 및 환경변수 오버라이드 동작 검증."""
    assert StrategyPolicy.is_local_autonomous_buy_enabled() is True
    assert StrategyPolicy.get_local_autonomous_buy_min_alpha() == 70
    assert StrategyPolicy.get_local_autonomous_buy_alloc_ratio() == 0.80

    with patch.dict(os.environ, {"LOCAL_AUTONOMOUS_BUY_ENABLED": "false"}):
        assert StrategyPolicy.is_local_autonomous_buy_enabled() is False

    with patch.dict(os.environ, {"LOCAL_AUTONOMOUS_BUY_MIN_ALPHA": "85"}):
        assert StrategyPolicy.get_local_autonomous_buy_min_alpha() == 85

    with patch.dict(os.environ, {"LOCAL_AUTONOMOUS_BUY_ALLOC_RATIO": "0.65"}):
        assert StrategyPolicy.get_local_autonomous_buy_alloc_ratio() == 0.65


def _build_dummy_runtime_and_inputs(
    allow_ai: bool = False,
    local_allow_buy: bool = True,
    alpha_score: int = 75,
    current_price: float = 1000.0,
):
    """process_entry_gating 테스트를 위한 더미 런타임 및 입력값 구성"""
    candles_5m = [
        {
            "opening_price": 995.0,
            "high_price": 1005.0,
            "low_price": 990.0,
            "trade_price": current_price,
            "candle_acc_trade_volume": 100.0,
            "timestamp": 1234567890 + i * 300,
        }
        for i in range(30)
    ]
    candles_1h = [
        {
            "opening_price": 980.0,
            "high_price": 1010.0,
            "low_price": 970.0,
            "trade_price": current_price,
            "candle_acc_trade_volume": 1000.0,
            "timestamp": 1234567890 + i * 3600,
        }
        for i in range(30)
    ]
    candles_4h = [
        {
            "opening_price": 950.0,
            "high_price": 1020.0,
            "low_price": 940.0,
            "trade_price": current_price,
            "candle_acc_trade_volume": 5000.0,
            "timestamp": 1234567890 + i * 14400,
        }
        for i in range(25)
    ]
    btc_candles = candles_5m

    orderbook = {
        "orderbook_units": [
            {"bid_price": 999.0, "ask_price": 1001.0, "bid_size": 10.0, "ask_size": 5.0}
        ]
    }

    entry_profile = MagicMock()
    entry_profile.signal_exchange = "upbit"
    entry_profile.use_closed_candles = True
    entry_profile.block_on_reentry_denied = False
    entry_profile.use_hold_price_fallbacks = True
    entry_profile.use_dynamic_default_alloc = True
    entry_profile.whale_flow_requires_capability = False
    entry_profile.hold_reason_fallback = "관망"
    entry_profile.ws_unhealthy_label = "WebSocket"
    entry_profile.recovery_db_exchange = "upbit"

    config = MagicMock()
    config.interval_minutes = 5
    config.new_buy_block_reason = None
    config.min_order_krw = 5000.0
    config.max_position_pct = 0.20
    config.profile = MagicMock()
    config.exit_profile = MagicMock()
    config.entry_profile = entry_profile
    config.buy_profile = MagicMock()
    config.analyzer_factory = None
    config.gemini_api_key = None

    ctx = MagicMock()
    ctx.exchange.get_candles.return_value = candles_5m
    ctx.exchange.get_orderbook.return_value = orderbook
    ctx.cooldown_manager.is_in_cooldown.return_value = False
    ctx.cooldown_manager.check_reentry_allowed.return_value = (True, "재진입 가능")
    ctx.risk_manager.is_crashing.return_value = False
    ctx.risk_manager.cooldown_until_ts = 0.0
    ctx.ws_client.get_health_status.return_value = {"is_healthy": True, "status": "OK"}
    ctx.ws_client.get_whale_flow_summary.return_value = ""
    ctx.trade_memory.get_feedback_context.return_value = ""
    ctx.decision_db.has_recovery_entry_since.return_value = False

    runtime = TradingCycleEngine(config=config, context=ctx)

    market_inputs = MarketEntryInputs(
        exchange=ctx.exchange,
        market="KRW-TEST",
        korean_name="테스트코인",
        candidate_type="STANDARD",
        candidate_metadata={},
        analyzer=None,
        coin_available=0.0,
        avg_buy_price=0.0,
        current_price=current_price,
        coin_value=0.0,
        krw_available=1000000.0,
        candles_5m=candles_5m,
        candles_1h=candles_1h,
        candles_4h=candles_4h,
        orderbook=orderbook,
        btc_regime="NORMAL",
        btc_status_msg="정상",
        is_btc_crashing=False,
        is_cooldown=False,
        is_extreme_fear=False,
        is_bot_paused=False,
        is_kill_switch=False,
        is_entry_ready=True,
        dyn_max_pos_pct=0.20,
        now_str="2026-09-24 08:00:00",
        audit_decision=lambda *args, **kwargs: None,
        allow_ai_analysis=allow_ai,
        momentum_entry_slot_available=True,
        btc_candles_5m=btc_candles,
    )

    dummy_signal = {
        "allow_buy": local_allow_buy,
        "alpha_score": alpha_score,
        "entry_price": current_price,
        "target_price": current_price * 1.03,
        "stop_loss": current_price * 0.98,
        "reason": "테스트 퀀트 돌파 시그널",
    }

    return runtime, market_inputs, dummy_signal


@patch("trading_runtime.has_confirmed_swing_trend_candles", return_value=True)
@patch("trading_runtime.select_completed_candles", side_effect=lambda c, minimum_count=20: c or [])
def test_local_autonomous_buy_triggers_when_ai_disabled_and_high_alpha(mock_select, mock_has_swing):
    """AI 예산이 없더라도(allow_ai=False) 퀀트 allow_buy=True 및 고알파(75점)이면 자율 매수가 발동하는지 검증."""
    runtime, market_inputs, dummy_signal = _build_dummy_runtime_and_inputs(
        allow_ai=False, local_allow_buy=True, alpha_score=75
    )

    with patch("trading_runtime.entry_signal", return_value=dummy_signal):
        result = runtime.process_entry_gating(market_inputs)

    assert result.should_continue is False
    assert result.action == "BUY"
    assert "[로컬 퀀트 고알파 자율 매수·AI예산보존]" in result.reason
    assert "(알파:75점)" in result.reason
    # 기본 비중 0.20 * 0.80 = 0.16
    assert pytest.approx(result.alloc_pct, 0.001) == 0.16
    assert result.called_ai is False


@patch("trading_runtime.has_confirmed_swing_trend_candles", return_value=True)
@patch("trading_runtime.select_completed_candles", side_effect=lambda c, minimum_count=20: c or [])
def test_local_autonomous_buy_blocked_when_alpha_below_threshold(mock_select, mock_has_swing):
    """알파 점수가 65점(기준 70점 미만)인 경우 AI가 없을 때 HOLD로 관망 유지되는지 검증."""
    runtime, market_inputs, dummy_signal = _build_dummy_runtime_and_inputs(
        allow_ai=False, local_allow_buy=True, alpha_score=65
    )

    with patch("trading_runtime.entry_signal", return_value=dummy_signal):
        result = runtime.process_entry_gating(market_inputs)

    assert result.action == "HOLD"
    assert "1차 퀀트 관망 대기" in result.reason
    assert result.called_ai is False


@patch("trading_runtime.has_confirmed_swing_trend_candles", return_value=True)
@patch("trading_runtime.select_completed_candles", side_effect=lambda c, minimum_count=20: c or [])
def test_local_autonomous_buy_disabled_by_policy(mock_select, mock_has_swing):
    """LOCAL_AUTONOMOUS_BUY_ENABLED=False일 때는 고알파여도 자율 매수를 실행하지 않고 HOLD 유지되는지 검증."""
    runtime, market_inputs, dummy_signal = _build_dummy_runtime_and_inputs(
        allow_ai=False, local_allow_buy=True, alpha_score=85
    )

    with patch.dict(os.environ, {"LOCAL_AUTONOMOUS_BUY_ENABLED": "false"}), \
         patch("trading_runtime.entry_signal", return_value=dummy_signal):
        result = runtime.process_entry_gating(market_inputs)

    assert result.action == "HOLD"
    assert "1차 퀀트 관망 대기" in result.reason
