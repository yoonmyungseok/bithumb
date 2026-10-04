#!/bin/bash
set -euo pipefail

# 호출한 위치와 무관하게 이 스크립트가 있는 프로젝트에서 실행한다.
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

PYTHON_BIN="$PROJECT_DIR/venv/bin/python3"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN="$PROJECT_DIR/venv/bin/python"
fi
if [ ! -x "$PYTHON_BIN" ]; then
    echo "[오류] 프로젝트 가상환경의 Python을 찾을 수 없습니다. venv를 먼저 준비하세요." >&2
    exit 1
fi

echo "[업비트] 기존 봇과 워치독을 종료합니다."
"$PYTHON_BIN" src/process_manager.py upbit stop
sleep 3

echo "[업비트] 워치독과 봇을 백그라운드로 실행합니다."
# 워치독을 통해 비정상 종료 복구를 유지하고 터미널과 분리해 시작한다.
"$PYTHON_BIN" src/process_manager.py upbit start --background
echo "[안내] 상태 확인: ./status_all.sh / 종료: venv/bin/python src/process_manager.py upbit stop"
