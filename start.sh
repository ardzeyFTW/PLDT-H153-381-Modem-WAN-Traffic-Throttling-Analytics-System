#!/usr/bin/env bash
# PLDT H153-381 Modem WAN Traffic & Throttling Analytics System
# Linux & macOS Quick Start Script

set -e
cd "$(dirname "$0")"

echo "======================================================================"
echo "   PLDT H153-381 Modem WAN Traffic & Throttling Analytics System"
echo "======================================================================"
echo ""

# 1. Check Python
if command -v python3 >/dev/null 2>&1; then
    PY_CMD="python3"
elif command -v python >/dev/null 2>&1; then
    PY_CMD="python"
else
    echo "[ERROR] Python 3 was not found. Please install Python 3.9+."
    exit 1
fi

# 2. Virtual Environment
if [ ! -d ".venv" ]; then
    echo "[*] Creating virtual environment in .venv ..."
    $PY_CMD -m venv .venv
fi

source .venv/bin/activate

# 3. Dependencies
if ! python -c "import huawei_lte_api" >/dev/null 2>&1; then
    echo "[*] Installing dependencies from requirements.txt ..."
    pip install --quiet --upgrade pip
    pip install -r requirements.txt
fi

# 4. Check .env
NEED_SETUP=0
if [ ! -f ".env" ]; then
    NEED_SETUP=1
elif grep -q "YOUR_MODEM_PASSWORD" .env; then
    NEED_SETUP=1
fi

if [ "$NEED_SETUP" -eq 1 ]; then
    echo "[*] Launching setup wizard..."
    python setup_wizard.py
fi

# 5. Run Poller
echo ""
echo "[*] Starting PLDT Modem Analytics Engine..."
echo ""
python snmp_poller.py
