"""9대 버그 수정 및 안정성 강화에 대한 회귀 테스트 스위트."""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from bithumb_api import BithumbAPI
from gemini_analyzer import GeminiAnalyzer
from market_screener import MarketScreener
from process_manager import _kill_matching_script_processes
from risk_controls import calculate_risk_position_size
from telegram_alert import TelegramAlert
from upbit_api import UpbitAPI
from watchdog import BITHUMB_WATCHDOG_PROFILE


class BugfixesRegressionTests(unittest.TestCase):
    """오류 수정 항목 회귀 검증 테스트"""

    def test_process_manager_no_name_error_on_non_win32(self):
        """_kill_matching_script_processes가 non-win32 환경에서도 NameError(my_pid) 없이 실행된다."""
        with patch("sys.platform", "linux"), \
             patch("subprocess.run") as mock_run, \
             patch("process_manager._kill_pid", return_value=True):
            mock_run.return_value = MagicMock(returncode=0, stdout="12345\n99999\n")
            killed = _kill_matching_script_processes(["dummy_pattern.py"])
            self.assertIsInstance(killed, list)

    def test_market_screener_confirmed_vs_momentum_leader_separation(self):
        """변동률 2%, RS 2% 종목은 CONFIRMED로 유지되고, 변동률 5%, RS 5% 종목만 MOMENTUM_BREAKOUT으로 분류된다."""
        class MockAPI:
            def get_all_markets(self, is_details=True):
                return [{"market": "KRW-BTC"}, {"market": "KRW-LEADER"}, {"market": "KRW-NORMAL"}]

            def get_tickers(self, markets):
                return [
                    {"market": "KRW-BTC", "trade_price": "100000", "signed_change_rate": "0.0", "acc_trade_price_24h": "10000000000"},
                    {"market": "KRW-LEADER", "trade_price": "5000", "signed_change_rate": "0.05", "acc_trade_price_24h": "10000000000"},
                    {"market": "KRW-NORMAL", "trade_price": "2000", "signed_change_rate": "0.02", "acc_trade_price_24h": "5000000000"},
                ]

            def get_orderbook(self, market):
                return {"orderbook_units": [{"ask_price": 5001.0, "bid_price": 5000.0, "bid_size": 10000.0}]}

        screener = MarketScreener(
            MockAPI(),
            min_trade_value_krw=1,
            min_change_rate=0.005,
            enable_early_breakout=True,
        )
        markets = screener.scan_markets(top_count=2)

        leader = next(m for m in markets if m["market"] == "KRW-LEADER")
        self.assertEqual(leader["candidate_type"], "MOMENTUM_BREAKOUT")

        normal = next(m for m in markets if m["market"] == "KRW-NORMAL")
        self.assertEqual(normal["candidate_type"], "CONFIRMED")

    def test_bithumb_watchdog_profile_string_isolation(self):
        """빗썸 워치독 알림 문구에 '업비트'가 포함되지 않고 '빗썸'으로 올바르게 분리되어 있다."""
        self.assertIn("빗썸 봇", BITHUMB_WATCHDOG_PROFILE.crash_recovery_process_line)
        self.assertNotIn("업비트", BITHUMB_WATCHDOG_PROFILE.crash_recovery_process_line)

    def test_risk_controls_zero_available_krw(self):
        """가용 원화(available_krw)가 0.0원일 때 포지션 크기가 0.0원으로 안전하게 제한된다."""
        pos = calculate_risk_position_size(
            total_equity=10_000_000.0,
            entry_price=1000.0,
            stop_loss=970.0,
            available_krw=0.0,
        )
        self.assertEqual(pos, 0.0)

    def test_bithumb_api_zero_volume_validation(self):
        """빗썸 API create_order에 volume <= 0 입력 시 ValueError가 발생한다."""
        api = BithumbAPI("dummy", "dummy")
        with self.assertRaises(ValueError):
            api.create_order("KRW-BTC", "bid", volume=0.0, price=1000.0, ord_type="limit")
        with self.assertRaises(ValueError):
            api.create_order("KRW-BTC", "ask", volume=-0.1, ord_type="market")

    def test_upbit_api_zero_volume_validation(self):
        """업비트 API create_order에 volume <= 0 입력 시 ValueError가 발생한다."""
        api = UpbitAPI("dummy", "dummy")
        with self.assertRaises(ValueError):
            api.create_order("KRW-BTC", "bid", volume=0.0, price=1000.0, ord_type="limit")
        with self.assertRaises(ValueError):
            api.create_order("KRW-BTC", "ask", volume=-0.5, ord_type="market")

    def test_gemini_analyzer_safe_text_extraction(self):
        """Gemini 응답이 비어있거나 이상 구조일 때 예외 없이 안전하게 None을 반환한다."""
        self.assertIsNone(GeminiAnalyzer._extract_text_from_response({}))
        self.assertIsNone(GeminiAnalyzer._extract_text_from_response({"candidates": []}))
        self.assertIsNone(GeminiAnalyzer._extract_text_from_response({"candidates": [{"content": {}}]}))
        self.assertIsNone(GeminiAnalyzer._extract_text_from_response({"candidates": [{"content": {"parts": []}}]}))

        valid_resp = {
            "candidates": [
                {"content": {"parts": [{"text": "{\"STATUS\": \"ACTIVE\"}"}]}}
            ]
        }
        self.assertEqual(GeminiAnalyzer._extract_text_from_response(valid_resp), '{"STATUS": "ACTIVE"}')

    def test_telegram_alert_fallback_on_html_400(self):
        """HTML 파싱 에러(400) 발생 시 parse_mode를 제거한 일반 텍스트 모드로 자동 재전송(폴백)된다."""
        alert = TelegramAlert(bot_token="test_token", chat_id="12345", enable_async=False)
        with patch("requests.post") as mock_post:
            resp_400 = MagicMock(status_code=400, text="Bad Request: can't parse entities")
            resp_200 = MagicMock(status_code=200, text='{"ok": true}')
            mock_post.side_effect = [resp_400, resp_200]

            success = alert.send_message("RSI 과열 (< 30 & > 70)", parse_mode="HTML")
            self.assertTrue(success)
            self.assertEqual(mock_post.call_count, 2)
            second_payload = mock_post.call_args_list[1][1]["json"]
            self.assertNotIn("parse_mode", second_payload)


if __name__ == "__main__":
    unittest.main()
