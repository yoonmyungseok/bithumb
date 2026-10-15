#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

PID_FILE="data/.sheets_sync.pid"

if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if ps -p "$PID" > /dev/null 2>&1; then
        echo "[SheetsSync] 워커(PID: $PID)를 안전하게 종료합니다..."
        kill -TERM "$PID" 2>/dev/null
        sleep 2
        if ps -p "$PID" > /dev/null 2>&1; then
            kill -9 "$PID" 2>/dev/null
        fi
        rm -f "$PID_FILE"
        echo "[SheetsSync] 워커가 종료되었습니다."
    else
        echo "[SheetsSync] 프로세스가 실행 중이지 않습니다."
        rm -f "$PID_FILE"
    fi
else
    # 프로세스 이름으로 조회하여 정리
    PIDS=$(pgrep -f "src/sheets/sync_worker.py")
    if [ -n "$PIDS" ]; then
        echo "[SheetsSync] 실행 중인 워커(PID: $PIDS)를 종료합니다..."
        kill $PIDS 2>/dev/null
    else
        echo "[SheetsSync] 실행 중인 워커가 없습니다."
    fi
fi
