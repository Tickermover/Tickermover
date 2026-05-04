@echo off
setlocal

REM Get the folder this bat file lives in (handles spaces in path)
set "PROJ=%~dp0"
REM Remove trailing backslash
if "%PROJ:~-1%"=="\" set "PROJ=%PROJ:~0,-1%"

echo.
echo === AlphaHunt - GitHub Push ===
echo Folder: %PROJ%
echo.

REM Check git
git --version >nul 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Git not found. Install from: https://git-scm.com/download/win
    pause & exit /b 1
)
git --version

REM ── Wipe any broken .git and start fresh ──────────────────────────
echo.
echo Removing old .git folder if present...
if exist "%PROJ%\.git" (
    rmdir /s /q "%PROJ%\.git"
    echo [OK] Old .git removed
)

REM ── Init ──────────────────────────────────────────────────────────
git -C "%PROJ%" init
git -C "%PROJ%" branch -M main
echo [OK] Fresh git repo created

REM ── Identity ──────────────────────────────────────────────────────
git -C "%PROJ%" config user.email "digitalquery.ai@gmail.com"
git -C "%PROJ%" config user.name "AlphaHunt"

REM ── Remote with token ─────────────────────────────────────────────
git -C "%PROJ%" remote add origin https://digitalqueryai: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@github.com/digitalqueryai/USAstockdashboard-.git
echo [OK] Remote set

REM ── Stage all files ───────────────────────────────────────────────
git -C "%PROJ%" add -A
echo [OK] All files staged

REM ── Commit ────────────────────────────────────────────────────────
git -C "%PROJ%" commit -m "Fix daily article rotation, forgot-password redirectTo, redesign go-to-top button"
echo [OK] Committed

REM ── Push ──────────────────────────────────────────────────────────
echo.
echo Pushing to GitHub...
git -C "%PROJ%" push -u origin main --force

IF %ERRORLEVEL% EQU 0 (
    echo.
    echo ================================================
    echo  SUCCESS! Code is live at:
    echo  https://github.com/digitalqueryai/USAstockdashboard-
    echo ================================================
) ELSE (
    echo.
    echo [ERROR] Push failed. Token may have expired.
    echo Create a new token at: github.com/settings/tokens/new
)

echo.
pause
