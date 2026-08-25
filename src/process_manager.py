import argparse
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


def find_bot_processes(exchange: str = "bithumb") -> list[str]:
    """
    현재 실행 중인 특정 거래소의 트레이딩 봇 및 워치독 PID 목록 반환 (완전 분리 탐색)
    """
    pids = []
    exchange = exchange.lower()
    try:
        if exchange == "upbit":
            ps_cmd = (
                "Get-CimInstance Win32_Process | Where-Object { "
                "($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe') -and "
                "($_.CommandLine -like '*main_upbit.py*' -or $_.CommandLine -like '*watchdog_upbit.py*') "
                "} | Select-Object -ExpandProperty ProcessId"
            )
        else:  # bithumb (default)
            ps_cmd = (
                "Get-CimInstance Win32_Process | Where-Object { "
                "($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe') -and "
                "($_.CommandLine -like '*src\\main.py*' -or $_.CommandLine -like '*src/main.py*' -or "
                "$_.CommandLine -like '*src\\watchdog.py*' -or $_.CommandLine -like '*src/watchdog.py*') -and "
                "($_.CommandLine -notlike '*main_upbit.py*') -and ($_.CommandLine -notlike '*watchdog_upbit.py*') "
                "} | Select-Object -ExpandProperty ProcessId"
            )

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
        print(f"[ERROR] {exchange.upper()} 프로세스 탐색 실패: {e}")
    return pids


def status_action(exchange: str = "bithumb"):
    ex_name = "업비트 (Upbit)" if exchange.lower() == "upbit" else "빗썸 (Bithumb)"
    web_port = 7980 if exchange.lower() == "upbit" else 7979
    data_subdir = "upbit" if exchange.lower() == "upbit" else ""
    hb_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", data_subdir)
    hb_file = os.path.join(hb_dir, ".heartbeat")

    print("======================================================")
    print(f" [{ex_name} AI Pro Quant Trading Bot 실행 및 진단 상태] ")
    print("======================================================")

    pids = find_bot_processes(exchange)
    if pids:
        for pid in pids:
            print(f"🟢 [{ex_name} 봇 가동 중] PID: {pid}")
    else:
        print(f"⚪ [중지됨] 현재 실행 중인 {ex_name} 봇 프로세스가 없습니다.")

    # 하트비트 파일 진단
    if os.path.exists(hb_file):
        try:
            import json
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


def stop_action(exchange: str = "bithumb"):
    ex_name = "업비트 (Upbit)" if exchange.lower() == "upbit" else "빗썸 (Bithumb)"
    print("======================================================")
    print(f" [{ex_name} AI Pro Quant Trading Bot 종료] ")
    print("======================================================\n")

    pids = find_bot_processes(exchange)
    if pids:
        for pid in pids:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/PID", pid],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                print(f"🛑 [종료 완료] {ex_name} 봇 PID: {pid}")
            except Exception as e:
                print(f"⚠️ PID {pid} 종료 실패: {e}")
        print(f"\n✅ {ex_name} 봇 프로세스가 안전하게 종료되었습니다.")
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


def main():
    parser = argparse.ArgumentParser(description="Trading Bot Process Manager")
    parser.add_argument("action_or_exchange", nargs="?", default="status", help="status | stop | logs | upbit | bithumb")
    parser.add_argument("action_sub", nargs="?", default="", help="status | stop | logs")
    parser.add_argument("--exchange", "-e", default="", help="bithumb or upbit")

    args = parser.parse_args()

    # 인자 파싱 유연화
    # 1. python process_manager.py upbit stop
    # 2. python process_manager.py stop --exchange upbit
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

    if action == "status":
        status_action(exchange)
    elif action == "stop":
        stop_action(exchange)
    elif action in ("logs", "log"):
        logs_action(exchange)
    else:
        print(f"알 수 없는 액션: {action}")
        print("사용법:")
        print("  python src/process_manager.py [status|stop|logs]")
        print("  python src/process_manager.py [bithumb|upbit] [status|stop|logs]")


if __name__ == "__main__":
    main()
