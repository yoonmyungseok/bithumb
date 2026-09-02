import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from trading_watchdog import (
    ExchangeWatchdogProfile,
    TradingBotWatchdog,
    TradingWatchdogContext,
    acquire_single_owner_lock,
    is_pid_alive,
)


def _make_profile(**overrides) -> ExchangeWatchdogProfile:
    defaults = {
        "exchange_key": "bithumb",
        "data_dir": "/tmp/data",
        "main_script_name": "main.py",
        "startup_banner_lines": ("watchdog start",),
        "duplicate_instance_warning": "duplicate",
        "shutdown_signal_message": "shutdown",
        "shutdown_complete_message": "done",
        "process_start_log_label": "start",
        "process_spawn_error_log": "spawn failed",
        "hang_detect_log_label": "Hang",
        "abnormal_exit_log_label": "abnormal",
        "crash_loop_alert_title": "crash loop",
        "crash_recovery_alert_title": "recovery",
        "crash_recovery_process_line": "process died",
        "crash_restart_label": "bot",
    }
    defaults.update(overrides)
    return ExchangeWatchdogProfile(**defaults)


def _make_context(**overrides) -> TradingWatchdogContext:
    defaults = {
        "logger": MagicMock(),
        "project_root": "/tmp/project",
        "telegram_bot_token": "",
        "telegram_chat_id": "",
    }
    defaults.update(overrides)
    return TradingWatchdogContext(**defaults)


class TradingWatchdogTests(unittest.TestCase):
    def test_is_pid_alive_rejects_invalid_pid(self):
        self.assertFalse(is_pid_alive(None))
        self.assertFalse(is_pid_alive(0))
        self.assertFalse(is_pid_alive(-1))

    def test_acquire_single_owner_lock_creates_owner_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            lock_path = os.path.join(tmp_dir, ".watchdog.lock")
            lock_file = acquire_single_owner_lock(lock_path)
            self.assertIsNotNone(lock_file)
            owner_path = f"{lock_path}.owner.json"
            self.assertTrue(os.path.exists(owner_path))
            with open(owner_path, "r", encoding="utf-8") as owner_file:
                owner_data = json.load(owner_file)
            self.assertGreater(owner_data.get("pid", 0), 0)
            lock_file.close()

    @patch("trading_watchdog.time.sleep")
    @patch("trading_watchdog.subprocess.Popen")
    @patch.object(TradingBotWatchdog, "_monitor_process", return_value=(False, ""))
    @patch.object(TradingBotWatchdog, "_send_recovery_alert")
    @patch.object(TradingBotWatchdog, "_register_signal_handlers")
    @patch("trading_watchdog.acquire_single_owner_lock")
    def test_run_restarts_after_abnormal_exit(
        self,
        mock_acquire_lock,
        mock_register_handlers,
        mock_send_recovery_alert,
        mock_monitor_process,
        mock_popen,
        mock_sleep,
    ):
        mock_acquire_lock.return_value = MagicMock()
        process = MagicMock()
        process.poll.return_value = 1
        process.returncode = 1
        mock_popen.return_value = process
        mock_sleep.side_effect = KeyboardInterrupt

        watchdog = TradingBotWatchdog(_make_profile(), _make_context())
        with self.assertRaises(KeyboardInterrupt):
            watchdog.run()

        mock_send_recovery_alert.assert_called_once()
        mock_sleep.assert_called()

    @patch("trading_watchdog.get_heartbeat_health", return_value=(False, "하트비트 파일 없음", None))
    @patch("trading_watchdog.time.sleep")
    @patch("trading_watchdog.time.time")
    def test_monitor_process_detects_stale_heartbeat(self, mock_time, mock_sleep, mock_health):
        mock_time.side_effect = [0.0, 200.0]
        process = MagicMock()
        process.poll.return_value = None

        watchdog = TradingBotWatchdog(
            _make_profile(data_dir="/tmp/data"),
            _make_context(),
        )

        hung_detected, hang_reason = watchdog._monitor_process(process)

        self.assertTrue(hung_detected)
        self.assertEqual(hang_reason, "하트비트 파일 없음")
        process.terminate.assert_called_once()

    @patch("trading_watchdog.requests.post")
    def test_send_telegram_alert_skips_without_credentials(self, mock_post):
        watchdog = TradingBotWatchdog(_make_profile(), _make_context())
        watchdog._send_telegram_alert("test")
        mock_post.assert_not_called()

    @patch("trading_watchdog.requests.post")
    def test_send_telegram_alert_posts_when_configured(self, mock_post):
        watchdog = TradingBotWatchdog(
            _make_profile(),
            _make_context(telegram_bot_token="token", telegram_chat_id="chat"),
        )
        watchdog._send_telegram_alert("hello")
        mock_post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
