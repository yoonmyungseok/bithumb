import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from trading_runtime import validate_emergency_exit_safety, TradingCycleEngine


def test_guard1_manual_position_protection():
    """봇 매수 이력이 없는 외부/수동 매수 포지션은 긴급 탈출 자동 매도 차단 검증."""
    ai_eval = {"action": "EMERGENCY_EXIT", "confidence": 95, "reason": "덤핑 징후"}
    is_approved, reason, fallback = validate_emergency_exit_safety(
        market="KRW-TRUMP",
        korean_name="오피셜트럼프",
        current_price=3065.0,
        avg_buy_price=3071.0,
        pnl_pct_current=-0.20,
        hold_duration_sec=600.0,
        ai_eval=ai_eval,
        candles_5m=[],
        is_btc_crashing=False,
        is_bot_managed=False,  # 수동 매수 포지션
    )
    assert not is_approved
    assert fallback == "HOLD"
    assert "수동 관리 포지션 보호" in reason


def test_guard2_low_confidence_blocked():
    """AI 신뢰도가 80점 미만인 경우 긴급 탈출 차단 검증."""
    ai_eval = {"action": "EMERGENCY_EXIT", "confidence": 75, "reason": "불안한 흐름"}
    is_approved, reason, fallback = validate_emergency_exit_safety(
        market="KRW-ETH",
        korean_name="이더리움",
        current_price=3400000.0,
        avg_buy_price=3450000.0,
        pnl_pct_current=-1.45,
        hold_duration_sec=1200.0,
        ai_eval=ai_eval,
        candles_5m=[],
        is_btc_crashing=False,
        is_bot_managed=True,
    )
    assert not is_approved
    assert fallback == "HOLD"
    assert "AI 신뢰도 부족" in reason


def test_guard3_early_noise_protection_user_incident_reproduction():
    """실제 발생 사건 재현: 진입 후 10분 미만 & 손익률 -0.20%에서 긴급 탈출 차단 검증."""
    ai_eval = {
        "action": "EMERGENCY_EXIT",
        "confidence": 85,
        "reason": "체결강도 4.0% 급락 및 VWAP 하단 이탈",
    }
    is_approved, reason, fallback = validate_emergency_exit_safety(
        market="KRW-TRUMP",
        korean_name="오피셜트럼프",
        current_price=3065.0,
        avg_buy_price=3071.0,
        pnl_pct_current=-0.20,
        hold_duration_sec=300.0,  # 5분 보유 (10분 미만)
        ai_eval=ai_eval,
        candles_5m=[
            {"trade_price": 3065.0, "opening_price": 3070.0}
        ],
        is_btc_crashing=False,
        is_bot_managed=True,
    )
    assert not is_approved
    assert fallback == "HOLD"
    assert "진입 초기 노이즈 보호" in reason


def test_guard3_early_noise_exception_on_hard_crash():
    """진입 10분 미만이라도 손실률이 -2.0%를 초과하는 심각한 급락일 때는 비상 탈출 승인."""
    ai_eval = {
        "action": "EMERGENCY_EXIT",
        "confidence": 90,
        "reason": "장대음봉 폭락",
    }
    is_approved, reason, fallback = validate_emergency_exit_safety(
        market="KRW-ALT",
        korean_name="알트코인",
        current_price=970.0,
        avg_buy_price=1000.0,
        pnl_pct_current=-3.0,  # -3% 폭락
        hold_duration_sec=180.0,
        ai_eval=ai_eval,
        candles_5m=[
            {"trade_price": 970.0, "opening_price": 1000.0},
            {"trade_price": 1000.0, "opening_price": 1010.0},
        ],
        is_btc_crashing=False,
        is_bot_managed=True,
    )
    assert is_approved
    assert fallback == "EMERGENCY_EXIT"
    assert "다층 안전 가드 통과" in reason


def test_guard4_breakeven_downgrades_to_tighten_stop():
    """경미한 손실 구간(-0.6% 이내)에서는 시장가 투매 대신 TIGHTEN_STOP으로 완화 검증."""
    ai_eval = {
        "action": "EMERGENCY_EXIT",
        "confidence": 85,
        "reason": "매도벽 출회",
    }
    is_approved, reason, fallback = validate_emergency_exit_safety(
        market="KRW-SOL",
        korean_name="솔라나",
        current_price=199500.0,
        avg_buy_price=200000.0,
        pnl_pct_current=-0.25,  # -0.25% (>= -0.60%)
        hold_duration_sec=900.0,  # 15분 경과
        ai_eval=ai_eval,
        candles_5m=[
            {"trade_price": 199500.0, "opening_price": 200000.0},
            {"trade_price": 200000.0, "opening_price": 200500.0},
        ],
        is_btc_crashing=False,
        is_bot_managed=True,
    )
    assert not is_approved
    assert fallback == "TIGHTEN_STOP"
    assert "TIGHTEN_STOP(방어 손절선)으로 완화" in reason


