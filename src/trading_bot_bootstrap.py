"""Shared process bootstrap for dual-exchange trading entry points.

Telegram listener, internal API, WebSocket lifecycle, strategy cache restore,
APScheduler, graceful shutdown, and the main heartbeat loop are centralized here.
"""

from __future__ import annotations

import atexit
import os
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from apscheduler.schedulers.background import BackgroundScheduler

from db_manager import migrate_legacy_json_to_sqlite
from market_intelligence import MarketIntelligenceService
from web_server import DashboardWebServer


@dataclass(frozen=True)
class ExchangeBootstrapProfile:
    """Exchange-specific bootstrap labels, ports, and paths."""

    exchange_key: str
    migration_base_dir: str
    data_dir: str
    heartbeat_bot_name: str
    internal_port_env_key: str
    internal_port_default: int
    internal_api_title: str
    scheduler_cycle_job_id: str
    scheduler_morning_job_id: str
    startup_banner_lines: tuple[str, ...]
    shutdown_start_message: str
    shutdown_complete_message: str
    log_prefix: str = ""
    catch_main_loop_exceptions: bool = False


@dataclass
class TradingBootstrapContext:
    """Runtime dependencies injected from each exchange entry point."""

    logger: Any
    telegram: Any
    bot_controller: Any
    ws_client: Any
    private_ws: Any | None
    strategy_cache_manager: Any
    latest_strategies: dict[str, Any]
    interval_minutes: int
    create_exchange_client: Callable[[], Any]
    get_held_markets: Callable[..., list[str]]
    run_cycle: Callable[[], None]
    send_daily_morning_report: Callable[[], None]
    update_heartbeat: Callable[[], None]
    cycle_offset_seconds: int = 0
    reconcile_after_private_ws: Callable[[], None] | None = None
    warmup_callback: Callable[[], None] | None = None


