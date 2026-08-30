import base64
import hashlib
import json
import os
import re
import time
from datetime import datetime
import requests
from dotenv import load_dotenv
from filelock import FileLock

BASE = "https://backend-prd.b2m.stardreamcruises.com"
BOT = "http://127.0.0.1:5000"
POLL_SECONDS = 60
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(BASE_DIR, "state")
os.makedirs(STATE_DIR, exist_ok=True)
HEARTBEAT_FILE = os.path.join(STATE_DIR, "heartbeat_cruise_daemon.json")
TOKEN_EVENTS_FILE = os.path.join(STATE_DIR, "token_events.jsonl")
HEARTBEAT_INTERVAL_SECONDS = 10
_last_heartbeat_at = 0.0
_last_cycle_started_at = 0.0
_last_cycle_gap_seconds = 0.0
MONITORS_FILE = os.path.join(BASE_DIR, "monitors_cruise.json")
TIER_RULES_FILE = os.path.join(BASE_DIR, "cabin_name")
FEATURES_FILE = os.path.join(BASE_DIR, "features.json")
NO_ROOM_COOLDOWN_SECONDS = 0
FEATURE_CHECK_SECONDS = 10
CRUISE_AVAILABILITY_NOTIFY_TYPES = {
    "CRUISE_CABIN_AVAILABLE",
    "CRUISE_TIER_AVAILABLE",
    "CRUISE_STANDBY_AVAILABLE",
}
STANDBY_CABIN_KEYWORDS = (
    "候補客房",
    "候补客房",
    "standby stateroom",
    "standby",
)


def ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _decode_jwt_claims(token: str | None) -> dict:
    if not isinstance(token, str) or token.count(".") != 2:
        return {}
    try:
        encoded = token.split(".")[1]
        encoded += "=" * ((4 - len(encoded) % 4) % 4)
        claims = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")))
        return claims if isinstance(claims, dict) else {}
    except Exception:
        return {}


def _token_metadata(token: str | None) -> dict:
    if not isinstance(token, str) or not token:
        return {"present": False}
    claims = _decode_jwt_claims(token)
    now = int(time.time())
    iat = claims.get("iat")
    exp = claims.get("exp")
    metadata = {
        "present": True,
        "fingerprint": hashlib.sha256(token.encode("utf-8")).hexdigest()[:12],
        "iat": iat if isinstance(iat, (int, float)) else None,
        "exp": exp if isinstance(exp, (int, float)) else None,
        "sub": str(claims.get("sub")) if claims.get("sub") is not None else None,
        "session": str(claims.get("session")) if claims.get("session") is not None else None,
        "dpiHiFai": str(claims.get("dpiHiFai")) if claims.get("dpiHiFai") is not None else None,
    }
    if isinstance(iat, (int, float)) and isinstance(exp, (int, float)):
        metadata["ttl_seconds"] = int(exp - iat)
    if isinstance(exp, (int, float)):
        metadata["expired"] = exp <= now
        metadata["seconds_until_expiry"] = int(exp - now)
    return metadata


def _token_transition_metadata(previous_access: dict, previous_refresh: dict, received_access: dict, received_refresh: dict) -> dict:
    previous_session = previous_access.get("session") or previous_refresh.get("session")
    received_session = received_access.get("session") or received_refresh.get("session")
    previous_sub = previous_access.get("sub") or previous_refresh.get("sub")
    received_sub = received_access.get("sub") or received_refresh.get("sub")
    return {
        "previous_sub": previous_sub,
        "received_sub": received_sub,
        "same_sub": previous_sub == received_sub if previous_sub and received_sub else None,
        "previous_session": previous_session,
        "received_session": received_session,
        "same_session": previous_session == received_session if previous_session and received_session else None,
        "access_fingerprint_changed": previous_access.get("fingerprint") != received_access.get("fingerprint")
        if previous_access.get("present") and received_access.get("present")
        else None,
        "refresh_fingerprint_changed": previous_refresh.get("fingerprint") != received_refresh.get("fingerprint")
        if previous_refresh.get("present") and received_refresh.get("present")
        else None,
    }


