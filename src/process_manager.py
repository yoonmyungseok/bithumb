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
    ex = exchange.lower()
    if ex == "upbit":
        subdir = "upbit"
    elif ex == "dashboard":
        subdir = ""
        runtime_dir = os.path.join(_project_root(), "data")
        return os.path.join(runtime_dir, ".dashboard.pid.json"), ""
    else:
        subdir = ""
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
    """PID 프로세스의 생존 여부를 안전하게 확인한다."""
    if not pid or pid <= 0:
        return False
    try:
        import psutil
        return psutil.pid_exists(int(pid))
    except Exception:
        pass
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except OSError:
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
    """PID 파일, 워치독 소유자 파일, 하트비트로 관리되는 워치독 및 봇 PID를 반환한다."""
    ex = exchange.lower()
    if ex == "dashboard":
        pid_path, _ = _runtime_paths("dashboard")
        pid = _read_pid_file(pid_path)
        return [str(pid)] if _is_pid_alive(pid) else []

    pid_path, hb_path = _runtime_paths(exchange)
    owner_path = os.path.join(os.path.dirname(hb_path), ".watchdog.lock.owner.json")

    candidates = [pid_path, owner_path, hb_path]
    live_pids: list[str] = []
    for cand in candidates:
        pid = _read_pid_file(cand)
        if pid and _is_pid_alive(pid) and str(pid) not in live_pids:
            live_pids.append(str(pid))
    return live_pids


def _get_heartbeat_age(heartbeat_path: str) -> float | None:
    """정상적으로 읽힌 하트비트의 경과 시간을 반환하고 오류는 미확인 상태로 구분한다."""
    try:
        with open(heartbeat_path, "r", encoding="utf-8") as file:
            timestamp = float(json.load(file).get("timestamp", 0.0))
        return max(0.0, time.time() - timestamp) if timestamp > 0.0 else None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def status_action(exchange: str = "bithumb"):
    ex = exchange.lower()
    if ex == "all":
        status_action("bithumb")
        status_action("upbit")
        status_action("dashboard")
        return

    if ex == "dashboard":
        print("======================================================")
        print(" [통합 퀀트 트레이딩 대시보드 게이트웨이 서버 상태] ")
        print("======================================================")
        pids = find_bot_processes("dashboard")
        if pids:
            for pid in pids:
                print(f"🟢 [대시보드 서버 가동 중] PID: {pid}")
        else:
            print("⚪ [중지됨] 실행 중인 통합 대시보드 프로세스가 없습니다.")
        print("🌐 [통합 대시보드 접속 URL] http://localhost:7979")
        print()
        return

    ex_name = "업비트 (Upbit)" if ex == "upbit" else "빗썸 (Bithumb)"
    internal_port = 17980 if ex == "upbit" else 17979
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

    print(f"🔌 [내부 코어 API 포트] http://127.0.0.1:{internal_port}")
    print()


import signal

def _kill_pid(pid: int | str) -> bool:
    """psutil 또는 OS 명령으로 PID 프로세스를 안전하게 종료시킨다."""
    try:
        pid_int = int(pid)
        if pid_int <= 0:
            return False
    except (ValueError, TypeError):
        return False

    try:
        import psutil
        if psutil.pid_exists(pid_int):
            p = psutil.Process(pid_int)
            p.kill()
            return True
        return False
    except Exception:
        pass

    if sys.platform == "win32":
        res = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid_int)],
            check=False,
            creationflags=0x08000000,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return res.returncode == 0
    else:
        try:
            os.kill(pid_int, signal.SIGKILL)
            return True
        except (OSError, PermissionError):
            return False


