import time
import requests
from dotenv import load_dotenv

BASE = "https://backend-prd.b2m.stardreamcruises.com"
BOT = "http://127.0.0.1:5000"
POLL_SECONDS = 30


def notify(payload: dict) -> None:
    r = requests.post(f"{BOT}/cruise/notify", json=payload, timeout=10)
    r.raise_for_status()


def get_tokens() -> dict | None:
    """
    從 bot_server 取得 tokens。
    - 有 token：回傳 dict（含 accessToken/refreshToken）
    - 沒 token：回傳 None（不要丟例外，避免 log 洗版）
    """
    try:
        r = requests.get(f"{BOT}/cruise/tokens", timeout=5)
        r.raise_for_status()
        data = r.json() or {}
    except Exception as e:
        print("[DAEMON] warn: failed to GET /cruise/tokens:", repr(e), flush=True)
        return None

    access = data.get("accessToken")
    refresh_token = data.get("refreshToken")

    if not access or not refresh_token:
        return None

    return data


def refresh(refresh_token: str) -> dict:
    r = requests.get(
        f"{BASE}/auth/customer/refresh",
        headers={"Authorization": f"Bearer {refresh_token}"},
        timeout=20,
    )
    if r.status_code in (401, 403):
        body = (r.text or "")[:200].replace("\n", " ").replace("\r", " ")
        raise PermissionError(f"refresh unauthorized {r.status_code} body={body}")
    r.raise_for_status()
    return r.json()


def cabin_allotment(access_token: str, params: dict) -> dict:
    r = requests.get(
        f"{BASE}/customers/cabin-allotment",
        params=params,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json, text/plain, */*",
            "timezone": "Asia/Taipei",
            "Origin": "https://sdr.stardreamcruises.com",
            "Referer": "https://sdr.stardreamcruises.com/",
        },
        timeout=20,
    )
    if r.status_code in (401, 403):
        raise PermissionError(f"cabin unauthorized {r.status_code}")
    r.raise_for_status()
    return r.json()


def main():
    load_dotenv()

    params = {
        "itinerary_name": "探索星號 - 2晚 - 那霸 海上遊",
        "departure_date": "2026-02-25",
        "departure_port": "12",
        "pax": "4",
        "lang": "hant",
        "currentStep": "0",
        "page": "1",
    }

    print("[DAEMON] started. polling every", POLL_SECONDS, "seconds", flush=True)
    print("[DAEMON] params =", params, flush=True)

    last_had = None
    last_alert_at = 0.0

    while True:
        try:
            tokens = get_tokens()
            if not tokens:
                print("[DAEMON] waiting for tokens... (please login once)", flush=True)
                time.sleep(30)
                continue
            access = tokens["accessToken"]
            refresh_token = tokens["refreshToken"]

            try:
                data = cabin_allotment(access, params)
            except PermissionError:
                ref = refresh(refresh_token)

                new_access = ref.get("accessToken")
                new_refresh = ref.get("refreshToken")

                if new_access or new_refresh:
                    payload = {
                        "accessToken": new_access or access,
                        "refreshToken": new_refresh or refresh_token,
                        "user": tokens.get("user"),
                        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    }
                    # 回寫 bot_server（你已經有 POST /cruise/tokens）
                    try:
                        requests.post(f"{BOT}/cruise/tokens", json=payload, timeout=5)
                    except Exception as ex:
                        print("[DAEMON] warn: failed to POST /cruise/tokens:", repr(ex), flush=True)

                    access = payload["accessToken"]
                    refresh_token = payload["refreshToken"]

                data = cabin_allotment(access, params)

            total = data.get("meta", {}).get("totalItems", 0)
            now_had = total > 0
            print(f"[DAEMON] cabin totalItems={total} had={now_had}", flush=True)

            if last_had is False and now_had is True:
                cabins = [
                    (x.get("traditional_chinese_cabin_name") or x.get("cabin_name"))
                    for x in data.get("items", [])
                ]
                notify({
                    "type": "CRUISE_CABIN_AVAILABLE",
                    "at": time.time(),
                    "totalItems": total,
                    "cabins": cabins,
                    "params": params,
                })

            last_had = now_had
            time.sleep(POLL_SECONDS)

        except Exception as e:
            print("[DAEMON] error:", repr(e), flush=True)
            now = time.time()

            # refresh/cabin 被 401/403 -> 通知你要手動登入一次
            if isinstance(e, PermissionError):
                if now - last_alert_at > 300:  # 5 分鐘內不要狂洗
                    try:
                        notify({
                            "type": "CRUISE_NEED_RELOGIN",
                            "at": now,
                            "message": "Cruise token expired/unauthorized. Please open SDC login page and login once to resync tokens.",
                            "error": str(e),
                        })
                    except Exception as ex:
                        print("[DAEMON] notify failed:", repr(ex), flush=True)
                    last_alert_at = now

                # ✅ 這裡：清空 bot_server tokens，避免一直拿壞 token 重試
                try:
                    requests.post(f"{BOT}/cruise/tokens/clear", timeout=5)
                    print("[DAEMON] tokens cleared on bot_server", flush=True)
                except Exception as ex:
                    print("[DAEMON] warn: failed to clear tokens:", repr(ex), flush=True)

                time.sleep(60)  # 建議 60 秒，減少洗版
                continue


            # 其他錯誤照舊（10 分鐘一次）
            if now - last_alert_at > 600:
                try:
                    notify({
                        "type": "CRUISE_DAEMON_ERROR",
                        "at": now,
                        "error": str(e),
                    })
                except Exception as ex:
                    print("[DAEMON] notify failed:", repr(ex), flush=True)
                last_alert_at = now

            time.sleep(10)


if __name__ == "__main__":
    main()
