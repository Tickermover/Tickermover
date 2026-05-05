@echo off
REM ════════════════════════════════════════════════════════════════
REM  DEPRECATED — do not use this script
REM ════════════════════════════════════════════════════════════════
REM  The previous version of this file did:
REM     rmdir /s /q .git    (wiped your entire git history)
REM     git init             (fresh repo)
REM     git push --force     (clobbered GitHub)
REM
REM  That makes branch-based dev/prod IMPOSSIBLE because the 'dev'
REM  branch disappears every time. Use the proper scripts instead:
REM
REM     scripts\push.bat               — push current branch (dev)
REM     scripts\promote-to-prod.bat    — merge dev -> main and deploy
REM     scripts\rollback-prod.bat      — emergency prod revert
REM     scripts\repair-git.bat         — clean stale .git\index.lock
REM ════════════════════════════════════════════════════════════════

echo.
echo ===============================================================
echo  push_to_github.bat is deprecated.
echo.
echo  Use one of these instead:
echo     scripts\push.bat
echo     scripts\promote-to-prod.bat
echo     scripts\rollback-prod.bat
echo     scripts\repair-git.bat
echo.
echo  See docs\dev\DEV_ENV_SETUP.md for the full workflow.
echo ===============================================================
echo.
pause
