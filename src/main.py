import datetime
import logging
import os
import signal
import sys
import time
from logging.handlers import TimedRotatingFileHandler
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

from bithumb_api import BithumbAPI
from bot_controller import BotController
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
    get_dynamic_portfolio_tiers,
    write_json_atomically,
)
from paper_broker import PaperBroker
from private_websocket_manager import BithumbPrivateWebSocketClient
from realtime_engine import RealtimeRiskEngine
from risk_manager import (
    DailyRiskManager,
    TrailingStopTracker,
    build_positions_data,
    calculate_total_equity,
    get_fear_and_greed_index,
    get_held_markets,
    get_kst_now,
    get_kst_now_str,
)
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

# 1. 로깅(Logging) 환경 설정
LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "trading.log")

file_handler = TimedRotatingFileHandler(
    LOG_FILE, when="midnight", interval=1, backupCount=30, encoding="utf-8"
)
file_handler.suffix = "%Y-%m-%d"
formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] [%(filename)s:%(lineno)d] %(message)s")
file_handler.setFormatter(formatter)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)

logging.basicConfig(level=logging.INFO, handlers=[file_handler, console_handler])
logger = logging.getLogger(__name__)

# 2. 환경변수 로드
load_dotenv(override=True)

BITHUMB_ACCESS_KEY = os.getenv("BITHUMB_ACCESS_KEY", "").strip()
BITHUMB_SECRET_KEY = os.getenv("BITHUMB_SECRET_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "config/service_account.json").strip()
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "https://docs.google.com/spreadsheets/d/1BTIxerKa6Dxqwto0Y9A50cAiPMLBJFhuVzhg_8424N4/edit").strip()

TOP_COUNT = int(os.getenv("TOP_COUNT", "3"))
MIN_TRADE_VALUE = float(os.getenv("MIN_TRADE_VALUE", "1000000000"))  # 최소 10억 원
MIN_CHANGE_RATE = float(os.getenv("MIN_CHANGE_RATE", "0.01"))        # 최소 +1.0%
MAX_CHANGE_RATE = float(os.getenv("MAX_CHANGE_RATE", "0.25"))        # 최대 +25.0%

INTERVAL_MINUTES = int(os.getenv("INTERVAL_MINUTES", "5"))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

BTC_CRASH_THRESHOLD_PCT = float(os.getenv("BTC_CRASH_THRESHOLD_PCT", "0.015"))
MAX_DAILY_LOSS_PCT = float(os.getenv("MAX_DAILY_LOSS_PCT", "0.05"))
TRAILING_START_PCT = float(os.getenv("TRAILING_START_PCT", "0.02"))
TRAILING_STOP_PCT = float(os.getenv("TRAILING_STOP_PCT", "0.012"))

MIN_ORDER_KRW = 5000.0
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "2"))
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "0.50"))
MAX_TOTAL_EXPOSURE_PCT = float(os.getenv("MAX_TOTAL_EXPOSURE_PCT", "0.95"))
MAX_ORDER_KRW = float(os.getenv("MAX_ORDER_KRW", "0"))
TRADING_MODE = os.getenv("TRADING_MODE", "LIVE").strip().upper()
PAPER_INITIAL_KRW = float(os.getenv("PAPER_INITIAL_KRW", "1000000"))
PAPER_FEE_RATE = float(os.getenv("PAPER_FEE_RATE", "0"))

# 봇 일시정지 상태 플래그
IS_BOT_PAUSED = False
LATEST_STRATEGIES: dict[str, dict[str, Any]] = {}


def get_is_bot_paused() -> bool:
    return IS_BOT_PAUSED


