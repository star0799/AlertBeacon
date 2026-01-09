from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from flask import Flask, request, jsonify

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
@app.get("/_routes")
def list_routes():
    return jsonify(sorted([str(r) for r in app.url_map.iter_rules()]))

def ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")

print(f"[{ts()}] [BOOT] bot_server file={__file__}", flush=True)
COSTCO_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_COSTCO_CHANNEL_ACCESS_TOKEN")
COSTCO_CHANNEL_SECRET = os.getenv("LINE_COSTCO_CHANNEL_SECRET")

costco_line_bot_api = LineBotApi(COSTCO_CHANNEL_ACCESS_TOKEN)
costco_handler = WebhookHandler(COSTCO_CHANNEL_SECRET)

# Cruise
CRUISE_TOKEN = os.getenv("LINE_CRUISE_CHANNEL_ACCESS_TOKEN")
CRUISE_SECRET = os.getenv("LINE_CRUISE_CHANNEL_SECRET")

cruise_line_bot_api = LineBotApi(CRUISE_TOKEN)
cruise_handler = WebhookHandler(CRUISE_SECRET)

USERS_FILE = "users.json"
USERS_CRUISE_FILE = "users_cruise.json"
MONITORS_FILE = "monitors.json"
TOKENS_CACHE_FILE = "latest_tokens.json"
CRUISE_MONITORS_FILE = "monitors_cruise.json"
CRUISE_BACKEND_BASE = "https://backend-prd.b2m.stardreamcruises.com"

_latest_recaptcha = {"token": None, "at": None, "action": None}
_latest_tokens = {"accessToken": None, "refreshToken": None, "user": None, "at": None}

@app.post("/cruise/tokens")
def cruise_tokens():
    data = request.get_json(force=True, silent=True) or {}
    if not data.get("accessToken") or not data.get("refreshToken"):
        return jsonify({"ok": False, "error": "missing tokens"}), 400
    _latest_tokens.update({
        "accessToken": data["accessToken"],
        "refreshToken": data["refreshToken"],
        "user": data.get("user"),
        "at": data.get("at"),
    })
    write_json(TOKENS_CACHE_FILE, _latest_tokens)
    print(f"[{ts()}] [CRUISE] tokens updated", _latest_tokens["at"])
    return jsonify({"ok": True})

@app.get("/cruise/tokens")
def cruise_tokens_get():
    return jsonify(_latest_tokens)

@app.post("/cruise/notify")
def cruise_notify():
    data = request.get_json(force=True, silent=True) or {}
    print(f"[{ts()}] [CRUISE NOTIFY]", json.dumps(data, ensure_ascii=False))

    users = read_json(USERS_CRUISE_FILE, [])
    if not users:
        return jsonify({"ok": False, "error": "no cruise users yet"}), 400

    t = data.get("type", "CRUISE")

    if t == "CRUISE_CABIN_AVAILABLE":
        total = data.get("totalItems")
        cabins = data.get("cabins") or []
        text = f"🚢 有房通知！totalItems={total}\n" + "\n".join(cabins[:10])


    elif t == "CRUISE_TIER_AVAILABLE":
        tier = data.get("tier")
        tier_short = data.get("tier_short") or (str(tier) if tier is not None else "\u623f\u578b")
        tier_full = data.get("tier_full") or (f"{tier}\u5ba2\u623f" if tier is not None else "\u623f\u578b")
        date = data.get("date") or ""
        port_name = data.get("port_name") or ""
        itinerary_name = data.get("itinerary_name") or ""
        max_pax = data.get("max_pax")
        max_pax_text = f"\uff08{max_pax}\u4eba\uff09" if isinstance(max_pax, int) and max_pax > 0 else ""

        text = (
            f"\U0001F6A2\u3010\u67e5\u5230\u53ef\u8a02\u623f\u3011{tier_short}{max_pax_text}\n"
            f"\u65e5\u671f\uff1a{date}\n"
            f"\u51fa\u767c\uff1a{port_name}\n"
            f"\u822a\u7a0b\uff1a{itinerary_name}\n"
            f"\u623f\u578b\uff1a{tier_full}"
        )

    elif t == "CRUISE_NEED_RELOGIN":
        # daemon 偵測到 refresh / cabin 401/403 時會送這個
        err = data.get("error") or ""
        text = (
            "⚠️ Cruise 需要重新登入一次（token 失效/未授權）\n"
            "請開啟 SDC 登入頁手動登入，Token Sync 會自動回灌 tokens。\n"
            f"{err}"
        )

    else:
        # 其他事件先直接丟 type + error/message
        msg = data.get("message") or data.get("error") or ""
        text = f"⚠️ Cruise {t}\n{msg}"
    msg_obj = TextSendMessage(text=text)

    ok_count = 0
    errors = []
    for uid in users:
        try:
            cruise_line_bot_api.push_message(uid, msg_obj)
            ok_count += 1
        except Exception as e:
            errors.append({"user": uid, "error": str(e)})

    return jsonify({"ok": True, "sent": ok_count, "errors": errors})