def _api_error_summary(text: str) -> dict:
    body = _body_head(text, 500)
    summary = {"body": body}
    try:
        data = json.loads(text or "{}")
    except Exception:
        return summary
    if isinstance(data, dict):
        summary["message"] = data.get("message")
        summary["code"] = data.get("code")
        summary["statusCode"] = data.get("statusCode")
        summary["httpStatus"] = data.get("httpStatus")
    return summary


def _classify_auth_failure(status: int, token: str | None, token_kind: str) -> str:
    metadata = _token_metadata(token)
    if metadata.get("expired"):
        if token_kind == "refresh" and _last_cycle_gap_seconds >= 1800:
            return "refresh_expired_after_long_cycle_gap"
        return f"{token_kind}_expired"
    if status == 401:
        return f"{token_kind}_revoked_or_replaced_before_expiry"
    if status == 403:
        return f"{token_kind}_forbidden_or_security_policy"
    return f"{token_kind}_authorization_failed"


def _redact_token_event_value(value):
    if isinstance(value, str):
        return re.sub(
            r"eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
            "[REDACTED_JWT]",
            value,
        )
    if isinstance(value, dict):
        return {key: _redact_token_event_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_token_event_value(item) for item in value]
    return value


def _append_token_event(event: str, **fields) -> None:
    payload = {
        "ts": time.time(),
        "ts_str": ts(),
        "process": "monitor_cruise_daemon",
        "event": event,
        "cycle_gap_seconds": round(_last_cycle_gap_seconds, 3),
    }
    payload.update(fields)
    payload = _redact_token_event_value(payload)
    try:
        lock = FileLock(TOKEN_EVENTS_FILE + ".lock")
        with lock:
            with open(TOKEN_EVENTS_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as ex:
        print(f"[{ts()}] [DAEMON] token event write failed: {type(ex).__name__}: {ex}", flush=True)


class NotifyDeliveryFailed(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _notify_response_detail(response: requests.Response) -> str:
    parts = [f"status={response.status_code}"]
    try:
        data = response.json() or {}
    except Exception:
        data = {}

    if isinstance(data, dict):
        if "sent" in data:
            parts.append(f"sent={data.get('sent')}")
        error = data.get("error")
        if error:
            parts.append(f"error={error}")
        errors = data.get("errors")
        if isinstance(errors, list) and errors:
            sample = errors[0]
            if isinstance(sample, dict):
                sample_text = ", ".join(
                    f"{k}={sample.get(k)}" for k in ("user", "error") if sample.get(k)
                )
            else:
                sample_text = str(sample)
            if sample_text:
                parts.append(f"sample_error={sample_text}")

    body = _body_head(getattr(response, "text", "") or "")
    if body:
        parts.append(f"body_head={body}")
    return " ".join(parts)


def notify(payload: dict) -> None:
    try:
        r = requests.post(f"{BOT}/cruise/notify", json=payload, timeout=10)
    except requests.RequestException as ex:
        raise NotifyDeliveryFailed(
            f"/cruise/notify request failed: {type(ex).__name__}: {str(ex)[:200]}"
        ) from ex

    if not r.ok:
        raise NotifyDeliveryFailed(
            f"/cruise/notify failed {_notify_response_detail(r)}",
            status_code=r.status_code,
        )

    try:
        data = r.json() or {}
    except Exception:
        data = {}

    if isinstance(data, dict):
        if data.get("ok") is False:
            raise NotifyDeliveryFailed(
                f"/cruise/notify returned ok=false {_notify_response_detail(r)}",
                status_code=r.status_code,
            )
        sent = data.get("sent")
        if (
            payload.get("type") in CRUISE_AVAILABILITY_NOTIFY_TYPES
            and isinstance(sent, int)
            and sent <= 0
        ):
            raise NotifyDeliveryFailed(
                f"/cruise/notify sent=0 {_notify_response_detail(r)}",
                status_code=r.status_code,
            )


def get_tokens() -> tuple[dict | None, str, str]:
    try:
        r = requests.get(f"{BOT}/cruise/tokens", timeout=5)
        r.raise_for_status()
        data = r.json() or {}
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        print(f"[{ts()}] [DAEMON] warn: failed to GET /cruise/tokens:", repr(e), flush=True)
        return None, "fetch_failed", err

    access = data.get("accessToken")
    refresh_token = data.get("refreshToken")

    if not access or not refresh_token:
        return None, "missing_tokens", ""

    return data, "ok", ""


def check_bot_server_health() -> tuple[bool, str]:
    try:
        r = requests.get(f"{BOT}/health", timeout=3)
        if r.status_code != 200:
            return False, f"status={r.status_code} body_head={_body_head(r.text)}"
        return True, ""
    except requests.Timeout as e:
        return False, f"timeout {type(e).__name__}: {e}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:200]}"


def refresh(refresh_token: str) -> dict:
    try:
        r = requests.get(
            f"{BASE}/auth/customer/refresh",
            headers={"Authorization": f"Bearer {refresh_token}"},
            timeout=20,
        )
    except requests.RequestException as ex:
        _append_token_event(
            "refresh_request_failed",
            error_type=type(ex).__name__,
            detail=str(ex)[:500],
            refresh=_token_metadata(refresh_token),
        )
        raise
    if r.status_code in (401, 403):
        body = _body_head(r.text or "")
        refresh_meta = _token_metadata(refresh_token)
        _append_token_event(
            "refresh_unauthorized",
            status=r.status_code,
            classification=_classify_auth_failure(r.status_code, refresh_token, "refresh"),
            detail=body,
            api_error=_api_error_summary(r.text or ""),
            token_session=refresh_meta.get("session"),
            token_sub=refresh_meta.get("sub"),
            refresh=refresh_meta,
        )
        raise PermissionError(f"refresh unauthorized {r.status_code} body={body}")
    if r.status_code >= 400:
        _append_token_event(
            "refresh_http_failed",
            status=r.status_code,
            detail=(r.text or "")[:500].replace("\n", " ").replace("\r", " "),
            refresh=_token_metadata(refresh_token),
        )
    r.raise_for_status()
    return r.json()


class RefreshTempFailed(Exception):
    """Refresh failed (non-401/403) after retry; treat as transient and do not clear tokens."""


def _body_head(text: str, limit: int = 200) -> str:
    return (text or "")[:limit].replace("\n", " ").replace("\r", " ")


def _refresh_error_text(ex: Exception) -> str:
    if isinstance(ex, PermissionError):
        return str(ex) or "refresh unauthorized"
    if isinstance(ex, requests.HTTPError):
        sc = getattr(getattr(ex, "response", None), "status_code", None)
        body = _body_head(getattr(getattr(ex, "response", None), "text", "") or "")
        return f"refresh http error status={sc} body={body}"
    if isinstance(ex, requests.RequestException):
        return f"refresh request error {type(ex).__name__}: {str(ex)[:200]}"
    return f"refresh error {type(ex).__name__}: {str(ex)[:200]}"


def cabin_allotment(access_token: str, params: dict) -> tuple[int, dict]:
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
        body = _body_head(r.text or "", 500)
        access_meta = _token_metadata(access_token)
        _append_token_event(
            "cabin_unauthorized",
            status=r.status_code,
            classification=_classify_auth_failure(r.status_code, access_token, "access"),
            detail=body,
            api_error=_api_error_summary(r.text or ""),
            token_session=access_meta.get("session"),
            token_sub=access_meta.get("sub"),
            request_params=params,
            access=access_meta,
        )
        raise PermissionError(f"cabin unauthorized {r.status_code} body={body[:200]}")
    r.raise_for_status()
    return r.status_code, r.json()


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
        print(f"[{ts()}] [DAEMON] warn: failed to read {path}:", repr(e), flush=True)
        return default


def write_json(path: str, data):
    try:
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception as e:
        print(f"[{ts()}] [DAEMON] warn: failed to write {path}:", repr(e), flush=True)


def write_heartbeat(status: str, extra: dict | None = None) -> None:
    """Write a periodic heartbeat so the watchdog can detect liveness reliably."""
    global _last_heartbeat_at
    now = time.time()
    if (now - _last_heartbeat_at) < HEARTBEAT_INTERVAL_SECONDS:
        return
    _last_heartbeat_at = now

    payload: dict = {"ts": now, "ts_str": ts(), "status": status}
    if isinstance(extra, dict):
        payload.update(extra)

    try:
        write_json(HEARTBEAT_FILE, payload)
    except Exception as e:
        print(f"[{ts()}] [DAEMON] warn: failed to write heartbeat:", repr(e), flush=True)


def read_features():
    defaults = {"cruise_daemon": True}
    try:
        if not os.path.exists(FEATURES_FILE):
            return defaults
        lock = FileLock(FEATURES_FILE + ".lock")
        with lock:
            data = read_json(FEATURES_FILE, defaults)
    except Exception as e:
        print(f"[{ts()}] [DAEMON] warn: failed to read {FEATURES_FILE}:", repr(e), flush=True)
        return defaults
    if not isinstance(data, dict):
        return defaults
    merged = defaults.copy()
    for key, value in data.items():
        if key in merged:
            merged[key] = bool(value)
    return merged

def feature_enabled(name: str) -> bool:
    return bool(read_features().get(name, True))

def sleep_with_feature_checks(total_seconds: int):
    end_at = time.time() + total_seconds
    while True:
        if not feature_enabled("cruise_daemon"):
            write_heartbeat("paused")
            return
        remaining = end_at - time.time()
        if remaining <= 0:
            return
        time.sleep(min(FEATURE_CHECK_SECONDS, remaining))

def read_monitors():
    lock = FileLock(MONITORS_FILE + ".lock")
    with lock:
        return read_json(MONITORS_FILE, [])


def prune_past_monitors() -> int:
    today = datetime.now().date()
    lock = FileLock(MONITORS_FILE + ".lock")
    with lock:
        monitors = read_json(MONITORS_FILE, [])
        if not isinstance(monitors, list):
            return 0

        kept = []
        removed = []
        for monitor in monitors:
            if not isinstance(monitor, dict):
                kept.append(monitor)
                continue
            date_text = monitor.get("departure_date") or monitor.get("date")
            if not isinstance(date_text, str):
                kept.append(monitor)
                continue
            try:
                monitor_date = datetime.strptime(date_text.strip(), "%Y-%m-%d").date()
            except Exception:
                kept.append(monitor)
                continue
            if monitor_date < today:
                removed.append(monitor)
                continue
            kept.append(monitor)

        if len(kept) != len(monitors):
            write_json(MONITORS_FILE, kept)

    if removed:
        keys = [str(make_monitor_key(m)) for m in removed[:10]]
        print(
            f"[{ts()}] [DAEMON] pruned past monitors count={len(removed)} keys={keys}",
            flush=True,
        )
    return len(removed)


def update_monitor_fields(monitor_key: tuple, updates: dict):
    lock = FileLock(MONITORS_FILE + ".lock")
    with lock:
        monitors = read_json(MONITORS_FILE, [])
        for m in monitors:
            if make_monitor_key(m) == monitor_key:
                m.update(updates)
                break
        write_json(MONITORS_FILE, monitors)


def load_tier_rules() -> list:
    try:
        with open(TIER_RULES_FILE, "r", encoding="utf-8") as f:
            content = f.read()
        scope: dict = {}
        exec(content, {}, scope)
        rules = scope.get("TIER_RULES") or []
        normalized = []
        for tier, keywords in rules:
            try:
                normalized.append((int(tier), list(keywords)))
            except Exception:
                continue
        return normalized
    except Exception as e:
        print(f"[{ts()}] [DAEMON] warn: failed to load tier rules:", repr(e), flush=True)
        return []


def make_monitor_key(m: dict) -> tuple:
    return (
        m.get("itinerary_name"),
        m.get("departure_date") or m.get("date"),
        str(m.get("departure_port")),
        m.get("lang"),
    )


def find_tier_for_name(cabin_name: str, tier_rules: list) -> int | None:
    if not cabin_name:
        return None
    name = cabin_name.lower()
    for tier, keywords in tier_rules:
        for kw in keywords:
            if kw and str(kw).lower() in name:
                return tier
    return None


def iter_cabin_name_candidates(item: dict) -> list[str]:
    if not isinstance(item, dict):
        return []
    candidates = []
    seen = set()
    for key in (
        "traditional_chinese_cabin_name",
        "simplified_chinese_cabin_name",
        "cabin_name",
        "cabin_category_name",
    ):
        value = item.get(key)
        if not isinstance(value, str):
            continue
        text = value.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        candidates.append(text)
    return candidates


def pick_display_cabin_name(item: dict) -> str:
    for name in iter_cabin_name_candidates(item):
        return name
    return ""


def is_standby_cabin_item(item: dict) -> bool:
    for name in iter_cabin_name_candidates(item):
        lowered = name.lower()
        if any(keyword.lower() in lowered for keyword in STANDBY_CABIN_KEYWORDS):
            return True
    return False


def to_int_or_none(value):
    try:
        return int(value)
    except Exception:
        return None


def build_params(monitor: dict, pax: int) -> dict | None:
    itinerary_name = monitor.get("itinerary_name")
    departure_date = monitor.get("departure_date") or monitor.get("date")
    departure_port = monitor.get("departure_port")
    lang = monitor.get("lang")
    if not itinerary_name or not departure_date or departure_port is None or not lang:
        return None
    return {
        "itinerary_name": itinerary_name,
        "departure_date": departure_date,
        "departure_port": str(departure_port),
        "pax": str(pax),
        "lang": lang,
    }


def build_tier_text(tier: int) -> tuple[str, str]:
    tier_text_map = {
        3: ("露台", "露台客房"),
        2: ("海景", "海景客房"),
        1: ("內側", "內側客房"),
    }
    short_text, full_text = tier_text_map.get(tier, (str(tier), f"{tier}客房"))
    return short_text, full_text


def fetch_cabins(access: str, refresh_token: str, params: dict, user: str | None):
    try:
        status_code, data = cabin_allotment(access, params)
        return status_code, data, access, refresh_token
    except PermissionError:
        ref = None
        last_err: Exception | None = None

        for attempt in (1, 2):
            try:
                ref = refresh(refresh_token)
                last_err = None
                break
            except Exception as ex:
                last_err = ex

            if attempt == 1:
                # After a first refresh failure, wait a bit then retry once.
                sleep_with_feature_checks(30)
                # Token may have been synced/updated while we were waiting.
                latest_tokens, latest_state, _ = get_tokens()
                if latest_state == "ok" and latest_tokens:
                    access = latest_tokens.get("accessToken") or access
                    refresh_token = latest_tokens.get("refreshToken") or refresh_token
                    user = latest_tokens.get("user") or user

        if ref is None:
            if isinstance(last_err, PermissionError):
                raise last_err
            raise RefreshTempFailed(_refresh_error_text(last_err or Exception("unknown refresh error")))

        new_access = ref.get("accessToken")
        new_refresh = ref.get("refreshToken")

        if new_access or new_refresh:
            previous_access_meta = _token_metadata(access)
            previous_refresh_meta = _token_metadata(refresh_token)
            received_access_meta = _token_metadata(new_access or access)
            received_refresh_meta = _token_metadata(new_refresh or refresh_token)
            _append_token_event(
                "refresh_succeeded",
                source="daemon_refresh",
                transition=_token_transition_metadata(
                    previous_access_meta,
                    previous_refresh_meta,
                    received_access_meta,
                    received_refresh_meta,
                ),
                previous_access=previous_access_meta,
                previous_refresh=previous_refresh_meta,
                received_access=received_access_meta,
                received_refresh=received_refresh_meta,
            )
            payload = {
                "accessToken": new_access or access,
                "refreshToken": new_refresh or refresh_token,
                "user": user,
                "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "source": "daemon_refresh",
            }
            try:
                sync_response = requests.post(f"{BOT}/cruise/tokens", json=payload, timeout=5)
                if not sync_response.ok:
                    _append_token_event(
                        "token_update_post_rejected",
                        status=sync_response.status_code,
                        detail=(sync_response.text or "")[:500],
                        access=_token_metadata(payload["accessToken"]),
                        refresh=_token_metadata(payload["refreshToken"]),
                    )
                    if sync_response.status_code == 409:
                        latest_tokens, latest_state, _ = get_tokens()
                        if latest_state == "ok" and latest_tokens:
                            payload["accessToken"] = latest_tokens.get("accessToken") or payload["accessToken"]
                            payload["refreshToken"] = latest_tokens.get("refreshToken") or payload["refreshToken"]
                            payload["user"] = latest_tokens.get("user") or payload["user"]
            except Exception as ex:
                print(f"[{ts()}] [DAEMON] warn: failed to POST /cruise/tokens:", repr(ex), flush=True)
                _append_token_event(
                    "token_update_post_failed",
                    error_type=type(ex).__name__,
                    detail=str(ex)[:500],
                    access=_token_metadata(payload["accessToken"]),
                    refresh=_token_metadata(payload["refreshToken"]),
                )

            access = payload["accessToken"]
            refresh_token = payload["refreshToken"]

        status_code, data = cabin_allotment(access, params)
        return status_code, data, access, refresh_token


def main():
    global _last_cycle_started_at, _last_cycle_gap_seconds
    load_dotenv()

    print(f"[{ts()}] [DAEMON] started. polling every {POLL_SECONDS} seconds", flush=True)
    paused = False

    relogin_notified = False
    error_notified = False
    refresh_temp_failed = False
    tier_rules = load_tier_rules()

    while True:
        cycle_started_at = time.time()
        if _last_cycle_started_at > 0:
            _last_cycle_gap_seconds = cycle_started_at - _last_cycle_started_at
        _last_cycle_started_at = cycle_started_at
        if not feature_enabled("cruise_daemon"):
            if not paused:
                print(f"[{ts()}] ⏸️ Cruise 監控已停用，暫停檢查中", flush=True)
                paused = True
            time.sleep(FEATURE_CHECK_SECONDS)
            continue
        if paused:
            print(f"[{ts()}] ✅ Cruise 監控已啟用，恢復檢查", flush=True)
            paused = False

        prune_past_monitors()
        write_heartbeat("running")
        try:
            tokens, token_state, token_err = get_tokens()
            if token_state != "ok":
                refresh_temp_failed = False
                if token_state == "missing_tokens":
                    print(f"[{ts()}] [DAEMON] waiting for tokens... (please login once)", flush=True)
                else:
                    health_ok, health_err = check_bot_server_health()
                    if health_ok:
                        print(
                            f"[{ts()}] [DAEMON] bot_server health OK; /cruise/tokens unavailable ({token_err})",
                            flush=True,
                        )
                    else:
                        print(
                            f"[{ts()}] [DAEMON] bot_server health timeout/unavailable ({health_err})",
                            flush=True,
                        )
                sleep_with_feature_checks(30)
                continue
            access = tokens["accessToken"]
            refresh_token = tokens["refreshToken"]
            user = tokens.get("user")

            monitors = read_monitors()
            any_http_200_total = False
            for monitor in monitors:
                write_heartbeat("running", {"phase": "monitor_loop"})
                if not monitor.get("enabled", False):
                    continue

                monitor_key = make_monitor_key(monitor)
                update_fields = None
                notified_tiers = {
                    to_int_or_none(x) for x in (monitor.get("notified_tiers") or [])
                }
                notified_tiers.discard(None)
                notified_standby = bool(monitor.get("notified_standby"))
                try:
                    now = time.time()
                    max_pax = to_int_or_none(monitor.get("max_pax"))
                    no_room_until = float(monitor.get("no_room_until_epoch") or 0)

                    if now < no_room_until:
                        print(f"[{ts()}] [DAEMON] skip (no_room cooldown) key={monitor_key}", flush=True)
                        continue

                    probe_paxes = [max_pax] if max_pax is not None else [2, 4]
                    last_http = None
                    last_items = []
                    observed_max_pax = 0
                    any_items = False
                    any_http_200 = False

                    for pax in probe_paxes:
                        write_heartbeat("running", {"phase": "pax_loop"})
                        params = build_params(monitor, pax)
                        if not params:
                            print(f"[{ts()}] [DAEMON] warn: missing required params key={monitor_key}", flush=True)
                            break

                        status_code, data, access, refresh_token = fetch_cabins(
                            access, refresh_token, params, user
                        )
                        items = data.get("items", []) or []
                        print(
                            f"[{ts()}] [DAEMON] result key={monitor_key} pax={pax} "
                            f"status={status_code} items={len(items)}",
                            flush=True,
                        )
                        last_http = status_code
                        if status_code == 200:
                            any_http_200 = True
                            any_http_200_total = True
                        if items:
                            last_items = items
                            any_items = True
                        elif not any_items:
                            last_items = items

                        if max_pax is None and status_code == 200 and items:
                            observed_max_pax = max(
                                observed_max_pax,
                                max(int(x.get("cabin_pax") or 0) for x in items),
                            )

                    if last_http is None:
                        continue

                    effective_http = 200 if any_http_200 else last_http
                    last_seen_cabins = []
                    for item in last_items:
                        display_name = pick_display_cabin_name(item)
                        if display_name:
                            last_seen_cabins.append(display_name)
                    has_standby = any(is_standby_cabin_item(x) for x in last_items if isinstance(x, dict))
                    update_fields = {
                        "last_check_at": ts(),
                        "last_http": effective_http,
                        "last_seen_cabins": last_seen_cabins,
                        "last_seen_has_standby": has_standby,
                    }

                    if effective_http != 200:
                        update_monitor_fields(monitor_key, update_fields)
                        continue

                    tiers = []
                    cabin_tiers = {}
                    for name in update_fields["last_seen_cabins"]:
                        tier = find_tier_for_name(name, tier_rules)
                        if tier is not None:
                            tiers.append(tier)
                            cabin_tiers.setdefault(tier, []).append(name)

                    update_fields["last_seen_tiers"] = sorted(set(tiers))

                    if max_pax is None and observed_max_pax > 0:
                        update_fields["max_pax"] = observed_max_pax

                    if not any_items:
                        update_fields["no_room_until_epoch"] = now + NO_ROOM_COOLDOWN_SECONDS
                    else:
                        update_fields["no_room_until_epoch"] = 0

                    present_tiers = set(update_fields["last_seen_tiers"])
                    # Only suppress alerts for tiers that are still present.
                    # If a tier disappears and later comes back, alert again.
                    notified_tiers &= present_tiers
                    new_tiers = present_tiers - notified_tiers
                    notify_mode = monitor.get("notify_mode", "per_tier_first_seen")
                    baseline_tier = to_int_or_none(monitor.get("baseline_tier"))

                    if notify_mode == "above_baseline_first_seen" and baseline_tier is not None:
                        notify_tiers = {t for t in new_tiers if t > baseline_tier}
                    else:
                        notify_tiers = new_tiers

                    for tier in sorted(notify_tiers, reverse=True):
                        tier_short, tier_full = build_tier_text(tier)
                        max_pax_value = to_int_or_none(update_fields.get("max_pax", monitor.get("max_pax")))
                        payload = {
                            "type": "CRUISE_TIER_AVAILABLE",
                            "at": time.time(),
                            "tier": int(tier),
                            "tier_short": tier_short,
                            "tier_full": tier_full,
                            "date": monitor.get("departure_date") or monitor.get("date") or "",
                            "port_name": monitor.get("port_name") or "",
                            "itinerary_name": monitor.get("itinerary_name") or "",
                            "max_pax": max_pax_value,
                        }
                        notify(payload)
                        notified_tiers.add(tier)

                    update_fields["notified_tiers"] = sorted(notified_tiers)
                    if not has_standby:
                        notified_standby = False
                    if has_standby and not notified_standby:
                        max_pax_value = to_int_or_none(update_fields.get("max_pax", monitor.get("max_pax")))
                        payload = {
                            "type": "CRUISE_STANDBY_AVAILABLE",
                            "at": time.time(),
                            "date": monitor.get("departure_date") or monitor.get("date") or "",
                            "port_name": monitor.get("port_name") or "",
                            "itinerary_name": monitor.get("itinerary_name") or "",
                            "max_pax": max_pax_value,
                            "cabin_name": "候補客房",
                        }
                        notify(payload)
                        notified_standby = True
                    update_fields["notified_standby"] = notified_standby
                    update_monitor_fields(monitor_key, update_fields)
                except PermissionError:
                    raise
                except RefreshTempFailed:
                    raise
                except NotifyDeliveryFailed as ex:
                    if isinstance(update_fields, dict):
                        update_fields["notified_tiers"] = sorted(notified_tiers)
                        update_fields["notified_standby"] = notified_standby
                        update_monitor_fields(monitor_key, update_fields)
                    print(f"[{ts()}] [DAEMON] notify delivery failed key={monitor_key}:", repr(ex), flush=True)
                    continue
                except requests.HTTPError as ex:
                    code = getattr(ex.response, "status_code", None)
                    update_monitor_fields(monitor_key, {
                        "last_check_at": ts(),
                        "last_http": code or "HTTPError",
                    })
                    print(f"[{ts()}] [DAEMON] monitor http error key={monitor_key}:", repr(ex), flush=True)
                    continue
                except Exception as ex:
                    update_monitor_fields(monitor_key, {
                        "last_check_at": ts(),
                        "last_http": "ERR",
                    })
                    print(f"[{ts()}] [DAEMON] monitor error key={monitor_key}:", repr(ex), flush=True)
                    continue

            if refresh_temp_failed and any_http_200_total:
                try:
                    notify({
                        "type": "CRUISE_TOKEN_RECOVERED",
                        "at": time.time(),
                        "message": "Cruise Token 已恢復，監控已恢復",
                    })
                except Exception as ex:
                    print(f"[{ts()}] [DAEMON] notify failed:", repr(ex), flush=True)
                refresh_temp_failed = False
            relogin_notified = False
            error_notified = False
            sleep_with_feature_checks(POLL_SECONDS)

        except Exception as e:
            print(f"[{ts()}] [DAEMON] error:", repr(e), flush=True)
            now = time.time()

            if isinstance(e, RefreshTempFailed):
                if not refresh_temp_failed:
                    try:
                        notify({
                            "type": "CRUISE_REFRESH_TEMP_FAILED",
                            "at": now,
                            "message": "刷新 Token 暫時失敗，將持續重試",
                            "error": str(e),
                        })
                    except Exception as ex:
                        print(f"[{ts()}] [DAEMON] notify failed:", repr(ex), flush=True)
                    refresh_temp_failed = True
                sleep_with_feature_checks(60)
                continue

            if isinstance(e, PermissionError):
                err_text = str(e) or ""
                if "refresh unauthorized" in err_text.lower():
                    refresh_temp_failed = False
                    if not relogin_notified:
                        try:
                            notify({
                                "type": "CRUISE_NEED_RELOGIN",
                                "at": now,
                                "message": "Cruise 需要重新登入一次（refresh 失效/未授權）。請開啟 SDC 登入頁手動登入，Token Sync 會自動回灌。",
                                "error": err_text,
                            })
                        except Exception as ex:
                            print(f"[{ts()}] [DAEMON] notify failed:", repr(ex), flush=True)
                        relogin_notified = True
                    try:
                        requests.post(
                            f"{BOT}/cruise/tokens/clear",
                            json={
                                "source": "monitor_cruise_daemon",
                                "reason": "refresh_unauthorized",
                                "detail": err_text[:500],
                            },
                            timeout=5,
                        )
                        print(f"[{ts()}] [DAEMON] tokens cleared on bot_server", flush=True)
                    except Exception as ex:
                        print(f"[{ts()}] [DAEMON] warn: failed to clear tokens:", repr(ex), flush=True)
                else:
                    if not error_notified:
                        try:
                            notify({
                                "type": "CRUISE_DAEMON_ERROR",
                                "at": now,
                                "message": "Cruise 監控遇到未授權或後端異常，將持續重試。",
                                "error": err_text,
                            })
                        except Exception as ex:
                            print(f"[{ts()}] [DAEMON] notify failed:", repr(ex), flush=True)
                        error_notified = True

                sleep_with_feature_checks(60)
                continue

            if not error_notified:
                try:
                    notify({
                        "type": "CRUISE_DAEMON_ERROR",
                        "at": now,
                        "message": "Cruise 監控發生錯誤，將持續重試。",
                        "error": str(e),
                    })
                except Exception as ex:
                    print(f"[{ts()}] [DAEMON] notify failed:", repr(ex), flush=True)
                error_notified = True

            sleep_with_feature_checks(10)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"[{ts()}] [DAEMON] 已停止", flush=True)
