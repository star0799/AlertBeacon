from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from flask import Flask, request, jsonify
import html
import re
import os
import sys
import json
import re
import time
import requests
import secrets
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from dotenv import load_dotenv
from filelock import FileLock

load_dotenv()

app = Flask(__name__)
app.json.ensure_ascii = False
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
CRUISE_ADMIN_KEY = os.getenv("CRUISE_ADMIN_KEY")

cruise_line_bot_api = LineBotApi(CRUISE_TOKEN)
cruise_handler = WebhookHandler(CRUISE_SECRET)

USERS_FILE = "users.json"
USERS_CRUISE_FILE = "users_cruise.json"
MONITORS_FILE = "monitors.json"
TOKENS_CACHE_FILE = "latest_tokens.json"
FEATURES_FILE = "features.json"
CRUISE_MONITORS_FILE = "monitors_cruise.json"
STATE_DIR = "state"
os.makedirs(STATE_DIR, exist_ok=True)
PAY_LINKS_FILE = os.path.join(STATE_DIR, "pay_links.json")
PAY_LINKS_LOCK = os.path.join(STATE_DIR, "pay_links.json.lock")
CRUISE_ADMINS_FILE = os.path.join(STATE_DIR, "cruise_admins.json")
PRIVATE_PEOPLE_FILE = os.path.join(STATE_DIR, "private_people.json")
CRUISE_BACKEND_BASE = "https://backend-prd.b2m.stardreamcruises.com"

_latest_recaptcha = {"token": None, "at": None, "action": None}
_latest_tokens = {
    "accessToken": None,
    "refreshToken": None,
    "user": None,
    "customer_id": None,
    "user_mmid": None,
    "at": None,
}
CRUISE_RELOGIN_NEEDED = False
LAST_RELOGIN_ALERT_AT = 0.0
LAST_RECOVER_ALERT_AT = 0.0

@app.post("/cruise/tokens")
def cruise_tokens():
    data = request.get_json(force=True, silent=True) or {}
    if not data.get("accessToken") or not data.get("refreshToken"):
        return jsonify({"ok": False, "error": "missing tokens"}), 400
    global CRUISE_RELOGIN_NEEDED, LAST_RECOVER_ALERT_AT
    was_missing = not (_latest_tokens.get("accessToken") and _latest_tokens.get("refreshToken"))
    prev_access = _latest_tokens.get("accessToken")
    user_val = data.get("user")
    customer_id = None
    if isinstance(user_val, dict):
        sub = user_val.get("sub")
        if isinstance(sub, (str, int)) and str(sub).strip().isdigit():
            customer_id = str(sub).strip()
    elif isinstance(user_val, str) and user_val.strip().isdigit():
        customer_id = user_val.strip()
    user_mmid = _latest_tokens.get("user_mmid")
    if prev_access != data.get("accessToken"):
        user_mmid = None
    _latest_tokens.update({
        "accessToken": data["accessToken"],
        "refreshToken": data["refreshToken"],
        "user": user_val,
        "customer_id": customer_id,
        "user_mmid": user_mmid,
        "at": data.get("at"),
    })
    write_json(TOKENS_CACHE_FILE, _latest_tokens)
    print(f"[{ts()}] [CRUISE] tokens updated", _latest_tokens["at"])
    should_notify = CRUISE_RELOGIN_NEEDED or (prev_access != data.get("accessToken"))
    if should_notify:
        now = time.time()
        if now - LAST_RECOVER_ALERT_AT >= 60:
            users = read_json(USERS_CRUISE_FILE, [])
            if users:
                msg = TextSendMessage(text="✅ Cruise token 已更新，監控已恢復")
                for uid in users:
                    try:
                        cruise_line_bot_api.push_message(uid, msg)
                    except Exception as e:
                        print(f"[{ts()}] [CRUISE] notify failed:", uid, repr(e), flush=True)
                LAST_RECOVER_ALERT_AT = now
        CRUISE_RELOGIN_NEEDED = False
    return jsonify({"ok": True})

@app.get("/cruise/tokens")
def cruise_tokens_get():
    return jsonify(_latest_tokens)


def _is_missing(v) -> bool:
    if v is None:
        return True
    if isinstance(v, str) and not v.strip():
        return True
    return False


@app.get("/cruise/pay/<code>")
def cruise_pay(code: str):
    if not os.path.exists(PAY_LINKS_FILE):
        return jsonify({"ok": False, "error": f"pay_links not found: {PAY_LINKS_FILE}"}), 404

    now = int(time.time())
    lock = FileLock(PAY_LINKS_LOCK)
    with lock:
        links = read_json(PAY_LINKS_FILE, {})
        if not isinstance(links, dict) or code not in links:
            resp = jsonify({"ok": False, "error": "此連結已經使用過，請重新下單"})
            resp.headers["Content-Type"] = "application/json; charset=utf-8"
            return resp, 410


        # Optional cleanup of stale entries
        cleaned = False
        for k in list(links.keys()):
            item = links.get(k)

            # 非 dict 的舊/壞資料：直接清掉（或 continue）
            if not isinstance(item, dict):
                del links[k]
                cleaned = True
                continue
            
            created_at = int(item.get("created_at") or 0)
            expires_at = int(item.get("expires_at") or 0)
            used_at = int(item.get("used_at") or 0)

            if (expires_at and expires_at < now - 3600) or (used_at and created_at and created_at < now - 3600):
                del links[k]
                cleaned = True

        if cleaned:
            write_json_atomic(PAY_LINKS_FILE, links)

        entry = links.get(code)
        if not isinstance(entry, dict):
            resp = jsonify({"ok": False, "error": "此連結已經使用過，請重新下單"})
            resp.headers["Content-Type"] = "application/json; charset=utf-8"
            return resp, 410

        expires_at = int(entry.get("expires_at") or 0)
        used_at = int(entry.get("used_at") or 0)
        if expires_at and now > expires_at:
            return jsonify({"ok": False, "error": "expired"}), 410
        if used_at != 0:
            return jsonify({"ok": False, "error": "used"}), 410

        booking_id = entry.get("booking_id")
        record_updated_time = entry.get("recordUpdatedTime")
        payment_method = entry.get("payment_method")
        if not booking_id or not payment_method:
            return jsonify({"ok": False, "error": "invalid pay_links entry"}), 400

        method_map = {
            "credit_card": "Credit Card",
            "rwcc_points": "RWCC Points",
            "genting_points": "Genting Points",
        }
        payment_for_map = {
            "port_charge": "Port Charge",
            "non_member_surcharge": "Non Member Surcharge",
        }
        allowed_methods = set(method_map.values())
        allowed_payment_for = set(payment_for_map.values())

        def normalize_method(value):
            if value in method_map:
                return method_map[value]
            if value in allowed_methods:
                return value
            return None

        def normalize_payment_for(value):
            if value in payment_for_map:
                return payment_for_map[value]
            if value in allowed_payment_for:
                return value
            return None

        raw_items = payment_method
        if isinstance(raw_items, str):
            raw_items = [{"payment_method": raw_items}]
        elif isinstance(raw_items, dict):
            raw_items = [raw_items]
        if not isinstance(raw_items, list) or not raw_items:
            return jsonify({"ok": False, "error": "invalid payment_method"}), 400

        normalized_items = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            raw_for = item.get("payment_for") or "Port Charge"
            raw_method = item.get("payment_method")
            mapped_method = normalize_method(raw_method)
            if isinstance(raw_for, list):
                for pf in raw_for:
                    mapped_for = normalize_payment_for(pf)
                    if not mapped_for or not mapped_method:
                        print(
                            f"[{ts()}] [CRUISE] invalid payment mapping: "
                            f"payment_for={raw_for} -> {mapped_for}, "
                            f"payment_method={raw_method} -> {mapped_method}",
                            flush=True,
                        )
                        return jsonify({
                            "ok": False,
                            "error": "invalid payment enum",
                            "allowed_payment_for": sorted(allowed_payment_for),
                            "allowed_payment_method": sorted(allowed_methods),
                        }), 400
                    normalized_items.append({
                        "payment_for": mapped_for,
                        "payment_method": mapped_method,
                    })
            else:
                mapped_for = normalize_payment_for(raw_for)
                if not mapped_for or not mapped_method:
                    print(
                        f"[{ts()}] [CRUISE] invalid payment mapping: "
                        f"payment_for={raw_for} -> {mapped_for}, "
                        f"payment_method={raw_method} -> {mapped_method}",
                        flush=True,
                    )
                    return jsonify({
                        "ok": False,
                        "error": "invalid payment enum",
                        "allowed_payment_for": sorted(allowed_payment_for),
                        "allowed_payment_method": sorted(allowed_methods),
                    }), 400
                normalized_items.append({
                    "payment_for": mapped_for,
                    "payment_method": mapped_method,
                })

        if not normalized_items:
            return jsonify({"ok": False, "error": "invalid payment_method"}), 400

        access_token = _latest_tokens.get("accessToken")
        if not access_token:
            return jsonify({"ok": False, "error": "missing access token"}), 401

        try:
            summary = fetch_booking_summary(access_token, booking_id)
        except Exception:
            summary = None

        latest_rut = None
        if isinstance(summary, dict):
            latest_rut = _extract_record_updated_time(summary, None)

        if latest_rut and isinstance(latest_rut, str) and latest_rut.strip():
            latest_rut = latest_rut.strip()
            if latest_rut != record_updated_time:
                record_updated_time = latest_rut
                entry["recordUpdatedTime"] = record_updated_time
                links[code] = entry
                write_json_atomic(PAY_LINKS_FILE, links)
        else:
            if not record_updated_time:
                return jsonify({"ok": False, "error": "missing recordUpdatedTime (cannot refresh)"}), 400

        # ----- FC passenger passport completeness check (before payment) -----
        passenger_list = []
        if isinstance(summary, dict):
            if isinstance(summary.get("passenger_list"), list):
                passenger_list = summary.get("passenger_list")
            else:
                cb = summary.get("current_booking") or {}
                if isinstance(cb, dict) and isinstance(cb.get("passenger_list"), list):
                    passenger_list = cb.get("passenger_list")

        fc_missing = []
        passport_keys = [
            "passport_issuance_country",
            "passport_number",
            "passport_issuance_date",
            "passport_expiry_date",
        ]

        for idx, p in enumerate(passenger_list, 1):
            if not isinstance(p, dict):
                continue
            p_type = (p.get("type") or "").lower()
            mmid_val = p.get("mmid")
            is_fc = (
                (p_type == "frequent_cruiser")
                or (mmid_val is None)
                or (isinstance(mmid_val, str) and mmid_val.strip() == "")
            )
            if not is_fc:
                continue

            missing_keys = [k for k in passport_keys if _is_missing(p.get(k))]
            if missing_keys:
                name = p.get("chinese_name") or p.get("full_name") or f"同行{idx}"
                fc_missing.append({"passenger": name, "missing": missing_keys})

        if fc_missing:
            resp = jsonify({
                "ok": False,
                "error": "親友乘客資料缺少護照資訊，請先到 SDC 常用旅客補齊後再付款",
                "missing": fc_missing,
            })
            resp.headers["Content-Type"] = "application/json; charset=utf-8"
            return resp, 400
        # ----- end check -----

        # Mark used before calling payment API to prevent replay
        entry["used_at"] = now
        links[code] = entry
        write_json_atomic(PAY_LINKS_FILE, links)

    body = {
        "booking_id": booking_id,
        "payment_method": normalized_items,
        "recordUpdatedTime": record_updated_time,
    }

    url = f"{CRUISE_BACKEND_BASE}/customers/v2/booking/payment/{booking_id}"
    try:
        r = requests.post(url, headers=_cruise_payment_headers(access_token), json=body, timeout=20)
        r.raise_for_status()
        data = r.json() or {}
        cs = (data.get("cybersource_response") or {})
        endpoint = cs.get("endPoint")
        config = cs.get("config") or {}
        if not endpoint or not isinstance(config, dict) or not config:
            raise ValueError("missing cybersource response")
    except requests.HTTPError as ex:
        # HTTP 4xx/5xx: include response body for debugging
        resp = ex.response
        status = resp.status_code if resp is not None else None
        body_head = ""
        detail = None
        if resp is not None:
            try:
                detail = resp.json()
                body_head = json.dumps(detail, ensure_ascii=False)[:1200]
            except Exception:
                body_head = (resp.text or "")[:1200]
                detail = body_head
        print(
            f"[{ts()}] [CRUISE] payment failed: status={status} body_head={body_head}",
            flush=True,
        )
        # rollback used_at
        lock = FileLock(PAY_LINKS_LOCK)
        with lock:
            links = read_json(PAY_LINKS_FILE, {})
            entry2 = links.get(code)
            if isinstance(entry2, dict) and entry2.get("used_at") == now:
                entry2["used_at"] = 0
                links[code] = entry2
                write_json_atomic(PAY_LINKS_FILE, links)
    
        if status in (401, 403):
            trigger_relogin("payment", f"status={status}")
        return jsonify({"ok": False, "error": f"payment api failed ({status})", "detail": detail}), 502
    
    except Exception as ex:
        # timeout / json decode / ValueError 等
        lock = FileLock(PAY_LINKS_LOCK)
        with lock:
            links = read_json(PAY_LINKS_FILE, {})
            entry2 = links.get(code)
            if isinstance(entry2, dict) and entry2.get("used_at") == now:
                entry2["used_at"] = 0
                links[code] = entry2
                write_json_atomic(PAY_LINKS_FILE, links)
    
        return jsonify({"ok": False, "error": f"payment api failed", "detail": str(ex)}), 502        

    # Option B: delete entry after successful redirect to keep file clean.
    lock = FileLock(PAY_LINKS_LOCK)
    with lock:
        links = read_json(PAY_LINKS_FILE, {})
        if isinstance(links, dict) and code in links:
            del links[code]
            write_json_atomic(PAY_LINKS_FILE, links)

    html_page = _build_auto_post_form(endpoint, config)
    return html_page, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.post("/cruise/paylink/create")
