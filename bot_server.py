from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

import os
import sys
import json
import re
import time
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from dotenv import load_dotenv
from filelock import FileLock

load_dotenv()
app = Flask(__name__)

COSTCO_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_COSTCO_CHANNEL_ACCESS_TOKEN")
COSTCO_CHANNEL_SECRET = os.getenv("LINE_COSTCO_CHANNEL_SECRET")

costco_line_bot_api = LineBotApi(COSTCO_CHANNEL_ACCESS_TOKEN)
costco_handler = WebhookHandler(COSTCO_CHANNEL_SECRET)

# Cruise
CRUISE_TOKEN = os.getenv("LINE_CRUISE_CHANNEL_ACCESS_TOKEN")
CRUISE_SECRET = os.getenv("LINE_CRUISE_CHANNEL_SECRET")
if not CRUISE_TOKEN or not CRUISE_SECRET:
    print("Cruise LINE channel token/secret 缺失，請檢查 .env 設定")
    sys.exit(1)

cruise_line_bot_api = LineBotApi(CRUISE_TOKEN)
cruise_handler = WebhookHandler(CRUISE_SECRET)

USERS_FILE = "users.json"
MONITORS_FILE = "monitors.json"


# ------------------------------------------------------
# 基本 JSON 工具（不加鎖的版本）
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
        print(f"⚠️ 讀取 {path} 失敗：{e}")
        return default


def write_json(path: str, data):
    try:
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception as e:
        print(f"⚠️ 寫入 {path} 失敗：{e}")


# ------------------------------------------------------
# monitors.json 專用：一次 read-modify-write（有檔案鎖）
# ------------------------------------------------------
def update_monitors(mutator):
    """mutator(monitors_list) 會在同一個 lock 裡讀 / 改 / 寫 monitors.json"""
    lock = FileLock(MONITORS_FILE + ".lock")
    with lock:
        monitors = read_json(MONITORS_FILE, [])
        mutator(monitors)
        write_json(MONITORS_FILE, monitors)
        return monitors


# ------------------------------------------------------
# 時間 / alive 判斷
# ------------------------------------------------------
def now_ts() -> float:
    return time.time()


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def calc_alive(m: dict, now: float | None = None) -> bool:
    """根據 last_check_ts + interval 判斷監控是否還活著"""
    if now is None:
        now = now_ts()
    last_ts = float(m.get("last_check_ts") or 0)
    interval = int(m.get("interval", 180))
    timeout = max(interval * 3, 600)  # 至少 3 倍間隔或 10 分鐘
    return (now - last_ts) <= timeout


# ------------------------------------------------------
# 使用者管理（只有 bot_server 會改，不需要 file lock）
# ------------------------------------------------------
def add_user(user_id: str):
    users = read_json(USERS_FILE, [])
    if user_id not in users:
        users.append(user_id)
        write_json(USERS_FILE, users)
        print("⭐ 新增使用者:", user_id)


# ------------------------------------------------------
# 商品名稱 / 即時查庫存
# ------------------------------------------------------
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


def get_product_name(url: str) -> str:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        h1 = soup.find("h1")
        if h1:
            return h1.text.strip()

        title = soup.find("title")
        if title:
            return title.text.strip()
    except Exception as e:
        print(f"⚠️ 取得商品名稱失敗：{url} -> {e}")

    return "未命名商品"


def check_stock_once(url: str) -> bool:
    """立刻請求網站檢查是否有貨（盡量避免誤判）"""
    resp = requests.get(url, headers=HEADERS, timeout=(5, 20))
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    def is_disabled(btn) -> bool:
        if btn is None:
            return True
        if btn.has_attr("disabled"):
            return True
        if (btn.get("aria-disabled") or "").lower() == "true":
            return True
        return False

    # 1) 優先判斷：id=add-to-cart-button（你新貼的「有貨」HTML就是這顆）
    cart_btn = soup.select_one("#add-to-cart-button")
    if cart_btn:
        txt = cart_btn.get_text(strip=True)
        if ("加入購物車" in txt) and (not is_disabled(cart_btn)):
            return True
        return False  # 有按鈕但 disabled 或文字不是加入購物車 → 當缺貨

    # 2) 次要判斷：找主要按鈕文字包含「加入購物車」的（對應你貼過的另一種版型）
    for b in soup.select("button"):
        txt = b.get_text(strip=True)
        if "加入購物車" in txt:
            return not is_disabled(b)

    # 3) 保險：頁面文字包含「缺貨」→ 缺貨
    page_text = soup.get_text(" ", strip=True)
    if "缺貨" in page_text:
        return False

    # 4) 其他未知狀況（通常是缺貨/頁面結構變了）
    return False


# ------------------------------------------------------
# Webhook
# ------------------------------------------------------


def _handle_callback(handler):
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return "OK"


@app.route("/callback/costco", methods=["POST"])
def callback_costco():
    return _handle_callback(costco_handler)


@app.route("/callback/cruise", methods=["POST"])
def callback_cruise():
    return _handle_callback(cruise_handler)


