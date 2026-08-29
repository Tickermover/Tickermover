@echo off
REM ════════════════════════════════════════════════════════════════
REM  promote-to-prod.bat — merge dev into main and push
REM ════════════════════════════════════════════════════════════════
REM  Use this AFTER you've tested your changes on web-production-17a78.up.railway.app
REM  and you're confident they're ready for production.
REM
REM  What it does:
REM    1. Verifies you're on 'dev' and the working tree is clean
REM    2. Pulls latest origin/main and origin/dev
REM    3. Checks out main, merges dev (no fast-forward, so the merge
REM       is visible in history)
REM    4. Pushes main → Railway prod auto-deploys to tickermover.com
REM    5. Switches you back to dev
REM
REM  Requires: GH_TOKEN env var (same as push.bat)
REM ════════════════════════════════════════════════════════════════

setlocal EnableDelayedExpansion
set "PROJ=%~dp0.."
pushd "%PROJ%" || (echo [ERROR] cannot cd to project root & exit /b 1)

echo.
echo === TickerMover - Promote dev to prod ===
echo.

if "%GH_TOKEN%"=="" (
    echo [ERROR] GH_TOKEN not set. See scripts\push.bat for setup.
    popd & exit /b 1
)

git remote set-url origin https://Tickermover:%GH_TOKEN%@github.com/Tickermover/Tickermover.git
set "CLEAN_URL=https://github.com/Tickermover/Tickermover.git"

REM ── 1. Must be on dev ─────────────────────────────────────────────
for /f "delims=" %%B in ('git rev-parse --abbrev-ref HEAD') do set "BRANCH=%%B"
if /i not "%BRANCH%"=="dev" (
    echo [ERROR] You must be on the 'dev' branch to promote. Currently on: %BRANCH%
    echo         Switch first:  git checkout dev
    git remote set-url origin %CLEAN_URL%
    popd & exit /b 1
)

REM ── 2. Working tree must be clean ─────────────────────────────────
git diff --quiet && git diff --cached --quiet
if !ERRORLEVEL! NEQ 0 (
    echo [ERROR] Working tree is dirty. Commit or stash before promoting.
    git status -s
    git remote set-url origin %CLEAN_URL%
    popd & exit /b 1
)

REM ── 3. Pull latest of both branches ───────────────────────────────
echo === Fetching origin ===
git fetch origin
echo.
echo === Pulling latest dev ===
git pull --ff-only origin dev
if !ERRORLEVEL! NEQ 0 (
    echo [ERROR] Failed to fast-forward dev. Resolve manually.
    git remote set-url origin %CLEAN_URL%
    popd & exit /b 1
)

REM ── 4. Confirm before touching prod ───────────────────────────────
echo.
echo === Commits about to be merged into main ===
git log --oneline origin/main..dev
echo.
set /p "CONFIRM=Type YES to merge dev -> main and deploy to tickermover.com: "
if /i not "!CONFIRM!"=="YES" (
    echo Cancelled.
    git remote set-url origin %CLEAN_URL%
    popd & exit /b 0
)

REM ── 5. Merge ──────────────────────────────────────────────────────
echo.
echo === Switching to main ===
git checkout main
git pull --ff-only origin main
if !ERRORLEVEL! NEQ 0 (
    echo [ERROR] Failed to fast-forward main. Resolve manually.
    git checkout dev
    git remote set-url origin %CLEAN_URL%
    popd & exit /b 1
)

echo === Merging dev into main (no fast-forward) ===
git merge --no-ff dev -m "Promote dev -> main"
if !ERRORLEVEL! NEQ 0 (
    echo [ERROR] Merge conflict. Resolve manually, then 'git commit', then re-run.
    git remote set-url origin %CLEAN_URL%
    popd & exit /b 1
)

REM ── 6. Push main ──────────────────────────────────────────────────
echo === Pushing main ===
git push origin main
set "PUSH_RC=!ERRORLEVEL!"

REM ── 7. Switch back to dev ─────────────────────────────────────────
git checkout dev
git remote set-url origin %CLEAN_URL%

echo.
if !PUSH_RC! EQU 0 (
    echo ================================================
    echo  PROMOTED. main is now updated on GitHub.
    echo  Railway prod should auto-deploy.
    echo  Verify at: https://tickermover.com
    echo  You're back on 'dev'.
    echo ================================================
) else (
    echo [ERROR] Push of main failed. Token may have expired.
    echo Locally main was merged but not pushed. Run:
    echo     git checkout main
    echo     git push origin main
    echo after fixing the token.
)

popd
echo.
pause
