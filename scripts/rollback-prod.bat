@echo off
REM ════════════════════════════════════════════════════════════════
REM  rollback-prod.bat — emergency revert of last main commit
REM ════════════════════════════════════════════════════════════════
REM  Use ONLY when prod is broken and you need to roll back NOW.
REM  Reverts the most recent commit on main and pushes immediately.
REM  Railway prod auto-deploys the revert.
REM
REM  After running, the bad change is still on dev — fix it there
REM  and re-promote when ready.
REM ════════════════════════════════════════════════════════════════

setlocal EnableDelayedExpansion
set "PROJ=%~dp0.."
pushd "%PROJ%" || (echo [ERROR] cannot cd to project root & exit /b 1)

echo.
echo === TickerMover - PROD ROLLBACK ===
echo.

if "%GH_TOKEN%"=="" (
    echo [ERROR] GH_TOKEN not set.
    popd & exit /b 1
)

git remote set-url origin https://digitalqueryai:%GH_TOKEN%@github.com/digitalqueryai/USAstockdashboard-.git
set "CLEAN_URL=https://github.com/digitalqueryai/USAstockdashboard-.git"

git checkout main
git pull --ff-only origin main

echo.
echo === Last 5 commits on main ===
git log --oneline -5
echo.
echo This will revert the TOP commit on main.
set /p "CONFIRM=Type ROLLBACK to proceed: "
if /i not "!CONFIRM!"=="ROLLBACK" (
    echo Cancelled.
    git checkout dev
    git remote set-url origin %CLEAN_URL%
    popd & exit /b 0
)

git revert --no-edit HEAD
if !ERRORLEVEL! NEQ 0 (
    echo [ERROR] Revert failed. Resolve manually.
    git remote set-url origin %CLEAN_URL%
    popd & exit /b 1
)

git push origin main
set "PUSH_RC=!ERRORLEVEL!"

git checkout dev
git remote set-url origin %CLEAN_URL%

echo.
if !PUSH_RC! EQU 0 (
    echo ================================================
    echo  ROLLED BACK. Railway prod will redeploy in ~2min.
    echo  Verify at https://tickermover.com
    echo ================================================
) else (
    echo [ERROR] Push failed. Revert is local only.
)

popd
echo.
pause
