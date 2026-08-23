import datetime
import logging
import os
import sys
import traceback
from typing import Dict, List, Optional, Set, Tuple

from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv

from bithumb_api import BithumbAPI
from gemini_analyzer import GeminiAnalyzer
from market_screener import MarketScreener
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

# 마켓 모드 설정 (AUTO: 실시간 급등주 자동 탐색 / 고정 목록: KRW-BTC,KRW-ETH 등)
RAW_MARKETS = os.getenv("MARKETS", "AUTO").strip()
IS_AUTO_MODE = RAW_MARKETS.upper() == "AUTO"

TOP_COUNT = int(os.getenv("TOP_COUNT", "3"))
MIN_TRADE_VALUE = float(os.getenv("MIN_TRADE_VALUE", "5000000000"))  # 최소 50억 원
MIN_CHANGE_RATE = float(os.getenv("MIN_CHANGE_RATE", "0.01"))        # 최소 +1.0%
MAX_CHANGE_RATE = float(os.getenv("MAX_CHANGE_RATE", "0.25"))        # 최대 +25.0%

INTERVAL_MINUTES = int(os.getenv("INTERVAL_MINUTES", "5"))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

# ==========================================
# 🛡️ 1 & 2대 리스크 관리 파라미터
# ==========================================
# [리스크 1] 비트코인 급락 감지 임계치 (최근 3개봉 동안 -1.5% 이상 하락 시 알트코인 신규 매수 전면 차단)
BTC_CRASH_THRESHOLD_PCT = float(os.getenv("BTC_CRASH_THRESHOLD_PCT", "0.015"))

# [리스크 2] 일일 최대 손실 한도 킬 스위치 (당일 총 자산 손실률 -5.0% 도달 시 당일 모든 신규 매매 강제 중단)
MAX_DAILY_LOSS_PCT = float(os.getenv("MAX_DAILY_LOSS_PCT", "0.05"))

# ==========================================
# 🎯 트레일링 스탑(Trailing Stop) 설정
# ==========================================
# 트레일링 스탑 발동 최소 수익률 (기본: +2.0% 도달 시 추적 시작)
TRAILING_START_PCT = float(os.getenv("TRAILING_START_PCT", "0.02"))

# 최고점 대비 하락 시 즉시 익절 비율 (기본: 최고점에서 -1.2% 하락 시 익절)
TRAILING_STOP_PCT = float(os.getenv("TRAILING_STOP_PCT", "0.012"))

# 최소 주문 금액 제한 (빗썸 기준: 최소 5,000 KRW)
MIN_ORDER_KRW = 5000.0


class TrailingStopTracker:
    """
    고점 추적형 트레일링 스탑(Trailing Stop) 관리자
    - 수익률이 +TRAILING_START_PCT 이상 올라가면 최고점(Peak)을 실시간 추적
    - 최고점 대비 -TRAILING_STOP_PCT 만큼 꺾여 내려오는 순간 즉시 시장가 익절
    """

    def __init__(self, start_profit_pct: float = 0.02, trailing_drop_pct: float = 0.012):
        self.start_profit_pct = start_profit_pct
        self.trailing_drop_pct = trailing_drop_pct
        self.peaks: Dict[str, float] = {}  # market -> peak_price

    def check_trailing_stop(
        self, market: str, current_price: float, avg_buy_price: float
    ) -> Tuple[bool, float, float, float, float]:
        """
        트레일링 스탑 조건 검사
        반환: (발동여부, 최고점가격, 트레일링익절가, 최고수익률%, 실현수익률%)
        """
        if avg_buy_price <= 0:
            return False, 0.0, 0.0, 0.0, 0.0

        current_profit_rate = (current_price - avg_buy_price) / avg_buy_price

        # 1. 최소 활성화 수익률(예: +2.0%) 이상 도달 시 고점 추적 모드 진입
        if current_profit_rate >= self.start_profit_pct:
            previous_peak = self.peaks.get(market, avg_buy_price)
            current_peak = max(previous_peak, current_price)
            self.peaks[market] = current_peak

            peak_profit_pct = ((current_peak - avg_buy_price) / avg_buy_price) * 100.0

            # 트레일링 익절 라인 = 최고점 * (1 - trailing_drop_pct)
            trailing_stop_price = current_peak * (1.0 - self.trailing_drop_pct)

            # 안전 보장: 최소 평단가 대비 +0.5% 이상(수수료 차감 후 무조건 플러스) 보장
            min_guaranteed_profit = avg_buy_price * 1.005
            trailing_stop_price = max(trailing_stop_price, min_guaranteed_profit)

            logger.info(
                f"🎯 [{market}] 트레일링 추적 중: 최고점 {current_peak:,.2f}원 (+{peak_profit_pct:.2f}%) ➜ 익절기준선 {trailing_stop_price:,.2f}원"
            )

            # 현재 가격이 트레일링 기준선 이하로 꺾였을 때 익절 트리거!
            if current_price <= trailing_stop_price:
                realized_profit_pct = ((current_price - avg_buy_price) / avg_buy_price) * 100.0
                self.peaks.pop(market, None)  # 매도 후 초기화
                return True, current_peak, trailing_stop_price, peak_profit_pct, realized_profit_pct

        return False, self.peaks.get(market, current_price), 0.0, 0.0, current_profit_rate * 100.0

    def clear(self, market: str):
        self.peaks.pop(market, None)


