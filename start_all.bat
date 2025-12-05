@echo off
rem ==== 1. 切到專案資料夾（用批次檔所在路徑）====
cd /d "%~dp0"

rem ==== 2. 啟動虛擬環境 ====
call .venv\Scripts\activate.bat

rem ==== 3. 開三個獨立視窗執行 ====
start "LINE Bot Server" python bot_server.py
start "Stock Monitor" python monitor_linebot.py
start "ngrok" ngrok http 5000

echo.
echo 已啟動：bot_server + monitor_linebot + ngrok
echo 關閉時，請手動關掉各視窗即可結束服務。
pause