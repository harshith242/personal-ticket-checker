#!/usr/bin/env python3
"""
The Paradise watcher — SINGLE-RUN version for GitHub Actions.

Checks BOTH venues on BOTH dates:
    Allu Cinemas Kokapet (ALUC)  -> screen/format must mention DOLBY
    Prasads Multiplex    (PRHN)  -> screen/format must mention PCX or BARCO
    Dates: 23 Sep 2026 (premieres) and 26 Sep 2026
    Language: Telugu only

IMPORTANT — why this uses curl_cffi and not a browser:
Cloudflare 403s ("Attention Required!") any client whose TLS/HTTP2 fingerprint
does not match a real Chrome. Playwright's headless Chromium fails this, and so
does plain requests/curl. curl_cffi with impersonate="chrome124" presents a
genuine Chrome TLS fingerprint and is served 200.

No browser is needed at all: BookMyShow server-renders the whole showtimes
payload into `window.__INITIAL_STATE__` inline in the HTML, so the data is in
the raw response body. This also means no React virtualization problem — the
blob contains every film, not just the visible cards.

The trigger is The Paradise appearing in the required format — NOT the date
merely opening, since these dates are days away and already open for other
films.

Environment variables (set as GitHub repository Secrets):
  BOT_TOKEN        your bot token
  CHAT_ID          your chat id
  MOVIE_KEYWORD    optional override (default "paradise")
  LANG_KEYWORD     optional override (default "telugu"; "" = any language)
  TARGET_DATES     optional override, comma-separated YYYYMMDD
  IGNORE_FORMAT    "1" to alert on ANY matching show (for testing)
"""

import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from curl_cffi import requests as curl_requests
from dotenv import load_dotenv

load_dotenv()

# ------------------------------ Config ------------------------------
MOVIE_KEYWORD = (os.getenv("MOVIE_KEYWORD") or "paradise").lower()
LANG_KEYWORD  = (os.getenv("LANG_KEYWORD") if os.getenv("LANG_KEYWORD") is not None
                 else "telugu").lower().strip()

TARGET_DATES = [d.strip() for d in
                (os.getenv("TARGET_DATES") or "20260923,20260926").split(",")
                if d.strip()]

IGNORE_FORMAT = os.getenv("IGNORE_FORMAT") == "1"

VENUES = [
    {
        "code": "ALUC",
        "name": "Allu Cinemas Kokapet",
        "path": "cinemas/HYD/allu-cinemas-kokapet/buytickets",
        "formats": ["dolby"],
    },
    {
        "code": "PRHN",
        "name": "Prasads Multiplex",
        "path": "cinemas/HYD/prasads-multiplex-hyderabad/buytickets",
        "formats": ["pcx", "barco"],
    },
]

ALERT_ON_WRONG_FORMAT = True    # ping if Paradise appears but not in your format

ATTEMPTS_PER_TARGET = 3
GAP_BETWEEN_TARGETS = (3, 7)    # random seconds; avoid a burst of identical hits

# Rotated across retries; all are real Chrome fingerprints curl_cffi ships.
IMPERSONATE = ["chrome124", "chrome131", "chrome120"]

HEADERS = {
    "Accept-Language": "en-IN,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
}

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

IST = timezone(timedelta(hours=5, minutes=30))
OUT_DIR = "snapshots"
# --------------------------------------------------------------------


def log(msg):
    print(f"[{datetime.now(IST):%H:%M:%S}] {msg}", flush=True)


def url_for(venue, date_code):
    return f"https://in.bookmyshow.com/{venue['path']}/{venue['code']}/{date_code}"


def pretty_date(code):
    try:
        return datetime.strptime(code, "%Y%m%d").strftime("%a %d %b")
    except ValueError:
        return code


# ------------------------------ Telegram ------------------------------
def send_telegram(msg):
    if not (BOT_TOKEN and CHAT_ID):
        log("No Telegram credentials; skipping notification.")
        return
    try:
        requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                     params={"chat_id": CHAT_ID, "text": msg[:4000]}, timeout=10)
    except Exception as e:
        log(f"Telegram failed: {e}")


# -------------------------- State extraction ------------------------
STATE_RE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*")


def extract_state(html):
    """
    Pull the showtimes slice out of the inline __INITIAL_STATE__ blob.
    Mirrors the old in-page JS: find the queries slice whose key mentions
    showtimesByVenue, and read its data.
    """
    m = STATE_RE.search(html)
    if not m:
        return None
    try:
        root, _ = json.JSONDecoder().raw_decode(html[m.end():])
    except ValueError:
        return None

    for slice_value in root.values():
        if not isinstance(slice_value, dict):
            continue
        queries = slice_value.get("queries")
        if not isinstance(queries, dict):
            continue
        key = next((k for k in queries
                    if "showtimesbyvenue" in k.lower()), None)
        if not key:
            continue
        data = (queries.get(key) or {}).get("data") or {}
        if "ShowDatesArray" not in data and "showDetailsTransformed" not in data:
            continue
        return {
            "servedDate": key.split("-")[-1],
            "showDates": data.get("ShowDatesArray") or [],
            "events": (data.get("showDetailsTransformed") or {}).get("Event") or [],
        }
    return None


def date_is_open(show_dates, target):
    for d in show_dates:
        if d.get("DateCode") == target:
            return not d.get("isDisabled", True)
    return None


