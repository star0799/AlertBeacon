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

rem --- stop only processes launched from this AlertBeacon project ---
set "STOP_SCRIPT=%ROOT%scripts\stop_alertbeacon_processes.ps1"
if not exist "%STOP_SCRIPT%" (
  echo [ERR] Restart helper not found: "%STOP_SCRIPT%"
  pause
  exit /b 1
)

echo [RESTART] Stopping existing AlertBeacon services...
set "ALERTBEACON_RESTART_ROOT=%ROOT%"
set "ALERTBEACON_RESTART_PYTHON=%PY%"
powershell -NoProfile -ExecutionPolicy Bypass -File "%STOP_SCRIPT%"
if errorlevel 1 (
  echo [ERR] Existing services could not be stopped safely. New services were not started.
  pause
  exit /b 1
)

set "RESTART_EPOCH=0"
for /f %%T in ('powershell -NoProfile -Command "[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()"') do set "RESTART_EPOCH=%%T"
echo [RESTART] Existing AlertBeacon services stopped.
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
echo [VERIFY] Waiting for restarted services...
set "VERIFY_FAILED=0"

if "%START_BOT%"=="1" (
  powershell -NoProfile -Command "$deadline=(Get-Date).AddSeconds(25); $ok=$false; do { try { $h=Invoke-RestMethod -Uri 'http://127.0.0.1:5000/health' -TimeoutSec 2; if ($h.ok -eq $true -and $h.service -eq 'bot_server') { $ok=$true; break } } catch {}; Start-Sleep -Milliseconds 500 } while ((Get-Date) -lt $deadline); if ($ok) { Write-Host ('[OK] bot_server pid=' + $h.pid); exit 0 }; Write-Host '[WARN] bot_server health check failed'; exit 1"
  if errorlevel 1 set "VERIFY_FAILED=1"
)

if "%CRUISE_DAEMON%"=="1" (
  powershell -NoProfile -Command "$deadline=(Get-Date).AddSeconds(25); $ok=$false; do { try { $hb=Get-Content -LiteralPath '%ROOT%state\heartbeat_cruise_daemon.json' -Raw | ConvertFrom-Json; if ($hb.status -eq 'running' -and [double]$hb.ts -ge [double]%RESTART_EPOCH%) { $ok=$true; break } } catch {}; Start-Sleep -Milliseconds 500 } while ((Get-Date) -lt $deadline); if ($ok) { Write-Host ('[OK] cruise_daemon heartbeat=' + $hb.ts_str); exit 0 }; Write-Host '[WARN] cruise_daemon heartbeat check failed'; exit 1"
  if errorlevel 1 set "VERIFY_FAILED=1"
)

echo.
if "%VERIFY_FAILED%"=="1" (
  echo Restart completed with verification warnings. Check the service windows.
  powershell -NoProfile -Command "Start-Sleep -Seconds 3"
  exit /b 1
)

echo Restart complete. Check the service windows for details.
powershell -NoProfile -Command "Start-Sleep -Seconds 3"
exit /b 0
