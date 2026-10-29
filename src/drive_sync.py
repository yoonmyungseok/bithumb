"""구글 드라이브(24h_reports 폴더)에서 24시간 종목 분석 보고서를 동기화하는 모듈.

Gemini 웹 등에서 작성되어 구글 드라이브 24h_reports 폴더에 저장된 마크다운(.md) 보고서를
로컬 '24h/{종목심볼}_24h_{YYYYMMDD}.md' 형식으로 다운로드 및 동기화합니다.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from typing import Any

from google.oauth2.service_account import Credentials
import google.auth.transport.requests

logger = logging.getLogger(__name__)

# 허용되는 파일명 패턴: {SYMBOL}_24h_{YYYYMMDD}.md 또는 {SYMBOL}_24h_{YYYYMMDD} (확장자 없을 수도 있음)
ANALYSIS_24H_FILENAME_PATTERN = re.compile(r"^([A-Za-z0-9_]+)_24h_(\d{8})(?:\.md)?$", re.IGNORECASE)


class DriveAnalysis24hSyncer:
    """구글 드라이브 24h_reports 폴더와 로컬 24h 디렉터리 간 보고서 동기화 매니저."""

    SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

    def __init__(
        self,
        key_path: str | None = None,
        folder_id: str | None = None,
        folder_name: str = "24h_reports",
        local_dir: str | None = None,
        sync_ttl_seconds: float = 60.0,
    ) -> None:
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        raw_key = key_path or os.getenv("GOOGLE_SHEETS_KEY_PATH", "config/service_account.json")
        if not os.path.isabs(raw_key):
            self.key_path = os.path.join(project_root, raw_key)
        else:
            self.key_path = raw_key

        self.folder_id = folder_id or os.getenv("GOOGLE_DRIVE_24H_FOLDER_ID", "").strip() or None
        self.folder_name = folder_name
        self.local_dir = os.path.abspath(local_dir or os.path.join(project_root, "24h"))
        self.sync_ttl_seconds = sync_ttl_seconds

        self._creds: Credentials | None = None
        self._last_sync_time: float = 0.0

    def _get_access_token(self) -> str:
        """서비스 계정 키를 통해 유효한 Access Token 획득 및 갱신."""
        if not os.path.exists(self.key_path):
            raise FileNotFoundError(f"서비스 계정 키 파일이 없습니다: {self.key_path}")

        if self._creds is None:
            self._creds = Credentials.from_service_account_file(self.key_path, scopes=self.SCOPES)

        auth_req = google.auth.transport.requests.Request()
        if not self._creds.valid or self._creds.expired:
            self._creds.refresh(auth_req)

        return str(self._creds.token)

    def _find_folder_id(self, token: str) -> str | None:
        """폴더명이 '24h_reports'인 드라이브 폴더의 ID를 검색."""
        if self.folder_id:
            return self.folder_id

        q = urllib.parse.quote(
            f"name = '{self.folder_name}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        )
        url = f"https://www.googleapis.com/drive/v3/files?q={q}&fields=files(id,name)&pageSize=10"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})

        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            files = data.get("files", [])
            if files:
                self.folder_id = str(files[0]["id"])
                logger.info(f"[DriveSync] '{self.folder_name}' 폴더 감지 완료 (ID: {self.folder_id})")
                return self.folder_id

        logger.warning(f"[DriveSync] '{self.folder_name}' 폴더를 찾을 수 없습니다.")
        return None

    def _download_file_content(self, file_id: str, mime_type: str, token: str) -> str:
        """Google Docs(문서) 또는 바이너리 .md 파일의 텍스트 내용 다운로드."""
        if mime_type == "application/vnd.google-apps.document":
            # Google Docs인 경우 마크다운으로 Export
            url = f"https://www.googleapis.com/drive/v3/files/{file_id}/export?mimeType=text/markdown"
        else:
            # 일반 텍스트/.md 파일인 경우 직접 바이너리 다운로드
            url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"

        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw_bytes = resp.read()
            return raw_bytes.decode("utf-8", errors="replace")

    def sync(self, force: bool = False) -> dict[str, Any]:
        """드라이브의 24h_reports 폴더를 스캔하여 로컬 24h 디렉터리로 동기화."""
        now = time.time()
        if not force and (now - self._last_sync_time) < self.sync_ttl_seconds:
            return {
                "synced": False,
                "reason": "cached",
                "seconds_until_next": max(0.0, self.sync_ttl_seconds - (now - self._last_sync_time)),
            }

        if not os.path.exists(self.key_path):
            logger.debug(f"[DriveSync] 키 파일이 없어 동기화를 건너뜁니다: {self.key_path}")
            return {"synced": False, "reason": "no_key_file"}

        os.makedirs(self.local_dir, exist_ok=True)

        try:
            token = self._get_access_token()
            folder_id = self._find_folder_id(token)
            if not folder_id:
                return {"synced": False, "reason": "folder_not_found"}

            q = urllib.parse.quote(f"'{folder_id}' in parents and trashed = false")
            url = f"https://www.googleapis.com/drive/v3/files?q={q}&fields=files(id,name,mimeType,modifiedTime,size)&pageSize=50"
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})

            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                files = data.get("files", [])

            downloaded_count = 0
            updated_files: list[str] = []

            for f in files:
                name = str(f.get("name", "")).strip()
                file_id = str(f.get("id", ""))
                mime_type = str(f.get("mimeType", ""))
                mtime_str = f.get("modifiedTime", "")

                match = ANALYSIS_24H_FILENAME_PATTERN.match(name)
                if not match:
                    logger.debug(f"[DriveSync] 24h 명명 규칙과 불일치하여 스킵: {name}")
                    continue

                symbol = match.group(1).upper()
                date_str = match.group(2)
                target_filename = f"{symbol}_24h_{date_str}.md"
                target_path = os.path.join(self.local_dir, target_filename)

                # 파일 수정 여부 확인 (로컬에 파일이 없거나, 구글 드라이브 mtime이 더 최근인 경우 다운로드)
                need_download = True
                if os.path.exists(target_path):
                    # 로컬 파일이 이미 존재하면, mtime 비교
                    try:
                        local_mtime = os.path.getmtime(target_path)
                        # RFC 3339/ISO 8601 parsing (예: 2026-10-25T09:16:33.901Z)
                        clean_time = mtime_str.replace("Z", "+00:00")
                        import datetime
                        remote_dt = datetime.datetime.fromisoformat(clean_time)
                        remote_ts = remote_dt.timestamp()

                        # 원격 수정 시간이 로컬 수정 시간보다 이전이거나 같으면 다운로드 스킵
                        if remote_ts <= (local_mtime + 2.0):
                            need_download = False
                    except Exception as e:
                        logger.debug(f"[DriveSync] mtime 비교 실패, 재다운로드 수행: {e}")
                        need_download = True

                if need_download:
                    try:
                        content = self._download_file_content(file_id, mime_type, token)
                        if content:
                            with open(target_path, "w", encoding="utf-8") as out_f:
                                out_f.write(content)
                            downloaded_count += 1
                            updated_files.append(target_filename)
                            logger.info(f"[DriveSync] 새 24h 보고서 동기화 완료: {target_filename}")
                    except Exception as err:
                        logger.error(f"[DriveSync] 파일 다운로드 실패 ({name}): {err}")

            self._last_sync_time = now
            return {
                "synced": True,
                "downloaded_count": downloaded_count,
                "updated_files": updated_files,
                "total_remote_files": len(files),
            }

        except Exception as e:
            logger.warning(f"[DriveSync] 동기화 중 오류 발생: {e}")
            return {"synced": False, "error": str(e)}
