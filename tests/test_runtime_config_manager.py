"""Unit tests for CommonConfigManager and runtime hot-reload capabilities."""

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from runtime_config import COMMON_CONFIG_SCHEMA, CommonConfigManager
from bot_controller import BotController
from risk_controls import RiskGuard
from risk_manager import DailyRiskManager, TrailingStopTracker


class TestCommonConfigManager(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env_path = os.path.join(self.temp_dir.name, ".env")
        with open(self.env_path, "w", encoding="utf-8") as f:
            f.write("# Sample env\nTRAILING_START_PCT=0.02\nTOP_COUNT=3\n")
        self.manager = CommonConfigManager(env_path=self.env_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_schema_coverage(self):
        expected_keys = [
            "TRAILING_START_PCT", "TRAILING_STOP_PCT", "MAX_DAILY_LOSS_PCT", "BTC_CRASH_THRESHOLD_PCT",
            "ORDERBOOK_SLIPPAGE_ENFORCEMENT", "TOP_COUNT", "MIN_TRADE_VALUE", "MIN_CHANGE_RATE",
            "MAX_CHANGE_RATE", "MOMENTUM_BREAKOUT_ENABLED", "NEW_LISTING_ENABLED", "NEW_LISTING_ENFORCEMENT",
            "MAX_OPEN_POSITIONS", "MAX_POSITION_PCT", "MAX_TOTAL_EXPOSURE_PCT", "MAX_ORDER_KRW",
            "MAX_SWING_POSITIONS", "MAX_NEW_LISTING_POSITIONS", "MAX_SCALP_POSITIONS",
            "MAX_ALT_ALLOC_PCT"
        ]
        settings = self.manager.get_all_settings()
        for k in expected_keys:
            self.assertIn(k, settings)
            self.assertIn("value", settings[k])
            self.assertIn("display_value", settings[k])

    def test_validate_and_normalize_percent(self):
        ok, val, err = self.manager.validate_and_normalize("TRAILING_START_PCT", 2.5)
        self.assertTrue(ok)
        self.assertAlmostEqual(val, 0.025)
        self.assertEqual(err, "")

        ok, val, err = self.manager.validate_and_normalize("TRAILING_START_PCT", 0.025)
        self.assertTrue(ok)
        self.assertAlmostEqual(val, 0.025)

        # 100% 입력 시 1%(0.01)로 왜곡되지 않고 1.0(100%)으로 정상 정규화되는지 검증
        ok, val, err = self.manager.validate_and_normalize("MAX_POSITION_PCT", 1.0)
        self.assertTrue(ok)
        self.assertAlmostEqual(val, 1.0)

        ok, val, err = self.manager.validate_and_normalize("MAX_POSITION_PCT", 100)
        self.assertTrue(ok)
        self.assertAlmostEqual(val, 1.0)

        # 1% 미만(0.5%)을 백분율(0.5)로 입력 시 50%가 아닌 0.005(0.5%)로 정상 정규화되는지 검증
        ok, val, err = self.manager.validate_and_normalize("MIN_CHANGE_RATE", 0.5)
        self.assertTrue(ok)
        self.assertAlmostEqual(val, 0.005)

        ok, val, err = self.manager.validate_and_normalize("MIN_CHANGE_RATE", 0.005)
        self.assertTrue(ok)
        self.assertAlmostEqual(val, 0.005)

        # 하한(0.5%) 미만 실패 테스트 (0.1% = 0.001)
        ok, val, err = self.manager.validate_and_normalize("TRAILING_START_PCT", 0.001)
        self.assertFalse(ok)
        self.assertIn("최소", err)

        ok, val, err = self.manager.validate_and_normalize("TRAILING_START_PCT", 25.0)
        self.assertFalse(ok)
        self.assertIn("최대", err)

    def test_validate_and_normalize_int_and_bool(self):
        ok, val, err = self.manager.validate_and_normalize("TOP_COUNT", 5)
        self.assertTrue(ok)
        self.assertEqual(val, 5)

        ok, val, err = self.manager.validate_and_normalize("TOP_COUNT", 15)
        self.assertFalse(ok)

        ok, val, err = self.manager.validate_and_normalize("MOMENTUM_BREAKOUT_ENABLED", "true")
        self.assertTrue(ok)
        self.assertTrue(val)

        ok, val, err = self.manager.validate_and_normalize("MOMENTUM_BREAKOUT_ENABLED", False)
        self.assertTrue(ok)
        self.assertFalse(val)

    def test_update_settings_persists_to_env(self):
        updates = {
            "TRAILING_START_PCT": 3.5,
            "TOP_COUNT": 5,
            "ORDERBOOK_SLIPPAGE_ENFORCEMENT": True,
        }
        ok, normalized, errors = self.manager.update_settings(updates)
        self.assertTrue(ok)
        self.assertEqual(len(errors), 0)
        self.assertAlmostEqual(normalized["TRAILING_START_PCT"], 0.035)

        with open(self.env_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("TRAILING_START_PCT=0.035", content)
        self.assertIn("TOP_COUNT=5", content)
        self.assertIn("ORDERBOOK_SLIPPAGE_ENFORCEMENT=true", content)

    def test_auto_sum_slots_into_max_open_positions(self):
        """단타, 스윙, 신규상장 슬롯 변경 시 MAX_OPEN_POSITIONS 자동 합산 및 MAX_POSITION_PCT 자동 계산 검증"""
        updates = {
            "MAX_SCALP_POSITIONS": 4,
            "MAX_SWING_POSITIONS": 2,
            "MAX_NEW_LISTING_POSITIONS": 1,
        }
        ok, normalized, errors = self.manager.update_settings(updates)
        self.assertTrue(ok)
        self.assertEqual(len(errors), 0)
        self.assertEqual(normalized["MAX_OPEN_POSITIONS"], 7)  # 4 + 2 + 1 = 7
        self.assertEqual(os.environ.get("MAX_OPEN_POSITIONS"), "7")
        # 7개 슬롯: min(0.50, max(0.15, round(1/7 + 0.05, 2))) = round(0.1428 + 0.05, 2) = 0.19
        self.assertAlmostEqual(normalized["MAX_POSITION_PCT"], 0.19)
        self.assertEqual(os.environ.get("MAX_POSITION_PCT"), "0.19")

    def test_auto_max_position_pct_for_5_slots(self):
        """단타 3 + 스윙 1 + 신규상장 1 (총 5개) 입력 시 MAX_POSITION_PCT 25% 자동 산출 검증"""
        updates = {
            "MAX_SCALP_POSITIONS": 3,
            "MAX_SWING_POSITIONS": 1,
            "MAX_NEW_LISTING_POSITIONS": 1,
        }
        ok, normalized, errors = self.manager.update_settings(updates)
        self.assertTrue(ok)
        self.assertEqual(normalized["MAX_OPEN_POSITIONS"], 5)
        # 5개 슬롯: min(0.50, max(0.15, round(1/5 + 0.05, 2))) = 0.20 + 0.05 = 0.25 (25%)
        self.assertAlmostEqual(normalized["MAX_POSITION_PCT"], 0.25)
        self.assertEqual(os.environ.get("MAX_POSITION_PCT"), "0.25")


class TestBotControllerRuntimeConfig(unittest.TestCase):
    def setUp(self):
        self.trailing_tracker = TrailingStopTracker(start_profit_pct=0.02, trailing_drop_pct=0.02)
        self.risk_manager = DailyRiskManager(max_loss_pct=0.05)
        self.risk_guard = RiskGuard(
            min_order_krw=5000,
            max_open_positions=3,
            max_position_pct=0.35,
            max_total_exposure_pct=0.90,
            max_order_krw=20000000,
            max_swing_positions=1,
            max_new_listing_positions=1,
        )
        self.controller = BotController(
            exchange_factory=MagicMock(),
            order_executor=MagicMock(),
            order_journal=MagicMock(),
            risk_manager=self.risk_manager,
            trailing_tracker=self.trailing_tracker,
            trade_memory=MagicMock(),
            telegram=MagicMock(),
            get_is_paused=lambda: False,
            set_is_paused=MagicMock(),
            exchange_name="빗썸테스트",
            risk_guard=self.risk_guard,
        )

    def tearDown(self):
        from strategy_engine import StrategyPolicy
        StrategyPolicy.MAX_ALT_ALLOC_PCT = 0.15

    def test_get_runtime_config_reflects_live_objects(self):
        res = self.controller.get_runtime_config()
        self.assertTrue(res["success"])
        settings = res["settings"]
        self.assertAlmostEqual(settings["TRAILING_START_PCT"]["value"], 0.02)
        self.assertAlmostEqual(settings["TRAILING_START_PCT"]["display_value"], 2.0)
        self.assertEqual(settings["MAX_OPEN_POSITIONS"]["value"], 3)
        self.assertEqual(settings["MAX_SWING_POSITIONS"]["value"], 1)
        self.assertEqual(settings["MAX_NEW_LISTING_POSITIONS"]["value"], 1)
        self.assertEqual(settings["MAX_SCALP_POSITIONS"]["value"], 1)

    def test_update_runtime_config_mutates_live_objects_immediately(self):
        updates = {
            "TRAILING_START_PCT": 2.5,
            "TRAILING_STOP_PCT": 1.5,
            "MAX_DAILY_LOSS_PCT": 6.0,
            "MAX_SCALP_POSITIONS": 3,
            "MAX_SWING_POSITIONS": 1,
            "MAX_NEW_LISTING_POSITIONS": 1,
            "MAX_POSITION_PCT": 40.0,
            "MAX_ORDER_KRW": 30_000_000,
            "MAX_ALT_ALLOC_PCT": 20.0,
        }
        res = self.controller.update_runtime_config(updates)
        self.assertTrue(res["success"])

        self.assertAlmostEqual(self.trailing_tracker.start_profit_pct, 0.025)
        self.assertAlmostEqual(self.trailing_tracker.trailing_drop_pct, 0.015)
        self.assertAlmostEqual(self.risk_manager.max_loss_pct, 0.06)
        # StrategyPolicy.MAX_ALT_ALLOC_PCT 핫리로드 검증
        from strategy_engine import StrategyPolicy
        self.assertAlmostEqual(StrategyPolicy.MAX_ALT_ALLOC_PCT, 0.20)
        # 단타 3 + 스윙 1 + 신규 1 = 총 포지션 5 자동 갱신 검증
        self.assertEqual(self.risk_guard.max_open_positions, 5)
        self.assertEqual(self.risk_guard.max_scalp_positions, 3)
        self.assertEqual(self.risk_guard.max_swing_positions, 1)
        self.assertEqual(self.risk_guard.max_new_listing_positions, 1)
        self.assertAlmostEqual(self.risk_guard.max_position_pct, 0.40)
        self.assertEqual(self.risk_guard.max_order_krw, 30_000_000)


class TestRuntimePortfolioTiers(unittest.TestCase):
    """런타임 포트폴리오 티어 래퍼가 동적 설정을 올바르게 보존하는지 검증"""

    def test_get_dynamic_portfolio_tiers_respects_custom_pct_and_positions(self):
        from risk_controls import get_dynamic_portfolio_tiers

        # 기본(자산 50만 원): 5개, 25%, 12개
        pos, pct, top = get_dynamic_portfolio_tiers(500_000.0)
        self.assertEqual(pos, 5)
        self.assertAlmostEqual(pct, 0.25)
        self.assertEqual(top, 12)

        # 동적 커스텀 포지션 수(4개)와 비중(40%) 지정 시 그대로 유지되는지 검증
        pos, pct, top = get_dynamic_portfolio_tiers(500_000.0, custom_max_positions=4, custom_max_position_pct=0.40)
        self.assertEqual(pos, 4)
        self.assertAlmostEqual(pct, 0.40)
        self.assertEqual(top, 12)

    def test_screener_creation_reads_live_env(self):
        """환경 변수 MOMENTUM_BREAKOUT_ENABLED 런타임 변경 시 스크리너에 즉시 반영되는지 검증"""
        import main
        from exchange_adapter import ExchangeAdapter

        mock_exchange = MagicMock(spec=ExchangeAdapter)
        os.environ["MOMENTUM_BREAKOUT_ENABLED"] = "false"
        screener1 = main._create_bithumb_screener(mock_exchange)
        self.assertFalse(screener1.enable_early_breakout)

        os.environ["MOMENTUM_BREAKOUT_ENABLED"] = "true"
        screener2 = main._create_bithumb_screener(mock_exchange)
        self.assertTrue(screener2.enable_early_breakout)


class TestDashboardGatewayConfig(unittest.TestCase):
    """UnifiedDashboardServer 설정 제어 메서드 테스트"""

    def setUp(self):
        from dashboard_server import UnifiedDashboardServer
        self.server = UnifiedDashboardServer(
            port=18999,
            host="127.0.0.1",
            bithumb_api_url="http://127.0.0.1:17979",
            upbit_api_url="http://127.0.0.1:17980",
        )

    def test_get_common_config_returns_settings(self):
        res = self.server.get_common_config()
        self.assertTrue(res["success"])
        self.assertIn("settings", res)
        self.assertIn("TRAILING_START_PCT", res["settings"])

    def test_update_common_config_validation_failure(self):
        # 비정상적인 값(손실 한도 50%) 입력 시 유효성 검증 실패 반환
        res = self.server.update_common_config({"MAX_DAILY_LOSS_PCT": 0.50})
        self.assertFalse(res["success"])
        self.assertIn("errors", res)
        self.assertGreater(len(res["errors"]), 0)


class TestExchangeEnvHelpers(unittest.TestCase):
    """거래소별 우선순위 환경변수 로더 및 TRADING_MODE 정규화 검증"""

    def setUp(self):
        self.orig_environ = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.orig_environ)

    def test_exchange_prefix_priority(self):
        from runtime_config import get_exchange_env_setting

        os.environ["UPBIT_MOMENTUM_BREAKOUT_ENABLED"] = "false"
        os.environ["MOMENTUM_BREAKOUT_ENABLED"] = "true"
        # UPBIT 접두어가 공통 설정을 오버라이드해야 함
        self.assertFalse(get_exchange_env_setting("upbit", "MOMENTUM_BREAKOUT_ENABLED", default=True, type_cast=bool))
        # 빗썸은 UPBIT 접두어에 영향받지 않고 공통 설정을 읽어야 함
        self.assertTrue(get_exchange_env_setting("bithumb", "MOMENTUM_BREAKOUT_ENABLED", default=False, type_cast=bool))

    def test_exchange_prefix_fallback_to_common(self):
        from runtime_config import get_exchange_env_setting

        os.environ.pop("BITHUMB_MIN_TRADE_VALUE", None)
        os.environ["MIN_TRADE_VALUE"] = "500000000"
        self.assertEqual(get_exchange_env_setting("bithumb", "MIN_TRADE_VALUE", 1000000000, type_cast=float), 500000000.0)

    def test_normalize_trading_mode(self):
        from runtime_config import normalize_trading_mode

        self.assertEqual(normalize_trading_mode("REAL"), "LIVE")
        self.assertEqual(normalize_trading_mode("LIVE"), "LIVE")
        self.assertEqual(normalize_trading_mode("PAPER"), "PAPER")
        self.assertEqual(normalize_trading_mode("mock"), "PAPER")
        self.assertEqual(normalize_trading_mode(None), "LIVE")


if __name__ == "__main__":
    unittest.main()

