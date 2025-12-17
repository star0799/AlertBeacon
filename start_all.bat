@echo off
chcp 65001 >nul
cd /d "%~dp0"

set PY="%~dp0.venv\Scripts\python.exe"
if not exist %PY% (
  echo [ERROR] 找不到 venv python: %PY%
  pause
  exit /b 1
)

set NGROK="%~dp0ngrok.exe"
if not exist %NGROK% (
  echo [ERROR] 找不到 ngrok.exe: %NGROK%
  pause
  exit /b 1
)

start "LINE Bot Server" %PY% bot_server.py
start "Stock Monitor" %PY% monitor_linebot.py
start "ngrok" %NGROK% http 5000

echo.
echo 已啟動：bot_server + monitor_linebot + ngrok
pause