def cruise_paylink_create():
    data = request.get_json(force=True, silent=True) or {}
    try:
        booking_id = int(data.get("booking_id"))
    except Exception:
        booking_id = 0
    if booking_id <= 0:
        return jsonify({"ok": False, "error": "invalid booking_id"}), 400

    record_updated_time = data.get("recordUpdatedTime")
    if not isinstance(record_updated_time, str) or not record_updated_time.strip():
        return jsonify({"ok": False, "error": "invalid recordUpdatedTime"}), 400

    payment_method = data.get("payment_method")
    if not isinstance(payment_method, list) or not payment_method:
        return jsonify({"ok": False, "error": "invalid payment_method"}), 400
    for item in payment_method:
        if not isinstance(item, dict):
            return jsonify({"ok": False, "error": "invalid payment_method item"}), 400
        if not item.get("payment_for") or not item.get("payment_method"):
            return jsonify({"ok": False, "error": "missing payment_for/payment_method"}), 400

    ttl_seconds = _normalize_ttl_seconds(data.get("ttl_seconds", 600))
    result = create_paylink_entry(booking_id, record_updated_time, payment_method, ttl_seconds)
    if not result:
        return jsonify({"ok": False, "error": "failed to generate code"}), 500

    code = result["code"]
    expires_at = result["expires_at"]
    pay_url = f"{_get_base_url()}/cruise/pay/{code}"

    access_token = get_latest_access_token()
    booking_summary = fetch_booking_summary(access_token, booking_id) if access_token else None
    summary_text = build_paylink_summary_text(
        booking_id=booking_id,
        pay_url=pay_url,
        ttl_seconds=ttl_seconds,
        booking_summary=booking_summary,
    )

    resp = jsonify({
        "ok": True,
        "code": code,
        "pay_url": pay_url,
        "expires_at": expires_at,
        "summary_text": summary_text,
    })
    resp.headers["Content-Type"] = "application/json; charset=utf-8"
    return resp, 200


@app.post("/cruise/book-and-paylink")
def cruise_book_and_paylink():
    admin_key = CRUISE_ADMIN_KEY or ""
    if not admin_key:
        print(f"[{ts()}] [CRUISE][ADMINKEY] rejected reason=disabled", flush=True)
        resp = jsonify({"ok": False, "error": "API_DISABLED", "message": "book-and-paylink API disabled"})
        resp.headers["Content-Type"] = "application/json; charset=utf-8"
        return resp, 503
    provided_key = request.headers.get("X-CRUISE-ADMIN-KEY")
    if not provided_key or provided_key != admin_key:
        print(f"[{ts()}] [CRUISE][ADMINKEY] rejected reason=invalid", flush=True)
        resp = jsonify({"ok": False, "error": "UNAUTHORIZED", "message": "invalid admin key"})
        resp.headers["Content-Type"] = "application/json; charset=utf-8"
        return resp, 401

    data = request.get_json(force=True, silent=True) or {}
    line_user_id = request.headers.get("X-Line-User-Id") or data.get("line_user_id")
    if not line_user_id:
        resp = jsonify({"ok": False, "error": "BAD_REQUEST", "message": "missing line_user_id"})
        resp.headers["Content-Type"] = "application/json; charset=utf-8"
        return resp, 400
    if not is_cruise_daemon_enabled():
        resp = jsonify({"ok": False, "error": "FEATURE_DISABLED", "message": "訂房功能目前未啟用"})
        resp.headers["Content-Type"] = "application/json; charset=utf-8"
        return resp, 503
    if not is_cruise_admin(line_user_id):
        resp = jsonify({"ok": False, "error": "FORBIDDEN", "message": "你沒有訂房權限（僅限管理者）"})
        resp.headers["Content-Type"] = "application/json; charset=utf-8"
        return resp, 403

    trace_id = _make_trace_id()
    payload, status = _book_and_paylink_flow(data, _get_base_url(), trace_id)
    resp = jsonify(payload)
    resp.headers["Content-Type"] = "application/json; charset=utf-8"
    return resp, status


@app.post("/cruise/command")
def cruise_command():
    admin_key = CRUISE_ADMIN_KEY or ""
    if not admin_key:
        return jsonify({"ok": False, "error": "API_DISABLED", "message": "cruise command disabled"}), 503
    provided_key = request.headers.get("X-CRUISE-ADMIN-KEY")
    if not provided_key or provided_key != admin_key:
        return jsonify({"ok": False, "error": "UNAUTHORIZED", "message": "invalid admin key"}), 401

    data = request.get_json(force=True, silent=True) or {}
    text = data.get("text")
    line_user_id = data.get("line_user_id")
    reply = bool(data.get("reply", False))
    dry_run = bool(data.get("dry_run", False))
    if not text or not line_user_id:
        return jsonify({"ok": False, "error": "BAD_REQUEST", "message": "missing text/line_user_id"}), 400
    if not is_cruise_daemon_enabled():
        return jsonify({"ok": False, "error": "FEATURE_DISABLED", "message": "cruise disabled"}), 503
    if not is_cruise_admin(line_user_id):
        return jsonify({"ok": False, "error": "FORBIDDEN", "message": "not admin"}), 403

    result = process_cruise_text_command(
        text=text,
        line_user_id=line_user_id,
        reply=reply,
        dry_run=dry_run,
        source_type="test",
    )
    if result.get("ok"):
        return jsonify(result), 200
    error_type = result.get("error_type")
    if error_type == "relogin_required":
        return jsonify(result), 409
    if error_type in ("parse_error", "auth_mismatch"):
        return jsonify(result), 400
    if error_type == "upstream_error":
        return jsonify(result), 502
    return jsonify(result), 500


@app.post("/cruise/paylink/notify")
def cruise_paylink_notify():
    if not feature_enabled("cruise_daemon"):
        return jsonify({"ok": False, "error": "cruise disabled"}), 503
    data = request.get_json(force=True, silent=True) or {}
    target = data.get("to")
    message = data.get("message")
    if not target or not message:
        return jsonify({"ok": False, "error": "missing to/message"}), 400
    try:
        cruise_line_bot_api.push_message(target, TextSendMessage(text=message))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})

@app.post("/cruise/notify")
def cruise_notify():
    data = request.get_json(force=True, silent=True) or {}
    print(f"[{ts()}] [CRUISE NOTIFY]", json.dumps(data, ensure_ascii=False))

    if not feature_enabled("cruise_daemon"):
        print(f"[{ts()}] [CRUISE] cruise disabled", flush=True)
        return jsonify({"ok": False, "error": "cruise disabled"}), 503

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
    _latest_tokens.update({
        "accessToken": None,
        "refreshToken": None,
        "user": None,
        "customer_id": None,
        "user_mmid": None,
        "at": None,
    })
    write_json(TOKENS_CACHE_FILE, _latest_tokens)  # 若你做了持久化
    return jsonify({"ok": True})
# ------------------------------------------------------
# 基本 JSON 工具（不加鎖的版本）
# ------------------------------------------------------
def read_json(path: str, default):
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8-sig") as f:
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


def write_json_atomic(path: str, data):
    try:
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception as e:
        print(f"[{ts()}] \u26a0\ufe0f \u5beb\u5165 {path} \u5931\u6557:{type(e).__name__}: {e}")


def load_features() -> dict:
    defaults = {
        "bot_server": True,
        "costco": True,
        "cruise_daemon": True,
        "ngrok": True,
    }
    try:
        if not os.path.exists(FEATURES_FILE):
            return defaults
        lock = FileLock(FEATURES_FILE + ".lock")
        with lock:
            data = read_json(FEATURES_FILE, defaults)
    except Exception as e:
        print(f"[{ts()}] ⚠️ 讀取 {FEATURES_FILE} 失敗：{e}")
        return defaults
    if not isinstance(data, dict):
        return defaults
    merged = defaults.copy()
    for key, value in data.items():
        if key in merged:
            merged[key] = bool(value)
    return merged


def feature_enabled(name: str) -> bool:
    return bool(FEATURES.get(name, True))


def trigger_relogin(reason: str, detail: str = "") -> None:
    global CRUISE_RELOGIN_NEEDED
    if CRUISE_RELOGIN_NEEDED:
        print(f"[{ts()}] [CRUISE] relogin already needed reason={reason}", flush=True)
        return

    users = read_json(USERS_CRUISE_FILE, [])
    if users:
        msg_text = (
            "⚠️ Cruise 需要重新登入一次（token 失效/未授權）\n"
            "請開啟 SDC 登入並讓 Token Sync 回灌\n"
            f"{detail or reason}"
        )
        msg = TextSendMessage(text=msg_text)
        for uid in users:
            try:
                cruise_line_bot_api.push_message(uid, msg)
            except Exception as e:
                print(f"[{ts()}] [CRUISE] relogin notify failed:", uid, repr(e), flush=True)

    _latest_tokens.update({
        "accessToken": None,
        "refreshToken": None,
        "user": None,
        "customer_id": None,
        "user_mmid": None,
        "at": None,
    })
    write_json(TOKENS_CACHE_FILE, _latest_tokens)
    CRUISE_RELOGIN_NEEDED = True


def is_cruise_daemon_enabled() -> bool:
    if not os.path.exists(FEATURES_FILE):
        print(f"[{ts()}] [CRUISE] features missing: {FEATURES_FILE}", flush=True)
        return False
    try:
        lock = FileLock(FEATURES_FILE + ".lock")
        with lock:
            data = read_json(FEATURES_FILE, None)
    except Exception as e:
        print(f"[{ts()}] [CRUISE] features read error: {e}", flush=True)
        return False
    if not isinstance(data, dict):
        print(f"[{ts()}] [CRUISE] features parse error", flush=True)
        return False
    if "cruise_daemon" not in data:
        print(f"[{ts()}] [CRUISE] features missing key: cruise_daemon", flush=True)
        return False
    if not bool(data.get("cruise_daemon")):
        print(f"[{ts()}] [CRUISE] features cruise_daemon disabled", flush=True)
        return False
    return True


