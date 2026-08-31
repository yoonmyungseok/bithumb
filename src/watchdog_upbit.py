"""
스마트 자동 재시작 감시자 (Upbit Watchdog Engine)
- 업비트 봇 프로세스(src/main_upbit.py)를 24시간 실시간 감시하고 비정상 종료 시 텔레그램 알림 및 5초 내 자동 복구
"""

import logging
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler

from dotenv import load_dotenv
import requests
from heartbeat_monitor import get_heartbeat_health

# UTF-8 출력 보장
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 1. 로깅 설정 (콘솔 + logs/watchdog_upbit.log 파일 기록)
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
log_dir = os.path.join(project_root, "logs")
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, "watchdog_upbit.log")

file_handler = TimedRotatingFileHandler(
    filename=log_file, when="midnight", interval=1, backupCount=14, encoding="utf-8"
)
file_handler.setFormatter(
    logging.Formatter("[%(asctime)s] [UPBIT-WATCHDOG] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
)

handlers = [file_handler]
if sys.stderr is not None:
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(
        logging.Formatter("[%(asctime)s] [UPBIT-WATCHDOG] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    handlers.append(stream_handler)

logging.basicConfig(
    level=logging.INFO,
    handlers=handlers,
)
logger = logging.getLogger("UpbitWatchdog")

# 2. 업비트 환경변수 우선 로드
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
upbit_env_path = os.path.join(project_root, ".env.upbit")
if os.path.exists(upbit_env_path):
    load_dotenv(upbit_env_path, override=True)
else:
    load_dotenv(override=True)

TELEGRAM_BOT_TOKEN = os.getenv("UPBIT_TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("UPBIT_TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID", "").strip()


def send_telegram_alert(msg: str):
    """워치독 자체 긴급 텔레그램 알림 발송"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "HTML",
        }
        requests.post(url, json=payload, timeout=8)
    except Exception as e:
        logger.warning(f"텔레그램 알림 전송 실패: {e}")


def acquire_single_owner_lock(lock_file_path: str):
    """운영체제 파일 잠금 및 PID 생존 검증으로 단일 소유자를 안전하게 보장한다."""
    owner_path = f"{lock_file_path}.owner.json"
    
    # 1. 기존 소유자가 살아있는지 사전 점검
    if os.path.exists(owner_path):
        try:
            with open(owner_path, "r", encoding="utf-8") as of:
                owner_data = json.load(of)
            old_pid = int(owner_data.get("pid", 0))
            if old_pid > 0 and is_pid_alive(old_pid) and old_pid != os.getpid():
                # 다른 살아있는 워치독 프로세스가 이미 존재함
                return None
        except Exception:
            pass

    # 2. 파일 잠금 획득 시도
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
        # 잠금 실패 시, 소유자 PID가 이미 죽어있다면 잠금 파일 정리 후 재시도 허용
        if os.path.exists(owner_path):
            try:
                with open(owner_path, "r", encoding="utf-8") as of:
                    owner_data = json.load(of)
                old_pid = int(owner_data.get("pid", 0))
                if old_pid > 0 and not is_pid_alive(old_pid):
                    try:
                        os.remove(lock_file_path)
                    except OSError:
                        pass
            except Exception:
                pass
        return None

    # 3. 신규 소유자 정보 기록
    try:
        with open(owner_path, "w", encoding="utf-8") as owner_file:
            json.dump({"pid": os.getpid(), "started_at": time.time(), "start_token": uuid.uuid4().hex}, owner_file)
    except Exception:
        pass
    return lock_file


def main():
    logger.info("======================================================")
    logger.info("  Upbit Bot Smart Watchdog Engine 가동 시작")
    logger.info("  (24시간 무중단 감시 및 자동 복구 시스템)")
    logger.info("======================================================")

    python_exe = os.path.join(project_root, "venv", "Scripts", "python.exe")
    if not os.path.exists(python_exe):
        for candidate in [os.path.join("venv", "bin", "python3"), os.path.join("venv", "bin", "python")]:
            full_path = os.path.join(project_root, candidate)
            if os.path.exists(full_path):
                python_exe = full_path
                break
        else:
            python_exe = sys.executable

    if sys.platform == "win32":
        pythonw_candidate = os.path.join(os.path.dirname(python_exe), "pythonw.exe")
        if os.path.exists(pythonw_candidate):
            python_exe = pythonw_candidate

    main_script = os.path.join(project_root, "src", "main_upbit.py")
    hb_file = os.path.join(project_root, "data", "upbit", ".heartbeat")

    lock_file_path = os.path.join(project_root, "data", "upbit", ".watchdog.lock")
    os.makedirs(os.path.dirname(lock_file_path), exist_ok=True)
    lock_file = acquire_single_owner_lock(lock_file_path)
    if lock_file is None:
        logger.warning("⚠️ 이미 실행 중인 업비트 워치독 인스턴스가 존재합니다. 중복 실행을 방지하고 종료합니다.")
        sys.exit(0)

    recent_crashes: list[float] = []
    is_terminating = False
    current_process = None

    def _sig_handler(sig, frame):
        nonlocal is_terminating, current_process
        is_terminating = True
        logger.info("🛑 업비트 워치독 종료 시그널 감지. 봇 프로세스를 안전하게 종료합니다.")
        if current_process and current_process.poll() is None:
            try:
                current_process.terminate()
                current_process.wait(timeout=3)
            except Exception:
                try:
                    current_process.kill()
                except Exception:
                    pass
        sys.exit(0)

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _sig_handler)

    while not is_terminating:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logger.info(f"🚀 [업비트 봇 프로세스 시작] {main_script}")

        # 기존 오래된 하트비트 파일 제거
        if os.path.exists(hb_file):
            try:
                os.remove(hb_file)
            except Exception:
                pass

        creationflags = (
            (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000)
            if sys.platform == "win32"
            else 0
        )
        base_name = os.path.splitext(os.path.basename(main_script))[0]
        spawn_log = os.path.join(project_root, "logs", f"{base_name}_spawn.log")
        os.makedirs(os.path.dirname(spawn_log), exist_ok=True)
        f_spawn = open(spawn_log, "a", encoding="utf-8", errors="replace")

        try:
            current_process = subprocess.Popen(
                [python_exe, main_script],
                cwd=project_root,
                creationflags=creationflags,
                stdout=f_spawn,
                stderr=f_spawn,
                stdin=subprocess.DEVNULL,
                close_fds=(sys.platform != "win32"),
            )
            process = current_process
        except Exception as e:
            logger.error(f"업비트 봇 프로세스 실행 실패: {e}")
            time.sleep(5)
            continue

        start_ts = time.time()
        hung_detected = False
        hang_reason = ""

        while process.poll() is None:
            if is_terminating:
                break
            time.sleep(5)

            # 첫 하트비트 부재와 JSON 손상도 정상 상태로 간주하지 않는다.
            elapsed = time.time() - start_ts
            healthy, heartbeat_reason, heartbeat_age = get_heartbeat_health(hb_file)
            should_restart = elapsed > 120.0 and (
                not healthy or heartbeat_age is None or heartbeat_age > 600.0
            )
            if should_restart:
                hang_reason = heartbeat_reason if not healthy else f"하트비트 {heartbeat_age:.0f}초 지연"
                logger.critical("🛑 [Upbit Hang 감지] %s. 프로세스를 강제 재시작합니다.", hang_reason)
                hung_detected = True
                process.terminate()
                try:
                    process.wait(timeout=5)
                except Exception:
                    process.kill()
                break

        if is_terminating:
            try:
                process.terminate()
                process.wait(timeout=3)
            except Exception:
                process.kill()
            break

        return_code = process.poll() if process.poll() is not None else (process.returncode or 0)

        now_ts = time.time()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 사용자의 명시적 워치독 종료 시그널인 경우에만 루프 탈출
        if is_terminating:
            logger.info("✅ 업비트 봇 및 워치독 안전 종료 완료.")
            break

        # 비정상 종료 (returncode != 0 또는 Hang)
        reason_desc = f"무응답/하트비트 이상 감지: {hang_reason}" if hung_detected else f"종료 코드: {return_code}"
        logger.warning(f"⚠️ [업비트 봇 비정상 종료 감지] {reason_desc}")
        recent_crashes.append(now_ts)
        recent_crashes = [t for t in recent_crashes if now_ts - t <= 60]

        # 1분 내 연속 5회 이상 크래시 방어 (Crash-Loop Protection)
        if len(recent_crashes) >= 5:
            alert_msg = (
                f"🚨 <b>[업비트 봇 긴급 알림 - 연속 크래시 감지]</b>\n\n"
                f"• 1분 내 {len(recent_crashes)}회 연속 비정상 종료가 발생했습니다.\n"
                f"• API 키 또는 환경 설정을 점검해 주세요.\n"
                f"• <b>사유:</b> {reason_desc}\n"
                f"• <b>일시:</b> {now_str}\n\n"
                f"⚠️ 무한 재시작을 방지하기 위해 60초간 대기합니다."
            )
            logger.critical(alert_msg)
            send_telegram_alert(alert_msg)
            time.sleep(60)
            recent_crashes.clear()
            continue

        alert_msg = (
            f"⚠️ <b>[업비트 봇 비정상 종료 감지 & 자동 복구]</b>\n\n"
            f"• 업비트 봇 프로세스가 예기치 않게 종료되었습니다.\n"
            f"• <b>사유:</b> <code>{reason_desc}</code>\n"
            f"• <b>일시:</b> {now_str}\n\n"
            f"🔄 <b>5초 후 자동으로 업비트 봇을 재가동합니다...</b>"
        )
        logger.info("텔레그램 긴급 알림 발송 및 5초 후 자동 재시작 대기")
        send_telegram_alert(alert_msg)

        time.sleep(5)


if __name__ == "__main__":
    main()
