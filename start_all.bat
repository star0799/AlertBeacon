@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [ERROR] 找不到 venv python: "%PY%"
  pause
  exit /b 1
)

set "NGROK=%~dp0ngrok.exe"
if not exist "%NGROK%" (
  echo [ERROR] 找不到 ngrok.exe: "%NGROK%"
  pause
  exit /b 1
)

rem 
start "LINE Bot Server" cmd /k ""%PY%" "%~dp0bot_server.py""
start "Stock Monitor"  cmd /k ""%PY%" "%~dp0monitor_linebot.py""
start "ngrok"          cmd /k ""%NGROK%" http 5000"

exit /b 0
