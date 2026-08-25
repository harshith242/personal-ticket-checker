#!/usr/bin/env python3
"""
Toxic ticket watcher — SINGLE-RUN version for GitHub Actions.

Checks the BookMyShow venue page ONCE for a target date. BookMyShow serves the
showtimes as an embedded `window.__INITIAL_STATE__` blob, so this reads the
structured JSON instead of scraping text — the movie list is React-virtualized,
so only the visible cards exist in the DOM and text scraping misses the rest.

Two facts come out of one page load:
  * ShowDatesArray[].isDisabled  — whether the target date has opened at all.
    BookMyShow redirects an unopened date back to today, so the requested URL
    alone is not proof of what got served.
  * Event[] -> ChildEvents[] -> ShowTimes[]  — the actual shows, scoped per
    movie, so a format keyword can't match a different film on the same page.

Config comes from environment variables (set as GitHub repository Secrets):
  BOT_TOKEN        your bot token
  CHAT_ID          your chat id
  EXTRA_CHAT_IDS   optional, comma-separated extra recipients
  TARGET_DATE      optional, overrides the date below (YYYYMMDD)
  MOVIE_KEYWORD / FORMAT_KEYWORD / LANG_KEYWORD   optional filter overrides
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import requests
from playwright.sync_api import sync_playwright

# ------------------------------ Config ------------------------------
TARGET_DATE = os.getenv("TARGET_DATE") or "20260826"   # YYYYMMDD

VENUE_CODE  = "ALUC"
VENUE_PATH  = "cinemas/HYD/allu-cinemas-kokapet/buytickets"
TARGET_URL  = f"https://in.bookmyshow.com/{VENUE_PATH}/{VENUE_CODE}/{TARGET_DATE}"

# All three match case-insensitively; "" disables that filter. Env vars let a
# manual workflow_dispatch run test against a date/movie that already exists.
MOVIE_KEYWORD  = (os.getenv("MOVIE_KEYWORD")  or "toxic").lower()
FORMAT_KEYWORD = (os.getenv("FORMAT_KEYWORD") or "").lower()   # e.g. "imax", "dolby"
LANG_KEYWORD   = (os.getenv("LANG_KEYWORD")   or "").lower()   # e.g. "telugu"

# Alert once the date opens even if the movie isn't listed on it yet. The date
# opening is the rare event; the movie usually appears in the same wave.
ALERT_ON_DATE_OPEN = True

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36")

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_IDS = [c.strip() for c in
            [os.getenv("CHAT_ID", ""), *os.getenv("EXTRA_CHAT_IDS", "").split(",")]
            if c and c.strip()]

SHOT_PATH = "hit.png"
HTML_PATH = "hit.html"
JSON_PATH = "state.json"
IST = timezone(timedelta(hours=5, minutes=30))
# --------------------------------------------------------------------


def log(msg):
    print(f"[{datetime.now(IST):%H:%M:%S}] {msg}", flush=True)


def send_telegram(msg):
    if not (BOT_TOKEN and CHAT_IDS):
        log("No Telegram credentials; skipping notification.")
        return
    for cid in CHAT_IDS:
        try:
            requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                         params={"chat_id": cid, "text": msg[:4000]}, timeout=10)
        except Exception as e:
            log(f"Telegram to {cid} failed: {e}")


def send_telegram_photo(path, caption=""):
    if not (BOT_TOKEN and CHAT_IDS):
        log("No Telegram credentials; skipping notification.")
        return
    for cid in CHAT_IDS:
        try:
            with open(path, "rb") as f:
                requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
                              data={"chat_id": cid, "caption": caption[:1024]},
                              files={"photo": f}, timeout=30)
        except Exception as e:
            log(f"Telegram photo to {cid} failed: {e}")
            send_telegram(caption)


def pretty_date(code):
    try:
        return datetime.strptime(code, "%Y%m%d").strftime("%a %d %b %Y")
    except ValueError:
        return code


# -------------------------- Page interaction ------------------------
EXTRACT_JS = """
() => {
  const S = window.__INITIAL_STATE__;
  if (!S) return null;
  const q = (S.venueShowtimesFunctionalApi || {}).queries || {};
  const key = Object.keys(q).find(k => k.startsWith('getShowtimesByVenue'));
  if (!key) return null;
  const data = q[key].data || {};
  return {
    servedDate: key.split('-').pop(),
    showDates: data.ShowDatesArray || [],
    events: (data.showDetailsTransformed || {}).Event || []
  };
}
"""


def load_state(page):
    """Navigate and pull the embedded state. Returns the dict, or None."""
    for attempt in range(3):
        try:
            page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            log(f"goto attempt {attempt + 1} failed: {e}")
            page.wait_for_timeout(3000)
            continue
        # The blob is inlined by SSR, but the redirect to today re-renders it.
        for _ in range(20):
            state = page.evaluate(EXTRACT_JS)
            if state:
                return state
            page.wait_for_timeout(500)
        log(f"attempt {attempt + 1}: page loaded but __INITIAL_STATE__ never appeared")
    return None


def date_is_open(show_dates, target):
    """True/False if the date strip mentions the target, None if it doesn't."""
    for d in show_dates:
        if d.get("DateCode") == target:
            return not d.get("isDisabled", True)
    return None


def matching_shows(events):
    """Shows for MOVIE_KEYWORD, scoped per movie so filters can't cross-match."""
