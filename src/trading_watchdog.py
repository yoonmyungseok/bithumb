"""Shared watchdog engine for dual-exchange bot process supervision."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import requests

from heartbeat_monitor import get_heartbeat_health


def is_pid_alive(pid: int | None) -> bool:
    """PID 프로세스의 생존 여부를 안전하게 확인한다."""
    if not pid or pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259

            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                return False

            exit_code = wintypes.DWORD()
            success = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            kernel32.CloseHandle(handle)

            if success:
                return exit_code.value == STILL_ACTIVE
            return False
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def acquire_single_owner_lock(lock_file_path: str):
    """운영체제 파일 잠금 및 PID 생존 검증으로 단일 소유자를 안전하게 보장한다."""
    owner_path = f"{lock_file_path}.owner.json"

    if os.path.exists(owner_path):
        try:
            with open(owner_path, "r", encoding="utf-8") as owner_file:
                owner_data = json.load(owner_file)
            old_pid = int(owner_data.get("pid", 0))
            if old_pid > 0 and is_pid_alive(old_pid) and old_pid != os.getpid():
                return None
        except Exception:
            pass

    os.makedirs(os.path.dirname(lock_file_path), exist_ok=True)
    try:
        lock_file = open(lock_file_path, "a+", encoding="utf-8")
        lock_file.seek(0)
        if not lock_file.read(1):
            lock_file.seek(0)
            lock_file.write("0")
            lock_file.flush()
        if sys.platform == "win32":
            import msvcrt

            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        if os.path.exists(owner_path):
            try:
                with open(owner_path, "r", encoding="utf-8") as owner_file:
                    owner_data = json.load(owner_file)
                old_pid = int(owner_data.get("pid", 0))
                if old_pid > 0 and not is_pid_alive(old_pid):
                    try:
                        os.remove(lock_file_path)
                    except OSError:
                        pass
            except Exception:
                pass
        return None

    try:
        with open(owner_path, "w", encoding="utf-8") as owner_file:
            json.dump(
                {"pid": os.getpid(), "started_at": time.time(), "start_token": uuid.uuid4().hex},
                owner_file,
            )
    except Exception:
        pass
    return lock_file


@dataclass(frozen=True)
class ExchangeWatchdogProfile:
    """Exchange-specific watchdog paths, labels, and alert wording."""

    exchange_key: str
    data_dir: str
    main_script_name: str
    startup_banner_lines: tuple[str, ...]
    duplicate_instance_warning: str
    shutdown_signal_message: str
    shutdown_complete_message: str
    process_start_log_label: str
    process_spawn_error_log: str
    hang_detect_log_label: str
    abnormal_exit_log_label: str
    crash_loop_alert_title: str
    crash_recovery_alert_title: str
    crash_recovery_process_line: str
    crash_restart_label: str
    duplicate_lock_exit_delay_sec: float = 0.0


@dataclass
class TradingWatchdogContext:
    """Runtime dependencies injected from each watchdog entry point."""

    logger: Any
    project_root: str
    telegram_bot_token: str
    telegram_chat_id: str


class TradingBotWatchdog:
    """Supervise one exchange bot process with heartbeat-based hang detection."""

    HEARTBEAT_GRACE_SEC = 120.0
    HEARTBEAT_STALE_SEC = 600.0
    CRASH_LOOP_THRESHOLD = 5
    CRASH_LOOP_WINDOW_SEC = 60.0
    CRASH_LOOP_COOLDOWN_SEC = 60.0
    RESTART_DELAY_SEC = 5.0
    MONITOR_INTERVAL_SEC = 5.0

    def __init__(self, profile: ExchangeWatchdogProfile, context: TradingWatchdogContext) -> None:
        self.profile = profile
        self.ctx = context
        self._is_terminating = False
        self._current_process: subprocess.Popen | None = None

    def run(self) -> None:
        self._log_startup_banner()
        self._write_pid_file()
        lock_file = self._acquire_lock()
        if lock_file is None:
            return

        self._register_signal_handlers()
        recent_crashes: list[float] = []

        while not self._is_terminating:
            process = self._spawn_bot_process()
            if process is None:
                time.sleep(self.RESTART_DELAY_SEC)
                continue

            hung_detected, hang_reason = self._monitor_process(process)
            if self._is_terminating:
                self._terminate_process(process)
                break

            return_code = process.poll() if process.poll() is not None else (process.returncode or 0)
            if self._is_terminating:
                self.ctx.logger.info(self.profile.shutdown_complete_message)
                break

            reason_desc = (
                f"무응답/하트비트 이상 감지: {hang_reason}"
                if hung_detected
                else f"종료 코드: {return_code}"
            )
            now_ts = time.time()
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self.ctx.logger.warning("%s %s", self.profile.abnormal_exit_log_label, reason_desc)

            recent_crashes.append(now_ts)
            recent_crashes = [
                crash_ts for crash_ts in recent_crashes
                if now_ts - crash_ts <= self.CRASH_LOOP_WINDOW_SEC
            ]

            if len(recent_crashes) >= self.CRASH_LOOP_THRESHOLD:
                self._handle_crash_loop(reason_desc, now_str, len(recent_crashes))
                recent_crashes.clear()
                continue

            self._send_recovery_alert(reason_desc, now_str)
            time.sleep(self.RESTART_DELAY_SEC)

    def _log_startup_banner(self) -> None:
        self.ctx.logger.info("======================================================")
        for line in self.profile.startup_banner_lines:
            self.ctx.logger.info(line)
        self.ctx.logger.info("======================================================")

    def _write_pid_file(self) -> None:
        pid_file = os.path.join(self.profile.data_dir, ".watchdog.pid.json")
        os.makedirs(os.path.dirname(pid_file), exist_ok=True)
        try:
            with open(pid_file, "w", encoding="utf-8") as pid_handle:
                json.dump(
                    {
                        "pid": os.getpid(),
                        "exchange": self.profile.exchange_key,
                        "created_at": time.time(),
                    },
                    pid_handle,
                )
        except Exception:
            pass

    def _acquire_lock(self):
        lock_file_path = os.path.join(self.profile.data_dir, ".watchdog.lock")
        lock_file = acquire_single_owner_lock(lock_file_path)
        if lock_file is None:
            self.ctx.logger.warning(self.profile.duplicate_instance_warning)
            if self.profile.duplicate_lock_exit_delay_sec > 0:
                time.sleep(self.profile.duplicate_lock_exit_delay_sec)
            sys.exit(0)
        return lock_file

    def _resolve_python_exe(self) -> str:
        python_exe = os.path.join(self.ctx.project_root, "venv", "Scripts", "python.exe")
        if os.path.exists(python_exe):
            return python_exe
        for candidate in ("venv/bin/python3", "venv/bin/python"):
            full_path = os.path.join(self.ctx.project_root, candidate)
            if os.path.exists(full_path):
                return full_path
        return sys.executable

    def _spawn_bot_process(self) -> subprocess.Popen | None:
        main_script = os.path.join(self.ctx.project_root, "src", self.profile.main_script_name)
        hb_file = os.path.join(self.profile.data_dir, ".heartbeat")
        self.ctx.logger.info("%s %s", self.profile.process_start_log_label, main_script)

        if os.path.exists(hb_file):
            try:
                os.remove(hb_file)
            except Exception:
                pass

        creationflags = (
            (0x08000000 | 0x00000200 | 0x00000008)
            if sys.platform == "win32"
            else 0
        )
        base_name = os.path.splitext(os.path.basename(main_script))[0]
        spawn_log = os.path.join(self.ctx.project_root, "logs", f"{base_name}_spawn.log")
        os.makedirs(os.path.dirname(spawn_log), exist_ok=True)
        spawn_handle = open(spawn_log, "a", encoding="utf-8", errors="replace")

        try:
            self._current_process = subprocess.Popen(
                [self._resolve_python_exe(), main_script],
                cwd=self.ctx.project_root,
                creationflags=creationflags,
                stdout=spawn_handle,
                stderr=spawn_handle,
                stdin=subprocess.DEVNULL,
                close_fds=True,
            )
            return self._current_process
        except Exception as exc:
            self.ctx.logger.error("%s: %s", self.profile.process_spawn_error_log, exc)
            return None

    def _monitor_process(self, process: subprocess.Popen) -> tuple[bool, str]:
        hb_file = os.path.join(self.profile.data_dir, ".heartbeat")
        start_ts = time.time()
        hung_detected = False
        hang_reason = ""

        while process.poll() is None:
            if self._is_terminating:
                break
            time.sleep(self.MONITOR_INTERVAL_SEC)

            elapsed = time.time() - start_ts
            healthy, heartbeat_reason, heartbeat_age = get_heartbeat_health(hb_file)
            should_restart = elapsed > self.HEARTBEAT_GRACE_SEC and (
                not healthy or heartbeat_age is None or heartbeat_age > self.HEARTBEAT_STALE_SEC
            )
            if should_restart:
                hang_reason = (
                    heartbeat_reason
                    if not healthy
                    else f"하트비트 {heartbeat_age:.0f}초 지연"
                )
                self.ctx.logger.critical(
                    "🛑 [%s] %s. 프로세스를 강제 재시작합니다.",
                    self.profile.hang_detect_log_label,
                    hang_reason,
                )
                hung_detected = True
                process.terminate()
                try:
                    process.wait(timeout=5)
                except Exception:
                    process.kill()
                break

        return hung_detected, hang_reason

    def _terminate_process(self, process: subprocess.Popen) -> None:
        try:
            process.terminate()
            process.wait(timeout=3)
        except Exception:
            process.kill()

    def _handle_crash_loop(self, reason_desc: str, now_str: str, crash_count: int) -> None:
        alert_msg = (
            f"🚨 <b>[{self.profile.crash_loop_alert_title}]</b>\n\n"
            f"• 1분 내 {crash_count}회 연속 비정상 종료가 발생했습니다.\n"
            f"• API 키 또는 환경 설정을 점검해 주세요.\n"
            f"• <b>사유:</b> {reason_desc}\n"
            f"• <b>일시:</b> {now_str}\n\n"
            f"⚠️ 무한 재시작을 방지하기 위해 60초간 대기합니다."
        )
        self.ctx.logger.critical(alert_msg)
        self._send_telegram_alert(alert_msg)
        time.sleep(self.CRASH_LOOP_COOLDOWN_SEC)

    def _send_recovery_alert(self, reason_desc: str, now_str: str) -> None:
        alert_msg = (
            f"⚠️ <b>[{self.profile.crash_recovery_alert_title}]</b>\n\n"
            f"• {self.profile.crash_recovery_process_line}\n"
            f"• <b>사유:</b> <code>{reason_desc}</code>\n"
            f"• <b>일시:</b> {now_str}\n\n"
            f"🔄 <b>5초 후 자동으로 {self.profile.crash_restart_label}을 재가동합니다...</b>"
        )
        self.ctx.logger.info("텔레그램 긴급 알림 발송 및 5초 후 자동 재시작 대기")
        self._send_telegram_alert(alert_msg)

    def _send_telegram_alert(self, msg: str) -> None:
        if not self.ctx.telegram_bot_token or not self.ctx.telegram_chat_id:
            return
        try:
            url = f"https://api.telegram.org/bot{self.ctx.telegram_bot_token}/sendMessage"
            payload = {
                "chat_id": self.ctx.telegram_chat_id,
                "text": msg,
                "parse_mode": "HTML",
            }
            requests.post(url, json=payload, timeout=8)
        except Exception as exc:
            self.ctx.logger.warning("텔레그램 알림 전송 실패: %s", exc)

    def _register_signal_handlers(self) -> None:
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, signal.SIG_IGN)
        if sys.platform == "win32":
            signal.signal(signal.SIGINT, signal.SIG_IGN)
        else:
            signal.signal(signal.SIGINT, self._handle_exit_signal)
        signal.signal(signal.SIGTERM, self._handle_exit_signal)

    def _handle_exit_signal(self, sig=None, frame=None) -> None:
        self._is_terminating = True
        self.ctx.logger.info(self.profile.shutdown_signal_message)
        if self._current_process and self._current_process.poll() is None:
            try:
                self._current_process.terminate()
                self._current_process.wait(timeout=3)
            except Exception:
                try:
                    self._current_process.kill()
                except Exception:
                    pass
        sys.exit(0)
