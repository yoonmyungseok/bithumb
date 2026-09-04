import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from trading_bot_bootstrap import (
    ExchangeBootstrapProfile,
    TradingBootstrapContext,
    TradingBotBootstrap,
)


def _make_profile(**overrides) -> ExchangeBootstrapProfile:
    defaults = {
        "exchange_key": "bithumb",
        "migration_base_dir": "/tmp/data",
        "data_dir": "/tmp/data",
        "heartbeat_bot_name": "bithumb",
        "internal_port_env_key": "BITHUMB_INTERNAL_PORT",
        "internal_port_default": 17979,
        "internal_api_title": "Bithumb Trading Core API",
        "scheduler_cycle_job_id": "run_trading_cycle",
        "scheduler_morning_job_id": "morning_daily_report",
        "startup_banner_lines": ("  Bithumb AI Pro Quant Trading Bot v5.0 가동 시작",),
        "shutdown_start_message": "shutdown start",
        "shutdown_complete_message": "shutdown complete",
    }
    defaults.update(overrides)
    return ExchangeBootstrapProfile(**defaults)


def _make_context(**overrides) -> TradingBootstrapContext:
    latest_strategies = overrides.pop("latest_strategies", {})
    defaults = {
        "logger": MagicMock(),
        "telegram": MagicMock(),
        "bot_controller": MagicMock(),
        "ws_client": MagicMock(),
        "private_ws": MagicMock(),
        "strategy_cache_manager": MagicMock(),
        "latest_strategies": latest_strategies,
        "interval_minutes": 5,
        "create_exchange_client": MagicMock(),
        "get_held_markets": MagicMock(return_value=["KRW-BTC"]),
        "run_cycle": MagicMock(),
        "send_daily_morning_report": MagicMock(),
        "update_heartbeat": MagicMock(),
    }
    defaults.update(overrides)
    return TradingBootstrapContext(**defaults)


