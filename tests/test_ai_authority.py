import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from trading_runtime import (
    TradingCycleEngine,
    MarketEntryInputs,
    MarketExitInputs,
    StrategyPolicy,
    ExchangeEntryProfile,
    ExchangeExitProfile,
)


class AIAuthorityTests(unittest.TestCase):
    """
    AI 권한 확대(AI Direct Entry 및 AI 자율 포지션 홀딩/타임스탑 유예) 단위 테스트
    """

    def setUp(self):
        self.mock_exchange = MagicMock()
        self.mock_exchange.min_order_krw = 5000.0
        self.mock_analyzer = MagicMock()
        self.mock_ctx = MagicMock()
        self.mock_ctx.cooldown_manager.check_reentry_allowed.return_value = (True, "재진입 허용")
        self.mock_ctx.order_journal.has_active_exit_order.return_value = False
        self.mock_ctx.trailing_tracker.check_position.return_value = ("NONE", 1000.0, 0.0, 0.0, 0.0)
        self.runtime = TradingCycleEngine(
            config=MagicMock(interval_minutes=5, dynamic_alloc_enabled=False, min_order_krw=5000.0),
            context=self.mock_ctx,
        )




    def test_ai_direct_entry_when_local_quant_holds(self):
        """
        로컬 퀀트 룰이 관망(allow_buy=False) 상태이더라도,
        기본 안전망 통과 및 AI가 심층 분석 후 BUY를 승인하면 AI Direct Entry로 매수 승인되는지 검증
        """
        # Given: 로컬 룰은 관망 (allow_buy=False)
        self.mock_analyzer.analyze.return_value = {
            "status": "ACTIVE",
            "action": "BUY",
            "entry_price": 1000.0,
            "target_price": 1050.0,
            "stop_loss": 970.0,
            "alloc_pct": 0.35,
            "reason": "[gemini-3.5-flash-lite] 강력한 수급 및 추세 전환 확인",
            "alpha_score": 75,
        }

        # Mock candles & exchange
        self.mock_exchange.get_candles.return_value = [
            {"trade_price": 1000.0, "candle_acc_trade_volume": 100.0}
        ]

        inputs = MarketEntryInputs(
            exchange=self.mock_exchange,
            market="KRW-TEST",
            korean_name="테스트",
            candidate_type="HOT_CANDIDATE",
            candidate_metadata={"candidate_type": "HOT_CANDIDATE", "acc_trade_price_24h": 5000000000.0},
            analyzer=self.mock_analyzer,
            coin_available=0.0,
            avg_buy_price=0.0,
            current_price=1000.0,
            coin_value=0.0,
            krw_available=100000.0,
            candles_5m=[{"trade_price": 1000.0} for _ in range(25)],
            candles_1h=[{"trade_price": 1000.0} for _ in range(20)],
            orderbook={"orderbook_units": []},
            btc_regime="BULL_TREND",
            btc_status_msg="정상",
            is_btc_crashing=False,
            is_cooldown=False,
            is_extreme_fear=False,
            is_bot_paused=False,
            is_kill_switch=False,
            is_entry_ready=True,
            dyn_max_pos_pct=0.35,
            now_str="2026-09-04 17:00:00",
            audit_decision=MagicMock(),
        )

        with patch("trading_runtime.select_completed_candles", side_effect=lambda c, **kw: c), \
             patch("trading_runtime.entry_signal") as mock_entry_rules:
            # 로컬 룰은 차단
            mock_entry_rules.return_value = {
                "allow_buy": False,
                "reason": "하드게이트 차단, 최근고점대비 관망",
                "alpha_score": 50,
                "entry_price": 1000.0,
                "target_price": 1035.0,
                "stop_loss": 980.0,
            }

            self.mock_ctx.trade_memory.is_reentry_allowed.return_value = (True, "재진입 허용")
            self.mock_ctx.ws_client.get_health_status.return_value = {"is_healthy": True}
            self.mock_ctx.decision_db.has_recovery_entry_since.return_value = False

            # When
            res = self.runtime.process_entry_gating(inputs)

            # Then: AI의 BUY가 채택되어 action이 BUY가 되어야 함
            self.assertEqual(res.action, "BUY")
            self.assertIn("[AI 단독 자율 승인]", res.reason)
            self.assertEqual(res.target_price, 1050.0)
            self.assertEqual(res.stop_loss, 970.0)


    def test_time_stop_bypassed_when_ai_holds_position(self):
        """
        포지션 보유 중 15분 경과 및 추세 이평선 꺾임이 있더라도,
        AI가 evaluate_holding_position에서 'HOLD'를 판정한 경우 기계적 타임스탑 조기 손절이 유예되는지 검증
        """
        # Given: 보유시간 1800초 (30분), 현재 손익률 -0.8% (be_threshold_pct 미달)
        now_ts = time.time()
        entry_ts = now_ts - 1800.0

        self.mock_analyzer.evaluate_holding_position.return_value = {
            "action": "HOLD",
            "reason": "건강한 숨고르기 눌림목 지지선 유지 중",
            "adjusted_target_price": None,
            "adjusted_stop_loss": None,
            "confidence": 80,
        }

        # 5분봉 캔들 모의 (추세 이평선 꺾임 상태 모의)
        mock_candles = [{"trade_price": 990.0, "candle_acc_trade_volume": 10.0} for _ in range(25)]

        inputs = MarketExitInputs(
            exchange=self.mock_exchange,
            market="KRW-TEST",
            korean_name="테스트",
            coin_available=10.0,
            avg_buy_price=1000.0,
            current_price=992.0,
            coin_value=9920.0,
            candles_5m=mock_candles,
            candles_1h=mock_candles,
            orderbook={},
            btc_regime="NORMAL",
            now_str="2026-09-04 17:00:00",
            analyzer=self.mock_analyzer,
        )

        self.mock_ctx.risk_manager.entry_times = {"KRW-TEST": entry_ts}
        self.mock_ctx.trailing_tracker.get_entry_time.return_value = entry_ts
        self.mock_ctx.trailing_tracker.acquire_exit_lock.return_value = True
        self.mock_ctx.trailing_tracker.get_dynamic_stop_loss.return_value = None
        self.mock_ctx.order_journal.has_active_exit_order.return_value = False


        with patch("trading_runtime.calculate_vwap") as mock_vwap:
            mock_vwap.return_value = {"is_above": False, "vwap": 1005.0}

            # When
            exited = self.runtime.process_priority_exits(inputs)

            # Then: AI의 HOLD 진단으로 인해 기계적 타임스탑 손절이 유예되어 exited가 False여야 함
            self.assertFalse(exited)

    def test_emergency_exit_when_ai_diagnoses_dumping(self):
        """
        포지션 보유 중 세력 덤핑 등으로 AI가 EMERGENCY_EXIT를 판정하면 즉시 전량 시장가 탈출하는지 검증
        """
        now_ts = time.time()
        entry_ts = now_ts - 700.0

        self.mock_analyzer.evaluate_holding_position.return_value = {
            "action": "EMERGENCY_EXIT",
            "reason": "대량 매도벽 출회 및 세력 덤핑 감지",
            "confidence": 95,
        }

        mock_candles = [{"trade_price": 970.0} for _ in range(10)]

        inputs = MarketExitInputs(
            exchange=self.mock_exchange,
            market="KRW-TEST",
            korean_name="테스트",
            coin_available=10.0,
            avg_buy_price=1000.0,
            current_price=970.0,
            coin_value=9700.0,
            candles_5m=mock_candles,
            candles_1h=mock_candles,
            orderbook={},
            btc_regime="NORMAL",
            now_str="2026-09-04 17:00:00",
            analyzer=self.mock_analyzer,
        )

        self.mock_ctx.risk_manager.entry_times = {"KRW-TEST": entry_ts}
        self.mock_ctx.trailing_tracker.get_entry_time.return_value = entry_ts
        self.mock_ctx.trailing_tracker.acquire_exit_lock.return_value = True
        self.mock_ctx.trailing_tracker.get_dynamic_stop_loss.return_value = None

        # When

        exited = self.runtime.process_priority_exits(inputs)

        # Then
        self.assertTrue(exited)
        self.mock_ctx.order_executor.submit.assert_called_once()
        call_kwargs = self.mock_ctx.order_executor.submit.call_args[1]
        self.assertEqual(call_kwargs["exit_reason"], "AI_EMERGENCY_EXIT")




if __name__ == "__main__":
    unittest.main()