def test_guard5_bullish_candle_holds():
    """최신 5분봉이 양봉 지지 상태인 경우 단순 지표 노이즈로 보고 탈출 유보 검증."""
    ai_eval = {
        "action": "EMERGENCY_EXIT",
        "confidence": 85,
        "reason": "체결강도 일시 저하",
    }
    is_approved, reason, fallback = validate_emergency_exit_safety(
        market="KRW-NEAR",
        korean_name="니어",
        current_price=6050.0,
        avg_buy_price=6100.0,
        pnl_pct_current=-0.82,  # -0.82% (보호 범위 -0.6%는 벗어남)
        hold_duration_sec=1200.0,  # 20분 경과
        ai_eval=ai_eval,
        candles_5m=[
            {"trade_price": 6050.0, "opening_price": 6020.0},  # 양봉 (6050 >= 6020)
            {"trade_price": 6020.0, "opening_price": 6080.0},
        ],
        is_btc_crashing=False,
        is_bot_managed=True,
    )
    assert not is_approved
    assert fallback == "HOLD"
    assert "양봉 지지 유지" in reason


def test_all_guards_pass_real_emergency():
    """진짜 위험 상황(충분한 보유 시간, 손익 -1.8%, 음봉 하락, 신뢰도 90)에서는 승인 검증."""
    ai_eval = {
        "action": "EMERGENCY_EXIT",
        "confidence": 90,
        "reason": "대량 거래량 실린 음봉 급락 및 세력 덤핑",
    }
    is_approved, reason, fallback = validate_emergency_exit_safety(
        market="KRW-DUMP",
        korean_name="덤프코인",
        current_price=800.0,
        avg_buy_price=820.0,
        pnl_pct_current=-2.44,
        hold_duration_sec=1200.0,
        ai_eval=ai_eval,
        candles_5m=[
            {"trade_price": 800.0, "opening_price": 815.0},  # 음봉
            {"trade_price": 815.0, "opening_price": 820.0},
        ],
        is_btc_crashing=False,
        is_bot_managed=True,
    )
    assert is_approved
    assert fallback == "EMERGENCY_EXIT"
    assert "다층 안전 가드 통과" in reason


def test_is_bot_managed_position():
    """주문 저널 기반 봇 관리 포지션 vs 수동 매수 포지션 판별 테스트."""
    engine = MagicMock(spec=TradingCycleEngine)
    engine.context = MagicMock()
    journal = MagicMock()
    engine.context.order_journal = journal
    engine.context.trailing_tracker.get_entry_time.return_value = 0.0

    # 케이스 1: 봇이 매수한 포지션
    journal.orders = [
        {"market": "KRW-BTC", "side": "bid", "status": "FILLED"},
    ]
    assert TradingCycleEngine._is_bot_managed_position(engine, "KRW-BTC") is True

    # 케이스 2: 봇이 전량 매도 완료 후 남은 잔고 (수동 매수)
    journal.orders = [
        {"market": "KRW-TRUMP", "side": "bid", "status": "FILLED"},
        {"market": "KRW-TRUMP", "side": "ask", "status": "FILLED", "exit_reason": "TRAILING_STOP"},
    ]
    assert TradingCycleEngine._is_bot_managed_position(engine, "KRW-TRUMP") is False

    # 케이스 3: 봇이 분할 매도(PARTIAL_TP)한 후 남은 잔여 포지션
    journal.orders = [
        {"market": "KRW-ETH", "side": "bid", "status": "FILLED"},
        {"market": "KRW-ETH", "side": "ask", "status": "FILLED", "exit_reason": "PARTIAL_TP_1"},
    ]
    assert TradingCycleEngine._is_bot_managed_position(engine, "KRW-ETH") is True

    # 케이스 4: 봇 주문 내역에 아예 없는 종목 (앱에서 직접 수동 매수한 코인)
    journal.orders = []
    assert TradingCycleEngine._is_bot_managed_position(engine, "KRW-NEW") is False


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    tests = [
        test_guard1_manual_position_protection,
        test_guard2_low_confidence_blocked,
        test_guard3_early_noise_protection_user_incident_reproduction,
        test_guard3_early_noise_exception_on_hard_crash,
        test_guard4_breakeven_downgrades_to_tighten_stop,
        test_guard5_bullish_candle_holds,
        test_all_guards_pass_real_emergency,
        test_is_bot_managed_position,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            print(f"[OK] {t.__name__} PASSED")
            passed += 1
        except Exception as e:
            print(f"[FAIL] {t.__name__} FAILED: {e}")
            failed += 1
    print(f"\nResults: {passed} passed, {failed} failed")
    if failed > 0:
        sys.exit(1)
