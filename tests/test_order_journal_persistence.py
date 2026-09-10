"""Unit and integration tests for OrderJournal persistence verification and fail-closed safety."""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from order_safety.journal import (
    MAX_RECONCILIATION_STALENESS_SEC,
    OrderJournal,
)
from order_safety.types import OrderStatus


class TestOrderJournalPersistence(unittest.TestCase):
    """주문 저널 디스크 영속화 검증 및 안전망 테스트."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="journal_test_")
        self.bithumb_dir = os.path.join(self.temp_dir, "bithumb")
        self.upbit_dir = os.path.join(self.temp_dir, "upbit")
        os.makedirs(self.bithumb_dir, exist_ok=True)
        os.makedirs(self.upbit_dir, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_zero_update_reconciliation_persists_last_completed_at(self) -> None:
        """갱신 대상 주문이 0건인 정상 REST 대사도 last_completed_at이 JSON에 저장되고 새 인스턴스에서 복원된다."""
        journal = OrderJournal(data_dir=self.bithumb_dir, exchange_scope="bithumb")
        self.assertEqual(journal.reconciliation_metrics.get("last_completed_at"), 0.0)

        before_reconcile = time.time()
        # 갱신할 주문이 없으므로 get_order는 호출되지 않음
        updated = journal.reconcile_exchange_statuses(get_order=lambda uuid: {})
        self.assertEqual(updated, 0)
        completed_at = journal.reconciliation_metrics.get("last_completed_at", 0.0)
        self.assertGreaterEqual(completed_at, before_reconcile)

        # JSON 파일에서 직접 확인
        with open(journal.path, "r", encoding="utf-8") as f:
            disk_data = json.load(f)
        self.assertIn("reconciliation_metrics", disk_data)
        self.assertAlmostEqual(
            disk_data["reconciliation_metrics"]["last_completed_at"], completed_at, delta=1e-3
        )

        # 새 OrderJournal 인스턴스로 다시 로드했을 때 복원 확인
        reloaded_journal = OrderJournal(data_dir=self.bithumb_dir, exchange_scope="bithumb")
        self.assertAlmostEqual(
            reloaded_journal.reconciliation_metrics["last_completed_at"], completed_at, delta=1e-3
        )

    def test_persistence_failure_blocks_new_buy(self) -> None:
        """JSON 저장 또는 저장 후 재읽기 검증 실패 시 신규 BUY가 차단(PENDING)된다."""
        journal = OrderJournal(data_dir=self.bithumb_dir, exchange_scope="bithumb")

        # 정상 대사 및 영속화로 READY 상태로 만들기
        journal.reconcile_exchange_statuses(get_order=lambda uuid: {})
        ready_ok = journal.complete_reconciliation_if_safe()
        self.assertTrue(ready_ok)
        self.assertTrue(journal.is_entry_ready())

        # 1) _save가 실패(예: 쓰기 권한 오류 또는 검증 실패)하도록 모의
        with patch.object(journal, "_save", return_value=False):
            journal.reconcile_exchange_statuses(get_order=lambda uuid: {})
            # 영속화 실패로 PENDING 강제 및 실패 횟수 증가
            self.assertEqual(journal.reconciliation_state, "PENDING")
            self.assertFalse(journal.is_entry_ready())

            # complete_reconciliation_if_safe도 차단
            safe = journal.complete_reconciliation_if_safe()
            self.assertFalse(safe)
            self.assertFalse(journal.is_entry_ready())

    def test_reconciliation_complete_fails_if_save_verification_fails(self) -> None:
        """complete_reconciliation_if_safe 전환 시 저널 저장이 실패하면 READY로 전환되지 않고 PENDING을 유지한다."""
        journal = OrderJournal(data_dir=self.bithumb_dir, exchange_scope="bithumb")
        journal.reconciliation_state = "PENDING"
        journal._last_reconcile_failed_count = 0

        # _save 호출 시 False 반환 (디스크 검증 실패)
        with patch.object(journal, "_save", return_value=False):
            result = journal.complete_reconciliation_if_safe()
            self.assertFalse(result)
            self.assertEqual(journal.reconciliation_state, "PENDING")
            self.assertFalse(journal.is_entry_ready())

    def test_exchange_isolation_paths_and_scopes(self) -> None:
        """빗썸과 업비트 저널의 경로와 거래소 범위가 완전히 격리된다."""
        b_journal = OrderJournal(data_dir=self.bithumb_dir, exchange_scope="bithumb")
        u_journal = OrderJournal(data_dir=self.upbit_dir, exchange_scope="upbit")

        self.assertNotEqual(os.path.abspath(b_journal.path), os.path.abspath(u_journal.path))
        self.assertEqual(b_journal.exchange_scope, "bithumb")
        self.assertEqual(u_journal.exchange_scope, "upbit")

        # 빗썸에 주문 추가
        b_id = b_journal.record_intent("KRW-BTC", "buy", 0.1, 100000000.0, "limit", exchange="bithumb")
        # 업비트에 주문 추가
        u_id = u_journal.record_intent("KRW-ETH", "buy", 1.0, 4000000.0, "limit", exchange="upbit")

        # 빗썸 저널에는 빗썸 주문만 있고 업비트 주문은 없음
        self.assertTrue(b_journal.is_managed_order(b_id))
        self.assertFalse(b_journal.is_managed_order(u_id))

        # 업비트 저널에는 업비트 주문만 있고 빗썸 주문은 없음
        self.assertTrue(u_journal.is_managed_order(u_id))
        self.assertFalse(u_journal.is_managed_order(b_id))

    def test_stale_journal_on_startup_forces_pending(self) -> None:
        """오래된 저널(10분 초과)로 시작하면 저장된 값이 READY여도 PENDING으로 강제되며, 대사/저장 성공 후 READY가 된다."""
        journal_path = os.path.join(self.bithumb_dir, "order_journal.json")
        stale_time = time.time() - (MAX_RECONCILIATION_STALENESS_SEC + 120.0)  # 12분 전

        # 디스크에 READY 상태이지만 stale한 last_completed_at을 가진 저널 파일 생성
        stale_payload = {
            "schema_version": 4,
            "updated_at": stale_time,
            "exchange_scope": "bithumb",
            "reconciliation_state": "READY",
            "reconciliation_metrics": {
                "last_started_at": stale_time,
                "last_completed_at": stale_time,
                "last_updated_count": 0,
                "last_failed_count": 0,
            },
            "orders": [],
        }
        with open(journal_path, "w", encoding="utf-8") as f:
            json.dump(stale_payload, f)

        # 새 OrderJournal 인스턴스 생성 (프로세스 재시작 시뮬레이션)
        journal = OrderJournal(data_dir=self.bithumb_dir, exchange_scope="bithumb")

        # 저장된 값은 READY였으나 stale 하므로 PENDING으로 강제 초기화되었어야 함
        self.assertEqual(journal.reconciliation_state, "PENDING")
        self.assertFalse(journal.is_entry_ready())

        # REST 대사 수행
        journal.reconcile_exchange_statuses(get_order=lambda uuid: {})
        # complete_reconciliation_if_safe 호출
        success = journal.complete_reconciliation_if_safe()
        self.assertTrue(success)
        self.assertEqual(journal.reconciliation_state, "READY")
        self.assertTrue(journal.is_entry_ready())

    def test_save_verification_detects_disk_corruption(self) -> None:
        """저장 직후 디스크 내용이 변조되거나 손상된 경우 _save()가 False를 반환한다."""
        journal = OrderJournal(data_dir=self.bithumb_dir, exchange_scope="bithumb")

        # 정상 저장 검증
        self.assertTrue(journal._save())

        # load_json_with_backup_recovery를 모의하여 손상된 데이터를 반환하도록 설정
        with patch("order_safety.journal.load_json_with_backup_recovery", return_value={"corrupted": True}):
            self.assertFalse(journal._save())

        # 스키마 버전 불일치 모의
        with patch("order_safety.journal.load_json_with_backup_recovery", return_value={
            "schema_version": 999,
            "exchange_scope": "bithumb",
            "updated_at": time.time(),
        }):
            self.assertFalse(journal._save())

        # 거래소 불일치 모의
        with patch("order_safety.journal.load_json_with_backup_recovery", return_value={
            "schema_version": journal.SCHEMA_VERSION,
            "exchange_scope": "wrong_exchange",
            "updated_at": time.time(),
        }):
            self.assertFalse(journal._save())


if __name__ == "__main__":
    unittest.main()
