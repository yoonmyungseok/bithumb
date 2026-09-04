"""결함 수정 및 거래 안전 개선 단위 테스트."""

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from gemini_analyzer import GeminiAnalyzer
from order_safety import CooldownManager, OrderJournal, SafeOrderExecutor
from realtime_engine import RealtimeRiskEngine
from risk_manager import DailyRiskManager, TrailingStopTracker
from strategy_engine import entry_signal
from telegram_alert import TelegramAlert
from trade_memory import TradeMemoryManager


class DefectFixesTestCase(unittest.TestCase):
    """결함 수정 및 상태 정리 검증 테스트 케이스."""

    def test_checklist_details_momentum_breakout_unique(self):
        """entry_signal의 checklist_details에 momentum_breakout 키가 온전하게 유지되는지 검증."""
        candles = [
            {
                "opening_price": 1000.0,
                "high_price": 1050.0,
                "low_price": 990.0,
                "trade_price": 1040.0,
                "candle_acc_trade_volume": 1000.0,
            }
            for _ in range(60)
        ]
        res = entry_signal(candles, market="KRW-TEST", btc_regime="NORMAL")
        self.assertIn("checklist_details", res)
        cd = res["checklist_details"]
        self.assertIn("momentum_breakout", cd)
        mb = cd["momentum_breakout"]
        # 987행의 상세 키(mtf_pass, mtf_detail)가 보존되어야 함
        self.assertIn("pass", mb)
        self.assertIn("detail", mb)
        self.assertIn("mtf_pass", mb)
        self.assertIn("mtf_detail", mb)

    def test_gemini_local_fallback_factor_passing(self):
        """GeminiAnalyzer의 로컬 퀀트 엔진 폴백 시 vwap_info와 macd_acc가 점수에 정상 반영되는지 검증."""
        analyzer = GeminiAnalyzer(api_key="test_dummy_key")

        # 1) vwap_info가 있고 is_above인 경우
        res_with_factors = analyzer._run_local_quant_engine(
            current_price=1000.0,
            mtf_1h={"trend": "BULLISH"},
            disparity_ma20=100.0,
            rsi_val=50.0,
            bb={"pct_b": 0.5},
            vol_info={"is_spike": False, "vol_ratio": 1.0},
            candle_pattern="양봉",
            trade_strength={"trade_power_pct": 120.0},
            ob_info={"spread_pct": 0.001, "bid_ask_ratio": 1.2},
            dynamic_tp=1050.0,
            dynamic_sl=980.0,
            is_holding=False,
            pnl_pct=0.0,
            vwap_info={"is_above": True, "disparity_pct": 1.0},
            macd_acc={"is_accelerating": True, "momentum_state": "확장"},
        )

        # 2) vwap_info와 macd_acc가 누락(None)된 경우
        res_without_factors = analyzer._run_local_quant_engine(
            current_price=1000.0,
            mtf_1h={"trend": "BULLISH"},
            disparity_ma20=100.0,
            rsi_val=50.0,
            bb={"pct_b": 0.5},
            vol_info={"is_spike": False, "vol_ratio": 1.0},
            candle_pattern="양봉",
            trade_strength={"trade_power_pct": 120.0},
            ob_info={"spread_pct": 0.001, "bid_ask_ratio": 1.2},
            dynamic_tp=1050.0,
            dynamic_sl=980.0,
            is_holding=False,
            pnl_pct=0.0,
            vwap_info=None,
            macd_acc=None,
        )

        # 정상 전달 시 점수가 더 높아야 함 (+15 +15 vs +5 +5 = 20점 차이)
        self.assertGreater(res_with_factors["alpha_score"], res_without_factors["alpha_score"])
        self.assertEqual(res_with_factors["alpha_score"] - res_without_factors["alpha_score"], 20)

    def test_trailing_reconcile_cleans_runner_and_dynamic_targets(self):
        """TrailingStopTracker.reconcile_markets가 runner_markets와 dynamic_targets도 stale 정리하는지 검증."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = TrailingStopTracker(data_dir=tmpdir)
            # 활성 포지션 등록
            tracker.update_dynamic_exit("KRW-BTC", target_price=100000.0, stop_loss=90000.0, runner_mode=True)
            tracker.update_dynamic_exit("KRW-OLD", target_price=5000.0, stop_loss=4500.0, runner_mode=True)

            self.assertTrue(tracker.is_runner_mode("KRW-BTC"))
            self.assertTrue(tracker.is_runner_mode("KRW-OLD"))
            self.assertEqual(tracker.get_dynamic_target_price("KRW-OLD"), 5000.0)

            # KRW-OLD는 매도되어 현재 보유 종목에 없음
            held_markets = ["KRW-BTC"]
            cleaned_count = tracker.reconcile_markets(held_markets)

            self.assertGreaterEqual(cleaned_count, 1)
            self.assertTrue(tracker.is_runner_mode("KRW-BTC"))
            self.assertFalse(tracker.is_runner_mode("KRW-OLD"))
            self.assertIsNone(tracker.get_dynamic_target_price("KRW-OLD"))
            self.assertIsNone(tracker.get_dynamic_stop_loss("KRW-OLD"))

    def test_realtime_stop_loss_no_premature_cooldown_before_fill(self):
        """실시간 손절 주문 접수 직후(체결 전)에는 쿨다운이 걸리지 않는지 검증."""
        with tempfile.TemporaryDirectory() as tmpdir:
            journal_path = os.path.join(tmpdir, "order_journal.json")
            db_path = os.path.join(tmpdir, "trading.db")

            mock_exchange = MagicMock()
            mock_exchange.get_korean_name.return_value = "비트코인"
            mock_exchange.get_balances.return_value = {
                "BTC": {"balance": 0.1, "locked": 0.0, "avg_buy_price": 100000000.0}
            }
            # 주문은 접수(ACK)되었으나 아직 체결되지 않음 (raw_state='wait', executed_volume=0)
            mock_exchange.create_order.return_value = {"order_id": "ord-test-1", "status": "wait"}
            mock_exchange.get_order.return_value = {
                "order_id": "ord-test-1",
                "state": "wait",
                "executed_volume": 0.0,
                "remaining_volume": 0.1,
            }

            journal = OrderJournal(journal_path)
            executor = SafeOrderExecutor(journal)
            cooldown = CooldownManager(data_dir=tmpdir)
            risk = DailyRiskManager(data_dir=tmpdir)
            trade_mem = TradeMemoryManager(data_dir=tmpdir)
            trailing = TrailingStopTracker(data_dir=tmpdir)
            telegram = MagicMock(spec=TelegramAlert)

            engine = RealtimeRiskEngine(
                exchange_factory=lambda: mock_exchange,
                order_executor=executor,
                order_journal=journal,
                risk_manager=risk,
                cooldown_manager=cooldown,
                trade_memory=trade_mem,
                trailing_tracker=trailing,
                telegram=telegram,
                latest_strategies={"KRW-BTC": {"STOP_LOSS": 98000000.0}},
            )

            # 손절선 터치 틱 전송 (2회 연속으로 휩소 필터 통과)
            engine.on_price_tick("KRW-BTC", 95000000.0)
            engine.on_price_tick("KRW-BTC", 95000000.0)

            # 주문은 생성되었으나 미체결 상태이므로 쿨다운 매니저에 등록되지 않아야 함 (거래 안전 규칙 P0-1)
            self.assertTrue(cooldown.check_reentry_allowed("KRW-BTC", 95000000.0)[0])


if __name__ == "__main__":
    unittest.main()
