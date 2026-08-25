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
FORMAT_KEYWORD = (os.getenv("FORMAT_KEYWORD") or "dolby").lower()
LANG_KEYWORD   = (os.getenv("LANG_KEYWORD")   or "").lower()   # e.g. "telugu"

# Alert once the date opens even if no matching show is listed on it yet. The
# date opening is the rare event; the shows usually appear in the same wave.
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


def matching_shows(events, format_keyword=None):
    """Shows for MOVIE_KEYWORD, scoped per movie so filters can't cross-match."""
    fmt = FORMAT_KEYWORD if format_keyword is None else format_keyword
    hits = []
    for event in events:
        title = event.get("EventTitle", "")
        if MOVIE_KEYWORD not in title.lower():
            continue
        for child in event.get("ChildEvents", []):
            dimension = child.get("EventDimension", "")
            language = child.get("EventLanguage", "")
            name = child.get("EventName", title)
            if LANG_KEYWORD and LANG_KEYWORD not in language.lower():
                continue
            for show in child.get("ShowTimes", []):
                attributes = show.get("Attributes", "")
                blob = f"{name} {dimension} {attributes}".lower()
                if fmt and fmt not in blob:
                    continue
                hits.append({
                    "name": name,
                    "language": language,
                    "dimension": dimension,
                    "time": show.get("ShowTime", "?"),
                    "attributes": attributes,
                    "screen": show.get("ScreenName", ""),
                    "price": f"{show.get('MinPrice', '?')}-{show.get('MaxPrice', '?')}",
                })
    return hits


def format_shows(hits):
    lines = []
    for h in hits:
        bits = [h["time"], f"{h['dimension']} {h['language']}".strip()]
        if h["attributes"]:
            bits.append(h["attributes"])
        if h["screen"]:
            bits.append(h["screen"])
        bits.append(f"Rs {h['price']}")
        lines.append("  - " + " | ".join(b for b in bits if b))
    return "\n".join(lines)


def scroll_through(page):
    """Force the virtualized list to render everything before screenshotting."""
    try:
        for _ in range(12):
            page.mouse.wheel(0, 900)
            page.wait_for_timeout(250)
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(500)
    except Exception as e:
        log(f"scroll pass failed (screenshot may be partial): {e}")


# --------------------------------- Main -----------------------------
def main():
    today = datetime.now(IST).strftime("%Y%m%d")
    if TARGET_DATE < today:
        log(f"TARGET_DATE {TARGET_DATE} is in the past (today {today}). Nothing to watch.")
        send_telegram(f"Toxic watcher is misconfigured: TARGET_DATE {TARGET_DATE} "
                      f"is already past (today {today}). Update it or disable the workflow.")
        sys.exit(1)

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

        state = load_state(page)

        if state is None:
            # Page never loaded, or BookMyShow changed how it ships showtimes.
            # Either way the watcher is blind, so make the run go red and say so.
            try:
                with open(HTML_PATH, "w", encoding="utf-8") as f:
                    f.write(page.content())
                page.screenshot(path=SHOT_PATH, full_page=True)
            except Exception:
                pass
            browser.close()
            log("Could not read showtimes state.")
            send_telegram("Toxic watcher could not read BookMyShow's showtimes data "
                          "(blocked, or the page structure changed). It is not "
                          "watching anything right now — check the Action logs.")
            sys.exit(1)

        with open(JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=1)

        served = state["servedDate"]
        opened = date_is_open(state["showDates"], TARGET_DATE)
        hits = matching_shows(state["events"]) if served == TARGET_DATE else []

        log(f"requested={TARGET_DATE} served={served} "
            f"date_open={opened} matching_shows={len(hits)}")

        if served != TARGET_DATE or opened is False:
            # Redirected back to today, or the date strip still flags it disabled.
            log("Target date not open yet.")
            browser.close()
            return

        if opened is None:
            log(f"Target date {TARGET_DATE} is no longer on the date strip. "
                f"Treating the served page as authoritative.")

        movie_label = MOVIE_KEYWORD.upper()
        fmt_label = FORMAT_KEYWORD.upper() or "any format"
        header = f"{pretty_date(TARGET_DATE)} at ALLU Cinemas Kokapet"

        if hits:
            msg = (f"\U0001F3AC {movie_label} ({fmt_label}) is LISTED for {header}!\n"
                   f"{format_shows(hits)}\n{TARGET_URL}\n"
                   f"(Disable the GitHub Action once you've booked to stop these.)")
        elif ALERT_ON_DATE_OPEN:
            other = matching_shows(state["events"], "")
            if other:
                msg = (f"\U0001F4C5 Bookings OPENED for {header}. {movie_label} is "
                       f"listed, but not in {fmt_label} yet:\n{format_shows(other)}\n"
                       f"{TARGET_URL}")
            else:
                titles = ", ".join(e.get("EventTitle", "?") for e in state["events"]) or "none"
                msg = (f"\U0001F4C5 Bookings OPENED for {header}, but {movie_label} "
                       f"is not listed at all.\nNow showing: {titles}\n{TARGET_URL}")
        else:
            log("Date open, no matching show, alert suppressed.")
            browser.close()
            return

        scroll_through(page)
        page.screenshot(path=SHOT_PATH, full_page=True)
        with open(HTML_PATH, "w", encoding="utf-8") as f:
            f.write(page.content())
        send_telegram_photo(SHOT_PATH, caption=msg)
        log("Alert sent.")
        browser.close()


if __name__ == "__main__":
    main()