def is_cruise_admin(user_id: str) -> bool:
    if not user_id:
        return False
    if not os.path.exists(CRUISE_ADMINS_FILE):
        print(f"[{ts()}] [CRUISE] admins missing: {CRUISE_ADMINS_FILE}", flush=True)
        return False
    try:
        lock = FileLock(CRUISE_ADMINS_FILE + ".lock")
        with lock:
            admins = read_json(CRUISE_ADMINS_FILE, [])
    except Exception as e:
        print(f"[{ts()}] [CRUISE] admins read error: {e}", flush=True)
        return False
    if not isinstance(admins, list):
        return False
    return user_id in admins


def set_feature(name: str, enabled: bool) -> bool:
    try:
        lock = FileLock(FEATURES_FILE + ".lock")
        with lock:
            data = read_json(FEATURES_FILE, {})
            if not isinstance(data, dict):
                data = {}
            data[name] = bool(enabled)
            write_json(FEATURES_FILE, data)
        FEATURES[name] = bool(enabled)
        return True
    except Exception as e:
        print(f"[{ts()}] ⚠️ 更新 {FEATURES_FILE} 失敗：{e}")
        return False


def get_latest_access_token():
    access = _latest_tokens.get("accessToken")
    if access:
        return access
    cached = read_json(TOKENS_CACHE_FILE, {})
    if isinstance(cached, dict):
        return cached.get("accessToken") or cached.get("access_token")
    return None


def _cruise_payment_headers(access_token: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "timezone": "Asia/Taipei",
        "Origin": "https://sdr.stardreamcruises.com",
        "Referer": "https://sdr.stardreamcruises.com/",
    }


def _build_auto_post_form(endpoint: str, config: dict) -> str:
    inputs = []
    for k, v in sorted(config.items()):
        name = html.escape(str(k), quote=True)
        value = html.escape(str(v), quote=True)
        inputs.append(f'<input type="hidden" name="{name}" value="{value}">')
    inputs_html = "\n  ".join(inputs)
    action = html.escape(endpoint, quote=True)
    return (
        "<!doctype html>\n"
        "<html><head><meta charset=\"utf-8\"><title>Redirecting...</title></head>\n"
        "<body>\n"
        "  <form id=\"pay\" method=\"POST\" action=\"{action}\">\n"
        "  {inputs}\n"
        "  </form>\n"
        "  <script>document.getElementById('pay').submit();</script>\n"
        "</body></html>\n"
    ).format(action=action, inputs=inputs_html)

#
_latest_tokens.update(read_json(TOKENS_CACHE_FILE, _latest_tokens))
FEATURES = load_features()
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


def fetch_booking_summary(access_token: str, booking_id_numeric: int) -> dict | None:
    url = f"{CRUISE_BACKEND_BASE}/booking/{booking_id_numeric}"
    try:
        r = requests.get(url, headers=_cruise_payment_headers(access_token), timeout=10)
        r.raise_for_status()
        payload = r.json() or {}
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            return payload.get("data")
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _fmt_field(value) -> str:
    if value is None:
        return "（未提供）"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        text = value.strip()
        return text if text else "（未提供）"
    return "（未提供）"


def _fmt_amount(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)
    if isinstance(value, str):
        return value.strip()
    return ""


def _full_name(person: dict) -> str:
    if not isinstance(person, dict):
        return ""
    name = (
        person.get("full_name")
        or person.get("english_name")
        or person.get("name_en")
        or ""
    )
    if name:
        return name
    given = person.get("given_name") or person.get("first_name") or ""
    surname = person.get("surname") or person.get("last_name") or ""
    return " ".join([x for x in [given, surname] if x])


def _chinese_name(person: dict) -> str:
    if not isinstance(person, dict):
        return ""
    return (
        person.get("chinese_name")
        or person.get("traditional_chinese_name")
        or person.get("name_zh")
        or ""
    )


def _phone_number(person: dict) -> str:
    if not isinstance(person, dict):
        return ""
    country = person.get("phone_country_code_number") or person.get("country_code") or ""
    number = person.get("phone_number") or person.get("phone") or person.get("mobile") or ""
    if country and number:
        return f"{country}{number}"
    return number or country or ""


def _extract_passengers(booking_summary: dict) -> list[dict]:
    for key in ("passenger_list", "passengers", "passengerList"):
        items = booking_summary.get(key)
        if isinstance(items, list):
            return [p for p in items if isinstance(p, dict)]
    return []


def build_paylink_summary_text(
    booking_id: int,
    pay_url: str,
    ttl_seconds: int,
    booking_summary: dict | None,
) -> str:
    ttl_minutes = max(1, int((ttl_seconds + 59) / 60))
    if not isinstance(booking_summary, dict):
        return "\n".join([
            "🧾【付款前二次確認】",
            f"訂單：{booking_id}",
            f"👉 前往付款：{pay_url}",
            f"（連結有效 {ttl_minutes} 分鐘）",
        ])

    summary = booking_summary
    booking_id_inner = summary.get("booking_id") or ""
    order_id = summary.get("id") or booking_id
    departure_date = summary.get("departure_date") or ""
    arrival_date = summary.get("arrival_date") or ""
    port_name = summary.get("traditional_chinese_departing_port") or ""
    itinerary_name = summary.get("traditional_chinese_itinerary_name") or ""
    ship_name = (
        summary.get("traditional_chinese_ship_name")
        or summary.get("ship_name")
        or summary.get("ship_name_zh")
        or ""
    )
    cabin_name = summary.get("traditional_chinese_cabin_name") or ""
    pax = summary.get("design_pax") or summary.get("customer_pax")

    amount = ""
    pcm = summary.get("port_charge_mode")
    if isinstance(pcm, dict):
        amount = _fmt_amount(pcm.get("credit_card"))
    if not amount:
        amount = _fmt_amount(summary.get("credit_card"))

    main = summary.get("main_passenger") if isinstance(summary.get("main_passenger"), dict) else {}
    main_zh = _fmt_field(_chinese_name(main))
    main_en = _fmt_field(_full_name(main))
    main_passport = _fmt_field(main.get("passport_number") or main.get("passport"))
    main_phone = _fmt_field(_phone_number(main))

    emergency = summary.get("emergency_contact") if isinstance(summary.get("emergency_contact"), dict) else {}
    emergency_zh = _fmt_field(_chinese_name(emergency)) if emergency else ""
    emergency_en = _fmt_field(_full_name(emergency)) if emergency else ""
    emergency_phone = _fmt_field(_phone_number(emergency)) if emergency else ""
    emergency_rel = _fmt_field(emergency.get("relationship_type") or emergency.get("relationship")) if emergency else ""

    passengers = _extract_passengers(summary)
    passengers_available = bool(passengers)

    def passenger_lines(max_count: int, include_details: bool) -> list[str]:
        lines = ["同行乘客："]
        if not passengers_available:
            lines.append("（後端未回傳/尚未完成）")
            return lines
        for idx, p in enumerate(passengers[:max_count], 1):
            zh = _fmt_field(_chinese_name(p))
            en = _fmt_field(_full_name(p))
            lines.append(f"- 中文名：{zh} / English：{en}")
            if include_details:
                passport = _fmt_field(p.get("passport_number") or p.get("passport"))
                phone = _fmt_field(_phone_number(p))
                lines.append(f"  護照：{passport}")
                lines.append(f"  電話：{phone}")
        if len(passengers) > max_count:
            lines.append("...其餘略")
        return lines

    def emergency_lines(include_details: bool) -> list[str]:
        lines = ["緊急聯絡人："]
        if not emergency:
            lines.append("（未提供）")
            return lines
        lines.append(f"- 中文名：{emergency_zh} / English：{emergency_en}")
        if include_details:
            lines.append(f"  電話：{emergency_phone}")
            if emergency_rel != "（未提供）":
                lines.append(f"  關係：{emergency_rel}")
        return lines

    def assemble(max_count: int, include_companion_details: bool, include_emergency_details: bool) -> str:
        date_text = departure_date if not arrival_date else f"{departure_date} ～ {arrival_date}"
        lines = [
            "🧾【付款前二次確認】",
            f"訂單：{_fmt_field(order_id)}（booking_id: {_fmt_field(booking_id_inner)}）",
            f"日期：{_fmt_field(date_text)}",
            f"出發：{_fmt_field(port_name)}",
            f"航程：{_fmt_field(itinerary_name)}",
        ]
        if ship_name:
            lines.append(f"船名：{_fmt_field(ship_name)}")
        lines.extend([
            f"房型：{_fmt_field(cabin_name)}",
            f"人數：{_fmt_field(pax)}",
            f"金額：TWD {_fmt_field(amount)}",
            "",
            "主要乘客：",
            f"- 中文名：{main_zh} / English：{main_en}",
            f"  護照：{main_passport}",
            f"  電話：{main_phone}",
        ])
        lines.append("")
        lines.extend(passenger_lines(max_count, include_companion_details))
        lines.append("")
        lines.extend(emergency_lines(include_emergency_details))
        lines.append("")
        lines.append(f"👉 前往付款：{pay_url}")
        lines.append(f"（連結有效 {ttl_minutes} 分鐘）")
        return "\n".join(lines).strip()

    max_count = min(len(passengers), 6) if passengers_available else 0
    include_companion_details = True
    include_emergency_details = True
    for _ in range(8):
        text = assemble(max_count, include_companion_details, include_emergency_details)
        if len(text) <= 2000:
            return text
        if max_count > 1:
            max_count -= 1
            continue
        if include_companion_details:
            include_companion_details = False
            continue
        if include_emergency_details:
            include_emergency_details = False
            continue
        break

    return "\n".join([
        "🧾【付款前二次確認】",
        f"訂單：{booking_id}",
        f"👉 前往付款：{pay_url}",
        f"（連結有效 {ttl_minutes} 分鐘）",
    ])


def _normalize_ttl_seconds(value) -> int:
    try:
        ttl_seconds = int(value)
    except Exception:
        ttl_seconds = 600
    if ttl_seconds < 60:
        ttl_seconds = 60
    if ttl_seconds > 3600:
        ttl_seconds = 3600
    return ttl_seconds


def _normalize_payment_method(value) -> list | None:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    if isinstance(value, str) and value.strip():
        return [{"payment_for": "Port Charge", "payment_method": value.strip()}]
    return None


def _build_payment_items(method_value, include_surcharge: bool) -> list | None:
    items = _normalize_payment_method(method_value)
    if not items:
        return None
    normalized = []
    has_port_charge = False
    has_surcharge = False
    for item in items:
        if not isinstance(item, dict):
            return None
        entry = dict(item)
        if not entry.get("payment_for"):
            entry["payment_for"] = "Port Charge"
        if not entry.get("payment_method"):
            return None
        if entry.get("payment_for") == "Port Charge":
            has_port_charge = True
        if entry.get("payment_for") == "Non Member Surcharge":
            has_surcharge = True
        normalized.append(entry)
    if not normalized:
        return None
    if not has_port_charge:
        normalized.insert(0, {
            "payment_for": "Port Charge",
            "payment_method": normalized[0].get("payment_method"),
        })
    if include_surcharge:
        if not has_surcharge:
            normalized.append({
                "payment_for": "Non Member Surcharge",
                "payment_method": normalized[0].get("payment_method"),
            })
    else:
        normalized = [item for item in normalized if item.get("payment_for") != "Non Member Surcharge"]
    return normalized


def _make_trace_id() -> str:
    return f"{int(time.time())}-{secrets.token_hex(3)}"

_RECORD_UPDATED_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _gen_record_updated_time() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _looks_like_record_updated_time(v: str | None) -> bool:
    return isinstance(v, str) and bool(_RECORD_UPDATED_TIME_RE.match(v.strip()))


