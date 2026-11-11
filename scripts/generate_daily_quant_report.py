#!/usr/bin/env python3
"""
Daily 24H Quant & Performance Report Generator CLI
수동 실행 및 테스트를 위한 명령행 인터페이스 도구
"""

import argparse
import datetime
import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "src"))

from src.daily_quant_reporter import DailyQuantReportGenerator


def main() -> None:
    parser = argparse.ArgumentParser(description="24시간 롤링 일일 퀀트 매매 성과 분석 보고서 생성기")
    parser.add_argument("--start", help="시작 시각 (YYYY-MM-DD_HH:MM:SS)")
    parser.add_argument("--end", help="종료 시각 (YYYY-MM-DD_HH:MM:SS)")
    args = parser.parse_args()

    start_dt = None
    end_dt = None

    if args.end:
        end_dt = datetime.datetime.strptime(args.end, "%Y-%m-%d_%H:%M:%S")
    if args.start:
        start_dt = datetime.datetime.strptime(args.start, "%Y-%m-%d_%H:%M:%S")

    print(f"🚀 일일 퀀트 성과 분석 보고서 생성 시작...")
    generator = DailyQuantReportGenerator()
    outcome = generator.generate(start_dt=start_dt, end_dt=end_dt)

    if outcome.success:
        print(f"✅ 보고서 생성 성공!")
        print(f"• 공급자: {outcome.used_provider} (모델: {outcome.used_model})")
        print(f"• 저장 경로: {outcome.report_filepath}")
        if outcome.needs_retry:
            print(f"⚠️ 임시 템플릿 발행됨 -> {outcome.retry_delay_minutes}분 후 재시도 필요")
    else:
        print(f"❌ 보고서 생성 실패: {outcome.error_message}")
        sys.exit(1)


if __name__ == "__main__":
    main()
