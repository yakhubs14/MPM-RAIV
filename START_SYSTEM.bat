@echo off
TITLE MPM-RAIV SYSTEM LAUNCHER
COLOR 0A

echo ===============================================
echo      MPM-RAIV AUTONOMOUS SYSTEM STARTUP
echo ===============================================
echo.
echo [1] Checking for Python...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo Python not found! Please install Python.
    pause
    exit
)

echo [2] Checking for Cloudflared...
if not exist cloudflared.exe (
    echo cloudflared.exe not found! Please put it in this folder.
    pause
    exit
)

echo [3] Launching Auto-Run Script...
echo     - Starting Camera Server
echo     - Starting Cloud Tunnel
echo     - Syncing Link to Firebase...
echo.

python auto_run.py

pause
