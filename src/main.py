import datetime
import json
import logging
import os
import sys
import threading
import time
import traceback
from logging.handlers import TimedRotatingFileHandler
from typing import Any

import requests
from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv

from bithumb_api import BithumbAPI
from chart_renderer import ChartRenderer
from gemini_analyzer import GeminiAnalyzer
from market_screener import MarketScreener
from order_safety import (
    AmbiguousOrderError,
    CooldownManager,
    OrderJournal,
    RiskGuard,
    SafeOrderExecutor,
    calculate_risk_position_size,
    write_json_atomically,
)
from paper_broker import PaperBroker
from private_websocket_manager import BithumbPrivateWebSocketClient
from sheets_manager import SheetsManager
from strategy_engine import classify_btc_regime, entry_signal
from telegram_alert import TelegramAlert
from trade_memory import TradeMemoryManager
from web_server import DashboardWebServer
from websocket_manager import BithumbWebSocketClient

# 윈도우 cp949 인코딩 에러 방지 (이모지 및 한글 UTF-8 표준화)
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (OSError, AttributeError):
        pass

# 로그 디렉토리 생성 및 일자별 로깅 설정 (자정마다 일자별 분할 보관, 30일치 유지)
LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "trading.log")

file_handler = TimedRotatingFileHandler(
    LOG_FILE, when="midnight", interval=1, backupCount=30, encoding="utf-8"
)
file_handler.suffix = "%Y-%m-%d"
stream_handler = logging.StreamHandler(sys.stdout)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[stream_handler, file_handler],
)
logger = logging.getLogger("TradingBot")

# 환경변수 로드 (override=True로 메모리 캐시 덮어쓰기 보장)
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
# 🛡️ 리스크 및 트레일링 스탑 파라미터
# ==========================================
BTC_CRASH_THRESHOLD_PCT = float(os.getenv("BTC_CRASH_THRESHOLD_PCT", "0.015"))
MAX_DAILY_LOSS_PCT = float(os.getenv("MAX_DAILY_LOSS_PCT", "0.05"))
TRAILING_START_PCT = float(os.getenv("TRAILING_START_PCT", "0.02"))
TRAILING_STOP_PCT = float(os.getenv("TRAILING_STOP_PCT", "0.012"))

# 최소 주문 금액 제한 (빗썸 기준: 최소 5,000 KRW)
MIN_ORDER_KRW = 5000.0
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "3"))
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "0.35"))
MAX_TOTAL_EXPOSURE_PCT = float(os.getenv("MAX_TOTAL_EXPOSURE_PCT", "0.85"))
MAX_ORDER_KRW = float(os.getenv("MAX_ORDER_KRW", "0"))  # 0: no absolute cap
TRADING_MODE = os.getenv("TRADING_MODE", "LIVE").strip().upper()
PAPER_INITIAL_KRW = float(os.getenv("PAPER_INITIAL_KRW", "1000000"))
PAPER_FEE_RATE = float(os.getenv("PAPER_FEE_RATE", "0"))

# 봇 일시정지 상태 플래그 (텔레그램 및 웹 대시보드 연동)
IS_BOT_PAUSED = False

# 전역 인스턴스 초기화
chart_renderer = ChartRenderer()
trade_memory = TradeMemoryManager()
order_journal = OrderJournal()
order_executor = SafeOrderExecutor(order_journal)
cooldown_manager = CooldownManager()
risk_guard = RiskGuard(
    min_order_krw=MIN_ORDER_KRW,
    max_open_positions=MAX_OPEN_POSITIONS,
    max_position_pct=MAX_POSITION_PCT,
    max_total_exposure_pct=MAX_TOTAL_EXPOSURE_PCT,
    max_order_krw=MAX_ORDER_KRW,
)
paper_broker: PaperBroker | None = None
private_ws = BithumbPrivateWebSocketClient(
    BITHUMB_ACCESS_KEY,
    BITHUMB_SECRET_KEY,
    on_order=lambda event: order_journal.apply_private_order_event(event),
)


def create_exchange_client() -> BithumbAPI | PaperBroker:
    """Use public market data in PAPER mode while keeping all money virtual."""
    global paper_broker
    live_client = BithumbAPI(BITHUMB_ACCESS_KEY, BITHUMB_SECRET_KEY)
    if TRADING_MODE != "PAPER":
        return live_client
    if paper_broker is None:
        paper_broker = PaperBroker(live_client, PAPER_INITIAL_KRW, PAPER_FEE_RATE)
        logger.warning("🧪 PAPER 모드: 실제 주문은 전송되지 않으며 data/paper_account.json만 갱신됩니다.")
    return paper_broker

# 빗썸 실시간 웹소켓(WebSocket) 클라이언트 (0.1초 실시간 시세 및 5분 고래 수급 스트리밍)
ws_client = BithumbWebSocketClient(
    initial_markets=["KRW-BTC"],
    on_price_callback=lambda m, p: on_realtime_price_tick(m, p),
)


