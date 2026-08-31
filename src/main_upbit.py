"""
Upbit AI Pro Quant Trading Bot
- 업비트(Upbit) API 기반 5분 주기 AI 퀀트 자동매매 오케스트레이터
- REST API + 0.1초 실시간 WebSocket 스트리밍 + Google Gemini AI 분석
- data/upbit/* 및 logs/trading_upbit.log를 통한 완벽한 데이터/로그 물리적 격리
- KRW-HOLO 수동 매매 보호 종목 7중 차단망 적용
- 웹 대시보드 (기본 포트: 7980) 및 텔레그램 양방향 원격 제어
"""

from datetime import datetime, timedelta
import logging
import os
import signal
import sys
import time
from logging.handlers import TimedRotatingFileHandler
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

from bot_controller import BotController
from chart_renderer import ChartRenderer
from db_manager import migrate_legacy_json_to_sqlite
from exchange_adapter import ExchangeAdapter, UpbitAdapter
from gemini_analyzer import GeminiAnalyzer
from market_screener import MarketScreener
from order_safety import (
    AmbiguousOrderError,
    CooldownManager,
    OrderFillProcessor,
    OrderJournal,
    OrderStatus,
    RiskGuard,
    SafeOrderExecutor,
    calculate_risk_position_size,
    get_dynamic_portfolio_tiers,
    write_json_atomically,
)
from paper_broker import PaperBroker
from realtime_engine import RealtimeRiskEngine
from runtime_config import load_runtime_risk_settings
from risk_manager import (
    DailyRiskManager,
    StrategyCacheManager,
    TrailingStopTracker,
    build_positions_data,
    calculate_total_equity,
    get_fear_and_greed_index,
    get_held_markets,
    get_kst_now,
    get_kst_now_str,
)
from strategy_engine import (
    StrategyPolicy,
    calculate_relative_strength,
    calculate_vwap,
    classify_btc_regime,
    entry_signal,
    select_completed_candles,
)
from telegram_alert import TelegramAlert
from trade_memory import TradeMemoryManager
from trading_orchestrator import TradingOrchestrator
from upbit_api import UpbitAPI, get_upbit_excluded_markets
from upbit_private_websocket import UpbitPrivateWebSocketClient
from upbit_websocket import UpbitWebSocketClient
from web_server import DashboardWebServer

# 윈도우 cp949 인코딩 에러 방지 (이모지 및 한글 UTF-8 표준화)
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (OSError, AttributeError):
        pass

# 1. 로깅 환경 설정 (logs/trading_upbit.log)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(PROJECT_ROOT, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "trading_upbit.log")

file_handler = TimedRotatingFileHandler(
    LOG_FILE, when="midnight", interval=1, backupCount=30, encoding="utf-8"
)
file_handler.suffix = "%Y-%m-%d"
formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] [%(filename)s:%(lineno)d] %(message)s")
file_handler.setFormatter(formatter)

handlers = [file_handler]
if sys.stdout is not None:
    console_handler = logging.StreamHandler(sys.stdout)
    # 포그라운드 콘솔은 운영 중 확인이 필요한 경고 이상만 출력한다.
    console_handler.setLevel(logging.WARNING)
    console_handler.setFormatter(formatter)
    handlers.append(console_handler)

logging.basicConfig(level=logging.INFO, handlers=handlers)
logger = logging.getLogger(__name__)

# 2. 업비트 환경변수 우선 로드 (.env.upbit -> .env)
UPBIT_ENV_FILE = os.path.join(PROJECT_ROOT, ".env.upbit")
if os.path.exists(UPBIT_ENV_FILE):
    load_dotenv(UPBIT_ENV_FILE, override=True)
else:
    load_dotenv(override=True)

UPBIT_ACCESS_KEY = os.getenv("UPBIT_ACCESS_KEY", "").strip()
UPBIT_SECRET_KEY = os.getenv("UPBIT_SECRET_KEY", "").strip()
UPBIT_API_BASE_URL = os.getenv("UPBIT_API_BASE_URL", "https://api.upbit.com/v1").strip()
UPBIT_WEBSOCKET_URL = os.getenv("UPBIT_WEBSOCKET_URL", "wss://api.upbit.com/websocket/v1").strip()
UPBIT_PRIVATE_WEBSOCKET_URL = os.getenv("UPBIT_PRIVATE_WEBSOCKET_URL", "wss://api.upbit.com/websocket/v1/private").strip()

# 수동 격리 종목 설정 (KRW-HOLO 강제 포함)
UPBIT_EXCLUDED_MARKETS = os.getenv("UPBIT_EXCLUDED_MARKETS", "KRW-HOLO,HOLO").strip()
os.environ["UPBIT_EXCLUDED_MARKETS"] = UPBIT_EXCLUDED_MARKETS

TELEGRAM_BOT_TOKEN = os.getenv("UPBIT_TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("UPBIT_TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID", "").strip()


WEB_PORT = int(os.getenv("UPBIT_WEB_PORT") or os.getenv("WEB_PORT", "7980"))

TOP_COUNT = int(os.getenv("TOP_COUNT", "3"))
MIN_TRADE_VALUE = float(os.getenv("MIN_TRADE_VALUE", "1000000000"))  # 최소 10억 원
MIN_CHANGE_RATE = float(os.getenv("MIN_CHANGE_RATE", "0.005"))        # 최소 +0.5%
MAX_CHANGE_RATE = float(os.getenv("MAX_CHANGE_RATE", "0.25"))        # 최대 +25.0%

INTERVAL_MINUTES = int(os.getenv("INTERVAL_MINUTES", "5"))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

_risk_settings = load_runtime_risk_settings()
BTC_CRASH_THRESHOLD_PCT = _risk_settings.btc_crash_threshold_pct
MAX_DAILY_LOSS_PCT = _risk_settings.max_daily_loss_pct
TRAILING_START_PCT = _risk_settings.trailing_start_pct
TRAILING_STOP_PCT = _risk_settings.trailing_stop_pct

MIN_ORDER_KRW = 5000.0  # 업비트 KRW 마켓 최소 주문 금액
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "2"))
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "0.50"))
MAX_TOTAL_EXPOSURE_PCT = float(os.getenv("MAX_TOTAL_EXPOSURE_PCT", "0.95"))
MAX_ORDER_KRW = float(os.getenv("MAX_ORDER_KRW", "0"))
TRADING_MODE = os.getenv("TRADING_MODE", "LIVE").strip().upper()
PAPER_INITIAL_KRW = float(os.getenv("PAPER_INITIAL_KRW", "1000000"))
PAPER_FEE_RATE = float(os.getenv("PAPER_FEE_RATE", "0.0005"))

