"""DB 동시성 경합 방어, 언락 대기 및 graceful dispose 회귀 테스트."""

import os
import sqlite3
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import db_manager
import process_manager


class DbConcurrencyResilienceTests(unittest.TestCase):
    """프로세스 재시작 시 DB 락 해제 대기 및 연결 재시도 복원력 검증"""

    def test_wait_for_db_unlocked_immediate_success(self):
        """DB 파일이 존재하고 즉시 쿼리가 가능할 때 True를 반환한다."""
        with patch("process_manager.os.path.exists", return_value=True), \
             patch("sqlite3.connect") as mock_conn:
            mock_cursor = MagicMock()
            mock_conn.return_value.execute = mock_cursor
            result = process_manager._wait_for_db_unlocked("bithumb", timeout=2.0)
            self.assertTrue(result)
            mock_cursor.assert_called_with("PRAGMA schema_version;")

    def test_wait_for_db_unlocked_retries_and_succeeds(self):
        """이전 프로세스의 잔여 락으로 인한 disk I/O error 발생 시 재시도 후 성공한다."""
        with patch("process_manager.os.path.exists", return_value=True), \
             patch("sqlite3.connect") as mock_conn:
            succeed_conn = MagicMock()
            mock_conn.side_effect = [
                sqlite3.OperationalError("disk I/O error"),
                sqlite3.OperationalError("disk I/O error"),
                succeed_conn,
            ]
            result = process_manager._wait_for_db_unlocked("upbit", timeout=3.0)
            self.assertTrue(result)
            self.assertEqual(mock_conn.call_count, 3)

    def test_wait_for_db_unlocked_timeout(self):
        """타임아웃 동안 락이 풀리지 않으면 False를 반환한다."""
        with patch("process_manager.os.path.exists", return_value=True), \
             patch("sqlite3.connect", side_effect=sqlite3.OperationalError("disk I/O error")):
            result = process_manager._wait_for_db_unlocked("bithumb", timeout=0.3)
            self.assertFalse(result)

    def test_db_manager_get_connection_retries_on_disk_io_error(self):
        """DatabaseManager._get_connection이 disk I/O error 시 최대 14회까지 재시도 후 복구된다."""
        dummy_path = os.path.abspath("data/test_retry.db")
        manager = db_manager.DatabaseManager.__new__(db_manager.DatabaseManager)
        manager.db_path = dummy_path
        manager._local = MagicMock()
        manager._local.conn = None
        manager._conns_lock = MagicMock()
        manager._all_conns = set()

        real_conn = MagicMock()
        real_conn.execute.return_value.fetchone.return_value = ("wal",)

        with patch("sqlite3.connect") as mock_connect, \
             patch("time.sleep"):
            # 3회 OperationalError 후 성공
            mock_connect.side_effect = [
                sqlite3.OperationalError("disk I/O error"),
                sqlite3.OperationalError("disk I/O error"),
                sqlite3.OperationalError("disk I/O error"),
                real_conn,
            ]
            conn = manager._get_connection()
            self.assertEqual(conn, real_conn)
            self.assertEqual(mock_connect.call_count, 4)

    def test_dispose_all_db_managers_and_reset_cache(self):
        """dispose_all_db_managers 및 reset_db_manager_cache가 모든 등록된 DB 매니저를 정리한다."""
        mock_mgr1 = MagicMock()
        mock_mgr2 = MagicMock()
        with db_manager._DB_LOCK:
            db_manager._DB_MANAGER_BY_PATH["path1"] = mock_mgr1
            db_manager._DB_MANAGER_BY_PATH["path2"] = mock_mgr2

        db_manager.dispose_all_db_managers()

        mock_mgr1.dispose.assert_called_once()
        mock_mgr2.dispose.assert_called_once()
        self.assertEqual(len(db_manager._DB_MANAGER_BY_PATH), 0)

    def test_ai_entry_safety_ttl_auto_unblock(self):
        """일시적 Gemini 오류로 인한 신규 BUY 차단은 TTL 경과 후 자동 만료되어 다음 사이클 복구를 허용한다."""
        from ai_provider import AIProviderTelemetry
        # 1. 일시적 통신 오류 기록 시 즉시 차단
        AIProviderTelemetry.record_entry_safety("bithumb", blocked=True, reason="exception", context="KRW-SUI")
        self.assertTrue(AIProviderTelemetry.get_entry_block_reason("bithumb"))

        # 2. TTL(0.01초 지정) 경과 후 조회 시 자동 만료 및 차단 해제
        import time
        time.sleep(0.02)
        reason_after_ttl = AIProviderTelemetry.get_entry_block_reason("bithumb", max_age_sec=0.01)
        self.assertEqual(reason_after_ttl, "")


if __name__ == "__main__":
    unittest.main()
