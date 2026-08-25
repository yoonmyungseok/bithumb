"""
Architecture and System Stability Tests (Self-Healing, Thread Safety, Heartbeat, Lifecycle)
"""

import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from order_safety import (
    CooldownManager,
    OrderJournal,
    load_json_with_backup_recovery,
    write_json_atomically,
)
from risk_manager import DailyRiskManager, TrailingStopTracker
from telegram_alert import TelegramAlert
from trade_memory import TradeMemoryManager


class ArchitectureStabilityTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_arch_stability_")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_atomic_write_and_backup_creation(self):
        """write_json_atomically가 주 파일과 .bak 파일을 모두 원자적으로 생성하는지 검증"""
        path = os.path.join(self.test_dir, "state.json")
        backup_path = f"{path}.bak"
        data = {"count": 42, "status": "OK", "items": ["a", "b", "c"]}

        write_json_atomically(path, data)

        self.assertTrue(os.path.exists(path))
        self.assertTrue(os.path.exists(backup_path))

        loaded = load_json_with_backup_recovery(path)
        self.assertEqual(loaded, data)

    def test_load_json_with_backup_recovery_on_corrupt_file(self):
        """주 파일이 깨졌을 때 .bak 백업에서 자가 복구하고 주 파일을 복원하는지 검증"""
        path = os.path.join(self.test_dir, "daily_stats.json")
        backup_path = f"{path}.bak"
        valid_data = {"date": "2026-08-25", "start_equity": 1000000.0, "realized_pnl_krw": 50000.0}

        # 정상 데이터 기록
        write_json_atomically(path, valid_data)

        # 주 파일을 비정상 파손 (Corrupted JSON) 상태로 변조
        with open(path, "w", encoding="utf-8") as f:
            f.write("{{CORRUPTED_JSON_TRUNCATED_...")

        # 로드 시도 -> .bak에서 자동 복구되어 정상 데이터 반환
        recovered = load_json_with_backup_recovery(path)
        self.assertEqual(recovered, valid_data)

        # 복구 후 주 파일도 다시 올바르게 고쳐졌는지 확인
        with open(path, "r", encoding="utf-8") as f:
            import json
            re_saved = json.load(f)
        self.assertEqual(re_saved, valid_data)

    def test_load_json_with_backup_recovery_missing_main_file(self):
        """주 파일이 유실되고 .bak만 존재할 때 자동 복원 검증"""
        path = os.path.join(self.test_dir, "missing_main.json")
        backup_path = f"{path}.bak"
        valid_data = {"orders": [{"id": 1}, {"id": 2}]}

        # .bak 파일만 직접 생성
        with open(backup_path, "w", encoding="utf-8") as bf:
            import json
            json.dump(valid_data, bf)

        self.assertFalse(os.path.exists(path))
        recovered = load_json_with_backup_recovery(path)
        self.assertEqual(recovered, valid_data)
        self.assertTrue(os.path.exists(path))

    def test_telegram_alert_stop_lifecycle(self):
        """TelegramAlert.stop() 호출 시 스레드가 안전하게 종료되는지 검증"""
        alert = TelegramAlert(bot_token="fake_token", chat_id="123456", enable_async=True)
        self.assertTrue(alert._is_running)
        if alert._worker_thread:
            self.assertTrue(alert._worker_thread.is_alive())

        alert.stop(timeout=1.0)
        self.assertFalse(alert._is_running)
        if alert._worker_thread:
            self.assertFalse(alert._worker_thread.is_alive())

    def test_thread_safety_daily_risk_manager_concurrent_access(self):
        """DailyRiskManager 멀티스레드 동시 접근 시 RLock 보호 및 무결성 검증"""
        drm = DailyRiskManager(max_loss_pct=0.05, data_dir=self.test_dir)
        import datetime
        now = datetime.datetime(2026, 8, 25, 12, 0, 0)
        drm.update_daily_equity(1000000.0, now)

        num_threads = 10
        trades_per_thread = 20

        def worker(thread_idx: int):
            for i in range(trades_per_thread):
                is_win = (thread_idx + i) % 2 == 0
                pnl = 1000.0 if is_win else -500.0
                drm.add_realized_trade(pnl, is_win=is_win)
                time.sleep(0.001)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(drm.total_trades_today, num_threads * trades_per_thread)

    def test_thread_safety_trailing_stop_tracker_concurrent_access(self):
        """TrailingStopTracker 멀티스레드 동시 조작 시 락 안전성 검증"""
        tracker = TrailingStopTracker(start_profit_pct=0.02, trailing_drop_pct=0.012, data_dir=self.test_dir)
        markets = [f"KRW-COIN{i}" for i in range(5)]

        def worker(m: str):
            for step in range(30):
                price = 1000.0 + (step * 10)
                tracker.check_position(m, price, 1000.0)
                if tracker.acquire_exit_lock(m):
                    time.sleep(0.001)
                    tracker.release_exit_lock(m)
                time.sleep(0.001)

        threads = [threading.Thread(target=worker, args=(m,)) for m in markets]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for m in markets:
            self.assertFalse(tracker.is_exiting(m))

    def test_thread_safety_trade_memory_concurrent_access(self):
        """TradeMemoryManager 멀티스레드 동시 기록 시 무결성 검증"""
        tm = TradeMemoryManager(data_dir=self.test_dir)
        num_threads = 5
        records_per_thread = 10

        def worker(thread_id: int):
            for i in range(records_per_thread):
                tm.record_completed_trade(
                    market=f"KRW-TEST{thread_id}",
                    side="PARTIAL_TP",
                    entry_price=1000.0,
                    exit_price=1030.0,
                    pnl_pct=3.0,
                    pnl_krw=3000.0,
                    reason="PROFIT_TAKE",
                    timestamp="2026-08-25 12:00:00",
                )
                time.sleep(0.001)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        stats = tm.get_position_level_stats()
        self.assertGreater(stats["total_positions"], 0)

    def test_heartbeat_creation_and_recovery(self):
        """하트비트 파일 원자적 생성 및 파싱 검증"""
        hb_path = os.path.join(self.test_dir, ".heartbeat")
        payload = {
            "timestamp": time.time(),
            "pid": 99999,
            "datetime": "2026-08-25 12:00:00",
            "status": "RUNNING",
            "bot": "bithumb",
        }
        write_json_atomically(hb_path, payload)
        self.assertTrue(os.path.exists(hb_path))
        loaded = load_json_with_backup_recovery(hb_path)
        self.assertEqual(loaded["pid"], 99999)
        self.assertEqual(loaded["status"], "RUNNING")


if __name__ == "__main__":
    unittest.main()
