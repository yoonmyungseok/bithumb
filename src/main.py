import logging
import os
import sys
import time
from logging.handlers import TimedRotatingFileHandler
from typing import Any

from dotenv import load_dotenv

from bithumb_api import BithumbAPI
from db_manager import get_db_manager, get_exchange_db_path
from exchange_adapter import BithumbAdapter, ExchangeAdapter
from bot_controller import BotController
from chart_renderer import ChartRenderer
from gemini_analyzer import GeminiAnalyzer
from gemini_telemetry import GeminiTelemetry
from market_screener import MarketScreener
from order_safety import (
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
from private_websocket_manager import BithumbPrivateWebSocketClient
from realtime_engine import RealtimeRiskEngine
from runtime_config import load_runtime_risk_settings
from risk_manager import (
    DailyRiskManager,
    StrategyCacheManager,
    TrailingStopTracker,
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
    entry_signal,
    is_night_session,
    recovery_rebound_signal,
    select_completed_candles,
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
from websocket_manager import BithumbWebSocketClient

# 윈도우 cp949 인코딩 에러 방지 (이모지 및 한글 UTF-8 표준화)
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (OSError, AttributeError):
        pass

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
os.makedirs(DATA_DIR, exist_ok=True)
GeminiTelemetry.configure(data_dir=DATA_DIR)

# 1. 로깅(Logging) 환경 설정
LOG_DIR = os.path.join(PROJECT_ROOT, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "trading.log")

file_handler = TimedRotatingFileHandler(
    filename=LOG_FILE, when="midnight", interval=1, backupCount=30, encoding="utf-8"
)
file_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
)

handlers = [file_handler]
if sys.stderr is not None:
    stream_handler = logging.StreamHandler()
    # 포그라운드 콘솔은 운영 중 확인이 필요한 경고 이상만 출력한다.
    stream_handler.setLevel(logging.WARNING)
    stream_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    handlers.append(stream_handler)

logging.basicConfig(
    level=logging.INFO,
    handlers=handlers,
)
logger = logging.getLogger(__name__)

# 2. .env 환경 변수 로드
load_dotenv(override=True)

BITHUMB_ACCESS_KEY = os.getenv("BITHUMB_ACCESS_KEY", "")
BITHUMB_SECRET_KEY = os.getenv("BITHUMB_SECRET_KEY", "")
# 빗썸 전용 Gemini API 키 (미설정 시 공용 GEMINI_API_KEY 사용)
GEMINI_API_KEY = (os.getenv("BITHUMB_GEMINI_API_KEY") or os.getenv("GEMINI_API_KEY", "")).strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

INTERVAL_MINUTES = int(os.getenv("INTERVAL_MINUTES", "5"))
# 타 거래소와의 동시 퀀트 사이클 호출 분산을 위한 오프셋 (기본값: 0초)
CYCLE_OFFSET_SECONDS = int(os.getenv("BITHUMB_CYCLE_OFFSET_SECONDS", os.getenv("CYCLE_OFFSET_SECONDS", "0")))
TARGET_MARKETS = os.getenv("TARGET_MARKETS", "KRW-BTC,KRW-ETH,KRW-XRP,KRW-SOL,KRW-DOGE")
MIN_ORDER_KRW = float(os.getenv("MIN_ORDER_KRW", "5000"))
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "3"))
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "0.35"))
MAX_TOTAL_EXPOSURE_PCT = float(os.getenv("MAX_TOTAL_EXPOSURE_PCT", "0.90"))
MAX_ORDER_KRW = float(os.getenv("MAX_ORDER_KRW", "20000000"))
# 관찰 기간에는 차단 후보만 기록하고, 검증 후 환경 변수로 신규 매수 차단을 활성화한다.
ORDERBOOK_SLIPPAGE_ENFORCEMENT = os.getenv("ORDERBOOK_SLIPPAGE_ENFORCEMENT", "false").strip().lower() in {"1", "true", "yes", "on"}
# 모멘텀 돌파는 확정봉·호가·주문 안전 검증을 모두 통과한 소수 후보만 직접 진입한다.
# 기존 EARLY_BREAKOUT 환경 변수도 읽어 기존 운영 설정과의 호환성을 유지한다.
MOMENTUM_BREAKOUT_ENABLED = os.getenv("MOMENTUM_BREAKOUT_ENABLED", os.getenv("EARLY_BREAKOUT_ENABLED", "true")).strip().lower() in {"1", "true", "yes", "on"}
MOMENTUM_BREAKOUT_MIN_CHANGE_RATE = float(os.getenv("MOMENTUM_BREAKOUT_MIN_CHANGE_RATE", os.getenv("EARLY_BREAKOUT_MIN_CHANGE_RATE", "0.003")))
MOMENTUM_BREAKOUT_MAX_CANDIDATES = int(os.getenv("MOMENTUM_BREAKOUT_MAX_CANDIDATES", os.getenv("EARLY_BREAKOUT_MAX_CANDIDATES", "2")))
# 비율 설정은 소수(0.05)와 기존 퍼센트 표기(5, -5)를 모두 지원한다.
# 모든 실행 경로에서 동일한 정규화 함수를 사용해 200% 등의 오입력을 막는다.
_risk_settings = load_runtime_risk_settings()
MAX_DAILY_LOSS_PCT = _risk_settings.max_daily_loss_pct
TRAILING_START_PCT = _risk_settings.trailing_start_pct
TRAILING_STOP_PCT = _risk_settings.trailing_stop_pct
BTC_CRASH_THRESHOLD_PCT = _risk_settings.btc_crash_threshold_pct

