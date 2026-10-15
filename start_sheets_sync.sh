#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

PYTHON_BIN="venv/bin/python3"
if [ ! -f "$PYTHON_BIN" ]; then
    PYTHON_BIN="venv/bin/python"
fi

PID_FILE="data/.sheets_sync.pid"
LOG_FILE="logs/sheets_sync.log"
mkdir -p data logs

if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if ps -p "$OLD_PID" > /dev/null 2>&1; then
        echo "[SheetsSync] 이미 실행 중입니다. (PID: $OLD_PID)"
        exit 0
    else
        rm -f "$PID_FILE"
    fi
fi

echo "[SheetsSync] 구글 스프레드시트 5분 동기화 워커를 백그라운드로 실행합니다..."
nohup $PYTHON_BIN src/sheets/sync_worker.py --interval 300 > /dev/null 2>&1 &
PID=$!
echo $PID > "$PID_FILE"
echo "[SheetsSync] 실행 완료! (PID: $PID)"
echo "[SheetsSync] 로그 확인: tail -f $LOG_FILE"
