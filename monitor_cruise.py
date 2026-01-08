from urllib.parse import urlencode
from playwright.sync_api import sync_playwright

BASE = "https://backend-prd.b2m.stardreamcruises.com"

COMMON_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Origin": "https://sdr.stardreamcruises.com",
    "Referer": "https://sdr.stardreamcruises.com/",
}

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto("https://sdr.stardreamcruises.com/", wait_until="domcontentloaded")

        input("請在這個新視窗完成登入/進到可查房頁後，回來按 Enter…")

        refresh = context.request.get(f"{BASE}/auth/customer/refresh", headers=COMMON_HEADERS, timeout=20000)
        print("refresh status:", refresh.status)
        print("refresh body (first 200):", refresh.text()[:200])

        params = {
            "itinerary_name": "探索星號 - 2晚 - 那霸 海上遊",
            "departure_date": "2026-02-25",
            "departure_port": "12",
            "pax": "4",
            "lang": "hant",
            "currentStep": "0",
            "page": "1",
        }
        url = f"{BASE}/customers/cabin-allotment?{urlencode(params)}"
        cabin = context.request.get(url, headers={**COMMON_HEADERS, "timezone": "Asia/Taipei"}, timeout=20000)
        print("cabin status:", cabin.status)
        print("cabin body (first 200):", cabin.text()[:200])

        input("按 Enter 關閉…")
        browser.close()

if __name__ == "__main__":
    main()
