from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, FollowEvent
import os
import json
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

USERS_FILE = "users.json"


def add_user(user_id: str):
    """安全寫入 users.json，不重複、不壞檔"""
    try:
        users = []

        # 安全讀取，避免空白或壞掉報錯
        if os.path.exists(USERS_FILE):
            try:
                with open(USERS_FILE, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    users = json.loads(content) if content else []
            except Exception:
                users = []  # 檔案損壞 → 重建

        if user_id not in users:
            users.append(user_id)
            with open(USERS_FILE, "w", encoding="utf-8") as f:
                json.dump(users, f, indent=2, ensure_ascii=False)
            print(f"⭐ 新增至名單: {user_id}")
            return True

        return False

    except Exception as e:
        print("⚠️ 寫入 users.json 失敗:", e)
        return False


# ------------------------------------------------------------
#   Webhook 主入口
# ------------------------------------------------------------
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return 'OK'


# ------------------------------------------------------------
#   使用者「加入好友」時觸發 → 自動加入通知名單
# ------------------------------------------------------------
@handler.add(FollowEvent)
def handle_follow(event):
    user_id = event.source.user_id
    add_user(user_id)

    welcome = "歡迎加入 Costco 庫存通知機器人！傳『查庫存』即可查看最新狀態。"
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=welcome)
    )
    print(f"👤 FollowEvent：{user_id} 已加入好友")


# ------------------------------------------------------------
#   使用者傳訊息 → 回覆、並確保 userId 已加入名單
# ------------------------------------------------------------
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    add_user(user_id)  # 傳訊息也會加入（保險機制）

    text = event.message.text.strip().lower()

    if text == "查庫存":
        status = json.load(open("status.json", "r", encoding="utf-8"))
        reply = f"目前庫存：{'有貨 ✔️' if status['in_stock'] else '缺貨 ❌'}"

    else:
        reply = "嗨～傳『查庫存』即可查詢最新庫存喔！"

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply)
    )


if __name__ == "__main__":
    app.run(port=5000)
