# AlertBeacon 開發環境與元件整理

目的：把目前這個專案在本機會用到的程式元件、設定檔、啟動方式整理起來，之後換電腦可依此快速還原環境。

## 1. 專案元件（會跑哪些程式）

- `bot_server.py`
  - Flask 本機服務（預設 `http://127.0.0.1:5000`）。
  - LINE webhook / 推播（Costco 與 Cruise 兩個頻道）。
  - Cruise 指令 API（給本機 curl 測試用）、付款連結、`/health` 健康檢查。
  - Cruise token 會寫入 `latest_tokens.json`（由 Token Sync 回灌）。

- `monitor_linebot.py`（Costco 監控）
  - 依 `monitors.json` 的設定定期抓頁面並推播到 Costco LINE 群。
  - 會寫 heartbeat：`state/heartbeat_costco.json`

- `monitor_cruise_daemon.py`（Cruise 監控）
  - 依 `monitors_cruise.json` 定期輪詢 Cruise 後端的艙房狀態並推播到 Cruise LINE 群。
  - 會寫 heartbeat：`state/heartbeat_cruise_daemon.json`
  - 會呼叫 `bot_server.py` 的 `/cruise/tokens` 讀寫 token（必要時 `/cruise/tokens/clear`）。

- `watchdog_daemon.py`（存活監控/告警）
  - 依 `features.json` 監控哪些元件應該在跑。
  - 用 `http://127.0.0.1:5000/health` 判斷 `bot_server` 存活。
  - 讀 `state/heartbeat_*.json` 判斷各 daemon 是否正常更新 heartbeat。
  - 讀 `http://127.0.0.1:4040/api/tunnels` 判斷 ngrok 是否正常。

- `ngrok.exe`
  - 把本機 5000 port 對外公開，讓 LINE webhook 或付款連結可從外部打到本機。

## 2. 系統/軟體需求

- Windows（目前專案的啟動腳本為 `.bat`）。
- Python 3.13（目前工作環境為 3.13.11）。
- 建議使用虛擬環境：`.venv`

## 3. Python 套件（requirements）

本專案建議用 `.venv` 安裝依賴。套件清單以 `requirements.txt` 為準。

安裝步驟（PowerShell）：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\pip.exe install -r requirements.txt
```

若你會用到 Playwright（部分 Costco/自動化場景），需要額外下載瀏覽器：

```powershell
.\.venv\Scripts\python.exe -m playwright install
```

## 4. 環境變數（.env）

此專案會在啟動時 `load_dotenv()`，所以會讀取 repo 根目錄的 `.env`。

不要把 `.env` commit 到 git；換機時用安全方式備份/搬移。

必要/常用變數名稱：

- Costco LINE
  - `LINE_COSTCO_CHANNEL_ACCESS_TOKEN`
  - `LINE_COSTCO_CHANNEL_SECRET`

- Cruise LINE
  - `LINE_CRUISE_CHANNEL_ACCESS_TOKEN`
  - `LINE_CRUISE_CHANNEL_SECRET`
  - `CRUISE_ADMIN_KEY`（本機 curl 測試/管理用 header key）
  - `PUBLIC_BASE_URL`（產生對外付款連結用的 base URL；若用 ngrok，通常會填 ngrok 網域）

- Telegram（只有 `monitor_tg.py` 需要）
  - `TELEGRAM_BOT_TOKEN`
  - `TELEGRAM_CHAT_ID`

## 5. 設定檔與狀態檔（換機要搬哪些）

重要：這些檔案多半含有個資/Token/營運設定，請勿外流；也不要 commit 到 git。

- 功能開關：`features.json`
  - `bot_server` / `costco` / `cruise_daemon` / `ngrok`（true 才啟動/監控）

- 監控清單：
  - `monitors.json`（Costco）
  - `monitors_cruise.json`（Cruise）

- LINE 使用者清單（用於推播目標）：
  - `users.json`（Costco）
  - `users_cruise.json`（Cruise）

- Cruise token 快取：
  - `latest_tokens.json`

- `state/`（建議整個資料夾搬走）
  - `state/pay_links.json`（付款連結狀態）
  - `state/private_people.json`（乘客/緊急聯絡人資料）
  - `state/cruise_admins.json`（管理員）
  - `state/heartbeat_*.json`（watchdog 監控用）

## 6. Token Sync（Cruise 登入後自動回灌 token）

目前使用使用者腳本把官網登入回傳的 token 推到本機：

- 腳本：`scripts/sdc_token_sync.user.js`
- 會 POST 到：`http://127.0.0.1:5000/cruise/tokens`

換機後要點：

- 先確定 `bot_server.py` 在本機 5000 有跑起來。
- 再用瀏覽器安裝/啟用該 user script，並到 SDC 官網手動登入一次，讓 token 回灌。

## 7. 啟動方式

建議使用 `run_all.bat`（會依 `features.json` 自動決定要啟哪些元件）：

```bat
run_all.bat
```

LINE webhook（在 LINE Developers 後台設定）：

- Costco webhook URL：`{PUBLIC_BASE_URL}/callback/costco`
- Cruise webhook URL：`{PUBLIC_BASE_URL}/callback/cruise`

若你用 ngrok：

- `run_all.bat` 會啟動 `ngrok.exe http 5000`，ngrok 會產生一組對外 URL。
- 你需要把該 URL 更新到 `PUBLIC_BASE_URL`，並同步更新 LINE Developers 的 webhook URL。

常見檢查：

- 健康檢查：`curl http://127.0.0.1:5000/health`
- 路由列表：`curl http://127.0.0.1:5000/_routes`

## 8. 換機 Checklist（最短路徑）

1. 安裝 Python 3.13.x
2. `git clone` 專案
3. 建立 `.venv`，安裝 `requirements.txt`
4. 搬移 `.env`（或用安全管道重建）
5. 搬移 `features.json`、`monitors*.json`、`users*.json`、`state/`、（必要時）`latest_tokens.json`
6. 執行 `run_all.bat`
7. 進 SDC 官網手動登入一次，確認 Token Sync 已回灌（`/cruise/tokens` 有值）
