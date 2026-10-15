"""수동 매수 종목 구글 스프레드시트 5분 동기화 워커 프로세스."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from datetime import datetime

# 프로젝트 루트 및 src 디렉터리 경로 추가
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC_DIR = os.path.join(PROJECT_ROOT, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from dotenv import load_dotenv

# 환경변수 로드
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from sheets.manual_position_collector import ManualPositionCollector
from sheets.sheet_manager import GoogleSheetManager

# 로깅 설정
LOG_DIR = os.path.join(PROJECT_ROOT, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] (SheetsSync) %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "sheets_sync.log"), encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("SheetsSync")


class SheetsSyncWorker:
    """5분 주기 구글 시트 동기화 워커."""

    def __init__(self, interval: int = 300, min_eval: float = 1000.0):
        self.interval = interval
        self.min_eval = min_eval
        self.running = False
        self.collector = ManualPositionCollector(min_eval_krw=self.min_eval)
        self.sheet_manager = GoogleSheetManager()

    def run_once(self) -> int:
        """1회 동기화 실행 (테스트 및 단발성 실행)."""
        logger.info(f"=== [SheetsSync] 수동 종목 동기화 시작 ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')}) ===")
        try:
            positions = self.collector.collect_all_manual_positions()
            logger.info(f"[SheetsSync] 수집된 수동 매수 종목 수: {len(positions)}개")
            for p in positions:
                logger.info(
                    f"  - [{p['exchange']}] {p['currency']}: "
                    f"수량={p['volume']}, 평단={p['avg_buy_price']:,.0f}원, "
                    f"현재가={p['current_price']:,.0f}원, "
                    f"수익률={p['unrealized_pnl_pct']:+.2f}%, "
                    f"평가금={p['eval_amount']:,.0f}원"
                )

            self.sheet_manager.sync_all(positions)
            logger.info("=== [SheetsSync] 구글 스프레드시트 동기화 완료 ===")
            return len(positions)
        except Exception as e:
            logger.error(f"[SheetsSync] 동기화 중 오류 발생: {e}", exc_info=True)
            return 0

    def start_loop(self) -> None:
        """지정된 interval(초) 주기로 무한 루프 실행."""
        self.running = True
        logger.info(f"[SheetsSync] 5분 주기 동기화 데몬 시작 (주기: {self.interval}초)")

        while self.running:
            start_time = time.time()
            self.run_once()

            elapsed = time.time() - start_time
            sleep_time = max(1.0, self.interval - elapsed)

            logger.info(f"[SheetsSync] 다음 동기화까지 {int(sleep_time)}초 대기합니다...")
            # 1초 단위로 sleep하여 종료 시그널에 즉각 반응
            for _ in range(int(sleep_time)):
                if not self.running:
                    break
                time.sleep(1)

        logger.info("[SheetsSync] 워커가 정상 종료되었습니다.")

    def stop(self) -> None:
        """워커 중지."""
        self.running = False


def main() -> None:
    parser = argparse.ArgumentParser(description="수동 매수 종목 구글 스프레드시트 동기화 워커")
    parser.add_argument("--once", action="store_true", help="1회만 실행 후 즉시 종료")
    parser.add_argument("--interval", type=int, default=300, help="동기화 주기(초, 기본값: 300초=5분)")
    parser.add_argument("--min-eval", type=float, default=1000.0, help="최소 평가금액 필터(원, 기본값: 1000원)")
    args = parser.parse_args()

    worker = SheetsSyncWorker(interval=args.interval, min_eval=args.min_eval)

    def sig_handler(signum, frame):
        logger.info(f"[SheetsSync] 종료 시그널({signum}) 수신. 종료 중...")
        worker.stop()

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    if args.once:
        worker.run_once()
    else:
        worker.start_loop()


if __name__ == "__main__":
    main()