TRADING_MODE = os.getenv("TRADING_MODE", "REAL").upper()
PAPER_INITIAL_KRW = float(os.getenv("PAPER_INITIAL_KRW", "1000000.0"))
PAPER_FEE_RATE = float(os.getenv("PAPER_FEE_RATE", "0.0004"))

IS_BOT_PAUSED = False
LATEST_STRATEGIES: dict[str, dict[str, Any]] = {}


def get_is_bot_paused() -> bool:
    return IS_BOT_PAUSED


def set_is_bot_paused(val: bool) -> None:
    global IS_BOT_PAUSED
    IS_BOT_PAUSED = val


# 전역 인스턴스 초기화
chart_renderer = ChartRenderer()
# data/는 빗썸 전용 저장소이므로 구형 무표기 기록은 빗썸으로 한 번만 승격한다.
trade_memory = TradeMemoryManager(exchange_scope="bithumb", legacy_exchange="bithumb")
order_journal = OrderJournal(exchange_scope="bithumb")
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
# 빗썸 판단 이력은 data/trading.db에만 기록해 업비트 DB와 분리한다.
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
    """Use public market data in PAPER mode while keeping all money virtual."""
    global paper_broker
    live_client = BithumbAPI(BITHUMB_ACCESS_KEY, BITHUMB_SECRET_KEY)

    if TRADING_MODE != "PAPER":
        return BithumbAdapter(live_client, data_dir="data", web_port=7979)
    if paper_broker is None:
        paper_broker = PaperBroker(live_client, PAPER_INITIAL_KRW, PAPER_FEE_RATE)
        logger.warning("🧪 PAPER 모드: 실제 주문은 전송되지 않으며 data/paper_account.json만 갱신됩니다.")
    return BithumbAdapter(paper_broker, data_dir="data", web_port=7979)


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
    # 지연 평가 람다로 웹소켓 생성 이후에도 최신 건강 상태를 대시보드에 제공한다.
    get_feed_health=lambda: ws_client.get_health_status(),
)

strategy_cache_mgr = StrategyCacheManager(exchange_name="bithumb")

# 빗썸 실시간 웹소켓(WebSocket) 클라이언트 (0.1초 실시간 틱 스트리밍)
ws_client = BithumbWebSocketClient(
    initial_markets=["KRW-BTC"],
    on_price_callback=lambda m, p: realtime_engine.on_price_tick(m, p),
)

private_ws = BithumbPrivateWebSocketClient(
    BITHUMB_ACCESS_KEY,
    BITHUMB_SECRET_KEY,
    # Private 이벤트는 빠른 상태 알림만 제공하며 체결 확정은 REST 대사만 사용한다.
    on_order=lambda event: order_journal.apply_private_order_event(
        event, fill_processor=fill_processor, require_rest_confirmation=True,
    ),
)


def _create_bithumb_screener(exchange: ExchangeAdapter) -> MarketScreener:
    """사이클마다 최신 env를 반영한 빗썸 스크리너를 생성한다."""
    return MarketScreener(
        exchange,
        min_trade_value_krw=float(os.getenv("MIN_TRADE_VALUE", "1000000000")),
        min_change_rate=float(os.getenv("MIN_CHANGE_RATE", "0.005")),
        max_change_rate=float(os.getenv("MAX_CHANGE_RATE", "0.25")),
        enable_early_breakout=MOMENTUM_BREAKOUT_ENABLED,
        early_breakout_min_change_rate=MOMENTUM_BREAKOUT_MIN_CHANGE_RATE,
        early_breakout_max_candidates=MOMENTUM_BREAKOUT_MAX_CANDIDATES,
    )


BITHUMB_CYCLE_PROFILE = ExchangeCycleProfile(
    exchange_key="bithumb",
    reconcile_label="",
    decision_exchange="bithumb",
    log_prefix="",
    extra_excluded_markets=frozenset(),
    create_screener=_create_bithumb_screener,
    cycle_start_label="5분 AI 퀀트 트레이딩",
    tier_label="스마트 자산 티어",
    tier_top_wording="스크리닝 상위",
    summary_label="자산 요약",
    btc_crash_label="비트코인 급락 위험 감지",
)

BITHUMB_EXIT_PROFILE = ExchangeExitProfile()

BITHUMB_ENTRY_PROFILE = ExchangeEntryProfile(
    signal_exchange="bithumb",
    recovery_db_exchange="bithumb",
    continue_on_inactive_status=True,
)


