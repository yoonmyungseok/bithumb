"""
Upbit AI Pro Quant Trading Bot
- 업비트(Upbit) API 기반 5분 주기 AI 퀀트 자동매매 오케스트레이터
- REST API + 0.1초 실시간 WebSocket 스트리밍 + Google Gemini AI 분석
- data/upbit/* 및 logs/trading_upbit.log를 통한 완벽한 데이터/로그 물리적 격리
- KRW-HOLO 수동 매매 보호 종목 7중 차단망 적용
- 웹 대시보드 (기본 포트: 7980) 및 텔레그램 양방향 원격 제어
"""

import logging
import os
import sys
import time
from logging.handlers import TimedRotatingFileHandler
from typing import Any

from dotenv import load_dotenv

from bot_controller import BotController
from chart_renderer import ChartRenderer
from db_manager import get_db_manager, get_exchange_db_path
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
    calculate_vwap,
    classify_btc_regime,
)
from telegram_alert import TelegramAlert
from trade_memory import TradeMemoryManager
from trading_bot_bootstrap import (
    ExchangeBootstrapProfile,
    TradingBootstrapContext,
    TradingBotBootstrap,
)
from trading_orchestrator import TradingOrchestrator
from trading_runtime import (
    ExchangeBuyProfile,
    ExchangeCycleProfile,
    ExchangeEntryProfile,
    ExchangeExitProfile,
    TradingCycleEngine,
    TradingRuntimeConfig,
    TradingRuntimeContext,
)
from upbit_api import UpbitAPI, get_upbit_excluded_markets
from upbit_private_websocket import UpbitPrivateWebSocketClient
from upbit_websocket import UpbitWebSocketClient

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
# 업비트도 빗썸과 같은 확정봉 모멘텀 돌파 정책을 사용하되, 기존 환경 변수는 호환한다.
MOMENTUM_BREAKOUT_ENABLED = os.getenv("UPBIT_MOMENTUM_BREAKOUT_ENABLED", os.getenv("MOMENTUM_BREAKOUT_ENABLED", os.getenv("EARLY_BREAKOUT_ENABLED", "true"))).strip().lower() in {"1", "true", "yes", "on"}
MOMENTUM_BREAKOUT_MIN_CHANGE_RATE = float(os.getenv("UPBIT_MOMENTUM_BREAKOUT_MIN_CHANGE_RATE", os.getenv("MOMENTUM_BREAKOUT_MIN_CHANGE_RATE", os.getenv("EARLY_BREAKOUT_MIN_CHANGE_RATE", "0.003"))))
MOMENTUM_BREAKOUT_MAX_CANDIDATES = int(os.getenv("UPBIT_MOMENTUM_BREAKOUT_MAX_CANDIDATES", os.getenv("MOMENTUM_BREAKOUT_MAX_CANDIDATES", os.getenv("EARLY_BREAKOUT_MAX_CANDIDATES", "2"))))

INTERVAL_MINUTES = int(os.getenv("INTERVAL_MINUTES", "5"))
# 업비트 전용 Gemini API 키 (미설정 시 공용 GEMINI_API_KEY 사용)
GEMINI_API_KEY = (os.getenv("UPBIT_GEMINI_API_KEY") or os.getenv("GEMINI_API_KEY", "")).strip()
# 빗썸과의 동시 퀀트 사이클 호출 분산을 위한 오프셋 (기본값: 150초 = 2분 30초)
CYCLE_OFFSET_SECONDS = int(os.getenv("UPBIT_CYCLE_OFFSET_SECONDS", os.getenv("CYCLE_OFFSET_SECONDS", "150")))

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
# 관찰 기간에는 차단 후보만 기록하고, 검증 후 환경 변수로 신규 매수 차단을 활성화한다.
ORDERBOOK_SLIPPAGE_ENFORCEMENT = os.getenv("ORDERBOOK_SLIPPAGE_ENFORCEMENT", "false").strip().lower() in {"1", "true", "yes", "on"}
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
# 최신 캐시와 분리된 판단 이력은 업비트 전용 SQLite에만 기록한다.
decision_db = get_db_manager(get_exchange_db_path(DATA_DIR))
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