def _ensure_record_updated_time(v: str | None) -> str:
    if _looks_like_record_updated_time(v):
        return v.strip()
    return _gen_record_updated_time()


def _extract_record_updated_time(summary: dict, fallback: str | None) -> str | None:
    for key in ("recordUpdatedTime", "record_updated_time", "record_updated_at", "updated_at"):
        value = summary.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if isinstance(fallback, str) and fallback.strip():
        return fallback.strip()
    return None


def _is_booking_cancelled(summary: dict) -> bool:
    for key in ("deleted_at", "cancelled_at", "canceled_at"):
        if summary.get(key):
            return True
    status = (summary.get("status") or summary.get("booking_status") or "").lower()
    return "cancel" in status


def _get_base_url() -> str:
    proto = request.headers.get("X-Forwarded-Proto")
    host = request.headers.get("X-Forwarded-Host")
    if proto and host:
        return f"{proto}://{host}"
    return request.host_url.rstrip("/")


def create_paylink_entry(
    booking_id: int,
    record_updated_time: str,
    payment_method: list,
    ttl_seconds: int,
) -> dict | None:
    now = int(time.time())
    expires_at = now + ttl_seconds

    lock = FileLock(PAY_LINKS_LOCK)
    with lock:
        links = read_json(PAY_LINKS_FILE, {})
        if not isinstance(links, dict):
            links = {}

        code = None
        for _ in range(5):
            candidate = secrets.token_urlsafe(8)
            if candidate not in links:
                code = candidate
                break
        if not code:
            return None

        links[code] = {
            "booking_id": booking_id,
            "recordUpdatedTime": record_updated_time,
            "payment_method": payment_method,
            "created_at": now,
            "expires_at": expires_at,
            "used_at": 0,
        }
        write_json_atomic(PAY_LINKS_FILE, links)

    return {"code": code, "expires_at": expires_at}


def _log_backend_response(trace_id: str, label: str, resp: requests.Response):
    body = ""
    try:
        body = resp.text or ""
    except Exception:
        body = ""
    body = body[:500]
    print(
        f"[{ts()}] [CRUISE] trace={trace_id} {label} status={resp.status_code} body={body}",
        flush=True,
    )


def _parse_json_response(resp: requests.Response) -> dict | None:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _book_and_paylink_flow(data: dict, base_url: str, trace_id: str) -> tuple[dict, int]:
    access_token = get_latest_access_token()
    if not access_token:
        return {"ok": False, "error": "Token 已過期，請重新登入 SDC 後再試"}, 401

    def to_int(value):
        try:
            return int(value)
        except Exception:
            return 0

    def to_int_or_none(value):
        try:
            v = int(value)
            return v if v > 0 else None
        except Exception:
            return None

    cabin_allotment_id = to_int(data.get("cabin_allotment_id"))
    customer_pax = to_int(data.get("customer_pax"))
    itinerary_id = to_int(data.get("itinerary_id"))
    non_member_surcharge_id = to_int_or_none(data.get("non_member_surcharge_id"))
    record_updated_time = data.get("record_updated_time") or data.get("recordUpdatedTime")

    missing = []
    if cabin_allotment_id <= 0:
        missing.append("cabin_allotment_id")
    if customer_pax <= 0:
        missing.append("customer_pax")
    if itinerary_id <= 0:
        missing.append("itinerary_id")
    if missing:
        return {"ok": False, "error": f"缺少必要欄位: {', '.join(missing)}"}, 400

    record_updated_time = _ensure_record_updated_time(record_updated_time)

    payment_method = _build_payment_items(
        data.get("payment_method"),
        include_surcharge=non_member_surcharge_id is not None,
    )
    if not payment_method:
        return {"ok": False, "error": "invalid payment_method"}, 400

    ttl_seconds = _normalize_ttl_seconds(data.get("ttl_seconds", 600))
    headers = _cruise_payment_headers(access_token)

    print(f"[{ts()}] [CRUISE] trace={trace_id} draft record_updated_time={record_updated_time}", flush=True)
    draft_payload = {
        "cabin_allotment_id": cabin_allotment_id,
        "customer_pax": customer_pax,
        "gratuity_charge_id": None,
        "itinerary_id": itinerary_id,
        "record_updated_time": record_updated_time,
        "non_member_surcharge_id": non_member_surcharge_id,
    }

    for attempt in range(2):
        draft_url = f"{CRUISE_BACKEND_BASE}/customers/v2/booking/draft"
        try:
            r = requests.post(draft_url, headers=headers, json=draft_payload, timeout=20)
        except Exception as ex:
            print(f"[{ts()}] [CRUISE] trace={trace_id} draft error={type(ex).__name__}", flush=True)
            return {"ok": False, "error": "後端建立草稿失敗，請稍後重試"}, 502
        if r.status_code == 401:
            trigger_relogin("draft", f"status={r.status_code}")
            _log_backend_response(trace_id, "draft", r)
            return {"ok": False, "error": "Token 已過期，請重新登入 SDC 後再試"}, 401
        if r.status_code >= 400:
            _log_backend_response(trace_id, "draft", r)
            return {"ok": False, "error": "後端建立草稿失敗，請稍後重試"}, 502

        draft_data = _parse_json_response(r) or {}
        booking_id = draft_data.get("booking_id")
        if not booking_id:
            return {"ok": False, "error": "後端回傳缺少 booking_id"}, 502

        check_url = f"{CRUISE_BACKEND_BASE}/booking/check-status/{booking_id}"
        try:
            r = requests.get(check_url, headers=headers, timeout=20)
        except Exception as ex:
            print(f"[{ts()}] [CRUISE] trace={trace_id} check-status error={type(ex).__name__}", flush=True)
            return {"ok": False, "error": "後端查詢訂單狀態失敗，請稍後重試"}, 502
        if r.status_code == 401:
            trigger_relogin("check-status", f"status={r.status_code}")
            _log_backend_response(trace_id, "check-status", r)
            return {"ok": False, "error": "Token 已過期，請重新登入 SDC 後再試"}, 401
        if r.status_code >= 400:
            _log_backend_response(trace_id, "check-status", r)
            return {"ok": False, "error": "後端查詢訂單狀態失敗，請稍後重試"}, 502

        check_data = _parse_json_response(r) or {}
        numeric_id = check_data.get("id") or check_data.get("booking_id")
        try:
            numeric_id = int(numeric_id)
        except Exception:
            numeric_id = 0
        if numeric_id <= 0:
            return {"ok": False, "error": "後端回傳缺少 numeric_id"}, 502

        booking_url = f"{CRUISE_BACKEND_BASE}/booking/{numeric_id}"
        try:
            r = requests.get(booking_url, headers=headers, timeout=20)
        except Exception as ex:
            print(f"[{ts()}] [CRUISE] trace={trace_id} booking error={type(ex).__name__}", flush=True)
            return {"ok": False, "error": "後端查詢訂單詳情失敗，請稍後重試"}, 502
        if r.status_code == 401:
            trigger_relogin("booking", f"status={r.status_code}")
            _log_backend_response(trace_id, "booking", r)
            return {"ok": False, "error": "Token 已過期，請重新登入 SDC 後再試"}, 401
        if r.status_code >= 400:
            _log_backend_response(trace_id, "booking", r)
            body_text = ""
            try:
                body_text = r.text or ""
            except Exception:
                body_text = ""
            if "Order has been cancelled" in body_text:
                if attempt == 0:
                    continue
                return {"ok": False, "error": "訂單已取消，請稍後重試"}, 400
            return {"ok": False, "error": "後端查詢訂單詳情失敗，請稍後重試"}, 502

        booking_payload = _parse_json_response(r) or {}
        booking_summary = booking_payload.get("data") if isinstance(booking_payload, dict) else None
        if booking_summary is None:
            booking_summary = booking_payload
        if not isinstance(booking_summary, dict):
            return {"ok": False, "error": "後端回傳訂單資料格式錯誤"}, 502

        if _is_booking_cancelled(booking_summary):
            if attempt == 0:
                continue
            return {"ok": False, "error": "訂單已取消，請稍後重試"}, 400

        latest_record_updated_time = _extract_record_updated_time(booking_summary, record_updated_time)
        if not latest_record_updated_time:
            print(f"[{ts()}] [CRUISE] trace={trace_id} missing recordUpdatedTime", flush=True)
            return {"ok": False, "error": "後端缺少 recordUpdatedTime"}, 502

        paylink_entry = create_paylink_entry(
            booking_id=numeric_id,
            record_updated_time=latest_record_updated_time,
            payment_method=payment_method,
            ttl_seconds=ttl_seconds,
        )
        if not paylink_entry:
            return {"ok": False, "error": "failed to generate code"}, 500

        code = paylink_entry["code"]
        expires_at_epoch = paylink_entry["expires_at"]
        expires_at_iso = datetime.utcfromtimestamp(expires_at_epoch).strftime("%Y-%m-%dT%H:%M:%SZ")
        pay_url = f"{base_url}/cruise/pay/{code}"
        summary_text = build_paylink_summary_text(
            booking_id=numeric_id,
            pay_url=pay_url,
            ttl_seconds=ttl_seconds,
            booking_summary=booking_summary,
        )

        return {
            "ok": True,
            "booking_id": booking_id,
            "numeric_id": numeric_id,
            "summary_text": summary_text,
            "pay_url": pay_url,
            "expires_at": expires_at_iso,
            "code": code,
        }, 200

    return {"ok": False, "error": "訂單已取消，請稍後重試"}, 400


def _parse_flexible_date(text: str) -> str | None:
    m = re.match(r"^(\d{4})[-/\.]?(\d{1,2})[-/\.]?(\d{1,2})$", text)
    if not m:
        return None
    y, mo, d = (int(v) for v in m.groups())
    try:
        return datetime(y, mo, d).strftime("%Y-%m-%d")
    except ValueError:
        return None


def _normalize_room_token(text: str) -> str:
    s = (text or "").strip()
    if s.endswith("\u623f"):
        s = s[:-1]
    return s


def _parse_tier(text: str) -> int | None:
    ROOM_TIER_MAP = {
        1: {"\u5167\u5074", "\u5167\u8259"},
        2: {"\u6d77\u666f"},
        3: {"\u9732\u53f0", "\u967d\u53f0", "\u9732\u81fa", "\u967d\u81fa"},
    }
    token = _normalize_room_token(text)
    for tier, synonyms in ROOM_TIER_MAP.items():
        if token in synonyms:
            return tier
    return None


def _normalize_alias(text: str) -> str:
    return (text or "").strip().upper()


def _load_private_people() -> tuple[list, list]:
    data = read_json(PRIVATE_PEOPLE_FILE, {})
    if not isinstance(data, dict):
        return [], []
    people = data.get("people") if isinstance(data.get("people"), list) else []
    emergencies = data.get("emergencies") if isinstance(data.get("emergencies"), list) else []
    return people, emergencies


def _match_alias(token: str, entries: list) -> dict | None:
    needle = _normalize_alias(token)
    for item in entries:
        if not isinstance(item, dict):
            continue
        aliases = item.get("aliases") or []
        if not isinstance(aliases, list):
            continue
        for a in aliases:
            if _normalize_alias(a) == needle:
                return item
    return None


def _fetch_fc_list(access_token: str) -> tuple[list, str | None]:
    url = f"{CRUISE_BACKEND_BASE}/frequent-cruisers-customer"
    r = requests.get(url, headers=_cruise_payment_headers(access_token), timeout=20)
    if r.status_code == 401:
        raise PermissionError("fc unauthorized")
    r.raise_for_status()
    payload = r.json() or {}
    customer_mmid = None
    if isinstance(payload, dict):
        customer_mmid = payload.get("customer_mmid") or payload.get("customerMmid")
        data = payload.get("data")
        if isinstance(data, list):
            fc_list = [x for x in data if isinstance(x, dict)]
            if not customer_mmid:
                for item in fc_list:
                    mmid = item.get("customer_mmid") or item.get("customerMmid")
                    if mmid:
                        customer_mmid = mmid
                        break
            return fc_list, customer_mmid
    if isinstance(payload, list):
        fc_list = [x for x in payload if isinstance(x, dict)]
        if not customer_mmid:
            for item in fc_list:
                mmid = item.get("customer_mmid") or item.get("customerMmid")
                if mmid:
                    customer_mmid = mmid
                    break
        return fc_list, customer_mmid
    return [], customer_mmid


