@echo off
REM ════════════════════════════════════════════════════════════════
REM  push.bat — non-destructive push to current branch
REM ════════════════════════════════════════════════════════════════
REM  Replaces the old push_to_github.bat which wiped .git on every
REM  run. This script preserves history, supports branches, and
REM  refuses to push to main directly (you must promote via
REM  promote-to-prod.bat instead).
REM
REM  Usage:
REM     scripts\push.bat              — interactive (asks for message)
REM     scripts\push.bat "msg here"   — uses given commit message
REM
REM  Requires: GH_TOKEN env var (set once with:
REM     setx GH_TOKEN "ghp_yourTokenHere"
REM     then open a NEW terminal so the var loads)
REM ════════════════════════════════════════════════════════════════

setlocal EnableDelayedExpansion
set "PROJ=%~dp0.."
pushd "%PROJ%" || (echo [ERROR] cannot cd to project root & exit /b 1)

echo.
echo === TickerMover - Push to GitHub ===
echo Folder: %CD%
echo.

REM ── 1. Token check ────────────────────────────────────────────────
if "%GH_TOKEN%"=="" (
    echo [ERROR] GH_TOKEN environment variable is not set.
    echo   Create a new token at https://github.com/settings/tokens/new
    echo   with the 'repo' scope, then run:
    echo     setx GH_TOKEN "ghp_yourNewToken"
    echo   Open a new terminal and re-run this script.
    popd & exit /b 1
)

REM ── 2. Make sure remote uses token via env (no token in .git/config) ──
git remote set-url origin https://digitalqueryai:%GH_TOKEN%@github.com/digitalqueryai/USAstockdashboard-.git
REM ── Save tokenless URL for cleanup at the end ─────────────────────
set "CLEAN_URL=https://github.com/digitalqueryai/USAstockdashboard-.git"

REM ── 3. Branch check — refuse direct pushes to main ────────────────
for /f "delims=" %%B in ('git rev-parse --abbrev-ref HEAD') do set "BRANCH=%%B"
echo Current branch: %BRANCH%

if /i "%BRANCH%"=="main" (
    echo.
    echo [ERROR] You're on 'main'. This script refuses direct pushes to main
    echo         because it would deploy to tickermover.com immediately.
    echo         Switch to dev:    git checkout dev
    echo         Or to promote:    scripts\promote-to-prod.bat
    git remote set-url origin %CLEAN_URL%
    popd & exit /b 1
)

REM ── 4. Show what's about to be committed ──────────────────────────
echo.
echo === Changes to be committed ===
git add -A
git status -s
echo.

REM ── 5. Bail if nothing changed ────────────────────────────────────
git diff --cached --quiet
if !ERRORLEVEL! EQU 0 (
    echo [INFO] No changes to commit. Pushing existing commits anyway...
    goto :do_push
)

REM ── 6. Commit message ─────────────────────────────────────────────
if "%~1"=="" (
    set /p "MSG=Commit message: "
) else (
    set "MSG=%~1"
)
if "!MSG!"=="" (
    echo [ERROR] Empty commit message — aborting.
    git remote set-url origin %CLEAN_URL%
    popd & exit /b 1
)

git commit -m "!MSG!"
if !ERRORLEVEL! NEQ 0 (
    echo [ERROR] Commit failed.
    git remote set-url origin %CLEAN_URL%
    popd & exit /b 1
)

:do_push
echo.
echo === Pushing to origin/%BRANCH% ===
git push -u origin "%BRANCH%"
set "PUSH_RC=!ERRORLEVEL!"

REM ── 7. Strip token back out of .git/config ────────────────────────
git remote set-url origin %CLEAN_URL%

echo.
if !PUSH_RC! EQU 0 (
    echo ================================================
    echo  SUCCESS! Pushed to origin/%BRANCH%
    if /i "%BRANCH%"=="dev" (
        echo  Railway dev project should auto-deploy in ~90s.
        echo  Test at: https://web-production-17a78.up.railway.app
    )
    echo ================================================
) else (
    echo [ERROR] Push failed. Token may have expired.
    echo Create a new one at https://github.com/settings/tokens/new
    echo Then: setx GH_TOKEN "ghp_yourNewToken"
)

popd
echo.
pause
