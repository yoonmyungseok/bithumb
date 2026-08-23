import datetime
import logging
import os
import sys
import traceback
from typing import List, Optional

from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv

from bithumb_api import BithumbAPI
from gemini_analyzer import GeminiAnalyzer
from sheets_manager import SheetsManager
from telegram_alert import TelegramAlert

# 로깅 설정 (콘솔 및 타임스탬프)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("TradingBot")

# 환경변수 로드
load_dotenv()

BITHUMB_ACCESS_KEY = os.getenv("BITHUMB_ACCESS_KEY", "")
BITHUMB_SECRET_KEY = os.getenv("BITHUMB_SECRET_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "Bithumb_Trading_Bot")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv(
    "GOOGLE_SERVICE_ACCOUNT_JSON", "config/service_account.json"
)

# 다중 마켓 설정 파싱 (예: "KRW-BTC,KRW-ETH,KRW-SOL")
raw_markets = os.getenv("MARKETS", os.getenv("MARKET", "KRW-BTC"))
MARKETS: List[str] = [m.strip().upper() for m in raw_markets.split(",") if m.strip()]

INTERVAL_MINUTES = int(os.getenv("INTERVAL_MINUTES", "3"))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

# 최소 주문 금액 제한 (빗썸 기준: 최소 5,000 KRW)
MIN_ORDER_KRW = 5000.0


def get_kst_now_str() -> str:
    """현재 한국 표준시 문자열 반환"""
    kst = datetime.timezone(datetime.timedelta(hours=9))
    return datetime.datetime.now(kst).strftime("%Y-%m-%d %H:%M:%S")


