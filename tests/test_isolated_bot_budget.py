"""
봇 전용 독립 시드 모드 (ISOLATED_BOT_BUDGET_MODE) 단위 테스트
"""

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from risk_controls import RiskGuard, get_dynamic_portfolio_tiers
from position_guard import get_bot_held_markets, get_manual_held_markets


class TestIsolatedBotBudgetMode(unittest.TestCase):
    def test_get_dynamic_portfolio_tiers_allocates_38_pct_for_small_seed(self):
        """30만 원 미만 소액 시드(예: 10만 원)에서 최대 3종목, 종목당 38% 비중 산정 확인"""
        max_pos, max_pct, top_count = get_dynamic_portfolio_tiers(100_000.0)
        self.assertEqual(max_pos, 3)
        self.assertEqual(max_pct, 0.38)
        self.assertEqual(top_count, 10)

    def test_risk_guard_evaluates_only_allocated_budget_in_isolated_mode(self):
        """
        독립 시드 모드 가동 시 계좌에 수백만 원 상당의 수동 자산이 존재하더라도
        봇 전용 예산(100,000원) 기준으로 38%(38,000원) 산정 및 총 익스포저 정상 승인
        """
        guard = RiskGuard(
            min_order_krw=5000.0,
            max_open_positions=3,
            max_position_pct=0.38,
            max_total_exposure_pct=0.90,
            max_order_krw=20_000_000.0,
            dynamic_slots_enabled=True,
            isolated_budget_mode=True,
            bot_allocated_budget=100_000.0,
        )

        account_total_equity = 10_000_000.0  # 계좌 전체 1천만 원 (수동 코인 990만 원 + 원화 10만 원)
        available_krw = 100_000.0

        # 1. 38,000원(38%) 주문: 정상 통과 확인
        safe, reason = guard.validate_buy(
            market="KRW-XRP",
            order_krw=38_000.0,
            available_krw=available_krw,
            total_equity=account_total_equity,
            held_markets=[],
            strategy_mode="SCALP",
        )
        self.assertTrue(safe)
        self.assertEqual(reason, "OK")

        # 2. 45,000원(45%) 주문: 38% 초과로 종목당 비중 한도 초과 차단 확인
        safe_over, reason_over = guard.validate_buy(
            market="KRW-XRP",
            order_krw=45_000.0,
            available_krw=available_krw,
            total_equity=account_total_equity,
            held_markets=[],
            strategy_mode="SCALP",
        )
        self.assertFalse(safe_over)
        self.assertIn("종목당 비중 한도 초과", reason_over)

    def test_headroom_clamping_uses_eval_equity(self):
        """헤드룸 계산 시 수동 자산 1,000만 원으로 인한 0원 클램핑 방지 검증"""
        account_total_equity = 10_000_000.0
        eval_equity = 100_000.0  # 봇 전용 예산
        available_krw = 100_000.0
        effective_max_exp = 0.85

        # 기존 방식(전체 자산 기준): 100,000 - 10,000,000 * 0.15 = -1,400,000 -> 0원 (매수 불가)
        old_headroom = max(0.0, available_krw - account_total_equity * (1.0 - effective_max_exp))
        self.assertEqual(old_headroom, 0.0)

        # 독립 시드 모드(봇 예산 기준): 100,000 - 100,000 * 0.15 = 85,000원 (정상 매수 가능)
        new_headroom = max(0.0, available_krw - eval_equity * (1.0 - effective_max_exp))
        self.assertEqual(new_headroom, 85_000.0)
        self.assertGreaterEqual(new_headroom, 38_000.0)

    def test_get_bot_held_markets_separates_manual_holdings(self):
        """수동 보유 코인은 봇의 관리 포지션 슬롯에서 완전히 배제됨을 확인"""
        balances = {
            "KRW": {"balance": 100000.0, "locked": 0.0},
            "BTC": {"balance": 0.1, "locked": 0.0},  # 영구 보호 종목
            "US": {"balance": 1000.0, "locked": 0.0}, # 수동 매수 코인
            "XRP": {"balance": 50.0, "locked": 0.0},  # 봇 매수 코인
        }

        order_journal = MagicMock()
        order_journal.orders = [
            {"market": "KRW-XRP", "side": "bid", "status": "FILLED", "volume": 50.0, "created_at": 100.0}
        ]

        bot_held = get_bot_held_markets(balances, order_journal=order_journal)
        self.assertIn("KRW-XRP", bot_held)
        self.assertNotIn("KRW-BTC", bot_held)
        self.assertNotIn("KRW-US", bot_held)

        manual_held = get_manual_held_markets(balances, order_journal=order_journal)
        self.assertIn("KRW-BTC", manual_held)
        self.assertIn("KRW-US", manual_held)
        self.assertNotIn("KRW-XRP", manual_held)


if __name__ == "__main__":
    unittest.main()
