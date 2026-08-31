# -*- coding: utf-8 -*-
"""
로컬 PC <-> 원격 리눅스 서버 양방향 고속 동기화 매니저
- SFTP를 통해 data/, src/, config/, logs/ 등 선택 동기화
- mtime 및 size 기반 변경분만 스마트 고속 전송
"""

import argparse
import os
import posixpath
import stat
import sys
import time
from dotenv import load_dotenv
import paramiko

# UTF-8 출력 보장
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _get_ssh_client() -> tuple[paramiko.SSHClient, paramiko.SFTPClient, str]:
    load_dotenv(os.path.join(_project_root(), ".env"))
    host = os.getenv("REMOTE_SERVER_HOST", "100.72.97.0")
    port = int(os.getenv("REMOTE_SERVER_PORT", "22"))
    user = os.getenv("REMOTE_SERVER_USER", "trader")
    password = os.getenv("REMOTE_SERVER_PASS", "trader")
    remote_dir = os.getenv("REMOTE_SERVER_DIR", "/home/trader/bithumb-bot")

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(host, port=port, username=user, password=password, timeout=10)
        sftp = ssh.open_sftp()
        return ssh, sftp, remote_dir
    except Exception as e:
        print(f"❌ 원격 서버({host}:{port}) SSH 연결 실패: {e}")
        sys.exit(1)


def _sftp_makedirs(sftp: paramiko.SFTPClient, remote_dir: str):
    dirs_to_create = []
    current = remote_dir
    while current not in ("", "/", "."):
        try:
            sftp.stat(current)
            break
        except FileNotFoundError:
            dirs_to_create.append(current)
            current = posixpath.dirname(current)

    for d in reversed(dirs_to_create):
        try:
            sftp.mkdir(d)
        except Exception:
            pass


IGNORE_DIRS = {".git", "venv", "__pycache__", ".pytest_cache", ".idea", ".vscode", "node_modules"}
IGNORE_FILES = {".DS_Store", "desktop.ini", ".watchdog.lock"}


def sync_upload_folder(sftp: paramiko.SFTPClient, local_dir: str, remote_dir: str, folder_name: str) -> int:
    local_path = os.path.join(local_dir, folder_name)
    remote_path = posixpath.join(remote_dir, folder_name.replace(os.sep, "/"))

    if not os.path.exists(local_path):
        print(f"⚠️ 로컬 경로 없음: {local_path}")
        return 0

    _sftp_makedirs(sftp, remote_path)
    count = 0

    for root, dirs, files in os.walk(local_path):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS and not d.startswith("test_")]
        rel_dir = os.path.relpath(root, local_path)
        cur_remote_dir = remote_path if rel_dir == "." else posixpath.join(remote_path, rel_dir.replace(os.sep, "/"))
        _sftp_makedirs(sftp, cur_remote_dir)

        for file in files:
            if file in IGNORE_FILES or file.endswith(".tmp"):
                continue

            l_file = os.path.join(root, file)
            r_file = posixpath.join(cur_remote_dir, file)

            try:
                l_stat = os.stat(l_file)
                need_upload = True
                try:
                    r_stat = sftp.stat(r_file)
                    if r_stat.st_size == l_stat.st_size and abs(r_stat.st_mtime - l_stat.st_mtime) < 2.0:
                        need_upload = False
                except FileNotFoundError:
                    need_upload = True

                if need_upload:
                    sftp.put(l_file, r_file)
                    try:
                        sftp.utime(r_file, (int(l_stat.st_atime), int(l_stat.st_mtime)))
                    except Exception:
                        pass
                    print(f"  ⬆️ [업로드] {folder_name}/{posixpath.relpath(r_file, remote_path)} ({l_stat.st_size:,} bytes)")
                    count += 1
            except Exception as e:
                print(f"  ❌ 업로드 실패 ({l_file}): {e}")

    return count