def _match_fc_person(fc_list: list, fc_match: dict) -> dict | None:
    if not isinstance(fc_match, dict):
        return None
    passport = fc_match.get("passport_number")
    given = fc_match.get("given_name")
    surname = fc_match.get("surname")
    chinese = fc_match.get("chinese_name")
    for p in fc_list:
        if passport and p.get("passport_number") == passport:
            return p
    for p in fc_list:
        if chinese and p.get("chinese_name") == chinese:
            return p
        if given and surname and p.get("given_name") == given and p.get("surname") == surname:
            return p
    return None


def _normalize_name_value(value: str | None) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().lower()


def _find_fc_matches(
    fc_list: list,
    token: str,
    hints: dict | None,
) -> tuple[dict | None, str | None]:
    if not isinstance(fc_list, list):
        return None, "找不到該乘客的親友資料，請先加入常用旅客或補 private_people.json"
    hints = hints or {}
    token_norm = _normalize_name_value(token)

    chinese = _normalize_name_value(hints.get("chinese_name")) or token_norm
    given = _normalize_name_value(hints.get("first_name"))
    surname = _normalize_name_value(hints.get("last_name"))
    passport = _normalize_name_value(hints.get("passport_number"))
    dob = _normalize_name_value(hints.get("date_of_birth"))

    if token_norm and (" " in token_norm or "-" in token_norm):
        parts = [p for p in re.split(r"[\s]+", token_norm) if p]
        if len(parts) >= 2 and not (given and surname):
            given = given or parts[0]
            surname = surname or parts[-1]

    def match_by(field: str, value: str) -> list:
        if not value:
            return []
        matches = []
        for p in fc_list:
            v = _normalize_name_value(p.get(field))
            if v and v == value:
                matches.append(p)
        return matches

    matches = match_by("chinese_name", chinese)
    if matches:
        if len(matches) > 1:
            return None, "乘客資料不只一筆，請提供更完整資料以辨識"
        return matches[0], None

    if given and surname:
        matches = [
            p for p in fc_list
            if _normalize_name_value(p.get("given_name")) == given
            and _normalize_name_value(p.get("surname")) == surname
        ]
        if matches:
            if len(matches) > 1:
                return None, "乘客資料不只一筆，請提供更完整資料以辨識"
            return matches[0], None

    matches = match_by("passport_number", passport)
    if matches:
        if len(matches) > 1:
            return None, "乘客資料不只一筆，請提供更完整資料以辨識"
        return matches[0], None

    matches = match_by("date_of_birth", dob)
    if matches:
        if len(matches) > 1:
            return None, "乘客資料不只一筆，請提供更完整資料以辨識"
        return matches[0], None

    return None, "找不到該乘客的親友資料，請先加入常用旅客或補 private_people.json"


def _merge_dict(base: dict, override: dict) -> dict:
    merged = dict(base or {})
    for k, v in (override or {}).items():
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        merged[k] = v
    return merged


def _map_fc_to_passenger(fc: dict) -> dict:
    if not isinstance(fc, dict):
        return {}
    return {
        "first_name": fc.get("given_name") or "",
        "last_name": fc.get("surname") or "",
        "chinese_name": fc.get("chinese_name") or "",
        "date_of_birth": fc.get("date_of_birth") or "",
        "gender": fc.get("gender") or "",
        "nationality": fc.get("nationality") or "",
        "passport_issuance_country": fc.get("passport_issuance_country") or "",
        "passport_number": fc.get("passport_number") or "",
        "passport_issuance_date": fc.get("passport_issuance_date") or "",
        "passport_expiry_date": fc.get("passport_expiry_date") or "",
    }


def _normalize_gender(value: str) -> str:
    if not isinstance(value, str):
        return value
    v = value.strip().lower()
    if v in ("m", "male"):
        return "male"
    if v in ("f", "female"):
        return "female"
    return value


def _normalize_phone_digits(value: str) -> str:
    if not isinstance(value, str):
        return value
    return re.sub(r"[^\d]", "", value)


def _mask_id(value: str | int | None, keep: int = 5) -> str:
    if value is None:
        return ""
    s = str(value)
    if len(s) <= keep:
        return s
    return s[:keep] + "***"


def _looks_like_mmid(value: str | None) -> bool:
    if not isinstance(value, str):
        return False
    return re.fullmatch(r"\d+", value.strip()) is not None


def set_current_user_mmid(mmid: str) -> None:
    if not isinstance(mmid, str) or not mmid.strip().isdigit():
        return
    _latest_tokens["user_mmid"] = mmid.strip()
    write_json(TOKENS_CACHE_FILE, _latest_tokens)


def _validate_mmid(
    access_token: str,
    booking_id: int,
    record_updated_time: str,
    mmid_list: list[str] | None = None,
    fc_ids: list[int] | None = None,
) -> tuple[bool, dict | str]:
    if (not mmid_list and not fc_ids) or (mmid_list and fc_ids):
        return False, "validate-mmid 參數錯誤：請提供 mmid 或 fc_ids"
    url = f"{CRUISE_BACKEND_BASE}/customers/v2/validate-mmid"
    payload = {
        "mmid": [m for m in (mmid_list or []) if m],
        "fc_ids": [i for i in (fc_ids or []) if i],
        "id": booking_id,
        "recordUpdatedTime": record_updated_time,
    }
    r = requests.post(url, headers=_cruise_payment_headers(access_token), json=payload, timeout=20)
    if r.status_code == 401:
        return False, "Token 已過期，請重新登入 SDC 後再試"
    if r.status_code >= 400:
        return False, f"validate-mmid failed ({r.status_code})"
    data = r.json() or {}
    if isinstance(data, dict) and (data.get("status_code") or data.get("messages")):
        msg = data.get("messages") or data.get("message") or "validate-mmid failed"
        return False, msg
    return True, data if isinstance(data, dict) else {}


def _require_fields(
    passenger: dict,
    label: str,
    require_contact: bool = True,
    require_passport: bool = True,
) -> list:
    required = [
        "first_name",
        "last_name",
        "date_of_birth",
        "gender",
        "nationality",
    ]
    if require_passport:
        required += [
            "passport_issuance_country",
            "passport_number",
            "passport_issuance_date",
            "passport_expiry_date",
        ]
    if require_contact:
        required += [
            "email",
            "re-email",
            "phone_country_code",
            "phone_number",
        ]
    missing = []
    for key in required:
        v = passenger.get(key)
        if not v or (isinstance(v, str) and not v.strip()):
            missing.append(f"{label}:{key}")
    return missing


def _require_emergency_fields(emergency: dict) -> list:
    required = [
        "given_name",
        "surname",
        "relationship_type",
        "phone_country_code",
        "phone_country_code_number",
        "phone_number",
        "email",
    ]
    missing = []
    for key in required:
        v = emergency.get(key)
        if not v or (isinstance(v, str) and not v.strip()):
            missing.append(f"\u7dca\u6025\u806f\u7d61\u4eba:{key}")
    return missing


def _resolve_allotment(access_token: str, date: str, tier: int, pax: int) -> tuple[dict | None, str | None]:
    itinerary_name = fetch_itinerary(access_token, date)
    if not itinerary_name:
        return None, "\u8a72\u65e5\u671f\u6c92\u6709\u63a2\u7d22\u661f\u865f\u822a\u7a0b"
    port_info = fetch_port(access_token, date)
    if not port_info or port_info.get("departure_port") is None:
        return None, "\u67e5\u7121\u53ef\u7528\u51fa\u767c\u6e2f\u53e3"

    params = {
        "itinerary_name": itinerary_name,
        "departure_date": date,
        "departure_port": str(port_info.get("departure_port")),
        "pax": str(pax),
        "lang": "hant",
    }
    try:
        r = requests.get(
            f"{CRUISE_BACKEND_BASE}/customers/cabin-allotment",
            params=params,
            headers=_cruise_payment_headers(access_token),
            timeout=20,
        )
    except Exception:
        return None, "\u67e5\u8a62\u623f\u578b\u5931\u6557\uff0c\u8acb\u7a0d\u5f8c\u518d\u8a66"
    if r.status_code in (401, 403):
        trigger_relogin("cabin-allotment", f"status={r.status_code}")
        return None, "Token \u5df2\u904e\u671f\uff0c\u8acb\u91cd\u65b0\u767b\u5165 SDC \u5f8c\u518d\u8a66"
    try:
        r.raise_for_status()
    except Exception:
        return None, "\u67e5\u8a62\u623f\u578b\u5931\u6557\uff0c\u8acb\u7a0d\u5f8c\u518d\u8a66"
    data = r.json() or {}
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        items = []

    tier_keywords = {
        3: ["balcony", "terrace", "balcony cabin", "\u9732\u53f0", "\u967d\u53f0"],
        2: ["oceanview", "ocean view", "\u6d77\u666f"],
        1: ["interior", "\u5167\u5074", "\u5167\u8259"],
    }
    keywords = tier_keywords.get(tier, [])

    def pick_item():
        for it in items:
            name = (it.get("cabin_name") or it.get("cabin_category_name") or "").lower()
            for kw in keywords:
                if kw.lower() in name:
                    return it
        return None

    picked = pick_item()
    if not picked:
        TIER_LABEL = {1: "\u5167\u5074", 2: "\u6d77\u666f", 3: "\u9732\u53f0"}
        label = TIER_LABEL.get(tier, "\u6307\u5b9a")
        return None, f"\u6c92\u6709{label}\u623f"

    try:
        print(f"[{ts()}] [CRUISE] allotment picked keys: {list(picked.keys())}", flush=True)
        picked_head = json.dumps(picked, ensure_ascii=False)[:800]
        print(f"[{ts()}] [CRUISE] allotment picked head: {picked_head}", flush=True)
        data_head = json.dumps(data, ensure_ascii=False)[:800] if isinstance(data, dict) else ""
        if data_head:
            print(f"[{ts()}] [CRUISE] allotment data head: {data_head}", flush=True)
    except Exception as ex:
        print(f"[{ts()}] [CRUISE] allotment debug log failed:", repr(ex), flush=True)

    result = {
        "cabin_allotment_id": picked.get("cabin_allotment_id") or picked.get("id"),
        "itinerary_id": (data.get("itinerary_id") or data.get("itineraryId")) if isinstance(data, dict) else None,
        "non_member_surcharge_id": (data.get("non_member_surcharge_id") or data.get("nonMemberSurchargeId")) if isinstance(data, dict) else None,
        "record_updated_time": picked.get("recordUpdatedTime") or picked.get("record_updated_time") or data.get("recordUpdatedTime"),
        "itinerary_name": itinerary_name,
        "port_name": port_info.get("port_name") or "",
    }
    if not result["itinerary_id"]:
        result["itinerary_id"] = picked.get("itinerary_id") or picked.get("itineraryId")
    if not result["non_member_surcharge_id"]:
        result["non_member_surcharge_id"] = picked.get("non_member_surcharge_id") or picked.get("nonMemberSurchargeId")
    missing = []
    if not result["cabin_allotment_id"]:
        missing.append("cabin_allotment_id")
    if not result["itinerary_id"]:
        missing.append("itinerary_id")
    if missing:
        return None, f"\u7121\u6cd5\u53d6\u5f97\u8a02\u623f\u53c3\u6578\uff0c\u7f3a\u5c11\uff1a{', '.join(missing)}"
    return result, None