trailing_tracker = TrailingStopTracker(
    start_profit_pct=TRAILING_START_PCT, trailing_drop_pct=TRAILING_STOP_PCT
)


class DailyRiskManager:
    """
    일일 손익 추적 및 킬 스위치(Kill-Switch) 관리자
    """

    def __init__(self, max_loss_pct: float = 0.05):
        self.max_loss_pct = max_loss_pct
        self.current_date_str = ""
        self.daily_start_equity = 0.0
        self.kill_switch_active = False

    def update_daily_equity(self, current_total_equity: float, now_kst: datetime.datetime) -> Tuple[bool, float]:
        """
        일일 손익률 계산 및 킬스위치 발동 여부 반환 (발동여부, 당일손익률)
        """
        date_key = (now_kst - datetime.timedelta(hours=9)).strftime("%Y-%m-%d")

        if date_key != self.current_date_str:
            self.current_date_str = date_key
            self.daily_start_equity = current_total_equity
            self.kill_switch_active = False
            logger.info(f"📅 [일일 손익 기준일 갱신: {date_key}] 시작 총 자산: {self.daily_start_equity:,.0f}원")

        if self.daily_start_equity <= 0:
            self.daily_start_equity = current_total_equity

        daily_pnl_pct = (
            (current_total_equity - self.daily_start_equity) / self.daily_start_equity
            if self.daily_start_equity > 0
            else 0.0
        )

        if daily_pnl_pct <= -self.max_loss_pct:
            if not self.kill_switch_active:
                self.kill_switch_active = True
                logger.warning(
                    f"🛑 [일일 킬 스위치 발동!] 당일 손실률({daily_pnl_pct*100:.2f}%)이 한도(-{self.max_loss_pct*100:.1f}%)를 초과했습니다."
                )
        else:
            self.kill_switch_active = False

        return self.kill_switch_active, daily_pnl_pct


risk_manager = DailyRiskManager(max_loss_pct=MAX_DAILY_LOSS_PCT)


def get_kst_now() -> datetime.datetime:
    """현재 한국 표준시 datetime 객체 반환"""
    kst = datetime.timezone(datetime.timedelta(hours=9))
    return datetime.datetime.now(kst)


def get_kst_now_str() -> str:
    """현재 한국 표준시 문자열 반환"""
    return get_kst_now().strftime("%Y-%m-%d %H:%M:%S")


def calculate_total_equity(balances: Dict[str, Dict[str, float]], bithumb: BithumbAPI) -> float:
    """
    현재 계좌의 총 평가 자산(원화 + 보유 코인 평가액 합산) 계산
    """
    krw_balance = balances.get("KRW", {}).get("balance", 0.0) + balances.get("KRW", {}).get("locked", 0.0)
    total_coin_val = 0.0

    for cur, info in balances.items():
        if cur == "KRW":
            continue
        vol = info.get("balance", 0.0) + info.get("locked", 0.0)
        if vol > 0:
            try:
                price = bithumb.get_current_price(f"KRW-{cur}")
                total_coin_val += vol * price
            except Exception:
                pass

    return krw_balance + total_coin_val


