from linebot import LineBotApi
from linebot.models import TextSendMessage
import os

from dotenv import load_dotenv
load_dotenv()

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")

def push_message(user_id: str, text: str):
    line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
    line_bot_api.push_message(user_id, TextSendMessage(text=text))