def _send_btc_crash_buy_block_alert(market: str, korean_name: str, btc_status_msg: str) -> None:
    """BTC 급락 시 알트코인 매수 차단 알림."""
    telegram.send_debounced_message(
        category_key=f"btc_crash_{market}",
        text=(
            f"⚠️ <b>[{korean_name}({market}) 매수 차단 - BTC 급락 방어]</b>\n"
            f"• 사유: <i>{btc_status_msg}</i>\n"
            f"• 대장주(BTC) 급락으로 인한 알트코인 동반 폭락 위험 방지"
        ),
        min_interval_sec=900.0,
    )


BITHUMB_BUY_PROFILE = ExchangeBuyProfile(
    exchange_name="bithumb",
    enable_cycle_stop_loss=True,
    enforce_pre_buy_safety_gates=True,
    block_unresolved_market=True,
    block_duplicate_position=True,
    block_alt_on_btc_crash=True,
    use_slot_based_budget=True,
    use_entry_price_guard=True,
    risk_guard_log_label="통합 리스크 검증",
    send_btc_crash_block_alert=_send_btc_crash_buy_block_alert,
)


def cancel_bot_open_orders(bithumb: BithumbAPI, market: str | None = None) -> int:
    """봇이 발행한 미체결 주문만 선별 취소하여 외부/수동 주문 보호"""
    return realtime_engine.cancel_bot_open_orders(market=market)


cycle_engine = TradingCycleEngine(
    TradingRuntimeConfig(
        profile=BITHUMB_CYCLE_PROFILE,
        exit_profile=BITHUMB_EXIT_PROFILE,
        entry_profile=BITHUMB_ENTRY_PROFILE,
        buy_profile=BITHUMB_BUY_PROFILE,
        env_file=None,
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


def check_btc_market_crash(bithumb: BithumbAPI, threshold_pct: float = BTC_CRASH_THRESHOLD_PCT) -> tuple[bool, str, str]:
    return cycle_orchestrator.classify_market_regime(
        bithumb, interval_minutes=INTERVAL_MINUTES, crash_threshold_pct=threshold_pct,
    )


def send_daily_morning_report():
    """매일 아침 09:00 KST 일일 결산 모닝 리포트를 전송한다."""
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
        held_names = [f"{bithumb.get_korean_name(m)}({m.split('-')[-1]})" for m in held_markets]
        held_desc = ", ".join(held_names) if held_names else "없음 (100% 현금 보유)"

        ai_briefing = ""
        analyzer = GeminiAnalyzer() if os.getenv("GEMINI_API_KEY") else None
        if analyzer is not None and hasattr(analyzer, "generate_market_briefing"):
            try:
                candles_1h = bithumb.get_candles(unit=60, count=30, market="KRW-BTC")
                macro_diag = analyzer.diagnose_macro_regime(candles_1h, fng_index=fng)
                ai_comment = analyzer.generate_market_briefing(
                    exchange_name="빗썸",
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
                logger.debug(f"AI 브리핑 생성 예외: {e}")

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
            f"{ai_briefing}"
        )
    except Exception as e:
        logger.error(f"모닝 리포트 발송 실패: {e}")


def run_cycle():
    """
    [MTF + 호가창 수급 + ATR 변동성 + 자가학습 메모리 + 차트 이미지 + 복리 자금관리] 5분 오케스트레이션 사이클
    """
    cycle_engine.run_cycle()


def update_heartbeat() -> None:
    """워치독 헬스체크 및 무응답(Hang) 방지를 위한 하트비트 파일 원자적 갱신"""
    hb_file = os.path.join(DATA_DIR, ".heartbeat")
    try:
        write_json_atomically(hb_file, {
            "timestamp": time.time(),
            "pid": os.getpid(),
            "datetime": get_kst_now_str(),
            "status": "RUNNING",
            "bot": "bithumb",
        })
    except Exception as e:
        logger.debug(f"하트비트 기록 예외: {e}")


BITHUMB_BOOTSTRAP_PROFILE = ExchangeBootstrapProfile(
    exchange_key="bithumb",
    migration_base_dir=DATA_DIR,
    data_dir=DATA_DIR,
    heartbeat_bot_name="bithumb",
    internal_port_env_key="BITHUMB_INTERNAL_PORT",
    internal_port_default=17979,
    internal_api_title="Bithumb Trading Core API",
    scheduler_cycle_job_id="run_trading_cycle",
    scheduler_morning_job_id="morning_daily_report",
    startup_banner_lines=("  Bithumb AI Pro Quant Trading Bot v5.0 가동 시작",),
    shutdown_start_message="🛑 프로세스 종료 시그널 감지. 자원을 안전하게 해제합니다...",
    shutdown_complete_message="✅ 빗썸 봇 모든 자원 정상 해제 완료",
    catch_main_loop_exceptions=True,
)


def main():
    bootstrap = TradingBotBootstrap(
        BITHUMB_BOOTSTRAP_PROFILE,
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
