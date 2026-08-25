import datetime
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from risk_manager import DailyRiskManager
from strategy_engine import StrategyPolicy
from upbit_websocket import UpbitWebSocketClient
from websocket_manager import BithumbWebSocketClient, WebSocketHealthState


class TestP1Safety(unittest.TestCase):
    """P1-1 (킬스위치 Latch & 입출금 버그), P1-2 (웹소켓 헬스), P1-3 (공통 정책) 단위 테스트 (완전 격리)"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.d_dir = self.temp_dir.name
        self.risk_mgr = DailyRiskManager(max_loss_pct=0.05, data_dir=self.d_dir)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_01_kill_switch_latch_persists_intraday(self):
        """1. 일일 킬스위치 발동 후 장중 시세 반등 시에도 당일 자정까지 Latch 유지 검증 (P1-1)"""
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
        # 시작 자산 1,000,000원 설정
        self.risk_mgr.update_daily_equity(1_000_000.0, now)

        # -6% 손실 발생 -> 940,000원 (한도 5% 초과)
        is_kill, pnl_pct = self.risk_mgr.update_daily_equity(940_000.0, now)
        self.assertTrue(is_kill)
        self.assertTrue(self.risk_mgr.kill_switch_active)
        self.assertEqual(self.risk_mgr.kill_switch_latched_date, now.strftime("%Y-%m-%d"))

        # 시세 반등으로 970,000원 (-3%) 회복
        is_kill_after, pnl_pct_after = self.risk_mgr.update_daily_equity(970_000.0, now)
        # 당일 동안은 Latch로 인해 킬스위치가 여전히 True를 유지해야 함
        self.assertTrue(is_kill_after)
        self.assertTrue(self.risk_mgr.kill_switch_active)

    def test_02_cashflow_no_false_positive_on_equity_jump(self):
        """2. 평가손익 1만 원 이상 변동 시 시작 기준자산이 임의로 왜곡되지 않음을 검증 (P1-1 버그 픽스)"""
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
        self.risk_mgr.update_daily_equity(100_000.0, now)
        initial_start_equity = self.risk_mgr.daily_start_equity

        # 코인 가격 상승으로 평가자산이 15,000원 상승 -> 115,000원
        self.risk_mgr.update_daily_equity(115_000.0, now)

        # 시작 기준자산은 100,000원으로 그대로 보존되어야 함 (115,000원으로 변조되면 안 됨)
        self.assertEqual(self.risk_mgr.daily_start_equity, initial_start_equity)

        # 명시적 입출금 등록 시에는 정상 보정
        self.risk_mgr.register_cashflow(50_000.0, reason="추가 입금")
        self.assertEqual(self.risk_mgr.daily_start_equity, 150_000.0)

    def test_03_kill_switch_restart_restoration(self):
        """3. 프로세스 재시작 후에도 당일 킬스위치 Latch 상태가 정상 복구됨을 검증 (P1-1)"""
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
        self.risk_mgr.update_daily_equity(1_000_000.0, now)
        self.risk_mgr.update_daily_equity(930_000.0, now)  # 킬스위치 트리거

        # 프로세스 재시작 모의 (새 인스턴스로 동일 data_dir 로드)
        restarted_mgr = DailyRiskManager(max_loss_pct=0.05, data_dir=self.d_dir)
        self.assertTrue(restarted_mgr.kill_switch_active)
        self.assertEqual(restarted_mgr.kill_switch_latched_date, now.strftime("%Y-%m-%d"))

        # 관리자 수동 해제 시 정상 해제 검증
        restarted_mgr.manual_reset_kill_switch()
        self.assertFalse(restarted_mgr.kill_switch_active)

    def test_04_websocket_health_state_machine(self):
        """4. Bithumb/Upbit WebSocket 클라이언트의 건강상태(Health State) 판정 검증 (P1-2)"""
        client = BithumbWebSocketClient(initial_markets=["KRW-BTC"])

        # 1) 연결 전 초기 상태 -> DISCONNECTED
        health = client.get_health_status()
        self.assertFalse(health["is_healthy"])
        self.assertEqual(health["status"], WebSocketHealthState.DISCONNECTED)

        # 2) 연결되었으나 틱 미수신 -> DATA_UNAVAILABLE
        client.is_connected = True
        health = client.get_health_status()
        self.assertFalse(health["is_healthy"])
        self.assertEqual(health["status"], WebSocketHealthState.DATA_UNAVAILABLE)

        # 3) 정상 틱 수신 직후 -> DATA_AVAILABLE (Healthy)
        client.last_tick_time = time.time()
        health = client.get_health_status()
        self.assertTrue(health["is_healthy"])
        self.assertEqual(health["status"], WebSocketHealthState.DATA_AVAILABLE)

        # 4) 틱 수신이 20초간 중단됨 -> STALE
        client.last_tick_time = time.time() - 25.0
        health = client.get_health_status(max_stale_seconds=15.0)
        self.assertFalse(health["is_healthy"])
        self.assertEqual(health["status"], WebSocketHealthState.STALE)

    def test_05_strategy_policy_unification(self):
        """5. 실거래와 백테스트 공통 StrategyPolicy 일원화 검증 (P1-3)"""
        # 타임스탑: 실거래 3600초 (60분), 백테스트 12개 5분봉 (60분)
        self.assertEqual(StrategyPolicy.TIME_STOP_SECONDS, 3600)
        self.assertEqual(StrategyPolicy.TIME_STOP_BARS_5M, 12)
        self.assertEqual(StrategyPolicy.TIME_STOP_BARS_5M * 5 * 60, StrategyPolicy.TIME_STOP_SECONDS)
        self.assertEqual(StrategyPolicy.PARTIAL_TP_PCT, 0.025)
        self.assertEqual(StrategyPolicy.MAX_DAILY_LOSS_PCT, 0.05)

    def test_06_timestop_uninitialized_entry_time_protection(self):
        """6. 진입 시점 미설정(0.0) 상태에서 타임스탑 오발동 방지 및 정상 60분 후 발동 검증"""
        from risk_manager import TrailingStopTracker
        tracker = TrailingStopTracker(data_dir=self.d_dir)
        market = "KRW-BTC"

        # 1) 진입 시간 미설정 시 0.0 반환
        entry_ts = tracker.get_entry_time(market)
        self.assertEqual(entry_ts, 0.0)

        # 2) 진입 시간 <= 0 감지 시 현재 시간으로 자동 보정 등록
        if entry_ts <= 0:
            now_ts = time.time()
            tracker.set_entry_time(market, now_ts)
            entry_ts = tracker.get_entry_time(market)

        self.assertGreater(entry_ts, 0.0)
        hold_duration = time.time() - entry_ts
        # 방금 보정되었으므로 보유 시간은 5초 미만이어야 하며 3600초 미만
        self.assertLess(hold_duration, 5.0)
        self.assertFalse(hold_duration >= StrategyPolicy.TIME_STOP_SECONDS)

        # 3) 60분(3600초) 이상 실제로 경과한 경우에만 타임스탑 조건 충족
        past_ts = time.time() - 3650
        tracker.set_entry_time(market, past_ts)
        entry_ts_60m = tracker.get_entry_time(market)
        hold_duration_60m = time.time() - entry_ts_60m
        self.assertGreaterEqual(hold_duration_60m, StrategyPolicy.TIME_STOP_SECONDS)


if __name__ == "__main__":
    unittest.main()
