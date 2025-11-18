import os
import time
import json
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from linebot import LineBotApi
from linebot.models import TextSendMessage

# 讀取 .env
load_dotenv()
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)

USERS_FILE = "users.json"

# Costco 商品網址
PRODUCT_URL = "https://www.costco.com.tw/Digital-Mobile/Mobile-Tablets/iPhone-Mobile-Phones/Apple-iPhone-17-512GB-Black/p/158010"

# 每 3 分鐘檢查一次
CHECK_INTERVAL_SECONDS = 180


def get_all_users():
    """讀取 users.json，沒有則回空 list"""
    if not os.path.exists(USERS_FILE):
        return []
    return json.load(open(USERS_FILE, "r", encoding="utf-8"))


def push_to_all_users(text: str):
    """LINE 多人推播"""
    users = get_all_users()
    print(f"📨 正在推播給 {len(users)} 個使用者")

    for uid in users:
        try:
            line_bot_api.push_message(uid, TextSendMessage(text=text))
        except Exception as e:
            print(f"❌ 推播給 {uid} 失敗：", e)


def save_status(in_stock: bool):
    data = {
        "in_stock": in_stock,
        "last_update": time.strftime("%Y-%m-%d %H:%M:%S")
    }
    json.dump(data, open("status.json", "w", encoding="utf-8"), indent=2, ensure_ascii=False)


def is_in_stock() -> bool:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
            " AppleWebKit/537.36 (KHTML, like Gecko)"
            " Chrome/120.0.0.0 Safari/537.36"
        )
    }

    try:
        resp = requests.get(PRODUCT_URL, headers=headers, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        print("⚠️ 網路錯誤:", e)
        return False

    soup = BeautifulSoup(resp.text, "html.parser")
    return "缺貨" not in soup.get_text()


def main():
    push_to_all_users("🔍 Costco 商品監控啟動")
    print("🔍 Costco 商品監控啟動")

    last_in_stock = None

    while True:
        try:
            in_stock = is_in_stock()
            save_status(in_stock)

            print(time.strftime("[%Y-%m-%d %H:%M:%S]"),
                  "✅ 有貨" if in_stock else "❌ 缺貨")

            if last_in_stock is False and in_stock is True:
                push_to_all_users(f"📦 Costco 補貨啦！快去搶🔥\n{PRODUCT_URL}")

            last_in_stock = in_stock

        except Exception as e:
            print("⚠️ 發生錯誤：", e)

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