# 3. 데이터 저장 디렉토리 분리 (data/upbit/)
DATA_DIR = os.path.join(PROJECT_ROOT, "data", "upbit")
os.makedirs(DATA_DIR, exist_ok=True)

# 봇 일시정지 상태 플래그
IS_BOT_PAUSED = False
LATEST_STRATEGIES: dict[str, dict[str, Any]] = {}


def get_is_bot_paused() -> bool:
    return IS_BOT_PAUSED


def set_is_bot_paused(val: bool) -> None:
    global IS_BOT_PAUSED
    IS_BOT_PAUSED = val


# 전역 인스턴스 초기화 (업비트 독립 데이터 디렉토리 적용)
chart_renderer = ChartRenderer()
trade_memory = TradeMemoryManager(data_dir=DATA_DIR, exchange_scope="upbit")
order_journal = OrderJournal(data_dir=DATA_DIR, exchange_scope="upbit")
order_executor = SafeOrderExecutor(order_journal)
cooldown_manager = CooldownManager(data_dir=DATA_DIR)
risk_guard = RiskGuard(
    min_order_krw=MIN_ORDER_KRW,
    max_open_positions=MAX_OPEN_POSITIONS,
    max_position_pct=MAX_POSITION_PCT,
    max_total_exposure_pct=MAX_TOTAL_EXPOSURE_PCT,
    max_order_krw=MAX_ORDER_KRW,
)
trailing_tracker = TrailingStopTracker(
    start_profit_pct=TRAILING_START_PCT,
    trailing_drop_pct=TRAILING_STOP_PCT,
    data_dir=DATA_DIR,
)
risk_manager = DailyRiskManager(max_loss_pct=MAX_DAILY_LOSS_PCT, data_dir=DATA_DIR)
telegram = TelegramAlert(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)

fill_processor = OrderFillProcessor(
    order_journal=order_journal,
    risk_manager=risk_manager,
    trade_memory=trade_memory,
    trailing_tracker=trailing_tracker,
    cooldown_manager=cooldown_manager,
    telegram=telegram,
)

paper_broker: PaperBroker | None = None
cycle_orchestrator = TradingOrchestrator(logger)


def create_exchange_client() -> ExchangeAdapter:
    """업비트 거래소 클라이언트 생성 (PAPER 모드인 경우 모의투자 어댑터 반환)"""
    global paper_broker
    live_client = UpbitAPI(UPBIT_ACCESS_KEY, UPBIT_SECRET_KEY)

    if TRADING_MODE != "PAPER":
        return UpbitAdapter(
            live_client,
            data_dir=DATA_DIR,
            web_port=WEB_PORT,
            excluded_markets=frozenset(get_upbit_excluded_markets()),
        )
    if paper_broker is None:
        paper_broker = PaperBroker(
            live_client,
            PAPER_INITIAL_KRW,
            PAPER_FEE_RATE,
            data_dir=DATA_DIR,
        )
        logger.warning(f"🧪 [업비트 PAPER 모드] 가상 주문 모드로 구동되며 {DATA_DIR}/paper_account.json이 사용됩니다.")
    return UpbitAdapter(
        paper_broker,
        data_dir=DATA_DIR,
        web_port=WEB_PORT,
        excluded_markets=frozenset(get_upbit_excluded_markets()),
    )


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
    exchange_name="업비트",
    web_port=WEB_PORT,
    # 지연 평가 람다로 웹소켓 생성 이후에도 최신 건강 상태를 대시보드에 제공한다.
    get_feed_health=lambda: ws_client.get_health_status(),
)

strategy_cache_mgr = StrategyCacheManager(exchange_name="upbit")

# 업비트 실시간 Public / Private WebSocket 클라이언트
ws_client = UpbitWebSocketClient(
    initial_markets=["KRW-BTC"],
    on_price_callback=lambda m, p: realtime_engine.on_price_tick(m, p),
)

private_ws: UpbitPrivateWebSocketClient | None = None
if UPBIT_ACCESS_KEY and UPBIT_SECRET_KEY:
    private_ws = UpbitPrivateWebSocketClient(
        UPBIT_ACCESS_KEY,
        UPBIT_SECRET_KEY,
        # Private 이벤트는 알림만 반영하고 손익 확정은 REST 대사에서만 수행한다.
        on_order=lambda event: order_journal.apply_private_order_event(
            event, fill_processor=fill_processor, require_rest_confirmation=True,
        ),
    )


def cancel_bot_open_orders(upbit: UpbitAPI, market: str | None = None) -> int:
    """봇이 발행한 미체결 주문만 선별 취소하여 외부/수동 주문 보호"""
    return realtime_engine.cancel_bot_open_orders(market=market)


def check_btc_market_crash(upbit: UpbitAPI, threshold_pct: float = BTC_CRASH_THRESHOLD_PCT) -> tuple[bool, str, str]:
    return cycle_orchestrator.classify_market_regime(
        upbit, interval_minutes=INTERVAL_MINUTES, crash_threshold_pct=threshold_pct,
    )


