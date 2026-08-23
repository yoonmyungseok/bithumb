import datetime
import json
import logging
import os
import sys
import traceback
from logging.handlers import RotatingFileHandler

import requests
from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv

from bithumb_api import BithumbAPI
from gemini_analyzer import GeminiAnalyzer
from market_screener import MarketScreener
from sheets_manager import SheetsManager
from telegram_alert import TelegramAlert

# 윈도우 cp949 인코딩 에러 방지 (이모지 및 한글 UTF-8 표준화)
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

# 로그 디렉토리 생성 및 로깅 설정 (콘솔 + 파일 동시 기록)
LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "trading.log")

file_handler = RotatingFileHandler(LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
stream_handler = logging.StreamHandler(sys.stdout)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[stream_handler, file_handler],
)
logger = logging.getLogger("TradingBot")

# 환경변수 로드 (override=True로 기존 메모리 캐시 덮어쓰기 보장)
load_dotenv(override=True)

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
BTC_CRASH_THRESHOLD_PCT = float(os.getenv("BTC_CRASH_THRESHOLD_PCT", "0.015"))
MAX_DAILY_LOSS_PCT = float(os.getenv("MAX_DAILY_LOSS_PCT", "0.05"))

# ==========================================
# 🎯 트레일링 스탑(Trailing Stop) 설정
# ==========================================
TRAILING_START_PCT = float(os.getenv("TRAILING_START_PCT", "0.02"))
TRAILING_STOP_PCT = float(os.getenv("TRAILING_STOP_PCT", "0.012"))

# 최소 주문 금액 제한 (빗썸 기준: 최소 5,000 KRW)
MIN_ORDER_KRW = 5000.0


class TrailingStopTracker:
    """
    고점 추적형 트레일링 스탑(Trailing Stop) 관리자
    """

    def __init__(self, start_profit_pct: float = 0.02, trailing_drop_pct: float = 0.012):
        self.start_profit_pct = start_profit_pct
        self.trailing_drop_pct = trailing_drop_pct
        self.peaks: dict[str, float] = {}

    def check_trailing_stop(
        self, market: str, current_price: float, avg_buy_price: float
    ) -> tuple[bool, float, float, float, float]:
        """
        반환: (발동여부, 최고점가격, 트레일링익절가, 최고수익률%, 실현수익률%)
        """
        if avg_buy_price <= 0:
            return False, 0.0, 0.0, 0.0, 0.0

        current_profit_rate = (current_price - avg_buy_price) / avg_buy_price

        if current_profit_rate >= self.start_profit_pct:
            previous_peak = self.peaks.get(market, avg_buy_price)
            current_peak = max(previous_peak, current_price)
            self.peaks[market] = current_peak

            peak_profit_pct = ((current_peak - avg_buy_price) / avg_buy_price) * 100.0
            trailing_stop_price = current_peak * (1.0 - self.trailing_drop_pct)

            # 수수료 차감 후 최소 +0.5% 익절 안전 보장
            min_guaranteed_profit = avg_buy_price * 1.005
            trailing_stop_price = max(trailing_stop_price, min_guaranteed_profit)

            logger.info(
                f"🎯 [{market}] 트레일링 추적 중: 최고점 {current_peak:,.2f}원 (+{peak_profit_pct:.2f}%) ➜ 익절기준선 {trailing_stop_price:,.2f}원"
            )

            if current_price <= trailing_stop_price:
                realized_profit_pct = ((current_price - avg_buy_price) / avg_buy_price) * 100.0
                self.peaks.pop(market, None)
                return True, current_peak, trailing_stop_price, peak_profit_pct, realized_profit_pct

        return False, self.peaks.get(market, current_price), 0.0, 0.0, current_profit_rate * 100.0

    def clear(self, market: str):
        self.peaks.pop(market, None)


trailing_tracker = TrailingStopTracker(
    start_profit_pct=TRAILING_START_PCT, trailing_drop_pct=TRAILING_STOP_PCT
)


