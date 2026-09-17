import datetime
import os
import sys
from unittest.mock import MagicMock, patch
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from market_screener import (
    MarketScreener,
    is_reset_grace_period,
    get_effective_change_rate_thresholds,
)


def _make_kst(hour: int, minute: int) -> datetime.datetime:
    kst = datetime.timezone(datetime.timedelta(hours=9))
    return datetime.datetime(2026, 9, 17, hour, minute, 0, tzinfo=kst)


def test_is_reset_grace_period_upbit():
    # 업비트는 09:00 ~ 09:29 KST에 완충 세션 활성
    assert is_reset_grace_period("upbit", _make_kst(9, 0)) is True
    assert is_reset_grace_period("upbit", _make_kst(9, 15)) is True
    assert is_reset_grace_period("upbit", _make_kst(9, 29)) is True
    assert is_reset_grace_period("upbit", _make_kst(9, 30)) is False
    assert is_reset_grace_period("upbit", _make_kst(8, 59)) is False
    assert is_reset_grace_period("upbit", _make_kst(14, 0)) is False
    assert is_reset_grace_period("upbit", _make_kst(0, 15)) is False


def test_is_reset_grace_period_bithumb():
    # 빗썸은 00:00 ~ 00:29 KST에 완충 세션 활성
    assert is_reset_grace_period("bithumb", _make_kst(0, 0)) is True
    assert is_reset_grace_period("bithumb", _make_kst(0, 15)) is True
    assert is_reset_grace_period("bithumb", _make_kst(0, 29)) is True
    assert is_reset_grace_period("bithumb", _make_kst(0, 30)) is False
    assert is_reset_grace_period("bithumb", _make_kst(23, 59)) is False
    assert is_reset_grace_period("bithumb", _make_kst(9, 15)) is False


def test_get_effective_change_rate_thresholds():
    # 완충 세션 OFF -> 기존 임계값 유지
    eff_min, eff_early = get_effective_change_rate_thresholds(0.005, 0.003, in_grace_period=False)
    assert eff_min == 0.005
    assert eff_early == 0.003

    # 완충 세션 ON -> 50% 수준 완화 (최소 0.002, 0.001)
    eff_min, eff_early = get_effective_change_rate_thresholds(0.005, 0.003, in_grace_period=True)
    assert eff_min == pytest.approx(0.0025)
    assert eff_early == pytest.approx(0.0015)

    # 매우 낮은 기본값이 주어졌을 때 하한선(0.002, 0.001) 방어
    eff_min, eff_early = get_effective_change_rate_thresholds(0.002, 0.001, in_grace_period=True)
    assert eff_min == 0.002
    assert eff_early == 0.001


def test_screener_grace_period_candidate_selection(monkeypatch):
    """업비트 09:10 KST 상황에서 완충된 변동률(+0.3%) 종목이 스크리너에 정상 발굴되는지 검증"""
    mock_api = MagicMock()
    mock_api.key = "upbit"
    mock_api.get_all_markets.return_value = [
        {"market": "KRW-BTC"},
        {"market": "KRW-DOGE"},
    ]
    mock_api.get_tickers.return_value = [
        {
            "market": "KRW-BTC",
            "trade_price": 80_000_000.0,
            "signed_change_rate": 0.001,
            "acc_trade_price_24h": 50_000_000_000.0,
        },
        {
            "market": "KRW-DOGE",
            "trade_price": 200.0,
            "signed_change_rate": 0.003,  # +0.3% (기본 0.5% 미만이지만 완충 0.25% 이상)
            "acc_trade_price_24h": 5_000_000_000.0,
        },
    ]
    mock_api.get_orderbooks.return_value = [
        {
            "market": "KRW-DOGE",
            "orderbook_units": [
                {"ask_price": 200.1, "bid_price": 200.0, "bid_size": 200_000.0},
            ],
        }
    ]

    screener = MarketScreener(
        bithumb_api=mock_api,
        min_trade_value_krw=1_000_000_000.0,
        min_change_rate=0.005,  # 평상시 +0.5%
        max_change_rate=0.25,
    )

    # 1. 09:10 KST 완충 세션: DOGE(+0.3%)가 통과해야 함
    import market_screener
    monkeypatch.setattr(market_screener, "is_reset_grace_period", lambda ex, now=None: True)
    candidates = screener.scan_markets(top_count=2, btc_regime="NORMAL")
    markets = [c["market"] for c in candidates]
    assert "KRW-DOGE" in markets

    # 2. 14:00 KST 일반 세션: DOGE(+0.3%)가 min_change_rate(0.5%) 미달로 탈락해야 함
    monkeypatch.setattr(market_screener, "is_reset_grace_period", lambda ex, now=None: False)
    candidates_normal = screener.scan_markets(top_count=2, btc_regime="NORMAL")
    assert not any(c["market"] == "KRW-DOGE" and c.get("candidate_type") == "CONFIRMED" for c in candidates_normal)