def send_daily_morning_report():
    """매일 아침 09:00 KST 업비트 일일 결산 모닝 리포트를 전송한다."""
    now_str = get_kst_now_str()
    logger.info(f"📊 [업비트 아침 9시 일일 결산 브리핑 발송: {now_str}]")

    try:
        upbit = create_exchange_client()
        fng = get_fear_and_greed_index()
        balances = upbit.get_balances()
        total_equity = calculate_total_equity(balances, upbit)
        krw_avail = balances.get("KRW", {}).get("balance", 0.0)

        daily_pnl_krw = total_equity - risk_manager.daily_start_equity
        daily_pnl_pct = (
            (daily_pnl_krw / risk_manager.daily_start_equity) * 100.0
            if risk_manager.daily_start_equity > 0
            else 0.0
        )

        held_markets = get_held_markets(balances, upbit)
        held_names = [f"{upbit.get_korean_name(m)}({m.split('-')[-1]})" for m in held_markets]
        held_desc = ", ".join(held_names) if held_names else "없음 (100% 현금 보유)"

        telegram.send_message(
            f"🌅 <b>[업비트 AI 퀀트 봇 - 09:00 KST 일일 성과 결산 브리핑]</b>\n\n"
            f"• <b>총 평가 자산:</b> {total_equity:,.0f} KRW\n"
            f"• <b>금일 자산 변동:</b> {daily_pnl_krw:+,.0f} KRW ({daily_pnl_pct:+.2f}%)\n"
            f"• <b>금일 확정 실현 손익:</b> {risk_manager.realized_pnl_krw:+,.0f} KRW (총 {risk_manager.total_trades_today}회 거래)\n"
            f"• <b>가용 원화 잔고:</b> {krw_avail:,.0f} KRW\n"
            f"• <b>현재 보유 포지션:</b> {held_desc}\n"
            f"• <b>크립토 공포/탐욕 지수:</b> {fng['desc']}\n"
            f"• <b>웹 대시보드:</b> <code>http://localhost:{WEB_PORT}</code>\n"
            f"• <b>기준 일시:</b> {now_str}"
        )
    except Exception as e:
        logger.error(f"업비트 모닝 리포트 발송 실패: {e}")


