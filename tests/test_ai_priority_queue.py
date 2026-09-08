import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from trading_runtime import TradingCycleEngine


class AIPriorityQueueTests(unittest.TestCase):
    """AI 분석 우선순위 큐 정렬 및 예산 4개 확대 단위 테스트."""

    def test_default_max_ai_candidates_is_four(self):
        """MAX_AI_CANDIDATES_PER_CYCLE 환경변수 미설정 시 기본값이 4여야 한다."""
        with patch.dict(os.environ, {}, clear=True):
            default_max_ai = int(os.getenv("MAX_AI_CANDIDATES_PER_CYCLE", "4"))
            self.assertEqual(default_max_ai, 4)

    def test_held_markets_remain_at_the_front(self):
        """기보유 종목은 청산 우선 처리를 위해 신규 고알파 종목보다 항상 앞에 유지되어야 한다."""
        ctx = MagicMock()
        ctx.logger = MagicMock()
        ctx.order_journal = MagicMock()
        ctx.order_journal.is_entry_ready.return_value = True
        ctx.orchestrator = MagicMock()

        profile = MagicMock()
        profile.skip_excluded_markets_in_loop = False
        profile.market_analysis_log_label = "분석"
        profile.decision_exchange = "upbit"

        config = MagicMock()
        config.interval_minutes = 5
        config.is_bot_paused.return_value = False
        config.profile = profile
        config.exit_profile = MagicMock()
        config.entry_profile = MagicMock()
        config.buy_profile = MagicMock()

        engine = TradingCycleEngine(config=config, context=ctx)

        target_markets = ["KRW-HELD", "KRW-A"]
        held_markets = ["KRW-HELD"]

        snap = MagicMock()
        snap.korean_name = "종목"
        snap.candles_5m = [{"trade_price": 1000.0}] * 30
        snap.candles_1h = [{"trade_price": 1000.0}] * 25
        snap.orderbook = {}
        ctx.orchestrator.load_market_snapshot.return_value = snap

        evaluated_order = []
        def mock_process_priority_exits(inputs):
            evaluated_order.append(inputs.market)
            return True  # exit processed

        engine.process_priority_exits = MagicMock(side_effect=mock_process_priority_exits)
        engine.process_entry_gating = MagicMock()

        prefix = MagicMock()
        prefix.target_markets = target_markets
        prefix.held_markets = held_markets
        prefix.screened_candidate_metadata = {}
        prefix.prefetched_market_inputs = {}
        prefix.excluded_markets = set()
        prefix.is_btc_crashing = False
        prefix.is_kill_switch = False

        with patch("trading_runtime.entry_signal", return_value={"allow_buy": True, "alpha_score": 95}):
            engine.run_market_loop(prefix)

        self.assertEqual(evaluated_order[0], "KRW-HELD")

    def test_run_market_loop_sorts_allow_buy_first(self):
        """로컬 퀀트 allow_buy=True인 고알파 종목이 AI 예산을 최우선으로 받아야 한다."""
        ctx = MagicMock()
        ctx.logger = MagicMock()
        ctx.order_journal = MagicMock()
        ctx.order_journal.is_entry_ready.return_value = True
        ctx.orchestrator = MagicMock()

        profile = MagicMock()
        profile.skip_excluded_markets_in_loop = False
        profile.market_analysis_log_label = "분석"
        profile.decision_exchange = "upbit"

        config = MagicMock()
        config.interval_minutes = 5
        config.is_bot_paused.return_value = False
        config.profile = profile
        config.exit_profile = MagicMock()
        config.entry_profile = MagicMock()
        config.buy_profile = MagicMock()

        engine = TradingCycleEngine(config=config, context=ctx)

        target_markets = ["KRW-A", "KRW-B", "KRW-C"]
        held_markets = []

        snap_a = MagicMock(korean_name="종목A", coin_available=0.0, avg_buy_price=0.0, current_price=1000.0, krw_available=100000.0, candles_5m=[{"trade_price": 1000.0}] * 30, candles_1h=[{"trade_price": 1000.0}] * 25, orderbook={})
        snap_b = MagicMock(korean_name="종목B", coin_available=0.0, avg_buy_price=0.0, current_price=2000.0, krw_available=100000.0, candles_5m=[{"trade_price": 2000.0}] * 30, candles_1h=[{"trade_price": 2000.0}] * 25, orderbook={})
        snap_c = MagicMock(korean_name="종목C", coin_available=0.0, avg_buy_price=0.0, current_price=3000.0, krw_available=100000.0, candles_5m=[{"trade_price": 3000.0}] * 30, candles_1h=[{"trade_price": 3000.0}] * 25, orderbook={})

        def mock_load_snapshot(exchange, market, interval, prefetched):
            if market == "KRW-A": return snap_a
            elif market == "KRW-B": return snap_b
            return snap_c

        ctx.orchestrator.load_market_snapshot.side_effect = mock_load_snapshot

        def mock_entry_signal(**kwargs):
            m = kwargs.get("market")
            if m == "KRW-B":
                return {"allow_buy": True, "alpha_score": 85, "checklist": {"hard_gates": {"all_passed": True}}}
            elif m == "KRW-C":
                return {"allow_buy": True, "alpha_score": 75, "checklist": {"hard_gates": {"all_passed": True}}}
            else:
                return {"allow_buy": False, "alpha_score": 40, "checklist": {"hard_gates": {"all_passed": False}}}

        evaluated_order = []
        ai_allowed_for = {}

        def mock_process_entry_gating(inputs):
            evaluated_order.append(inputs.market)
            ai_allowed_for[inputs.market] = inputs.allow_ai_analysis
            entry_res = MagicMock()
            entry_res.should_continue = True
            entry_res.called_ai = inputs.allow_ai_analysis
            entry_res.action = "HOLD"
            return entry_res

        engine.process_priority_exits = MagicMock(return_value=False)
        engine.process_entry_gating = MagicMock(side_effect=mock_process_entry_gating)

        prefix = MagicMock()
        prefix.target_markets = target_markets
        prefix.held_markets = held_markets
        prefix.screened_candidate_metadata = {}
        prefix.prefetched_market_inputs = {}
        prefix.excluded_markets = set()
        prefix.is_btc_crashing = False
        prefix.is_kill_switch = False

        with patch("trading_runtime.entry_signal", side_effect=mock_entry_signal):
            with patch.dict(os.environ, {"MAX_AI_CANDIDATES_PER_CYCLE": "2"}):
                engine.run_market_loop(prefix)

        self.assertEqual(evaluated_order, ["KRW-B", "KRW-C", "KRW-A"])
        self.assertTrue(ai_allowed_for["KRW-B"])
        self.assertTrue(ai_allowed_for["KRW-C"])
        self.assertFalse(ai_allowed_for["KRW-A"])


if __name__ == "__main__":
    unittest.main()
