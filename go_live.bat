@echo off
echo ============================
echo   直播复盘系统 v2.0
echo ============================

:: Check WSL
wsl --status >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] WSL 未运行, 请先打开 WSL
    pause
    exit /b 1
)

:: Start backend in WSL
echo [1/2] 启动后端...
wsl -d Ubuntu-24.04 bash -c "cd /mnt/d/workbuddy/2026-07-10-22-52-13/live-replay && venv/bin/python app.py --port 9999"

:: Open browser
echo [2/2] 打开面板...
start http://localhost:9999

pause
