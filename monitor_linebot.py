import os
import time
import json
import requests
from requests.exceptions import RequestException
from bs4 import BeautifulSoup
from linebot import LineBotApi
from linebot.models import TextSendMessage
from datetime import datetime
from dotenv import load_dotenv
from filelock import FileLock

MONITOR_FILE = "monitors.json"
USERS_FILE = "users.json"

LOG_FOLDER = "logs"
os.makedirs(LOG_FOLDER, exist_ok=True)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# ------------------------------------------------------
# 初始化 LINE
# ------------------------------------------------------
load_dotenv()
LINE_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
if not LINE_TOKEN:
    raise RuntimeError("Missing LINE_CHANNEL_ACCESS_TOKEN in .env")
line_bot_api = LineBotApi(LINE_TOKEN)

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
# JSON 工具
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
        log(f"⚠️ 讀取 {path} 失敗：{type(e).__name__}: {e}")
        return default

def write_json(path: str, data):
    try:
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception as e:
        log(f"⚠️ 寫入 {path} 失敗：{type(e).__name__}: {e}")

def update_monitors(mutator):
    lock = FileLock(MONITOR_FILE + ".lock")
    with lock:
        monitors = read_json(MONITOR_FILE, [])
        mutator(monitors)
        write_json(MONITOR_FILE, monitors)
        return monitors

# ------------------------------------------------------
# 推播
# ------------------------------------------------------
def push_all(text: str):
    users = read_json(USERS_FILE, [])
    if not users:
        log("⚠️ users.json 內沒有任何使用者，無法推播")
        return

    for u in users:
        try:
            line_bot_api.push_message(u, TextSendMessage(text=text))
        except Exception as e:
            log(f"❌ 推播給 {u} 失敗：{type(e).__name__}: {e}")

# ------------------------------------------------------
# Alive 計算
# ------------------------------------------------------
def calc_alive(m: dict, now_ts: float) -> bool:
    last_ts = float(m.get("last_check_ts") or 0)
    interval = int(m.get("interval", 180))
    timeout = max(interval * 3, 600)
    return (now_ts - last_ts) <= timeout

# ------------------------------------------------------
# 庫存判斷（支援多種版型）
# ------------------------------------------------------
def _find_cart_button(soup: BeautifulSoup):
    """
    優先找 #add-to-cart-button（你新貼的版型），找不到再 fallback 找文字含「加入購物車」的 button。
    """
    btn = soup.select_one("#add-to-cart-button")
    if btn:
        return btn

    for b in soup.select("button"):
        if "加入購物車" in b.get_text(strip=True):
            return b

    return None

def is_in_stock(url: str) -> bool | None:
    """
    True  = 有貨
    False = 缺貨
    None  = 網路/解析問題（不要把 None 當缺貨，避免誤觸發 缺→有）
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=(5, 20))
        resp.raise_for_status()
    except RequestException as e:
        log(f"⚠️ 請求失敗：{type(e).__name__}: {e}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    cart_btn = _find_cart_button(soup)

    if not cart_btn:
        # 找不到加入購物車按鈕，通常就是缺貨或頁面沒完整載入
        return False

    # disabled / aria-disabled => 缺貨
    if cart_btn.has_attr("disabled"):
        return False
    if (cart_btn.get("aria-disabled") or "").lower() == "true":
        return False

    # 按鈕文字如果是缺貨也算缺貨（保險）
    if "缺貨" in cart_btn.get_text(strip=True):
        return False

    return True

def confirm_in_stock(url: str, tries: int = 3, delay: int = 3) -> bool:
    """
    防假補貨：多次確認，3 次裡至少 2 次 True 才當真有貨
    """
    ok = 0
    for _ in range(tries):
        r = is_in_stock(url)
        if r is True:
            ok += 1
        time.sleep(delay)
    return ok >= 2

# ------------------------------------------------------
# 主迴圈
# ------------------------------------------------------
def main():
    log("📡 監控程式啟動")

    while True:
        monitors_snapshot = read_json(MONITOR_FILE, [])
        now_ts = time.time()

        status_updates = {}  # url -> updated fields

        for m in monitors_snapshot:
            try:
                url = m["url"]
            except KeyError:
                continue

            interval = int(m.get("interval", 180))
            last_ts = float(m.get("last_check_ts") or 0)

            # 還沒到排程時間就跳過
            if now_ts - last_ts < interval:
                continue

            old_status = m.get("last_in_stock", None)
            result = is_in_stock(url)

            if result is None:
                # 網路錯誤：只更新 last_check，不更新 last_in_stock（避免假補貨）
                status_updates[url] = {
                    "last_check_ts": now_ts,
                    "last_check": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                continue

            in_stock = result

            log(
                f"[{datetime.now().strftime('%H:%M:%S')}] "
                f"{url} → {'有貨' if in_stock else '缺貨'}"
            )

            # 只有「缺 → 有」才推播（避免一直刷）
            if old_status is False and in_stock is True:
                name = m.get("name", "未命名商品")

                if confirm_in_stock(url):
                    push_all(f"📦 補貨啦！\n{name}\n{url}")
                else:
                    log("⚠️ 疑似秒補/快取抖動，confirm 未通過，不推播")

            status_updates[url] = {
                "last_in_stock": in_stock,
                "last_check_ts": now_ts,
                "last_check": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }

        # 合併寫回 + 更新 alive
        def mut(monitors_list):
            for item in monitors_list:
                url = item.get("url")
                if url and url in status_updates:
                    item.update(status_updates[url])
                item["alive"] = calc_alive(item, now_ts)

        update_monitors(mut)

        time.sleep(1)

if __name__ == "__main__":
    main()
# GPT DIFF