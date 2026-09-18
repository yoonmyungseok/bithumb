"""
Telegram Test Morning Report Sender
현재 시점 기준 24시간 롤링 퀀트 결산 브리핑을 텔레그램으로 즉시 테스트 발송
"""

import os
import sys
import datetime
from pathlib import Path
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding='utf-8')

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "src"))

from telegram_alert import TelegramAlert
from confirmed_fill_performance import get_24h_quant_summary, format_24h_quant_telegram_block


def send_test_report(exchange_name: str):
    ex_key = exchange_name.lower()
    print(f"\n==========================================")
    print(f"[{exchange_name.upper()}] 텔레그램 테스트 전송 준비 중...")
    
    # 환경변수 로드
    common_env = ROOT_DIR / ".env"
    ex_env = ROOT_DIR / f".env.{ex_key}"
    
    if common_env.exists():
        load_dotenv(common_env, override=True)
    if ex_env.exists():
        load_dotenv(ex_env, override=True)

    token = os.getenv(f"{ex_key.upper()}_TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv(f"{ex_key.upper()}_TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat_id:
        print(f"❌ {exchange_name} 텔레그램 토큰 또는 Chat ID가 설정되지 않았습니다.")
        return False

    # 24시간 퀀트 성과 분석 데이터 조회
    # 직전 24시간 롤링 퀀트 요약
    quant_summary = get_24h_quant_summary(exchange_name)
    quant_block = format_24h_quant_telegram_block(quant_summary)

    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 모닝 리포트 양식 조립
    msg = (
        f"🧪 <b>[테스트 발송: {exchange_name} AI 퀀트 봇 - 24시간 성과 결산]</b>\n\n"
        f"• <b>총 평가 자산:</b> {quant_summary.get('start_equity', 1200000):,.0f} KRW (추정)\n"
        f"• <b>금일 확정 실현 손익:</b> {quant_summary['realized_pnl_krw']:+,.0f} KRW\n"
        f"{quant_block}\n\n"
        f"• <b>전송 유형:</b> 사용자 수동 테스트 트리거\n"
        f"• <b>기준 일시:</b> {now_str}\n"
        f"• <b>안내:</b> 매일 09:00 KST 정각에 이와 동일한 퀀트 분석 결산이 자동 발송됩니다."
    )

    print("전송할 메시지 내용:")
    print(msg)
    print("\n텔레그램 API로 전송 중...")

    # TelegramAlert 생성 (동기 모드로 전송하여 결과 즉시 확인)
    tg = TelegramAlert(token, chat_id, enable_async=False)
    success = tg.send_message(msg)

    if success:
        print(f"✅ [{exchange_name}] 텔레그램 전송 성공!")
    else:
        print(f"❌ [{exchange_name}] 텔레그램 전송 실패.")

    return success


if __name__ == "__main__":
    target = sys.argv[1].lower() if len(sys.argv) > 1 else "both"

    if target in ["bithumb", "both"]:
        send_test_report("Bithumb")
    if target in ["upbit", "both"]:
        send_test_report("Upbit")