# ------------------------------------------------------
# 處理訊息
# ------------------------------------------------------
@costco_handler.add(MessageEvent, message=TextMessage)
def handle_costco_message(event):

    user_id = event.source.user_id
    add_user(user_id)

    raw_text = event.message.text.strip()
    text = raw_text.lower()
    parts = raw_text.split()
    cmd = parts[0].lower() if parts else ""

    # ==================================================
    # 1) 查庫存 / stock
    #    - 立即掃一次所有監控網址
    #    - 再把結果「合併寫回」monitors.json
    # ==================================================
    if cmd in ("庫存", "查庫存", "stock"):
        monitors_snapshot = read_json(MONITORS_FILE, [])

        if not monitors_snapshot:
            reply = "目前沒有任何監控項目。"
        else:
            status_updates = {}
            lines = ["📦 目前庫存（即時重查）：\n"]
            now = now_ts()

            for i, m in enumerate(monitors_snapshot, 1):
                url = m["url"]
                name = m.get("name", "未命名商品")

                in_stock = check_stock_once(url)
                status_updates[url] = {
                    "last_in_stock": in_stock,
                    "last_check_ts": now,
                    "last_check": now_str(),
                }

                status_txt = "有貨 ✔️" if in_stock else "缺貨 ❌"
                lines.append(
                    f"{i}. {name}\n"
                    f"🔗 {url}\n"
                    f"➡️ 狀態：{status_txt}\n"
                    f"🕒 更新時間：{status_updates[url]['last_check']}\n"
                )

            # 合併寫回（短時間持有 lock，不做網路 I/O）
            def mut(monitors_list):
                for m in monitors_list:
                    url = m["url"]
                    if url in status_updates:
                        m.update(status_updates[url])
                    # 每次更新完順便重算 alive
                    m["alive"] = calc_alive(m, now)

            update_monitors(mut)
            reply = "\n".join(lines)

        costco_line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    # ==================================================
    # 2) 新增監控 / add
    #    新增 URL [秒數]  （秒數省略預設 180）
    # ==================================================
    if cmd in ("新增", "add"):
        if len(parts) < 2:
            reply = "格式：\n\n新增 URL [秒數]\nadd URL [秒數]\n\n秒數省略則預設 180 秒。"
            costco_line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
            return

        url = parts[1]
        if len(parts) >= 3 and parts[2].isdigit():
            sec = int(parts[2])
        else:
            sec = 180  # 預設 3 分鐘

        name = get_product_name(url)
        now = now_ts()
        now_s = now_str()

        result = {"added": False, "duplicate": False}

        def mut(monitors_list):
            # 檢查是否已存在
            if any(m["url"] == url for m in monitors_list):
                result["duplicate"] = True
                return

            monitors_list.append(
                {
                    "url": url,
                    "interval": sec,
                    "name": name,
                    "last_in_stock": None,
                    "last_check_ts": now,
                    "last_check": now_s,
                    "alive": True,
                }
            )
            result["added"] = True

        update_monitors(mut)

        if result["duplicate"]:
            reply = "❗ 此 URL 已在監控列表中。"
        elif result["added"]:
            reply = (
                "✅ 已新增監控：\n\n"
                f"{name}\n"
                f"🔗 {url}\n"
                f"⏱ 頻率：{sec} 秒"
            )
        else:
            reply = "⚠️ 新增監控時發生未知錯誤（理論上不會到這裡）。"

        costco_line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    # ==================================================
    # 3) 移除監控 / remove / del
    # ==================================================
    if cmd in ("移除", "刪除", "remove", "del"):
        if len(parts) < 2:
            reply = "格式：\n\n移除 URL\nremove URL"
            costco_line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
            return

        url = parts[1]
        result = {"removed": False}

        def mut(monitors_list):
            before = len(monitors_list)
            monitors_list[:] = [m for m in monitors_list if m["url"] != url]
            if len(monitors_list) < before:
                result["removed"] = True

        update_monitors(mut)

        if result["removed"]:
            reply = f"🗑 已移除監控：\n{url}"
        else:
            reply = "找不到這個 URL 的監控。"

        costco_line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    # ==================================================
    # 4) 列出監控 / list
    #    只讀檔，用 last_check_ts 即時計算 alive，不寫檔
    # ==================================================
    if cmd in ("列出監控", "監控", "list"):
        monitors = read_json(MONITORS_FILE, [])

        if not monitors:
            reply = "目前沒有監控項目。"
        else:
            now = now_ts()
            msg_lines = ["📄 監控列表：\n"]
            for i, m in enumerate(monitors, 1):
                name = m.get("name", "未命名商品")
                url = m["url"]
                interval = m.get("interval", 180)
                last_check = m.get("last_check", "尚未檢查")
                in_stock = m.get("last_in_stock", None)

                alive = calc_alive(m, now)
                status_txt = (
                    "有貨 ✔️" if in_stock is True
                    else "缺貨 ❌" if in_stock is False
                    else "未知 ⏳"
                )
                alive_txt = "🟢 監控中" if alive else "🔴 監控異常"

                msg_lines.append(
                    f"{i}. {name}\n"
                    f"🔗 {url}\n"
                    f"⏱ 每 {interval} 秒\n"
                    f"➡️ 庫存：{status_txt}\n"
                    f"🕒 最後檢查：{last_check}\n"
                    f"{alive_txt}\n"
                )

            reply = "\n".join(msg_lines)

        costco_line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    # ==================================================
    # 其他訊息 -> 顯示幫助
    # ==================================================
    help_text = (
        "可用指令：\n\n"
        "📦 庫存 / stock  → 立即重查所有庫存\n"
        "📄 列出監控 / 監控 / list  → 顯示監控清單與狀態\n"
        "➕ 新增 [URL] [秒數] / add [URL] [秒數]  (未輸入秒數預設3分鐘)\n"
        "➖ 移除 [URL] / remove [URL]"
    )

    costco_line_bot_api.reply_message(event.reply_token, TextSendMessage(text=help_text))


@cruise_handler.add(MessageEvent, message=TextMessage)
def handle_cruise_message(event):
    cruise_line_bot_api.reply_message(event.reply_token, TextSendMessage(text="cruise ok"))


# ------------------------------------------------------
# 主程式
# ------------------------------------------------------
if __name__ == "__main__":
    app.run(port=5000)