def run_cycle():
    """
    [업비트 5분 오케스트레이션 사이클]
    - 자산 티어 산출 -> 스크리닝 (HOLO 제외) -> 정량 지표 + AI 분석 -> 리스크 가드 -> 안전 주문 집행
    """
    if os.path.exists(UPBIT_ENV_FILE):
        load_dotenv(UPBIT_ENV_FILE, override=True)
    else:
        load_dotenv(override=True)

    raw_markets = os.getenv("MARKETS", "AUTO").strip()
    is_auto_mode = raw_markets.upper() == "AUTO"
    top_count = int(os.getenv("TOP_COUNT", "3"))
    min_trade_val = float(os.getenv("MIN_TRADE_VALUE", "1000000000"))
    min_change = float(os.getenv("MIN_CHANGE_RATE", "0.005"))
    max_change = float(os.getenv("MAX_CHANGE_RATE", "0.25"))
    risk_settings = load_runtime_risk_settings()
    btc_crash_pct = risk_settings.btc_crash_threshold_pct
    max_daily_loss = risk_settings.max_daily_loss_pct
    trailing_start = risk_settings.trailing_start_pct
    trailing_stop = risk_settings.trailing_stop_pct

    trailing_tracker.start_profit_pct = trailing_start
    trailing_tracker.trailing_drop_pct = trailing_stop
    risk_manager.max_loss_pct = max_daily_loss

    now_dt = get_kst_now()
    now_str = get_kst_now_str()
    logger.info("============================================================")
    logger.info(f"🚀 [업비트 5분 AI 퀀트 트레이딩 사이클 가동: {now_str}]")
    logger.info("============================================================")

    try:
        upbit = create_exchange_client()
        analyzer = GeminiAnalyzer(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

        # 0. REST 기반 미완료 주문 체결 상태 자동 재조정 (공통 오케스트레이터)
        cycle_orchestrator.reconcile_orders(upbit, order_journal, fill_processor, label="업비트 ")

        snapshot = cycle_orchestrator.refresh_portfolio(
            upbit, calculate_total_equity=calculate_total_equity, get_held_markets=get_held_markets,
            trailing_tracker=trailing_tracker, realtime_engine=realtime_engine, risk_manager=risk_manager,
            risk_guard=risk_guard, get_portfolio_tiers=get_dynamic_portfolio_tiers, now=now_dt,
        )
        balances, krw_available = snapshot.balances, snapshot.krw_available
        current_total_equity, held_markets = snapshot.total_equity, snapshot.held_markets
        stale_state_count = snapshot.stale_states
        if stale_state_count:
            logger.info("보유 잔고와 불일치하는 과거 트레일링 상태 %d건 자동 정리", stale_state_count)
        canceled_stale, requoted = snapshot.canceled_stale, snapshot.requoted
        if canceled_stale > 0 or requoted > 0:
            logger.info("업비트 미체결 주문 정리/정정: 취소 %d건, 정정 %d건", canceled_stale, requoted)
        is_kill_switch, daily_pnl = snapshot.is_kill_switch, snapshot.daily_pnl
        is_cooldown, cd_minutes = snapshot.is_cooldown, snapshot.cooldown_minutes
        dyn_max_positions, dyn_max_pos_pct, dyn_top_count = snapshot.max_positions, snapshot.max_position_pct, snapshot.top_count
        tot_disp = f"{current_total_equity:,.2f}원" if (0 < current_total_equity < 100 or current_total_equity % 1 != 0 and current_total_equity < 1000) else f"{current_total_equity:,.0f}원"
        krw_disp = f"{krw_available:,.2f}원" if (0 < krw_available < 100 or krw_available % 1 != 0 and krw_available < 1000) else f"{krw_available:,.0f}원"
        logger.info(
            f"📊 [업비트 스마트 자산 티어] 총 자산 {tot_disp} ➜ 최대 {dyn_max_positions}종목 분할 (종목당 {dyn_max_pos_pct*100:.0f}% 한도, 상위 {dyn_top_count}개)"
        )

        # 비트코인 급락 및 시장 레짐 감시
        is_btc_crashing, btc_regime, btc_status_msg = check_btc_market_crash(upbit, threshold_pct=btc_crash_pct)
        trailing_tracker.set_macro_defensive_mode(is_btc_crashing)
        if is_btc_crashing:
            logger.warning(f"⚠️ [업비트 비트코인 급락 위험 감지] 레짐: {btc_regime} ({btc_status_msg}) ➜ 보유 알트코인 비상 방어 모드 가동")

        fng = get_fear_and_greed_index()
        logger.info(f"📊 [업비트 자산 요약] 총 자산: {tot_disp} | 원화: {krw_disp} | 당일 손익: {daily_pnl*100:+.2f}% | 공포탐욕: {fng['desc']}")

        bot_state_badge = "⏸️ 일시정지 중" if IS_BOT_PAUSED else ("🛑 킬스위치 발동" if is_kill_switch else ("❄️ 쿨다운 대기" if is_cooldown else "🟢 정상 가동 중"))

        # 대시보드 데이터 동기화
        bot_controller.get_dashboard_data()



        excluded_markets = get_upbit_excluded_markets()
        target_markets = cycle_orchestrator.select_target_markets(
            upbit, held_markets=held_markets, is_auto_mode=is_auto_mode, raw_markets=raw_markets,
            max_positions=dyn_max_positions, top_count=dyn_top_count,
            create_screener=lambda: MarketScreener(upbit, min_trade_value_krw=min_trade_val,
                                                    min_change_rate=min_change, max_change_rate=max_change),
            btc_regime=btc_regime,
        )

        logger.info(f"업비트 이번 사이클 최종 분석 대상 마켓 ({len(target_markets)}개): {target_markets}")



        # 업비트 실시간 웹소켓 구독 갱신 (HOLO 제외)
        ws_client.update_subscriptions(list(dict.fromkeys(target_markets + held_markets + ["KRW-BTC"])))

        # 5. 마켓별 순회 분석 및 매매 실행
        for market in target_markets:
            if market in excluded_markets or market.replace("KRW-", "") in excluded_markets:
                logger.warning(f"🛑 [보호 규칙 작동] 관리 제외 종목 ({market}) 분석 건너뜀")
                continue

            try:
                market_snapshot = cycle_orchestrator.load_market_snapshot(upbit, market, INTERVAL_MINUTES)
                currency, korean_name = market_snapshot.currency, market_snapshot.korean_name
                logger.info(f"--- [{korean_name} / {market} 업비트 AI 퀀트 분석 시작] ---")
                balances, krw_available = market_snapshot.balances, market_snapshot.krw_available
                coin_available, avg_buy_price = market_snapshot.coin_available, market_snapshot.avg_buy_price
                current_price = market_snapshot.current_price
                coin_value = coin_available * current_price
                candles_5m, candles_1h, orderbook = market_snapshot.candles_5m, market_snapshot.candles_1h, market_snapshot.orderbook

                # =========================================================================
                # 🎯 [최우선 1: 50% 분할 익절 & 가속 트레일링 스탑 즉시 청산]
                # =========================================================================
                if coin_value >= MIN_ORDER_KRW and avg_buy_price > 0 and not order_journal.has_active_exit_order(market):
                    action_type, peak_p, _trigger_p, peak_profit_pct, realized_profit_pct = (
                        trailing_tracker.check_position(market, current_price, avg_buy_price)
                    )

                    if action_type in ("PARTIAL_TP", "PARTIAL_TP_1", "PARTIAL_TP_2"):
                        is_stage2 = (action_type == "PARTIAL_TP_2")
                        sell_ratio = StrategyPolicy.PARTIAL_TP_2_RATIO if is_stage2 else StrategyPolicy.PARTIAL_TP_1_RATIO
                        sell_vol = coin_available * sell_ratio
                        sell_val = sell_vol * current_price
                        stage_name = "2차 50%" if is_stage2 else "1차 50%"
                        if sell_val >= MIN_ORDER_KRW and trailing_tracker.acquire_exit_lock(market):
                            try:
                                logger.info(
                                    f"🎉 [{korean_name} / {market} 업비트 {stage_name} 분할익절 발동] 현재가 {current_price:,.2f}원(+{realized_profit_pct:.2f}%). {stage_name} 시장가 익절!"
                                )
                                cancel_bot_open_orders(upbit, market)

                                order_res = order_executor.submit(
                                    upbit,
                                    market=market,
                                    side="ask",
                                    volume=sell_vol,
                                    ord_type="market",
                                    position_id=market,
                                    exit_reason=f"PARTIAL_TP_{2 if is_stage2 else 1}",
                                    avg_buy_price=avg_buy_price,
                                )
                                order_uuid = order_res.get("uuid") or order_res.get("order_id", "UNKNOWN")
                                client_order_id = order_res.get("client_order_id", "")

                                # ACK 뒤 50ms 단건 조회는 제거한다. 다음 REST 대사에서만 체결을 확정한다.

                                # [알림 최적화] 분할익절 접수 알림 제거 (실체결 완료 시 OrderFillProcessor에서 발송)
                                pass
                            finally:
                                trailing_tracker.release_exit_lock(market)

                    elif action_type == "TRAILING_STOP":
                        if trailing_tracker.acquire_exit_lock(market):
                            try:
                                logger.info(
                                    f"🎯 [{korean_name} / {market} 트레일링 스탑 익절 발동] 최고점 {peak_p:,.2f}원(+{peak_profit_pct:.2f}%) ➜ 현재 {current_price:,.2f}원(+{realized_profit_pct:.2f}%). 잔여 전량 시장가 익절!"
                                )
                                cancel_bot_open_orders(upbit, market)

                                order_res = order_executor.submit(
                                    upbit,
                                    market=market,
                                    side="ask",
                                    volume=coin_available,
                                    ord_type="market",
                                    position_id=market,
                                    exit_reason="TRAILING_STOP",
                                    avg_buy_price=avg_buy_price,
                                )
                                order_uuid = order_res.get("uuid") or order_res.get("order_id", "UNKNOWN")
                                client_order_id = order_res.get("client_order_id", "")
                                # 쿨다운과 손익은 다음 REST 대사가 확인한 체결 증가분에서만 기록한다.

                                # [알림 최적화] 트레일링 스탑 접수 알림 제거 (실체결 완료 시 OrderFillProcessor에서 발송)
                                pass
                            finally:
                                trailing_tracker.release_exit_lock(market)
                            continue

                # =========================================================================
                # ⏳ [최우선 2: 15분 모멘텀 소멸 조기 탈출 & 60분 횡보 방지 타임스탑]
                # =========================================================================
                if coin_value >= MIN_ORDER_KRW and avg_buy_price > 0 and not order_journal.has_active_exit_order(market):
                    entry_ts = trailing_tracker.get_entry_time(market)
                    if entry_ts <= 0:
                        entry_ts = time.time()
                        trailing_tracker.set_entry_time(market, entry_ts)
                        logger.info(f"⏱️ [{market}] 업비트 진입 시점 미등록 포지션 ➜ 현재 시간 보정 등록 ({now_str})")

                    hold_duration_sec = time.time() - entry_ts
                    pnl_pct_current = ((current_price - avg_buy_price) / avg_buy_price) * 100.0
                    effective_time_stop = (
                        StrategyPolicy.TIME_STOP_SECONDS_RISK_OFF
                        if btc_regime == "RISK_OFF"
                        else StrategyPolicy.TIME_STOP_SECONDS_NORMAL
                    )

                    # 1. 5분봉 지지선 및 추세 붕괴 여부 정밀 판정
                    is_holding_support = False
                    is_trend_broken = False
                    is_above_vwap = True
                    if candles_5m:
                        vwap_data = calculate_vwap(candles_5m)
                        is_above_vwap = vwap_data.get("is_above", True)
                        if len(candles_5m) >= 20:
                            prices_5m = [float(c.get("trade_price", 0.0)) for c in candles_5m]
                            ma20_5m = sum(prices_5m[:20]) / 20.0
                            ma5_5m = sum(prices_5m[:5]) / 5.0
                            is_holding_support = (current_price >= ma20_5m) or is_above_vwap
                            is_trend_broken = (ma5_5m < ma20_5m * 0.995) and (not is_above_vwap)

                    # 2. [본전 보장 타임스탑 - Case A]: 실질 본전 이상(+0.05% 이상) 구간
                    #    - 5분봉 지지선(MA20/VWAP) 유지 시 최대 180분까지 추세 상승 대기
                    #    - 지지선 이탈 시에만 타임스탑 경과 후 안전하게 분할/본전 청산
                    be_threshold_pct = StrategyPolicy.TIME_STOP_BREAKEVEN_MIN_PNL_PCT * 100.0  # +0.05%
                    is_breakeven_or_profit = pnl_pct_current >= be_threshold_pct
                    is_time_stop_profit_trigger = (
                        hold_duration_sec >= effective_time_stop
                        and is_breakeven_or_profit
                        and (not is_holding_support or hold_duration_sec >= StrategyPolicy.TIME_STOP_MAX_HOLD_SECONDS)
                    )

                    # 3. [본전 보장 타임스탑 - Case B]: 마이너스 손실 구간에서는 성급한 투매를 차단하고 반등 대기
                    #    - 지지선 유지 시 최대 180분까지 손절선(-2.2%)을 지키며 반등 기회 보장
                    #    - 추세가 명백히 붕괴되었거나 최대 유예 시간(180분) 초과 시에만 방어적 청산
                    is_time_stop_loss_trigger = (
                        (hold_duration_sec >= StrategyPolicy.TIME_STOP_MAX_HOLD_SECONDS or (hold_duration_sec >= effective_time_stop and is_trend_broken))
                        and (pnl_pct_current < be_threshold_pct)
                    )

                    # 4. [모멘텀 조기 본전 탈출]: 30분 경과 후 실질 본전권(0.0% ~ +0.3%)에서 모멘텀 소멸(VWAP 하회) 시 수수료 세이브 탈출
                    is_early_momentum_exit = (
                        hold_duration_sec >= StrategyPolicy.MOMENTUM_EARLY_EXIT_SECONDS
                        and (0.0 <= pnl_pct_current <= 0.3)
                        and (not is_above_vwap)
                        and is_trend_broken
                    )

                    is_time_stop_trigger = (
                        (is_time_stop_profit_trigger or is_time_stop_loss_trigger or is_early_momentum_exit)
                        and not order_journal.has_active_exit_order(market)
                    )

                    if is_time_stop_trigger:
                        exit_reason_label = "MOMENTUM_EARLY_EXIT" if is_early_momentum_exit else "TIME_STOP"
                        if trailing_tracker.acquire_exit_lock(market):
                            try:
                                exit_desc = (
                                    "15분 모멘텀 소멸 조기 본전 탈출"
                                    if is_early_momentum_exit
                                    else f"{effective_time_stop/60:.0f}분 횡보 타임스탑"
                                )
                                logger.info(
                                    f"⏳ [{korean_name} / {market}] 업비트 {exit_desc} 발동! (레짐: {btc_regime}, 손익률: {pnl_pct_current:+.2f}%, 보유시간: {hold_duration_sec/60:.0f}분) ➜ 시장가 전량 청산"
                                )
                                cancel_bot_open_orders(upbit, market)

                                order_res = order_executor.submit(
                                    upbit,
                                    market=market,
                                    side="ask",
                                    volume=coin_available,
                                    ord_type="market",
                                    position_id=market,
                                    exit_reason=exit_reason_label,
                                    avg_buy_price=avg_buy_price,
                                )
                                order_uuid = order_res.get("uuid") or order_res.get("order_id", "UNKNOWN")
                                client_order_id = order_res.get("client_order_id", "")
                                # 쿨다운과 손익은 다음 REST 대사가 확인한 체결 증가분에서만 기록한다.

                                # [알림 최적화] 타임스탑 접수 알림 제거 (실체결 완료 시 OrderFillProcessor에서 발송)
                                pass
                                continue
                            finally:
                                trailing_tracker.release_exit_lock(market)

# =========================================================================
                # 🛡️ [신규 매수 게이트 검증]
                # =========================================================================
                if IS_BOT_PAUSED or is_kill_switch or not order_journal.is_entry_ready():
                    # 상태 대사가 끝나기 전에는 청산·보호 주문만 허용한다.
                    if not order_journal.is_entry_ready():
                        logger.warning("[%s] REST 주문 대사 미완료로 신규 매수를 차단합니다.", market)
                    logger.info(f"[{market}] 봇 일시정지 또는 킬스위치 상태로 신규 매수 생략")
                    continue

                reentry_allowed, reentry_reason = cooldown_manager.check_reentry_allowed(market, current_price)
                if not reentry_allowed:
                    logger.info(f"[{market}] {reentry_reason}으로 신규 매수 생략")
                    continue

                if not candles_5m or len(candles_5m) < 20:
                    logger.warning(f"[{market}] 캔들 데이터 부족으로 진입 생략")
                    continue

                # =========================================================================
                # 🧠 [하이브리드 2단계 게이팅: 1차 퀀트 사전 필터 ➜ 2차 AI 최종 승인]
                # ※ 확정 완료봉(candles_5m[1:]) 기준으로 지표를 연산하여 백테스트와 100% 동일한 진입 정책 보장 (과제 C)
                # =========================================================================
                completed_candles_5m = select_completed_candles(candles_5m, minimum_count=25)
                completed_candles_1h = select_completed_candles(candles_1h, minimum_count=20)
                if not completed_candles_5m or not completed_candles_1h:
                    logger.warning("[%s] 5분/1시간 확정봉 데이터가 부족하거나 불일치하여 신규 매수 차단", market)
                    continue
                local_entry = entry_signal(
                    candles=completed_candles_5m,
                    candles_1h=completed_candles_1h,
                    btc_regime=btc_regime,
                    orderbook=orderbook,
                    market=market,
                    exchange="upbit",
                )
                is_holding = (coin_value >= MIN_ORDER_KRW and avg_buy_price > 0)
                ws_health = ws_client.get_health_status(market=market) if hasattr(ws_client, "get_health_status") else {"is_healthy": True, "status": "OK"}
                ws_healthy = ws_health.get("is_healthy", True)

                should_call_ai = (
                    analyzer is not None
                    and not is_holding
                    and reentry_allowed
                    and local_entry.get("allow_buy", False)
                    and not is_btc_crashing
                    and ws_healthy
                )

                if should_call_ai and analyzer is not None:
                    feedback_context = trade_memory.get_feedback_context()
                    whale_flow_context = ws_client.get_whale_flow_summary(market) if hasattr(ws_client, "get_whale_flow_summary") else ""
                    btc_candles_5m = upbit.get_candles(unit=INTERVAL_MINUTES, count=30, market="KRW-BTC")
                    rs_info = calculate_relative_strength(candles_5m, btc_candles_5m)
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
                        rs_context=rs_info.get("desc", ""),
                    )
                else:
                    # 1차 퀀트 관망 또는 기보유 포지션 기준선 산출 (Zero API Quota)
                    quant_reason = (
                        f"기보유 포지션 퀀트 감시 ({local_entry.get('reason', '')})"
                        if is_holding
                        else (
                            f"{reentry_reason}"
                            if not reentry_allowed
                            else (
                                f"BTC 레짐 경보 ({btc_status_msg})"
                                if is_btc_crashing
                                else (
                                    f"업비트 웹소켓 데이터 불안정 ({ws_health.get('status', 'UNHEALTHY')}) 진입 차단"
                                    if not ws_healthy
                                    else f"1차 퀀트 관망 대기: {local_entry.get('reason', '조건 미충족')}"
                                )
                            )
                        )
                    )
                    strategy = {
                        "status": "ACTIVE",
                        "action": "HOLD",
                        "entry_price": local_entry.get("entry_price", current_price),
                        "target_price": local_entry.get("target_price", current_price * 1.03),
                        "stop_loss": local_entry.get("stop_loss", current_price * 0.98),
                        "alloc_pct": 0.0,
                        "reason": quant_reason,
                    }

                status = strategy.get("status", "ACTIVE")
                action = strategy.get("action", "HOLD")
                entry_price = strategy.get("entry_price", current_price)
                target_price = strategy.get("target_price", local_entry.get("target_price", current_price * 1.03))
                stop_loss = strategy.get("stop_loss", local_entry.get("stop_loss", current_price * 0.98))
                alloc_pct = strategy.get("alloc_pct", dyn_max_pos_pct)
                reason = strategy.get("reason", "자동 분석")

                if not reentry_allowed and action == "BUY":
                    action = "HOLD"
                    reason = f"{reentry_reason} | {reason}"

                if action == "BUY" and not local_entry.get("allow_buy", False):
                    action = "HOLD"
                    reason = f"정량 공통 진입 게이트 차단: {local_entry.get('reason', '')} | {reason}"
                elif action == "BUY" and local_entry.get("allow_buy", False):
                    target_price = local_entry.get("target_price", target_price)
                    stop_loss = local_entry.get("stop_loss", stop_loss)

                if is_holding:
                    entry_price = avg_buy_price
                    stop_loss = avg_buy_price * (1.0 - StrategyPolicy.STOP_LOSS_PCT)
                    if trailing_tracker.is_breakeven_active(market):
                        stop_loss = max(stop_loss, avg_buy_price * (1.0 + StrategyPolicy.BREAKEVEN_STOP_PCT))
                    target_price = avg_buy_price * (1.0 + getattr(StrategyPolicy, "PROFIT_TARGET_PCT", StrategyPolicy.PARTIAL_TP_1_PCT))

                # 알파 스코어 연동 가변 사이징 (A+ 85점 이상 비중 확대)
                alpha_val = local_entry.get("alpha_score", 70)
                if alpha_val >= 85 and btc_regime != "RISK_OFF" and action == "BUY":
                    alloc_pct = min(dyn_max_pos_pct * 1.3, 0.65)
                    reason = f"[🔥알파 {alpha_val}점 A+ 특급 셋업 비중 확대(65%)] {reason}"
                elif alpha_val < 75 and btc_regime != "RISK_OFF" and action == "BUY":
                    alloc_pct = dyn_max_pos_pct * 0.7

                if btc_regime == "RISK_OFF" and action == "BUY":
                    alloc_ratio = StrategyPolicy.RISK_OFF_ALLOC_RATIO
                    if alpha_val >= 80:
                        alloc_ratio = min(0.8, alloc_ratio * 1.3)
                    alloc_pct = alloc_pct * alloc_ratio
                    reason = f"[BTC 약세 레짐 비중 {int(alloc_ratio*100)}% 적용 & 알파 {alpha_val}점 엄선] {reason}"

                if fng.get("is_extreme_fear", False) and action == "BUY":
                    alloc_pct = min(alloc_pct, 0.4)

                alpha_score_val = int(strategy.get("alpha_score") or local_entry.get("alpha_score", 0) or 0)
                factor_breakdown = (
                    local_entry.get("factor_breakdown")
                    or local_entry.get("checklist_details", {}).get("factor_breakdown")
                    or local_entry.get("checklist", {}).get("factor_breakdown")
                    or local_entry.get("checklist", {})
                    or {}
                )
                allow_buy_val = bool(local_entry.get("allow_buy", False))

                target_pct_val = (((target_price - current_price) / current_price * 100) if current_price > 0 and target_price > 0 else 0.0)
                stop_pct_val = (((stop_loss - current_price) / current_price * 100) if current_price > 0 and stop_loss > 0 else 0.0)
                rr_denom_val = max(1e-6, current_price - stop_loss)
                rr_ratio_val = (((target_price - current_price) / rr_denom_val) if current_price > stop_loss > 0 and target_price > current_price else 0.0)

                LATEST_STRATEGIES[market] = {
                    "market": market,
                    "korean_name": korean_name,
                    "status": status,
                    "action": action,
                    "current_price": current_price,
                    "entry_price": entry_price,
                    "target_price": target_price,
                    "stop_loss": stop_loss,
                    "alloc_pct": alloc_pct,
                    "reason": reason,
                    "alpha_score": alpha_score_val,
                    "allow_buy": allow_buy_val,
                    "factor_breakdown": factor_breakdown,
                    "target_pct": round(target_pct_val, 2),
                    "stop_pct": round(stop_pct_val, 2),
                    "risk_reward_ratio": round(rr_ratio_val, 2),
                    "updated_at": now_str,
                    "ACTION": action,
                    "TARGET_PRICE": target_price,
                    "STOP_LOSS": stop_loss,
                    "REASON": reason,
                }



                if action == "BUY":
                    order_price = upbit.adjust_price_to_tick(entry_price or current_price, side="bid")
                    alloc_pct = alloc_pct or dyn_max_pos_pct
                    max_slot_budget = current_total_equity * alloc_pct
                    risk_scale = risk_manager.get_risk_scale_factor()
                    calculated_size = calculate_risk_position_size(
                        total_equity=current_total_equity,
                        entry_price=order_price,
                        stop_loss=stop_loss,
                        max_position_pct=alloc_pct,
                        min_order_krw=MIN_ORDER_KRW,
                        risk_scale_factor=risk_scale,
                    )
                    trade_budget = min(krw_available, max_slot_budget, calculated_size)

                    SAFE_ORDER_KRW = 5500.0
                    if trade_budget < SAFE_ORDER_KRW and krw_available >= SAFE_ORDER_KRW:
                        trade_budget = min(krw_available, max(max_slot_budget, SAFE_ORDER_KRW))

                    if trade_budget < SAFE_ORDER_KRW or (trade_budget * 0.995) < MIN_ORDER_KRW:
                        logger.warning(
                            f"[{korean_name} / {market}] 매수 예산 부족: 요청 {trade_budget:,.0f}원 < 최소 {SAFE_ORDER_KRW:,.0f}원"
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
                        logger.warning(f"[{market}] 리스크 가드로 매수 차단: {rejection_reason}")
                        continue

                    order_volume = (trade_budget * 0.995) / order_price
                    formatted_volume = upbit.round_volume(market, order_volume)

                    if formatted_volume <= 0:
                        logger.warning(f"[{market}] 주문 수량 오류: {formatted_volume}")
                        continue

                    logger.info(
                        f"🛒 [업비트 {korean_name} / {market} 신규 매수 실행] 주문가={order_price:,.2f}원, 수량={formatted_volume:.6f}, 투입금액={int(trade_budget):,d}원"
                    )

                    cancel_bot_open_orders(upbit, market)

                    # 승인 시점 값만 주문 원장에 저장해, 미래 체결 시점 정보와 혼동하지 않는다.
                    entry_snapshot = dict(local_entry.get("strategy_snapshot", {}))
                    entry_snapshot.update({
                        "exchange": "upbit",
                        "market": market,
                        "entry_decision_at": now_str,
                        "entry_reason": reason,
                        "target_price": target_price,
                        "stop_loss": stop_loss,
                    })
                    position_id = f"upbit:{market}:{int(time.time() * 1000)}"
                    order_res = order_executor.submit(
                        upbit,
                        market=market,
                        side="bid",
                        price=order_price,
                        volume=formatted_volume,
                        ord_type="limit",
                        position_id=position_id,
                        expected_price=order_price,
                        entry_strategy_snapshot=entry_snapshot,
                        exchange_name="upbit",
                    )
                    order_uuid = order_res.get("uuid") or order_res.get("order_id", "UNKNOWN")
                    client_order_id = order_res.get("client_order_id", "")

                    # 매수 ACK는 체결이 아니다. 진입 시각은 REST 대사에서만 생성한다.

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

                    # [알림 최적화] 매수 주문 접수 알림 제거 (실체결 완료 시 OrderFillProcessor에서 발송)
                    pass

            except Exception as e:
                logger.error(f"[{market}] 매매 사이클 오류 발생: {e}", exc_info=True)

        # 6. 이번 사이클 대상(기보유 + 신규 스크리닝 후보) 외의 과거 종목 정리 후 디스크 캐시 저장
        current_valid_markets = set(target_markets).union(held_markets)
        for old_m in list(LATEST_STRATEGIES.keys()):
            if old_m not in current_valid_markets:
                LATEST_STRATEGIES.pop(old_m, None)

        strategy_cache_mgr.save_cache(LATEST_STRATEGIES)

    except Exception as e:
        logger.error(f"업비트 전체 트레이딩 사이클 예외 발생: {e}", exc_info=True)


def update_heartbeat() -> None:
    """워치독 헬스체크 및 무응답(Hang) 방지를 위한 업비트 하트비트 파일 원자적 갱신"""
    hb_file = os.path.join(DATA_DIR, ".heartbeat")
    try:
        write_json_atomically(hb_file, {
            "timestamp": time.time(),
            "pid": os.getpid(),
            "datetime": get_kst_now_str(),
            "status": "RUNNING",
            "bot": "upbit",
        })
    except Exception as e:
        logger.debug(f"업비트 하트비트 기록 예외: {e}")


def main():
    logger.info("============================================================")
    logger.info("  Upbit AI Pro Quant Trading Bot 가동 시작")
    logger.info(f"  (데이터 경로: {DATA_DIR} | 웹 대시보드 포트: {WEB_PORT})")
    logger.info("============================================================")

    # 0. SQLite 자동 마이그레이션 및 첫 하트비트 기록
    try:
        migrate_legacy_json_to_sqlite(os.path.dirname(DATA_DIR))
    except Exception as e:
        logger.warning("SQLite 초기 마이그레이션 건너뜀: %s", e)
    update_heartbeat()

    # 1. 텔레그램 명령어 리스너 가동
    telegram.start_command_listener(
        status_callback=bot_controller.get_status_message,
        balance_callback=bot_controller.get_balance_message,
        panic_callback=bot_controller.execute_panic_sell,
        pause_callback=bot_controller.pause_bot,
        resume_callback=bot_controller.resume_bot,
        diag_callback=bot_controller.get_diagnostics_message,
        trades_callback=bot_controller.get_trades_summary_message,
    )

    # 2. 로컬 전용 초경량 API 서버 가동 (127.0.0.1:17980 - 통합 대시보드 연동용)
    upbit_internal_port = int(os.getenv("UPBIT_INTERNAL_PORT", "17980"))
    web_server = DashboardWebServer(
        host="0.0.0.0",
        port=upbit_internal_port,
        data_provider=bot_controller.get_dashboard_data,
        action_handler=bot_controller.handle_web_action,
        title="Upbit Trading Core API",
        is_api_only=True,
    )
    web_server.start()

    # 3. 실시간 웹소켓 스트리밍 가동
    ws_client.start()
    if private_ws:
        private_ws.start()

    # 4. 전략 캐시 복원 및 스마트 스케줄러 등록 (5분 캔들 주기 내 재시작 시 중복 분석/AI 호출 방지)
    cycle_ttl = INTERVAL_MINUTES * 60 - 30
    cached_strats, elapsed_sec, is_cache_valid = strategy_cache_mgr.get_valid_strategies(ttl=cycle_ttl)
    if cached_strats:
        LATEST_STRATEGIES.update(cached_strats)
        try:
            exchange = create_exchange_client()
            held_markets = get_held_markets(exchange.get_balances(), exchange)
            bot_controller.restore_missing_position_strategies(held_markets)
        except Exception as e:
            logger.debug(f"업비트 포지션 전략 복원 예외: {e}")
        logger.info(f"📂 [전략 캐시 복원] 디스크에서 {len(cached_strats)}개 종목의 직전 분석 데이터를 대시보드에 즉시 복원했습니다.")

    should_run_immediate = not is_cache_valid
    if is_cache_valid:
        remaining_sec = max(15, int(INTERVAL_MINUTES * 60 - elapsed_sec))
        first_run_time = datetime.now() + timedelta(seconds=remaining_sec)
        logger.info(
            f"⚡ [스마트 캐시 유지] 직전 분석 후 {elapsed_sec:.0f}초 경과 (5분 캔들 유효). "
            f"중복 AI/REST 호출을 생략하고 {remaining_sec}초 후 다음 정기 분석을 시작합니다."
        )
    else:
        first_run_time = datetime.now() + timedelta(minutes=INTERVAL_MINUTES)

    scheduler = BackgroundScheduler(timezone="Asia/Seoul")
    scheduler.add_job(
        run_cycle,
        "interval",
        minutes=INTERVAL_MINUTES,
        next_run_time=first_run_time,
        id="run_upbit_trading_cycle",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        send_daily_morning_report,
        "cron",
        hour=9,
        minute=0,
        id="upbit_morning_daily_report",
        max_instances=1,
    )
    scheduler.start()
    logger.info(f"⏰ APScheduler 가동 완료 ({INTERVAL_MINUTES}분 주기 매매 및 매일 09:00 모닝 리포트)")

    # 5. 초기 사이클 실행 (캐시 만료 또는 첫 실행 시에만 즉시 분석 실행)
    if should_run_immediate:
        try:
            run_cycle()
        except Exception as e:
            logger.error(f"초기 사이클 실행 중 오류: {e}")

    # 6. 프로세스 종료 시그널 핸들러 등록
    is_exiting = False

    def _handle_exit(sig=None, frame=None):
        nonlocal is_exiting
        if is_exiting:
            return
        is_exiting = True
        logger.info("🛑 프로세스 종료 시그널 감지. 업비트 자원을 안전하게 해제합니다...")
        try:
            telegram.stop()
        except Exception as e:
            logger.debug(f"업비트 텔레그램 종료 예외: {e}")
        try:
            ws_client.stop()
        except Exception as e:
            logger.debug(f"업비트 WebSocket 종료 예외: {e}")
        if private_ws:
            try:
                private_ws.stop()
            except Exception as e:
                logger.debug(f"업비트 Private WS 종료 예외: {e}")
        try:
            web_server.stop()
        except Exception as e:
            logger.debug(f"업비트 웹 대시보드 종료 예외: {e}")
        try:
            scheduler.shutdown(wait=False)
        except Exception as e:
            logger.debug(f"업비트 스케줄러 종료 예외: {e}")
        logger.info("✅ 업비트 봇 모든 자원 정상 해제 완료")
        sys.exit(0)

    signal.signal(signal.SIGINT, _handle_exit)
    signal.signal(signal.SIGTERM, _handle_exit)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.SIG_IGN)

    # 7. 메인 스레드 유지 및 주기적 하트비트 루프
    last_hb_ts = 0.0
    while True:
        try:
            now_ts = time.time()
            # WebSocket 수신 스레드는 큐만 적재하므로, 주문/파일 작업은 메인 스레드에서 직렬 처리한다.
            ws_client.drain_callbacks()
            if private_ws:
                private_ws.drain_order_events()
            if now_ts - last_hb_ts >= 15.0:
                update_heartbeat()
                last_hb_ts = now_ts
            time.sleep(1)
        except (KeyboardInterrupt, SystemExit):
            _handle_exit(None, None)


if __name__ == "__main__":
    main()
