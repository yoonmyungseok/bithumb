import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from risk_controls import RiskGuard
from strategy_engine import StrategyPolicy


class TestDynamicSlotsPolicy:
    """StrategyPolicy 동적 슬롯 정책 및 헬퍼 메서드 검증"""

    def test_default_values(self):
        assert StrategyPolicy.DYNAMIC_SLOT_SAFETY_MAX_POSITIONS == 8
        assert StrategyPolicy.get_regime_max_exposure("BULL_TREND") == 0.85
        assert StrategyPolicy.get_regime_max_exposure("NORMAL") == 0.55
        assert StrategyPolicy.get_regime_max_exposure("RISK_OFF") == 0.50
        assert StrategyPolicy.get_regime_max_exposure("CRASH") == 0.00
        assert StrategyPolicy.RISK_OFF_MAX_ALT_ALLOC_PCT == 0.12
        assert StrategyPolicy.RISK_OFF_MAX_ALT_BUDGET_KRW == 200000.0

    def test_regime_swing_caps(self):
        assert StrategyPolicy.get_regime_swing_cap("BULL_TREND") >= 3
        assert StrategyPolicy.get_regime_swing_cap("NORMAL") == 1
        assert StrategyPolicy.get_regime_swing_cap("RISK_OFF") == 0
        assert StrategyPolicy.get_regime_swing_cap("CRASH") == 0


