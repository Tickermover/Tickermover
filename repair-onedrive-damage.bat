@echo off
REM Repairs OneDrive-induced damage to the HTML templates.
REM Safe to run any time — it's a no-op if everything is already healthy.
cd /d "%~dp0"
python repair-onedrive-damage.py
pause
