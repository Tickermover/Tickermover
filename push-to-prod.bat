@echo off
REM ════════════════════════════════════════════════════════════════
REM  push-to-prod.bat
REM  Promotes the dev branch to main. Railway prod auto-deploys to:
REM     https://alphahunt.in
REM
REM  ⚠ This deploys to production. Only run after you've tested
REM    your changes on https://web-production-17a78.up.railway.app
REM
REM  What it does:
REM    1. Verifies you've already pushed to dev
REM    2. Pulls latest origin/dev and origin/main
REM    3. Shows the commits about to ship
REM    4. Asks you to type YES
REM    5. Fast-forward merges dev into main
REM    6. Pushes main, then switches you back to dev
REM ════════════════════════════════════════════════════════════════

setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo.
echo ============================================
echo   AlphaHunt - Promote DEV to PROD
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

REM ── 3. Set remote with token ──────────────────────────────────────
git remote set-url origin https://digitalqueryai:%GH_TOKEN%@github.com/digitalqueryai/USAstockdashboard-.git
set "CLEAN_URL=https://github.com/digitalqueryai/USAstockdashboard-.git"

REM ── 4. Must be on dev with a clean working tree ───────────────────
for /f "delims=" %%B in ('git rev-parse --abbrev-ref HEAD') do set "BRANCH=%%B"
if /i not "!BRANCH!"=="dev" (
    echo [ERROR] You must be on 'dev' to promote. Currently on '!BRANCH!'.
    echo         Run push-to-dev.bat first, then come back to this script.
    git remote set-url origin %CLEAN_URL%
    pause & exit /b 1
)

git diff --quiet
set "DIRTY1=!ERRORLEVEL!"
git diff --cached --quiet
set "DIRTY2=!ERRORLEVEL!"
if !DIRTY1! NEQ 0 (set "DIRTY=1") else (set "DIRTY=0")
if !DIRTY2! NEQ 0 (set "DIRTY=1")
if "!DIRTY!"=="1" (
    echo [ERROR] Working tree has uncommitted changes:
    git status -s
    echo.
    echo Run push-to-dev.bat first to commit and push them.
    git remote set-url origin %CLEAN_URL%
    pause & exit /b 1
)

REM ── 5. Pull latest dev and main ───────────────────────────────────
echo === Fetching origin ===
git fetch origin
echo.
echo === Pulling latest dev ===
git pull --ff-only origin dev
if !ERRORLEVEL! NEQ 0 (
    echo [ERROR] Could not fast-forward dev. Resolve manually.
    git remote set-url origin %CLEAN_URL%
    pause & exit /b 1
)

REM ── 6. Show what's shipping ───────────────────────────────────────
echo.
echo === Commits about to ship to PROD (alphahunt.in) ===
git log --oneline origin/main..dev
echo.

REM ── 7. Confirm ────────────────────────────────────────────────────
echo This will deploy the above commits to https://alphahunt.in
set /p "CONFIRM=Type YES to proceed: "
if /i not "!CONFIRM!"=="YES" (
    echo Cancelled. No changes made.
    git remote set-url origin %CLEAN_URL%
    pause & exit /b 0
)

REM ── 8. Switch to main and merge dev ───────────────────────────────
echo.
echo === Switching to main ===
git checkout main
git pull --ff-only origin main
if !ERRORLEVEL! NEQ 0 (
    echo [ERROR] Could not fast-forward main. Resolve manually.
    git checkout dev
    git remote set-url origin %CLEAN_URL%
    pause & exit /b 1
)

echo === Merging dev into main ===
git merge --no-ff dev -m "Promote dev -> main"
if !ERRORLEVEL! NEQ 0 (
    echo [ERROR] Merge conflict. Resolve manually, run 'git commit', then re-run.
    git remote set-url origin %CLEAN_URL%
    pause & exit /b 1
)

REM ── 9. Push main ──────────────────────────────────────────────────
echo === Pushing main ===
git push origin main
set "PUSH_RC=!ERRORLEVEL!"

REM ── 10. Switch back to dev ────────────────────────────────────────
git checkout dev
git remote set-url origin %CLEAN_URL%

echo.
if !PUSH_RC! EQU 0 (
    echo ============================================
    echo   SUCCESS — main updated on GitHub.
    echo   Railway prod will redeploy in ~90 seconds.
    echo   Verify: https://alphahunt.in
    echo   You're back on the 'dev' branch.
    echo ============================================
) else (
    echo [ERROR] Push of main failed.
    echo The merge happened locally but didn't reach GitHub.
    echo Fix your token, then run:
    echo     git checkout main
    echo     git push origin main
)

echo.
pause
