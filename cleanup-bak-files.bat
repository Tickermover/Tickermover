@echo off
REM ───────────────────────────────────────────────────────────────────────
REM Remove the 40 stale .bak files cluttering the repo + slowing Railway
REM deploys. They are already in .gitignore now, but the committed copies
REM still need to be removed from history.
REM ───────────────────────────────────────────────────────────────────────

cd /d "%~dp0"

echo === Removing stale .bak files from git ===
git rm -f *.bak 2>NUL
git rm -f *-bak 2>NUL
git rm -f templates\*.bak 2>NUL
git rm -f templates\*-bak 2>NUL
git rm -f static\*.bak 2>NUL
git rm -f static\*-bak 2>NUL

echo.
echo === Also removing local copies ===
del /q *.bak 2>NUL
del /q *-bak 2>NUL
del /q templates\*.bak 2>NUL
del /q templates\*-bak 2>NUL
del /q static\*.bak 2>NUL
del /q static\*-bak 2>NUL

echo.
echo === Status ===
git status

echo.
echo === Next steps ===
echo   git commit -m "chore: remove stale .bak files (now in .gitignore)"
echo   .\push-to-dev.bat       (deploy to Railway dev)
echo   .\push-to-prod.bat      (deploy to Railway prod)
echo.
pause