def _kill_matching_script_processes(patterns: list[str]) -> list[int]:
    """스크립트명을 포함하는 프로세스를 찾아 일괄 강제 종료한다."""
    killed_pids: list[int] = []
    my_pid = os.getpid()

    try:
        import psutil
        for proc in psutil.process_iter(['pid', 'cmdline']):
            try:
                p_id = proc.info['pid']
                if p_id == my_pid:
                    continue
                cmdline = " ".join(proc.info['cmdline'] or [])
                if any(p in cmdline for p in patterns):
                    proc.kill()
                    killed_pids.append(p_id)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except Exception:
        pass

    if sys.platform != "win32":
        for pat in patterns:
            try:
                res = subprocess.run(
                    ["pgrep", "-f", pat],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if res.returncode == 0 and res.stdout.strip():
                    for line in res.stdout.strip().splitlines():
                        pid_str = line.strip()
                        if pid_str.isdigit():
                            pid_val = int(pid_str)
                            if pid_val != my_pid and pid_val not in killed_pids:
                                if _kill_pid(pid_val):
                                    killed_pids.append(pid_val)
            except Exception:
                pass
    return killed_pids


def stop_action(exchange: str = "bithumb"):
    ex = exchange.lower()
    if ex == "all":
        stop_action("dashboard")
        stop_action("bithumb")
        stop_action("upbit")
        return

    if ex == "dashboard":
        print("======================================================")
        print(" [통합 퀀트 트레이딩 대시보드 서버 종료] ")
        print("======================================================\n")
        pid_file, _ = _runtime_paths("dashboard")
        pids = find_bot_processes("dashboard")
        stopped_count = 0
        if pids:
            for pid in pids:
                if _kill_pid(pid):
                    print(f"🛑 [종료 완료] 대시보드 서버 PID: {pid}")
                    stopped_count += 1
                else:
                    print(f"⚠️ PID {pid} 종료 실패 또는 이미 종료됨")

        if os.path.exists(pid_file):
            try:
                os.remove(pid_file)
            except OSError:
                pass

        orphans = _kill_matching_script_processes(["dashboard_server.py"])
        for opid in orphans:
            if str(opid) not in pids:
                print(f"🛑 [백그라운드 잔여 대시보드 정리] PID: {opid}")
                stopped_count += 1
        if stopped_count > 0:
            print(f"\n✅ 통합 대시보드 서버(총 {stopped_count}개)를 완전히 종료했습니다.")
        else:
            print("ℹ️ 현재 실행 중인 대시보드 프로세스가 없습니다.")
        return

    ex_name = "업비트 (Upbit)" if ex == "upbit" else "빗썸 (Bithumb)"
    print("======================================================")
    print(f" [{ex_name} AI Pro Quant Trading Bot 종료] ")
    print("======================================================\n")

    pid_file, hb_file = _runtime_paths(exchange)
    pids = find_bot_processes(exchange)
    stopped_count = 0
    if pids:
        for pid in pids:
            if _kill_pid(pid):
                print(f"🛑 [종료 완료] {ex_name} 봇 PID: {pid}")
                stopped_count += 1
            else:
                print(f"⚠️ PID {pid} 종료 실패 또는 이미 종료됨")
        try:
            os.remove(pid_file)
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"⚠️ PID 파일 정리 실패: {exc}")

    # 고아 워치독 및 봇 잔여 프로세스 전수 소탕
    script_patterns = ["watchdog_upbit.py", "main_upbit.py"] if ex == "upbit" else ["watchdog.py", "main.py"]
    orphaned_pids = _kill_matching_script_processes(script_patterns)
    for opid in orphaned_pids:
        if str(opid) not in pids:
            print(f"🛑 [백그라운드 잔여 프로세스 강제 정리] {ex_name} PID: {opid}")
            stopped_count += 1

    # 하트비트 및 락 파일 정리
    lock_file = os.path.join(os.path.dirname(hb_file), ".watchdog.lock")
    owner_file = f"{lock_file}.owner.json"
    for f in (hb_file, lock_file, owner_file):
        if os.path.exists(f):
            try:
                os.remove(f)
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

def _spawn_background_process(python_exe: str, script_path: str, cwd: str) -> int | None:
    """Windows와 Unix 환경 모두에서 부모 창이 닫혀도 영구 유지되는 완전 무창 백그라운드 프로세스를 즉각 스폰한다."""
    base_name = os.path.splitext(os.path.basename(script_path))[0]
    log_dir = os.path.join(cwd, "logs")
    os.makedirs(log_dir, exist_ok=True)
    spawn_log = os.path.join(log_dir, f"{base_name}_spawn.log")

    if sys.platform == "win32":
        flags = 0x08000000 | 0x00000200 | 0x00000008
    else:
        flags = 0

    try:
        f_log = open(spawn_log, "a", encoding="utf-8", errors="replace")
        proc = subprocess.Popen(
            [python_exe, script_path],
            cwd=cwd,
            creationflags=flags,
            stdout=f_log,
            stderr=f_log,
            stdin=subprocess.DEVNULL,
            close_fds=(sys.platform != "win32"),
        )
        return proc.pid
    except Exception as e:
        print(f"❌ 스폰 실패 ({script_path}): {e}")
        return None


