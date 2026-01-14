import os
import time
import requests
import re
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout


# 你從 Charles 複製的 booth 完整 URL（通常含 IDToken/accessToken 參數）
BOOTH_URL = os.getenv("MOGILY_BOOTH_URL", "").strip()

# 判斷「有票/可申請」的關鍵字：你可以依實際頁面再補
OPEN_KEYWORDS = ["受付中", "可申請", "申請", "可受理"]
CLOSED_KEYWORD = "結束受理"

# 監控頻率（建議 30~60 秒以上，避免被封）
INTERVAL_SEC = int(os.getenv("INTERVAL_SEC", "60"))

# 有票時可選擇打 webhook（例如你自己的 bot_server /notify）
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()

def notify(msg: str) -> None:
    print(msg, flush=True)
    if WEBHOOK_URL:
        try:
            requests.post(WEBHOOK_URL, json={"text": msg}, timeout=10)
        except Exception as e:
            print(f"[WARN] webhook failed: {e}", flush=True)

def page_status_text(page) -> str:
    """
    盡量抓最穩的文字來源：優先 #groups（你前端程式碼有 replaceChildren 到這裡）
    如果抓不到，就退回整頁 innerText。
    """
    # 等待資料載入：#groups 出現或頁面穩定
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PwTimeout:
        pass

    # 優先抓 groups
    try:
        loc = page.locator("#groups")
        if loc.count() > 0:
            return loc.inner_text(timeout=3000)
    except Exception:
        pass

    # 退回整頁文字
    try:
        return page.locator("body").inner_text(timeout=3000)
    except Exception:
        return ""

def is_open(text: str) -> bool:
    if not text:
        return False  # 沒抓到內容就先當關閉

    # 有出現這些字樣才算開放（你也可以再加字）
    open_keywords = ["受付中", "可申請", "申請", "可受理"]
    if any(k in text for k in open_keywords):
        return True

    # 只要看到「結束受理」就當關閉（你現在頁面就是這種狀態）
    if "結束受理" in text:
        return False

    # 其他情況保守當關閉，避免誤報
    return False

def find_open_slots(text: str) -> list[str]:
    """
    從 innerText 裡找出「HH:MM-HH:MM ... 狀態」的片段。
    回傳所有處於開放狀態的時段字串。
    """
    if not text:
        return []

    # 常見狀態字樣：你現在看到「開放申請中」
    open_markers = ["開放申請中", "受付中", "可申請", "申請"]

    # 抓時段（12:40-13:00）後面一小段文字
    # 例：12:40-13:00 ... 開放申請中
    pattern = re.compile(r"(\d{2}:\d{2}-\d{2}:\d{2}).{0,80}?(開放申請中|受付中|可申請|申請)")
    matches = pattern.findall(text)

    # matches = [(slot, status), ...]
    results = []
    for slot, status in matches:
        results.append(f"{slot}（{status}）")

    # 去重保持順序
    seen = set()
    uniq = []
    for s in results:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


def main() -> None:
    if not BOOTH_URL:
        raise SystemExit("請先設定 MOGILY_BOOTH_URL（booth 完整網址）")

    with sync_playwright() as p:
        # 用 persistent context 可保存快取/狀態（必要時也能手動操作一次）
        ctx = p.chromium.launch_persistent_context(
            user_data_dir="pw_profile_mogily",
            headless=False,  # 想看畫面可改 False
            viewport={"width": 430, "height": 932},  # iPhone 大小感
        )
        page = ctx.new_page()

        last = None

        last_open_slots: list[str] | None = None

        while True:
            try:
                page.goto(BOOTH_URL, wait_until="domcontentloaded", timeout=30000)
                text = page_status_text(page)

                # Debug（保留你原本的）
                print("[DEBUG] text head:", text[:200].replace("\n", " "), flush=True)

                open_slots = find_open_slots(text)

                # 每圈都印出目前狀態（更直覺）
                if open_slots:
                    print("[OK] OPEN:", " | ".join(open_slots), flush=True)
                else:
                    print("[OK] CLOSED (sleep {}s)".format(INTERVAL_SEC), flush=True)

                # 只在「狀態變化」時通知（避免一直洗訊息）
                if last_open_slots is None:
                    last_open_slots = open_slots

                if open_slots != last_open_slots:
                    if open_slots:
                        msg = "✅ 發現可申請整理券時段：\n" + "\n".join(open_slots) + "\n\n請立刻到頁面申請。"
                    else:
                        msg = "ℹ️ 目前已無可申請時段（全數關閉/結束受理）。"
                    notify(msg)
                    last_open_slots = open_slots

            except Exception as e:
                notify(
                    f"⚠️ 監控出錯：{e}\n"
                    "可能 token 過期了，請用手機重新進入頁面，從 Charles 複製新的 booth URL 更新。"
                )

            time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    main()
