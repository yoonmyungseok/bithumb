import os
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
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


class BackgroundProcessTests(unittest.TestCase):
    """백그라운드 실행의 터미널 분리와 로그 핸들 정리를 검증한다."""

    @unittest.skipIf(sys.platform == "win32", "새 세션 검사는 Unix 전용")
    def test_child_runs_in_own_session_with_detached_io(self):
        """실제 검사 자식은 부모 세션과 분리되고 로그는 부모 핸들 종료 뒤에도 기록된다."""
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "검사용.py"
            script.write_text(
                "import json, os, sys\n"
                "# 검사 전용 프로세스는 서비스나 주문을 실행하지 않는다.\n"
                "print(json.dumps({'pid': os.getpid(), 'sid': os.getsid(0), "
                "'pgid': os.getpgrp(), 'stdin': sys.stdin.read()}), flush=True)\n",
                encoding="utf-8",
            )
            real_popen = subprocess.Popen
            children = []
            handles = []

            def spawn(*args, **kwargs):
                handles.append(kwargs["stdout"])
                child = real_popen(*args, **kwargs)
                children.append(child)
                return child

            try:
                with patch("process_manager.subprocess.Popen", side_effect=spawn):
                    pid = process_manager._spawn_background_process(sys.executable, str(script), folder)
                self.assertIsNotNone(pid)
                self.assertTrue(handles[0].closed)
                self.assertEqual(children[0].wait(timeout=5), 0)
                data = json.loads((Path(folder) / "logs" / "검사용_spawn.log").read_text())
                self.assertEqual(data["pid"], pid)
                self.assertEqual(data["sid"], pid)
                self.assertEqual(data["pgid"], pid)
                self.assertNotEqual(data["sid"], os.getsid(0))
                self.assertEqual(data["stdin"], "")
            finally:
                for child in children:
                    if child.poll() is None:
                        child.terminate()
                    child.wait(timeout=5)

    def test_spawn_failure_closes_log_handle(self):
        """자식 시작 실패 시 로그 핸들을 남기지 않고 실패를 반환한다."""
        with tempfile.TemporaryDirectory() as folder:
            handles = []

            def fail(*args, **kwargs):
                handles.append(kwargs["stdout"])
                raise OSError("검사 전용 시작 실패")

            with patch("process_manager.subprocess.Popen", side_effect=fail):
                pid = process_manager._spawn_background_process("검사용", "검사용.py", folder)
            self.assertIsNone(pid)
            self.assertTrue(handles[0].closed)

    def test_windows_keeps_existing_background_flags(self):
        """Windows는 기존 생성 플래그를 유지하고 Unix 세션 생성을 요청하지 않는다."""
        with tempfile.TemporaryDirectory() as folder:
            with patch("process_manager.sys.platform", "win32"), \
                 patch("process_manager.subprocess.Popen") as spawn:
                spawn.return_value.pid = 123
                pid = process_manager._spawn_background_process("검사용", "검사용.py", folder)
            self.assertEqual(pid, 123)
            kwargs = spawn.call_args.kwargs
            self.assertFalse(kwargs["start_new_session"])
            self.assertEqual(kwargs["creationflags"], 0x08000000 | 0x00000200 | 0x00000008)
            self.assertTrue(kwargs["stdout"].closed)


if __name__ == "__main__":
    unittest.main()
