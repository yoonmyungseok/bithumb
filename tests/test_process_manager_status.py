import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import process_manager


class ProcessManagerStatusTests(unittest.TestCase):
    """status 경로가 파일을 변경하지 않고 PID와 하트비트를 분리하는지 검증한다."""

    def test_fresh_heartbeat_without_live_pid_is_not_running(self):
        with patch("process_manager._runtime_paths", return_value=("pid.json", "heartbeat.json")), \
             patch("process_manager._read_pid_file", return_value=None), \
             patch("process_manager._get_heartbeat_age", return_value=10.0), \
             patch("process_manager._is_pid_alive", return_value=False), \
             patch("process_manager.os.path.exists", return_value=True), \
             patch("process_manager.os.remove") as remove_file:
            status = process_manager.inspect_runtime_status("bithumb", now=11.0)
            self.assertFalse(status["watchdog_alive"])
            self.assertFalse(status["owner_alive"])
            self.assertTrue(status["heartbeat_fresh"])
            # status는 하트비트만 신선한 경우에도 파일을 정리하거나 프로세스를 제어하지 않는다.
            self.assertFalse(remove_file.called)

    def test_pid_and_watchdog_only_states_are_distinguished(self):
        with patch("process_manager._runtime_paths", return_value=("pid.json", "heartbeat.json")), \
             patch("process_manager._read_pid_file", side_effect=[101, 202]), \
             patch("process_manager._get_heartbeat_age", return_value=None), \
             patch("process_manager._is_pid_alive", side_effect=lambda pid: pid == 202), \
             patch("process_manager.os.path.exists", return_value=True):
            status = process_manager.inspect_runtime_status("upbit")
        self.assertFalse(status["watchdog_alive"])
        self.assertTrue(status["owner_alive"])


if __name__ == "__main__":
    unittest.main()