class DailyRiskManager:
    """
    일일 손익 추적 및 킬 스위치(Kill-Switch) 관리자 (영구 저장 연동)
    - data/daily_stats.json 파일에 당일 기준 자산 및 확정 실현 손익 저장
    - 프로그램 재시작 시에도 아침 9시 기준 자산 보존
    """

    def __init__(self, max_loss_pct: float = 0.05):
        self.max_loss_pct = max_loss_pct
        self.data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        os.makedirs(self.data_dir, exist_ok=True)
        self.stats_file = os.path.join(self.data_dir, "daily_stats.json")

        self.current_date_str = ""
        self.daily_start_equity = 0.0
        self.realized_pnl_krw = 0.0
        self.kill_switch_active = False
        self.total_trades_today = 0
        self.win_trades_today = 0
        self._load_state()

    def _load_state(self):
        if os.path.exists(self.stats_file):
            try:
                with open(self.stats_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.current_date_str = data.get("date", "")
                    self.daily_start_equity = float(data.get("start_equity", 0.0))
                    self.realized_pnl_krw = float(data.get("realized_pnl_krw", 0.0))
                    self.total_trades_today = int(data.get("total_trades", 0))
                    self.win_trades_today = int(data.get("win_trades", 0))
            except (json.JSONDecodeError, OSError, KeyError, ValueError) as e:
                logger.warning(f"일일 통계 로드 실패: {e}")

    def _save_state(self):
        try:
            with open(self.stats_file, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "date": self.current_date_str,
                        "start_equity": self.daily_start_equity,
                        "realized_pnl_krw": self.realized_pnl_krw,
                        "total_trades": self.total_trades_today,
                        "win_trades": self.win_trades_today,
                    },
                    f,
                    indent=2,
                    ensure_ascii=False,
                )
        except (OSError, TypeError) as e:
            logger.warning(f"일일 통계 저장 실패: {e}")

    def add_realized_trade(self, pnl_krw: float, is_win: bool):
        self.realized_pnl_krw += pnl_krw
        self.total_trades_today += 1
        if is_win:
            self.win_trades_today += 1
        self._save_state()

    def update_daily_equity(self, current_total_equity: float, now_kst: datetime.datetime) -> tuple[bool, float]:
        date_key = (now_kst - datetime.timedelta(hours=9)).strftime("%Y-%m-%d")

        if date_key != self.current_date_str or self.daily_start_equity <= 0:
            if date_key != self.current_date_str:
                self.realized_pnl_krw = 0.0
                self.total_trades_today = 0
                self.win_trades_today = 0
            self.current_date_str = date_key
            self.daily_start_equity = current_total_equity
            self.kill_switch_active = False
            self._save_state()
            logger.info(f"📅 [일일 손익 기준일 갱신: {date_key}] 시작 총 자산: {self.daily_start_equity:,.0f}원")

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
    kst = datetime.timezone(datetime.timedelta(hours=9))
    return datetime.datetime.now(kst)


def get_kst_now_str() -> str:
    return get_kst_now().strftime("%Y-%m-%d %H:%M:%S")


def calculate_total_equity(balances: dict[str, dict[str, float]], bithumb: BithumbAPI) -> float:
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
            except (requests.exceptions.RequestException, KeyError, ValueError):
                pass

    return krw_balance + total_coin_val


def get_held_markets(balances: dict[str, dict[str, float]], bithumb: BithumbAPI) -> list[str]:
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
            except (requests.exceptions.RequestException, KeyError, ValueError):
                pass
    return held


def check_btc_market_crash(bithumb: BithumbAPI, threshold_pct: float = BTC_CRASH_THRESHOLD_PCT) -> tuple[bool, str]:
    try:
        btc_candles = bithumb.get_candles(unit=INTERVAL_MINUTES, count=6, market="KRW-BTC")
        if not btc_candles or len(btc_candles) < 3:
            return False, "BTC 데이터 정상"

        current_btc = float(btc_candles[0].get("trade_price", 0.0))
        past_btc = float(btc_candles[min(2, len(btc_candles) - 1)].get("opening_price", current_btc))

        drop_rate = (current_btc - past_btc) / past_btc if past_btc > 0 else 0.0

        if drop_rate <= -threshold_pct:
            return True, f"비트코인(BTC) 단기 급락세 감지 ({drop_rate*100:.2f}%)"

        return False, f"BTC 안정세 ({drop_rate*100:+.2f}%)"

    except (requests.exceptions.RequestException, KeyError, ValueError, IndexError) as e:
        logger.warning(f"BTC 시장 상태 검사 실패: {e}")
        return False, "BTC 검사 오류"


