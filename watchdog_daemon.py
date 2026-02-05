import json
import os
import time

import requests
from dotenv import load_dotenv
from filelock import FileLock
from linebot import LineBotApi
from linebot.models import TextSendMessage

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FEATURES_FILE = os.path.join(BASE_DIR, "features.json")
USERS_COSTCO_FILE = os.path.join(BASE_DIR, "users.json")
USERS_CRUISE_FILE = os.path.join(BASE_DIR, "users_cruise.json")

STATE_DIR = os.path.join(BASE_DIR, "state")
HEARTBEAT_COSTCO_FILE = os.path.join(STATE_DIR, "heartbeat_costco.json")
HEARTBEAT_CRUISE_FILE = os.path.join(STATE_DIR, "heartbeat_cruise_daemon.json")

BOT_HEALTH_URL = "http://127.0.0.1:5000/health"
NGROK_TUNNELS_URL = "http://127.0.0.1:4040/api/tunnels"

CHECK_INTERVAL_SECONDS = 15
STARTUP_GRACE_SECONDS = 60
FAIL_THRESHOLD = 2
HEARTBEAT_STALE_COSTCO_SECONDS = 600
HEARTBEAT_STALE_CRUISE_SECONDS = 600


def ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


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
        print(f"[{ts()}] [WATCHDOG] warn: failed to read {path}: {type(e).__name__}: {e}", flush=True)
        return default


def read_features() -> dict:
    defaults = {"bot_server": True, "costco": True, "cruise_daemon": True, "ngrok": True}
    try:
        if not os.path.exists(FEATURES_FILE):
            return defaults
        lock = FileLock(FEATURES_FILE + ".lock")
        with lock:
            data = read_json(FEATURES_FILE, defaults)
    except Exception as e:
        print(f"[{ts()}] [WATCHDOG] warn: failed to read {FEATURES_FILE}: {type(e).__name__}: {e}", flush=True)
        return defaults
    if not isinstance(data, dict):
        return defaults
    merged = defaults.copy()
    for key in merged:
        if key in data:
            merged[key] = bool(data.get(key))
    return merged


def load_user_ids(path: str) -> list[str]:
    ids = read_json(path, [])
    if not isinstance(ids, list):
        return []
    out: list[str] = []
    for x in ids:
        if isinstance(x, str) and x.strip():
            out.append(x.strip())
    return out


def _push(line_api: LineBotApi | None, user_ids: list[str], text: str, channel_label: str) -> None:
    if not line_api:
        print(f"[{ts()}] [WATCHDOG] warn: {channel_label} LINE token not configured; skip notify", flush=True)
        return
    if not user_ids:
        print(f"[{ts()}] [WATCHDOG] warn: {channel_label} users list empty; skip notify", flush=True)
        return
    msg = TextSendMessage(text=text)
    for uid in user_ids:
        try:
            line_api.push_message(uid, msg)
        except Exception as e:
            print(
                f"[{ts()}] [WATCHDOG] warn: push failed channel={channel_label} uid={uid}: "
                f"{type(e).__name__}: {e}",
                flush=True,
            )


def notify(scope: str, text: str, features: dict, costco_api: LineBotApi | None, cruise_api: LineBotApi | None) -> None:
    # scope: "costco" | "cruise" | "all"
    allow_costco = bool((features or {}).get("costco", False))
    allow_cruise = bool((features or {}).get("cruise_daemon", False))

    if allow_costco and scope in ("costco", "all"):
        _push(costco_api, load_user_ids(USERS_COSTCO_FILE), text, "costco")
    if allow_cruise and scope in ("cruise", "all"):
        _push(cruise_api, load_user_ids(USERS_CRUISE_FILE), text, "cruise")


def _body_head(text: str, limit: int = 200) -> str:
    return (text or "")[:limit].replace("\n", " ").replace("\r", " ")


def check_bot_server() -> tuple[bool, str]:
    try:
        r = requests.get(BOT_HEALTH_URL, timeout=5)
        if r.status_code != 200:
            return False, f"health status={r.status_code} body_head={_body_head(r.text)}"
        payload = r.json()
        if isinstance(payload, dict) and payload.get("ok") is True:
            return True, ""
        return False, f"health payload invalid body_head={_body_head(r.text)}"
    except Exception as e:
        return False, f"health request failed {type(e).__name__}: {e}"


def check_ngrok() -> tuple[bool, str]:
    try:
        r = requests.get(NGROK_TUNNELS_URL, timeout=5)
        if r.status_code != 200:
            return False, f"ngrok api status={r.status_code} body_head={_body_head(r.text)}"
        return True, ""
    except Exception as e:
        return False, f"ngrok api request failed {type(e).__name__}: {e}"


def check_heartbeat(path: str, stale_seconds: int) -> tuple[bool, str]:
    data = read_json(path, None)
    if not isinstance(data, dict):
        return False, "heartbeat missing"
    try:
        hb_ts = float(data.get("ts") or 0)
    except Exception:
        hb_ts = 0.0
    if hb_ts <= 0:
        return False, "heartbeat invalid"
    age = time.time() - hb_ts
    if age > stale_seconds:
        return False, f"heartbeat stale age={int(age)}s"
    return True, ""


def main():
    load_dotenv()

    costco_token = (os.getenv("LINE_COSTCO_CHANNEL_ACCESS_TOKEN") or "").strip()
    cruise_token = (os.getenv("LINE_CRUISE_CHANNEL_ACCESS_TOKEN") or "").strip()

    costco_api = LineBotApi(costco_token) if costco_token else None
    cruise_api = LineBotApi(cruise_token) if cruise_token else None

    started_at = time.time()
    state = {
        "bot_server": {"fails": 0, "down": False},
        "ngrok": {"fails": 0, "down": False},
        "costco": {"fails": 0, "down": False},
        "cruise_daemon": {"fails": 0, "down": False},
    }

    print(f"[{ts()}] [WATCHDOG] started. interval={CHECK_INTERVAL_SECONDS}s grace={STARTUP_GRACE_SECONDS}s", flush=True)

    while True:
        features = read_features()

        checks: list[tuple[str, str, callable]] = [
            ("bot_server", "all", check_bot_server),
            ("ngrok", "all", check_ngrok),
            ("costco", "costco", lambda: check_heartbeat(HEARTBEAT_COSTCO_FILE, HEARTBEAT_STALE_COSTCO_SECONDS)),
            ("cruise_daemon", "cruise", lambda: check_heartbeat(HEARTBEAT_CRUISE_FILE, HEARTBEAT_STALE_CRUISE_SECONDS)),
        ]

        for name, scope, fn in checks:
            enabled = bool(features.get(name, False))
            if not enabled:
                state[name]["fails"] = 0
                state[name]["down"] = False
                continue

            ok, err = fn()
            if ok:
                state[name]["fails"] = 0
                state[name]["down"] = False
                continue

            state[name]["fails"] += 1

            # Avoid noisy alerts during startup; also require consecutive failures.
            if (time.time() - started_at) < STARTUP_GRACE_SECONDS:
                continue
            if state[name]["fails"] < FAIL_THRESHOLD:
                continue
            if state[name]["down"]:
                continue

            state[name]["down"] = True
            msg = f"[{ts()}] 【系統監控】偵測到 {name} 可能已停止：{err}。"
            notify(scope, msg, features, costco_api, cruise_api)

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