@app.post("/cruise/test_push")
def cruise_test_push():
    users = read_json(USERS_CRUISE_FILE, [])
    if not users:
        return jsonify({"ok": False, "error": "no cruise users yet. Please message cruise bot once."}), 400

    text = (request.get_json(silent=True) or {}).get("text") or "✅ Cruise 測試推播成功"
    msg = TextSendMessage(text=text)

    ok_count = 0
    errors = []

    for uid in users:
        try:
            cruise_line_bot_api.push_message(uid, msg)
            ok_count += 1
        except Exception as e:
            errors.append({"user": uid, "error": str(e)})

    return jsonify({"ok": True, "sent": ok_count, "errors": errors})


@app.post("/cruise/recaptcha")
def cruise_recaptcha():
    data = request.get_json(force=True, silent=True) or {}
    token = data.get("recaptcha_token")
    if not token:
        return jsonify({"ok": False, "error": "missing recaptcha_token"}), 400

    _latest_recaptcha["token"] = token
    _latest_recaptcha["at"] = time.time()
    _latest_recaptcha["action"] = data.get("action")

    print(f"[{ts()}] [CRUISE] recaptcha updated", _latest_recaptcha["action"])
    return jsonify({"ok": True})

@app.get("/cruise/recaptcha")
def cruise_recaptcha_get():
    return jsonify(_latest_recaptcha)

@app.post("/cruise/tokens/clear")
def cruise_tokens_clear():
    _latest_tokens.update({"accessToken": None, "refreshToken": None, "user": None, "at": None})
    write_json(TOKENS_CACHE_FILE, _latest_tokens)  # 若你做了持久化
    return jsonify({"ok": True})
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
        print(f"[{ts()}] ⚠️ 讀取 {path} 失敗：{e}")
        return default


def write_json(path: str, data):
    try:
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception as e:
        print(f"[{ts()}] ⚠️ 寫入 {path} 失敗：{e}")

#
_latest_tokens.update(read_json(TOKENS_CACHE_FILE, _latest_tokens))
#
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


def update_cruise_monitors(mutator):
    """mutator(monitors_list) -> read/modify/write monitors_cruise.json under lock"""
    lock = FileLock(CRUISE_MONITORS_FILE + ".lock")
    with lock:
        monitors = read_json(CRUISE_MONITORS_FILE, [])
        mutator(monitors)
        write_json(CRUISE_MONITORS_FILE, monitors)
        return monitors


def read_cruise_monitors():
    lock = FileLock(CRUISE_MONITORS_FILE + ".lock")
    with lock:
        return read_json(CRUISE_MONITORS_FILE, [])


