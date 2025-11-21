import os
import time
import json
import requests
from bs4 import BeautifulSoup
from linebot import LineBotApi
from linebot.models import TextSendMessage
from datetime import datetime
from dotenv import load_dotenv
from filelock import FileLock

MONITOR_FILE = "monitors.json"
USERS_FILE = "users.json"

load_dotenv()
line_bot_api = LineBotApi(os.getenv("LINE_CHANNEL_ACCESS_TOKEN"))

LOG_FOLDER = "logs"
os.makedirs(LOG_FOLDER, exist_ok=True)


# ------------------------------------------------------
# 基本 JSON 工具（不加鎖）
# ------------------------------------------------------
def read_json(path: str, default):
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            return default
        return json.loads(content)
    except Exception as e:
        log(f"⚠️ 讀取 {path} 失敗：{e}")
        return default


def write_json(path: str, data):
    try:
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception as e:
        log(f"⚠️ 寫入 {path} 失敗：{e}")


# ------------------------------------------------------
# monitors.json 專用：一次 read-modify-write（有檔案鎖）
# ------------------------------------------------------
def update_monitors(mutator):
    lock = FileLock(MONITOR_FILE + ".lock")
    with lock:
        monitors = read_json(MONITOR_FILE, [])
        mutator(monitors)
        write_json(MONITOR_FILE, monitors)
        return monitors


# ------------------------------------------------------
# 日誌
# ------------------------------------------------------
def log(msg: str):
    today = datetime.now().strftime("%Y-%m-%d")
    filename = os.path.join(LOG_FOLDER, f"{today}.log")
    with open(filename, "a", encoding="utf-8") as f:
        f.write(msg + "\n")
    print(msg)


# ------------------------------------------------------
# 共用工具
# ------------------------------------------------------
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


def is_in_stock(url: str) -> bool:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        return "缺貨" not in soup.get_text()
    except Exception as e:
        log(f"⚠️ {url} 網路錯誤: {e}")
        return False


def push_all(text: str):
    users = read_json(USERS_FILE, [])
    for u in users:
        try:
            line_bot_api.push_message(u, TextSendMessage(text=text))
        except Exception as e:
            log(f"❌ 推播給 {u} 失敗：{e}")


def calc_alive(m: dict, now_ts: float) -> bool:
    last_ts = float(m.get("last_check_ts") or 0)
    interval = int(m.get("interval", 180))
    timeout = max(interval * 3, 600)
    return (now_ts - last_ts) <= timeout


# ------------------------------------------------------
# 主迴圈
# ------------------------------------------------------
def main():
    log("📡 監控程式啟動")

    while True:
        # 先拿 snapshot，避免在持有 lock 時做網路 I/O
        monitors_snapshot = read_json(MONITOR_FILE, [])
        now_ts = time.time()

        status_updates = {}  # url -> { last_in_stock, last_check_ts, last_check }
        any_checked = False

        for m in monitors_snapshot:
            url = m["url"]
            interval = int(m.get("interval", 180))
            last_ts = float(m.get("last_check_ts") or 0)

            # 還沒到排程時間就跳過
            if now_ts - last_ts < interval:
                continue

            any_checked = True
            old_status = m.get("last_in_stock", None)
            in_stock = is_in_stock(url)

            log(
                f"[{datetime.now().strftime('%H:%M:%S')}] "
                f"{url} → {'有貨' if in_stock else '缺貨'}"
            )

            # 缺 → 有 才推播
            if old_status is False and in_stock is True:
                name = m.get("name", "未命名商品")
                push_all(f"📦 補貨啦！\n{name}\n{url}")

            status_updates[url] = {
                "last_in_stock": in_stock,
                "last_check_ts": now_ts,
                "last_check": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }

        # 用單一 lock 合併寫回，順便更新 alive
        def mut(monitors_list):
            for m in monitors_list:
                url = m["url"]
                if url in status_updates:
                    m.update(status_updates[url])
                # 不論這輪有沒有檢查，都更新 alive 狀態
                m["alive"] = calc_alive(m, now_ts)

        update_monitors(mut)

        time.sleep(1)


if __name__ == "__main__":
    main()