class TrailingStopTracker:
    """
    [50% 분할 익절 + 50% 가속 트레일링 러너] 관리자 (영구 저장 연동)
    - 1차 목표가(+2.5%) 도달 시: 50% 분할 익절 & 본절 방어선 가동
    - +5.0% 이상 돌파 시: 트레일링 폭 0.8%로 축소 (고점 밀착 방어)
    - +10.0% 이상 돌파 시: 트레일링 폭 0.5%로 초밀착 (초고점 극대화)
    - 왕복 수수료(0.1%) 차감 후 최소 +0.2% 이상 순수익 보장선 강제
    - data/position_state.json 파일에 최고점 및 1차 익절 여부 영구 저장
    """

    def __init__(self, start_profit_pct: float = 0.02, trailing_drop_pct: float = 0.012):
        self.start_profit_pct = start_profit_pct
        self.trailing_drop_pct = trailing_drop_pct
        self.peaks: dict[str, float] = {}
        self.partial_tp_done: dict[str, bool] = {}
        self.entry_times: dict[str, float] = {}
        self.data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        os.makedirs(self.data_dir, exist_ok=True)
        self.state_file = os.path.join(self.data_dir, "position_state.json")
        self._load_state()

    def _load_state(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.peaks = data.get("peaks", {})
                    self.partial_tp_done = data.get("partial_tp_done", {})
                    self.entry_times = data.get("entry_times", {})
            except (OSError, json.JSONDecodeError, ValueError) as e:
                logger.warning(f"포지션 상태 파일 로드 실패: {e}")

    def _save_state(self):
        try:
            write_json_atomically(self.state_file, {
                "peaks": self.peaks,
                "partial_tp_done": self.partial_tp_done,
                "entry_times": self.entry_times,
            })
        except OSError as e:
            logger.warning(f"포지션 상태 파일 저장 실패: {e}")

    def set_entry_time(self, market: str, ts: float | None = None):
        self.entry_times[market] = ts or time.time()
        self._save_state()

    def get_entry_time(self, market: str) -> float:
        if market not in self.entry_times:
            self.entry_times[market] = time.time()
            self._save_state()
        return self.entry_times[market]

    def check_position(
        self, market: str, current_price: float, avg_buy_price: float
    ) -> tuple[str, float, float, float, float]:
        if avg_buy_price <= 0:
            return "NONE", 0.0, 0.0, 0.0, 0.0

        current_profit_rate = (current_price - avg_buy_price) / avg_buy_price
        current_profit_pct = current_profit_rate * 100.0

        # 1. [1차 50% 분할 익절 체크 (+2.5% 이상 도달 시)]
        if current_profit_rate >= 0.025 and not self.partial_tp_done.get(market, False):
            self.partial_tp_done[market] = True
            self.peaks[market] = max(self.peaks.get(market, avg_buy_price), current_price)
            self._save_state()
            return "PARTIAL_TP", current_price, current_price, current_profit_pct, current_profit_pct

        # 2. [수익률 단계별 가속 트레일링 스탑 (Ratchet Tightening)]
        if current_profit_rate >= self.start_profit_pct or self.partial_tp_done.get(market, False):
            previous_peak = self.peaks.get(market, avg_buy_price)
            current_peak = max(previous_peak, current_price)
            self.peaks[market] = current_peak
            self._save_state()

            peak_profit_pct = ((current_peak - avg_buy_price) / avg_buy_price) * 100.0

            if peak_profit_pct >= 10.0:
                active_drop_pct = 0.005  # +10% 이상: 0.5% 초밀착
            elif peak_profit_pct >= 5.0:
                active_drop_pct = 0.008  # +5% 이상: 0.8% 밀착
            else:
                active_drop_pct = self.trailing_drop_pct  # 기본 1.2%

            trailing_stop_price = current_peak * (1.0 - active_drop_pct)

            # 수수료(0.1%) 차감 후 최소 +0.2% 순수익 안전 보장
            min_guaranteed_profit = avg_buy_price * 1.002
            trailing_stop_price = max(trailing_stop_price, min_guaranteed_profit)

            logger.info(
                f"🎯 [{market}] 가속 트레일링 추적 중: 최고점 {current_peak:,.2f}원 (+{peak_profit_pct:.2f}% | 드롭폭 {active_drop_pct*100:.1f}%) ➜ 익절기준선 {trailing_stop_price:,.2f}원"
            )

            if current_price <= trailing_stop_price:
                realized_profit_pct = ((current_price - avg_buy_price) / avg_buy_price) * 100.0
                self.clear(market)
                return "TRAILING_STOP", current_peak, trailing_stop_price, peak_profit_pct, realized_profit_pct

        return "NONE", self.peaks.get(market, current_price), 0.0, 0.0, current_profit_pct

    def clear(self, market: str):
        self.peaks.pop(market, None)
        self.partial_tp_done.pop(market, None)
        self.entry_times.pop(market, None)
        self._save_state()

    def reconcile_markets(self, held_markets: list[str]) -> int:
        """Drop stale trailing state after a restart; exchange balances are authoritative."""
        stale_markets = (set(self.peaks) | set(self.partial_tp_done) | set(self.entry_times)) - set(held_markets)
        for market in stale_markets:
            self.peaks.pop(market, None)
            self.partial_tp_done.pop(market, None)
            self.entry_times.pop(market, None)
        if stale_markets:
            self._save_state()
        return len(stale_markets)


trailing_tracker = TrailingStopTracker(
    start_profit_pct=TRAILING_START_PCT, trailing_drop_pct=TRAILING_STOP_PCT
)


class DailyRiskManager:
    """
    일일 손익 추적, 킬 스위치(Kill-Switch) 및 연속 손절 30분 쿨다운 관리자 (영구 저장 연동)
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
        self.consecutive_losses = 0
        self.cooldown_until_ts = 0.0
        self.daily_history: list[dict[str, Any]] = []
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
                    self.consecutive_losses = int(data.get("consecutive_losses", 0))
                    self.cooldown_until_ts = float(data.get("cooldown_until_ts", 0.0))
                    self.daily_history = data.get("history", [])
            except (OSError, json.JSONDecodeError, ValueError) as e:
                logger.warning(f"일일 통계 로드 실패: {e}")

    def _save_state(self):
        try:
            write_json_atomically(self.stats_file, {
                "date": self.current_date_str,
                "start_equity": self.daily_start_equity,
                "realized_pnl_krw": self.realized_pnl_krw,
                "total_trades": self.total_trades_today,
                "win_trades": self.win_trades_today,
                "consecutive_losses": self.consecutive_losses,
                "cooldown_until_ts": self.cooldown_until_ts,
                "history": self.daily_history,
            })
        except OSError as e:
            logger.warning(f"일일 통계 저장 실패: {e}")

    def add_realized_trade(self, pnl_krw: float, is_win: bool):
        self.realized_pnl_krw += pnl_krw
        self.total_trades_today += 1
        if is_win:
            self.win_trades_today += 1
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1
            if self.consecutive_losses >= 2:
                self.cooldown_until_ts = time.time() + 1800.0  # 30분 쿨다운
                logger.warning("🛑 [연속 2회 손절 발생] 30분간 뇌동매매 방지 신규 매수 쿨다운 가동!")

        self._save_state()

    def is_cooling_down(self) -> tuple[bool, int]:
        now_ts = time.time()
        if now_ts < self.cooldown_until_ts:
            remain_minutes = max(1, int((self.cooldown_until_ts - now_ts) / 60))
            return True, remain_minutes
        return False, 0

    def update_daily_equity(self, current_total_equity: float, now_kst: datetime.datetime) -> tuple[bool, float]:
        date_key = now_kst.strftime("%Y-%m-%d")

        if date_key != self.current_date_str or self.daily_start_equity <= 0:
            if date_key != self.current_date_str and self.current_date_str:
                self.daily_history.append({
                    "date": self.current_date_str,
                    "total_trades": self.total_trades_today,
                    "win_trades": self.win_trades_today,
                    "realized_pnl_krw": self.realized_pnl_krw,
                })
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


def get_fear_and_greed_index() -> dict[str, Any]:
    """글로벌 가상자산 크립토 공포 & 탐욕 지수 실시간 조회"""
    try:
        res = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
        if res.status_code == 200:
            data = res.json().get("data", [{}])[0]
            val = int(data.get("value", 50))
            classification = data.get("value_classification", "Neutral")

            korean_map = {
                "Extreme Fear": "😱 극단적 공포 (투매/바닥권)",
                "Fear": "😨 공포 (보수적 접근)",
                "Neutral": "😐 중립 (균형)",
                "Greed": "🤑 탐욕 (상승 모멘텀)",
                "Extreme Greed": "🚀 극단적 탐욕 (과열/익절권)",
            }
            desc = korean_map.get(classification, classification)
            return {
                "value": val,
                "classification": classification,
                "desc": f"{val}점 ({desc})",
                "is_extreme_fear": val <= 25,
                "is_extreme_greed": val >= 75,
            }
    except (requests.exceptions.RequestException, KeyError, ValueError):
        pass
    return {"value": 50, "classification": "Neutral", "desc": "50점 (중립)", "is_extreme_fear": False, "is_extreme_greed": False}


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
                logger.debug(f"{cur} 잔고 시세 조회 예외 무시")

    return krw_balance + total_coin_val


def get_held_markets(balances: dict[str, dict[str, float]], bithumb: BithumbAPI) -> list[str]:
    held = []
    for cur, info in balances.items():
        if cur in ("KRW", "P"):
            continue
        total_vol = info.get("balance", 0.0) + info.get("locked", 0.0)
        if total_vol > 0:
            market = f"KRW-{cur}"
            try:
                price = bithumb.get_current_price(market)
                if total_vol * price >= 4000.0:
                    held.append(market)
            except (requests.exceptions.RequestException, KeyError, ValueError):
                logger.debug(f"{market} 보유 여부 확인 예외 무시")
    return held


def build_positions_data(
    balances: dict[str, dict[str, float]],
    bithumb: BithumbAPI,
    strategies: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """웹 대시보드 표시용 보유 코인 포지션 목록 생성"""
    positions = []
    strategies = strategies or {}
    for cur, info in balances.items():
        if cur in ("KRW", "P"):
            continue
        vol = info.get("balance", 0.0) + info.get("locked", 0.0)
        if vol <= 0:
            continue
        market = f"KRW-{cur}"
        try:
            price = bithumb.get_current_price(market)
            if price <= 0:
                continue
            val = vol * price
            if val < 1000.0:  # 1천원 미만 자투리/에어드랍 먼지 제외
                continue
            avg_price = info.get("avg_buy_price", 0.0)
            pnl_pct = ((price - avg_price) / avg_price * 100) if avg_price > 0 else 0.0
            strat = strategies.get(market, {})
            positions.append({
                "market": market,
                "korean_name": bithumb.get_korean_name(market),
                "current_price": price,
                "balance": f"{vol:.6f}".rstrip("0").rstrip("."),
                "value": int(val),
                "pnl_pct": pnl_pct,
                "action": strat.get("ACTION", "HOLD"),
                "target_price": strat.get("TARGET_PRICE", 0),
                "stop_loss": strat.get("STOP_LOSS", 0),
                "reason": strat.get("REASON", "보유 중 (AI 실시간 관망 및 모니터링)"),
            })
        except (requests.exceptions.RequestException, KeyError, ValueError):
            continue
    return positions


def check_btc_market_crash(bithumb: BithumbAPI, threshold_pct: float = BTC_CRASH_THRESHOLD_PCT) -> tuple[bool, str, str]:
    """Classify BTC market state into (is_crash: bool, btc_regime: str, status_msg: str)."""
    try:
        btc_candles_5m = bithumb.get_candles(unit=INTERVAL_MINUTES, count=20, market="KRW-BTC")
        btc_candles_1h = bithumb.get_candles(unit=60, count=50, market="KRW-BTC")
        regime_data = classify_btc_regime(btc_candles_5m, btc_candles_1h, crash_threshold_pct=threshold_pct)
        regime = regime_data.get("regime", "NORMAL")
        is_crash = regime == "CRASH"
        return is_crash, regime, regime_data.get("reason", "BTC 정상 안정세")
    except Exception as e:
        logger.warning(f"BTC 시장 상태 검사 실패: {e}")
        return False, "NORMAL", "BTC 검사 오류 (정상 가정)"


def clean_stale_orders(bithumb: BithumbAPI, max_age_seconds: int = 180) -> int:
    """3분(180초) 이상 미체결 주문 자동 취소 및 예수금 즉시 회수"""
    canceled_count = 0
    try:
        open_orders = bithumb.get_open_orders()
        now_ts = time.time()
        for order in open_orders:
            created_at_str = order.get("created_at", "")
            order_uuid = order.get("uuid") or order.get("order_id", "")
            if not order_uuid:
                continue
            market = order.get("market", "")
            side = "매수" if order.get("side") == "bid" else "매도"

            try:
                dt = datetime.datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                order_age = now_ts - dt.timestamp()
            except (ValueError, TypeError):
                order_age = 1000

            if order_age >= max_age_seconds:
                logger.info(f"🧹 [장기 미체결 주문 청소] {market} {side} 주문 (경과: {order_age/60:.1f}분) 취소 진행")
                bithumb.cancel_order(order_uuid)
                canceled_count += 1

    except (requests.exceptions.RequestException, KeyError, ValueError) as e:
        logger.warning(f"미체결 주문 청소 중 오류 (매매 지속): {e}")

    return canceled_count


def requote_pending_orders(bithumb: BithumbAPI) -> int:
    """[스마트 최우선 호가 재정정 (Smart Re-quoter)]"""
    requoted_count = 0
    try:
        open_orders = bithumb.get_open_orders()
        now_ts = time.time()
        for order in open_orders:
            if order.get("side") != "bid":
                continue

            created_at_str = order.get("created_at", "")
            order_uuid = order.get("uuid") or order.get("order_id", "")
            if not order_uuid:
                continue
            market = order.get("market", "")
            order_price = float(order.get("price", 0.0))
            order_vol = float(order.get("volume", 0.0))

            try:
                dt = datetime.datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                order_age = now_ts - dt.timestamp()
            except (ValueError, TypeError):
                order_age = 0

            if 60 <= order_age <= 300:
                current_price = bithumb.get_current_price(market)
                if current_price > order_price and current_price <= order_price * 1.008:
                    new_price = bithumb.round_price_to_tick(current_price)
                    logger.info(
                        f"🎛️ [스마트 호가 재정정] {market} 기존 지정가({order_price:,.2f}원) ➜ 최신 체결가({new_price:,.2f}원)로 자동 정정"
                    )
                    bithumb.cancel_order(order_uuid)
                    order_executor.submit(bithumb,
                        market=market,
                        side="bid",
                        price=new_price,
                        volume=order_vol,
                        ord_type="limit",
                    )
                    requoted_count += 1
    except (requests.exceptions.RequestException, KeyError, ValueError) as e:
        logger.warning(f"스마트 호가 재정정 중 오류: {e}")

    return requoted_count


def send_daily_morning_report():
    """매일 아침 09:00 KST 일일 결산 모닝 리포트 전송"""
    now_str = get_kst_now_str()
    logger.info(f"📊 [아침 9시 일일 결산 브리핑 발송: {now_str}]")

    try:
        bithumb = create_exchange_client()
        telegram = TelegramAlert(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        fng = get_fear_and_greed_index()
        balances = bithumb.get_balances()
        total_equity = calculate_total_equity(balances, bithumb)
        krw_avail = balances.get("KRW", {}).get("balance", 0.0)

        daily_pnl_krw = total_equity - risk_manager.daily_start_equity
        daily_pnl_pct = (
            (daily_pnl_krw / risk_manager.daily_start_equity) * 100.0
            if risk_manager.daily_start_equity > 0
            else 0.0
        )

        held_markets = get_held_markets(balances, bithumb)
        held_desc = ", ".join(held_markets) if held_markets else "없음 (100% 현금 보유)"

        telegram.send_message(
            f"🌅 <b>[빗썸 AI 퀀트 봇 - 09:00 KST 일일 성과 결산 브리핑]</b>\n\n"
            f"• <b>총 평가 자산:</b> {total_equity:,.0f} KRW\n"
            f"• <b>금일 자산 변동:</b> {daily_pnl_krw:+,.0f} KRW ({daily_pnl_pct:+.2f}%)\n"
            f"• <b>금일 확정 실현 손익:</b> {risk_manager.realized_pnl_krw:+,.0f} KRW (총 {risk_manager.total_trades_today}회 거래)\n"
            f"• <b>가용 원화 잔고:</b> {krw_avail:,.0f} KRW\n"
            f"• <b>현재 보유 포지션:</b> {held_desc}\n"
            f"• <b>크립토 공포/탐욕 지수:</b> {fng['desc']}\n"
            f"• <b>웹 대시보드:</b> <code>http://localhost:7979</code>\n"
            f"• <b>기준 일시:</b> {now_str}"
        )
    except (requests.exceptions.RequestException, KeyError, ValueError) as e:
        logger.error(f"모닝 리포트 발송 실패: {e}")


def get_telegram_status_callback() -> str:
    """텔레그램 /status 명령어 응답 콜백"""
    now_str = get_kst_now_str()
    try:
        bithumb = create_exchange_client()
        fng = get_fear_and_greed_index()
        balances = bithumb.get_balances()
        total_equity = calculate_total_equity(balances, bithumb)
        krw_avail = balances.get("KRW", {}).get("balance", 0.0)
        daily_pnl_krw = total_equity - risk_manager.daily_start_equity
        daily_pnl_pct = (daily_pnl_krw / risk_manager.daily_start_equity * 100) if risk_manager.daily_start_equity > 0 else 0.0
        held = get_held_markets(balances, bithumb)
        held_str = ", ".join(held) if held else "없음 (100% 현금)"

        state_badge = "⏸️ 일시정지 중 (관망)" if IS_BOT_PAUSED else "🟢 정상 가동 중"

        return (
            f"📊 <b>[빗썸 AI 퀀트 봇 실시간 종합 대시보드]</b>\n\n"
            f"• <b>봇 상태:</b> {state_badge}\n"
            f"• <b>총 평가 자산:</b> {total_equity:,.0f} KRW\n"
            f"• <b>가용 원화 잔고:</b> {krw_avail:,.0f} KRW\n"
            f"• <b>금일 자산 변동:</b> {daily_pnl_krw:+,.0f} KRW ({daily_pnl_pct:+.2f}%)\n"
            f"• <b>금일 확정 실현 손익:</b> {risk_manager.realized_pnl_krw:+,.0f} KRW (거래 {risk_manager.total_trades_today}회)\n"
            f"• <b>현재 보유 종목:</b> {held_str}\n"
            f"• <b>크립토 공포/탐욕 지수:</b> {fng['desc']}\n"
            f"• <b>웹소켓 스트리밍:</b> ⚡ 0.1초 실시간 체결 감시 가동 중\n"
            f"• <b>웹 대시보드:</b> <code>http://localhost:7979</code>\n"
            f"• <b>조회 일시:</b> {now_str}"
        )
    except (requests.exceptions.RequestException, KeyError, ValueError) as e:
        return f"❌ 상태 조회 실패: {e}"


def get_telegram_balance_callback() -> str:
    """텔레그램 /balance 명령어 응답 콜백"""
    now_str = get_kst_now_str()
    try:
        bithumb = create_exchange_client()
        balances = bithumb.get_balances()
        lines = ["💰 <b>[실시간 계좌 잔고 상세 내역]</b>\n"]
        krw = balances.get("KRW", {})
        lines.append(f"• <b>KRW (원화):</b> {krw.get('balance', 0.0):,.0f}원 (주문중: {krw.get('locked', 0.0):,.0f}원)")

        for cur, info in balances.items():
            if cur == "KRW":
                continue
            bal = info.get("balance", 0.0) + info.get("locked", 0.0)
            if bal > 0:
                avg_p = info.get("avg_buy_price", 0.0)
                try:
                    cur_p = bithumb.get_current_price(f"KRW-{cur}")
                    val = bal * cur_p
                    pnl = ((cur_p - avg_p) / avg_p * 100) if avg_p > 0 else 0.0
                    k_name = bithumb.get_korean_name(f"KRW-{cur}")
                    lines.append(f"• <b>{k_name}({cur}):</b> {bal:.6f}개 (평가: {val:,.0f}원 / 수익률: {pnl:+.2f}%)")
                except (requests.exceptions.RequestException, KeyError, ValueError):
                    lines.append(f"• <b>{cur}:</b> {bal:.6f}개")
        lines.append(f"\n• 기준 일시: {now_str}")
        return "\n".join(lines)
    except (requests.exceptions.RequestException, KeyError, ValueError) as e:
        return f"❌ 잔고 조회 실패: {e}"


def get_telegram_panic_callback() -> str:
    """텔레그램 /panic 또는 /긴급매도 원격 명령어 콜백"""
    global IS_BOT_PAUSED
    now_str = get_kst_now_str()
    logger.warning("🚨 [긴급 매도 명령 수신!] 전 보유 종목 전량 시장가 매도 진행")
    IS_BOT_PAUSED = True

    try:
        bithumb = create_exchange_client()
        open_orders = bithumb.get_open_orders()
        for o in open_orders:
            o_id = o.get("uuid") or o.get("order_id", "")
            if o_id:
                bithumb.cancel_order(o_id)

        balances = bithumb.get_balances()
        sold_summary = []

        for cur, info in balances.items():
            if cur == "KRW":
                continue
            bal = info.get("balance", 0.0)
            market = f"KRW-{cur}"
            cur_p = bithumb.get_current_price(market)
            if bal * cur_p >= MIN_ORDER_KRW:
                k_name = bithumb.get_korean_name(market)
                order_executor.submit(bithumb, market=market, side="ask", volume=bal, ord_type="market")
                sold_summary.append(f"{k_name}({cur}) {bal:.6f}개")
                trailing_tracker.clear(market)

        summary_text = "\n• " + "\n• ".join(sold_summary) if sold_summary else "없음 (이미 현금 100%)"

        return (
            f"🚨 <b>[긴급 전량 매도 (Panic Sell) 완료]</b>\n\n"
            f"• <b>청산 완료 종목:</b> {summary_text}\n"
            f"• <b>봇 상태:</b> ⏸️ 안전을 위해 자동매매를 일시정지했습니다.\n"
            f"• 재개하시려면 <code>/재개</code> 또는 <code>/resume</code>를 입력하세요.\n"
            f"• 일시: {now_str}"
        )
    except (requests.exceptions.RequestException, KeyError, ValueError) as e:
        return f"❌ 긴급 매도 실행 중 오류: {e}"


def get_telegram_pause_callback() -> str:
    """텔레그램 /pause 명령어 콜백"""
    global IS_BOT_PAUSED
    IS_BOT_PAUSED = True
    return "⏸️ <b>[자동매매 일시정지]</b>\n• 신규 매수를 차단하고 관망 모드로 전환합니다.\n• 기존 보유 종목의 손절/익절 감시는 계속 유지됩니다.\n• 재개: <code>/재개</code>"


def get_telegram_resume_callback() -> str:
    """텔레그램 /resume 명령어 콜백"""
    global IS_BOT_PAUSED
    IS_BOT_PAUSED = False
    return "▶️ <b>[자동매매 정상 재개]</b>\n• 실시간 급등주 자동 탐색 및 매매 사이클이 활성화되었습니다."


LATEST_STRATEGIES: dict[str, dict[str, Any]] = {}

LATEST_DASHBOARD_DATA: dict[str, Any] = {
    "total_equity": 0,
    "krw_available": 0,
    "daily_start_equity": 0,
    "daily_pnl_krw": 0,
    "daily_pnl_pct": 0.0,
    "realized_pnl_krw": 0,
    "total_trades": 0,
    "win_trades": 0,
    "win_rate": 0.0,
    "fear_and_greed": "50점 (중립)",
    "bot_state": "🟢 정상 가동 중",
    "positions": [],
}


def get_web_dashboard_data() -> dict[str, Any]:
    """로컬 웹 대시보드 API 즉시 반환 (캐시된 최신 상태)"""
    LATEST_DASHBOARD_DATA["bot_state"] = "⏸️ 일시정지 중 (관망)" if IS_BOT_PAUSED else "🟢 24시간 실시간 자동매매 가동 중"
    try:
        mem = trade_memory.load_memory()
        completed = mem.get("completed_trades", [])
        LATEST_DASHBOARD_DATA["recent_trades"] = completed[-10:][::-1]

        with order_journal._lock:
            recent_orders = list(order_journal.orders[-10:][::-1])
        LATEST_DASHBOARD_DATA["recent_orders"] = recent_orders
    except Exception as e:
        logger.debug(f"대시보드 부가 데이터 로드 예외: {e}")
    return LATEST_DASHBOARD_DATA


_realtime_risk_lock = threading.Lock()
_last_realtime_trigger: dict[str, float] = {}


def on_realtime_price_tick(market: str, current_price: float):
    """
    0.1초 실시간 웹소켓 체결가 수신 시 즉시 트레일링 스탑 / 손절 감시 및 자동 청산
    """
    if current_price <= 0 or not market.startswith("KRW-"):
        return

    now_ts = time.time()
    with _realtime_risk_lock:
        if now_ts - _last_realtime_trigger.get(market, 0.0) < 5.0:
            return

    try:
        bithumb = create_exchange_client()
        currency = market.split("-")[-1]
        balances = bithumb.get_balances()
        coin_info = balances.get(currency, {"balance": 0.0, "locked": 0.0, "avg_buy_price": 0.0})
        coin_available = float(coin_info.get("balance", 0.0))
        avg_buy_price = float(coin_info.get("avg_buy_price", 0.0))
        coin_value = coin_available * current_price

        if coin_value < MIN_ORDER_KRW or avg_buy_price <= 0:
            return

        korean_name = bithumb.get_korean_name(market)
        strat = LATEST_STRATEGIES.get(market, {})
        stop_loss = float(strat.get("STOP_LOSS", 0.0))
        # 전략상 손절가가 0이면 평단가 대비 -3.0% 비상 하한선 기본 적용
        effective_stop_loss = stop_loss if stop_loss > 0 else (avg_buy_price * 0.97)
        telegram = TelegramAlert(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        now_str = get_kst_now_str()

        # 1. 실시간 손절 검사
        if effective_stop_loss > 0 and current_price <= effective_stop_loss:
            with _realtime_risk_lock:
                if now_ts - _last_realtime_trigger.get(market, 0.0) < 5.0:
                    return
                _last_realtime_trigger[market] = now_ts

            logger.warning(
                f"⚡ [실시간 웹소켓 손절 발동] {korean_name}({market}) 현재가({current_price:,.2f}원) <= 손절가({effective_stop_loss:,.2f}원). 즉시 시장가 매도!"
            )
            trailing_tracker.clear(market)
            for order in bithumb.get_open_orders(market):
                o_id = order.get("uuid") or order.get("order_id", "")
                if o_id:
                    bithumb.cancel_order(o_id)

            order_res = order_executor.submit(
                bithumb,
                market=market,
                side="ask",
                volume=coin_available,
                ord_type="market",
            )
            pnl_krw = (current_price - avg_buy_price) * coin_available
            loss_pct = ((current_price - avg_buy_price) / avg_buy_price * 100)
            risk_manager.add_realized_trade(pnl_krw, is_win=False)
            cooldown_manager.record_exit(market, "STOP_LOSS")
            trade_memory.record_completed_trade(
                market=market,
                side="STOP_LOSS",
                entry_price=avg_buy_price,
                exit_price=current_price,
                pnl_pct=loss_pct,
                pnl_krw=pnl_krw,
                reason=f"0.1초 실시간 웹소켓 즉각 손절 실행 (손절선 {effective_stop_loss:,.2f}원 터치)",
                timestamp=now_str,
            )
            telegram.send_message(
                f"🚨 <b>[실시간 초저지연 손절 매도 실행]</b>\n"
                f"• 종목: {korean_name}({market})\n"
                f"• 체결가: {current_price:,.2f} KRW (손절가: {effective_stop_loss:,.2f} KRW)\n"
                f"• 손실: {pnl_krw:,.0f} KRW ({loss_pct:.2f}%)\n"
                f"• 사유: 0.1초 실시간 급락 방어선 즉시 청산\n"
                f"• 일시: {now_str}"
            )
            return

        # 2. 실시간 50% 분할 익절 & 가속 트레일링 스탑 검사
        action_type, peak_p, _trigger_p, peak_profit_pct, realized_profit_pct = (
            trailing_tracker.check_position(market, current_price, avg_buy_price)
        )

        if action_type == "PARTIAL_TP":
            sell_vol = coin_available * 0.5
            sell_val = sell_vol * current_price
            if sell_val >= MIN_ORDER_KRW:
                with _realtime_risk_lock:
                    if now_ts - _last_realtime_trigger.get(market, 0.0) < 5.0:
                        return
                    _last_realtime_trigger[market] = now_ts

                logger.info(
                    f"⚡ [실시간 1차 50% 분할익절] {korean_name}({market}) 현재가 {current_price:,.2f}원(+{realized_profit_pct:.2f}%). 즉시 50% 시장가 익절!"
                )
                for order in bithumb.get_open_orders(market):
                    o_id = order.get("uuid") or order.get("order_id", "")
                    if o_id:
                        bithumb.cancel_order(o_id)

                order_executor.submit(
                    bithumb,
                    market=market,
                    side="ask",
                    volume=sell_vol,
                    ord_type="market",
                )
                pnl_krw = (current_price - avg_buy_price) * sell_vol
                risk_manager.add_realized_trade(pnl_krw, is_win=True)
                trade_memory.record_completed_trade(
                    market=market,
                    side="PARTIAL_TP",
                    entry_price=avg_buy_price,
                    exit_price=current_price,
                    pnl_pct=realized_profit_pct,
                    pnl_krw=pnl_krw,
                    reason="0.1초 실시간 1차 +2.5% 도달 50% 분할 익절",
                    timestamp=now_str,
                )
                telegram.send_message(
                    f"🎉 <b>[실시간 1차 50% 분할익절 체결]</b>\n"
                    f"• 종목: {korean_name}({market})\n"
                    f"• 체결가: {current_price:,.2f} KRW (+{realized_profit_pct:.2f}%)\n"
                    f"• 실현수익: +{pnl_krw:,.0f} KRW 💰\n"
                    f"• 남은 50%: 무한 트레일링 러너 자동 전환\n"
                    f"• 일시: {now_str}"
                )

        elif action_type == "TRAILING_STOP":
            with _realtime_risk_lock:
                if now_ts - _last_realtime_trigger.get(market, 0.0) < 5.0:
                    return
                _last_realtime_trigger[market] = now_ts

            logger.info(
                f"⚡ [실시간 트레일링 스탑 최고점 익절] {korean_name}({market}) 최고 {peak_p:,.2f}원(+{peak_profit_pct:.2f}%) ➜ 현재 {current_price:,.2f}원(+{realized_profit_pct:.2f}%). 즉시 전량 시장가 익절!"
            )
            for order in bithumb.get_open_orders(market):
                o_id = order.get("uuid") or order.get("order_id", "")
                if o_id:
                    bithumb.cancel_order(o_id)

            order_executor.submit(
                bithumb,
                market=market,
                side="ask",
                volume=coin_available,
                ord_type="market",
            )
            pnl_krw = (current_price - avg_buy_price) * coin_available
            risk_manager.add_realized_trade(pnl_krw, is_win=True)
            cooldown_manager.record_exit(market, "TRAILING_STOP")
            trade_memory.record_completed_trade(
                market=market,
                side="TRAILING_STOP",
                entry_price=avg_buy_price,
                exit_price=current_price,
                pnl_pct=realized_profit_pct,
                pnl_krw=pnl_krw,
                reason="0.1초 실시간 가속 트레일링 스탑 최고점 익절",
                timestamp=now_str,
            )
            trailing_tracker.clear(market)
            telegram.send_message(
                f"🎯 <b>[실시간 트레일링 스탑 최고점 익절 완료]</b>\n"
                f"• 종목: {korean_name}({market})\n"
                f"• 최고가: {peak_p:,.2f} KRW (+{peak_profit_pct:.2f}%)\n"
                f"• 익절 체결가: {current_price:,.2f} KRW (+{realized_profit_pct:.2f}%)\n"
                f"• 확정 실현수익: +{pnl_krw:,.0f} KRW 🚀\n"
                f"• 일시: {now_str}"
            )
    except Exception as e:
        logger.debug(f"실시간 시세 콜백 처리 예외 ({market}): {e}")


def handle_web_action(action: str) -> str:
    """웹 대시보드 원격 버튼 액션 핸들러"""
    if action == "panic":
        return get_telegram_panic_callback()
    elif action == "pause":
        return get_telegram_pause_callback()
    elif action == "resume":
        return get_telegram_resume_callback()
    return "알 수 없는 작업"


def run_cycle():
    """
    [MTF + 호가창 수급 + ATR 변동성 + 자가학습 메모리 + 차트 이미지 + 복리 자금관리] 사이클
    """
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
        bithumb = create_exchange_client()
        sheets = SheetsManager(GOOGLE_SERVICE_ACCOUNT_JSON, GOOGLE_SHEET_NAME)
        telegram = TelegramAlert(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        analyzer = GeminiAnalyzer(GEMINI_API_KEY) if GEMINI_API_KEY else None
        fng = get_fear_and_greed_index()

        # A prior POST may have reached the exchange despite a lost response.  Reconcile
        # what can be proven; anything still unknown remains blocked from new BUY orders.
        resolved = order_journal.reconcile_exchange_statuses(
            bithumb.get_order,
            lambda client_id: bithumb.get_order(client_order_id=client_id),
        )
        reconciled = order_journal.reconcile_open_orders(bithumb.get_open_orders())
        if resolved:
            logger.info("주문 저널 동기화: 완료/취소 상태 %d건 반영", resolved)
        if reconciled:
            logger.info("주문 저널 동기화: 미확정 주문 %d건을 거래소 미체결 주문과 연결", reconciled)

        # 0-1. 미체결 주문 TTL 자동 정리 (3분/180초 이상 미체결 주문 취소) & 스마트 리쿼팅
        cleaned = clean_stale_orders(bithumb, max_age_seconds=180)
        if cleaned > 0:
            logger.info(f"🧹 총 {cleaned}건의 3분(180초) 경과 미체결 주문 자동 취소 및 예수금 회수 완료")

        requoted = requote_pending_orders(bithumb)
        if requoted > 0:
            logger.info(f"🎛️ 총 {requoted}건의 미체결 호가 최우선가 재정정 완료")

        # 1. 총 자산 평가 및 일일 킬 스위치 점검
        balances = bithumb.get_balances()
        current_total_equity = calculate_total_equity(balances, bithumb)
        is_kill_switch, daily_pnl = risk_manager.update_daily_equity(current_total_equity, now_kst)

        krw_available = balances.get("KRW", {}).get("balance", 0.0)
        held_markets = get_held_markets(balances, bithumb)
        stale_state_count = trailing_tracker.reconcile_markets(held_markets)
        if stale_state_count:
            logger.info("거래소 잔고 동기화: 오래된 포지션 추적 상태 %d건 제거", stale_state_count)

        logger.info(
            f"💰 총 평가 자산: {current_total_equity:,.0f}원 (가용 원화: {krw_available:,.0f}원) | 당일 손익률: {daily_pnl*100:+.2f}% | 공포탐욕: {fng['desc']}"
        )

        if is_kill_switch:
            logger.warning(f"🛑 [일일 킬 스위치 작동 중] 당일 신규 매수를 전면 차단합니다 (손실률: {daily_pnl*100:.2f}%).")
            telegram.send_message(
                f"🛑 <b>[일일 킬 스위치 발동 중]</b>\n"
                f"• 당일 손실률: <b>{daily_pnl*100:.2f}%</b> (한도: -{max_daily_loss*100:.1f}%)\n"
                f"• 총 평가 자산: {current_total_equity:,.0f} KRW\n"
                f"• 원금 보호를 위해 당일 모든 신규 매수를 중단하고 관망합니다."
            )

        # 2. BTC 대세 레짐 및 급락 필터 (3단계: NORMAL / RISK_OFF / CRASH)
        is_btc_crashing, btc_regime, btc_status_msg = check_btc_market_crash(bithumb, threshold_pct=btc_crash_pct)
        if is_btc_crashing:
            logger.warning(f"⚠️ [BTC 급락 방어선 작동 - {btc_regime}] {btc_status_msg} ➜ 알트코인 신규 매수 차단!")
        elif btc_regime == "RISK_OFF":
            logger.info(f"🛡️ [BTC 조정/약세 레짐 - {btc_regime}] {btc_status_msg} ➜ 신규 매수 비중 50% 축소 운용")

        # 3. 구글 스프레드시트 갱신
        bot_state_badge = "⏸️ 일시정지 (관망)" if IS_BOT_PAUSED else "🟢 24시간 실시간 자동매매 가동 중"
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
                "btc_health": f"⚠️ [{btc_regime}] {btc_status_msg}" if is_btc_crashing or btc_regime != "NORMAL" else "🟢 정상 안정세",
                "fear_and_greed": fng["desc"],
                "bot_state": bot_state_badge,
            }
        )

        # 3-1. 로컬 웹 대시보드(포트 7979) 캐시 즉시 갱신
        win_rate = (risk_manager.win_trades_today / risk_manager.total_trades_today * 100) if risk_manager.total_trades_today > 0 else 0.0
        positions_data = build_positions_data(balances, bithumb, LATEST_STRATEGIES)
        LATEST_DASHBOARD_DATA.update({
            "total_equity": int(current_total_equity),
            "krw_available": int(krw_available),
            "daily_start_equity": int(risk_manager.daily_start_equity),
            "daily_pnl_krw": int(current_total_equity - risk_manager.daily_start_equity),
            "daily_pnl_pct": daily_pnl * 100,
            "realized_pnl_krw": int(risk_manager.realized_pnl_krw),
            "total_trades": risk_manager.total_trades_today,
            "win_trades": risk_manager.win_trades_today,
            "win_rate": win_rate,
            "fear_and_greed": fng["desc"],
            "bot_state": bot_state_badge,
            "positions": positions_data,
        })

        sheets.update_performance_tab(
            total_trades=risk_manager.total_trades_today,
            win_trades=risk_manager.win_trades_today,
            realized_pnl_krw=risk_manager.realized_pnl_krw,
            daily_history=risk_manager.daily_history,
        )

        # 4. 대상 마켓 선정
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

        # ⚡ 빗썸 실시간 웹소켓(WebSocket) 구독 갱신
        ws_client.update_subscriptions(list(dict.fromkeys(target_markets + held_markets + ["KRW-BTC"])))

        # 5. 마켓별 순회 분석 및 매매 실행
        for market in target_markets:
            currency = market.split("-")[-1] if "-" in market else market
            korean_name = bithumb.get_korean_name(market)
            logger.info(f"--- [{korean_name} / {market} AI 퀀트 분석 시작] ---")

            try:
                balances = bithumb.get_balances()
                krw_available = balances.get("KRW", {}).get("balance", 0.0)

                coin_info = balances.get(currency, {"balance": 0.0, "locked": 0.0, "avg_buy_price": 0.0})
                coin_available = coin_info["balance"]
                coin_total = coin_available + coin_info["locked"]
                avg_buy_price = coin_info["avg_buy_price"]

                current_price = bithumb.get_current_price(market)
                if current_price <= 0:
                    continue

                candles_5m = bithumb.get_candles(unit=INTERVAL_MINUTES, count=30, market=market)
                candles_1h = bithumb.get_candles(unit=60, count=24, market=market)
                orderbook = bithumb.get_orderbook(market)

                logger.info(
                    f"[{korean_name} / {market}] 현재가: {current_price:,.2f}원 | 가용 KRW: {krw_available:,.0f}원 | 보유 {currency}: {coin_total:.6f} (평단: {avg_buy_price:,.2f}원)"
                )

                # =========================================================================
                # 🎯 [최우선 1: 50% 분할익절 + 50% 가속 트레일링 러너]
                # =========================================================================
                coin_value = coin_total * current_price
                if coin_value >= MIN_ORDER_KRW and avg_buy_price > 0:
                    action_type, peak_p, _trigger_p, peak_profit_pct, realized_profit_pct = (
                        trailing_tracker.check_position(market, current_price, avg_buy_price)
                    )

                    if action_type == "PARTIAL_TP":
                        sell_vol = coin_available * 0.5
                        sell_val = sell_vol * current_price
                        if sell_val >= MIN_ORDER_KRW:
                            logger.info(
                                f"🎉 [{korean_name} / {market} 1차 50% 분할익절 발동] 현재가 {current_price:,.2f}원(+{realized_profit_pct:.2f}%). 50% 물량 시장가 익절!"
                            )
                            for order in bithumb.get_open_orders(market):
                                o_id = order.get("uuid") or order.get("order_id", "")
                                if o_id:
                                    bithumb.cancel_order(o_id)

                            order_res = order_executor.submit(bithumb,
                                market=market,
                                side="ask",
                                volume=sell_vol,
                                ord_type="market",
                            )
                            order_uuid = order_res.get("uuid", "UNKNOWN")
                            pnl_krw = (current_price - avg_buy_price) * sell_vol
                            risk_manager.add_realized_trade(pnl_krw, is_win=True)
                            trade_memory.record_completed_trade(
                                market=market,
                                side="PARTIAL_TP",
                                entry_price=avg_buy_price,
                                exit_price=current_price,
                                pnl_pct=realized_profit_pct,
                                pnl_krw=pnl_krw,
                                reason="1차 +2.5% 도달 50% 분할 익절 완료",
                                timestamp=now_str,
                            )

                            # 차트 이미지 렌더링 및 텔레그램 사진 발송
                            chart_img = chart_renderer.render_trade_chart(
                                market=market,
                                korean_name=korean_name,
                                candles=candles_5m,
                                entry_price=avg_buy_price,
                                target_price=current_price * 1.05,
                                stop_loss=avg_buy_price * 1.002,
                                action="SELL",
                            )

                            caption = (
                                f"🎉 <b>[{korean_name}({market}) 1차 50% 분할익절 완료!]</b>\n"
                                f"• 진입 평단가: {avg_buy_price:,.2f} KRW\n"
                                f"• 익절 체결가: {current_price:,.2f} KRW (+{realized_profit_pct:.2f}%)\n"
                                f"• <b>확정 실현수익: +{pnl_krw:,.0f} KRW 💰</b>\n"
                                f"• 매도 수량: {sell_vol:.8f} {currency} (보유량의 50%)\n"
                                f"• <b>남은 50% 물량: 무한 트레일링 러너 자동 전환 🚀</b>\n"
                                f"• 일시: {now_str}"
                            )
                            if chart_img:
                                telegram.send_photo(chart_img, caption=caption)
                            else:
                                telegram.send_message(caption)

                            sheets.append_trade_log(
                                {
                                    "Timestamp": now_str,
                                    "Korean_Name": korean_name,
                                    "Market": market,
                                    "Order_UUID": order_uuid,
                                    "Side": "PARTIAL_TP",
                                    "Order_Type": "MARKET",
                                    "Price": current_price,
                                    "Volume": f"{sell_vol:.8f}",
                                    "Total_KRW": int(sell_val),
                                    "Realized_PnL_Pct": f"+{realized_profit_pct:.2f}%",
                                    "Stop_Loss": avg_buy_price * 1.002,
                                    "Target_Price": current_price * 1.05,
                                    "Current_Balance_KRW": int(krw_available + sell_val),
                                    "Status_Reason": f"1차 50% 분할 익절 (+{realized_profit_pct:.2f}%)",
                                }
                            )

                    elif action_type == "TRAILING_STOP":
                        logger.info(
                            f"🎯 [{korean_name} / {market} 트레일링 스탑 익절 발동] 최고점 {peak_p:,.2f}원(+{peak_profit_pct:.2f}%) ➜ 현재 {current_price:,.2f}원(+{realized_profit_pct:.2f}%). 잔여 전량 시장가 익절!"
                        )

                        for order in bithumb.get_open_orders(market):
                            o_id = order.get("uuid") or order.get("order_id", "")
                            if o_id:
                                bithumb.cancel_order(o_id)

                        order_res = order_executor.submit(bithumb,
                            market=market,
                            side="ask",
                            volume=coin_available,
                            ord_type="market",
                        )
                        order_uuid = order_res.get("uuid", "UNKNOWN")

                        pnl_krw = (current_price - avg_buy_price) * coin_available
                        risk_manager.add_realized_trade(pnl_krw, is_win=True)
                        trade_memory.record_completed_trade(
                            market=market,
                            side="TRAILING_STOP",
                            entry_price=avg_buy_price,
                            exit_price=current_price,
                            pnl_pct=realized_profit_pct,
                            pnl_krw=pnl_krw,
                            reason="가속 트레일링 스탑 최고점 익절 완료",
                            timestamp=now_str,
                        )
                        trailing_tracker.clear(market)

                        chart_img = chart_renderer.render_trade_chart(
                            market=market,
                            korean_name=korean_name,
                            candles=candles_5m,
                            entry_price=avg_buy_price,
                            target_price=peak_p,
                            stop_loss=avg_buy_price,
                            action="SELL",
                        )

                        caption = (
                            f"🎯 <b>[{korean_name}({market}) 트레일링 스탑 최고점 익절 완료!]</b>\n"
                            f"• 진입 평단가: {avg_buy_price:,.2f} KRW\n"
                            f"• 도달 최고가: {peak_p:,.2f} KRW (+{peak_profit_pct:.2f}%)\n"
                            f"• 익절 체결가: {current_price:,.2f} KRW\n"
                            f"• <b>실현 수익: +{pnl_krw:,.0f} KRW (+{realized_profit_pct:.2f}%) 🚀</b>\n"
                            f"• 매도 수량: {coin_available:.8f} {currency}\n"
                            f"• 주문 ID: <code>{order_uuid}</code>\n"
                            f"• 일시: {now_str}"
                        )
                        if chart_img:
                            telegram.send_photo(chart_img, caption=caption)
                        else:
                            telegram.send_message(caption)

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

                # =========================================================================
                # ⏳ [최우선 2: 60분 횡보 자금 묶임 방지 타임스탑(Time-Stop)]
                # =========================================================================
                if coin_value >= MIN_ORDER_KRW and avg_buy_price > 0:
                    entry_ts = trailing_tracker.get_entry_time(market)
                    hold_duration_sec = time.time() - entry_ts
                    pnl_pct_current = ((current_price - avg_buy_price) / avg_buy_price) * 100.0

                    # 60분(3,600초) 이상 보유 중이고 손익률이 -1.0% ~ +1.0% 박스권 횡보일 때
                    if hold_duration_sec >= 3600 and (-1.0 <= pnl_pct_current <= 1.0):
                        logger.info(
                            f"⏳ [{korean_name} / {market}] 60분 횡보 타임스탑 발동! (손익률: {pnl_pct_current:+.2f}%, 보유시간: {hold_duration_sec/60:.0f}분) ➜ 신규 기회를 위해 시장가 전량 청산"
                        )
                        for order in bithumb.get_open_orders(market):
                            o_id = order.get("uuid") or order.get("order_id", "")
                            if o_id:
                                bithumb.cancel_order(o_id)

                        order_res = order_executor.submit(bithumb,
                            market=market,
                            side="ask",
                            volume=coin_available,
                            ord_type="market",
                        )
                        order_uuid = order_res.get("uuid", "UNKNOWN")
                        pnl_krw = (current_price - avg_buy_price) * coin_available
                        risk_manager.add_realized_trade(pnl_krw, is_win=(pnl_krw >= 0))
                        trade_memory.record_completed_trade(
                            market=market,
                            side="TIME_STOP",
                            entry_price=avg_buy_price,
                            exit_price=current_price,
                            pnl_pct=pnl_pct_current,
                            pnl_krw=pnl_krw,
                            reason="60분 이상 박스권 횡보로 인한 자금 회전 타임스탑 청산",
                            timestamp=now_str,
                        )
                        trailing_tracker.clear(market)

                        caption = (
                            f"⏳ <b>[{korean_name}({market}) 60분 횡보 타임스탑 청산]</b>\n"
                            f"• 청산가: {current_price:,.2f} KRW | 평단가: {avg_buy_price:,.2f} KRW\n"
                            f"• 실현 손익: {pnl_krw:+,.0f} KRW ({pnl_pct_current:+.2f}%)\n"
                            f"• 사유: <i>장기 횡보에 따른 자금 잠김 방지 및 다음 급등 유망주 순환매 확보</i>\n"
                            f"• 일시: {now_str}"
                        )
                        telegram.send_message(caption)
                        continue

                # AI 전략 수립 및 실시간 고래 수급 + 자가학습 피드백 주입
                if analyzer:
                    feedback_context = trade_memory.get_feedback_context()
                    whale_flow_context = ws_client.get_whale_flow_summary(market)
                    strategy = analyzer.analyze(
                        market=market,
                        current_price=current_price,
                        candles=candles_5m,
                        krw_balance=krw_available,
                        coin_balance=coin_available,
                        avg_buy_price=avg_buy_price,
                        candles_1h=candles_1h,
                        orderbook=orderbook,
                        trade_memory_context=feedback_context,
                        btc_context=f"{btc_status_msg} ({'급락 위험 감지' if is_btc_crashing else '정상 안정세'})",
                        whale_context=whale_flow_context,
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

                # The LLM may explain and rank candidates, but it cannot bypass the
                # deterministic entry rule used by the backtester.
                in_cooldown, cd_remaining = cooldown_manager.is_in_cooldown(market)
                if in_cooldown and action == "BUY":
                    action = "HOLD"
                    reason = f"재진입 쿨다운 대기중 ({cd_remaining/60:.0f}분 남음) | {reason}"

                local_entry = entry_signal(
                    candles=candles_5m,
                    candles_1h=candles_1h,
                    btc_regime=btc_regime,
                )
                if action == "BUY" and not local_entry["allow_buy"]:
                    action = "HOLD"
                    reason = f"정량 공통 진입 게이트 차단: {local_entry['reason']} | {reason}"
                elif action == "BUY" and local_entry["allow_buy"]:
                    target_price = local_entry["target_price"]
                    stop_loss = local_entry["stop_loss"]

                if btc_regime == "RISK_OFF" and action == "BUY":
                    alloc_pct = alloc_pct * 0.5
                    reason = f"[BTC 약세 레짐 비중 50% 축소] {reason}"

                if fng["is_extreme_fear"] and action == "BUY":
                    alloc_pct = min(alloc_pct, 0.4)

                logger.info(f"[{market}] 전략: ACTION={action}, 진입가={entry_price:,.2f}, 목표가={target_price:,.2f}, 손절가={stop_loss:,.2f}, 비중={alloc_pct*100:.0f}%")
                logger.info(f"[{market}] 근거: {reason}")

                LATEST_STRATEGIES[market] = {
                    "ACTION": action,
                    "TARGET_PRICE": target_price,
                    "STOP_LOSS": stop_loss,
                    "REASON": reason,
                }

                if status != "ACTIVE":
                    continue

                # 5. [손절 검사]
                if coin_value >= MIN_ORDER_KRW and stop_loss > 0 and current_price <= stop_loss:
                    logger.warning(
                        f"🚨 [{market} 손절 발생] 현재가({current_price:,.2f}원) <= 손절가({stop_loss:,.2f}원). 전량 시장가 매도!"
                    )

                    trailing_tracker.clear(market)
                    for order in bithumb.get_open_orders(market):
                        o_id = order.get("uuid") or order.get("order_id", "")
                        if o_id:
                            bithumb.cancel_order(o_id)

                    order_res = order_executor.submit(bithumb,
                        market=market,
                        side="ask",
                        volume=coin_available,
                        ord_type="market",
                    )
                    order_uuid = order_res.get("uuid", "UNKNOWN")

                    pnl_krw = (current_price - avg_buy_price) * coin_available if avg_buy_price > 0 else 0.0
                    loss_pct = ((current_price - avg_buy_price) / avg_buy_price * 100) if avg_buy_price > 0 else 0.0
                    risk_manager.add_realized_trade(pnl_krw, is_win=False)
                    cooldown_manager.record_exit(market, "STOP_LOSS")
                    trade_memory.record_completed_trade(
                        market=market,
                        side="STOP_LOSS",
                        entry_price=avg_buy_price,
                        exit_price=current_price,
                        pnl_pct=loss_pct,
                        pnl_krw=pnl_krw,
                        reason=reason,
                        timestamp=now_str,
                    )

                    chart_img = chart_renderer.render_trade_chart(
                        market=market,
                        korean_name=korean_name,
                        candles=candles_5m,
                        entry_price=avg_buy_price,
                        target_price=target_price,
                        stop_loss=stop_loss,
                        action="SELL",
                    )

                    caption = (
                        f"🚨 <b>[{korean_name}({market}) 손절 전량 매도 실행]</b>\n"
                        f"• 현재가: {current_price:,.2f} KRW\n"
                        f"• 손절 기준가: {stop_loss:,.2f} KRW\n"
                        f"• 손실 금액: {pnl_krw:,.0f} KRW ({loss_pct:.2f}%)\n"
                        f"• 매도 수량: {coin_available:.8f} {currency}\n"
                        f"• 주문 ID: <code>{order_uuid}</code>\n"
                        f"• 일시: {now_str}"
                    )
                    if chart_img:
                        telegram.send_photo(chart_img, caption=caption)
                    else:
                        telegram.send_message(caption)

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
                            "Realized_PnL_Pct": f"{loss_pct:.2f}%",
                            "Stop_Loss": stop_loss,
                            "Target_Price": target_price,
                            "Current_Balance_KRW": int(krw_available),
                            "Status_Reason": "손절 조건 충족으로 인한 시장가 전량 매도",
                        }
                    )
                    continue

                # 6. [신규 주문 실행 - 동적 복리 자금 관리 적용]
                if action == "BUY":
                    if order_journal.has_unresolved_market(market):
                        logger.warning(
                            "[%s] 이전 주문의 거래소 결과가 확정되지 않아 신규 매수를 차단합니다. data/order_journal.json을 확인하세요.",
                            market,
                        )
                        continue

                    if IS_BOT_PAUSED:
                        logger.info(f"[{korean_name} / {market}] 봇이 일시정지 상태이므로 신규 매수를 건너뜁니다.")
                        continue

                    if is_kill_switch:
                        logger.info(f"[{korean_name} / {market}] 킬 스위치 발동 상태이므로 신규 매수를 건너뜁니다.")
                        continue

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

                    num_unheld_targets = len([m for m in target_markets if m not in held_markets])
                    num_slots = max(1, num_unheld_targets)

                    # 💰 1% 고정 리스크 포지션 사이징 모델과 슬롯 예산 결합
                    effective_capital = current_total_equity if current_total_equity > 0 else (krw_available + max(0.0, risk_manager.realized_pnl_krw))
                    max_market_budget = min(krw_available, effective_capital / num_slots)
                    slot_budget = max_market_budget * alloc_pct

                    # 호가 단위 자동 보정 및 슬리피지 보호
                    order_price = entry_price if (0 < entry_price <= current_price * 1.002) else current_price
                    order_price = bithumb.round_price_to_tick(order_price)

                    # 1회 거래당 1% 리스크 기반 주문금액 산출
                    risk_based_budget = calculate_risk_position_size(
                        total_equity=effective_capital,
                        entry_price=order_price,
                        stop_loss=stop_loss,
                        risk_fraction=0.01,
                        fee_rate=0.0004,
                        slippage_rate=0.001,
                        max_position_pct=MAX_POSITION_PCT,
                        min_order_krw=MIN_ORDER_KRW,
                    )

                    # 슬롯 예산과 리스크 기반 예산 중 안전한 최소값 채택
                    effective_risk_budget = risk_based_budget if risk_based_budget > 0 else slot_budget
                    trade_budget = min(krw_available, slot_budget, effective_risk_budget)

                    # 🛡️ 최소 5,500원 안전 마진 확보 (빗썸 5,000원 최소주문금액 및 수수료 0.995 반영)
                    SAFE_ORDER_KRW = 5500.0
                    if trade_budget < SAFE_ORDER_KRW and krw_available >= SAFE_ORDER_KRW:
                        trade_budget = min(krw_available, max(max_market_budget, SAFE_ORDER_KRW))

                    if trade_budget < SAFE_ORDER_KRW or (trade_budget * 0.995) < MIN_ORDER_KRW:
                        logger.warning(
                            f"[{korean_name} / {market}] 매수 예산 부족: 요청금액 {trade_budget:,.0f}원 < 안전최소주문금액 {SAFE_ORDER_KRW:,.0f}원"
                        )
                        continue

                    is_safe, rejection_reason = risk_guard.validate_buy(
                        market=market,
                        order_krw=trade_budget,
                        available_krw=krw_available,
                        total_equity=current_total_equity,
                        held_markets=held_markets,
                    )
                    if not is_safe:
                        logger.warning("[%s] 통합 리스크 검증으로 매수 차단: %s", market, rejection_reason)
                        continue

                    net_trade_budget = trade_budget * 0.995
                    buy_volume = net_trade_budget / order_price

                    # ⚡ 대량 주문 슬리피지 방지 TWAP 시간분할 체결 (5만 원 이상 주문 시 3회 분할)
                    if trade_budget >= 50000.0:
                        order_list = order_executor.execute_twap(
                            bithumb,
                            market=market,
                            side="bid",
                            volume=buy_volume,
                            price=order_price,
                            splits=3,
                            interval_seconds=2.0,
                        )
                        order_uuid = order_list[0].get("uuid", "TWAP_MULTI") if order_list else "UNKNOWN"
                    else:
                        order_res = order_executor.submit(bithumb,
                            market=market,
                            side="bid",
                            price=order_price,
                            volume=buy_volume,
                            ord_type="limit",
                        )
                        order_uuid = order_res.get("uuid", "UNKNOWN")

                    # 진입 시간 기록 (타임스탑 추적 시작 및 영구 저장)
                    trailing_tracker.set_entry_time(market)
                    if market not in held_markets:
                        held_markets.append(market)
                    krw_available = max(0.0, krw_available - trade_budget)

                    # 진입 캔들 차트 이미지 렌더링 및 텔레그램 사진 전송
                    chart_img = chart_renderer.render_trade_chart(
                        market=market,
                        korean_name=korean_name,
                        candles=candles_5m,
                        entry_price=order_price,
                        target_price=target_price,
                        stop_loss=stop_loss,
                        action="BUY",
                    )

                    caption = (
                        f"🟢 <b>[{korean_name}({market}) 급등 포착 지정가 매수 주문]</b>\n"
                        f"• 주문가: {order_price:,.2f} KRW (호가 단위 보정 완료)\n"
                        f"• 수량: {buy_volume:.8f} {currency}\n"
                        f"• 투입금액: {trade_budget:,.0f} KRW (비중: {alloc_pct*100:.0f}%)\n"
                        f"• 목표가: {target_price:,.2f} KRW | 손절가: {stop_loss:,.2f} KRW\n"
                        f"• 분석근거: <i>{reason}</i>\n"
                        f"• 일시: {now_str}"
                    )
                    if chart_img:
                        telegram.send_photo(chart_img, caption=caption)
                    else:
                        telegram.send_message(caption)

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
                    order_price = bithumb.round_price_to_tick(order_price)
                    estimated_krw = sell_volume * order_price

                    if estimated_krw < MIN_ORDER_KRW:
                        logger.warning(
                            f"[{korean_name} / {market}] 매도 가치 부족: {estimated_krw:,.0f}원 < 최소주문금액 {MIN_ORDER_KRW:,.0f}원"
                        )
                        continue

                    trailing_tracker.clear(market)
                    order_res = order_executor.submit(bithumb,
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
                        f"• 주문가: {order_price:,.2f} KRW (호가 단위 보정 완료)\n"
                        f"• 매도수량: {sell_volume:.8f} {currency}\n"
                        f"• 예상금액: {estimated_krw:,.0f} KRW (비중: {alloc_pct*100:.0f}%)\n"
                        f"• 예상수익: {pnl_krw:+,.0f} KRW\n"
                        f"• 분석근거: <i>{reason}</i>\n"
                        f"• 일시: {now_str}"
                    )

            except (requests.exceptions.RequestException, KeyError, ValueError, AmbiguousOrderError) as e:
                logger.error(f"[{market}] 처리 중 오류: {e}")
                logger.error(traceback.format_exc())

        # 사이클 종료 시 최종 보유 포지션 상태 웹 대시보드 반영
        try:
            latest_balances = bithumb.get_balances()
            pos_list = []
            for cur, info in latest_balances.items():
                if cur == "KRW":
                    continue
                bal = info.get("balance", 0.0) + info.get("locked", 0.0)
                m_code = f"KRW-{cur}"
                cur_p = ws_client.get_latest_price(m_code) or bithumb.get_current_price(m_code)
                if bal * cur_p >= 4000.0:
                    avg_p = info.get("avg_buy_price", 0.0)
                    pnl = ((cur_p - avg_p) / avg_p * 100) if avg_p > 0 else 0.0
                    pos_list.append({
                        "market": m_code,
                        "korean_name": bithumb.get_korean_name(m_code),
                        "balance": round(bal, 6),
                        "current_price": cur_p,
                        "value": int(bal * cur_p),
                        "pnl_pct": pnl,
                        "action": "HOLD",
                        "target_price": cur_p * 1.03,
                        "stop_loss": avg_p * 0.98 if avg_p > 0 else cur_p * 0.98,
                        "reason": "보유 포지션 실시간 트레일링 추종 중",
                    })
            LATEST_DASHBOARD_DATA["positions"] = pos_list
        except (requests.exceptions.RequestException, KeyError, ValueError) as e:
            logger.debug(f"웹 대시보드 포지션 갱신 예외: {e}")

    except (requests.exceptions.RequestException, KeyError, ValueError, RuntimeError) as e:
        logger.error(f"전체 사이클 실행 중 에러:\n{traceback.format_exc()}")
        try:
            telegram = TelegramAlert(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
            err_summary = str(e)[:300]
            telegram.send_message(f"⚠️ <b>[자동매매 봇 사이클 경고]</b>\n{err_summary}")
        except requests.exceptions.RequestException:
            pass


def main():
    logger.info("🚀 빗썸 API 2.0 프로 퀀트 AI 자동매매 봇 v4.0 시작")
    mode_text = f"실시간 급등주 자동 스캔 (상위 {TOP_COUNT}종목)" if IS_AUTO_MODE else f"고정 마켓 ({RAW_MARKETS})"
    logger.info(
        f"실행 주기: {INTERVAL_MINUTES}분 | 모드: {mode_text} | 전략: 50% 분할익절 + 가속 트레일링 | 킬스위치: -{MAX_DAILY_LOSS_PCT*100:.1f}%"
    )

    # 1. 텔레그램 양방향 원격 제어 명령어 리스너 가동 (/status, /balance, /panic, /pause, /resume, /help)
    telegram = TelegramAlert(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
    telegram.start_command_listener(
        status_callback=get_telegram_status_callback,
        balance_callback=get_telegram_balance_callback,
        panic_callback=get_telegram_panic_callback,
        pause_callback=get_telegram_pause_callback,
        resume_callback=get_telegram_resume_callback,
    )

    # 2. 빗썸 2.0 실시간 웹소켓(WebSocket) 0.1초 스트리밍 가동
    ws_client.start()
    if TRADING_MODE == "LIVE":
        private_ws.start()

    # 3. 로컬 실시간 웹 대시보드 (포트 7979) 가동
    web_dashboard = DashboardWebServer(
        port=7979,
        get_status_data_func=get_web_dashboard_data,
        action_handler_func=handle_web_action,
    )
    web_dashboard.start()

    try:
        run_cycle()
    except (requests.exceptions.RequestException, KeyError, ValueError, RuntimeError) as e:
        logger.error(f"최초 실행 실패: {e}")

    scheduler = BlockingScheduler(timezone="Asia/Seoul")

    # 4. 주기별 자동매매 사이클
    scheduler.add_job(
        run_cycle,
        "interval",
        minutes=INTERVAL_MINUTES,
        id="trading_cycle_job",
        replace_existing=True,
    )

    # 5. 매일 아침 09:00 KST 일일 결산 브리핑 발송
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