def find_shows(events, format_keywords):
    """Shows for MOVIE_KEYWORD, scoped per movie so a format can't cross-match."""
    hits = []
    for event in events:
        if MOVIE_KEYWORD not in event.get("EventTitle", "").lower():
            continue
        for child in event.get("ChildEvents", []):
            dimension = child.get("EventDimension", "")
            language = child.get("EventLanguage", "")
            name = child.get("EventName", event.get("EventTitle", ""))
            if LANG_KEYWORD and LANG_KEYWORD not in language.lower():
                continue
            for show in child.get("ShowTimes", []):
                attributes = show.get("Attributes", "")
                screen = show.get("ScreenName", "")
                blob = f"{name} {dimension} {attributes} {screen}".lower()
                if format_keywords and not any(k in blob for k in format_keywords):
                    continue
                hits.append({
                    "name": name, "language": language, "dimension": dimension,
                    "time": show.get("ShowTime", "?"), "attributes": attributes,
                    "screen": screen,
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


# ---------------------------- One target ----------------------------
def check_target(venue, date_code):
    """
    Fetch the page HTML with a real-Chrome TLS fingerprint, parse the inline
    state, alert if wanted. Returns 'hit', 'partial', 'none', or 'blind'.
    """
    url = url_for(venue, date_code)
    tag = f"{venue['code']}-{date_code}"
    fmt_label = "/".join(f.upper() for f in venue["formats"])
    log(f"{venue['name']} | {pretty_date(date_code)} | need {fmt_label}")

    state = None
    html = ""
    for attempt in range(1, ATTEMPTS_PER_TARGET + 1):
        profile = IMPERSONATE[(attempt - 1) % len(IMPERSONATE)]
        try:
            resp = curl_requests.get(url, impersonate=profile,
                                     headers=HEADERS, timeout=30)
            html = resp.text
            state = extract_state(html)
            if state:
                break
            title = re.search(r"<title>([^<]*)", html)
            log(f"  attempt {attempt} ({profile}): status={resp.status_code} "
                f"title={(title.group(1)[:40] if title else '')!r} — no state")
        except Exception as e:
            log(f"  attempt {attempt} ({profile}) error: {e}")

        if attempt < ATTEMPTS_PER_TARGET:
            time.sleep(random.uniform(3, 6))

    if not state:
        os.makedirs(OUT_DIR, exist_ok=True)
        try:
            with open(os.path.join(OUT_DIR, f"blind-{tag}.html"),
                      "w", encoding="utf-8") as f:
                f.write(html)
        except Exception:
            pass
        return "blind"

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, f"state-{tag}.json"), "w",
              encoding="utf-8") as f:
        json.dump(state, f, indent=1)

    served = state["servedDate"]
    opened = date_is_open(state["showDates"], date_code)

    # BMS redirects an unopened date to today, so the URL proves nothing.
    if served != date_code or opened is False:
        log(f"  date not open yet (served={served}, open={opened})")
        return "none"

    fmt_keys = None if IGNORE_FORMAT else venue["formats"]
    hits = find_shows(state["events"], fmt_keys)
    any_fmt = find_shows(state["events"], None)
    log(f"  served={served} matching={len(hits)} any-format={len(any_fmt)}")

    header = f"{venue['name']} — {pretty_date(date_code)}"
    if hits:
        msg = (f"\U0001F3AC THE PARADISE ({fmt_label}) is LIVE!\n{header}\n"
               f"{format_shows(hits)}\n{url}\n"
               f"(Disable the cron job once you've booked.)")
        result = "hit"
    elif any_fmt and ALERT_ON_WRONG_FORMAT:
        msg = (f"⚠️ The Paradise is listed at {header}, but NOT "
               f"in {fmt_label} yet:\n{format_shows(any_fmt)}\n{url}")
        result = "partial"
    else:
        log("  not listed yet")
        return "none"

    with open(os.path.join(OUT_DIR, f"hit-{tag}.html"), "w",
              encoding="utf-8") as f:
        f.write(html)
    send_telegram(msg)
    log(f"  ALERT SENT ({result})")
    return result


# --------------------------------- Main -----------------------------
def main():
    today = datetime.now(IST).strftime("%Y%m%d")
    live_dates = [d for d in TARGET_DATES if d >= today]
    if not live_dates:
        log(f"All target dates {TARGET_DATES} are past (today {today}).")
        send_telegram("Paradise watcher: all target dates are in the past. "
                      "Update TARGET_DATES or disable the job.")
        sys.exit(1)

    log(f"Watching {MOVIE_KEYWORD!r} "
        f"({'any format' if IGNORE_FORMAT else 'per-venue formats'}, "
        f"lang={LANG_KEYWORD or 'any'}) on {', '.join(live_dates)}")

    results = []
    targets = [(v, d) for v in VENUES for d in live_dates]
    for i, (venue, date_code) in enumerate(targets):
        results.append(check_target(venue, date_code))
        if i < len(targets) - 1:
            time.sleep(random.uniform(*GAP_BETWEEN_TARGETS))

    log(f"Summary: {results.count('hit')} hit, {results.count('partial')} partial, "
        f"{results.count('none')} none, {results.count('blind')} blind")

    if results and all(r == "blind" for r in results):
        send_telegram("Paradise watcher could not read BookMyShow on ANY target "
                      "(blocked, or page structure changed). Not watching anything "
                      "right now — check the Action logs.")
        sys.exit(1)


if __name__ == "__main__":
    main()
