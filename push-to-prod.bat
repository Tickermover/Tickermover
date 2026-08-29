@echo off
REM ════════════════════════════════════════════════════════════════
REM  push-to-prod.bat  (v2 — safe, never switches branches)
REM
REM  Pushes the current dev branch directly to origin/main on GitHub.
REM  Railway prod auto-deploys to https://tickermover.com within ~90s.
REM
REM  Why this is safe:
REM    - You stay on the 'dev' branch the whole time
REM    - Your working tree never changes
REM    - If the push fails, NO files are deleted or moved
REM
REM  Usage:
REM    1. Make sure you've run push-to-dev.bat first and tested on
REM       https://web-production-17a78.up.railway.app
REM    2. Double-click this file (or run from cmd)
REM    3. Type YES when prompted
REM ════════════════════════════════════════════════════════════════

setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo.
echo ============================================
echo   TickerMover - Promote DEV to PROD
echo ============================================
echo.

REM ── 1. Token check ────────────────────────────────────────────────
if "%GH_TOKEN%"=="" (
    echo [ERROR] GH_TOKEN environment variable is not set.
    echo See push-to-dev.bat for setup instructions.
    pause & exit /b 1
)

REM ── 2. Stale lock cleanup ─────────────────────────────────────────
if exist ".git\index.lock" (
    for %%F in (".git\index.lock") do set "LSZ=%%~zF"
    if "!LSZ!"=="0" (
        del /f /q ".git\index.lock"
        echo [OK] Cleared stale .git\index.lock
    )
)

REM ── 3. Set remote with token (kept only for this run) ─────────────
git remote set-url origin https://Tickermover:%GH_TOKEN%@github.com/Tickermover/Tickermover.git
set "CLEAN_URL=https://github.com/Tickermover/Tickermover.git"

REM ── 4. Must be on dev ─────────────────────────────────────────────
for /f "delims=" %%B in ('git rev-parse --abbrev-ref HEAD') do set "BRANCH=%%B"
if /i not "!BRANCH!"=="dev" (
    echo [ERROR] You must be on the 'dev' branch. Currently on: !BRANCH!
    echo         Run:  git checkout dev
    git remote set-url origin %CLEAN_URL%
    pause & exit /b 1
)

REM ── 5. Working tree must be clean ─────────────────────────────────
git diff --quiet
set "D1=!ERRORLEVEL!"
git diff --cached --quiet
set "D2=!ERRORLEVEL!"
if "!D1!" NEQ "0" set "DIRTY=1"
if "!D2!" NEQ "0" set "DIRTY=1"
if "!DIRTY!"=="1" (
    echo [ERROR] You have uncommitted changes:
    git status -s
    echo.
    echo Run push-to-dev.bat first to commit and push them.
    git remote set-url origin %CLEAN_URL%
    pause & exit /b 1
)

REM ── 6. Fetch latest origin so we know what's on main right now ────
echo === Fetching origin ===
git fetch origin
if !ERRORLEVEL! NEQ 0 (
    echo [ERROR] Fetch failed. Check your token and network.
    git remote set-url origin %CLEAN_URL%
    pause & exit /b 1
)

REM ── 7. Show what's about to ship ──────────────────────────────────
echo.
echo === Commits on dev that will ship to PROD ===
git log --oneline origin/main..HEAD
echo.
echo === Currently on PROD (origin/main) ===
git log --oneline origin/main -3
echo.

REM ── 8. Confirm ────────────────────────────────────────────────────
echo This will REPLACE origin/main with dev's current commit.
echo Railway prod will then auto-deploy to https://tickermover.com
echo.
set /p "CONFIRM=Type YES to proceed: "
if /i not "!CONFIRM!"=="YES" (
    echo Cancelled. Nothing changed.
    git remote set-url origin %CLEAN_URL%
    pause & exit /b 0
)

REM ── 9. Push dev DIRECTLY to origin/main ───────────────────────────
REM  No checkout, no merge, no local branch movement.
REM  --force-with-lease = "only push if origin/main is what we just fetched"
REM  This blocks accidentally clobbering changes someone else pushed.
echo.
echo === Pushing dev -^> origin/main ===
git push origin dev:main --force-with-lease
set "PUSH_RC=!ERRORLEVEL!"

REM ── 10. Strip token from .git/config ──────────────────────────────
git remote set-url origin %CLEAN_URL%

echo.
if "!PUSH_RC!"=="0" goto :ok
goto :failed

:ok
echo ============================================
echo   SUCCESS
echo   origin/main now matches dev.
echo   Railway prod will redeploy in about 90 seconds.
echo   Verify: https://tickermover.com
echo   You are still on the dev branch. Working tree unchanged.
echo ============================================
goto :end

:failed
echo [ERROR] Push failed.
echo.
echo Common reasons:
echo   - Stale lease: someone or something pushed to main since
echo     your last fetch. Run 'git fetch origin' and try again.
echo   - Token expired: generate a new one at
echo     github.com/settings/tokens, then run
echo     setx GH_TOKEN "ghp_yourNewToken"
echo     and open a NEW cmd window.
echo.
echo IMPORTANT: this failure did NOT change anything locally.
echo Your working tree is intact, you are still on dev, files are safe.
goto :end

:end

echo.
pause
