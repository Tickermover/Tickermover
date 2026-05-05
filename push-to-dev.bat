@echo off
REM ════════════════════════════════════════════════════════════════
REM  push-to-dev.bat
REM  Commits everything and pushes it to the 'dev' branch.
REM  Railway dev project auto-deploys to:
REM     https://web-production-17a78.up.railway.app
REM
REM  Prod (alphahunt.in) is NOT touched by this script.
REM  Use push-to-prod.bat to promote dev -> prod.
REM ════════════════════════════════════════════════════════════════

setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo.
echo ============================================
echo   AlphaHunt - Push to DEV
echo ============================================
echo.

REM ── 1. Token check ────────────────────────────────────────────────
if "%GH_TOKEN%"=="" (
    echo [ERROR] GH_TOKEN environment variable is not set.
    echo.
    echo   1. Create a token at https://github.com/settings/tokens/new
    echo      ^(scope: repo, 90-day expiry^)
    echo   2. Open a NEW cmd window and run:
    echo        setx GH_TOKEN "ghp_yourTokenHere"
    echo   3. Open ANOTHER new cmd window and re-run this script.
    echo.
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

REM ── 3. Set remote with token (kept only for the duration of this run) ──
git remote set-url origin https://digitalqueryai:%GH_TOKEN%@github.com/digitalqueryai/USAstockdashboard-.git
set "CLEAN_URL=https://github.com/digitalqueryai/USAstockdashboard-.git"

REM ── 4. Make sure we're on dev (auto-switch if not) ────────────────
for /f "delims=" %%B in ('git rev-parse --abbrev-ref HEAD') do set "BRANCH=%%B"
if /i not "!BRANCH!"=="dev" (
    echo Currently on '!BRANCH!'. Switching to 'dev'...
    git checkout dev
    if !ERRORLEVEL! NEQ 0 (
        echo [ERROR] Could not switch to dev. Commit or stash your changes first.
        git remote set-url origin %CLEAN_URL%
        pause & exit /b 1
    )
)

REM ── 5. Show what's about to be committed ──────────────────────────
echo.
echo === Changes to commit ===
git add -A
git status -s
echo.

REM ── 6. Bail if nothing to commit AND nothing unpushed ─────────────
git diff --cached --quiet
set "HAS_STAGED=!ERRORLEVEL!"

REM ── 7. Get a commit message ───────────────────────────────────────
if "!HAS_STAGED!" NEQ "0" (
    if "%~1"=="" (
        set /p "MSG=Commit message: "
    ) else (
        set "MSG=%~1"
    )
    if "!MSG!"=="" (
        echo [ERROR] Empty commit message. Aborting.
        git remote set-url origin %CLEAN_URL%
        pause & exit /b 1
    )
    git commit -m "!MSG!"
    if !ERRORLEVEL! NEQ 0 (
        echo [ERROR] Commit failed.
        git remote set-url origin %CLEAN_URL%
        pause & exit /b 1
    )
) else (
    echo [INFO] Nothing new to commit. Will push existing commits if any.
)

REM ── 8. Push ───────────────────────────────────────────────────────
echo.
echo === Pushing to origin/dev ===
git push -u origin dev
set "PUSH_RC=!ERRORLEVEL!"

REM ── 9. Strip token from .git/config so it's never persisted ───────
git remote set-url origin %CLEAN_URL%

echo.
if !PUSH_RC! EQU 0 (
    echo ============================================
    echo   SUCCESS
    echo   Railway dev will redeploy in ~90 seconds.
    echo   Test at: https://web-production-17a78.up.railway.app
    echo ============================================
) else (
    echo [ERROR] Push failed. Token may have expired.
    echo Get a new one: https://github.com/settings/tokens/new
    echo Then: setx GH_TOKEN "ghp_yourNewToken"
)

echo.
pause
