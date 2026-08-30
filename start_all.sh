#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

if [ ! -f "venv/bin/python3" ] && [ ! -f "venv/bin/python" ]; then
    echo "[ERROR] Virtual environment 'venv' not found!"
    echo "Please create venv first: python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt"
    exit 1
fi

PYTHON_BIN="venv/bin/python3"
if [ ! -f "$PYTHON_BIN" ]; then
    PYTHON_BIN="venv/bin/python"
fi

$PYTHON_BIN src/process_manager.py all start
EXIT_CODE=$?

echo ""
echo "======================================================"
echo "  Bithumb Dashboard: http://localhost:7979"
echo "  Upbit Dashboard:   http://localhost:7980"
echo "  Check Status:      ./status_all.sh"
echo "  Stop All:          ./stop_all.sh"
echo "======================================================"
exit $EXIT_CODE
