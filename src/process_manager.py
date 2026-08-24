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


def find_bot_processes() -> list[str]:
    """현재 실행 중인 트레이딩 봇 PID 목록 반환"""
    pids = []
    try:
        ps_cmd = "Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe') -and ($_.CommandLine -like '*src\\main.py*' -or $_.CommandLine -like '*src/main.py*') } | Select-Object -ExpandProperty ProcessId"
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            text=True,
            errors="replace",
            timeout=10,
        )
        for line in out.strip().splitlines():
            line = line.strip()
            if line.isdigit():
                pids.append(line)
    except Exception as e:
        print(f"[ERROR] 프로세스 탐색 실패: {e}")
    return pids


def status_action():
    print("======================================================")
    print(" [빗썸 AI Pro Quant Trading Bot 실행 상태] ")
    print("======================================================\n")

    pids = find_bot_processes()
    if pids:
        for pid in pids:
            print(f"🟢 [빗썸 봇 정상 가동 중] PID: {pid}")
    else:
        print("⚪ [중지됨] 현재 실행 중인 봇 프로세스가 없습니다.")
    print()


def stop_action():
    print("======================================================")
    print(" [빗썸 AI Pro Quant Trading Bot 종료] ")
    print("======================================================\n")

    pids = find_bot_processes()
    if pids:
        for pid in pids:
            try:
                subprocess.run(["taskkill", "/F", "/PID", pid], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                print(f"🛑 [종료 완료] 빗썸 봇 PID: {pid}")
            except Exception as e:
                print(f"⚠️ PID {pid} 종료 실패: {e}")
        print("\n✅ 빗썸 봇 프로세스가 안전하게 종료되었습니다.")
    else:
        print("ℹ️ 현재 실행 중인 봇 프로세스가 없습니다.")
    time.sleep(1)


def logs_action():
    log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "trading.log")

    print("======================================================")
    print(" [빗썸 AI 봇 실시간 로그 모니터링] ")
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
        print("\n👋 로그 모니터링을 종료합니다.")


def main():
    if len(sys.argv) < 2:
        print("사용법: python process_manager.py [status|stop|logs]")
        return

    action = sys.argv[1].lower()
    if action == "status":
        status_action()
    elif action == "stop":
        stop_action()
    elif action in ("logs", "log"):
        logs_action()
    else:
        print(f"알 수 없는 액션: {action}")


if __name__ == "__main__":
    main()