def _cruise_headers(access_token: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json, text/plain, */*",
        "timezone": "Asia/Taipei",
    }


def fetch_itinerary(access_token: str, date: str) -> str | None:
    url = f"{CRUISE_BACKEND_BASE}/customers/list/itinerary"
    params = {"departure_date": date, "lang": "hant", "page": 1}
    r = requests.get(url, params=params, headers=_cruise_headers(access_token), timeout=10)
    r.raise_for_status()
    items = (r.json() or {}).get("items") or []
    for it in items:
        name = it.get("traditional_chinese_name") or ""
        if "\u63a2\u7d22\u661f\u865f" in name:
            return name
    return None


def fetch_port(access_token: str, date: str) -> dict | None:
    url = f"{CRUISE_BACKEND_BASE}/customers/list/port"
    params = {"departure_date": date, "lang": "hant", "page": 1}
    r = requests.get(url, params=params, headers=_cruise_headers(access_token), timeout=10)
    r.raise_for_status()
    items = (r.json() or {}).get("items") or []
    if not items:
        return None

    def pick_port(match_name: str, match_code: str):
        for p in items:
            if (p.get("traditional_chinese_port_name") == match_name) or (p.get("port_code") == match_code):
                return p
        return None

    picked = (
        pick_port("\u57fa\u9686", "KEL")
        or pick_port("\u9ad8\u96c4", "KHH")
        or items[0]
    )
    return {
        "departure_port": picked.get("id"),
        "port_code": picked.get("port_code"),
        "port_name": picked.get("traditional_chinese_port_name") or picked.get("port_name") or "",
    }


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
        print(f"[{ts()}] ⭐ 新增使用者:", user_id)



def add_cruise_user(user_id: str):
    users = read_json(USERS_CRUISE_FILE, [])
    if user_id not in users:
        users.append(user_id)
        write_json(USERS_CRUISE_FILE, users)
        print(f"[{ts()}] ⭐ 新增 Cruise 使用者:", user_id)


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
        print(f"[{ts()}] ⚠️ 取得商品名稱失敗：{url} -> {e}")

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
    user_id = event.source.user_id
    add_cruise_user(user_id)

    raw_text = (event.message.text or "").strip()

    list_keywords = ("列出監控", "監控列表", "顯示監控", "列出", "列表","LIST","List","list")
    delete_keywords = ("刪除", "移除", "取消","DELETE","Delete","delete")

    if any(k in raw_text for k in list_keywords):
        monitors = read_cruise_monitors()
        if not monitors:
            reply = "目前沒有監控航程"
            cruise_line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
            return

        def to_int_or_none(v):
            try:
                return int(v)
            except Exception:
                return None

        def tier_name(tier):
            if tier == 3:
                return "露台"
            if tier == 2:
                return "海景"
            if tier == 1:
                return "內側"
            return str(tier)

        lines = []
        for m in monitors[:10]:
            date = m.get("date") or ""
            enabled = bool(m.get("enabled", False))
            status_txt = "啟用" if enabled else "停用"
            port_name = m.get("port_name") or str(m.get("departure_port") or "")
            itinerary_name = m.get("itinerary_name") or ""
            notify_mode = m.get("notify_mode")
            baseline_tier = to_int_or_none(m.get("baseline_tier"))

            if notify_mode == "per_tier_first_seen":
                rule = "各等級首次出現各通知一次"
            elif notify_mode == "above_baseline_first_seen" and baseline_tier == 1:
                rule = "海景/露台出現才通知"
            elif notify_mode == "above_baseline_first_seen" and baseline_tier == 2:
                rule = "只通知露台"
            else:
                if baseline_tier is None:
                    rule = "（未知規則）"
                else:
                    rule = f"{tier_name(baseline_tier)}以上才通知"

            max_pax = m.get("max_pax")
            pax_text = f"{max_pax}人" if isinstance(max_pax, int) and max_pax > 0 else "未確定"
            last_check = m.get("last_check_at") or "尚未檢查"

            tiers = m.get("last_seen_tiers") or []
            if tiers:
                tiers_txt = "/".join(tier_name(t) for t in tiers)
            else:
                tiers_txt = "目前無房"

            block = "\n".join([
                f"日期：{date}",
                f"出發：{port_name}",
                f"航程：{itinerary_name}",
                f"人數：{pax_text}",
                f"最後監控：{last_check}",
                f"目前房型：{tiers_txt}",
            ])
            lines.append(block)

        reply = "\n\n".join(lines)
        if len(monitors) > 10:
            reply = reply + "\n(其餘省略)"
        cruise_line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    if any(k in raw_text for k in delete_keywords):
        date_match = re.search(r"\d{4}-\d{2}-\d{2}", raw_text)
        if not date_match:
            reply = "請輸入：刪除 YYYY-MM-DD"
            cruise_line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
            return
        date = date_match.group(0)
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            reply = "日期不合法，請輸入 YYYY-MM-DD"
            cruise_line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
            return

        result = {"removed": False}

        def mut(monitors_list):
            before = len(monitors_list)
            monitors_list[:] = [m for m in monitors_list if m.get("date") != date]
            if len(monitors_list) < before:
                result["removed"] = True

        update_cruise_monitors(mut)

        if result["removed"]:
            reply = f"✅ 已刪除監控：{date}"
        else:
            reply = f"找不到監控：{date}"
        cruise_line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return
    date_match = re.search(r"\d{4}-\d{2}-\d{2}", raw_text)
    if not date_match:
        help_text = (
            "\u53ef\u7528\u6307\u4ee4\uff1a\n\n"
            "\u5217\u51fa\u76e3\u63a7 / \u76e3\u63a7\u5217\u8868 / \u986f\u793a\u76e3\u63a7 / \u5217\u51fa / \u5217\u8868\n"
            "\u522a\u9664 YYYY-MM-DD / \u79fb\u9664 YYYY-MM-DD / \u53d6\u6d88 YYYY-MM-DD\n"
            "YYYY-MM-DD [\u5167\u5074/\u6d77\u666f/\u9732\u53f0]\n"
            "\u4f8b\u5982\uff1a2026-02-27 \u6d77\u666f"
        )
        cruise_line_bot_api.reply_message(event.reply_token, TextSendMessage(text=help_text))
        return
    date = date_match.group(0)
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        reply = "日期不合法，請輸入 YYYY-MM-DD"
        cruise_line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    if any(k in raw_text for k in ("\u9732\u53f0", "\u9732\u81fa", "\u967d\u53f0")):
        tier_short = "\u9732\u53f0"
        notify_mode = "above_baseline_first_seen"
        baseline_tier = 2
    elif "\u6d77\u666f" in raw_text:
        tier_short = "\u6d77\u666f"
        notify_mode = "above_baseline_first_seen"
        baseline_tier = 1
    elif ("\u5167\u5074" in raw_text) or ("\u5167\u8259" in raw_text):
        tier_short = "\u5167\u5074"
        notify_mode = "per_tier_first_seen"
        baseline_tier = 1
    else:
        tier_short = "\u5167\u5074"
        notify_mode = "per_tier_first_seen"
        baseline_tier = 1

    if notify_mode == "per_tier_first_seen":
        rule_text = "\u5404\u7b49\u7d1a\u9996\u6b21\u51fa\u73fe\u5404\u901a\u77e5\u4e00\u6b21"
    elif notify_mode == "above_baseline_first_seen":
        if tier_short == "\u6d77\u666f":
            rule_text = "\u6d77\u666f/\u9732\u53f0\u51fa\u73fe\u624d\u901a\u77e5"
        elif tier_short == "\u9732\u53f0":
            rule_text = "\u53ea\u901a\u77e5\u9732\u53f0"
        else:
            rule_text = f"{tier_short}\u4ee5\u4e0a\u624d\u901a\u77e5"
    else:
        rule_text = f"{tier_short}\u4ee5\u4e0a\u624d\u901a\u77e5"

    access = _latest_tokens.get("accessToken")
    if not access:
        reply = "\u8acb\u5148\u624b\u52d5\u767b\u5165\u4e00\u6b21\u8b93 Token Sync \u56de\u704c"
        cruise_line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    try:
        itinerary_name = fetch_itinerary(access, date)
        if not itinerary_name:
            reply = "\u8a72\u65e5\u671f\u6c92\u6709\u63a2\u7d22\u661f\u865f\u822a\u7a0b"
            cruise_line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
            return

        port_info = fetch_port(access, date)
        if not port_info or port_info.get("departure_port") is None:
            reply = "\u67e5\u7121\u53ef\u7528\u51fa\u767c\u6e2f\u53e3"
            cruise_line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
            return
    except Exception as ex:
        print(f"[{ts()}] [CRUISE] warn: failed to fetch cruise list:", repr(ex), flush=True)
        reply = "\u67e5\u8a62\u822a\u7a0b\u5931\u6557，\u8acb\u7a0d\u5f8c\u518d\u8a66"
        cruise_line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    result = {"updated": False, "added": False}

    def mut(monitors_list):
        for m in monitors_list:
            if m.get("date") == date:
                m["lang"] = "hant"
                m["itinerary_name"] = itinerary_name
                m["departure_port"] = port_info.get("departure_port")
                m["port_code"] = port_info.get("port_code")
                m["port_name"] = port_info.get("port_name")
                m["notify_mode"] = notify_mode
                m["baseline_tier"] = baseline_tier
                m["notified_tiers"] = []
                m["enabled"] = True
                m["no_room_until_epoch"] = 0
                result["updated"] = True
                return

        monitors_list.append({
            "date": date,
            "enabled": True,
            "lang": "hant",
            "itinerary_name": itinerary_name,
            "departure_port": port_info.get("departure_port"),
            "port_code": port_info.get("port_code"),
            "port_name": port_info.get("port_name"),
            "baseline_tier": baseline_tier,
            "notify_mode": notify_mode,
            "max_pax": None,
            "last_check_at": None,
            "last_http": None,
            "last_seen_cabins": [],
            "last_seen_tiers": [],
            "notified_tiers": [],
            "no_room_until_epoch": 0,
        })
        result["added"] = True

    update_cruise_monitors(mut)

    status = "✅ \u5df2\u66f4\u65b0\u76e3\u63a7" if result["updated"] else "✅ \u5df2\u65b0\u589e\u76e3\u63a7"
    reply = (
        f"{status}\n"
        f"\u65e5\u671f：{date}\n"
        f"\u51fa\u767c：{port_info.get('port_name', '')}\n"
        f"\u822a\u7a0b：{itinerary_name}\n"
        f"\u901a\u77e5\u898f\u5247：{rule_text}\n"
        "daemon \u6703\u81ea\u52d5\u67e5\u623f"
    )
    cruise_line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))


# ------------------------------------------------------
# 主程式
# ------------------------------------------------------
if __name__ == "__main__":
    app.run(port=5000)
