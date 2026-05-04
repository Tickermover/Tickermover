@echo off
setlocal enabledelayedexpansion
title PopDetector v5

echo ============================================================
echo  PopDetector v5  --  FastAPI Live Dashboard
echo ============================================================
echo.

cd /d "%~dp0"
echo [1/4] Working directory: %CD%
echo.

REM ── Check Python ─────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found in PATH.
    echo         Install Python 3.10+ from https://python.org
    echo         Make sure to tick "Add Python to PATH" during install.
    pause
    exit /b 1
)
echo [2/4] Python found:
python --version
echo.

REM ── Install dependencies ──────────────────────────────────────
echo [3/4] Installing requirements...
python -m pip install --quiet -r requirements.txt
if errorlevel 1 (
    echo [WARNING] pip install had errors — trying without --quiet
    python -m pip install -r requirements.txt
)
echo       Done.
echo.

REM ── Create output dir ─────────────────────────────────────────
if not exist "output" mkdir output

REM ── Start server in background, open browser after delay ──────
echo [4/4] Starting server...
echo.
echo  URL  :  http://127.0.0.1:8000
echo  Stop :  Close this window or press Ctrl+C
echo.
echo ============================================================
echo.

REM Open browser after a 4-second delay (in background)
start /b cmd /c "timeout /t 4 /nobreak >nul && start http://127.0.0.1:8000"

REM Run uvicorn (blocks until Ctrl+C)
python -m uvicorn app:app --host 127.0.0.1 --port 8000 --reload

echo.
echo Server stopped.
pause
