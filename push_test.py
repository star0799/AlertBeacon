from linebot import LineBotApi
from linebot.models import TextSendMessage
from dotenv import load_dotenv
import os

load_dotenv()
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)

USER_ID = "U7f96b113534cd778efd5fdc2a18a8f31"  # 換你的

line_bot_api.push_message(
    USER_ID,
    TextSendMessage(text="Hello，我是主動通知機器人 ❤️")
)

print("推播成功！")