def run_cycle():
    """
    다중 마켓(Multi-Market) 자동매매 실행 사이클
    """
    now_str = get_kst_now_str()
    logger.info(f"========== [다중 코인 자동매매 사이클 시작: {now_str}] ==========")
    logger.info(f"감시 마켓 목록: {MARKETS}")

    try:
        bithumb = BithumbAPI(BITHUMB_ACCESS_KEY, BITHUMB_SECRET_KEY)
        sheets = SheetsManager(GOOGLE_SERVICE_ACCOUNT_JSON, GOOGLE_SHEET_NAME)
        telegram = TelegramAlert(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        analyzer = GeminiAnalyzer(GEMINI_API_KEY) if GEMINI_API_KEY else None

        # 마켓별 순회 실행
        for market in MARKETS:
            currency = market.split("-")[-1] if "-" in market else market
            logger.info(f"--- [{market} 분석 및 매매 시작] ---")

            try:
                # 1. 잔고 및 시세 데이터 수집
                balances = bithumb.get_balances()
                krw_available = balances.get("KRW", {}).get("balance", 0.0)
                
                coin_info = balances.get(currency, {"balance": 0.0, "locked": 0.0, "avg_buy_price": 0.0})
                coin_available = coin_info["balance"]
                coin_total = coin_available + coin_info["locked"]
                avg_buy_price = coin_info["avg_buy_price"]

                current_price = bithumb.get_current_price(market)
                candles = bithumb.get_candles(unit=INTERVAL_MINUTES, count=30, market=market)

                logger.info(
                    f"[{market}] 현재가: {current_price:,.2f}원 | 가용 KRW: {krw_available:,.0f}원 | 보유 {currency}: {coin_total:.6f}"
                )

                # 2. Gemini 전략 수립 또는 시트 조회
                if analyzer:
                    strategy = analyzer.analyze(
                        market=market,
                        current_price=current_price,
                        candles=candles,
                        krw_balance=krw_available,
                        coin_balance=coin_available,
                        avg_buy_price=avg_buy_price,
                    )
                    # 구글 시트에 마켓별 전략 실시간 동기화
                    sheets.update_strategy(market, strategy, now_str)
                else:
                    strategy = sheets.get_strategy(market)

                status = strategy.get("status", "PAUSE")
                action = strategy.get("action", "HOLD")
                entry_price = strategy.get("entry_price", current_price)
                target_price = strategy.get("target_price", 0.0)
                stop_loss = strategy.get("stop_loss", 0.0)
                alloc_pct = strategy.get("alloc_pct", 0.3)
                reason = strategy.get("reason", "자동 분석")

                logger.info(f"[{market}] 전략: ACTION={action}, 진입가={entry_price:,.0f}, 목표가={target_price:,.0f}, 손절가={stop_loss:,.0f}, 비중={alloc_pct*100:.0f}%")
                logger.info(f"[{market}] 근거: {reason}")

                if status != "ACTIVE":
                    logger.info(f"[{market}] 상태가 ACTIVE가 아니므로 건너뜁니다. ({status})")
                    continue

                # 3. [최우선 긴급 손절 로직]
                coin_value = coin_total * current_price
                if coin_value >= MIN_ORDER_KRW and stop_loss > 0 and current_price <= stop_loss:
                    logger.warning(
                        f"🚨 [{market} 손절 발생] 현재가({current_price:,.0f}원) <= 손절가({stop_loss:,.0f}원). 전량 시장가 매도!"
                    )

                    # 기존 미체결 주문 취소
                    for order in bithumb.get_open_orders(market):
                        bithumb.cancel_order(order["uuid"])

                    # 시장가 전량 매도
                    order_res = bithumb.create_order(
                        market=market,
                        side="ask",
                        volume=coin_available,
                        ord_type="market",
                    )
                    order_uuid = order_res.get("uuid", "UNKNOWN")

                    telegram.send_message(
                        f"🚨 <b>[{market} 손절 전량 매도 실행]</b>\n"
                        f"• 현재가: {current_price:,.2f} KRW\n"
                        f"• 손절 기준가: {stop_loss:,.2f} KRW\n"
                        f"• 매도 수량: {coin_available:.8f} {currency}\n"
                        f"• 주문 ID: <code>{order_uuid}</code>\n"
                        f"• 일시: {now_str}"
                    )

                    sheets.append_trade_log(
                        {
                            "Timestamp": now_str,
                            "Market": market,
                            "Order_UUID": order_uuid,
                            "Side": "STOP_LOSS",
                            "Order_Type": "MARKET",
                            "Price": current_price,
                            "Volume": f"{coin_available:.8f}",
                            "Total_KRW": int(coin_available * current_price),
                            "Stop_Loss": stop_loss,
                            "Target_Price": target_price,
                            "Current_Balance_KRW": int(krw_available),
                            "Status_Reason": "손절 조건 충족으로 인한 시장가 전량 매도",
                        }
                    )
                    continue

                # 4. [미체결 주문 정리]
                open_orders = bithumb.get_open_orders(market)
                if open_orders:
                    logger.info(f"[{market}] 기존 미체결 주문 {len(open_orders)}건 취소 진행")
                    for order in open_orders:
                        bithumb.cancel_order(order["uuid"])

                # 5. [신규 주문 실행]
                if action == "BUY":
                    # 다중 종목 분할 예산 계산 (종목 수로 원화 분할 후 비중 적용)
                    max_market_budget = krw_available / max(1, len(MARKETS))
                    trade_budget = max_market_budget * alloc_pct
                    order_price = entry_price if entry_price > 0 else current_price

                    # 소액 보정: 가용 잔고가 5,000원 이상인데 비중 계산으로 5,000원 미만이 된 경우 최소 5,000원으로 자동 상향
                    if trade_budget < MIN_ORDER_KRW and max_market_budget >= MIN_ORDER_KRW:
                        trade_budget = min(max_market_budget, MIN_ORDER_KRW)

                    if trade_budget < MIN_ORDER_KRW:
                        logger.warning(
                            f"[{market}] 매수 예산 부족: 요청금액 {trade_budget:,.0f}원 < 최소주문금액 {MIN_ORDER_KRW:,.0f}원"
                        )
                        continue

                    # 거래소 수수료(0.04%~0.25%) 및 절사 오차를 고려하여 99.5%로 안전 마진 적용
                    net_trade_budget = trade_budget * 0.995
                    buy_volume = net_trade_budget / order_price
                    order_res = bithumb.create_order(
                        market=market,
                        side="bid",
                        price=order_price,
                        volume=buy_volume,
                        ord_type="limit",
                    )
                    order_uuid = order_res.get("uuid", "UNKNOWN")

                    telegram.send_message(
                        f"🟢 <b>[{market} 지정가 매수 주문]</b>\n"
                        f"• 주문가: {order_price:,.2f} KRW\n"
                        f"• 수량: {buy_volume:.8f} {currency}\n"
                        f"• 투입금액: {trade_budget:,.0f} KRW (비중: {alloc_pct*100:.0f}%)\n"
                        f"• 목표가: {target_price:,.2f} KRW | 손절가: {stop_loss:,.2f} KRW\n"
                        f"• 분석근거: <i>{reason}</i>\n"
                        f"• 일시: {now_str}"
                    )

                    sheets.append_trade_log(
                        {
                            "Timestamp": now_str,
                            "Market": market,
                            "Order_UUID": order_uuid,
                            "Side": "BUY",
                            "Order_Type": "LIMIT",
                            "Price": order_price,
                            "Volume": f"{buy_volume:.8f}",
                            "Total_KRW": int(trade_budget),
                            "Stop_Loss": stop_loss,
                            "Target_Price": target_price,
                            "Current_Balance_KRW": int(krw_available - trade_budget),
                            "Status_Reason": f"정상 매수 접수 ({reason})",
                        }
                    )

                elif action == "SELL":
                    sell_volume = coin_available * alloc_pct
                    order_price = target_price if target_price > 0 else current_price
                    estimated_krw = sell_volume * order_price

                    if estimated_krw < MIN_ORDER_KRW:
                        logger.warning(
                            f"[{market}] 매도 가치 부족: {estimated_krw:,.0f}원 < 최소주문금액 {MIN_ORDER_KRW:,.0f}원"
                        )
                        continue

                    order_res = bithumb.create_order(
                        market=market,
                        side="ask",
                        price=order_price,
                        volume=sell_volume,
                        ord_type="limit",
                    )
                    order_uuid = order_res.get("uuid", "UNKNOWN")

                    telegram.send_message(
                        f"🔴 <b>[{market} 지정가 매도 주문]</b>\n"
                        f"• 주문가: {order_price:,.2f} KRW\n"
                        f"• 매도수량: {sell_volume:.8f} {currency}\n"
                        f"• 예상금액: {estimated_krw:,.0f} KRW (비중: {alloc_pct*100:.0f}%)\n"
                        f"• 분석근거: <i>{reason}</i>\n"
                        f"• 일시: {now_str}"
                    )

                    sheets.append_trade_log(
                        {
                            "Timestamp": now_str,
                            "Market": market,
                            "Order_UUID": order_uuid,
                            "Side": "SELL",
                            "Order_Type": "LIMIT",
                            "Price": order_price,
                            "Volume": f"{sell_volume:.8f}",
                            "Total_KRW": int(estimated_krw),
                            "Stop_Loss": stop_loss,
                            "Target_Price": target_price,
                            "Current_Balance_KRW": int(krw_available),
                            "Status_Reason": f"정상 매도 접수 ({reason})",
                        }
                    )

                elif action == "HOLD":
                    logger.info(f"[{market}] HOLD (관망) - 사유: {reason}")

            except Exception as e:
                logger.error(f"[{market}] 처리 중 오류 발생: {e}")

    except Exception as e:
        error_msg = traceback.format_exc()
        logger.error(f"전체 사이클 실행 중 에러:\n{error_msg}")
        try:
            telegram = TelegramAlert(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
            telegram.send_message(
                f"❌ <b>[멀티봇 시스템 오류]</b>\n"
                f"• 시간: {now_str}\n"
                f"• 에러 내용: <code>{str(e)}</code>"
            )
        except Exception:
            pass


def main():
    logger.info("🚀 빗썸 API 2.0 & 구글 스프레드시트 멀티코인 무인 자동매매 봇 시작")
    logger.info(f"실행 주기: {INTERVAL_MINUTES}분 | 대상 마켓: {MARKETS} | Gemini AI: {'ON' if GEMINI_API_KEY else 'OFF'}")

    try:
        run_cycle()
    except Exception as e:
        logger.error(f"최초 실행 실패: {e}")

    scheduler = BlockingScheduler(timezone="Asia/Seoul")
    scheduler.add_job(
        run_cycle,
        "interval",
        minutes=INTERVAL_MINUTES,
        id="trading_cycle_job",
        replace_existing=True,
    )

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("자동매매 봇이 정상적으로 종료되었습니다.")


if __name__ == "__main__":
    main()
