import os
import time
import json
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# 讀取 .env
load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Costco 商品網址
PRODUCT_URL = "https://www.costco.com.tw/Digital-Mobile/Mobile-Tablets/iPhone-Mobile-Phones/Apple-iPhone-17-512GB-Black/p/158010"

# 每幾秒檢查一次（180 秒 = 3 分鐘）
CHECK_INTERVAL_SECONDS = 180


def save_status(in_stock: bool):
    data = {
        "in_stock": in_stock,
        "last_update": time.strftime("%Y-%m-%d %H:%M:%S")
    }
    with open("status.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def is_in_stock() -> bool:
    """檢查 Costco 是否有貨：有回 True，沒有回 False"""

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
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


def send_tg_message(text: str):
    """發 Telegram 通知"""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
    }
    resp = requests.post(url, json=payload)
    if not resp.ok:
        print("❌ Telegram 傳送失敗:", resp.text)


def main():
    print("BOT_TOKEN =", BOT_TOKEN)
    print("CHAT_ID =", CHAT_ID)
    send_tg_message("🔍 Costco 商品監控啟動")
    print("🔍 Costco 商品監控啟動")
    print("商品網址:", PRODUCT_URL)

    last_in_stock = None

    while True:
        try:
            in_stock = is_in_stock()
            save_status(in_stock)

            print(time.strftime("[%Y-%m-%d %H:%M:%S]"),
                  "✅ 有貨" if in_stock else "❌ 缺貨")

            # 「缺貨 → 有貨」才通知
            if last_in_stock is False and in_stock is True:
                send_tg_message(f"📦 Costco 補貨啦！\n{PRODUCT_URL}")

            last_in_stock = in_stock

        except Exception as e:
            print("⚠️ 發生錯誤：", e)

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()