import os, time, json, requests
from bs4 import BeautifulSoup
from linebot import LineBotApi
from linebot.models import TextSendMessage
from datetime import datetime

MONITOR_FILE = "monitors.json"
USERS_FILE = "users.json"

from dotenv import load_dotenv
load_dotenv()

line_bot_api = LineBotApi(os.getenv("LINE_CHANNEL_ACCESS_TOKEN"))

LOG_FOLDER = "logs"
os.makedirs(LOG_FOLDER, exist_ok=True)

def log(msg):
    today = datetime.now().strftime("%Y-%m-%d")
    filename = f"{LOG_FOLDER}/{today}.log"
    with open(filename, "a", encoding="utf-8") as f:
        f.write(msg + "\n")
    print(msg)

def load_json(path):
    if not os.path.exists(path):
        return []
    try:
        return json.load(open(path, "r", encoding="utf-8"))
    except:
        return []

def is_in_stock(url):
    headers = {
        "User-Agent": "Mozilla/5.0"
    }
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        return "缺貨" not in soup.get_text()
    except Exception as e:
        log(f"⚠️ {url} 網路錯誤: {e}")
        return False

def push_all(text):
    users = load_json(USERS_FILE)
    for u in users:
        try:
            line_bot_api.push_message(u, TextSendMessage(text=text))
        except Exception as e:
            log(f"❌ 推播給 {u} 失敗：{e}")

def main():

    log("📡 監控程式啟動")

    last_check = {}  # 每個 URL 的上次檢查時間（紀錄格式： { url: timestamp }）

    while True:
        monitors = load_json(MONITOR_FILE)

        for m in monitors:
            url = m["url"]
            interval = m["interval"]

            # 是否到了該檢查的時間
            if url not in last_check or time.time() - last_check[url] >= interval:

                in_stock = is_in_stock(url)

                log(f"[{datetime.now().strftime('%H:%M:%S')}] {url} → {'有貨' if in_stock else '缺貨'}")

                # 補貨通知（缺 → 有）
                if m["last_in_stock"] is False and in_stock is True:
                    push_all(f"📦 補貨啦！\n{url}")

                # 更新狀態
                m["last_in_stock"] = in_stock

                last_check[url] = time.time()

        # 儲存更新後的監控清單
        json.dump(monitors, open(MONITOR_FILE, "w", encoding="utf-8"), indent=2, ensure_ascii=False)

        time.sleep(1)


if __name__ == "__main__":
    main()