def get_held_markets(balances: Dict[str, Dict[str, float]], bithumb: BithumbAPI) -> List[str]:
    """
    현재 계좌에서 평가금액 4,000원 이상 보유 중인 코인 마켓 목록 추출
    """
    held = []
    for cur, info in balances.items():
        if cur == "KRW":
            continue
        total_vol = info.get("balance", 0.0) + info.get("locked", 0.0)
        if total_vol > 0:
            market = f"KRW-{cur}"
            try:
                price = bithumb.get_current_price(market)
                if total_vol * price >= 4000.0:
                    held.append(market)
            except Exception:
                pass
    return held


def check_btc_market_crash(bithumb: BithumbAPI) -> Tuple[bool, str]:
    """
    [리스크 1] 비트코인(BTC) 대세 급락 상태 검사
    """
    try:
        btc_candles = bithumb.get_candles(unit=INTERVAL_MINUTES, count=6, market="KRW-BTC")
        if not btc_candles or len(btc_candles) < 3:
            return False, "BTC 데이터 정상"

        current_btc = float(btc_candles[0].get("trade_price", 0.0))
        past_btc = float(btc_candles[min(2, len(btc_candles) - 1)].get("opening_price", current_btc))

        drop_rate = (current_btc - past_btc) / past_btc if past_btc > 0 else 0.0

        if drop_rate <= -BTC_CRASH_THRESHOLD_PCT:
            return True, f"비트코인(BTC) 단기 급락세 감지 ({drop_rate*100:.2f}%)"

        return False, f"BTC 안정세 ({drop_rate*100:+.2f}%)"

    except Exception as e:
        logger.warning(f"BTC 시장 상태 검사 실패: {e}")
        return False, "BTC 검사 오류"


