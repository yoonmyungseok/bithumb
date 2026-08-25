"""
스마트 자동 재시작 감시자 (Watchdog Engine)
- 봇 프로세스를 24시간 실시간 감시하고 비정상 종료 시 텔레그램 알림 및 5초 내 자동 복구
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

# UTF-8 출력 보장
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 1. 로깅 설정 (콘솔 + logs/watchdog.log 파일 기록)
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
log_dir = os.path.join(project_root, "logs")
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, "watchdog.log")

file_handler = TimedRotatingFileHandler(
    filename=log_file, when="midnight", interval=1, backupCount=14, encoding="utf-8"
)
file_handler.setFormatter(
    logging.Formatter("[%(asctime)s] [WATCHDOG] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
)

handlers = [file_handler]
if sys.stderr is not None:
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(
        logging.Formatter("[%(asctime)s] [WATCHDOG] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    handlers.append(stream_handler)

logging.basicConfig(
    level=logging.INFO,
    handlers=handlers,
)
logger = logging.getLogger("Watchdog")

# 2. 환경변수 로드
load_dotenv(override=True)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


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
    """운영체제 파일 잠금으로 빗썸 워치독 중복 실행을 원천 차단한다."""
    lock_file = open(lock_file_path, "a+", encoding="utf-8")
    lock_file.seek(0)
    if not lock_file.read(1):
        lock_file.seek(0)
        lock_file.write("0")
        lock_file.flush()
    try:
        if sys.platform == "win32":
            import msvcrt
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        return None

    # 운영 장애 분석을 위해 잠금 소유 프로세스와 시작 토큰을 별도로 남긴다.
    with open(f"{lock_file_path}.owner.json", "w", encoding="utf-8") as owner_file:
        json.dump({"pid": os.getpid(), "started_at": time.time(), "start_token": uuid.uuid4().hex}, owner_file)
    return lock_file


def main():
    logger.info("======================================================")
    logger.info("  Bithumb Bot Smart Watchdog Engine 가동 시작")
    logger.info("  (24시간 무중단 감시 및 자동 복구 시스템)")
    logger.info("======================================================")

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    python_exe = os.path.join(project_root, "venv", "Scripts", "python.exe")
    if not os.path.exists(python_exe):
        python_exe = sys.executable

    main_script = os.path.join(project_root, "src", "main.py")
    hb_file = os.path.join(project_root, "data", ".heartbeat")

    lock_file_path = os.path.join(project_root, "data", ".watchdog.lock")
    os.makedirs(os.path.dirname(lock_file_path), exist_ok=True)
    lock_file = acquire_single_owner_lock(lock_file_path)
    if lock_file is None:
        logger.warning("⚠️ 이미 실행 중인 빗썸 워치독 인스턴스가 존재합니다. 중복 실행을 방지하고 종료합니다.")
        sys.exit(0)

    recent_crashes: list[float] = []
    is_terminating = False

    def _sig_handler(sig, frame):
        nonlocal is_terminating
        is_terminating = True
        logger.info("🛑 워치독 종료 시그널 감지. 봇 프로세스를 안전하게 종료합니다.")
        sys.exit(0)

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _sig_handler)

    while not is_terminating:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logger.info(f"🚀 [봇 프로세스 시작] {main_script}")

        # 기존 오래된 하트비트 파일 제거
        if os.path.exists(hb_file):
            try:
                os.remove(hb_file)
            except Exception:
                pass

        try:
            process = subprocess.Popen(
                [python_exe, main_script],
                cwd=project_root,
            )
        except Exception as e:
            logger.error(f"봇 프로세스 실행 실패: {e}")
            time.sleep(5)
            continue

        start_ts = time.time()
        hung_detected = False

        while process.poll() is None:
            if is_terminating:
                break
            time.sleep(5)

            # 프로세스 시작 후 2분이 경과한 시점부터 하트비트 타임아웃 감시 (최대 10분 허용)
            if time.time() - start_ts > 120.0 and os.path.exists(hb_file):
                try:
                    with open(hb_file, "r", encoding="utf-8") as hbf:
                        import json
                        hb_data = json.load(hbf)
                        last_hb = float(hb_data.get("timestamp", 0.0))
                        if last_hb > 0 and (time.time() - last_hb) > 600.0:  # 10분 무응답
                            logger.critical(f"🛑 [Hang 감지] 봇 프로세스가 10분 이상 무응답 상태입니다. 프로세스를 강제 재시작합니다.")
                            hung_detected = True
                            process.terminate()
                            try:
                                process.wait(timeout=5)
                            except Exception:
                                process.kill()
                            break
                except Exception as e:
                    logger.debug(f"하트비트 검사 예외 (무시): {e}")

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

        # 정상 종료 (returncode == 0)이고 Hang이 아니었던 경우
        if return_code == 0 and not hung_detected:
            logger.info("✅ 봇 프로세스가 정상 종료되었습니다.")
            break

        # 비정상 종료 (returncode != 0 또는 Hang)
        reason_desc = "10분 이상 응답 없음(Hang/Deadlock 감지)" if hung_detected else f"종료 코드: {return_code}"
        logger.warning(f"⚠️ [비정상 종료 감지] {reason_desc}")
        recent_crashes.append(now_ts)
        recent_crashes = [t for t in recent_crashes if now_ts - t <= 60]

        # 1분 내 연속 5회 이상 크래시 방어 (Crash-Loop Protection)
        if len(recent_crashes) >= 5:
            alert_msg = (
                f"🚨 <b>[빗썸 봇 긴급 알림 - 연속 크래시 감지]</b>\n\n"
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
            f"⚠️ <b>[빗썸 봇 비정상 종료 감지 & 자동 복구]</b>\n\n"
            f"• 봇 프로세스가 예기치 않게 종료되었습니다.\n"
            f"• <b>사유:</b> <code>{reason_desc}</code>\n"
            f"• <b>일시:</b> {now_str}\n\n"
            f"🔄 <b>5초 후 자동으로 봇을 재가동합니다...</b>"
        )
        logger.info("텔레그램 긴급 알림 발송 및 5초 후 자동 재시작 대기")
        send_telegram_alert(alert_msg)

        time.sleep(5)


if __name__ == "__main__":
    main()