class TradingBotBootstrap:
    """Bootstraps long-running trading process lifecycle for one exchange."""

    def __init__(self, profile: ExchangeBootstrapProfile, context: TradingBootstrapContext) -> None:
        self.profile = profile
        self.ctx = context
        self._web_server: DashboardWebServer | None = None
        self._scheduler: BackgroundScheduler | None = None
        self._is_exiting = False

    def run(self) -> None:
        """Start services and block on the main heartbeat loop."""
        self._log_startup_banner()
        self._migrate_sqlite()
        self.ctx.update_heartbeat()
        self._start_telegram_listener()
        self._start_internal_api()
        self._start_websockets()
        self._run_warmup_if_provided()
        should_run_immediate = self._restore_strategy_cache()
        self._start_scheduler(should_run_immediate)
        self._run_initial_cycle_if_needed(should_run_immediate)
        self._register_shutdown_handlers()
        self._main_loop()

    def _run_warmup_if_provided(self) -> None:
        if self.ctx.warmup_callback is not None:
            try:
                self.ctx.warmup_callback()
            except Exception as exc:
                self.ctx.logger.debug("선제 웜업 콜백 실행 예외 (무시): %s", exc)

    def _log_startup_banner(self) -> None:
        self.ctx.logger.info("============================================================")
        for line in self.profile.startup_banner_lines:
            self.ctx.logger.info(line)
        self.ctx.logger.info("============================================================")

    def _migrate_sqlite(self) -> None:
        try:
            migrate_legacy_json_to_sqlite(self.profile.migration_base_dir)
        except Exception as exc:
            self.ctx.logger.warning("SQLite 초기 마이그레이션 건너뜀: %s", exc)

    def _start_telegram_listener(self) -> None:
        controller = self.ctx.bot_controller
        self.ctx.telegram.start_command_listener(
            status_callback=controller.get_status_message,
            balance_callback=controller.get_balance_message,
            panic_callback=controller.execute_panic_sell,
            pause_callback=controller.pause_bot,
            resume_callback=controller.resume_bot,
            diag_callback=controller.get_diagnostics_message,
            trades_callback=controller.get_trades_summary_message,
        )

    def _start_internal_api(self) -> None:
        internal_port = int(
            os.getenv(self.profile.internal_port_env_key, str(self.profile.internal_port_default))
        )
        self._web_server = DashboardWebServer(
            host="0.0.0.0",
            port=internal_port,
            data_provider=self.ctx.bot_controller.get_dashboard_data,
            action_handler=self.ctx.bot_controller.handle_web_action,
            config_provider=self.ctx.bot_controller.get_runtime_config,
            config_updater=self.ctx.bot_controller.update_runtime_config,
            title=self.profile.internal_api_title,
            is_api_only=True,
        )
        self._web_server.start()

    def _start_websockets(self) -> None:
        self.ctx.ws_client.start()
        if self.ctx.private_ws:
            self.ctx.private_ws.start()

    def _restore_strategy_cache(self) -> bool:
        cycle_ttl = self.ctx.interval_minutes * 60 - 30
        cached_strats, elapsed_sec, is_cache_valid = (
            self.ctx.strategy_cache_manager.get_valid_strategies(ttl=cycle_ttl)
        )
        if cached_strats:
            self.ctx.latest_strategies.update(cached_strats)
            try:
                exchange = self.ctx.create_exchange_client()
                held_markets = self.ctx.get_held_markets(exchange.get_balances(), exchange)
                self.ctx.bot_controller.restore_missing_position_strategies(held_markets)
            except Exception as exc:
                self.ctx.logger.debug(
                    "%s포지션 전략 복원 예외: %s",
                    self.profile.log_prefix,
                    exc,
                )
            self.ctx.logger.info(
                "📂 [전략 캐시 복원] 디스크에서 %d개 종목의 직전 분석 데이터를 대시보드에 즉시 복원했습니다.",
                len(cached_strats),
            )
        return not is_cache_valid

    def _start_scheduler(self, should_run_immediate: bool) -> None:
        interval_minutes = self.ctx.interval_minutes
        offset_sec = max(0, getattr(self.ctx, "cycle_offset_seconds", 0))
        cycle_ttl = interval_minutes * 60 - 30
        _, elapsed_sec, is_cache_valid = (
            self.ctx.strategy_cache_manager.get_valid_strategies(ttl=cycle_ttl)
        )

        if is_cache_valid:
            remaining_sec = max(15, int(interval_minutes * 60 - elapsed_sec))
            first_run_time = datetime.now() + timedelta(seconds=remaining_sec)
            self.ctx.logger.info(
                "⚡ [스마트 캐시 유지] 직전 분석 후 %.0f초 경과 (5분 캔들 유효). "
                "중복 AI/REST 호출을 생략하고 %d초 후 다음 정기 분석을 시작합니다.",
                elapsed_sec,
                remaining_sec,
            )
        else:
            if offset_sec > 0:
                first_run_time = datetime.now() + timedelta(seconds=offset_sec)
                self.ctx.logger.info(
                    "⏰ [사이클 오프셋 적용] 타 거래소와의 API 호출 분산을 위해 %d초(%.1f분) 후 첫 정기 분석을 시작합니다.",
                    offset_sec,
                    offset_sec / 60.0,
                )
            else:
                first_run_time = datetime.now() + timedelta(minutes=interval_minutes)

        self._scheduler = BackgroundScheduler(timezone="Asia/Seoul")
        self._scheduler.add_job(
            self.ctx.run_cycle,
            "interval",
            minutes=interval_minutes,
            next_run_time=first_run_time,
            id=self.profile.scheduler_cycle_job_id,
            max_instances=1,
            coalesce=True,
        )
        self._scheduler.add_job(
            self.ctx.send_daily_morning_report,
            "cron",
            hour=9,
            minute=0,
            id=self.profile.scheduler_morning_job_id,
            max_instances=1,
        )
        self._scheduler.start()
        self.ctx.logger.info(
            "⏰ APScheduler 가동 완료 (%d분 주기 매매 및 매일 09:00 모닝 리포트)",
            interval_minutes,
        )

    def _run_initial_cycle_if_needed(self, should_run_immediate: bool) -> None:
        offset_sec = max(0, getattr(self.ctx, "cycle_offset_seconds", 0))
        # 오프셋이 설정된 경우 초기 즉시 실행을 건너뛰고 스케줄러의 first_run_time(offset 후)에 실행
        if not should_run_immediate or offset_sec > 0:
            return
        try:
            self.ctx.run_cycle()
        except Exception as exc:
            self.ctx.logger.error("초기 사이클 실행 중 오류: %s", exc)

    def _register_shutdown_handlers(self) -> None:
        signal.signal(signal.SIGINT, self._handle_exit)
        signal.signal(signal.SIGTERM, self._handle_exit)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, self._handle_exit)
        atexit.register(self._handle_exit)

    def _handle_exit(self, sig=None, frame=None) -> None:
        if self._is_exiting:
            return
        self._is_exiting = True
        prefix = self.profile.log_prefix
        self.ctx.logger.info(self.profile.shutdown_start_message)
        try:
            self.ctx.telegram.stop()
        except Exception as exc:
            self.ctx.logger.debug("%s텔레그램 종료 예외: %s", prefix, exc)
        try:
            self.ctx.ws_client.stop()
        except Exception as exc:
            self.ctx.logger.debug("%sWebSocket 종료 예외: %s", prefix, exc)
        if self.ctx.private_ws:
            try:
                self.ctx.private_ws.stop()
            except Exception as exc:
                self.ctx.logger.debug("%sPrivate WS 종료 예외: %s", prefix, exc)
        if self._web_server:
            try:
                self._web_server.stop()
            except Exception as exc:
                self.ctx.logger.debug("%s웹 대시보드 종료 예외: %s", prefix, exc)
        if self._scheduler:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception as exc:
                self.ctx.logger.debug("%s스케줄러 종료 예외: %s", prefix, exc)
        try:
            scope = getattr(self.profile, "exchange_key", "bithumb").lower()
            MarketIntelligenceService.get_instance(exchange_scope=scope).stop_periodic_updater()
        except Exception as exc:
            self.ctx.logger.debug("%s정기 시장 분석 스레드 종료 예외: %s", prefix, exc)
        try:
            from db_manager import dispose_all_db_managers
            dispose_all_db_managers()
        except Exception as exc:
            self.ctx.logger.debug("%s데이터베이스 해제 예외: %s", prefix, exc)
        self.ctx.logger.info(self.profile.shutdown_complete_message)
        if sig is not None or not getattr(sys, "is_finalizing", lambda: False)():
            sys.exit(0)

    def _main_loop(self) -> None:
        last_hb_ts = 0.0
        prefix = self.profile.log_prefix
        while True:
            try:
                now_ts = time.time()
                # WebSocket 수신 콜백은 큐만 적재하므로 주문·파일 작업은 메인 스레드에서 직렬화한다.
                drained = self.ctx.ws_client.drain_callbacks()
                if self.ctx.private_ws is not None:
                    self.ctx.private_ws.drain_order_events()
                    if self.ctx.reconcile_after_private_ws is not None:
                        try:
                            self.ctx.reconcile_after_private_ws()
                        except Exception as exc:
                            self.ctx.logger.debug(
                                "%sPrivate WebSocket 큐 복구 REST 대사 예외: %s",
                                prefix,
                                exc,
                            )
                if now_ts - last_hb_ts >= 15.0:
                    self.ctx.update_heartbeat()
                    last_hb_ts = now_ts
                # 큐에 대기 중인 작업이 많았을 경우 즉시 추가 소화하고, 비어있을 때는 짧게 대기(50ms)하여 CPU 과열 방지 및 실시간성 확보
                drained_count = 0
                try:
                    drained_count = int(drained) if drained is not None else 0
                except (TypeError, ValueError):
                    drained_count = 0
                if drained_count < 200:
                    time.sleep(0.05)
            except (KeyboardInterrupt, SystemExit):
                self._handle_exit(None, None)
            except Exception as exc:
                if not self.profile.catch_main_loop_exceptions:
                    raise
                self.ctx.logger.error(
                    "%s메인 루프 예외 발생: %s",
                    prefix,
                    exc,
                    exc_info=True,
                )
                time.sleep(1)