class TestDynamicRiskGuard:
    """RiskGuard의 동적 슬롯 유동성 및 레짐별 안전 가드 검증"""

    @pytest.fixture
    def guard(self):
        return RiskGuard(
            min_order_krw=5000.0,
            max_open_positions=3,  # 고정 모드용 기본값 (동적 모드에서는 안전 상한 8개 적용)
            max_position_pct=0.25,
            max_total_exposure_pct=0.90,
            max_order_krw=500_000.0,
            max_swing_positions=1,
            max_new_listing_positions=1,
            dynamic_slots_enabled=True,
            dynamic_safety_max_positions=8,
        )

    def test_bull_trend_multiple_swing_approval(self, guard):
        """BULL_TREND 강세장에서는 스윙 슬롯 1개 제한에 묶이지 않고 2개, 3개까지 승인된다."""
        guard.set_current_regime("BULL_TREND")
        total_equity = 2_000_000.0
        order_krw = 300_000.0  # 15%

        # 1번째 스윙 진입
        ok, msg = guard.validate_buy(
            market="KRW-BTC",
            order_krw=order_krw,
            available_krw=2_000_000.0,
            total_equity=total_equity,
            held_markets=[],
            strategy_mode="SWING",
            held_swing_markets=[],
        )
        assert ok is True

        # 2번째 스윙 진입 (기존엔 1개 초과로 차단되던 상황) -> 동적 모드에선 승인!
        ok, msg = guard.validate_buy(
            market="KRW-ETH",
            order_krw=order_krw,
            available_krw=1_700_000.0,
            total_equity=total_equity,
            held_markets=["KRW-BTC"],
            strategy_mode="SWING",
            held_swing_markets=["KRW-BTC"],
        )
        assert ok is True, f"2번째 스윙이 승인되어야 함: {msg}"

        # 3번째 스윙 진입 -> 여전히 총 노출도(85%) 내이므로 승인!
        ok, msg = guard.validate_buy(
            market="KRW-SOL",
            order_krw=order_krw,
            available_krw=1_400_000.0,
            total_equity=total_equity,
            held_markets=["KRW-BTC", "KRW-ETH"],
            strategy_mode="SWING",
            held_swing_markets=["KRW-BTC", "KRW-ETH"],
        )
        assert ok is True, f"3번째 스윙이 승인되어야 함: {msg}"

    def test_normal_regime_swing_cap_one(self, guard):
        """NORMAL 횡보장에서는 스윙 1개까지만 허용되고 2번째 스윙은 차단되지만 단타는 허용된다."""
        guard.set_current_regime("NORMAL")
        total_equity = 1_000_000.0
        order_krw = 150_000.0

        # 이미 스윙 1종목 보유 중
        held = ["KRW-BTC"]
        held_swing = ["KRW-BTC"]

        # 추가 스윙 시도 -> 차단
        ok, msg = guard.validate_buy(
            market="KRW-ETH",
            order_krw=order_krw,
            available_krw=850_000.0,
            total_equity=total_equity,
            held_markets=held,
            strategy_mode="SWING",
            held_swing_markets=held_swing,
        )
        assert ok is False
        assert "스윙" in msg and "한도" in msg

        # 같은 상황에서 단타 시도 -> 정상 승인
        ok, msg = guard.validate_buy(
            market="KRW-XRP",
            order_krw=order_krw,
            available_krw=850_000.0,
            total_equity=total_equity,
            held_markets=held,
            strategy_mode="SCALP",
            held_swing_markets=held_swing,
        )
        assert ok is True, f"NORMAL에서 단타는 승인되어야 함: {msg}"

    def test_risk_off_blocks_swing_and_limits_exposure(self, guard):
        """RISK_OFF 약세장에서는 스윙이 전면 차단되고 총 노출도 50% 초과 시 단타도 차단된다."""
        guard.set_current_regime("RISK_OFF")
        total_equity = 1_000_000.0
        order_krw = 200_000.0  # 20%

        # 스윙 시도 -> 즉시 차단
        ok, msg = guard.validate_buy(
            market="KRW-BTC",
            order_krw=order_krw,
            available_krw=1_000_000.0,
            total_equity=total_equity,
            held_markets=[],
            strategy_mode="SWING",
        )
        assert ok is False
        assert "스윙 신규 진입이 차단" in msg

        # 1번째 단타 시도 (20% <= 50%) -> 승인
        ok, msg = guard.validate_buy(
            market="KRW-XRP",
            order_krw=order_krw,
            available_krw=1_000_000.0,
            total_equity=total_equity,
            held_markets=[],
            strategy_mode="SCALP",
        )
        assert ok is True

        # 2번째 단타 시도 (누적 노출 40% <= 50% 한도) -> 적극 매수 승인!
        ok, msg = guard.validate_buy(
            market="KRW-DOGE",
            order_krw=order_krw,
            available_krw=800_000.0,  # 200,000 이미 매수됨
            total_equity=total_equity,
            held_markets=["KRW-XRP"],
            strategy_mode="SCALP",
        )
        assert ok is True, f"50% 한도 내 2번째 단타는 승인되어야 함: {msg}"

        # 3번째 단타 시도 (누적 노출 60% > 50% 한도) -> 50% 한도 초과 차단
        ok, msg = guard.validate_buy(
            market="KRW-SOL",
            order_krw=order_krw,
            available_krw=600_000.0,  # 400,000 이미 매수됨
            total_equity=total_equity,
            held_markets=["KRW-XRP", "KRW-DOGE"],
            strategy_mode="SCALP",
        )
        assert ok is False
        assert "총 투자 비중 한도 초과" in msg
        assert "RISK_OFF" in msg
        assert "50%" in msg

    def test_crash_regime_blocks_all(self, guard):
        """CRASH 급락장에서는 모든 매수 진입이 차단된다."""
        guard.set_current_regime("CRASH")
        ok, msg = guard.validate_buy(
            market="KRW-BTC",
            order_krw=50_000.0,
            available_krw=1_000_000.0,
            total_equity=1_000_000.0,
            held_markets=[],
            strategy_mode="SCALP",
        )
        assert ok is False
        assert "총 투자 비중 한도 초과" in msg or "0%" in msg

    def test_dynamic_safety_ceiling(self, guard):
        """물리적 안전 상한선(8개) 도달 시 BULL_TREND라도 추가 진입을 차단한다."""
        guard.set_current_regime("BULL_TREND")
        held = [f"KRW-COIN{i}" for i in range(8)]
        ok, msg = guard.validate_buy(
            market="KRW-NEW",
            order_krw=10_000.0,
            available_krw=1_000_000.0,
            total_equity=2_000_000.0,
            held_markets=held,
            strategy_mode="SCALP",
        )
        assert ok is False
        assert "물리적 안전 상한" in msg

    def test_fallback_to_static_when_disabled(self):
        """dynamic_slots_enabled=False일 때는 기존의 엄격한 슬롯 수량 칸막이가 완벽히 적용된다."""
        static_guard = RiskGuard(
            min_order_krw=5000.0,
            max_open_positions=3,
            max_position_pct=0.35,
            max_total_exposure_pct=0.90,
            max_order_krw=500_000.0,
            max_swing_positions=1,
            max_new_listing_positions=1,
            dynamic_slots_enabled=False,
        )
        # 스윙 1개 보유 상태에서 2번째 스윙 시도
        ok, msg = static_guard.validate_buy(
            market="KRW-ETH",
            order_krw=100_000.0,
            available_krw=900_000.0,
            total_equity=1_000_000.0,
            held_markets=["KRW-BTC"],
            strategy_mode="SWING",
            held_swing_markets=["KRW-BTC"],
        )
        assert ok is False
        assert "스윙 전용 보유 종목 수 한도" in msg

    def test_risk_off_env_overrides(self, monkeypatch):
        """환경변수로 RISK_OFF 익스포저 및 알트 비중/예산 오버라이드가 정상 동작함을 검증"""
        monkeypatch.setenv("REGIME_RISK_OFF_MAX_EXPOSURE", "0.45")
        monkeypatch.setenv("RISK_OFF_MAX_ALT_ALLOC_PCT", "0.15")
        monkeypatch.setenv("RISK_OFF_MAX_ALT_BUDGET_KRW", "250000")

        assert StrategyPolicy.get_regime_max_exposure("RISK_OFF") == 0.45
        assert StrategyPolicy.get_risk_off_max_alt_alloc_pct() == 0.15
        assert StrategyPolicy.get_risk_off_max_alt_budget_krw() == 250000.0
