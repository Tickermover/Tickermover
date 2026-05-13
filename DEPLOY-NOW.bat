@echo off
REM ───────────────────────────────────────────────────────────────────────
REM ONE-CLICK DEPLOY — Pushes the audit fixes through dev to prod
REM ───────────────────────────────────────────────────────────────────────
REM   Fixes included:
REM     1. landing.html  — restored truncated tail (was breaking page load on prod)
REM     2. dashboard.html — restored truncated tail + ToS-once + new logo + retired signup link
REM     3. infographics templates — stripped null padding
REM     4. .gitignore — exclude .bak files from future deploys
REM ───────────────────────────────────────────────────────────────────────

cd /d "%~dp0"

echo.
echo === Step 1: Show what will be committed ===
git status --short
echo.
echo Press Ctrl-C now to abort, or
pause

echo.
echo === Step 2: Clean up stale .bak files ===
git rm -f *.bak 2>NUL
git rm -f *-bak 2>NUL
git rm -f templates\*.bak 2>NUL
git rm -f templates\*-bak 2>NUL
git rm -f static\*.bak 2>NUL
git rm -f static\*-bak 2>NUL
del /q *.bak 2>NUL
del /q *-bak 2>NUL
del /q templates\*.bak 2>NUL
del /q templates\*-bak 2>NUL
del /q static\*.bak 2>NUL
del /q static\*-bak 2>NUL

echo.
echo === Step 3: Stage all fixes ===
git add .gitignore
git add templates\landing.html
git add templates\dashboard.html
git add templates\infographics.html
git add templates\earnings_infographic.html
git add templates\earnings_tearsheet.html
git add data\model_portfolio.json
git add cleanup-bak-files.bat
git add DEPLOY-NOW.bat

echo.
echo === Step 4: Show staged changes ===
git status --short

echo.
echo Press Ctrl-C if anything looks wrong. Otherwise...
pause

echo.
echo === Step 5: Commit ===
git commit -m "fix: restore truncated templates + persist ToS consent + new modal logo + clean .bak clutter"

echo.
echo === Step 6: Push to dev (Railway dev environment) ===
call push-to-dev.bat

echo.
echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
echo  Dev deploy initiated. Wait ~60 seconds, then verify dev URL.
echo  When dev looks correct, run: push-to-prod.bat
echo ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
pause
