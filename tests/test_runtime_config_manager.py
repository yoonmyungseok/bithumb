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
            "MAX_OPEN_POSITIONS", "MAX_POSITION_PCT", "MAX_TOTAL_EXPOSURE_PCT", "MAX_ORDER_KRW"
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

    def test_get_runtime_config_reflects_live_objects(self):
        res = self.controller.get_runtime_config()
        self.assertTrue(res["success"])
        settings = res["settings"]
        self.assertAlmostEqual(settings["TRAILING_START_PCT"]["value"], 0.02)
        self.assertAlmostEqual(settings["TRAILING_START_PCT"]["display_value"], 2.0)
        self.assertEqual(settings["MAX_OPEN_POSITIONS"]["value"], 3)

    def test_update_runtime_config_mutates_live_objects_immediately(self):
        updates = {
            "TRAILING_START_PCT": 2.5,
            "TRAILING_STOP_PCT": 1.5,
            "MAX_DAILY_LOSS_PCT": 6.0,
            "MAX_OPEN_POSITIONS": 4,
            "MAX_POSITION_PCT": 40.0,
        }
        res = self.controller.update_runtime_config(updates)
        self.assertTrue(res["success"])

        self.assertAlmostEqual(self.trailing_tracker.start_profit_pct, 0.025)
        self.assertAlmostEqual(self.trailing_tracker.trailing_drop_pct, 0.015)
        self.assertAlmostEqual(self.risk_manager.max_loss_pct, 0.06)
        self.assertEqual(self.risk_guard.max_open_positions, 4)
        self.assertAlmostEqual(self.risk_guard.max_position_pct, 0.40)


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


if __name__ == "__main__":
    unittest.main()
