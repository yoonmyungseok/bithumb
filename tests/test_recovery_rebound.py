"""급락 후 반등 전용 진입과 판단 이력의 안전 경계를 검증한다."""

import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from db_manager import DatabaseManager
from strategy_engine import recovery_rebound_signal


def _base_signal(alpha_score: int = 80) -> dict:
    """반등 신호가 의존하는 기존 정량 결과를 결정론적으로 만든다."""
    passed = {"pass": True}
    return {
        "allow_buy": False,
        "reason": "일반 RISK_OFF MTF 미달",
        "entry_price": 100.0,
        "target_price": 104.0,
        "stop_loss": 98.0,
        "alpha_score": alpha_score,
        "strategy_snapshot": {"entry_btc_regime": "RISK_OFF"},
        "checklist_details": {
            "hard_gates": {
                "btc_regime": passed,
                "rsi_guard": passed,
                "bb_guard": passed,
                "ma_alignment": passed,
                "disparity_guard": passed,
                "shadow_guard": passed,
                "pullback_zone": passed,
                "near_recent_low": passed,
                "rebound_confirmation": passed,
            },
            "factor_breakdown": {"mtf_score": 10},
        },
        "factor_breakdown": {"mtf_score": 10},
    }


class RecoveryReboundTests(unittest.TestCase):
    def setUp(self):
        self.candles = [{"trade_price": 100.0}] * 25
        self.candles_1h = [{"trade_price": 100.0}] * 25

    @patch("strategy_engine.entry_signal")
    def test_recovery_rebound_approves_only_all_confirmed_inputs(self, mocked_entry):
        """일반 MTF 미달이어도 반등 전용의 모든 엄격 조건 충족 시에만 승인한다."""
        mocked_entry.return_value = _base_signal()

        result = recovery_rebound_signal(
            self.candles, self.candles_1h, "RISK_OFF", {}, "KRW-TEST", "upbit",
            relative_strength=0.02, candidate_trade_value=3_000_000_000.0,
        )

        self.assertTrue(result["allow_buy"])
        self.assertEqual(result["policy_mode"], "RECOVERY_REBOUND")
        self.assertTrue(result["recovery_checklist"]["mtf_recovery_pass"])

    @patch("strategy_engine.entry_signal")
    def test_recovery_rebound_rejects_low_relative_strength(self, mocked_entry):
        """BTC 약세장에서 독자 강도가 부족하면 반등처럼 보여도 진입하지 않는다."""
        mocked_entry.return_value = _base_signal()

        result = recovery_rebound_signal(
            self.candles, self.candles_1h, "RISK_OFF", {}, "KRW-TEST", "upbit",
            relative_strength=0.0149, candidate_trade_value=3_000_000_000.0,
        )

        self.assertFalse(result["allow_buy"])
        self.assertFalse(result["recovery_checklist"]["rs_passed"])

    @patch("strategy_engine.entry_signal")
    def test_recovery_rebound_never_bypasses_crash(self, mocked_entry):
        """CRASH 레짐은 반등 전용 정책으로 우회할 수 없다."""
        mocked_entry.return_value = _base_signal()

        result = recovery_rebound_signal(
            self.candles, self.candles_1h, "CRASH", {}, "KRW-TEST", "upbit",
            relative_strength=0.03, candidate_trade_value=3_000_000_000.0,
        )

        self.assertFalse(result["allow_buy"])
        self.assertIn("대상 아님", result["reason"])


class StrategyDecisionAuditTests(unittest.TestCase):
    def setUp(self):
        # 윈도우 시스템 임시 폴더 ACL과 분리해 기존 테스트 스크래치 경로에 DB를 생성한다.
        scratch_dir = os.path.join(os.getcwd(), "data", "test_scratch")
        os.makedirs(scratch_dir, exist_ok=True)
        self.db_path = os.path.join(scratch_dir, f"recovery_decisions_{time.time_ns()}.db")
        self.db = DatabaseManager(self.db_path)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.db_path + suffix)
            except OSError:
                pass

    def test_recovery_entry_is_exchange_scoped_and_window_limited(self):
        """반등 주문 제한은 업비트 내부의 같은 회복 구간에만 적용한다."""
        now = time.time()
        self.db.record_strategy_decision(
            exchange="upbit", cycle_id="cycle-1", market="KRW-TEST", action="BUY_SUBMITTED",
            policy_mode="RECOVERY_REBOUND", block_reasons=[], payload={}, decision_ts=now,
        )

        self.assertTrue(self.db.has_recovery_entry_since("upbit", now - 1))
        self.assertFalse(self.db.has_recovery_entry_since("bithumb", now - 1))
        self.assertFalse(self.db.has_recovery_entry_since("upbit", now + 1))


if __name__ == "__main__":
    unittest.main()
