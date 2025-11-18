from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

import os
import json
import re
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# 檔案名稱
USERS_FILE = "users.json"
MONITORS_FILE = "monitors.json"


# ------------------------------------------------------
# 安全 JSON 讀寫
# ------------------------------------------------------
def safe_load_json(path, default):
    try:
        if not os.path.exists(path):
            return default
        content = open(path, "r", encoding="utf-8").read().strip()
        if not content:
            return default
        return json.loads(content)
    except:
        return default


def safe_save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ 無法寫入 {path}：", e)


# ------------------------------------------------------
# 使用者管理
# ------------------------------------------------------
def add_user(user_id):
    users = safe_load_json(USERS_FILE, [])

    if user_id not in users:
        users.append(user_id)
        safe_save_json(USERS_FILE, users)
        print("⭐ 新增使用者:", user_id)


# ------------------------------------------------------
# 自動抓取商品名稱
# ------------------------------------------------------
def get_product_name(url):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Costco 商品名稱通常在 <h1>
        h1 = soup.find("h1")
        if h1:
            return h1.text.strip()

        # 後備方案：抓 <title>
        title = soup.find("title")
        if title:
            return title.text.strip()

    except:
        pass

    return "未命名商品"


# ------------------------------------------------------
# 監控項目管理
# ------------------------------------------------------
def load_monitors():
    return safe_load_json(MONITORS_FILE, [])


def save_monitors(monitors):
    safe_save_json(MONITORS_FILE, monitors)


# ------------------------------------------------------
# Webhook
# ------------------------------------------------------
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return "OK"


# ------------------------------------------------------
# 處理訊息
# ------------------------------------------------------
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):

    user_id = event.source.user_id
    add_user(user_id)

    text = event.message.text.strip()

    # ==================================================
    # 查庫存
    # ==================================================
    if text == "查庫存":
        monitors = load_monitors()

        if not monitors:
            reply = "目前沒有任何監控項目。"
        else:
            msg = "📦 目前監控庫存狀態：\n\n"
            for i, m in enumerate(monitors, 1):
                name = m.get("name", "未命名商品")
                url = m["url"]
                status = m.get("last_in_stock", None)

                if status is True:
                    s = "有貨 ✔️"
                elif status is False:
                    s = "缺貨 ❌"
                else:
                    s = "未檢查 ⏳"

                msg += (
                    f"{i}. {name}\n"
                    f"🔗 {url}\n"
                    f"➡️ 狀態：{s}\n\n"
                )

            reply = msg

        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    # ==================================================
    # 新增監控（自動抓名稱 / 預設 180 秒）
    # ==================================================
    if text.startswith("新增監控"):
        parts = text.split()

        if len(parts) < 2:
            reply = "格式錯誤！請用：\n\n新增監控 URL [秒數]"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
            return

        url = parts[1]

        # 使用者自訂秒數 or 預設 180 秒
        if len(parts) >= 3 and parts[2].isdigit():
            sec = int(parts[2])
        else:
            sec = 180

        monitors = load_monitors()

        exists = any(m["url"] == url for m in monitors)
        if exists:
            reply = "❗ 此 URL 已存在監控列表。"
        else:
            name = get_product_name(url)

            monitors.append({
                "url": url,
                "interval": sec,
                "name": name,
                "last_in_stock": None
            })
            save_monitors(monitors)

            reply = f"已新增監控：\n\n{name}\n{url}\n頻率：{sec} 秒"

        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    # ==================================================
    # 移除監控
    # ==================================================
    if text.startswith("移除監控"):
        match = re.match(r"移除監控\s+(https?://\S+)", text)
        if not match:
            reply = "格式錯誤！請用：\n\n移除監控 URL"
        else:
            url = match.group(1)
            monitors = load_monitors()
            new_list = [m for m in monitors if m["url"] != url]

            save_monitors(new_list)
            reply = f"已移除監控：\n{url}"

        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    # ==================================================
    # 列出監控
    # ==================================================
    if text == "列出監控":
        monitors = load_monitors()

        if not monitors:
            reply = "目前沒有監控項目。"
        else:
            msg = "📄 目前監控項目：\n\n"
            for i, m in enumerate(monitors, 1):
                msg += (
                    f"{i}. {m['name']}\n"
                    f"🔗 {m['url']}\n"
                    f"⏱ 每 {m['interval']} 秒\n\n"
                )
            reply = msg

        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    # ==================================================
    # 其他訊息
    # ==================================================
    reply = (
        "可用指令：\n\n"
        "🟢 查庫存\n"
        "🟢 列出監控\n"
        "🟢 新增監控 URL 秒數\n"
        "🟢 移除監控 URL"
    )

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))


# ------------------------------------------------------
# 主程式
# ------------------------------------------------------
if __name__ == "__main__":
    app.run(port=5000)
