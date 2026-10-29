import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from drive_sync import DriveAnalysis24hSyncer
from dashboard_server import UnifiedDashboardServer
from web_server import DashboardWebServer


class DriveSyncTests(unittest.TestCase):
    """구글 드라이브 24h_reports 폴더 동기화 단위 테스트"""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.local_dir = self.tmp_dir.name

    def tearDown(self):
        self.tmp_dir.cleanup()

    @patch("drive_sync.Credentials")
    @patch("drive_sync.urllib.request.urlopen")
    def test_drive_syncer_downloads_and_saves(self, mock_urlopen, mock_creds_cls):
        """드라이브에 있는 .md 파일 및 Google Docs 파일이 로컬로 안전하게 다운로드되는지 검증"""
        # 모의 인증
        mock_creds = MagicMock()
        mock_creds.valid = True
        mock_creds.expired = False
        mock_creds.token = "fake_token"
        mock_creds_cls.from_service_account_file.return_value = mock_creds

        # HTTP 응답 모킹 (1: 폴더 검색, 2: 파일 목록, 3: 파일 다운로드)
        mock_folder_resp = MagicMock()
        mock_folder_resp.read.return_value = b'{"files": [{"id": "folder_123", "name": "24h_reports"}]}'
        mock_folder_resp.__enter__.return_value = mock_folder_resp

        mock_files_resp = MagicMock()
        mock_files_resp.read.return_value = b'''{
            "files": [
                {
                    "id": "file_1",
                    "name": "BTC_24h_20261025.md",
                    "mimeType": "application/vnd.google-apps.document",
                    "modifiedTime": "2026-10-25T10:00:00Z"
                },
                {
                    "id": "file_2",
                    "name": "invalid_name.txt",
                    "mimeType": "text/plain",
                    "modifiedTime": "2026-10-25T10:00:00Z"
                }
            ]
        }'''
        mock_files_resp.__enter__.return_value = mock_files_resp

        mock_content_resp = MagicMock()
        mock_content_resp.read.return_value = "# 비트코인(BTC) 24시간 분석\n\n내용".encode("utf-8")
        mock_content_resp.__enter__.return_value = mock_content_resp

        mock_urlopen.side_effect = [
            mock_folder_resp,
            mock_files_resp,
            mock_content_resp,
        ]

        syncer = DriveAnalysis24hSyncer(
            key_path=os.path.join(self.local_dir, "fake_key.json"),
            local_dir=self.local_dir,
            sync_ttl_seconds=0.0,
        )
        # 키 파일 존재 모의
        with open(syncer.key_path, "w") as f:
            f.write("{}")

        result = syncer.sync(force=True)
        self.assertTrue(result["synced"])
        self.assertEqual(result["downloaded_count"], 1)
        self.assertIn("BTC_24h_20261025.md", result["updated_files"])

        # 파일이 로컬 디렉터리에 생성되었는지 확인
        target_path = os.path.join(self.local_dir, "BTC_24h_20261025.md")
        self.assertTrue(os.path.isfile(target_path))
        with open(target_path, "r", encoding="utf-8") as f:
            content = f.read()
            self.assertIn("비트코인(BTC)", content)

    def test_syncer_respects_ttl(self):
        """TTL 이내 중복 호출 시 다운로드를 생략하는지 검증"""
        syncer = DriveAnalysis24hSyncer(
            key_path=os.path.join(self.local_dir, "fake_key.json"),
            local_dir=self.local_dir,
            sync_ttl_seconds=300.0,
        )
        syncer._last_sync_time = 9999999999.0  # 미래 시점
        res = syncer.sync(force=False)
        self.assertFalse(res["synced"])
        self.assertEqual(res["reason"], "cached")


if __name__ == "__main__":
    unittest.main()
