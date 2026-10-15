#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

PID_FILE="data/.sheets_sync.pid"
LOG_FILE="logs/sheets_sync.log"

echo "======================================================"
echo "  Google Sheets Sync Worker Status"
echo "======================================================"

IS_RUNNING=0
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if ps -p "$PID" > /dev/null 2>&1; then
        echo "  상태: 🟢 실행 중 (PID: $PID)"
        IS_RUNNING=1
    fi
fi

if [ $IS_RUNNING -eq 0 ]; then
    PIDS=$(pgrep -f "src/sheets/sync_worker.py")
    if [ -n "$PIDS" ]; then
        echo "  상태: 🟢 실행 중 (PID: $PIDS)"
        IS_RUNNING=1
    else
        echo "  상태: 🔴 중지됨 (실행 중이 아님)"
    fi
fi

echo "======================================================"
if [ -f "$LOG_FILE" ]; then
    echo "  [최근 로그 (마지막 5줄)]"
    tail -n 5 "$LOG_FILE"
fi
echo "======================================================"
