#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

PYTHON_BIN="venv/bin/python3"
if [ ! -f "$PYTHON_BIN" ]; then
    PYTHON_BIN="venv/bin/python"
fi
if [ ! -f "$PYTHON_BIN" ]; then
    PYTHON_BIN="python3"
fi

echo "======================================================"
echo "   [로컬 PC ➡️ 리눅스 서버] 전체 프로젝트 배포"
echo "======================================================"
$PYTHON_BIN src/sync_manager.py upload_all
