"""Shared process bootstrap for dual-exchange trading entry points.

Telegram listener, internal API, WebSocket lifecycle, strategy cache restore,
APScheduler, graceful shutdown, and the main heartbeat loop are centralized here.
"""

from __future__ import annotations

import os
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from apscheduler.schedulers.background import BackgroundScheduler

from db_manager import migrate_legacy_json_to_sqlite
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
        should_run_immediate = self._restore_strategy_cache()
        self._start_scheduler(should_run_immediate)
        self._run_initial_cycle_if_needed(should_run_immediate)
        self._register_shutdown_handlers()
        self._main_loop()

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
        if not should_run_immediate:
            return
        try:
            self.ctx.run_cycle()
        except Exception as exc:
            self.ctx.logger.error("초기 사이클 실행 중 오류: %s", exc)

    def _register_shutdown_handlers(self) -> None:
        signal.signal(signal.SIGINT, self._handle_exit)
        signal.signal(signal.SIGTERM, self._handle_exit)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, signal.SIG_IGN)

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
        self.ctx.logger.info(self.profile.shutdown_complete_message)
        sys.exit(0)

    def _main_loop(self) -> None:
        last_hb_ts = 0.0
        prefix = self.profile.log_prefix
        while True:
            try:
                now_ts = time.time()
                # WebSocket 수신 콜백은 큐만 적재하므로 주문·파일 작업은 메인 스레드에서 직렬화한다.
                self.ctx.ws_client.drain_callbacks()
                if self.ctx.private_ws is not None:
                    self.ctx.private_ws.drain_order_events()
                if now_ts - last_hb_ts >= 15.0:
                    self.ctx.update_heartbeat()
                    last_hb_ts = now_ts
                time.sleep(1)
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
