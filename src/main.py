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

from bithumb_api import BithumbAPI
from exchange_adapter import BithumbAdapter, ExchangeAdapter
from bot_controller import BotController
from chart_renderer import ChartRenderer
from gemini_analyzer import GeminiAnalyzer
from market_screener import MarketScreener
from order_safety import (
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
from sheets_manager import SheetsManager
from strategy_engine import (
    StrategyPolicy,
    calculate_relative_strength,
    calculate_vwap,
    entry_signal,
    select_completed_candles,
)
from telegram_alert import TelegramAlert
from trading_orchestrator import TradingOrchestrator
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
    filename=LOG_FILE, when="midnight", interval=1, backupCount=30, encoding="utf-8"
)
file_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
)

handlers = [file_handler]
if sys.stderr is not None:
    stream_handler = logging.StreamHandler()
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
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "service_account.json")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "비트코인자동매매")

INTERVAL_MINUTES = int(os.getenv("INTERVAL_MINUTES", "5"))
TARGET_MARKETS = os.getenv("TARGET_MARKETS", "KRW-BTC,KRW-ETH,KRW-XRP,KRW-SOL,KRW-DOGE")
MIN_ORDER_KRW = float(os.getenv("MIN_ORDER_KRW", "5000"))
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "3"))
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "0.35"))
MAX_TOTAL_EXPOSURE_PCT = float(os.getenv("MAX_TOTAL_EXPOSURE_PCT", "0.90"))
MAX_ORDER_KRW = float(os.getenv("MAX_ORDER_KRW", "20000000"))
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
telegram = TelegramAlert(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
sheets = SheetsManager(
    json_key_path=GOOGLE_SERVICE_ACCOUNT_JSON,
    sheet_name=GOOGLE_SHEET_NAME,
)

fill_processor = OrderFillProcessor(
    order_journal=order_journal,
    risk_manager=risk_manager,
    trade_memory=trade_memory,
    trailing_tracker=trailing_tracker,
    telegram=telegram,
    sheets=sheets,
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
    sheets=sheets,
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


def cancel_bot_open_orders(bithumb: BithumbAPI, market: str | None = None) -> int:
    """봇이 발행한 미체결 주문만 선별 취소하여 외부/수동 주문 보호"""
    return realtime_engine.cancel_bot_open_orders(market=market)


def check_btc_market_crash(bithumb: BithumbAPI, threshold_pct: float = BTC_CRASH_THRESHOLD_PCT) -> tuple[bool, str, str]:
    return cycle_orchestrator.classify_market_regime(
        bithumb, interval_minutes=INTERVAL_MINUTES, crash_threshold_pct=threshold_pct,
    )


def send_daily_morning_report():
    """매일 아침 09:00 KST 일일 결산 모닝 리포트 전송 및 구글 스프레드시트 일괄 배치 동기화"""
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

        # 🔄 구글 스프레드시트 09:00 일괄 배치 동기화 실행
        sheet_sync_status = "건너뜀 (미설정)"
        if sheets:
            try:
                summary_data = {
                    "updated_at": now_str,
                    "total_equity": total_equity,
                    "krw_available": krw_avail,
                    "daily_pnl_pct": daily_pnl_pct,
                    "daily_pnl_krw": daily_pnl_krw,
                    "realized_pnl_krw": risk_manager.realized_pnl_krw,
                    "trades_count": risk_manager.total_trades_today,
                    "win_count": risk_manager.win_trades_today,
                    "held_coins": held_desc,
                    "kill_switch_status": "🟢 정상",
                    "btc_health": "🟢 정상",
                    "fear_and_greed": fng.get("desc", "50점 (중립)"),
                    "bot_state": "🟢 24시간 정상 가동 중",
                }
                synced = sheets.sync_all_daily_batch(
                    summary_data=summary_data,
                    total_trades=risk_manager.total_trades_today,
                    win_trades=risk_manager.win_trades_today,
                    realized_pnl_krw=risk_manager.realized_pnl_krw,
                    daily_history=risk_manager.daily_history,
                    order_journal_orders=order_journal.orders,
                    trade_memory_trades=trade_memory.trades,
                    latest_strategies=LATEST_STRATEGIES,
                    target_markets=list(LATEST_STRATEGIES.keys()),
                    get_korean_name_fn=bithumb.get_korean_name,
                )
                sheet_sync_status = "✅ 동기화 완료" if synced else "⚠️ 동기화 실패"
            except Exception as se:
                logger.warning(f"모닝 리포트 구글 시트 동기화 예외: {se}")
                sheet_sync_status = f"⚠️ 오류 ({se})"

        telegram.send_message(
            f"🌅 <b>[빗썸 AI 퀀트 봇 - 09:00 KST 일일 성과 결산 브리핑]</b>\n\n"
            f"• <b>총 평가 자산:</b> {total_equity:,.0f} KRW\n"
            f"• <b>금일 자산 변동:</b> {daily_pnl_krw:+,.0f} KRW ({daily_pnl_pct:+.2f}%)\n"
            f"• <b>금일 확정 실현 손익:</b> {risk_manager.realized_pnl_krw:+,.0f} KRW (총 {risk_manager.total_trades_today}회 거래)\n"
            f"• <b>가용 원화 잔고:</b> {krw_avail:,.0f} KRW\n"
            f"• <b>현재 보유 포지션:</b> {held_desc}\n"
            f"• <b>크립토 공포/탐욕 지수:</b> {fng['desc']}\n"
            f"• <b>구글 시트 동기화:</b> {sheet_sync_status}\n"
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
    logger.info(f"============================================================")
    logger.info(f"🚀 [5분 AI 퀀트 트레이딩 사이클 가동: {now_str}]")
    logger.info(f"============================================================")

    try:
        bithumb = create_exchange_client()
        analyzer = GeminiAnalyzer(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

        # 0. REST 기반 미완료 주문 체결 상태 자동 재조정 (공통 오케스트레이터)
        cycle_orchestrator.reconcile_orders(bithumb, order_journal, fill_processor)

        snapshot = cycle_orchestrator.refresh_portfolio(
            bithumb, calculate_total_equity=calculate_total_equity, get_held_markets=get_held_markets,
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
            logger.info("미체결 주문 정리/정정: 취소 %d건, 정정 %d건", canceled_stale, requoted)
        is_kill_switch, daily_pnl = snapshot.is_kill_switch, snapshot.daily_pnl
        is_cooldown, cd_minutes = snapshot.is_cooldown, snapshot.cooldown_minutes
        dyn_max_positions, dyn_max_pos_pct, dyn_top_count = snapshot.max_positions, snapshot.max_position_pct, snapshot.top_count
        tot_disp = f"{current_total_equity:,.2f}원" if (0 < current_total_equity < 100 or current_total_equity % 1 != 0 and current_total_equity < 1000) else f"{current_total_equity:,.0f}원"
        krw_disp = f"{krw_available:,.2f}원" if (0 < krw_available < 100 or krw_available % 1 != 0 and krw_available < 1000) else f"{krw_available:,.0f}원"
        logger.info(
            f"📊 [스마트 자산 티어] 총 자산 {tot_disp} ➜ 최대 {dyn_max_positions}종목 분할 (종목당 {dyn_max_pos_pct*100:.0f}% 한도, 스크리닝 상위 {dyn_top_count}개)"
        )

        # 3-3. 비트코인 급락 및 시장 레짐 감시
        is_btc_crashing, btc_regime, btc_status_msg = check_btc_market_crash(bithumb, threshold_pct=btc_crash_pct)
        trailing_tracker.set_macro_defensive_mode(is_btc_crashing)
        if is_btc_crashing:
            logger.warning(f"⚠️ [비트코인 급락 위험 감지] 레짐: {btc_regime} ({btc_status_msg}) ➜ 보유 알트코인 비상 방어 모드 가동")

        fng = get_fear_and_greed_index()
        tot_disp = f"{current_total_equity:,.2f}원" if (0 < current_total_equity < 100 or current_total_equity % 1 != 0 and current_total_equity < 1000) else f"{current_total_equity:,.0f}원"
        krw_disp = f"{krw_available:,.2f}원" if (0 < krw_available < 100 or krw_available % 1 != 0 and krw_available < 1000) else f"{krw_available:,.0f}원"
        logger.info(f"📊 [자산 요약] 총 자산: {tot_disp} | 원화: {krw_disp} | 당일 손익: {daily_pnl*100:+.2f}% | 공포탐욕: {fng['desc']}")

        bot_state_badge = "⏸️ 일시정지 중" if IS_BOT_PAUSED else ("🛑 킬스위치 발동" if is_kill_switch else ("❄️ 쿨다운 대기" if is_cooldown else "🟢 정상 가동 중"))

        # 대시보드 및 구글 시트 동기화
        bot_controller.get_dashboard_data()



        target_markets = cycle_orchestrator.select_target_markets(
            bithumb, held_markets=held_markets, is_auto_mode=is_auto_mode, raw_markets=raw_markets,
            max_positions=dyn_max_positions, top_count=dyn_top_count,
            create_screener=lambda: MarketScreener(bithumb, min_trade_value_krw=min_trade_val,
                                                    min_change_rate=min_change, max_change_rate=max_change),
            btc_regime=btc_regime,
        )
        logger.info(f"이번 사이클 최종 분석 대상 마켓 ({len(target_markets)}개): {target_markets}")



        # ⚡ 빗썸 실시간 웹소켓(WebSocket) 구독 갱신
        ws_client.update_subscriptions(list(dict.fromkeys(target_markets + held_markets + ["KRW-BTC"])))

        # 5. 마켓별 순회 분석 및 매매 실행
        for market in target_markets:
            try:
                market_snapshot = cycle_orchestrator.load_market_snapshot(bithumb, market, INTERVAL_MINUTES)
                currency, korean_name = market_snapshot.currency, market_snapshot.korean_name
                logger.info(f"--- [{korean_name} / {market} AI 퀀트 분석 시작] ---")
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
                        sell_ratio = (30.0 / 70.0) if is_stage2 else StrategyPolicy.PARTIAL_TP_1_RATIO
                        sell_vol = coin_available * sell_ratio
                        sell_val = sell_vol * current_price
                        stage_name = "2차 30%" if is_stage2 else "1차 30%"
                        if sell_val >= MIN_ORDER_KRW and trailing_tracker.acquire_exit_lock(market):
                            try:
                                logger.info(
                                    f"🎉 [{korean_name} / {market} {stage_name} 분할익절 발동] 현재가 {current_price:,.2f}원(+{realized_profit_pct:.2f}%). {stage_name} 시장가 익절!"
                                )
                                cancel_bot_open_orders(bithumb, market)

                                order_res = order_executor.submit(
                                    bithumb,
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

                                # 실제 체결 내역 조회 (가상 손익 미생성, P0-1, P0-3)
                                time.sleep(0.05)
                                try:
                                    remote = bithumb.get_order(order_uuid) if order_uuid != "UNKNOWN" else None
                                    if isinstance(remote, dict) and float(remote.get("executed_volume", 0.0) or 0.0) > 0:
                                        fill_processor.process_order_fill(
                                            order_identifier=client_order_id or order_uuid,
                                            status=OrderStatus.FILLED if float(remote.get("remaining_volume", 0.0) or 0.0) == 0 else OrderStatus.PARTIALLY_FILLED,
                                            executed_volume=float(remote.get("executed_volume", 0.0)),
                                            avg_price=float(remote.get("price", 0.0) or current_price),
                                            fee=float(remote.get("paid_fee", 0.0) or 0.0),
                                            remaining_volume=float(remote.get("remaining_volume", 0.0) or 0.0),
                                            exchange_uuid=order_uuid,
                                            exit_reason=f"PARTIAL_TP_{2 if is_stage2 else 1}",
                                            avg_buy_price=avg_buy_price,
                                            korean_name=korean_name,
                                            timestamp_str=now_str,
                                        )
                                except Exception as exc:
                                    logger.debug("PARTIAL_TP 체결 조회 예외: %s", exc)

                                caption = (
                                    f"🎉 <b>[{korean_name}({market}) {stage_name} 분할익절 접수]</b>\n"
                                    f"• 요청단가: {current_price:,.2f} KRW (+{realized_profit_pct:.2f}%)\n"
                                    f"• 매도수량: {sell_vol:.8f} {currency}\n"
                                    f"• 주문 ID: <code>{order_uuid}</code>\n"
                                    f"• 상태: <i>거래소 접수 완료 (실체결 시 정산)</i>\n"
                                    f"• 일시: {now_str}"
                                )
                                telegram.send_message(caption)
                            finally:
                                trailing_tracker.release_exit_lock(market)

                    elif action_type == "TRAILING_STOP":
                        if trailing_tracker.acquire_exit_lock(market):
                            try:
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
                                    position_id=market,
                                    exit_reason="TRAILING_STOP",
                                    avg_buy_price=avg_buy_price,
                                )
                                order_uuid = order_res.get("uuid") or order_res.get("order_id", "UNKNOWN")
                                client_order_id = order_res.get("client_order_id", "")

                                # 실제 체결 내역 조회 (가상 손익 미생성, P0-1, P0-3)
                                time.sleep(0.05)
                                try:
                                    remote = bithumb.get_order(order_uuid) if order_uuid != "UNKNOWN" else None
                                    if isinstance(remote, dict) and float(remote.get("executed_volume", 0.0) or 0.0) > 0:
                                        fill_processor.process_order_fill(
                                            order_identifier=client_order_id or order_uuid,
                                            status=OrderStatus.FILLED if float(remote.get("remaining_volume", 0.0) or 0.0) == 0 else OrderStatus.PARTIALLY_FILLED,
                                            executed_volume=float(remote.get("executed_volume", 0.0)),
                                            avg_price=float(remote.get("price", 0.0) or current_price),
                                            fee=float(remote.get("paid_fee", 0.0) or 0.0),
                                            remaining_volume=float(remote.get("remaining_volume", 0.0) or 0.0),
                                            exchange_uuid=order_uuid,
                                            exit_reason="TRAILING_STOP",
                                            avg_buy_price=avg_buy_price,
                                            korean_name=korean_name,
                                            timestamp_str=now_str,
                                        )
                                except Exception as exc:
                                    logger.debug("TRAILING_STOP 체결 조회 예외: %s", exc)

                                pnl_krw = (current_price - avg_buy_price) * coin_available
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


                                cooldown_manager.record_exit(market, "TRAILING_STOP", exit_price=current_price)
                            finally:
                                trailing_tracker.release_exit_lock(market)
                            continue

                # =========================================================================
                # ⏳ [최우선 2: 15분 모멘텀 소멸 조기 탈출 & 60분 횡보 방지 타임스탑]
                # =========================================================================
                if coin_value >= MIN_ORDER_KRW and avg_buy_price > 0 and not order_journal.has_active_exit_order(market):
                    entry_ts = trailing_tracker.get_entry_time(market)
                    if entry_ts <= 0:
                        # 진입 시점이 미등록된 포지션은 즉시 타임스탑 오발동을 방지하기 위해 현재 시각으로 보정 등록
                        entry_ts = time.time()
                        trailing_tracker.set_entry_time(market, entry_ts)
                        logger.info(f"⏱️ [{market}] 진입 시점 미등록 포지션 감지 ➜ 현재 시간으로 보정 등록 ({now_str})")

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

                    # 2. [본전 보장 타임스탑 - Case A]: 실질 본전 이상(+0.05% 이상) 시 60분 경과 즉시 익절/본전 청산하여 자금 회전
                    be_threshold_pct = StrategyPolicy.TIME_STOP_BREAKEVEN_MIN_PNL_PCT * 100.0  # +0.05%
                    is_breakeven_or_profit = pnl_pct_current >= be_threshold_pct
                    is_time_stop_profit_trigger = (
                        hold_duration_sec >= effective_time_stop and is_breakeven_or_profit
                    )

                    # 3. [본전 보장 타임스탑 - Case B]: 마이너스 손실(-1.5% ~ +0.05%) 구간에서는 성급한 투매를 차단하고 반등 대기
                    #    - 지지선 유지 시 최대 120분까지 손절선(-1.5%)을 지키며 반등 기회 보장
                    #    - 추세가 명백히 붕괴되었거나 최대 유예 시간(120분) 초과 시에만 방어적 청산
                    is_time_stop_loss_trigger = (
                        (hold_duration_sec >= StrategyPolicy.TIME_STOP_MAX_HOLD_SECONDS or (hold_duration_sec >= effective_time_stop and is_trend_broken))
                        and (-1.5 <= pnl_pct_current < be_threshold_pct)
                    )

                    # 4. [15분 모멘텀 조기 본전 탈출]: 15분 경과 후 실질 본전권(0.0% ~ +0.3%)에서 모멘텀 소멸(VWAP 하회) 시 수수료 세이브 탈출
                    is_early_momentum_exit = (
                        hold_duration_sec >= StrategyPolicy.MOMENTUM_EARLY_EXIT_SECONDS
                        and (0.0 <= pnl_pct_current <= 0.3)
                        and (not is_above_vwap)
                    )

                    is_time_stop_trigger = is_time_stop_profit_trigger or is_time_stop_loss_trigger or is_early_momentum_exit

                    if is_time_stop_trigger:
                        exit_reason_label = "MOMENTUM_EARLY_EXIT" if is_early_momentum_exit else "TIME_STOP"
                        if trailing_tracker.acquire_exit_lock(market):
                            try:
                                exit_desc = (
                                    f"15분 모멘텀 소멸 조기 본전 탈출"
                                    if is_early_momentum_exit
                                    else f"{effective_time_stop/60:.0f}분 횡보 타임스탑"
                                )
                                logger.info(
                                    f"⏳ [{korean_name} / {market}] {exit_desc} 발동! (레짐: {btc_regime}, 손익률: {pnl_pct_current:+.2f}%, 보유시간: {hold_duration_sec/60:.0f}분) ➜ 신규 기회를 위해 시장가 전량 청산"
                                )
                                cancel_bot_open_orders(bithumb, market)

                                order_res = order_executor.submit(
                                    bithumb,
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

                                # 실제 체결 내역 조회 (가상 손익 미생성, P0-1, P0-3)
                                time.sleep(0.05)
                                try:
                                    remote = bithumb.get_order(order_uuid) if order_uuid != "UNKNOWN" else None
                                    if isinstance(remote, dict) and float(remote.get("executed_volume", 0.0) or 0.0) > 0:
                                        fill_processor.process_order_fill(
                                            order_identifier=client_order_id or order_uuid,
                                            status=OrderStatus.FILLED if float(remote.get("remaining_volume", 0.0) or 0.0) == 0 else OrderStatus.PARTIALLY_FILLED,
                                            executed_volume=float(remote.get("executed_volume", 0.0)),
                                            avg_price=float(remote.get("price", 0.0) or current_price),
                                            fee=float(remote.get("paid_fee", 0.0) or 0.0),
                                            remaining_volume=float(remote.get("remaining_volume", 0.0) or 0.0),
                                            exchange_uuid=order_uuid,
                                            exit_reason=exit_reason_label,
                                            avg_buy_price=avg_buy_price,
                                            korean_name=korean_name,
                                            timestamp_str=now_str,
                                        )
                                except Exception as exc:
                                    logger.debug(f"{exit_reason_label} 체결 조회 예외: %s", exc)

                                caption = (
                                    f"⏳ <b>[{korean_name}({market}) {exit_desc} 접수]</b>\n"
                                    f"• 요청단가: {current_price:,.2f} KRW | 평단가: {avg_buy_price:,.2f} KRW (손익: {pnl_pct_current:+.2f}%)\n"
                                    f"• 주문 ID: <code>{order_uuid}</code>\n"
                                    f"• 사유: <i>모멘텀 둔화에 따른 자금 보호 및 다음 독자 급등주 순환매 확보</i>\n"
                                    f"• 일시: {now_str}"
                                )
                                telegram.send_message(caption)
                                cooldown_manager.record_exit(market, exit_reason_label, exit_price=current_price)
                            finally:
                                trailing_tracker.release_exit_lock(market)
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
                    exchange="bithumb",
                )
                reentry_allowed, reentry_reason = cooldown_manager.check_reentry_allowed(market, current_price)
                is_holding = (coin_value >= MIN_ORDER_KRW and avg_buy_price > 0)
                ws_health = ws_client.get_health_status(market=market) if hasattr(ws_client, "get_health_status") else {"is_healthy": True, "status": "OK"}
                ws_healthy = ws_health.get("is_healthy", True)

                should_call_ai = (
                    analyzer is not None
                    and not is_holding
                    and reentry_allowed
                    and local_entry["allow_buy"]
                    and not is_btc_crashing
                    and ws_healthy
                )

                if should_call_ai and analyzer is not None:
                    feedback_context = trade_memory.get_feedback_context()
                    whale_flow_context = ws_client.get_whale_flow_summary(market)
                    btc_candles_5m = bithumb.get_candles(unit=INTERVAL_MINUTES, count=30, market="KRW-BTC")
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
                        f"기보유 포지션 퀀트 감시 ({local_entry['reason']})"
                        if is_holding
                        else (
                            f"{reentry_reason}"
                            if not reentry_allowed
                            else (
                                f"BTC 레짐 경보 ({btc_status_msg})"
                                if is_btc_crashing
                                else (
                                    f"웹소켓 데이터 불안정 ({ws_health.get('status', 'UNHEALTHY')}) 진입 차단"
                                    if not ws_healthy
                                    else f"1차 퀀트 관망 대기: {local_entry['reason']}"
                                )
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

                if not reentry_allowed and action == "BUY":
                    action = "HOLD"
                    reason = f"{reentry_reason} | {reason}"

                if action == "BUY" and not local_entry["allow_buy"]:
                    action = "HOLD"
                    reason = f"정량 공통 진입 게이트 차단: {local_entry['reason']} | {reason}"
                elif action == "BUY" and local_entry["allow_buy"]:
                    target_price = local_entry["target_price"]
                    stop_loss = local_entry["stop_loss"]

                # 알파 스코어 연동 가변 사이징 (A+ 85점 이상 비중 확대)
                alpha_val = local_entry.get("alpha_score", 70)
                if alpha_val >= 85 and btc_regime != "RISK_OFF" and action == "BUY":
                    alloc_pct = min(dyn_max_pos_pct * 1.3, 0.65)
                    reason = f"[🔥알파 {alpha_val}점 A+ 특급 셋업 비중 확대(65%)] {reason}"
                elif alpha_val < 75 and btc_regime != "RISK_OFF" and action == "BUY":
                    alloc_pct = dyn_max_pos_pct * 0.7

                if btc_regime == "RISK_OFF" and action == "BUY":
                    alloc_pct = alloc_pct * StrategyPolicy.RISK_OFF_ALLOC_RATIO
                    reason = f"[BTC 약세 레짐 비중 30% 축소 & 알파 80점 이상 엄선] {reason}"

                if fng["is_extreme_fear"] and action == "BUY":
                    alloc_pct = min(alloc_pct, 0.4)

                logger.info(f"[{market}] 전략: ACTION={action}, 진입가={entry_price:,.2f}, 목표가={target_price:,.2f}, 손절가={stop_loss:,.2f}, 비중={alloc_pct*100:.0f}%")
                logger.info(f"[{market}] 근거: {reason}")

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

                if status != "ACTIVE":
                    continue

                # 5. [손절 검사]
                if (
                    coin_value >= MIN_ORDER_KRW
                    and stop_loss > 0
                    and current_price <= stop_loss
                    and not order_journal.has_active_exit_order(market)
                ):
                    if trailing_tracker.acquire_exit_lock(market):
                        try:
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
                                position_id=market,
                                exit_reason="STOP_LOSS",
                                avg_buy_price=avg_buy_price,
                                expected_price=current_price,
                            )
                            order_uuid = order_res.get("uuid") or order_res.get("order_id", "UNKNOWN")
                            client_order_id = order_res.get("client_order_id", "")
                            cooldown_manager.record_exit(market, "STOP_LOSS", exit_price=current_price)

                            # 실제 체결 내역 조회 (가상 손익 미생성, P0-1, P0-3)
                            time.sleep(0.05)
                            try:
                                remote = bithumb.get_order(order_uuid) if order_uuid != "UNKNOWN" else None
                                if isinstance(remote, dict) and float(remote.get("executed_volume", 0.0) or 0.0) > 0:
                                    fill_processor.process_order_fill(
                                        order_identifier=client_order_id or order_uuid,
                                        status=OrderStatus.FILLED if float(remote.get("remaining_volume", 0.0) or 0.0) == 0 else OrderStatus.PARTIALLY_FILLED,
                                        executed_volume=float(remote.get("executed_volume", 0.0)),
                                        avg_price=float(remote.get("price", 0.0) or current_price),
                                        fee=float(remote.get("paid_fee", 0.0) or 0.0),
                                        remaining_volume=float(remote.get("remaining_volume", 0.0) or 0.0),
                                        exchange_uuid=order_uuid,
                                        exit_reason="STOP_LOSS",
                                        avg_buy_price=avg_buy_price,
                                        korean_name=korean_name,
                                        timestamp_str=now_str,
                                        expected_price=current_price,
                                    )
                            except Exception as exc:
                                logger.debug("STOP_LOSS 체결 조회 예외: %s", exc)
                        finally:
                            trailing_tracker.release_exit_lock(market)

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
                        f"🚨 <b>[{korean_name}({market}) 손절 주문 접수]</b>\n"
                        f"• 진입 평단가: {avg_buy_price:,.2f} KRW\n"
                        f"• 요청 단가: {current_price:,.2f} KRW\n"
                        f"• 주문 ID: <code>{order_uuid}</code>\n"
                        f"• 사유: <i>{reason}</i>\n"
                        f"• 일시: {now_str}"
                    )
                    if chart_img:
                        telegram.send_photo(chart_img, caption=caption)
                    else:
                        telegram.send_message(caption)
                    continue

                # 6. [신규 주문 실행 - 동적 복리 자금 관리 적용]
                if action == "BUY":
                    if order_journal.has_unresolved_market(market):
                        logger.warning(
                            "[%s] 이전 주문의 거래소 결과가 확정되지 않아 신규 매수를 차단합니다. data/order_journal.json을 확인하세요.",
                            market,
                        )
                        continue

                    if IS_BOT_PAUSED or not order_journal.is_entry_ready():
                        if not order_journal.is_entry_ready():
                            logger.warning("[%s] REST 주문 대사 미완료로 신규 매수를 차단합니다.", market)
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
                    order_price = bithumb.adjust_price_to_tick(order_price, side="bid")

                    risk_scale = risk_manager.get_risk_scale_factor()
                    risk_based_budget = calculate_risk_position_size(
                        total_equity=effective_capital,
                        entry_price=order_price,
                        stop_loss=stop_loss,
                        risk_fraction=0.01,
                        fee_rate=0.0004,
                        slippage_rate=0.001,
                        max_position_pct=dyn_max_pos_pct,
                        min_order_krw=MIN_ORDER_KRW,
                        risk_scale_factor=risk_scale,
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

                    # 승인 시점 값만 주문 원장에 저장해, 미래 체결 시점 정보와 혼동하지 않는다.
                    entry_snapshot = dict(local_entry.get("strategy_snapshot", {}))
                    entry_snapshot.update({
                        "exchange": "bithumb",
                        "market": market,
                        "entry_decision_at": now_str,
                        "entry_reason": reason,
                        "target_price": target_price,
                        "stop_loss": stop_loss,
                    })
                    position_id = f"bithumb:{market}:{int(time.time() * 1000)}"
                    order_res = order_executor.submit(
                        bithumb,
                        market=market,
                        side="bid",
                        price=order_price,
                        volume=formatted_volume,
                        ord_type="limit",
                        position_id=position_id,
                        expected_price=order_price,
                        entry_strategy_snapshot=entry_snapshot,
                        exchange_name="bithumb",
                    )
                    order_uuid = order_res.get("uuid") or order_res.get("order_id", "UNKNOWN")
                    client_order_id = order_res.get("client_order_id", "")

                    # 즉시 체결 여부 확인 (실제 체결 시에만 진입시간 생성, P0-1)
                    time.sleep(0.05)
                    try:
                        remote = bithumb.get_order(order_uuid) if order_uuid != "UNKNOWN" else None
                        if isinstance(remote, dict) and float(remote.get("executed_volume", 0.0) or 0.0) > 0:
                            fill_processor.process_order_fill(
                                order_identifier=client_order_id or order_uuid,
                                status=OrderStatus.FILLED if float(remote.get("remaining_volume", 0.0) or 0.0) == 0 else OrderStatus.PARTIALLY_FILLED,
                                executed_volume=float(remote.get("executed_volume", 0.0)),
                                avg_price=float(remote.get("price", 0.0) or order_price),
                                fee=float(remote.get("paid_fee", 0.0) or 0.0),
                                remaining_volume=float(remote.get("remaining_volume", 0.0) or 0.0),
                                exchange_uuid=order_uuid,
                                korean_name=korean_name,
                                timestamp_str=now_str,
                                expected_price=order_price,
                            )
                    except Exception as exc:
                        logger.debug("BUY 체결 조회 예외: %s", exc)

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
                        f"🛒 <b>[{korean_name}({market}) AI 신규 매수 주문 접수]</b>\n"
                        f"• 지정가: {order_price:,.2f} KRW\n"
                        f"• 주문수량: {formatted_volume:.6f} {currency}\n"
                        f"• <b>투입예산: {int(trade_budget):,d} KRW (슬롯비중: {alloc_pct*100:.0f}%)</b>\n"
                        f"• 목표가: {target_price:,.2f} KRW | 손절가: {stop_loss:,.2f} KRW\n"
                        f"• 상태: <i>거래소 접수 완료 (체결 대기)</i>\n"
                        f"• 주문 ID: <code>{order_uuid}</code>\n"
                        f"• 일시: {now_str}"
                    )
                    if chart_img:
                        telegram.send_photo(chart_img, caption=caption)
                    else:
                        telegram.send_message(caption)

            except Exception as e:
                logger.error(f"[{market}] 매매 사이클 오류 발생: {e}", exc_info=True)

        # 6. 이번 사이클에서 분석/갱신된 전체 전략 영속 디스크 캐시 저장 (재시작 시 중복 API 호출 방지)
        strategy_cache_mgr.save_cache(LATEST_STRATEGIES)

    except Exception as e:
        logger.error(f"전체 트레이딩 사이클 예외 발생: {e}", exc_info=True)


def update_heartbeat() -> None:
    """워치독 헬스체크 및 무응답(Hang) 방지를 위한 하트비트 파일 원자적 갱신"""
    data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    hb_file = os.path.join(data_dir, ".heartbeat")
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


def main():
    logger.info("============================================================")
    logger.info("  Bithumb AI Pro Quant Trading Bot v5.0 가동 시작")
    logger.info("============================================================")

    # 0. 즉시 첫 하트비트 기록
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

    # 2. 로컬 전용 초경량 API 서버 가동 (127.0.0.1:17979 - 통합 대시보드 연동용)
    bithumb_internal_port = int(os.getenv("BITHUMB_INTERNAL_PORT", "17979"))
    web_server = DashboardWebServer(
        host="0.0.0.0",
        port=bithumb_internal_port,
        data_provider=bot_controller.get_dashboard_data,
        action_handler=bot_controller.handle_web_action,
        title="Bithumb Trading Core API",
        is_api_only=True,
    )
    web_server.start()

    # 3. 실시간 웹소켓(WebSocket) 스트리밍 가동
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
            logger.debug(f"포지션 전략 복원 예외: {e}")
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
        first_run_time = datetime.now()

    scheduler = BackgroundScheduler(timezone="Asia/Seoul")
    scheduler.add_job(
        run_cycle,
        "interval",
        minutes=INTERVAL_MINUTES,
        next_run_time=first_run_time if not should_run_immediate else None,
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
        logger.info("🛑 프로세스 종료 시그널 감지. 자원을 안전하게 해제합니다...")
        try:
            telegram.stop()
        except Exception as e:
            logger.debug(f"텔레그램 종료 예외: {e}")
        try:
            ws_client.stop()
        except Exception as e:
            logger.debug(f"WebSocket 종료 예외: {e}")
        if private_ws:
            try:
                private_ws.stop()
            except Exception as e:
                logger.debug(f"Private WS 종료 예외: {e}")
        try:
            web_server.stop()
        except Exception as e:
            logger.debug(f"웹 대시보드 종료 예외: {e}")
        try:
            scheduler.shutdown(wait=False)
        except Exception as e:
            logger.debug(f"스케줄러 종료 예외: {e}")
        logger.info("✅ 빗썸 봇 모든 자원 정상 해제 완료")
        sys.exit(0)

    signal.signal(signal.SIGINT, _handle_exit)
    signal.signal(signal.SIGTERM, _handle_exit)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _handle_exit)

    # 7. 메인 스레드 유지 및 주기적 하트비트 루프
    last_hb_ts = 0.0
    while True:
        try:
            now_ts = time.time()
            # 네트워크 수신 콜백은 큐 적재만 하며 주문·파일 작업은 메인 스레드에서 직렬화한다.
            ws_client.drain_callbacks()
            if private_ws is not None:
                private_ws.drain_order_events()
            if now_ts - last_hb_ts >= 15.0:
                update_heartbeat()
                last_hb_ts = now_ts
            time.sleep(1)
        except (KeyboardInterrupt, SystemExit):
            _handle_exit(None, None)
        except Exception as e:
            logger.error(f"메인 루프 예외 발생: {e}", exc_info=True)
            time.sleep(1)


if __name__ == "__main__":
    main()
