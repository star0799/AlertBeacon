@echo off
setlocal EnableExtensions EnableDelayedExpansion
goto :main

:feature
rem usage: call :feature cruise CRUISE
set "NAME=%~1"
set "VAR=%~2"

set "VAL=1"
if exist "features.json" (
  findstr /i /c:"\"%NAME%\": false" "features.json" >nul 2>&1 && set "VAL=0"
)

set "%VAR%=%VAL%"
exit /b 0

:main

rem --- always run from this .bat folder ---
chcp 65001 >nul
set "ROOT=%~dp0"
cd /d "%ROOT%"

if not exist "logs" mkdir "logs"

rem --- find python (prefer .venv / venv) using ABSOLUTE path ---
set "PY=python"
if exist "%ROOT%.venv\Scripts\python.exe" set "PY=%ROOT%.venv\Scripts\python.exe"
if exist "%ROOT%venv\Scripts\python.exe"  set "PY=%ROOT%venv\Scripts\python.exe"

rem --- verify python exists ---
if /i "%PY%"=="python" goto :check_python_in_path
if not exist "%PY%" (
  echo [ERR] Python not found: "%PY%"
  pause
  exit /b 1
)
goto :after_python_check

:check_python_in_path
where python >nul 2>&1
if errorlevel 1 (
  echo [ERR] Python not found in PATH.
  echo       Please install Python or create venv (.venv or venv).
  pause
  exit /b 1
)

:after_python_check

set "PORT=5000"
set "START_BOT=0"

call :feature costco  COSTCO
call :feature cruise_daemon CRUISE_DAEMON
call :feature ngrok   NGROK
call :feature bot_server BOT_SERVER

echo COSTCO=%COSTCO% CRUISE_DAEMON=%CRUISE_DAEMON% BOT_SERVER=%BOT_SERVER% NGROK=%NGROK%
echo PY=%PY%
echo.

if "%BOT_SERVER%"=="1" set "START_BOT=1"

if "%START_BOT%"=="1" (
  start "[BOT] bot_server" cmd /k ""%PY%" "%ROOT%bot_server.py""
) else (
  echo [WARN] bot_server not started because bot_server disabled or features read failed.
)

if "%NGROK%"=="1" (
  if exist "%ROOT%ngrok.exe" (
    start "[NGROK]" cmd /k ""%ROOT%ngrok.exe" http %PORT%"
  ) else (
    echo [WARN] NGROK enabled but ngrok.exe not found at: "%ROOT%ngrok.exe"
  )
)

if "%COSTCO%"=="1" (
  start "[MON] linebot" cmd /k ""%PY%" "%ROOT%monitor_linebot.py""
)

if "%CRUISE_DAEMON%"=="1" (
  start "[MON] cruise_daemon" cmd /k ""%PY%" "%ROOT%monitor_cruise_daemon.py""
)

if exist "%ROOT%watchdog_daemon.py" (
  start "[MON] watchdog" cmd /k ""%PY%" "%ROOT%watchdog_daemon.py""
)

echo.
echo Done. Check logs\*.log if something didn't start.
pause
exit /b 0