def _create_upbit_screener(exchange: ExchangeAdapter) -> MarketScreener:
    """사이클마다 최신 env를 반영한 업비트 스크리너를 생성한다."""
    return MarketScreener(
        exchange,
        min_trade_value_krw=float(os.getenv("MIN_TRADE_VALUE", "1000000000")),
        min_change_rate=float(os.getenv("MIN_CHANGE_RATE", "0.005")),
        max_change_rate=float(os.getenv("MAX_CHANGE_RATE", "0.25")),
        enable_early_breakout=MOMENTUM_BREAKOUT_ENABLED,
        early_breakout_min_change_rate=MOMENTUM_BREAKOUT_MIN_CHANGE_RATE,
        early_breakout_max_candidates=MOMENTUM_BREAKOUT_MAX_CANDIDATES,
    )


def _get_cycle_excluded_markets() -> frozenset[str]:
    return frozenset(get_upbit_excluded_markets())


UPBIT_CYCLE_PROFILE = ExchangeCycleProfile(
    exchange_key="upbit",
    reconcile_label="업비트 ",
    decision_exchange="upbit",
    log_prefix="업비트 ",
    extra_excluded_markets=_get_cycle_excluded_markets,
    create_screener=_create_upbit_screener,
    cycle_start_label="업비트 5분 AI 퀀트 트레이딩",
    tier_label="업비트 스마트 자산 티어",
    tier_top_wording="상위",
    summary_label="업비트 자산 요약",
    btc_crash_label="업비트 비트코인 급락 위험 감지",
    markets_log_prefix="업비트 ",
    stale_orders_log_prefix="업비트 ",
    market_analysis_log_label="업비트 AI 퀀트 분석 시작",
    skip_excluded_markets_in_loop=True,
    cycle_error_log_prefix="업비트 ",
)

UPBIT_EXIT_PROFILE = ExchangeExitProfile(
    partial_tp_log_prefix="업비트 ",
    partial_tp_stage1_name="1차 50%",
    partial_tp_stage2_name="2차 50%",
    partial_tp_stage2_ratio=StrategyPolicy.PARTIAL_TP_2_RATIO,
    render_trailing_chart=False,
    time_stop_log_prefix="업비트 ",
    time_stop_close_suffix="시장가 전량 청산",
    time_stop_recheck_active_exit=True,
    entry_time_missing_log_template=(
        "⏱️ [{market}] 업비트 진입 시점 미등록 포지션 ➜ 현재 시간 보정 등록 ({now_str})"
    ),
)


UPBIT_ENTRY_PROFILE = ExchangeEntryProfile(
    signal_exchange="upbit",
    recovery_db_exchange="upbit",
    ws_unhealthy_label="업비트 웹소켓",
    hold_reason_fallback="조건 미충족",
    use_dynamic_default_alloc=True,
    enforce_pre_entry_safety_gates=True,
    block_on_reentry_denied=True,
    require_minimum_candles=True,
    include_candidate_metadata_in_latest=False,
    whale_flow_requires_capability=True,
    use_hold_price_fallbacks=True,
)


UPBIT_BUY_PROFILE = ExchangeBuyProfile(
    exchange_name="upbit",
    use_simple_buy_log=True,
    buy_log_prefix="업비트 ",
)


def cancel_bot_open_orders(upbit: UpbitAPI, market: str | None = None) -> int:
    """봇이 발행한 미체결 주문만 선별 취소하여 외부/수동 주문 보호"""
    return realtime_engine.cancel_bot_open_orders(market=market)