def sync_download_folder(sftp: paramiko.SFTPClient, local_dir: str, remote_dir: str, folder_name: str) -> int:
    local_path = os.path.join(local_dir, folder_name)
    remote_path = posixpath.join(remote_dir, folder_name.replace(os.sep, "/"))
    os.makedirs(local_path, exist_ok=True)

    count = 0

    def _walk_remote(r_dir, l_dir):
        nonlocal count
        try:
            entries = sftp.listdir_attr(r_dir)
        except Exception:
            return

        for entry in entries:
            name = entry.filename
            if name in IGNORE_DIRS or name in IGNORE_FILES or name.endswith(".tmp"):
                continue

            r_sub = posixpath.join(r_dir, name)
            l_sub = os.path.join(l_dir, name)

            if stat.S_ISDIR(entry.st_mode):
                os.makedirs(l_sub, exist_ok=True)
                _walk_remote(r_sub, l_sub)
            else:
                l_stat = None
                if os.path.exists(l_sub):
                    l_stat = os.stat(l_sub)

                need_download = True
                if l_stat and l_stat.st_size == entry.st_size and abs(l_stat.st_mtime - entry.st_mtime) < 2.0:
                    need_download = False

                if need_download:
                    try:
                        sftp.get(r_sub, l_sub)
                        try:
                            os.utime(l_sub, (entry.st_atime, entry.st_mtime))
                        except Exception:
                            pass
                        print(f"  ⬇️ [다운로드] {folder_name}/{os.path.relpath(l_sub, local_path)} ({entry.st_size:,} bytes)")
                        count += 1
                    except Exception as e:
                        print(f"  ❌ 다운로드 실패 ({r_sub}): {e}")

    _walk_remote(remote_path, local_path)
    return count


def main():
    parser = argparse.ArgumentParser(description="로컬 PC <-> 리눅스 서버 양방향 동기화 도구")
    parser.add_argument("direction", choices=["upload_data", "download_data", "upload_all", "download_all", "upload_code", "status"], help="동기화 작업 선택")
    args = parser.parse_args()

    project_root = _project_root()
    ssh, sftp, remote_dir = _get_ssh_client()

    print("======================================================")
    print("  🔄 퀀트 봇 서버 동기화 매니저")
    print(f"  • 로컬 PC: {project_root}")
    print(f"  • 원격 서버: {remote_dir}")
    print("======================================================")

    start_t = time.time()

    if args.direction == "upload_data":
        print("\n🚀 [로컬 ➡️ 서버] 매매 데이터(data/) 업로드 동기화 시작...")
        c = sync_upload_folder(sftp, project_root, remote_dir, "data")
        print(f"\n✅ 매매 데이터 업로드 완료! (총 {c}개 파일 갱신)")

    elif args.direction == "download_data":
        print("\n🚀 [서버 ➡️ 로컬] 매매 데이터(data/) 다운로드 동기화 시작...")
        c = sync_download_folder(sftp, project_root, remote_dir, "data")
        print(f"\n✅ 매매 데이터 다운로드 완료! (총 {c}개 파일 갱신)")

    elif args.direction == "upload_all":
        print("\n🚀 [로컬 ➡️ 서버] 전체 프로젝트 (소스코드 + 설정 + 데이터) 배포 업로드 시작...")
        c = 0
        for f in ["src", "config", "data", "dashboard"]:
            c += sync_upload_folder(sftp, project_root, remote_dir, f)
        for root_file in os.listdir(project_root):
            full_p = os.path.join(project_root, root_file)
            if os.path.isfile(full_p) and not root_file.endswith(".tmp") and root_file not in IGNORE_FILES:
                r_file = posixpath.join(remote_dir, root_file)
                try:
                    sftp.put(full_p, r_file)
                    c += 1
                except Exception:
                    pass
        print(f"\n✅ 전체 프로젝트 서버 배포 완료! (총 {c}개 항목 전송)")

    elif args.direction == "upload_code":
        print("\n🚀 [로컬 ➡️ 서버] 소스코드 및 설정(src/, config/, dashboard/) 업로드 시작...")
        c = 0
        for f in ["src", "config", "dashboard"]:
            c += sync_upload_folder(sftp, project_root, remote_dir, f)
        print(f"\n✅ 소스코드 업로드 완료! (총 {c}개 파일 갱신)")

    elif args.direction == "download_all":
        print("\n🚀 [서버 ➡️ 로컬] 전체 데이터 및 로그 다운로드 시작...")
        c = sync_download_folder(sftp, project_root, remote_dir, "data")
        c += sync_download_folder(sftp, project_root, remote_dir, "logs")
        print(f"\n✅ 전체 다운로드 완료! (총 {c}개 파일 갱신)")

    elif args.direction == "status":
        print("\n🔍 원격 서버 프로세스 상태 확인:")
        stdin, stdout, stderr = ssh.exec_command(f"cd {remote_dir} && bash status_all.sh 2>&1 || true")
        print(stdout.read().decode("utf-8", errors="replace"))

    sftp.close()
    ssh.close()
    elapsed = time.time() - start_t
    print(f"⏱️ 소요 시간: {elapsed:.2f}초\n")


if __name__ == "__main__":
    main()
