@echo off
REM ════════════════════════════════════════════════════════════════
REM  repair-git.bat — clean up stale git lock + verify branch state
REM ════════════════════════════════════════════════════════════════
REM  Run this if a git command ever says "Another git process seems
REM  to be running" or "Unable to create '.git/index.lock'".
REM  Safe to run any time — it only deletes EMPTY lock files.
REM ════════════════════════════════════════════════════════════════

setlocal
set "PROJ=%~dp0.."
pushd "%PROJ%" || (echo [ERROR] cannot cd to project root & exit /b 1)

echo.
echo === TickerMover - Git Repair ===
echo Folder: %CD%
echo.

REM ── 1. Remove stale lock files ────────────────────────────────────
if exist ".git\index.lock" (
    REM Only delete if the lock is empty (size 0) — non-empty means a real
    REM git process might be running.
    for %%F in (".git\index.lock") do set "LOCK_SIZE=%%~zF"
    if "%LOCK_SIZE%"=="0" (
        del /f /q ".git\index.lock"
        echo [OK] Removed stale empty .git\index.lock
    ) else (
        echo [WARN] .git\index.lock is not empty (size %LOCK_SIZE%) — a real git
        echo        process may still be running. Close any open git GUI / VS Code
        echo        terminals and re-run this script.
        popd & exit /b 1
    )
) else (
    echo [OK] No .git\index.lock to remove
)

if exist ".git\HEAD.lock" (
    del /f /q ".git\HEAD.lock"
    echo [OK] Removed .git\HEAD.lock
)

REM ── 2. Verify branches exist ──────────────────────────────────────
echo.
echo === Branches ===
git branch
echo.

REM ── 3. Verify HEAD is valid ───────────────────────────────────────
echo === HEAD ===
git rev-parse --abbrev-ref HEAD
git rev-parse --short HEAD
echo.

REM ── 4. Refresh index ──────────────────────────────────────────────
echo === Refreshing index ===
git update-index --refresh >nul 2>&1
echo [OK] Index refreshed
echo.

REM ── 5. Working-tree status ────────────────────────────────────────
echo === git status ===
git status -sb

popd
echo.
echo Done.
pause
