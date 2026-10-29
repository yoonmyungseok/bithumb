#!/usr/bin/env python3
"""수동 매수 종목 24시간 시세 분석 보고서 생성 CLI 스크립트.

사용 예시:
  python scripts/generate_24h_reports.py --exchange all
  python scripts/generate_24h_reports.py --exchange upbit --symbol HOLO
  python scripts/generate_24h_reports.py --exchange bithumb --symbol US
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

# 프로젝트 루트 경로 등록
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from dotenv import load_dotenv

# 공통 및 거래소별 환경변수 로드
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
load_dotenv(os.path.join(PROJECT_ROOT, ".env.upbit"))
load_dotenv(os.path.join(PROJECT_ROOT, ".env.bithumb"))

from src.analysis_24h_generator import Analysis24hGenerator


def main() -> int:
    parser = argparse.ArgumentParser(description="수동 매수 종목 24시간 Groq 분석 보고서 생성 도구")
    parser.add_argument(
        "--exchange",
        choices=["upbit", "bithumb", "all"],
        default="all",
        help="대상 거래소 범위 (기본: all)",
    )
    parser.add_argument(
        "--symbol",
        type=str,
        default=None,
        help="특정 종목 심볼 (예: HOLO, RSR, US) - 지정하지 않으면 해당 거래소의 전체 수동 종목",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="보고서 저장 경로 (기본: 프로젝트 루트 '24h')",
    )
    parser.add_argument(
        "--min-eval",
        type=float,
        default=1000.0,
        help="최소 평가금액(KRW) 필터 (기본: 1000.0)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="디버그 로그 출력",
    )

    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    generator = Analysis24hGenerator(
        exchange_scope=args.exchange,
        output_dir=args.output_dir,
        min_eval_krw=args.min_eval,
    )

    try:
        saved_files = generator.run(target_currency=args.symbol)
        if saved_files:
            print(f"\n🎉 성공적으로 {len(saved_files)}개의 보고서가 생성되었습니다:")
            for path in saved_files:
                print(f"  - {path}")
            return 0
        else:
            print("\nℹ️ 생성된 보고서가 없습니다 (수동 매수 종목 없음 또는 생성 실패).")
            return 0
    except Exception as e:
        logging.error(f"실행 중 치명적 오류: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