def run_cycle():
    """
    실시간 급등주 탐색 + AI 퀀트 분석 + 트레일링 스탑 + 리스크 관리(킬스위치 & BTC급락필터) + 자동매매 실행 사이클
    """
    now_kst = get_kst_now()
    now_str = now_kst.strftime("%Y-%m-%d %H:%M:%S")
    logger.info(f"========== [자동매매 사이클 시작: {now_str}] ==========")

    try:
        bithumb = BithumbAPI(BITHUMB_ACCESS_KEY, BITHUMB_SECRET_KEY)
        sheets = SheetsManager(GOOGLE_SERVICE_ACCOUNT_JSON, GOOGLE_SHEET_NAME)
        telegram = TelegramAlert(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        analyzer = GeminiAnalyzer(GEMINI_API_KEY) if GEMINI_API_KEY else None

        # 1. 총 자산 평가 및 [리스크 2: 일일 킬 스위치] 점검
        balances = bithumb.get_balances()
        current_total_equity = calculate_total_equity(balances, bithumb)
        is_kill_switch, daily_pnl = risk_manager.update_daily_equity(current_total_equity, now_kst)

        krw_available = balances.get("KRW", {}).get("balance", 0.0)
        held_markets = get_held_markets(balances, bithumb)

        logger.info(
            f"💰 총 평가 자산: {current_total_equity:,.0f}원 (가용 원화: {krw_available:,.0f}원) | 당일 손익률: {daily_pnl*100:+.2f}%"
        )

        if is_kill_switch:
            logger.warning(f"🛑 [일일 킬 스위치 작동 중] 당일 신규 매수를 전면 차단합니다 (손실률: {daily_pnl*100:.2f}%).")
            telegram.send_message(
                f"🛑 <b>[일일 킬 스위치 발동 중]</b>\n"
                f"• 당일 손실률: <b>{daily_pnl*100:.2f}%</b> (한도: -{MAX_DAILY_LOSS_PCT*100:.1f}%)\n"
                f"• 총 평가 자산: {current_total_equity:,.0f} KRW\n"
                f"• 원금 보호를 위해 당일 모든 신규 매수를 중단하고 관망합니다."
            )

        # 2. [리스크 1: 비트코인(BTC) 대세 급락 필터] 점검
        is_btc_crashing, btc_status_msg = check_btc_market_crash(bithumb)
        if is_btc_crashing:
            logger.warning(f"⚠️ [BTC 급락 방어선 작동] {btc_status_msg} ➜ 알트코인 신규 매수 차단!")

        # 3. 거래 대상 마켓 결정 (동적 스크리닝 vs 고정 목록)
        target_markets: List[str] = []
        if IS_AUTO_MODE:
            screener = MarketScreener(
                bithumb,
                min_trade_value_krw=MIN_TRADE_VALUE,
                min_change_rate=MIN_CHANGE_RATE,
                max_change_rate=MAX_CHANGE_RATE,
            )
            screened_items = screener.scan_markets(
                top_count=TOP_COUNT, held_markets=held_markets
            )
            target_markets = [item["market"] for item in screened_items]
        else:
            fixed_list = [m.strip().upper() for m in RAW_MARKETS.split(",") if m.strip()]
            target_markets = list(dict.fromkeys(held_markets + fixed_list))

        logger.info(f"이번 사이클 최종 분석 대상 마켓 ({len(target_markets)}개): {target_markets}")

        # 4. 마켓별 순회 분석 및 매매 실행
        for market in target_markets:
            currency = market.split("-")[-1] if "-" in market else market
            logger.info(f"--- [{market} AI 퀀트 분석 시작] ---")

            try:
                # 잔고 및 시세 데이터 갱신
                balances = bithumb.get_balances()
                krw_available = balances.get("KRW", {}).get("balance", 0.0)

                coin_info = balances.get(currency, {"balance": 0.0, "locked": 0.0, "avg_buy_price": 0.0})
                coin_available = coin_info["balance"]
                coin_total = coin_available + coin_info["locked"]
                avg_buy_price = coin_info["avg_buy_price"]

                current_price = bithumb.get_current_price(market)
                if current_price <= 0:
                    continue

                candles = bithumb.get_candles(unit=INTERVAL_MINUTES, count=30, market=market)

                logger.info(
                    f"[{market}] 현재가: {current_price:,.2f}원 | 가용 KRW: {krw_available:,.0f}원 | 보유 {currency}: {coin_total:.6f} (평단: {avg_buy_price:,.2f}원)"
                )

                # =========================================================================
                # 🎯 [최우선 1: 트레일링 스탑(Trailing Stop) 익절 검사]
                # =========================================================================
                coin_value = coin_total * current_price
                if coin_value >= MIN_ORDER_KRW and avg_buy_price > 0:
                    is_trailing_hit, peak_p, trail_stop_p, peak_profit_pct, realized_profit_pct = (
                        trailing_tracker.check_trailing_stop(market, current_price, avg_buy_price)
                    )

                    if is_trailing_hit:
                        logger.info(
                            f"🎯 [{market} 트레일링 스탑 익절 발동] 최고점 {peak_p:,.2f}원(+{peak_profit_pct:.2f}%) ➜ 현재 {current_price:,.2f}원(+{realized_profit_pct:.2f}%). 전량 시장가 익절!"
                        )

                        for order in bithumb.get_open_orders(market):
                            bithumb.cancel_order(order["uuid"])

                        order_res = bithumb.create_order(
                            market=market,
                            side="ask",
                            volume=coin_available,
                            ord_type="market",
                        )
                        order_uuid = order_res.get("uuid", "UNKNOWN")

                        telegram.send_message(
                            f"🎯 <b>[{market} 트레일링 스탑 최고점 익절 완료!]</b>\n"
                            f"• 진입 평단가: {avg_buy_price:,.2f} KRW\n"
                            f"• 도달 최고가: {peak_p:,.2f} KRW (+{peak_profit_pct:.2f}%)\n"
                            f"• 익절 체결가: {current_price:,.2f} KRW\n"
                            f"• <b>실현 수익률: +{realized_profit_pct:.2f}% 🚀</b>\n"
                            f"• 매도 수량: {coin_available:.8f} {currency}\n"
                            f"• 주문 ID: <code>{order_uuid}</code>\n"
                            f"• 일시: {now_str}"
                        )

                        sheets.append_trade_log(
                            {
                                "Timestamp": now_str,
                                "Market": market,
                                "Order_UUID": order_uuid,
                                "Side": "TRAILING_STOP",
                                "Order_Type": "MARKET",
                                "Price": current_price,
                                "Volume": f"{coin_available:.8f}",
                                "Total_KRW": int(coin_available * current_price),
                                "Stop_Loss": avg_buy_price,
                                "Target_Price": peak_p,
                                "Current_Balance_KRW": int(krw_available + (coin_available * current_price)),
                                "Status_Reason": f"트레일링 스탑 최고점 익절 (+{realized_profit_pct:.2f}%)",
                            }
                        )
                        continue

                # AI 전략 수립 또는 시트 조회
                if analyzer:
                    strategy = analyzer.analyze(
                        market=market,
                        current_price=current_price,
                        candles=candles,
                        krw_balance=krw_available,
                        coin_balance=coin_available,
                        avg_buy_price=avg_buy_price,
                    )
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

                logger.info(f"[{market}] 전략: ACTION={action}, 진입가={entry_price:,.2f}, 목표가={target_price:,.2f}, 손절가={stop_loss:,.2f}, 비중={alloc_pct*100:.0f}%")
                logger.info(f"[{market}] 근거: {reason}")

                if status != "ACTIVE":
                    logger.info(f"[{market}] 상태가 ACTIVE가 아니므로 건너뜁니다. ({status})")
                    continue

                # =========================================================================
                # 🚨 [최우선 2: 긴급 손절 로직]
                # =========================================================================
                if coin_value >= MIN_ORDER_KRW and stop_loss > 0 and current_price <= stop_loss:
                    logger.warning(
                        f"🚨 [{market} 손절 발생] 현재가({current_price:,.2f}원) <= 손절가({stop_loss:,.2f}원). 전량 시장가 매도!"
                    )

                    trailing_tracker.clear(market)

                    for order in bithumb.get_open_orders(market):
                        bithumb.cancel_order(order["uuid"])

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

                # 6. [미체결 주문 정리]
                open_orders = bithumb.get_open_orders(market)
                if open_orders:
                    logger.info(f"[{market}] 기존 미체결 주문 {len(open_orders)}건 취소 진행")
                    for order in open_orders:
                        bithumb.cancel_order(order["uuid"])

                # 7. [신규 주문 실행]
                if action == "BUY":
                    if is_kill_switch:
                        logger.info(f"[{market}] 킬 스위치 발동 상태이므로 신규 매수를 건너뜁니다.")
                        continue

                    if market != "KRW-BTC" and is_btc_crashing:
                        logger.warning(f"[{market}] 비트코인 급락세({btc_status_msg})로 인해 알트코인 매수를 방어적으로 차단합니다.")
                        telegram.send_message(
                            f"⚠️ <b>[{market} 매수 차단 - BTC 급락 방어]</b>\n"
                            f"• 사유: <i>{btc_status_msg}</i>\n"
                            f"• 대장주(BTC) 급락으로 인한 알트코인 동반 폭락 위험 방지"
                        )
                        continue

                    num_slots = max(1, len(target_markets) - len(held_markets) + 1)
                    max_market_budget = krw_available / num_slots
                    trade_budget = max_market_budget * alloc_pct
                    order_price = entry_price if entry_price > 0 else current_price

                    if trade_budget < MIN_ORDER_KRW and max_market_budget >= MIN_ORDER_KRW:
                        trade_budget = min(max_market_budget, MIN_ORDER_KRW)

                    if trade_budget < MIN_ORDER_KRW:
                        logger.warning(
                            f"[{market}] 매수 예산 부족: 요청금액 {trade_budget:,.0f}원 < 최소주문금액 {MIN_ORDER_KRW:,.0f}원"
                        )
                        continue

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
                        f"🟢 <b>[{market} 급등 포착 지정가 매수 주문]</b>\n"
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

                    trailing_tracker.clear(market)

                    order_res = bithumb.create_order(
                        market=market,
                        side="ask",
                        price=order_price,
                        volume=sell_volume,
                        ord_type="limit",
                    )
                    order_uuid = order_res.get("uuid", "UNKNOWN")

                    telegram.send_message(
                        f"🔴 <b>[{market} 지정가 익절 매도 주문]</b>\n"
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
                f"❌ <b>[시스템 오류 알림]</b>\n"
                f"• 시간: {now_str}\n"
                f"• 에러 내용: <code>{str(e)}</code>"
            )
        except Exception:
            pass


def main():
    logger.info("🚀 빗썸 API 2.0 트레일링 스탑 & 급등주 AI 퀀트 봇 시작")
    mode_text = f"실시간 급등주 자동 스캔 (상위 {TOP_COUNT}종목)" if IS_AUTO_MODE else f"고정 마켓 ({RAW_MARKETS})"
    logger.info(
        f"실행 주기: {INTERVAL_MINUTES}분 | 모드: {mode_text} | 트레일링 시작: +{TRAILING_START_PCT*100:.1f}% (고점대비 -{TRAILING_STOP_PCT*100:.1f}%) | 킬스위치: -{MAX_DAILY_LOSS_PCT*100:.1f}%"
    )

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