def _build_booking_payload(
    numeric_id: int,
    cabin_allotment_id: int,
    customer_pax: int,
    record_updated_time: str,
    main_passenger: dict,
    passengers: list,
    emergency_contact: dict,
) -> dict:
    return {
        "id": numeric_id,
        "cabin_allotment_id": cabin_allotment_id,
        "customer_pax": customer_pax,
        "recordUpdatedTime": record_updated_time,
        "email": main_passenger.get("email"),
        "given_name": main_passenger.get("first_name"),
        "surname": main_passenger.get("last_name"),
        "emergency_contact": emergency_contact,
        "main_passenger": main_passenger,
        "passenger_list": passengers,
    }


def _format_passenger_details(passenger: dict) -> str:
    return "\n".join([
        f"\u4e2d\u6587\u540d\uff1a{passenger.get('chinese_name') or ''}",
        f"English\uff1a{passenger.get('first_name')} {passenger.get('last_name')}",
        f"\u8b77\u7167\uff1a{passenger.get('passport_number')} / {passenger.get('passport_issuance_date')} / {passenger.get('passport_expiry_date')}",
        f"\u767c\u7167\u5730\uff1a{passenger.get('passport_issuance_country')}",
        f"\u751f\u65e5\uff1a{passenger.get('date_of_birth')}  \u6027\u5225\uff1a{passenger.get('gender')}",
        f"Email\uff1a{passenger.get('email')} / {passenger.get('re-email')}",
        f"\u96fb\u8a71\uff1a{passenger.get('phone_country_code')} {passenger.get('phone_number')}",
    ])


def _format_emergency_details(emergency: dict) -> str:
    return "\n".join([
        f"\u4e2d\u6587\u540d\uff1a{emergency.get('chinese_name') or ''}",
        f"English\uff1a{emergency.get('given_name')} {emergency.get('surname')}",
        f"\u95dc\u4fc2\uff1a{emergency.get('relationship_type')}",
        f"\u96fb\u8a71\uff1a{emergency.get('phone_country_code')} +{emergency.get('phone_country_code_number')} {emergency.get('phone_number')}",
        f"Email\uff1a{emergency.get('email')}",
    ])


def _split_line_messages(text: str, limit: int = 1800) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts = []
    buf = []
    count = 0
    for line in text.splitlines():
        if count + len(line) + 1 > limit and buf:
            parts.append("\n".join(buf))
            buf = [line]
            count = len(line) + 1
        else:
            buf.append(line)
            count += len(line) + 1
    if buf:
        parts.append("\n".join(buf))
    return parts


def _prepare_passengers_from_private(names: list[str]) -> tuple[dict | None, list | None, dict | None, list]:
    people, emergencies = _load_private_people()
    errors = []

    def build_passenger(token: str, label: str) -> dict | None:
        entry = _match_alias(token, people)
        if not entry:
            errors.append(f"\u627e\u4e0d\u5230\u4e58\u5ba2\u8cc7\u6599\uff1a{label}({token})")
            return None
        passenger = entry.get("passenger") if isinstance(entry.get("passenger"), dict) else {}
        overrides = entry.get("passenger_overrides") if isinstance(entry.get("passenger_overrides"), dict) else {}
        base = _merge_dict(passenger, overrides)
        base["gender"] = _normalize_gender(base.get("gender"))
        if not base.get("nationality"):
            base["nationality"] = "TW"
        if base.get("email"):
            base["re-email"] = base.get("email")
        if is_main:
            require_contact = True
            require_passport = True
        else:
            require_contact = False
            require_passport = is_member
        missing = _require_fields(
            base,
            label,
            require_contact=require_contact,
            require_passport=require_passport,
        )
        if missing:
            errors.append("\u7f3a\u5c11\u5fc5\u586b\u6b04\u4f4d\uff1a" + ", ".join(missing))
            return None
        return base

    main = build_passenger(names[0], "\u4e3b\u4e58\u5ba2")
    companions = []
    for idx, token in enumerate(names[1:-1], 1):
        p = build_passenger(token, f"\u540c\u884c{idx}")
        if p:
            companions.append(p)

    emergency_entry = _match_alias(names[-1], emergencies)
    emergency = None
    if not emergency_entry:
        errors.append(f"\u627e\u4e0d\u5230\u7dca\u6025\u806f\u7d61\u4eba\uff1a{names[-1]}")
    else:
        emergency = emergency_entry.get("emergency_contact") if isinstance(emergency_entry.get("emergency_contact"), dict) else {}
        missing_emg = _require_emergency_fields(emergency)
        if missing_emg:
            errors.append("\u7dca\u6025\u806f\u7d61\u4eba\u7f3a\u6b04\uff1a" + ", ".join(missing_emg))

    return main, companions, emergency, errors


def process_cruise_text_command(
    text: str,
    line_user_id: str,
    reply_token: str | None = None,
    reply: bool = False,
    dry_run: bool = False,
    source_type: str = "webhook",
) -> dict:
    result = {
        "ok": False,
        "input": {"text": text, "line_user_id": line_user_id},
        "parsed": {},
        "actions": {
            "booking": {"attempted": False},
            "paylink": {"attempted": False},
        },
        "errors": [],
    }

    raw_text = (text or "").strip()
    if not raw_text:
        result["errors"].append("missing text")
        return result
    if not raw_text.startswith(("\u8a02\u623f", "\u8ba2\u623f")):
        result["errors"].append("unsupported command")
        return result

    json_mode = raw_text.startswith(("\u8a02\u623f {", "\u8ba2\u623f {"))
    if json_mode:
        payload_text = raw_text[2:].strip()
        if not payload_text:
            result["errors"].append("missing JSON payload")
            result["error_type"] = "parse_error"
            return result
        try:
            payload = json.loads(payload_text)
        except Exception:
            result["errors"].append("invalid JSON payload")
            result["error_type"] = "parse_error"
            return result
        result["parsed"] = {"send_flag": True}
        if dry_run:
            result["actions"]["booking"] = {"attempted": False, "booking_request": payload}
            result["actions"]["paylink"] = {"attempted": False, "paylink_record": {"ttl_seconds": payload.get("ttl_seconds")}}
            result["ok"] = True
            return result
        trace_id = _make_trace_id()
        flow_result, status = _book_and_paylink_flow(payload, _get_base_url(), trace_id)
        result["actions"]["booking"]["attempted"] = True
        result["actions"]["paylink"]["attempted"] = True
        if status != 200 or not flow_result.get("ok"):
            result["errors"].append(flow_result.get("error") or "booking failed")
            if CRUISE_RELOGIN_NEEDED:
                result["relogin_required"] = True
                result["error_type"] = "relogin_required"
            elif status >= 500:
                result["error_type"] = "upstream_error"
            else:
                result["error_type"] = "parse_error"
            return result
        result["ok"] = True
        result["actions"]["paylink"].update({
            "pay_url": flow_result.get("pay_url"),
            "expires_at": flow_result.get("expires_at"),
            "summary_text": flow_result.get("summary_text"),
        })
        if reply or reply_token:
            summary = flow_result.get("summary_text") or ""
            pay_url = flow_result.get("pay_url") or ""
            messages = []
            if summary:
                for chunk in _split_line_messages(summary):
                    messages.append(TextSendMessage(text=chunk))
            if pay_url:
                messages.append(TextSendMessage(text=f"👉 前往付款：{pay_url}"))
            if messages:
                if reply_token:
                    cruise_line_bot_api.reply_message(reply_token, messages[:5])
                else:
                    cruise_line_bot_api.push_message(line_user_id, messages[:5])
                result["sent_to"] = line_user_id
        return result

    parts = raw_text.split()
    if len(parts) < 5:
        result["errors"].append(
            "格式錯誤，請用：\n"
            "訂房 <日期> <房型> <主乘客> [同行乘客...] <緊急聯絡人>\n"
            "日期支援：2026-02-22 / 2026/2/22 / 2026.02.22 / 20260222\n"
            "房型：內側 / 海景 / 露臺 / 陽台（露臺=陽台）"
        )
        result["error_type"] = "parse_error"
        return result
    date_text = _parse_flexible_date(parts[1])
    tier = _parse_tier(parts[2])
    names = parts[3:]
    result["parsed"] = {
        "date": date_text,
        "cabin_type": parts[2],
        "passengers": names,
        "main_passenger": names[0] if names else None,
        "emergency_contact": names[-1] if names else None,
        "send_flag": True,
    }
    if not date_text:
        result["errors"].append("日期格式錯誤")
        result["error_type"] = "parse_error"
        return result
    if not tier:
        result["errors"].append("invalid cabin type")
        result["error_type"] = "parse_error"
        return result
    if len(names) < 2:
        result["errors"].append(
            "格式錯誤，請用：\n"
            "訂房 <日期> <房型> <主乘客> [同行乘客...] <緊急聯絡人>\n"
            "日期支援：2026-02-22 / 2026/2/22 / 2026.02.22 / 20260222\n"
            "房型：內側 / 海景 / 露臺 / 陽台（露臺=陽台）"
        )
        result["error_type"] = "parse_error"
        return result

    if dry_run:
        main, companions, emergency, errors = _prepare_passengers_from_private(names)
        if errors:
            result["errors"].extend(errors)
        booking_request = {
            "customer_pax": max(len(names) - 1, 0),
            "recordUpdatedTime": None,
            "main_passenger": main,
            "passenger_list": companions or [],
            "emergency_contact": emergency,
        }
        result["actions"]["booking"] = {"attempted": False, "booking_request": booking_request}
        result["actions"]["paylink"] = {"attempted": False, "paylink_record": {"ttl_seconds": 1800}}
        result["ok"] = not result["errors"]
        if result["errors"]:
            result["error_type"] = "parse_error"
        return result

    people_list, _ = _load_private_people()
    selected_main = _match_alias(names[0], people_list)
    if not selected_main:
        result["errors"].append(f"main passenger not found: {names[0]}")
        result["error_type"] = "parse_error"
        return result
    current_user = _latest_tokens.get("user_mmid")
    if not _looks_like_mmid(current_user):
        current_user = None
    main_is_member = bool(selected_main.get("is_member"))
    main_mmid = selected_main.get("mmid")

    trace_id = _make_trace_id()
    flow_result, status = _book_and_paylink_with_people(
        date=date_text,
        tier=tier,
        names=names,
        ttl_seconds=1800,
        trace_id=trace_id,
    )
    result["actions"]["booking"]["attempted"] = True
    result["actions"]["paylink"]["attempted"] = True
    if status != 200 or not flow_result.get("ok"):
        result["errors"].append(flow_result.get("error") or "booking failed")
        if CRUISE_RELOGIN_NEEDED:
            result["relogin_required"] = True
            result["error_type"] = "relogin_required"
        elif status >= 500:
            result["error_type"] = "upstream_error"
        else:
            result["error_type"] = "parse_error"
        return result
    result["ok"] = True
    result["actions"]["paylink"].update({
        "pay_url": flow_result.get("pay_url"),
        "expires_at": flow_result.get("expires_at"),
        "summary_text": flow_result.get("summary_text"),
    })
    if CRUISE_RELOGIN_NEEDED:
        result["relogin_required"] = True
    if reply or reply_token:
        summary = flow_result.get("summary_text") or ""
        pay_url = flow_result.get("pay_url") or ""
        messages = []
        if summary:
            for chunk in _split_line_messages(summary):
                messages.append(TextSendMessage(text=chunk))
        if pay_url:
            messages.append(TextSendMessage(text=f"👉 前往付款：{pay_url}"))
        if messages:
            if reply_token:
                cruise_line_bot_api.reply_message(reply_token, messages[:5])
            else:
                cruise_line_bot_api.push_message(line_user_id, messages[:5])
            result["sent_to"] = line_user_id
    return result

