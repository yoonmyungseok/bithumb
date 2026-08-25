import argparse
import json
import os
import subprocess
import sys
import time

# UTF-8 출력 보장
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _project_root() -> str:
    """프로세스 상태 파일과 실행 스크립트가 공통으로 사용하는 프로젝트 루트를 반환한다."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _runtime_paths(exchange: str) -> tuple[str, str]:
    """거래소별 워치독 PID 파일과 봇 하트비트 파일 위치를 반환한다."""
    subdir = "upbit" if exchange.lower() == "upbit" else ""
    runtime_dir = os.path.join(_project_root(), "data", subdir)
    return os.path.join(runtime_dir, ".watchdog.pid.json"), os.path.join(runtime_dir, ".heartbeat")


def _read_pid_file(path: str) -> int | None:
    """손상되었거나 과거 형식인 PID 파일도 안전하게 무시한다."""
    try:
        with open(path, "r", encoding="utf-8") as file:
            payload = json.load(file)
        pid = int(payload.get("pid", 0))
        return pid if pid > 0 else None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _is_pid_alive(pid: int | None) -> bool:
    """WMI 권한 없이 동일 사용자 PID의 생존 여부만 보수적으로 확인한다."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, PermissionError):
        return False


def _write_pid_file(path: str, pid: int, exchange: str) -> None:
    """백그라운드 워치독 PID를 원자적으로 보관해 다음 실행의 중복 기동을 막는다."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary_path = f"{path}.{os.getpid()}.tmp"
    payload = {"pid": pid, "exchange": exchange.lower(), "created_at": time.time()}
    with open(temporary_path, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary_path, path)


def find_bot_processes(exchange: str = "bithumb") -> list[str]:
    """PID 파일로 관리되는 워치독만 반환한다. WMI/CIM 의존성은 사용하지 않는다."""
    pid_path, _ = _runtime_paths(exchange)
    pid = _read_pid_file(pid_path)
    return [str(pid)] if _is_pid_alive(pid) else []


def _get_heartbeat_age(heartbeat_path: str) -> float | None:
    """정상적으로 읽힌 하트비트의 경과 시간을 반환하고 오류는 미확인 상태로 구분한다."""
    try:
        with open(heartbeat_path, "r", encoding="utf-8") as file:
            timestamp = float(json.load(file).get("timestamp", 0.0))
        return max(0.0, time.time() - timestamp) if timestamp > 0.0 else None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def status_action(exchange: str = "bithumb"):
    ex_name = "업비트 (Upbit)" if exchange.lower() == "upbit" else "빗썸 (Bithumb)"
    web_port = 7980 if exchange.lower() == "upbit" else 7979
    pid_file, hb_file = _runtime_paths(exchange)

    print("======================================================")
    print(f" [{ex_name} AI Pro Quant Trading Bot 실행 및 진단 상태] ")
    print("======================================================")

    pids = find_bot_processes(exchange)
    heartbeat_age = _get_heartbeat_age(hb_file)
    if pids:
        for pid in pids:
            print(f"🟢 [{ex_name} 봇 가동 중] PID: {pid}")
    elif heartbeat_age is not None and heartbeat_age < 600.0:
        # PID 파일 도입 전 실행된 레거시 워치독도 중지로 오인해 중복 기동하지 않도록 표시한다.
        print(f"🟡 [{ex_name} 봇 추정 가동 중] 신선한 하트비트가 있으나 PID 파일이 없습니다.")
    else:
        print(f"⚪ [중지됨] PID 파일에서 실행 중인 {ex_name} 워치독을 확인하지 못했습니다.")

    # 하트비트 파일 진단
    if os.path.exists(hb_file):
        try:
            with open(hb_file, "r", encoding="utf-8") as f:
                hb_data = json.load(f)
            hb_age = time.time() - float(hb_data.get("timestamp", 0.0))
            hb_status = "🟢 신선 (정상)" if hb_age < 600 else f"🔴 지연 ({hb_age/60:.1f}분 전 갱신)"
            print(f"💓 [하트비트 상태] {hb_status} (최근 갱신: {hb_data.get('datetime', 'N/A')})")
        except Exception:
            print("💓 [하트비트 상태] 파일 읽기 실패")
    else:
        print("💓 [하트비트 상태] ⚠️ 하트비트 파일 없음")

    print(f"🌐 [웹 대시보드 URL] http://localhost:{web_port}")
    print()


def _kill_matching_script_processes(patterns: list[str]) -> list[int]:
    """PowerShell을 통해 해당 스크립트명을 포함하는 모든 고아 프로세스를 찾아 강제 종료한다."""
    if sys.platform != "win32":
        return []
    killed_pids: list[int] = []
    my_pid = os.getpid()
    for pat in patterns:
        ps_cmd = (
            f"Get-CimInstance Win32_Process | "
            f"Where-Object {{ $_.CommandLine -like '*{pat}*' }} | "
            f"Select-Object -ExpandProperty ProcessId"
        )
        try:
            res = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=5,
            )
            if res.returncode == 0 and res.stdout.strip():
                for line in res.stdout.strip().splitlines():
                    pid_str = line.strip()
                    if pid_str.isdigit():
                        pid_val = int(pid_str)
                        if pid_val != my_pid:
                            subprocess.run(
                                ["taskkill", "/F", "/T", "/PID", str(pid_val)],
                                check=False,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                            )
                            killed_pids.append(pid_val)
        except Exception:
            pass
    return killed_pids


def stop_action(exchange: str = "bithumb"):
    ex_name = "업비트 (Upbit)" if exchange.lower() == "upbit" else "빗썸 (Bithumb)"
    print("======================================================")
    print(f" [{ex_name} AI Pro Quant Trading Bot 종료] ")
    print("======================================================\n")

    pid_file, hb_file = _runtime_paths(exchange)
    pids = find_bot_processes(exchange)
    stopped_count = 0
    if pids:
        for pid in pids:
            try:
                subprocess.run(
                    # 워치독의 자식 봇까지 함께 종료해 고아 프로세스를 남기지 않는다.
                    ["taskkill", "/F", "/T", "/PID", pid],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                print(f"🛑 [종료 완료] {ex_name} 봇 PID: {pid}")
                stopped_count += 1
            except Exception as e:
                print(f"⚠️ PID {pid} 종료 실패: {e}")
        try:
            os.remove(pid_file)
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"⚠️ PID 파일 정리 실패: {exc}")

    # 고아 워치독 및 봇 잔여 프로세스 전수 소탕
    script_patterns = ["watchdog_upbit.py", "main_upbit.py"] if exchange.lower() == "upbit" else ["watchdog.py", "main.py"]
    orphaned_pids = _kill_matching_script_processes(script_patterns)
    for opid in orphaned_pids:
        if str(opid) not in pids:
            print(f"🛑 [백그라운드 잔여 프로세스 강제 정리] {ex_name} PID: {opid}")
            stopped_count += 1

    # 하트비트 파일 정리
    if os.path.exists(hb_file):
        try:
            os.remove(hb_file)
        except OSError:
            pass

    if stopped_count > 0:
        print(f"\n✅ {ex_name} 워치독 및 봇 프로세스(총 {stopped_count}개)를 완전히 종료했습니다.")
    else:
        print(f"ℹ️ 현재 실행 중인 {ex_name} 봇 프로세스가 없습니다.")
    time.sleep(1)


def logs_action(exchange: str = "bithumb"):
    ex_name = "업비트 (Upbit)" if exchange.lower() == "upbit" else "빗썸 (Bithumb)"
    log_filename = "trading_upbit.log" if exchange.lower() == "upbit" else "trading.log"
    log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, log_filename)

    print("======================================================")
    print(f" [{ex_name} AI 봇 실시간 로그 모니터링] ")
    print(f" 파일: {log_file}")
    print(" (종료하려면 창을 닫거나 Ctrl+C 를 누르세요)")
    print("======================================================\n")

    while not os.path.exists(log_file):
        print(f"⏳ 로그 파일 생성 대기 중... ({log_file})")
        time.sleep(2)

    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
            for line in lines[-50:]:
                print(line, end="")

            while True:
                line = f.readline()
                if line:
                    print(line, end="", flush=True)
                else:
                    time.sleep(0.5)
    except KeyboardInterrupt:
        print(f"\n👋 {ex_name} 로그 모니터링을 종료합니다.")


def start_action(exchange: str = "bithumb", background: bool = True) -> bool:
    """트레이딩 봇 및 워치독 프로세스 안전 시작 (과제: 중복 실행 방지 및 백그라운드 완벽 스폰)"""
    ex_name = "업비트 (Upbit)" if exchange.lower() == "upbit" else "빗썸 (Bithumb)"
    print("======================================================")
    print(f" [{ex_name} AI Pro Quant Trading Bot 가동 시작] ")
    print("======================================================\n")

    # 1. 기존 프로세스 확인: 실행 중이면 절대로 새 워치독을 중복 기동하지 않는다.
    pids = find_bot_processes(exchange)
    if pids:
        print(f"ℹ️ {ex_name} 워치독이 이미 실행 중입니다 (PID: {', '.join(pids)}). 중복 기동하지 않습니다.")
        status_action(exchange)
        return False

    pid_file, heartbeat_file = _runtime_paths(exchange)
    heartbeat_age = _get_heartbeat_age(heartbeat_file)
    if heartbeat_age is not None and heartbeat_age < 600.0:
        print(f"⚠️ {ex_name} 하트비트가 신선합니다 ({heartbeat_age:.0f}초 전). PID 파일 없이 실행 중인 기존 봇일 수 있어 기동을 차단합니다.")
        print("   기존 프로세스 확인 후 stop 배치 파일 또는 수동 점검을 진행하세요.")
        return False
    # 종료된 워치독의 PID 파일만 제거한다. 실행 중 PID는 위에서 이미 차단했다.
    if os.path.exists(pid_file):
        try:
            os.remove(pid_file)
        except OSError as exc:
            print(f"❌ 오래된 PID 파일 정리 실패: {exc}")
            return False

    # 2. 파이썬 실행 바이너리 및 스크립트 경로 탐색
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if background:
        python_exe = os.path.join(project_root, "venv", "Scripts", "pythonw.exe")
        if not os.path.exists(python_exe):
            python_exe = os.path.join(project_root, "venv", "Scripts", "python.exe")
    else:
        python_exe = os.path.join(project_root, "venv", "Scripts", "python.exe")

    if not os.path.exists(python_exe):
        python_exe = sys.executable

    script_name = "watchdog_upbit.py" if exchange.lower() == "upbit" else "watchdog.py"
    script_path = os.path.join(project_root, "src", script_name)

    # 3. 백그라운드 분리 스폰 (PowerShell Start-Process -WindowStyle Hidden)
    if background:
        ps_cmd = (
            f"Start-Process -FilePath '{python_exe}' "
            f"-ArgumentList '\"{script_path}\"' "
            f"-WorkingDirectory '{project_root}' "
            f"-WindowStyle Hidden -PassThru | Select-Object -ExpandProperty Id"
        )
        try:
            spawned = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                check=True,
                timeout=10,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            watchdog_pid = int(spawned.stdout.strip().splitlines()[-1])
            _write_pid_file(pid_file, watchdog_pid, exchange)
            print(f"🚀 {ex_name} 워치독 및 봇을 백그라운드 프로세스로 안전하게 가동했습니다.")
        except Exception as e:
            print(f"❌ 프로세스 스폰 실패: {e}")
            return False

        time.sleep(3.0)
        print()
        status_action(exchange)
        return True
    else:
        try:
            subprocess.run([python_exe, script_path], cwd=project_root)
        except KeyboardInterrupt:
            print(f"\n👋 {ex_name} 봇 콘솔 실행을 종료합니다.")
        return True


def main():
    parser = argparse.ArgumentParser(description="Trading Bot Process Manager")
    parser.add_argument("action_or_exchange", nargs="?", default="status", help="status | start | stop | logs | upbit | bithumb")
    parser.add_argument("action_sub", nargs="?", default="", help="status | start | stop | logs")
    parser.add_argument("--exchange", "-e", default="", help="bithumb or upbit")
    parser.add_argument("--background", "-b", action="store_true", default=True, help="Run in background (pythonw)")
    parser.add_argument("--foreground", "-f", action="store_true", help="Run in foreground console")

    args = parser.parse_args()

    # 인자 파싱 유연화
    # 1. python process_manager.py upbit start
    # 2. python process_manager.py start --exchange bithumb
    # 3. python process_manager.py status
    first = (args.action_or_exchange or "").lower()
    second = (args.action_sub or "").lower()
    exchange = (args.exchange or "").lower()

    if first in ("upbit", "bithumb"):
        exchange = first
        action = second if second else "status"
    else:
        action = first if first else "status"
        if not exchange:
            exchange = "bithumb"

    is_bg = not args.foreground

    if action == "status":
        status_action(exchange)
    elif action in ("start", "run"):
        # 배치 파일이 기동 차단과 성공을 구분할 수 있도록 종료 코드를 명확히 반환한다.
        sys.exit(0 if start_action(exchange, background=is_bg) else 2)
    elif action == "stop":
        stop_action(exchange)
    elif action in ("logs", "log"):
        logs_action(exchange)
    else:
        print(f"알 수 없는 액션: {action}")
        print("사용법:")
        print("  python src/process_manager.py [start|status|stop|logs]")
        print("  python src/process_manager.py [bithumb|upbit] [start|status|stop|logs]")


if __name__ == "__main__":
    main()