def test_screener_grace_period_anti_pump(monkeypatch):
    """완충 세션 중 리셋 직후 10% 이상 급등한 펌핑 종목은 momentum_phase가 EXTENDED로 분류되는지 검증"""
    mock_api = MagicMock()
    mock_api.key = "upbit"
    mock_api.get_all_markets.return_value = [
        {"market": "KRW-BTC"},
        {"market": "KRW-DOGE"},
    ]
    mock_api.get_tickers.return_value = [
        {
            "market": "KRW-BTC",
            "trade_price": 80_000_000.0,
            "signed_change_rate": 0.001,
            "acc_trade_price_24h": 50_000_000_000.0,
        },
        {
            "market": "KRW-DOGE",
            "trade_price": 200.0,
            "signed_change_rate": 0.12,  # +12% 개장 펌핑
            "acc_trade_price_24h": 5_000_000_000.0,
        },
    ]
    mock_api.get_orderbooks.return_value = [
        {
            "market": "KRW-DOGE",
            "orderbook_units": [
                {"ask_price": 200.1, "bid_price": 200.0, "bid_size": 200_000.0},
            ],
        }
    ]

    screener = MarketScreener(
        bithumb_api=mock_api,
        min_trade_value_krw=1_000_000_000.0,
        min_change_rate=0.005,
        max_change_rate=0.25,
    )

    import market_screener
    monkeypatch.setattr(market_screener, "is_reset_grace_period", lambda ex, now=None: True)
    candidates = screener.scan_markets(top_count=2, btc_regime="NORMAL")
    doge_cand = next((c for c in candidates if c["market"] == "KRW-DOGE"), None)
    assert doge_cand is not None
    assert doge_cand.get("momentum_phase") == "EXTENDED"


def test_gemini_analyzer_reset_labeling():
    """GeminiAnalyzer가 거래소 스코프에 따라 프롬프트에 올바른 리셋 시점 라벨을 주입하는지 검증"""
    from gemini_analyzer import GeminiAnalyzer

    # 1. 업비트 스코프
    mock_upbit_provider = MagicMock()
    mock_upbit_provider.exchange = "upbit"
    mock_upbit_provider.models_for.return_value = ["gemini-3.5-flash-lite"]

    analyzer_upbit = GeminiAnalyzer(provider=mock_upbit_provider)
    analyzer_upbit.api_key = "test-key"

    candidates = [
        {
            "market": "KRW-DOGE",
            "trade_price": 200.0,
            "change_rate": 0.03,
            "acc_trade_price_24h": 5_000_000_000.0,
            "relative_strength": 0.02,
            "candidate_type": "CONFIRMED",
        },
        {
            "market": "KRW-ADA",
            "trade_price": 500.0,
            "change_rate": 0.02,
            "acc_trade_price_24h": 3_000_000_000.0,
            "relative_strength": 0.01,
            "candidate_type": "CONFIRMED",
        },
    ]

    captured_prompts = []

    def mock_complete_json(prompt, *args, **kwargs):
        captured_prompts.append(prompt)
        res = MagicMock()
        res.value = {"rankings": [{"market": "KRW-DOGE", "tier": "TIER_1", "score": 85, "reason": "테스트"}]}
        return res

    mock_upbit_provider.complete_json.side_effect = mock_complete_json
    analyzer_upbit.rank_candidate_markets(candidates, btc_regime="NORMAL", btc_change_rate=0.01)

    assert len(captured_prompts) == 1
    prompt_text = captured_prompts[0]
    assert "09:00 KST(업비트 기준)" in prompt_text
    assert "BTC 24h 변동" not in prompt_text
    assert "BTC 당일 변동" in prompt_text


def test_bot_controller_telegram_messages_reset_labeling():
    """BotController의 텔레그램 메시지(/status, /trades)에 자정 리셋 문구가 포함되는지 검증"""
    from bot_controller import BotController

    mock_exchange = MagicMock()
    mock_exchange.get_balances.return_value = {"KRW": {"balance": 1_000_000.0}}

    mock_risk = MagicMock()
    mock_risk.daily_start_equity = 1_000_000.0
    mock_risk.realized_pnl_krw = 20_000.0
    mock_risk.total_trades_today = 2

    mock_trade_mem = MagicMock()
    mock_trade_mem.get_recent_trades.return_value = [
        {"market": "KRW-BTC", "reason": "익절", "pnl_krw": 20000.0, "pnl_pct": 2.0, "slippage": 0.0001}
    ]

    ctrl = BotController(
        exchange_factory=lambda: mock_exchange,
        order_executor=MagicMock(),
        order_journal=MagicMock(),
        risk_manager=mock_risk,
        trailing_tracker=MagicMock(),
        trade_memory=mock_trade_mem,
        telegram=MagicMock(),
        get_is_paused=lambda: False,
        set_is_paused=lambda x: None,
        exchange_name="업비트",
    )

    with patch("bot_controller.get_fear_and_greed_index", return_value={"desc": "중립 50"}):
        status_msg = ctrl.get_status_message()
        assert "자정 리셋" in status_msg
        assert "20,000 KRW" in status_msg

    trades_msg = ctrl.get_trades_summary_message()
    assert "자정 리셋" in trades_msg
    assert "20,000원" in trades_msg
