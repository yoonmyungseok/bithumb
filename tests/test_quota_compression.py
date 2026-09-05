"""
Unit tests for Free-Tier (500 RPD) Quota Compression & Budget Guard
"""

import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import shutil
import tempfile

from gemini_telemetry import GeminiTelemetry
from gemini_analyzer import GeminiAnalyzer
from trading_runtime import MarketEntryInputs, EntryGatingResult, TradingCycleEngine


class TestQuotaCompression(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        GeminiTelemetry.configure(data_dir=self.temp_dir)
        GeminiTelemetry.reset(persist=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        # 기본 디렉토리 복원하되 실데이터 덮어쓰지 않음
        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        data_dir = os.path.join(project_root, "data")
        GeminiTelemetry.configure(data_dir=data_dir)

    def test_gemini_telemetry_quota_budget_levels(self):
        """GeminiTelemetry의 쿼터 단계별 플래그(tight 700, critical 900, exhausted 980) 검증 (1,000 RPD 기준)"""
        # 1. 초기 상태: 정상
        budget = GeminiTelemetry.get_daily_quota_budget()
        self.assertFalse(budget["is_tight"])
        self.assertFalse(budget["is_critical"])
        self.assertFalse(budget["is_exhausted"])
        self.assertTrue(GeminiTelemetry.can_make_api_call(for_emergency_exit=False))
        self.assertTrue(GeminiTelemetry.can_make_api_call(for_emergency_exit=True))

        # 2. 700회 도달 (70%): is_tight 활성화
        GeminiTelemetry._api_calls = 700
        budget = GeminiTelemetry.get_daily_quota_budget()
        self.assertTrue(budget["is_tight"])
        self.assertFalse(budget["is_critical"])
        self.assertTrue(GeminiTelemetry.can_make_api_call(for_emergency_exit=False))

        # 3. 900회 도달 (90%): is_critical 활성화 (신규 매수 AI 차단, 긴급 탈출은 허용)
        GeminiTelemetry._api_calls = 900
        budget = GeminiTelemetry.get_daily_quota_budget()
        self.assertTrue(budget["is_critical"])
        self.assertFalse(GeminiTelemetry.can_make_api_call(for_emergency_exit=False))
        self.assertTrue(GeminiTelemetry.can_make_api_call(for_emergency_exit=True))

        # 4. 980회 도달 (98%): is_exhausted 활성화 (긴급 탈출도 차단)
        GeminiTelemetry._api_calls = 980
        budget = GeminiTelemetry.get_daily_quota_budget()
        self.assertTrue(budget["is_exhausted"])
        self.assertFalse(GeminiTelemetry.can_make_api_call(for_emergency_exit=False))
        self.assertFalse(GeminiTelemetry.can_make_api_call(for_emergency_exit=True))

    def test_analyzer_respects_daily_quota_guard(self):
        """GeminiAnalyzer.analyze가 900회 도달 시 API 호출을 즉시 건너뛰고 로컬 엔진으로 폴백하는지 검증"""
        analyzer = GeminiAnalyzer(api_key="fake-key")
        GeminiTelemetry._api_calls = 900  # 900회 한도 도달

        candles_5m = [
            {
                "candle_date_time_utc": f"2026-09-04T12:{i:02d}:00Z",
                "opening_price": 100.0, "high_price": 102.0, "low_price": 99.0,
                "trade_price": 101.0, "candle_acc_trade_volume": 10.0,
            }
            for i in range(30)
        ]

        with patch.object(analyzer, "_run_local_quant_engine", return_value={"action": "HOLD", "reason": "local fallback"}) as mock_local:
            res = analyzer.analyze(
                market="KRW-TEST",
                current_price=101.0,
                candles=candles_5m,
                krw_balance=1000000.0,
                coin_balance=0.0,
                avg_buy_price=0.0,
            )
            self.assertTrue(mock_local.called)
            self.assertEqual(res["action"], "HOLD")

    def test_holding_evaluation_adaptive_caching(self):
        """횡보 시 900초(15분) 캐시 적용 및 급변 시 60초 단기 캐시 동작 검증"""
        analyzer = GeminiAnalyzer(api_key="fake-key")
        market = "KRW-BTC"
        candles = [
            {
                "candle_date_time_utc": f"2026-09-04T12:{i:02d}:00Z",
                "opening_price": 100.0, "high_price": 101.0, "low_price": 99.0,
                "trade_price": 100.0, "candle_acc_trade_volume": 10.0,
            }
            for i in range(25)
        ]
        orderbook = {"orderbook_units": [{"ask_price": 100.5, "bid_price": 100.0, "ask_size": 1.0, "bid_size": 1.0}]}

        # 1. 횡보 상태 (손익률 +0.2%): 캐시 TTL 900초 적용
        analyzer._holding_eval_cache[f"HOLDING:{market}"] = {
            "cached_at": time.time() - 300.0,  # 5분 전 캐시
            "result": {"action": "HOLD", "reason": "횡보 캐시 유지"}
        }
        res1 = analyzer.evaluate_holding_position(
            market=market,
            current_price=100.2,
            avg_buy_price=100.0,
            candles=candles,
            orderbook=orderbook,
        )
        self.assertEqual(res1["reason"], "횡보 캐시 유지")  # 5분 지났어도 15분 이내이므로 캐시 재사용!

        # 2. 급락 상태 (손익률 -2.0%): 60초 초과 시 캐시 만료되어 재평가 시도
        with patch.object(analyzer, "_call_gemini_json", return_value={"ACTION": "EMERGENCY_EXIT", "REASON": "덤핑 감지"}):
            analyzer._holding_eval_cache[f"HOLDING:{market}"] = {
                "cached_at": time.time() - 100.0,  # 100초 전 캐시
                "result": {"action": "HOLD", "reason": "이전 캐시"}
            }
            res2 = analyzer.evaluate_holding_position(
                market=market,
                current_price=98.0,  # -2% 급락
                avg_buy_price=100.0,
                candles=candles,
                orderbook=orderbook,
            )
            self.assertEqual(res2["action"], "EMERGENCY_EXIT")  # 캐시 무효화되고 즉시 재진단

    def test_entry_gating_skips_ai_when_allow_ai_analysis_is_false(self):
        """allow_ai_analysis가 False(사이클 예산 초과)일 때 AI 호출을 생략하고 로컬 관망으로 처리하는지 검증"""
        mock_config = MagicMock()
        mock_config.min_order_krw = 5000.0
        mock_config.interval_minutes = 5
        mock_context = MagicMock()
        mock_context.cooldown_manager.check_reentry_allowed.return_value = (True, "")
        mock_context.risk_manager.cooldown_until_ts = 0.0
        mock_context.decision_db.has_recovery_entry_since.return_value = False
        engine = TradingCycleEngine(mock_config, mock_context)
        mock_analyzer = MagicMock()

        candles_5m = [
            {"candle_date_time_utc": f"2026-09-04T12:{i:02d}:00Z", "opening_price": 100.0, "trade_price": 101.0, "candle_acc_trade_volume": 100.0, "high_price": 102.0, "low_price": 99.0}
            for i in range(30)
        ]
        candles_1h = [
            {"candle_date_time_utc": f"2026-09-04T{i:02d}:00:00Z", "opening_price": 100.0, "trade_price": 101.0, "candle_acc_trade_volume": 1000.0, "high_price": 102.0, "low_price": 99.0}
            for i in range(25)
        ]

        inputs = MarketEntryInputs(
            exchange=MagicMock(),
            market="KRW-TEST",
            korean_name="테스트",
            candidate_type="CONFIRMED",
            candidate_metadata={"acc_trade_price_24h": 50_000_000_000.0},
            analyzer=mock_analyzer,
            coin_available=0.0,
            avg_buy_price=0.0,
            current_price=101.0,
            coin_value=0.0,
            krw_available=1_000_000.0,
            candles_5m=candles_5m,
            candles_1h=candles_1h,
            orderbook={"orderbook_units": [{"ask_price": 101.5, "bid_price": 101.0, "ask_size": 1.0, "bid_size": 1.0}]},
            btc_regime="NORMAL",
            btc_status_msg="정상",
            is_btc_crashing=False,
            is_cooldown=False,
            is_extreme_fear=False,
            is_bot_paused=False,
            is_kill_switch=False,
            is_entry_ready=True,
            dyn_max_pos_pct=0.3,
            now_str="2026-09-04 21:00:00",
            audit_decision=MagicMock(),
            allow_ai_analysis=False,  # 사이클 예산 소진으로 AI 비활성화!
        )

        with patch("trading_runtime.entry_signal", return_value={"allow_buy": False, "reason": "관망 대기", "entry_price": 100.0, "target_price": 105.0, "stop_loss": 97.0}):
            result = engine.process_entry_gating(inputs)
            self.assertFalse(mock_analyzer.analyze.called)
            self.assertFalse(result.called_ai)
            self.assertEqual(result.action, "HOLD")

    def test_entry_gating_skips_ai_on_bearish_plunge_candle(self):
        """거래대금이 충분하고 allow_ai_analysis=True여도 5분봉이 장대음봉 추락 중이면 AI 호출을 스킵하는지 검증 (1차 유효성 게이팅)"""
        mock_config = MagicMock()
        mock_config.min_order_krw = 5000.0
        mock_config.interval_minutes = 5
        mock_context = MagicMock()
        mock_context.cooldown_manager.check_reentry_allowed.return_value = (True, "")
        mock_context.risk_manager.cooldown_until_ts = 0.0
        mock_context.decision_db.has_recovery_entry_since.return_value = False
        engine = TradingCycleEngine(mock_config, mock_context)
        mock_analyzer = MagicMock()

        # 시가 100원 ➜ 현재가 98원 (-2.0% 장대음봉 급락)
        candles_5m = [
            {"candle_date_time_utc": f"2026-09-04T12:{i:02d}:00Z", "opening_price": 100.0, "trade_price": 98.0, "candle_acc_trade_volume": 100.0, "high_price": 101.0, "low_price": 97.0}
            for i in range(30)
        ]
        candles_1h = [
            {"candle_date_time_utc": f"2026-09-04T{i:02d}:00:00Z", "opening_price": 100.0, "trade_price": 98.0, "candle_acc_trade_volume": 1000.0, "high_price": 101.0, "low_price": 97.0}
            for i in range(25)
        ]

        inputs = MarketEntryInputs(
            exchange=MagicMock(),
            market="KRW-TEST",
            korean_name="테스트",
            candidate_type="CONFIRMED",
            candidate_metadata={"acc_trade_price_24h": 50_000_000_000.0},  # 거래대금 500억 (충분)
            analyzer=mock_analyzer,
            coin_available=0.0,
            avg_buy_price=0.0,
            current_price=98.0,
            coin_value=0.0,
            krw_available=1_000_000.0,
            candles_5m=candles_5m,
            candles_1h=candles_1h,
            orderbook={"orderbook_units": [{"ask_price": 98.5, "bid_price": 98.0, "ask_size": 1.0, "bid_size": 1.0}]},
            btc_regime="NORMAL",
            btc_status_msg="정상",
            is_btc_crashing=False,
            is_cooldown=False,
            is_extreme_fear=False,
            is_bot_paused=False,
            is_kill_switch=False,
            is_entry_ready=True,
            dyn_max_pos_pct=0.3,
            now_str="2026-09-04 21:00:00",
            audit_decision=MagicMock(),
            allow_ai_analysis=True,  # AI 예산이 남아있음에도 불구하고!
        )

        with patch("trading_runtime.entry_signal", return_value={"allow_buy": False, "reason": "관망 대기", "entry_price": 100.0, "target_price": 105.0, "stop_loss": 97.0}):
            result = engine.process_entry_gating(inputs)
            # 음봉 추락으로 1차 유효성 게이팅 탈락 ➜ AI 호출 안 함!
            self.assertFalse(mock_analyzer.analyze.called)
            self.assertFalse(result.called_ai)
            self.assertEqual(result.action, "HOLD")


if __name__ == "__main__":
    unittest.main()
