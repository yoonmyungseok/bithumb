
"""
스마트 자동 재시작 감시자 (Bithumb Watchdog Engine)
- 빗썸 봇 프로세스(src/main.py)를 24시간 실시간 감시하고 비정상 종료 시 텔레그램 알림 및 5초 내 자동 복구
"""

import logging
import os
import sys
from logging.handlers import TimedRotatingFileHandler

from dotenv import load_dotenv

from trading_watchdog import ExchangeWatchdogProfile, TradingBotWatchdog, TradingWatchdogContext

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
LOG_DIR = os.path.join(PROJECT_ROOT, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "watchdog.log")

file_handler = TimedRotatingFileHandler(
    filename=LOG_FILE, when="midnight", interval=1, backupCount=14, encoding="utf-8"
)
file_handler.setFormatter(
    logging.Formatter("[%(asctime)s] [%(levelname)s] [WATCHDOG] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
)

handlers = [file_handler]
if sys.stderr is not None:
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.WARNING)
    stream_handler.setFormatter(
        logging.Formatter("[%(asctime)s] [%(levelname)s] [WATCHDOG] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    handlers.append(stream_handler)

logging.basicConfig(level=logging.INFO, handlers=handlers)
logger = logging.getLogger("Watchdog")

load_dotenv(override=True)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

BITHUMB_WATCHDOG_PROFILE = ExchangeWatchdogProfile(
    exchange_key="bithumb",
    data_dir=DATA_DIR,
    main_script_name="main.py",
    startup_banner_lines=(
        "  Bithumb Bot Smart Watchdog Engine 가동 시작",
        "  (24시간 무중단 감시 및 자동 복구 시스템)",
    ),
    duplicate_instance_warning="⚠️ 이미 실행 중인 빗썸 워치독 인스턴스가 존재합니다. 중복 실행을 방지하고 종료합니다.",
    shutdown_signal_message="🛑 빗썸 워치독 종료 시그널 감지. 봇 프로세스를 안전하게 종료합니다.",
    shutdown_complete_message="✅ 빗썸 봇 및 워치독 안전 종료 완료.",
    process_start_log_label="🚀 [빗썸 봇 프로세스 시작]",
    process_spawn_error_log="빗썸 봇 프로세스 실행 실패",
    hang_detect_log_label="Bithumb Hang 감지",
    abnormal_exit_log_label="⚠️ [빗썸 봇 비정상 종료 감지]",
    crash_loop_alert_title="빗썸 봇 긴급 알림 - 연속 크래시 감지",
    crash_recovery_alert_title="빗썸 봇 비정상 종료 감지 & 자동 복구",
    crash_recovery_process_line="업비트 봇 프로세스가 예기치 않게 종료되었습니다.",
    crash_restart_label="빗썸 봇",
    duplicate_lock_exit_delay_sec=3.0,
)


def main():
    watchdog = TradingBotWatchdog(
        BITHUMB_WATCHDOG_PROFILE,
        TradingWatchdogContext(
            logger=logger,
            project_root=PROJECT_ROOT,
            telegram_bot_token=TELEGRAM_BOT_TOKEN,
            telegram_chat_id=TELEGRAM_CHAT_ID,
        ),
    )
    watchdog.run()


if __name__ == "__main__":
    main()