def _resolve_passengers_and_emergency(
    access_token: str,
    numeric_id: int,
    record_updated_time: str,
    names: list[str],
) -> tuple[dict | None, list | None, dict | None, str | None]:
    people, emergencies = _load_private_people()

    booking_summary = fetch_booking_summary(access_token, numeric_id) or {}
    latest_record_updated_time = _extract_record_updated_time(booking_summary, record_updated_time)
    if not latest_record_updated_time:
        return None, None, None, "無法取得 recordUpdatedTime，請重試"

    def current_user_mmid() -> str | None:
        mmid = _latest_tokens.get("user_mmid")
        if _looks_like_mmid(mmid):
            return str(mmid)
        return None

    fc_list = None

    def get_fc_list():
        nonlocal fc_list
        if fc_list is None:
            fc_list, _ = _fetch_fc_list(access_token)
        return fc_list

    def merge_validate_fields(base: dict, result: dict):
        if not isinstance(result, dict):
            return base
        if not base.get("date_of_birth") and result.get("dob"):
            base["date_of_birth"] = result.get("dob")
        if not base.get("gender") and result.get("gender"):
            base["gender"] = result.get("gender")
        if not base.get("email") and result.get("email"):
            base["email"] = result.get("email")
        if not base.get("phone_number") and result.get("phone_number"):
            base["phone_number"] = result.get("phone_number")
        return base

    main_token = names[0]
    companion_tokens = names[1:-1]
    emergency_token = names[-1]

    def build_passenger(token: str, label: str, is_main: bool) -> tuple[dict | None, str | None]:
        entry = _match_alias(token, people)
        is_member = bool(entry and entry.get("is_member"))
        if is_member:
            mmid = entry.get("mmid")
            if not isinstance(mmid, str) or not mmid.strip():
                return None, "會員乘客缺少 mmid，請在 private_people.json 補齊"
            if is_main:
                current = current_user_mmid()

            passenger = entry.get("passenger") if isinstance(entry.get("passenger"), dict) else {}
            overrides = entry.get("passenger_overrides") if isinstance(entry.get("passenger_overrides"), dict) else {}
            base = _merge_dict(passenger, overrides)
            ok, result = _validate_mmid(
                access_token,
                numeric_id,
                latest_record_updated_time,
                mmid_list=[mmid],
                fc_ids=[],
            )
            if not ok:
                return None, f"會員驗證失敗：{mmid} {result}"
            base = merge_validate_fields(base, result if isinstance(result, dict) else {})
            if is_main:
                set_current_user_mmid(mmid)
        else:
            if is_main:
                return None, "主乘客必須為會員，請在 private_people.json 設定 is_member=true"
            try:
                fc_list_local = get_fc_list()
            except PermissionError:
                return None, "Token 已過期，請重新登入 SDC 後再試"

            overrides = {}
            hints = {}
            if entry:
                if isinstance(entry.get("passenger"), dict):
                    hints = dict(entry.get("passenger"))
                if isinstance(entry.get("passenger_overrides"), dict):
                    overrides = dict(entry.get("passenger_overrides"))
                    hints = _merge_dict(hints, overrides)

            fc_person, err = _find_fc_matches(fc_list_local, token, hints)
            if err:
                return None, err
            fc_id = fc_person.get("id")
            if not fc_id:
                return None, "找不到該乘客的親友資料，請先加入常用旅客或補 private_people.json"

            base = _map_fc_to_passenger(fc_person)
            if overrides:
                base = _merge_dict(base, overrides)

            ok, result = _validate_mmid(
                access_token,
                numeric_id,
                latest_record_updated_time,
                mmid_list=[],
                fc_ids=[int(fc_id)],
            )
            if not ok:
                return None, f"常用旅客驗證失敗：{result}"
            base = merge_validate_fields(base, result if isinstance(result, dict) else {})

        base["gender"] = _normalize_gender(base.get("gender"))
        if base.get("phone_number"):
            base["phone_number"] = _normalize_phone_digits(base.get("phone_number"))
        if not base.get("nationality"):
            base["nationality"] = "TW"
        if base.get("email"):
            base["re-email"] = base.get("email")
        if is_main:
            require_contact = True
            require_passport = True
        else:
            require_contact = False
            require_passport = is_member
        missing = _require_fields(
            base,
            label,
            require_contact=require_contact,
            require_passport=require_passport,
        )
        if missing:
            return None, "缺少必填欄位：" + ", ".join(missing)
        return base, None

    main_passenger, err = build_passenger(main_token, "\u4e3b\u4e58\u5ba2", True)
    if err:
        return None, None, None, err

    companions = []
    for idx, token in enumerate(companion_tokens, 1):
        passenger, err = build_passenger(token, f"\u540c\u884c{idx}", False)
        if err:
            return None, None, None, err
        companions.append(passenger)

    emergency_entry = _match_alias(emergency_token, emergencies)
    if not emergency_entry:
        return None, None, None, f"找不到緊急聯絡人：{emergency_token}"
    emergency = emergency_entry.get("emergency_contact") if isinstance(emergency_entry.get("emergency_contact"), dict) else {}
    missing_emg = _require_emergency_fields(emergency)
    if missing_emg:
        return None, None, None, "緊急聯絡人缺少必要欄位：" + ", ".join(missing_emg)

    return main_passenger, companions, emergency, None


def _book_and_paylink_with_people(
    date: str,
    tier: int,
    names: list[str],
    ttl_seconds: int,
    trace_id: str,
) -> tuple[dict, int]:
    access_token = get_latest_access_token()
    if not access_token:
        return {"ok": False, "error": "\u8acb\u5148\u624b\u52d5\u767b\u5165\u4e00\u6b21\u8b93 Token Sync \u56de\u704c"}, 401

    pax = max(len(names) - 1, 0)
    allotment, err = _resolve_allotment(access_token, date, tier, pax)
    if err:
        return {"ok": False, "error": err}, 400

    cabin_allotment_id = int(allotment.get("cabin_allotment_id") or 0)
    itinerary_id = int(allotment.get("itinerary_id") or 0)
    non_member_surcharge_id = allotment.get("non_member_surcharge_id")
    try:
        non_member_surcharge_id = int(non_member_surcharge_id)
    except Exception:
        non_member_surcharge_id = None
    if non_member_surcharge_id is not None and non_member_surcharge_id <= 0:
        non_member_surcharge_id = None
    record_updated_time = _ensure_record_updated_time(None)

    draft_payload = {
        "cabin_allotment_id": cabin_allotment_id,
        "customer_pax": pax,
        "gratuity_charge_id": None,
        "itinerary_id": itinerary_id,
        "record_updated_time": record_updated_time,
    }
    if non_member_surcharge_id is not None:
        draft_payload["non_member_surcharge_id"] = non_member_surcharge_id

    headers = _cruise_payment_headers(access_token)
    draft_url = f"{CRUISE_BACKEND_BASE}/customers/v2/booking/draft"
    try:
        r = requests.post(draft_url, headers=headers, json=draft_payload, timeout=20)
    except Exception as ex:
        print(f"[{ts()}] [CRUISE] trace={trace_id} draft error={type(ex).__name__}", flush=True)
        return {"ok": False, "error": "\u5efa\u7acb\u8349\u7a3f\u5931\u6557\uff0c\u8acb\u7a0d\u5f8c\u91cd\u8a66"}, 502
    if r.status_code == 401:
        trigger_relogin("draft", f"status={r.status_code}")
        _log_backend_response(trace_id, "draft", r)
        return {"ok": False, "error": "Token \u5df2\u904e\u671f\uff0c\u8acb\u91cd\u65b0\u767b\u5165 SDC \u5f8c\u518d\u8a66"}, 401
    if r.status_code >= 400:
        _log_backend_response(trace_id, "draft", r)
        body_head = ""
        try:
            body_head = (r.text or "")[:300]
        except Exception:
            body_head = ""
        return {
            "ok": False,
            "error": "後端建立草稿失敗，請稍後重試",
            "debug": {"status": r.status_code, "body_head": body_head}
        }, 502

    draft_data = _parse_json_response(r) or {}
    booking_id = draft_data.get("booking_id")
    if not booking_id:
        return {"ok": False, "error": "\u5f8c\u7aef\u56de\u50b3\u7f3a\u5c11 booking_id"}, 502

    check_url = f"{CRUISE_BACKEND_BASE}/booking/check-status/{booking_id}"
    r = requests.get(check_url, headers=headers, timeout=20)
    if r.status_code == 401:
        trigger_relogin("check-status", f"status={r.status_code}")
        _log_backend_response(trace_id, "check-status", r)
        return {"ok": False, "error": "Token \u5df2\u904e\u671f\uff0c\u8acb\u91cd\u65b0\u767b\u5165 SDC \u5f8c\u518d\u8a66"}, 401
    r.raise_for_status()
    check_data = _parse_json_response(r) or {}
    numeric_id = check_data.get("id") or check_data.get("booking_id")
    try:
        numeric_id = int(numeric_id)
    except Exception:
        numeric_id = 0
    if numeric_id <= 0:
        return {"ok": False, "error": "\u7121\u6cd5\u53d6\u5f97 numeric_id"}, 502

    booking_url = f"{CRUISE_BACKEND_BASE}/booking/{numeric_id}"
    r = requests.get(booking_url, headers=headers, timeout=20)
    if r.status_code == 401:
        trigger_relogin("booking", f"status={r.status_code}")
        _log_backend_response(trace_id, "booking", r)
        return {"ok": False, "error": "Token \u5df2\u904e\u671f\uff0c\u8acb\u91cd\u65b0\u767b\u5165 SDC \u5f8c\u518d\u8a66"}, 401
    r.raise_for_status()
    booking_payload = _parse_json_response(r) or {}
    if isinstance(booking_payload, dict) and isinstance(booking_payload.get("data"), dict):
        booking_summary = booking_payload.get("data")
    else:
        booking_summary = booking_payload if isinstance(booking_payload, dict) else {}
    if not isinstance(booking_summary, dict):
        return {"ok": False, "error": "\u8a02\u55ae\u8cc7\u6599\u683c\u5f0f\u932f\u8aa4"}, 502

    latest_record_updated_time = _extract_record_updated_time(booking_summary, record_updated_time)
    if not latest_record_updated_time:
        r = requests.get(booking_url, headers=headers, timeout=20)
        if r.status_code == 401:
            trigger_relogin("booking-refresh", f"status={r.status_code}")
            _log_backend_response(trace_id, "booking-refresh", r)
            return {"ok": False, "error": "Token \u5df2\u904e\u671f\uff0c\u8acb\u91cd\u65b0\u767b\u5165 SDC \u5f8c\u518d\u8a66"}, 401
        r.raise_for_status()
        booking_payload = _parse_json_response(r) or {}
        if isinstance(booking_payload, dict) and isinstance(booking_payload.get("data"), dict):
            booking_summary = booking_payload.get("data")
        else:
            booking_summary = booking_payload if isinstance(booking_payload, dict) else {}
        latest_record_updated_time = _extract_record_updated_time(booking_summary, None)
        if not latest_record_updated_time:
            return {"ok": False, "error": "\u7121\u6cd5\u53d6\u5f97 recordUpdatedTime\uff0c\u8acb\u91cd\u8a66"}, 400

    main_passenger, companions, emergency, err = _resolve_passengers_and_emergency(
        access_token, numeric_id, latest_record_updated_time, names
    )
    if err:
        return {"ok": False, "error": err}, 400

    booking_payload = _build_booking_payload(
        numeric_id=numeric_id,
        cabin_allotment_id=cabin_allotment_id,
        customer_pax=pax,
        record_updated_time=latest_record_updated_time,
        main_passenger=main_passenger,
        passengers=companions,
        emergency_contact=emergency,
    )

    update_url = f"{CRUISE_BACKEND_BASE}/customers/v2/booking"
    r = requests.post(update_url, headers=headers, json=booking_payload, timeout=20)
    if r.status_code == 401:
        trigger_relogin("booking-update", f"status={r.status_code}")
        _log_backend_response(trace_id, "booking-update", r)
        return {"ok": False, "error": "Token \u5df2\u904e\u671f\uff0c\u8acb\u91cd\u65b0\u767b\u5165 SDC \u5f8c\u518d\u8a66"}, 401
    if r.status_code >= 400:
        _log_backend_response(trace_id, "booking-update", r)
        return {"ok": False, "error": "\u66f4\u65b0\u4e58\u5ba2\u8cc7\u6599\u5931\u6557"}, 502

    r = requests.get(booking_url, headers=headers, timeout=20)
    if r.status_code == 401:
        trigger_relogin("booking-refresh", f"status={r.status_code}")
        _log_backend_response(trace_id, "booking-refresh", r)
        return {"ok": False, "error": "Token \u5df2\u904e\u671f\uff0c\u8acb\u91cd\u65b0\u767b\u5165 SDC \u5f8c\u518d\u8a66"}, 401
    r.raise_for_status()
    booking_payload = _parse_json_response(r) or {}
    if isinstance(booking_payload, dict) and isinstance(booking_payload.get("data"), dict):
        booking_summary = booking_payload.get("data")
    else:
        booking_summary = booking_payload if isinstance(booking_payload, dict) else {}
    latest_record_updated_time = _extract_record_updated_time(booking_summary, latest_record_updated_time)
    if not latest_record_updated_time:
        r = requests.get(booking_url, headers=headers, timeout=20)
        if r.status_code == 401:
            trigger_relogin("booking-refresh", f"status={r.status_code}")
            _log_backend_response(trace_id, "booking-refresh", r)
            return {"ok": False, "error": "Token \u5df2\u904e\u671f\uff0c\u8acb\u91cd\u65b0\u767b\u5165 SDC \u5f8c\u518d\u8a66"}, 401
        r.raise_for_status()
        booking_payload = _parse_json_response(r) or {}
        if isinstance(booking_payload, dict) and isinstance(booking_payload.get("data"), dict):
            booking_summary = booking_payload.get("data")
        else:
            booking_summary = booking_payload if isinstance(booking_payload, dict) else {}
        latest_record_updated_time = _extract_record_updated_time(booking_summary, None)
        if not latest_record_updated_time:
            return {"ok": False, "error": "\u7121\u6cd5\u53d6\u5f97 recordUpdatedTime\uff0c\u8acb\u91cd\u8a66"}, 400

    payment_method = _build_payment_items("credit_card", include_surcharge=non_member_surcharge_id is not None)
    if not payment_method:
        return {"ok": False, "error": "invalid payment_method"}, 400
    paylink_entry = create_paylink_entry(
        booking_id=numeric_id,
        record_updated_time=latest_record_updated_time,
        payment_method=payment_method,
        ttl_seconds=ttl_seconds,
    )
    if not paylink_entry:
        return {"ok": False, "error": "\u7121\u6cd5\u5efa\u7acb\u4ed8\u6b3e\u9023\u7d50"}, 500

    pay_url = f"{_get_base_url()}/cruise/pay/{paylink_entry['code']}"
    summary_text = build_paylink_summary_text(
        booking_id=numeric_id,
        pay_url=pay_url,
        ttl_seconds=ttl_seconds,
        booking_summary=booking_summary if isinstance(booking_summary, dict) else None,
    )

    payload_preview = json.dumps({
        "customer_pax": pax,
        "recordUpdatedTime": latest_record_updated_time,
        "main_passenger": main_passenger,
        "passenger_list": companions,
        "emergency_contact": emergency,
    }, ensure_ascii=False)

    details_lines = [
        summary_text,
        f"\u4e58\u5ba2\u7e3d\u4eba\u6578\uff1a{pax}",
        "",
        "\u4e3b\u4e58\u5ba2\uff1a",
        _format_passenger_details(main_passenger),
    ]
    for idx, p in enumerate(companions, 1):
        details_lines.append("")
        details_lines.append(f"\u540c\u884c{idx}\uff1a")
        details_lines.append(_format_passenger_details(p))
    details_lines.append("")
    details_lines.append("\u7dca\u6025\u806f\u7d61\u4eba\uff1a")
    details_lines.append(_format_emergency_details(emergency))
    details_lines.append("")
    details_lines.append("\u5373\u5c07\u9001\u51fa\u7684 booking payload\uff1a")
    details_lines.append(payload_preview)

    return {
        "ok": True,
        "summary_text": "\n".join(details_lines),
        "pay_url": pay_url,
        "expires_at": datetime.utcfromtimestamp(paylink_entry["expires_at"]).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }, 200

