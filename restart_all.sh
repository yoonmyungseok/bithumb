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

echo "======================================================"
echo "   Restarting All Quant Bots and Dashboard Server..."
echo "======================================================"

# 1. Stop all
echo "[1/2] Stopping all running bots and dashboard..."
$PYTHON_BIN src/process_manager.py all stop
sleep 2

# 2. Start all
echo "[2/2] Starting all bots and dashboard..."
$PYTHON_BIN src/process_manager.py all start
EXIT_CODE=$?

echo ""
echo "======================================================"
echo "  Unified Dashboard: http://localhost:7979"
echo "  Check Status:      ./status_all.sh"
echo "  Stop All:          ./stop_all.sh"
echo "======================================================"
exit $EXIT_CODE
