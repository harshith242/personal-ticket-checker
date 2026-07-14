#!/usr/bin/env python3
"""
Odyssey IMAX watcher — SINGLE-RUN version for GitHub Actions.

Checks the District venue page ONCE. If 'The Odyssey' is listed in IMAX for the
target date, it sends a Telegram alert with a screenshot and exits. No loop, no
heartbeat, no stop-command — GitHub's scheduler handles the "run again later".

Config comes from environment variables (set as GitHub repository Secrets):
  BOT_TOKEN        your bot token
  CHAT_ID          your chat id
  EXTRA_CHAT_IDS   optional, comma-separated extra recipients
"""

import os
import re
from datetime import datetime

import requests
from playwright.sync_api import sync_playwright

# ------------------------------ Config ------------------------------
TARGET_DATE    = "2026-07-25"
VENUE_SLUG     = "pvr-palazzo-the-nexus-vijaya-mall-chennai-in-chennai-CD1022274"
TARGET_URL     = f"https://www.district.in/movies/{VENUE_SLUG}?fromdate={TARGET_DATE}"
MOVIE_KEYWORD  = "odyssey"   # matched case-insensitively against the page text
FORMAT_KEYWORD = "imax"      # set to "" to alert on ANY Odyssey show, not just IMAX

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36")

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_IDS = [c.strip() for c in
            [os.getenv("CHAT_ID", ""), *os.getenv("EXTRA_CHAT_IDS", "").split(",")]
            if c and c.strip()]

SHOT_PATH = "hit.png"
HTML_PATH = "hit.html"
# --------------------------------------------------------------------


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def send_telegram(msg):
    if not (BOT_TOKEN and CHAT_IDS):
        return
    for cid in CHAT_IDS:
        try:
            requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                         params={"chat_id": cid, "text": msg}, timeout=10)
        except Exception as e:
            log(f"Telegram to {cid} failed: {e}")


def send_telegram_photo(path, caption=""):
    if not (BOT_TOKEN and CHAT_IDS):
        return
    for cid in CHAT_IDS:
        try:
            with open(path, "rb") as f:
                requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
                              data={"chat_id": cid, "caption": caption[:1024]},
                              files={"photo": f}, timeout=30)
        except Exception as e:
            log(f"Telegram photo to {cid} failed: {e}")


def classify(body):
    low = body.lower()
    if "no shows playing" in low:
        return "NO_SHOWS"
    if MOVIE_KEYWORD in low and (not FORMAT_KEYWORD or FORMAT_KEYWORD in low):
        return "HIT"
    if MOVIE_KEYWORD in low:
        return "MOVIE_NO_FORMAT"
    return "UNKNOWN"


def extract_showtimes(body):
    times = re.findall(r"\b\d{1,2}:\d{2}\s*[AP]M\b", body)
    return ", ".join(dict.fromkeys(times)) or "(open the page to see times)"


def load_page(page):
    for attempt in range(3):
        try:
            page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=45000)
            return True
        except Exception as e:
            log(f"goto attempt {attempt + 1} failed: {e}")
            page.wait_for_timeout(3000)
    return False


def main():
    fmt_label = FORMAT_KEYWORD.upper() or "any format"
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-http2", "--disable-blink-features=AutomationControlled"]
        )
        ctx = browser.new_context(user_agent=USER_AGENT,
                                  viewport={"width": 1280, "height": 900},
                                  locale="en-IN",
                                  extra_http_headers={"Accept-Language": "en-IN,en;q=0.9"})
        page = ctx.new_page()

        if not load_page(page):
            log("Page never loaded; skipping this run.")
            browser.close()
            return

        # wait up to ~10s for a decisive state to render
        for _ in range(20):
            low = page.inner_text("body").lower()
            if "no shows playing" in low or MOVIE_KEYWORD in low:
                break
            page.wait_for_timeout(500)

        body = page.inner_text("body")
        state = classify(body)
        log(f"State: {state}")

        if state == "HIT":
            page.screenshot(path=SHOT_PATH, full_page=True)
            with open(HTML_PATH, "w", encoding="utf-8") as f:
                f.write(page.content())
            times = extract_showtimes(body)
            msg = (f"\U0001F3AC ODYSSEY ({fmt_label}) is LISTED for {TARGET_DATE} "
                   f"at PVR Palazzo!\nShowtimes: {times}\n{TARGET_URL}\n"
                   f"(Disable the GitHub Action once you've booked to stop these.)")
            send_telegram_photo(SHOT_PATH, caption=msg)
            log("Alert sent.")
        else:
            # Save a snapshot anyway so we can confirm what the runner actually saw
            with open(HTML_PATH, "w", encoding="utf-8") as f:
                f.write(page.content())

        browser.close()


if __name__ == "__main__":
    main()