def fetch_itinerary(access_token: str, date: str) -> str | None:
    url = f"{CRUISE_BACKEND_BASE}/customers/list/itinerary"
    params = {"departure_date": date, "lang": "hant", "page": 1}
    r = requests.get(url, params=params, headers=_cruise_headers(access_token), timeout=10)
    if r.status_code in (401, 403):
        trigger_relogin("fetch_itinerary", f"status={r.status_code}")
        return None
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
    if r.status_code in (401, 403):
        trigger_relogin("fetch_port", f"status={r.status_code}")
        return None
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
    raw_text_lower = raw_text.lower()

    enable_cmds = ("\u555f\u7528", "\u958b\u555f", "\u5f00\u542f", "enable", "on")
    disable_cmds = ("\u505c\u7528", "\u95dc\u9589", "\u5173\u95ed", "disable", "off")
    toggle = None
    if cmd in enable_cmds or raw_text in enable_cmds or raw_text_lower in enable_cmds:
        toggle = True
    elif cmd in disable_cmds or raw_text in disable_cmds or raw_text_lower in disable_cmds:
        toggle = False
    elif any(raw_text.startswith(k) for k in enable_cmds):
        toggle = True
    elif any(raw_text.startswith(k) for k in disable_cmds):
        toggle = False

    if toggle is not None:
        ok = set_feature("costco", toggle)
        state = "\u555f\u7528" if toggle else "\u505c\u7528"
        print(f"[{ts()}] [COSTCO] feature {state}", flush=True)
        reply = f"\u2705 Costco \u529f\u80fd\u5df2{state}" if ok else "\u2757 \u7121\u6cd5\u66f4\u65b0 features.json"
        costco_line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    if not feature_enabled("costco"):
        reply = "Costco \u529f\u80fd\u76ee\u524d\u505c\u7528"
        costco_line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

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
        "\u529F\u80FD\u958B\u95DC\uff1a\u555f\u7528 / \u505c\u7528\n"
        "➕ 新增 [URL] [秒數] / add [URL] [秒數]  (未輸入秒數預設3分鐘)\n"
        "➖ 移除 [URL] / remove [URL]"
    )

    costco_line_bot_api.reply_message(event.reply_token, TextSendMessage(text=help_text))


@cruise_handler.add(MessageEvent, message=TextMessage)
def handle_cruise_message(event):
    user_id = event.source.user_id
    add_cruise_user(user_id)

    raw_text = (event.message.text or "").strip()
    raw_text_lower = raw_text.lower()

    enable_cmds = ("\u555f\u7528", "\u958b\u555f", "\u5f00\u542f", "enable", "on")
    disable_cmds = ("\u505c\u7528", "\u95dc\u9589", "\u5173\u95ed", "disable", "off")
    toggle = None
    if raw_text in enable_cmds or raw_text_lower in enable_cmds:
        toggle = True
    elif raw_text in disable_cmds or raw_text_lower in disable_cmds:
        toggle = False
    elif any(raw_text.startswith(k) for k in enable_cmds):
        toggle = True
    elif any(raw_text.startswith(k) for k in disable_cmds):
        toggle = False

    if toggle is not None:
        ok = set_feature("cruise_daemon", toggle)
        state = "\u555f\u7528" if toggle else "\u505c\u7528"
        print(f"[{ts()}] [CRUISE] feature {state}", flush=True)
        reply = f"\u2705 Cruise \u529f\u80fd\u5df2{state}" if ok else "\u2757 \u7121\u6cd5\u66f4\u65b0 features.json"
        cruise_line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    if not feature_enabled("cruise_daemon"):
        reply = "Cruise \u529f\u80fd\u76ee\u524d\u505c\u7528"
        cruise_line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    if raw_text.startswith(("\u8a02\u623f", "\u8ba2\u623f")):
        if not is_cruise_daemon_enabled():
            reply = "\u8a02\u623f\u529f\u80fd\u76ee\u524d\u672a\u555f\u7528"
            cruise_line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
            return
        if not is_cruise_admin(event.source.user_id):
            reply = "\u4f60\u6c92\u6709\u8a02\u623f\u6b0a\u9650\uff08\u50c5\u9650\u7ba1\u7406\u8005\uff09"
            cruise_line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
            return
        result = process_cruise_text_command(
            text=raw_text,
            line_user_id=event.source.user_id,
            reply_token=event.reply_token,
            reply=True,
            dry_run=False,
            source_type="webhook",
        )
        if not result.get("ok"):
            reason = (result.get("errors") or [""])[0]
            msg = (
                "\u683c\u5f0f\u932f\u8aa4\uff0c\u8acb\u7528\uff1a\n"
                "\u8a02\u623f <\u65e5\u671f> <\u623f\u578b> <\u4e3b\u4e58\u5ba2> [\u540c\u884c\u4e58\u5ba2...] <\u7dca\u6025\u806f\u7d61\u4eba> \u9001\u51fa\n"
                "\u65e5\u671f\u652f\u63f4\uff1a2026-02-22 / 2026/2/22 / 2026.02.22 / 20260222\n"
                "\u623f\u578b\uff1a\u5167\u5074 / \u6d77\u666f / \u9732\u53f0 / \u967d\u53f0\uff08\u9732\u53f0=\u967d\u53f0\uff09"
            )
            if reason:
                msg = f"{reason}\n\n" + msg
            cruise_line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        return

    list_keywords = ("列出監控", "監控列表", "顯示監控", "列出", "列表","LIST","List","list")
    delete_keywords = ("刪除", "移除", "取消","DELETE","Delete","delete")

    def parse_date_text(text: str) -> str | None:
        m = re.search(r"(\d{4})[-/\.]?(\d{2})[-/\.]?(\d{2})", text)
        if not m:
            return None
        y, mo, d = (int(v) for v in m.groups())
        try:
            return datetime(y, mo, d).strftime("%Y-%m-%d")
        except ValueError:
            return None

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

        def parse_date(v: str):
            try:
                return datetime.strptime(v, "%Y-%m-%d")
            except Exception:
                return datetime.max

        monitors_sorted = sorted(monitors, key=lambda m: parse_date(m.get("date") or ""))

        lines = []
        for m in monitors_sorted[:10]:
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
        if len(monitors_sorted) > 10:
            reply = reply + "\n(其餘省略)"
        cruise_line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    if any(k in raw_text for k in delete_keywords):
        date = parse_date_text(raw_text)
        if not date:
            reply = "\u8ACB\u8F38\u5165\uFF1A\u522A\u9664 YYYY-MM-DD\uFF08\u4E5F\u652F\u63F4 YYYYMMDD / YYYY/MM/DD / YYYY.MM.DD\uFF09"
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
    date = parse_date_text(raw_text)
    if not date:
        help_text = (
            "\u53ef\u7528\u6307\u4ee4\uff1a\n\n"
            "\u5217\u51fa\u76e3\u63a7 / \u76e3\u63a7\u5217\u8868 / \u986f\u793a\u76e3\u63a7 / \u5217\u51fa / \u5217\u8868\n"
            "\u522A\u9664/\u79FB\u9664/\u53D6\u6D88 YYYY-MM-DD\uFF08\u4E5F\u652F\u63F4 YYYYMMDD / YYYY/MM/DD / YYYY.MM.DD\uFF09\n"
            "\u529F\u80FD\u958B\u95DC\uff1a\u555f\u7528 / \u505c\u7528\n"
            "YYYY-MM-DD [\u5167\u5074/\u6d77\u666f/\u9732\u53f0]\n"
            "\u4f8b\u5982\uff1a2026-02-27 \u6d77\u666f"
        )
        cruise_line_bot_api.reply_message(event.reply_token, TextSendMessage(text=help_text))
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