def send_daily_morning_report():
    """
    매일 아침 09:00 KST 빗썸 일봉 리셋 시 일일 결산 모닝 리포트 전송
    """
    now_str = get_kst_now_str()
    logger.info(f"📊 [아침 9시 일일 결산 브리핑 발송: {now_str}]")

    try:
        bithumb = BithumbAPI(BITHUMB_ACCESS_KEY, BITHUMB_SECRET_KEY)
        telegram = TelegramAlert(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        balances = bithumb.get_balances()
        total_equity = calculate_total_equity(balances, bithumb)
        krw_avail = balances.get("KRW", {}).get("balance", 0.0)

        daily_pnl_pct = (
            (total_equity - risk_manager.daily_start_equity) / risk_manager.daily_start_equity * 100
            if risk_manager.daily_start_equity > 0
            else 0.0
        )
        daily_pnl_krw = total_equity - risk_manager.daily_start_equity

        held = get_held_markets(balances, bithumb)
        held_desc = ", ".join(held) if held else "없음 (100% 현금 보유)"

        telegram.send_message(
            f"☕ <b>[굿모닝! 빗썸 퀀트 일일 결산 리포트]</b>\n\n"
            f"• <b>총 평가 자산:</b> {total_equity:,.0f} KRW\n"
            f"• <b>24시간 실현 손익:</b> {daily_pnl_krw:+,.0f} KRW ({daily_pnl_pct:+.2f}%)\n"
            f"• <b>가용 원화 잔고:</b> {krw_avail:,.0f} KRW\n"
            f"• <b>현재 보유 포지션:</b> {held_desc}\n"
            f"• <b>일일 킬스위치 상태:</b> {'🚨 발동 중' if risk_manager.kill_switch_active else '🟢 정상 (리스크 양호)'}\n"
            f"• <b>기준 일시:</b> {now_str}"
        )
    except (requests.exceptions.RequestException, KeyError, ValueError) as e:
        logger.error(f"모닝 리포트 발송 실패: {e}")


def run_cycle():
    """
    실시간 급등주 탐색 + AI 퀀트 분석 + 트레일링 스탑 + 리스크 관리(킬스위치 & BTC급락필터) + 자동매매 실행 사이클
    """
    # .env 실시간 재로드 (봇을 끄지 않아도 .env 변경사항이 다음 사이클에 즉시 반영)
    load_dotenv(override=True)

    raw_markets = os.getenv("MARKETS", "AUTO").strip()
    is_auto_mode = raw_markets.upper() == "AUTO"
    top_count = int(os.getenv("TOP_COUNT", "3"))
    min_trade_val = float(os.getenv("MIN_TRADE_VALUE", "5000000000"))
    min_change = float(os.getenv("MIN_CHANGE_RATE", "0.01"))
    max_change = float(os.getenv("MAX_CHANGE_RATE", "0.25"))
    btc_crash_pct = float(os.getenv("BTC_CRASH_THRESHOLD_PCT", "0.015"))
    max_daily_loss = float(os.getenv("MAX_DAILY_LOSS_PCT", "0.05"))
    trailing_start = float(os.getenv("TRAILING_START_PCT", "0.02"))
    trailing_stop = float(os.getenv("TRAILING_STOP_PCT", "0.012"))

    trailing_tracker.start_profit_pct = trailing_start
    trailing_tracker.trailing_drop_pct = trailing_stop
    risk_manager.max_loss_pct = max_daily_loss

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
                f"• 당일 손실률: <b>{daily_pnl*100:.2f}%</b> (한도: -{max_daily_loss*100:.1f}%)\n"
                f"• 총 평가 자산: {current_total_equity:,.0f} KRW\n"
                f"• 원금 보호를 위해 당일 모든 신규 매수를 중단하고 관망합니다."
            )

        # 2. [리스크 1: 비트코인(BTC) 대세 급락 필터] 점검
        is_btc_crashing, btc_status_msg = check_btc_market_crash(bithumb, threshold_pct=btc_crash_pct)
        if is_btc_crashing:
            logger.warning(f"⚠️ [BTC 급락 방어선 작동] {btc_status_msg} ➜ 알트코인 신규 매수 차단!")

        # 3. 구글 스프레드시트 'Dashboard' 탭 실시간 갱신
        sheets.update_dashboard(
            {
                "updated_at": now_str,
                "total_equity": current_total_equity,
                "krw_available": krw_available,
                "daily_pnl_pct": daily_pnl * 100,
                "daily_pnl_krw": current_total_equity - risk_manager.daily_start_equity,
                "realized_pnl_krw": risk_manager.realized_pnl_krw,
                "trades_count": risk_manager.total_trades_today,
                "win_count": risk_manager.win_trades_today,
                "held_coins": ", ".join(held_markets) if held_markets else "없음 (100% 현금)",
                "kill_switch_status": "🛑 발동 중 (신규매수 차단)" if is_kill_switch else "🟢 정상 (리스크 양호)",
                "btc_health": f"⚠️ 급락 감지 ({btc_status_msg})" if is_btc_crashing else "🟢 정상 안정세",
            }
        )

        # 3. 거래 대상 마켓 결정 (동적 스크리닝 vs 고정 목록)
        target_markets: list[str] = []
        if is_auto_mode:
            screener = MarketScreener(
                bithumb,
                min_trade_value_krw=min_trade_val,
                min_change_rate=min_change,
                max_change_rate=max_change,
            )
            screened_items = screener.scan_markets(
                top_count=top_count, held_markets=held_markets
            )
            target_markets = [item["market"] for item in screened_items]
        else:
            fixed_list = [m.strip().upper() for m in raw_markets.split(",") if m.strip()]
            target_markets = list(dict.fromkeys(held_markets + fixed_list))

        logger.info(f"이번 사이클 최종 분석 대상 마켓 ({len(target_markets)}개): {target_markets}")

        # 4. 마켓별 순회 분석 및 매매 실행
        for market in target_markets:
            currency = market.split("-")[-1] if "-" in market else market
            korean_name = bithumb.get_korean_name(market)
            logger.info(f"--- [{korean_name} / {market} AI 퀀트 분석 시작] ---")

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
                    f"[{korean_name} / {market}] 현재가: {current_price:,.2f}원 | 가용 KRW: {krw_available:,.0f}원 | 보유 {currency}: {coin_total:.6f} (평단: {avg_buy_price:,.2f}원)"
                )

                # =========================================================================
                # 🎯 [최우선 1: 트레일링 스탑(Trailing Stop) 익절 검사]
                # =========================================================================
                coin_value = coin_total * current_price
                if coin_value >= MIN_ORDER_KRW and avg_buy_price > 0:
                    is_trailing_hit, peak_p, peak_profit_pct, realized_profit_pct = (
                        trailing_tracker.check_trailing_stop(market, current_price, avg_buy_price)
                    )

                    if is_trailing_hit:
                        logger.info(
                            f"🎯 [{korean_name} / {market} 트레일링 스탑 익절 발동] 최고점 {peak_p:,.2f}원(+{peak_profit_pct:.2f}%) ➜ 현재 {current_price:,.2f}원(+{realized_profit_pct:.2f}%). 전량 시장가 익절!"
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

                        pnl_krw = (current_price - avg_buy_price) * coin_available
                        risk_manager.add_realized_trade(pnl_krw, is_win=True)

                        telegram.send_message(
                            f"🎯 <b>[{korean_name}({market}) 트레일링 스탑 최고점 익절 완료!]</b>\n"
                            f"• 진입 평단가: {avg_buy_price:,.2f} KRW\n"
                            f"• 도달 최고가: {peak_p:,.2f} KRW (+{peak_profit_pct:.2f}%)\n"
                            f"• 익절 체결가: {current_price:,.2f} KRW\n"
                            f"• <b>실현 수익: +{pnl_krw:,.0f} KRW (+{realized_profit_pct:.2f}%) 🚀</b>\n"
                            f"• 매도 수량: {coin_available:.8f} {currency}\n"
                            f"• 주문 ID: <code>{order_uuid}</code>\n"
                            f"• 일시: {now_str}"
                        )

                        sheets.append_trade_log(
                            {
                                "Timestamp": now_str,
                                "Korean_Name": korean_name,
                                "Market": market,
                                "Order_UUID": order_uuid,
                                "Side": "TRAILING_STOP",
                                "Order_Type": "MARKET",
                                "Price": current_price,
                                "Volume": f"{coin_available:.8f}",
                                "Total_KRW": int(coin_available * current_price),
                                "Realized_PnL_Pct": f"+{realized_profit_pct:.2f}%",
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
                    sheets.update_strategy(market, strategy, now_str, korean_name=korean_name)
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

                    pnl_krw = (current_price - avg_buy_price) * coin_available if avg_buy_price > 0 else 0.0
                    risk_manager.add_realized_trade(pnl_krw, is_win=False)

                    telegram.send_message(
                        f"🚨 <b>[{korean_name}({market}) 손절 전량 매도 실행]</b>\n"
                        f"• 현재가: {current_price:,.2f} KRW\n"
                        f"• 손절 기준가: {stop_loss:,.2f} KRW\n"
                        f"• 손실 금액: {pnl_krw:,.0f} KRW\n"
                        f"• 매도 수량: {coin_available:.8f} {currency}\n"
                        f"• 주문 ID: <code>{order_uuid}</code>\n"
                        f"• 일시: {now_str}"
                    )

                    sheets.append_trade_log(
                        {
                            "Timestamp": now_str,
                            "Korean_Name": korean_name,
                            "Market": market,
                            "Order_UUID": order_uuid,
                            "Side": "STOP_LOSS",
                            "Order_Type": "MARKET",
                            "Price": current_price,
                            "Volume": f"{coin_available:.8f}",
                            "Total_KRW": int(coin_available * current_price),
                            "Realized_PnL_Pct": f"{((current_price - avg_buy_price) / avg_buy_price * 100):.2f}%" if avg_buy_price > 0 else "-",
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
                    logger.info(f"[{korean_name} / {market}] 기존 미체결 주문 {len(open_orders)}건 취소 진행")
                    for order in open_orders:
                        bithumb.cancel_order(order["uuid"])

                # 7. [신규 주문 실행]
                if action == "BUY":
                    if is_kill_switch:
                        logger.info(f"[{korean_name} / {market}] 킬 스위치 발동 상태이므로 신규 매수를 건너뜁니다.")
                        continue

                    # [포트폴리오 보호] 이미 보유 중인 코인은 중복 매수 금지 (1회 진입 락)
                    if coin_value >= MIN_ORDER_KRW:
                        logger.info(f"[{korean_name} / {market}] 이미 보유 중인 포지션({coin_value:,.0f}원)이므로 중복 매수를 건너뜁니다 (보유 유지).")
                        continue

                    if market != "KRW-BTC" and is_btc_crashing:
                        logger.warning(f"[{korean_name} / {market}] 비트코인 급락세({btc_status_msg})로 인해 알트코인 매수를 방어적으로 차단합니다.")
                        telegram.send_message(
                            f"⚠️ <b>[{korean_name}({market}) 매수 차단 - BTC 급락 방어]</b>\n"
                            f"• 사유: <i>{btc_status_msg}</i>\n"
                            f"• 대장주(BTC) 급락으로 인한 알트코인 동반 폭락 위험 방지"
                        )
                        continue

                    num_slots = max(1, len(target_markets) - len(held_markets) + 1)
                    max_market_budget = krw_available / num_slots
                    trade_budget = max_market_budget * alloc_pct
                    
                    # 슬리피지 보호: 현재가 대비 최대 +0.2% 이내로만 지정가 매수 허용
                    order_price = entry_price if (0 < entry_price <= current_price * 1.002) else current_price

                    if trade_budget < MIN_ORDER_KRW and max_market_budget >= MIN_ORDER_KRW:
                        trade_budget = min(max_market_budget, MIN_ORDER_KRW)

                    if trade_budget < MIN_ORDER_KRW:
                        logger.warning(
                            f"[{korean_name} / {market}] 매수 예산 부족: 요청금액 {trade_budget:,.0f}원 < 최소주문금액 {MIN_ORDER_KRW:,.0f}원"
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
                        f"🟢 <b>[{korean_name}({market}) 급등 포착 지정가 매수 주문]</b>\n"
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
                            "Korean_Name": korean_name,
                            "Market": market,
                            "Order_UUID": order_uuid,
                            "Side": "BUY",
                            "Order_Type": "LIMIT",
                            "Price": order_price,
                            "Volume": f"{buy_volume:.8f}",
                            "Total_KRW": int(trade_budget),
                            "Realized_PnL_Pct": "-",
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
                            f"[{korean_name} / {market}] 매도 가치 부족: {estimated_krw:,.0f}원 < 최소주문금액 {MIN_ORDER_KRW:,.0f}원"
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

                    pnl_krw = (order_price - avg_buy_price) * sell_volume if avg_buy_price > 0 else 0.0
                    risk_manager.add_realized_trade(pnl_krw, is_win=(pnl_krw > 0))

                    telegram.send_message(
                        f"🔴 <b>[{korean_name}({market}) 지정가 익절 매도 주문]</b>\n"
                        f"• 주문가: {order_price:,.2f} KRW\n"
                        f"• 매도수량: {sell_volume:.8f} {currency}\n"
                        f"• 예상금액: {estimated_krw:,.0f} KRW (비중: {alloc_pct*100:.0f}%)\n"
                        f"• 예상수익: {pnl_krw:+,.0f} KRW\n"
                        f"• 분석근거: <i>{reason}</i>\n"
                        f"• 일시: {now_str}"
                    )

                    sheets.append_trade_log(
                        {
                            "Timestamp": now_str,
                            "Korean_Name": korean_name,
                            "Market": market,
                            "Order_UUID": order_uuid,
                            "Side": "SELL",
                            "Order_Type": "LIMIT",
                            "Price": order_price,
                            "Volume": f"{sell_volume:.8f}",
                            "Total_KRW": int(estimated_krw),
                            "Realized_PnL_Pct": f"{((order_price - avg_buy_price) / avg_buy_price * 100):.2f}%" if avg_buy_price > 0 else "-",
                            "Stop_Loss": stop_loss,
                            "Target_Price": target_price,
                            "Current_Balance_KRW": int(krw_available),
                            "Status_Reason": f"정상 매도 접수 ({reason})",
                        }
                    )

                elif action == "HOLD":
                    logger.info(f"[{market}] HOLD (관망) - 사유: {reason}")

            except (requests.exceptions.RequestException, KeyError, ValueError, IndexError) as e:
                logger.error(f"[{market}] 처리 중 오류 발생: {e}")

    except Exception as e:  # noqa: BLE001
        error_msg = traceback.format_exc()
        logger.error(f"전체 사이클 실행 중 에러:\n{error_msg}")
        try:
            telegram = TelegramAlert(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
            telegram.send_message(
                f"❌ <b>[시스템 오류 알림]</b>\n"
                f"• 시간: {now_str}\n"
                f"• 에러 내용: <code>{e}</code>"
            )
        except requests.exceptions.RequestException:
            pass


class CodeChangeWatcher:
    """
    소스코드 및 Git 커밋 실시간 감시 핫리로더 (Hot-Reloader)
    - src/ 내의 파이썬 파일 변경
    - .env 환경변수 설정 변경
    - Git 커밋 및 Pull (HEAD, refs 변경)
    감지 시 1초 만에 봇을 새 버전으로 자동 안전 재시작
    """

    def __init__(self, watch_paths: list[str], check_interval: float = 3.0):
        self.watch_paths = watch_paths
        self.check_interval = check_interval
        self.snapshots = self._get_snapshots()

    def _get_snapshots(self) -> dict[str, float]:
        snapshots = {}
        for p in self.watch_paths:
            if not os.path.exists(p):
                continue
            if os.path.isfile(p):
                try:
                    snapshots[p] = os.path.getmtime(p)
                except OSError:
                    pass
            else:
                for root, _, files in os.walk(p):
                    for f in files:
                        if f.endswith((".py", ".env")) or f == "HEAD" or "refs" in root:
                            fpath = os.path.join(root, f)
                            try:
                                snapshots[fpath] = os.path.getmtime(fpath)
                            except OSError:
                                pass
        return snapshots

    def start(self):
        import threading

        def _watch_loop():
            import time
            while True:
                time.sleep(self.check_interval)
                try:
                    current_snapshots = self._get_snapshots()
                    if current_snapshots != self.snapshots:
                        logger.info("🔄 [코드 / Git 커밋 변경 감지] 최신 버전으로 봇을 즉시 자동 재시작(Hot-Reload)합니다...")
                        try:
                            telegram = TelegramAlert(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
                            telegram.send_message("🔄 <b>[코드 업데이트 감지]</b> 봇을 최신 코드로 자동 재시작합니다.")
                        except requests.exceptions.RequestException:
                            pass
                        time.sleep(0.5)
                        os.execv(sys.executable, [sys.executable] + sys.argv)
                except OSError as e:
                    logger.warning(f"코드 감시 루프 오류: {e}")

        t = threading.Thread(target=_watch_loop, daemon=True, name="CodeChangeWatcher")
        t.start()
        logger.info("👀 코드 변경 및 Git 커밋 실시간 감시자(Hot-Reloader) 활성화 완료")


def main():
    logger.info("🚀 빗썸 API 2.0 프로 퀀트 AI 자동매매 봇 v2.0 시작")
    mode_text = f"실시간 급등주 자동 스캔 (상위 {TOP_COUNT}종목)" if IS_AUTO_MODE else f"고정 마켓 ({RAW_MARKETS})"
    logger.info(
        f"실행 주기: {INTERVAL_MINUTES}분 | 모드: {mode_text} | 트레일링 시작: +{TRAILING_START_PCT*100:.1f}% (고점대비 -{TRAILING_STOP_PCT*100:.1f}%) | 킬스위치: -{MAX_DAILY_LOSS_PCT*100:.1f}%"
    )

    # 코드 및 Git 변경 자동 감시 핫리로더 가동
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    watcher = CodeChangeWatcher(
        watch_paths=[
            os.path.join(base_dir, "src"),
            os.path.join(base_dir, ".env"),
            os.path.join(base_dir, ".git", "HEAD"),
            os.path.join(base_dir, ".git", "refs"),
        ],
        check_interval=3.0,
    )
    watcher.start()

    try:
        run_cycle()
    except Exception as e:  # noqa: BLE001
        logger.error(f"최초 실행 실패: {e}")

    scheduler = BlockingScheduler(timezone="Asia/Seoul")
    
    # 1. 주기별 자동매매 사이클
    scheduler.add_job(
        run_cycle,
        "interval",
        minutes=INTERVAL_MINUTES,
        id="trading_cycle_job",
        replace_existing=True,
    )

    # 2. 매일 아침 09:00 KST 일일 결산 브리핑 발송
    scheduler.add_job(
        send_daily_morning_report,
        "cron",
        hour=9,
        minute=0,
        id="daily_morning_report_job",
        replace_existing=True,
    )

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("자동매매 봇이 정상적으로 종료되었습니다.")


if __name__ == "__main__":
    main()