def execute_daily_morning_report_shared(
    *,
    exchange_name: str,
    create_exchange_client: Callable[[], Any],
    risk_manager: Any,
    telegram: Any,
    logger: Any,
    build_analyzer: Callable[[], Any | None],
    web_port: int,
    now_str: str | None = None,
    calculate_total_equity: Callable[[dict[str, Any], Any], float] | None = None,
    get_held_markets: Callable[[dict[str, Any], Any], list[str]] | None = None,
    get_fear_and_greed_index: Callable[[], dict[str, Any]] | None = None,
) -> None:
    """빗썸/업비트 공통 매일 아침 09:00 KST 일일 결산 모닝 리포트를 생성 및 전송한다."""
    from risk_manager import (
        calculate_total_equity as _default_calc_equity,
        get_fear_and_greed_index as _default_fng,
        get_held_markets as _default_held_markets,
        get_kst_now_str as _default_kst_now,
    )

    if now_str is None:
        now_str = _default_kst_now()
    if calculate_total_equity is None:
        calculate_total_equity = _default_calc_equity
    if get_held_markets is None:
        get_held_markets = _default_held_markets
    if get_fear_and_greed_index is None:
        get_fear_and_greed_index = _default_fng

    logger.info("📊 [%s 아침 9시 일일 결산 브리핑 발송: %s]", exchange_name, now_str)

    try:
        exchange_client = create_exchange_client()
        fng = get_fear_and_greed_index()
        balances = exchange_client.get_balances()
        total_equity = calculate_total_equity(balances, exchange_client)
        krw_avail = balances.get("KRW", {}).get("balance", 0.0)

        daily_pnl_krw = total_equity - risk_manager.daily_start_equity
        daily_pnl_pct = (
            (daily_pnl_krw / risk_manager.daily_start_equity) * 100.0
            if risk_manager.daily_start_equity > 0
            else 0.0
        )

        held_markets = get_held_markets(balances, exchange_client)
        held_names = [f"{exchange_client.get_korean_name(m)}({m.split('-')[-1]})" for m in held_markets]
        held_desc = ", ".join(held_names) if held_names else "없음 (100% 현금 보유)"

        ai_briefing = ""
        analyzer = build_analyzer()
        if analyzer is not None and hasattr(analyzer, "generate_market_briefing"):
            try:
                candles_1h = exchange_client.get_candles(unit=60, count=30, market="KRW-BTC")
                macro_diag = analyzer.diagnose_macro_regime(candles_1h, fng_index=fng)
                ai_comment = analyzer.generate_market_briefing(
                    exchange_name=exchange_name,
                    total_equity=total_equity,
                    daily_pnl_krw=daily_pnl_krw,
                    daily_pnl_pct=daily_pnl_pct,
                    held_positions_desc=held_desc,
                    macro_diag=macro_diag,
                    fng_desc=fng.get("desc", ""),
                )
                if ai_comment:
                    ai_briefing = f"\n\n🤖 <b>[{exchange_name} Gemini AI 종합 시황 브리핑]</b>\n{ai_comment}"
            except Exception as e:
                logger.debug("%s AI 브리핑 생성 예외: %s", exchange_name, e)

        telegram.send_message(
            f"🌅 <b>[{exchange_name} AI 퀀트 봇 - 09:00 KST 일일 성과 결산 브리핑]</b>\n\n"
            f"• <b>총 평가 자산:</b> {total_equity:,.0f} KRW\n"
            f"• <b>금일 자산 변동:</b> {daily_pnl_krw:+,.0f} KRW ({daily_pnl_pct:+.2f}%)\n"
            f"• <b>금일 확정 실현 손익:</b> {risk_manager.realized_pnl_krw:+,.0f} KRW (총 {risk_manager.total_trades_today}회 거래)\n"
            f"• <b>가용 원화 잔고:</b> {krw_avail:,.0f} KRW\n"
            f"• <b>현재 보유 포지션:</b> {held_desc}\n"
            f"• <b>크립토 공포/탐욕 지수:</b> {fng['desc']}\n"
            f"• <b>손익 집계 기준:</b> KST 자정(00:00) 리셋 기준\n"
            f"• <b>웹 대시보드:</b> <code>http://localhost:{web_port}</code>\n"
            f"• <b>기준 일시:</b> {now_str}"
            f"{ai_briefing}"
        )
    except Exception as e:
        logger.error("%s 모닝 리포트 발송 실패: %s", exchange_name, e)


def create_exchange_screener_shared(
    exchange_name: str,
    exchange: Any,
    *,
    get_env_setting: Callable[..., Any],
    min_change_rate_early: float,
    max_candidates_early: int,
) -> Any:
    """사이클마다 최신 env를 반영한 거래소별 스크리너를 생성한다."""
    from market_screener import MarketScreener

    is_momentum_enabled = get_env_setting(
        exchange_name, "MOMENTUM_BREAKOUT_ENABLED", default=True, type_cast=bool,
    )
    return MarketScreener(
        exchange,
        min_trade_value_krw=float(get_env_setting(exchange_name, "MIN_TRADE_VALUE", 1000000000, type_cast=float)),
        min_change_rate=float(get_env_setting(exchange_name, "MIN_CHANGE_RATE", 0.005, type_cast=float)),
        max_change_rate=float(get_env_setting(exchange_name, "MAX_CHANGE_RATE", 0.25, type_cast=float)),
        enable_early_breakout=is_momentum_enabled,
        early_breakout_min_change_rate=min_change_rate_early,
        early_breakout_max_candidates=max_candidates_early,
    )