def set_is_bot_paused(val: bool) -> None:
    global IS_BOT_PAUSED
    IS_BOT_PAUSED = val


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
trailing_tracker = TrailingStopTracker(
    start_profit_pct=TRAILING_START_PCT, trailing_drop_pct=TRAILING_STOP_PCT
)
risk_manager = DailyRiskManager(max_loss_pct=MAX_DAILY_LOSS_PCT)
telegram = TelegramAlert(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
sheets = SheetsManager(
    json_key_path=GOOGLE_SERVICE_ACCOUNT_JSON,
    sheet_name=GOOGLE_SHEET_NAME,
)

paper_broker: PaperBroker | None = None


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


# 실시간 리스크 엔진 및 봇 제어기 초기화
realtime_engine = RealtimeRiskEngine(
    exchange_factory=create_exchange_client,
    order_executor=order_executor,
    order_journal=order_journal,
    risk_manager=risk_manager,
    cooldown_manager=cooldown_manager,
    trade_memory=trade_memory,
    trailing_tracker=trailing_tracker,
    telegram=telegram,
    min_order_krw=MIN_ORDER_KRW,
    latest_strategies=LATEST_STRATEGIES,
)

bot_controller = BotController(
    exchange_factory=create_exchange_client,
    order_executor=order_executor,
    order_journal=order_journal,
    risk_manager=risk_manager,
    trailing_tracker=trailing_tracker,
    trade_memory=trade_memory,
    telegram=telegram,
    get_is_paused=get_is_bot_paused,
    set_is_paused=set_is_bot_paused,
    latest_strategies=LATEST_STRATEGIES,
)

# 빗썸 실시간 웹소켓(WebSocket) 클라이언트 (0.1초 실시간 틱 스트리밍)
ws_client = BithumbWebSocketClient(
    initial_markets=["KRW-BTC"],
    on_price_callback=lambda m, p: realtime_engine.on_price_tick(m, p),
)

private_ws = BithumbPrivateWebSocketClient(
    BITHUMB_ACCESS_KEY,
    BITHUMB_SECRET_KEY,
    on_order=lambda event: order_journal.apply_private_order_event(event),
)


def cancel_bot_open_orders(bithumb: BithumbAPI, market: str | None = None) -> int:
    """봇이 발행한 미체결 주문만 선별 취소하여 외부/수동 주문 보호"""
    return realtime_engine.cancel_bot_open_orders(market=market)


def check_btc_market_crash(bithumb: BithumbAPI, threshold_pct: float = BTC_CRASH_THRESHOLD_PCT) -> tuple[bool, str, str]:
    """Classify BTC market state into (is_crash: bool, btc_regime: str, status_msg: str) with Fail-Closed policy."""
    try:
        btc_candles_5m = bithumb.get_candles(unit=INTERVAL_MINUTES, count=20, market="KRW-BTC")
        btc_candles_1h = bithumb.get_candles(unit=60, count=50, market="KRW-BTC")
        if not btc_candles_5m or len(btc_candles_5m) < 5:
            logger.warning("BTC 캔들 데이터 수신 부족으로 신규 매수 차단 (Fail-Closed)")
            return True, "CRASH", "BTC 데이터 부족 (Fail-Closed: 안전 관망)"

        regime_data = classify_btc_regime(btc_candles_5m, btc_candles_1h, crash_threshold_pct=threshold_pct)
        regime = regime_data.get("regime", "NORMAL")
        is_crash = regime == "CRASH"
        return is_crash, regime, regime_data.get("reason", "BTC 정상 안정세")
    except Exception as e:
        logger.warning(f"BTC 시장 상태 검사 실패 (Fail-Closed 작동): {e}")
        return True, "CRASH", f"BTC 조회 실패 (Fail-Closed: {e})"


def send_daily_morning_report():
    """매일 아침 09:00 KST 일일 결산 모닝 리포트 전송"""
    now_str = get_kst_now_str()
    logger.info(f"📊 [아침 9시 일일 결산 브리핑 발송: {now_str}]")

    try:
        bithumb = create_exchange_client()
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
    except Exception as e:
        logger.error(f"모닝 리포트 발송 실패: {e}")


def run_cycle():
    """
    [MTF + 호가창 수급 + ATR 변동성 + 자가학습 메모리 + 차트 이미지 + 복리 자금관리] 5분 오케스트레이션 사이클
    """
    load_dotenv(override=True)

    raw_markets = os.getenv("MARKETS", "AUTO").strip()
    is_auto_mode = raw_markets.upper() == "AUTO"
    top_count = int(os.getenv("TOP_COUNT", "3"))
    min_trade_val = float(os.getenv("MIN_TRADE_VALUE", "1000000000"))
    min_change = float(os.getenv("MIN_CHANGE_RATE", "0.01"))
    max_change = float(os.getenv("MAX_CHANGE_RATE", "0.25"))
    btc_crash_pct = float(os.getenv("BTC_CRASH_THRESHOLD_PCT", "0.015"))
    max_daily_loss = float(os.getenv("MAX_DAILY_LOSS_PCT", "0.05"))
    trailing_start = float(os.getenv("TRAILING_START_PCT", "0.02"))
    trailing_stop = float(os.getenv("TRAILING_STOP_PCT", "0.012"))

    trailing_tracker.start_profit_pct = trailing_start
    trailing_tracker.trailing_drop_pct = trailing_stop
    risk_manager.max_loss_pct = max_daily_loss

    now_dt = get_kst_now()
    now_str = get_kst_now_str()
    logger.info(f"============================================================")
    logger.info(f"🚀 [5분 AI 퀀트 트레이딩 사이클 가동: {now_str}]")
    logger.info(f"============================================================")

    try:
        bithumb = create_exchange_client()
        analyzer = GeminiAnalyzer(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

        # 1. 잔고 및 자산 현황 조회
        balances = bithumb.get_balances()
        krw_available = balances.get("KRW", {}).get("balance", 0.0)
        current_total_equity = calculate_total_equity(balances, bithumb)
        held_markets = get_held_markets(balances, bithumb)

        stale_state_count = trailing_tracker.reconcile_markets(held_markets)
        if stale_state_count:
            logger.info("보유 잔고와 불일치하는 과거 트레일링 상태 %d건 자동 정리", stale_state_count)

        # 2. 미체결 봇 주문 정리 및 최우선 호가 재정정
        canceled_stale = realtime_engine.clean_stale_orders(max_age_seconds=180)
        requoted = realtime_engine.requote_pending_orders()
        if canceled_stale > 0 or requoted > 0:
            balances = bithumb.get_balances()
            krw_available = balances.get("KRW", {}).get("balance", 0.0)

        # 3. 일일 리스크 매니저 갱신
        is_kill_switch, daily_pnl = risk_manager.update_daily_equity(current_total_equity, now_dt)
        is_cooldown, cd_minutes = risk_manager.is_cooling_down()

        # 3-2. 총 자산 규모에 따른 동적 포트폴리오 슬롯 및 한도 자동 산출 (Auto-Scaling)
        dyn_max_positions, dyn_max_pos_pct, dyn_top_count = get_dynamic_portfolio_tiers(current_total_equity)
        risk_guard.update_limits(max_open_positions=dyn_max_positions, max_position_pct=dyn_max_pos_pct)
        logger.info(
            f"📊 [스마트 자산 티어] 총 자산 {current_total_equity:,.0f}원 ➜ 최대 {dyn_max_positions}종목 분할 (종목당 {dyn_max_pos_pct*100:.0f}% 한도, 스크리닝 상위 {dyn_top_count}개)"
        )

        # 3-3. 비트코인 급락 및 시장 레짐 감시
        is_btc_crashing, btc_regime, btc_status_msg = check_btc_market_crash(bithumb, threshold_pct=btc_crash_pct)
        if is_btc_crashing:
            logger.warning(f"⚠️ [비트코인 급락 위험 감지] 레짐: {btc_regime} ({btc_status_msg})")

        fng = get_fear_and_greed_index()
        logger.info(f"📊 [자산 요약] 총 자산: {current_total_equity:,.0f}원 | 원화: {krw_available:,.0f}원 | 당일 손익: {daily_pnl*100:+.2f}% | 공포탐욕: {fng['desc']}")

        bot_state_badge = "⏸️ 일시정지 중" if IS_BOT_PAUSED else ("🛑 킬스위치 발동" if is_kill_switch else ("❄️ 쿨다운 대기" if is_cooldown else "🟢 정상 가동 중"))

        # 대시보드 및 구글 시트 동기화
        bot_controller.get_dashboard_data()
        sheets.update_performance_tab(
            total_trades=risk_manager.total_trades_today,
            win_trades=risk_manager.win_trades_today,
            realized_pnl_krw=risk_manager.realized_pnl_krw,
            daily_history=risk_manager.daily_history,
        )

        # 4. 대상 마켓 선정 (포지션 만석 시 신규 스크리닝 및 AI 호출 생략으로 쿼터 절약)
        target_markets: list[str] = []
        if is_auto_mode:
            if len(held_markets) >= dyn_max_positions:
                logger.info(
                    f"🔒 [보유 슬롯 만석 ({len(held_markets)}/{dyn_max_positions})] 신규 종목 스크리닝 및 AI 분석을 생략하고 보유 포지션 실시간 감시에 집중합니다."
                )
                target_markets = held_markets
            else:
                screener = MarketScreener(
                    bithumb,
                    min_trade_value_krw=min_trade_val,
                    min_change_rate=min_change,
                    max_change_rate=max_change,
                )
                screened_items = screener.scan_markets(
                    top_count=dyn_top_count, held_markets=held_markets
                )
                target_markets = [item["market"] for item in screened_items]
        else:
            fixed_list = [m.strip().upper() for m in raw_markets.split(",") if m.strip()]
            target_markets = list(dict.fromkeys(held_markets + fixed_list))

        logger.info(f"이번 사이클 최종 분석 대상 마켓 ({len(target_markets)}개): {target_markets}")

        # 🧹 구글 시트 Strategy 탭의 과거 비감시 종목 정리
        sheets.prune_unmonitored_strategies(target_markets)

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
                avg_buy_price = coin_info["avg_buy_price"]
                current_price = bithumb.get_current_price(market)
                coin_value = coin_available * current_price

                candles_5m = bithumb.get_candles(unit=INTERVAL_MINUTES, count=30, market=market)
                candles_1h = bithumb.get_candles(unit=60, count=50, market=market)
                orderbook = bithumb.get_orderbook(market)

                # =========================================================================
                # 🎯 [최우선 1: 50% 분할 익절 및 가속 트레일링 스탑]
                # =========================================================================
                if coin_value >= MIN_ORDER_KRW and avg_buy_price > 0:
                    action_type, peak_p, trigger_p, peak_profit_pct, realized_profit_pct = (
                        trailing_tracker.check_position(market, current_price, avg_buy_price)
                    )

                    if action_type == "PARTIAL_TP":
                        sell_vol = coin_available * 0.5
                        sell_val = sell_vol * current_price
                        if sell_val >= MIN_ORDER_KRW:
                            logger.info(
                                f"🎉 [{korean_name} / {market} 1차 50% 분할익절 발동] 현재가 {current_price:,.2f}원(+{realized_profit_pct:.2f}%). 50% 물량 시장가 익절!"
                            )
                            cancel_bot_open_orders(bithumb, market)

                            order_res = order_executor.submit(
                                bithumb,
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

                            caption = (
                                f"🎉 <b>[{korean_name}({market}) 1차 50% 분할익절 체결]</b>\n"
                                f"• 체결가: {current_price:,.2f} KRW (+{realized_profit_pct:.2f}%)\n"
                                f"• 실현수익: +{pnl_krw:,.0f} KRW 💰\n"
                                f"• 매도수량: {sell_vol:.8f} {currency}\n"
                                f"• 남은 50%: 무한 트레일링 러너 자동 전환\n"
                                f"• 일시: {now_str}"
                            )
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
                                    "Stop_Loss": avg_buy_price,
                                    "Target_Price": current_price,
                                    "Current_Balance_KRW": int(krw_available + sell_val),
                                    "Status_Reason": f"1차 50% 분할 익절 (+{realized_profit_pct:.2f}%)",
                                }
                            )

                    elif action_type == "TRAILING_STOP":
                        logger.info(
                            f"🎯 [{korean_name} / {market} 트레일링 스탑 익절 발동] 최고점 {peak_p:,.2f}원(+{peak_profit_pct:.2f}%) ➜ 현재 {current_price:,.2f}원(+{realized_profit_pct:.2f}%). 잔여 전량 시장가 익절!"
                        )
                        cancel_bot_open_orders(bithumb, market)

                        order_res = order_executor.submit(
                            bithumb,
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

                    if hold_duration_sec >= 3600 and (-1.0 <= pnl_pct_current <= 1.0):
                        logger.info(
                            f"⏳ [{korean_name} / {market}] 60분 횡보 타임스탑 발동! (손익률: {pnl_pct_current:+.2f}%, 보유시간: {hold_duration_sec/60:.0f}분) ➜ 신규 기회를 위해 시장가 전량 청산"
                        )
                        cancel_bot_open_orders(bithumb, market)

                        order_res = order_executor.submit(
                            bithumb,
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

                # =========================================================================
                # 🧠 [하이브리드 2단계 게이팅: 1차 퀀트 사전 필터 ➜ 2차 AI 최종 승인]
                # ※ 1차 퀀트 조건 미충족(95%) 시 Gemini API를 호출하지 않아 하루 쿼터 95% 절약
                # =========================================================================
                local_entry = entry_signal(
                    candles=candles_5m,
                    candles_1h=candles_1h,
                    btc_regime=btc_regime,
                )
                in_cooldown, cd_remaining = cooldown_manager.is_in_cooldown(market)
                is_holding = (coin_value >= MIN_ORDER_KRW and avg_buy_price > 0)

                should_call_ai = (
                    analyzer is not None
                    and not is_holding
                    and not in_cooldown
                    and local_entry["allow_buy"]
                    and not is_btc_crashing
                )

                if should_call_ai and analyzer is not None:
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
                else:
                    # 1차 퀀트 관망 또는 기보유 포지션 기준선 산출 (Zero API Quota)
                    quant_reason = (
                        f"기보유 포지션 퀀트 감시 ({local_entry['reason']})"
                        if is_holding
                        else (
                            f"재진입 쿨다운 대기중 ({cd_remaining/60:.0f}분 남음)"
                            if in_cooldown
                            else (
                                f"BTC 레짐 경보 ({btc_status_msg})"
                                if is_btc_crashing
                                else f"1차 퀀트 관망 대기: {local_entry['reason']}"
                            )
                        )
                    )
                    strategy = {
                        "status": "ACTIVE",
                        "action": "HOLD",
                        "entry_price": local_entry["entry_price"],
                        "target_price": local_entry["target_price"],
                        "stop_loss": local_entry["stop_loss"],
                        "alloc_pct": 0.0,
                        "reason": quant_reason,
                    }

                status = strategy.get("status", "ACTIVE")
                action = strategy.get("action", "HOLD")
                entry_price = strategy.get("entry_price", current_price)
                target_price = strategy.get("target_price", local_entry["target_price"])
                stop_loss = strategy.get("stop_loss", local_entry["stop_loss"])
                alloc_pct = strategy.get("alloc_pct", 0.3)
                reason = strategy.get("reason", "자동 분석")

                if in_cooldown and action == "BUY":
                    action = "HOLD"
                    reason = f"재진입 쿨다운 대기중 ({cd_remaining/60:.0f}분 남음) | {reason}"

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

                # 📊 최종 정량 게이트 및 리스크 필터가 반영된 확정 전략을 구글 시트에 갱신
                sheets.update_strategy(
                    market,
                    {
                        "status": status,
                        "action": action,
                        "entry_price": entry_price,
                        "target_price": target_price,
                        "stop_loss": stop_loss,
                        "alloc_pct": alloc_pct,
                        "reason": reason,
                    },
                    now_str,
                    korean_name=korean_name,
                )

                if status != "ACTIVE":
                    continue

                # 5. [손절 검사]
                if coin_value >= MIN_ORDER_KRW and stop_loss > 0 and current_price <= stop_loss:
                    logger.warning(
                        f"🚨 [{market} 손절 발생] 현재가({current_price:,.2f}원) <= 손절가({stop_loss:,.2f}원). 전량 시장가 매도!"
                    )
                    trailing_tracker.clear(market)
                    cancel_bot_open_orders(bithumb, market)

                    order_res = order_executor.submit(
                        bithumb,
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
                        reason=reason,
                    )

                    caption = (
                        f"🚨 <b>[{korean_name}({market}) 손절 매도 실행]</b>\n"
                        f"• 진입 평단가: {avg_buy_price:,.2f} KRW\n"
                        f"• 손절 체결가: {current_price:,.2f} KRW\n"
                        f"• <b>실현 손실: {pnl_krw:,.0f} KRW ({loss_pct:.2f}%)</b>\n"
                        f"• 사유: <i>{reason}</i>\n"
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
                        telegram.send_debounced_message(
                            category_key=f"btc_crash_{market}",
                            text=(
                                f"⚠️ <b>[{korean_name}({market}) 매수 차단 - BTC 급락 방어]</b>\n"
                                f"• 사유: <i>{btc_status_msg}</i>\n"
                                f"• 대장주(BTC) 급락으로 인한 알트코인 동반 폭락 위험 방지"
                            ),
                            min_interval_sec=900.0,
                        )
                        continue

                    # 💰 자산 규모별 동적 슬롯(2~4개) 예산 및 1% 리스크 관리 결합 (Auto-Scaling)
                    effective_capital = current_total_equity if current_total_equity > 0 else (krw_available + max(0.0, risk_manager.realized_pnl_krw))
                    max_slot_budget = (effective_capital / max(1, dyn_max_positions))
                    slot_budget = min(krw_available, max_slot_budget * (alloc_pct / dyn_max_pos_pct if alloc_pct < dyn_max_pos_pct else 1.0))

                    order_price = entry_price if (0 < entry_price <= current_price * 1.002) else current_price
                    order_price = bithumb.round_price_to_tick(order_price)

                    risk_based_budget = calculate_risk_position_size(
                        total_equity=effective_capital,
                        entry_price=order_price,
                        stop_loss=stop_loss,
                        risk_fraction=0.01,
                        fee_rate=0.0004,
                        slippage_rate=0.001,
                        max_position_pct=dyn_max_pos_pct,
                        min_order_krw=MIN_ORDER_KRW,
                    )

                    effective_risk_budget = risk_based_budget if risk_based_budget > 0 else slot_budget
                    trade_budget = min(krw_available, slot_budget, effective_risk_budget)

                    SAFE_ORDER_KRW = 5500.0
                    if trade_budget < SAFE_ORDER_KRW and krw_available >= SAFE_ORDER_KRW:
                        trade_budget = min(krw_available, max(max_slot_budget, SAFE_ORDER_KRW))

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

                    order_volume = (trade_budget * 0.995) / order_price
                    formatted_volume = bithumb.round_volume(market, order_volume)

                    if formatted_volume <= 0:
                        logger.warning(f"[{market}] 계산된 수량이 너무 작아 주문 취소: {formatted_volume}")
                        continue

                    logger.info(
                        f"🛒 [{korean_name} / {market} AI 승인 매수 실행] 주문가={order_price:,.2f}원, 수량={formatted_volume:.6f}, 투입금액={int(trade_budget):,d}원"
                    )

                    cancel_bot_open_orders(bithumb, market)

                    order_res = order_executor.submit(
                        bithumb,
                        market=market,
                        side="bid",
                        price=order_price,
                        volume=formatted_volume,
                        ord_type="limit",
                    )
                    order_uuid = order_res.get("uuid", "UNKNOWN")
                    trailing_tracker.set_entry_time(market)

                    chart_img = chart_renderer.render_trade_chart(
                        market=market,
                        korean_name=korean_name,
                        candles=candles_5m,
                        entry_price=order_price,
                        target_price=target_price,
                        stop_loss=stop_loss,
                        action="BUY",
                        reason=reason,
                    )

                    caption = (
                        f"🛒 <b>[{korean_name}({market}) AI 신규 매수 체결]</b>\n"
                        f"• 매수가: {order_price:,.2f} KRW\n"
                        f"• 주문수량: {formatted_volume:.6f} {currency}\n"
                        f"• <b>투입금액: {int(trade_budget):,d} KRW (슬롯비중: {alloc_pct*100:.0f}%)</b>\n"
                        f"• 목표가: {target_price:,.2f} KRW | 손절가: {stop_loss:,.2f} KRW\n"
                        f"• 사유: <i>{reason}</i>\n"
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
                            "Side": "BUY",
                            "Order_Type": "LIMIT",
                            "Price": order_price,
                            "Volume": f"{formatted_volume:.6f}",
                            "Total_KRW": int(trade_budget),
                            "Realized_PnL_Pct": "0.00%",
                            "Stop_Loss": stop_loss,
                            "Target_Price": target_price,
                            "Current_Balance_KRW": int(krw_available - trade_budget),
                            "Status_Reason": f"정량 5대 지표 및 Gemini AI 승인 진입 ({reason})",
                        }
                    )

            except Exception as e:
                logger.error(f"[{market}] 매매 사이클 오류 발생: {e}", exc_info=True)

    except Exception as e:
        logger.error(f"전체 트레이딩 사이클 예외 발생: {e}", exc_info=True)


def main():
    logger.info("============================================================")
    logger.info("  Bithumb AI Pro Quant Trading Bot v4.5 가동 시작")
    logger.info("============================================================")

    # 1. 텔레그램 명령어 리스너 가동
    telegram.start_command_listener(
        status_callback=bot_controller.get_status_message,
        balance_callback=bot_controller.get_balance_message,
        panic_callback=bot_controller.execute_panic_sell,
        pause_callback=bot_controller.pause_bot,
        resume_callback=bot_controller.resume_bot,
    )

    # 2. 로컬 실시간 웹 대시보드 서버 가동
    web_server = DashboardWebServer(
        host="0.0.0.0",
        port=7979,
        data_provider=bot_controller.get_dashboard_data,
        action_handler=bot_controller.handle_web_action,
    )
    web_server.start()

    # 3. 빗썸 실시간 웹소켓(WebSocket) 스트리밍 가동
    ws_client.start()
    private_ws.start()

    # 4. APScheduler 스케줄러 등록
    scheduler = BackgroundScheduler(timezone="Asia/Seoul")
    scheduler.add_job(
        run_cycle,
        "interval",
        minutes=INTERVAL_MINUTES,
        id="run_trading_cycle",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        send_daily_morning_report,
        "cron",
        hour=9,
        minute=0,
        id="morning_daily_report",
        max_instances=1,
    )
    scheduler.start()
    logger.info(f"⏰ APScheduler 가동 완료 ({INTERVAL_MINUTES}분 주기 매매 및 매일 09:00 모닝 리포트)")

    # 5. 시작 즉시 첫 번째 사이클 1회 실행
    try:
        run_cycle()
    except Exception as e:
        logger.error(f"초기 사이클 실행 중 오류: {e}")

    # 6. 프로세스 종료 시그널 핸들러 등록
    def _handle_exit(sig, frame):
        logger.info("🛑 프로세스 종료 시그널 감지. 자원을 안전하게 해제합니다...")
        ws_client.stop()
        private_ws.stop()
        web_server.stop()
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGINT, _handle_exit)
    signal.signal(signal.SIGTERM, _handle_exit)

    # 7. 메인 스레드 유지 루프
    while True:
        try:
            time.sleep(1)
        except (KeyboardInterrupt, SystemExit):
            _handle_exit(None, None)


if __name__ == "__main__":
    main()