class TradingBotBootstrapTests(unittest.TestCase):
    @patch("trading_bot_bootstrap.TradingBotBootstrap._main_loop")
    @patch("trading_bot_bootstrap.TradingBotBootstrap._register_shutdown_handlers")
    @patch("trading_bot_bootstrap.BackgroundScheduler")
    @patch("trading_bot_bootstrap.DashboardWebServer")
    @patch("trading_bot_bootstrap.migrate_legacy_json_to_sqlite")
    def test_strategy_cache_restore_updates_latest(
        self,
        mock_migrate,
        mock_web_server_cls,
        mock_scheduler_cls,
        mock_register_shutdown,
        mock_main_loop,
    ):
        cached = {"KRW-BTC": {"signal": "BUY", "price": 100.0}}
        latest = {}
        cache_mgr = MagicMock()
        cache_mgr.get_valid_strategies.return_value = (cached, 60.0, True)
        controller = MagicMock()
        exchange = MagicMock()
        exchange.get_balances.return_value = {"KRW": {"balance": 1000.0}}

        bootstrap = TradingBotBootstrap(
            _make_profile(),
            _make_context(
                strategy_cache_manager=cache_mgr,
                latest_strategies=latest,
                bot_controller=controller,
                create_exchange_client=MagicMock(return_value=exchange),
            ),
        )

        bootstrap.run()

        self.assertEqual(latest, cached)
        controller.restore_missing_position_strategies.assert_called_once_with(["KRW-BTC"])
        mock_main_loop.assert_called_once()

    @patch("trading_bot_bootstrap.TradingBotBootstrap._main_loop")
    @patch("trading_bot_bootstrap.TradingBotBootstrap._register_shutdown_handlers")
    @patch("trading_bot_bootstrap.BackgroundScheduler")
    @patch("trading_bot_bootstrap.DashboardWebServer")
    @patch("trading_bot_bootstrap.migrate_legacy_json_to_sqlite")
    def test_smart_cache_defers_first_run(
        self,
        mock_migrate,
        mock_web_server_cls,
        mock_scheduler_cls,
        mock_register_shutdown,
        mock_main_loop,
    ):
        scheduler = MagicMock()
        mock_scheduler_cls.return_value = scheduler
        cache_mgr = MagicMock()
        cache_mgr.get_valid_strategies.return_value = ({"KRW-ETH": {}}, 120.0, True)
        run_cycle = MagicMock()

        bootstrap = TradingBotBootstrap(
            _make_profile(),
            _make_context(strategy_cache_manager=cache_mgr, run_cycle=run_cycle),
        )
        bootstrap.run()

        run_cycle.assert_not_called()
        cycle_job_call = scheduler.add_job.call_args_list[0]
        self.assertEqual(cycle_job_call.args[0], run_cycle)
        self.assertIn("next_run_time", cycle_job_call.kwargs)

    @patch("trading_bot_bootstrap.sys.exit")
    @patch("trading_bot_bootstrap.signal.signal")
    def test_shutdown_stops_resources_in_order(self, mock_signal, mock_exit):
        telegram = MagicMock()
        ws_client = MagicMock()
        private_ws = MagicMock()
        web_server = MagicMock()
        scheduler = MagicMock()
        bootstrap = TradingBotBootstrap(
            _make_profile(),
            _make_context(
                telegram=telegram,
                ws_client=ws_client,
                private_ws=private_ws,
            ),
        )
        bootstrap._web_server = web_server
        bootstrap._scheduler = scheduler

        bootstrap._handle_exit()

        telegram.stop.assert_called_once()
        ws_client.stop.assert_called_once()
        private_ws.stop.assert_called_once()
        web_server.stop.assert_called_once()
        scheduler.shutdown.assert_called_once_with(wait=False)
        mock_exit.assert_called_once_with(0)

    @patch("trading_bot_bootstrap.TradingBotBootstrap._main_loop")
    @patch("trading_bot_bootstrap.TradingBotBootstrap._register_shutdown_handlers")
    @patch("trading_bot_bootstrap.BackgroundScheduler")
    @patch("trading_bot_bootstrap.DashboardWebServer")
    @patch("trading_bot_bootstrap.migrate_legacy_json_to_sqlite")
    def test_heartbeat_written_on_start(
        self,
        mock_migrate,
        mock_web_server_cls,
        mock_scheduler_cls,
        mock_register_shutdown,
        mock_main_loop,
    ):
        update_heartbeat = MagicMock()
        cache_mgr = MagicMock()
        cache_mgr.get_valid_strategies.return_value = ({}, 0.0, False)

        bootstrap = TradingBotBootstrap(
            _make_profile(),
            _make_context(
                strategy_cache_manager=cache_mgr,
                update_heartbeat=update_heartbeat,
            ),
        )
        bootstrap.run()

        update_heartbeat.assert_called_once()

    @patch("trading_bot_bootstrap.time.sleep", side_effect=KeyboardInterrupt)
    @patch("trading_bot_bootstrap.time.time", return_value=1000.0)
    def test_main_loop_drains_ws_queues(self, mock_time, mock_sleep):
        ws_client = MagicMock()
        private_ws = MagicMock()
        update_heartbeat = MagicMock()
        bootstrap = TradingBotBootstrap(
            _make_profile(),
            _make_context(
                ws_client=ws_client,
                private_ws=private_ws,
                update_heartbeat=update_heartbeat,
            ),
        )

        with self.assertRaises(SystemExit):
            bootstrap._main_loop()

        ws_client.drain_callbacks.assert_called()
        private_ws.drain_order_events.assert_called()
        update_heartbeat.assert_called_once()

    @patch("trading_bot_bootstrap.TradingBotBootstrap._main_loop")
    @patch("trading_bot_bootstrap.TradingBotBootstrap._register_shutdown_handlers")
    @patch("trading_bot_bootstrap.BackgroundScheduler")
    @patch("trading_bot_bootstrap.DashboardWebServer")
    @patch("trading_bot_bootstrap.migrate_legacy_json_to_sqlite")
    def test_cycle_offset_defers_initial_run(
        self,
        mock_migrate,
        mock_web_server_cls,
        mock_scheduler_cls,
        mock_register_shutdown,
        mock_main_loop,
    ):
        scheduler = MagicMock()
        mock_scheduler_cls.return_value = scheduler
        cache_mgr = MagicMock()
        # 캐시 없음 -> should_run_immediate = True
        cache_mgr.get_valid_strategies.return_value = ({}, 0.0, False)
        run_cycle = MagicMock()

        bootstrap = TradingBotBootstrap(
            _make_profile(),
            _make_context(
                strategy_cache_manager=cache_mgr,
                run_cycle=run_cycle,
                cycle_offset_seconds=150,
            ),
        )
        bootstrap.run()

        # 오프셋이 설정되어 있으므로 초기 즉시 실행을 건너뛰어야 함
        run_cycle.assert_not_called()
        cycle_job_call = scheduler.add_job.call_args_list[0]
        self.assertEqual(cycle_job_call.args[0], run_cycle)
        self.assertIn("next_run_time", cycle_job_call.kwargs)

    @patch("trading_bot_bootstrap.TradingBotBootstrap._main_loop")
    @patch("trading_bot_bootstrap.TradingBotBootstrap._register_shutdown_handlers")
    @patch("trading_bot_bootstrap.BackgroundScheduler")
    @patch("trading_bot_bootstrap.DashboardWebServer")
    @patch("trading_bot_bootstrap.migrate_legacy_json_to_sqlite")
    def test_cycle_offset_zero_runs_immediately(
        self,
        mock_migrate,
        mock_web_server_cls,
        mock_scheduler_cls,
        mock_register_shutdown,
        mock_main_loop,
    ):
        scheduler = MagicMock()
        mock_scheduler_cls.return_value = scheduler
        cache_mgr = MagicMock()
        # 캐시 없음 -> should_run_immediate = True
        cache_mgr.get_valid_strategies.return_value = ({}, 0.0, False)
        run_cycle = MagicMock()

        bootstrap = TradingBotBootstrap(
            _make_profile(),
            _make_context(
                strategy_cache_manager=cache_mgr,
                run_cycle=run_cycle,
                cycle_offset_seconds=0,
            ),
        )
        bootstrap.run()

        # 오프셋이 0이므로 초기 즉시 실행이 1회 호출되어야 함
        run_cycle.assert_called_once()

    @patch("trading_bot_bootstrap.TradingBotBootstrap._main_loop")
    @patch("trading_bot_bootstrap.TradingBotBootstrap._register_shutdown_handlers")
    @patch("trading_bot_bootstrap.BackgroundScheduler")
    @patch("trading_bot_bootstrap.DashboardWebServer")
    @patch("trading_bot_bootstrap.migrate_legacy_json_to_sqlite")
    def test_cycle_offset_first_run_time_calculation(
        self,
        mock_migrate,
        mock_web_server_cls,
        mock_scheduler_cls,
        mock_register_shutdown,
        mock_main_loop,
    ):
        from datetime import datetime
        scheduler = MagicMock()
        mock_scheduler_cls.return_value = scheduler
        cache_mgr = MagicMock()
        cache_mgr.get_valid_strategies.return_value = ({}, 0.0, False)

        before = datetime.now()
        bootstrap = TradingBotBootstrap(
            _make_profile(),
            _make_context(
                strategy_cache_manager=cache_mgr,
                cycle_offset_seconds=150,
            ),
        )
        bootstrap.run()
        after = datetime.now()

        cycle_job_call = scheduler.add_job.call_args_list[0]
        next_run = cycle_job_call.kwargs.get("next_run_time")
        self.assertIsNotNone(next_run)
        # next_run_time은 호출 시점의 now() + 150초 사이여야 함
        self.assertGreaterEqual((next_run - before).total_seconds(), 149.0)
        self.assertLessEqual((next_run - after).total_seconds(), 151.0)


if __name__ == "__main__":
    unittest.main()
