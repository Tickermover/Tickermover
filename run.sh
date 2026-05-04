#!/usr/bin/env bash
# PopDetector v5 — macOS / Linux launcher
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "============================================================"
echo " PopDetector v5  --  FastAPI Live Dashboard"
echo "============================================================"

# ── Check Python ─────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    echo "[ERROR] python3 not found. Please install Python 3.10+"
    exit 1
fi

# ── Install dependencies ──────────────────────────────────────
echo "Installing / upgrading requirements..."
python3 -m pip install -q -r requirements.txt

# ── Create output dir ─────────────────────────────────────────
mkdir -p output

# ── Launch server ─────────────────────────────────────────────
echo ""
echo " Server starting at  http://127.0.0.1:8000"
echo " Press Ctrl+C to stop"
echo ""

# Open browser (best-effort)
if command -v open &>/dev/null; then
    sleep 1.5 && open "http://127.0.0.1:8000" &
elif command -v xdg-open &>/dev/null; then
    sleep 1.5 && xdg-open "http://127.0.0.1:8000" &
fi

python3 -m uvicorn app:app --host 127.0.0.1 --port 8000 --reload