def start_action(exchange: str = "bithumb", background: bool = True) -> bool:
    """프로세스 안전 시작 (bithumb | upbit | dashboard | all)"""
    ex = exchange.lower()
    if ex == "all":
        print("======================================================")
        print(" [빗썸 + 업비트 + 통합 대시보드 전체 일괄 가동 시작] ")
        print("======================================================\n")
        r1 = start_action("bithumb", background=background)
        time.sleep(1.0)
        r2 = start_action("upbit", background=background)
        time.sleep(1.0)
        r3 = start_action("dashboard", background=background)
        return r1 and r2 and r3

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    python_exe = os.path.join(project_root, "venv", "Scripts", "python.exe")
    if not os.path.exists(python_exe):
        for candidate in [os.path.join("venv", "bin", "python3"), os.path.join("venv", "bin", "python")]:
            full_path = os.path.join(project_root, candidate)
            if os.path.exists(full_path):
                python_exe = full_path
                break
        else:
            python_exe = sys.executable

    if ex == "dashboard":
        print("======================================================")
        print(" [통합 퀀트 트레이딩 대시보드 서버 가동 시작] ")
        print("======================================================\n")
        pids = find_bot_processes("dashboard")
        if pids:
            print(f"ℹ️ 대시보드 서버가 이미 실행 중입니다 (PID: {', '.join(pids)}).")
            status_action("dashboard")
            return True

        pid_file, _ = _runtime_paths("dashboard")
        script_path = os.path.join(project_root, "src", "dashboard_server.py")
        if background:
            pid = _spawn_background_process(python_exe, script_path, project_root)
            if pid:
                _write_pid_file(pid_file, pid, "dashboard")
                print(f"🚀 통합 대시보드 서버를 백그라운드 프로세스로 가동했습니다. (PID: {pid})")
                time.sleep(1.0)
                status_action("dashboard")
                return True
            else:
                print("❌ 대시보드 스폰 실패")
                return False
        else:
            try:
                subprocess.run([python_exe, script_path], cwd=project_root)
            except KeyboardInterrupt:
                print("\n👋 대시보드 콘솔 실행을 종료합니다.")
            return True

    ex_name = "업비트 (Upbit)" if ex == "upbit" else "빗썸 (Bithumb)"
    print("======================================================")
    print(f" [{ex_name} AI Pro Quant Trading Bot 가동 시작] ")
    print("======================================================\n")

    pids = find_bot_processes(exchange)
    if pids:
        print(f"ℹ️ {ex_name} 워치독이 이미 실행 중입니다 (PID: {', '.join(pids)}). 중복 기동하지 않습니다.")
        status_action(exchange)
        return True

    pid_file, heartbeat_file = _runtime_paths(exchange)
    lock_file = os.path.join(os.path.dirname(heartbeat_file), ".watchdog.lock")
    owner_file = f"{lock_file}.owner.json"
    for f in (heartbeat_file, lock_file, owner_file):
        if os.path.exists(f):
            try:
                os.remove(f)
            except OSError:
                pass

    if os.path.exists(pid_file):
        try:
            os.remove(pid_file)
        except OSError as exc:
            print(f"❌ 오래된 PID 파일 정리 실패: {exc}")
            return False

    script_name = "watchdog_upbit.py" if ex == "upbit" else "watchdog.py"
    script_path = os.path.join(project_root, "src", script_name)

    if background:
        watchdog_pid = _spawn_background_process(python_exe, script_path, project_root)
        if watchdog_pid:
            _write_pid_file(pid_file, watchdog_pid, exchange)
            print(f"🚀 {ex_name} 워치독 및 봇을 백그라운드 프로세스로 안전하게 가동했습니다. (PID: {watchdog_pid})")
        else:
            print(f"❌ {ex_name} 프로세스 스폰 실패")
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
    parser.add_argument("action_or_exchange", nargs="?", default="status", help="status | start | stop | logs | upbit | bithumb | dashboard | all")
    parser.add_argument("action_sub", nargs="?", default="", help="status | start | stop | logs")
    parser.add_argument("--exchange", "-e", default="", help="bithumb | upbit | dashboard | all")
    parser.add_argument("--background", "-b", action="store_true", default=True, help="Run in background (pythonw)")
    parser.add_argument("--foreground", "-f", action="store_true", help="Run in foreground console")

    args = parser.parse_args()

    first = (args.action_or_exchange or "").lower()
    second = (args.action_sub or "").lower()
    exchange = (args.exchange or "").lower()

    if first in ("upbit", "bithumb", "dashboard", "all"):
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
        sys.exit(0 if start_action(exchange, background=is_bg) else 2)
    elif action == "stop":
        stop_action(exchange)
    elif action in ("logs", "log"):
        logs_action(exchange)
    else:
        print(f"알 수 없는 액션: {action}")
        print("사용법:")
        print("  python src/process_manager.py [start|status|stop|logs]")
        print("  python src/process_manager.py [bithumb|upbit|dashboard|all] [start|status|stop|logs]")


if __name__ == "__main__":
    main()