cycle_engine = TradingCycleEngine(
    TradingRuntimeConfig(
        profile=UPBIT_CYCLE_PROFILE,
        exit_profile=UPBIT_EXIT_PROFILE,
        entry_profile=UPBIT_ENTRY_PROFILE,
        buy_profile=UPBIT_BUY_PROFILE,
        env_file=UPBIT_ENV_FILE,
        interval_minutes=INTERVAL_MINUTES,
        gemini_api_key=GEMINI_API_KEY,
        is_bot_paused=get_is_bot_paused,
        min_order_krw=MIN_ORDER_KRW,
        orderbook_slippage_enforcement=ORDERBOOK_SLIPPAGE_ENFORCEMENT,
    ),
    TradingRuntimeContext(
        logger=logger,
        orchestrator=cycle_orchestrator,
        create_exchange_client=create_exchange_client,
        order_journal=order_journal,
        fill_processor=fill_processor,
        trailing_tracker=trailing_tracker,
        realtime_engine=realtime_engine,
        risk_manager=risk_manager,
        risk_guard=risk_guard,
        bot_controller=bot_controller,
        ws_client=ws_client,
        decision_db=decision_db,
        calculate_total_equity=calculate_total_equity,
        get_held_markets=get_held_markets,
        get_portfolio_tiers=get_dynamic_portfolio_tiers,
        order_executor=order_executor,
        chart_renderer=chart_renderer,
        cancel_bot_open_orders=cancel_bot_open_orders,
        cooldown_manager=cooldown_manager,
        trade_memory=trade_memory,
        latest_strategies=LATEST_STRATEGIES,
        strategy_cache_manager=strategy_cache_mgr,
    ),
)


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

        ai_briefing = ""
        upbit_gemini_key = os.getenv("UPBIT_GEMINI_API_KEY") or os.getenv("GEMINI_API_KEY")
        analyzer = GeminiAnalyzer(api_key=upbit_gemini_key) if upbit_gemini_key else None
        if analyzer is not None and hasattr(analyzer, "generate_market_briefing"):
            try:
                candles_1h = upbit.get_candles(unit=60, count=30, market="KRW-BTC")
                macro_diag = analyzer.diagnose_macro_regime(candles_1h, fng_index=fng)
                ai_comment = analyzer.generate_market_briefing(
                    exchange_name="업비트",
                    total_equity=total_equity,
                    daily_pnl_krw=daily_pnl_krw,
                    daily_pnl_pct=daily_pnl_pct,
                    held_positions_desc=held_desc,
                    macro_diag=macro_diag,
                    fng_desc=fng.get("desc", ""),
                )
                if ai_comment:
                    ai_briefing = f"\n\n🤖 <b>[Gemini AI 종합 시황 브리핑]</b>\n{ai_comment}"
            except Exception as e:
                logger.debug(f"업비트 AI 브리핑 생성 예외: {e}")

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
            f"{ai_briefing}"
        )
    except Exception as e:
        logger.error(f"업비트 모닝 리포트 발송 실패: {e}")


def run_cycle():
    """
    [업비트 5분 오케스트레이션 사이클]
    - 자산 티어 산출 -> 스크리닝 (HOLO 제외) -> 정량 지표 + AI 분석 -> 리스크 가드 -> 안전 주문 집행
    """
    cycle_engine.run_cycle()


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


UPBIT_BOOTSTRAP_PROFILE = ExchangeBootstrapProfile(
    exchange_key="upbit",
    migration_base_dir=os.path.dirname(DATA_DIR),
    data_dir=DATA_DIR,
    heartbeat_bot_name="upbit",
    internal_port_env_key="UPBIT_INTERNAL_PORT",
    internal_port_default=17980,
    internal_api_title="Upbit Trading Core API",
    scheduler_cycle_job_id="run_upbit_trading_cycle",
    scheduler_morning_job_id="upbit_morning_daily_report",
    startup_banner_lines=(
        "  Upbit AI Pro Quant Trading Bot 가동 시작",
        f"  (데이터 경로: {DATA_DIR} | 웹 대시보드 포트: {WEB_PORT})",
    ),
    shutdown_start_message="🛑 프로세스 종료 시그널 감지. 업비트 자원을 안전하게 해제합니다...",
    shutdown_complete_message="✅ 업비트 봇 모든 자원 정상 해제 완료",
    log_prefix="업비트 ",
)


def main():
    bootstrap = TradingBotBootstrap(
        UPBIT_BOOTSTRAP_PROFILE,
        TradingBootstrapContext(
            logger=logger,
            telegram=telegram,
            bot_controller=bot_controller,
            ws_client=ws_client,
            private_ws=private_ws,
            strategy_cache_manager=strategy_cache_mgr,
            latest_strategies=LATEST_STRATEGIES,
            interval_minutes=INTERVAL_MINUTES,
            create_exchange_client=create_exchange_client,
            get_held_markets=get_held_markets,
            run_cycle=run_cycle,
            send_daily_morning_report=send_daily_morning_report,
            update_heartbeat=update_heartbeat,
            cycle_offset_seconds=CYCLE_OFFSET_SECONDS,
        ),
    )
    bootstrap.run()


if __name__ == "__main__":
    main()
