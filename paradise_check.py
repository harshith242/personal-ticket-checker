#!/usr/bin/env python3
"""
The Paradise watcher — SINGLE-RUN version for GitHub Actions.

Checks BOTH venues on BOTH dates:
    Allu Cinemas Kokapet (ALUC)  -> screen/format must mention DOLBY
    Prasads Multiplex    (PRHN)  -> screen/format must mention PCX or BARCO
    Dates: 23 Sep 2026 (premieres) and 24 Sep 2026 (release)
    Language: Telugu only

IMPORTANT — why this uses curl_cffi, a session, and a date-list gate:

Cloudflare blocks this site at the bot-management layer, not the rate-limit
layer: the 403 body says "Sorry, you have been blocked" with no 1015 code, so
there is no request rate that is reliably safe. Three things reduce the block
score, and all three are done here:

  1. TLS/HTTP2 fingerprint. Playwright headless Chromium and plain curl are both
     403'd; curl_cffi with impersonate="chrome124" is served 200.
  2. Cookies. The block page literally says "Please enable cookies". One Session
     per run, warmed on the home page first, carries __cf_bm into every request.
  3. Volume. Neither venue lists 23/24 Sep yet, and a date absent from
     ShowDatesArray can hold no shows. The bare venue URL returns that array, so
     one request per venue rules both dates out. Steady state is 3 requests per
     run instead of 12.

What cannot be fixed here is datacenter IP reputation: GitHub runners are Azure,
and cf_clearance is bound to IP+UA+TLS so cookies cannot be earned elsewhere.
If Actions comes back blind every run, the fix is a residential IP, not code.

No browser is needed at all: BookMyShow server-renders the whole showtimes
payload into `window.__INITIAL_STATE__` inline in the HTML. That also sidesteps
the React virtualization problem — the blob contains every film, not just the
cards currently on screen.

Environment variables (set as GitHub repository Secrets):
  BOT_TOKEN        your bot token
  CHAT_ID          your chat id
  MOVIE_KEYWORD    optional override (default "paradise")
  LANG_KEYWORD     optional override (default "telugu"; blank = any language)
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
                (os.getenv("TARGET_DATES") or "20260923,20260924").split(",")
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

HOME_URL = "https://in.bookmyshow.com/explore/home/hyderabad"
IMPERSONATE = "chrome124"

# Retrying fast into a bot-layer block does not help; the one recovery seen in
# the logs came from a slower attempt. Budget keeps the run under 3 minutes.
ATTEMPTS_PER_REQUEST = 3
RETRY_BACKOFF = (8, 20)         # seconds before attempts 2 and 3
RUN_BUDGET_SECONDS = 170
GAP_BETWEEN_VENUES = (4, 8)

HEADERS = {
    "Accept-Language": "en-IN,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
}

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

IST = timezone(timedelta(hours=5, minutes=30))
OUT_DIR = "snapshots"

REQUEST_COUNT = 0
# --------------------------------------------------------------------


def log(msg):
    print(f"[{datetime.now(IST):%H:%M:%S}] {msg}", flush=True)


def venue_base_url(venue):
    """Venue page with no date — returns the full ShowDatesArray."""
    return f"https://in.bookmyshow.com/{venue['path']}/{venue['code']}"


def venue_date_url(venue, date_code):
    return f"{venue_base_url(venue)}/{date_code}"


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
    Pull the showtimes slice out of the inline __INITIAL_STATE__ blob:
    the queries slice whose key mentions showtimesByVenue.
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
        key = next((k for k in queries if "showtimesbyvenue" in k.lower()), None)
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


def open_dates(state):
    """Date codes the venue is actually selling, as a set."""
    return {d.get("DateCode") for d in state["showDates"]
            if not d.get("isDisabled", True)}


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


# ------------------------------ Fetching ----------------------------
def fetch(session, url, deadline, label):
    """GET with backoff, staying inside the run budget. Returns HTML or None."""
    global REQUEST_COUNT
    for attempt in range(1, ATTEMPTS_PER_REQUEST + 1):
        if time.monotonic() >= deadline:
            log(f"  {label}: run budget spent, giving up")
            return None
        try:
            resp = session.get(url, timeout=30)
            REQUEST_COUNT += 1
            if resp.status_code == 200:
                return resp.text
            title = re.search(r"<title>([^<]*)", resp.text or "")
            log(f"  {label} attempt {attempt}: status={resp.status_code} "
                f"title={(title.group(1)[:40] if title else '')!r}")
        except Exception as e:
            REQUEST_COUNT += 1
            log(f"  {label} attempt {attempt} error: {e}")

        if attempt < ATTEMPTS_PER_REQUEST:
            wait = RETRY_BACKOFF[attempt - 1]
            if time.monotonic() + wait >= deadline:
                log(f"  {label}: not enough budget to back off, giving up")
                return None
            time.sleep(wait)
    return None


def new_session(deadline):
    """Session carrying one cookie jar, warmed on the home page for __cf_bm."""
    session = curl_requests.Session(impersonate=IMPERSONATE, headers=HEADERS)
    if fetch(session, HOME_URL, deadline, "warm-up") is None:
        log("  warm-up blocked (continuing without cookies)")
    else:
        log(f"  warm-up ok, cookies: {len(session.cookies)}")
    return session


def save(name, text):
    os.makedirs(OUT_DIR, exist_ok=True)
    try:
        with open(os.path.join(OUT_DIR, name), "w", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        pass


# ---------------------------- One venue -----------------------------
def check_date(session, venue, date_code, deadline):
    """Fetch one date's showtimes and alert if wanted. Assumes date is open."""
    url = venue_date_url(venue, date_code)
    tag = f"{venue['code']}-{date_code}"
    fmt_label = "/".join(f.upper() for f in venue["formats"])

    html = fetch(session, url, deadline, f"{tag} showtimes")
    if html is None:
        return "blind"
    state = extract_state(html)
    if state is None:
        save(f"blind-{tag}.html", html)
        return "blind"

    save(f"state-{tag}.json", json.dumps(state, indent=1))

    # BMS redirects an unopened date to today, so the URL proves nothing.
    if state["servedDate"] != date_code:
        log(f"  {tag}: served {state['servedDate']} instead — not open")
        return "none"

    fmt_keys = None if IGNORE_FORMAT else venue["formats"]
    hits = find_shows(state["events"], fmt_keys)
    any_fmt = find_shows(state["events"], None)
    log(f"  {tag}: matching={len(hits)} any-format={len(any_fmt)}")

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
        log(f"  {tag}: not listed yet")
        return "none"

    save(f"hit-{tag}.html", html)
    send_telegram(msg)
    log(f"  ALERT SENT ({result})")
    return result


def check_venue(session, venue, dates, deadline):
    """
    One request for the venue's date list; only dates that actually exist cost
    a second request. Returns one result per target date.
    """
    fmt_label = "/".join(f.upper() for f in venue["formats"])
    log(f"{venue['name']} | need {fmt_label} | {', '.join(pretty_date(d) for d in dates)}")

    html = fetch(session, venue_base_url(venue), deadline, f"{venue['code']} date list")
    if html is None:
        return ["blind"] * len(dates)
    state = extract_state(html)
    if state is None:
        save(f"blind-{venue['code']}-datelist.html", html)
        log(f"  {venue['code']}: served a page with no state")
        return ["blind"] * len(dates)

    available = open_dates(state)
    log(f"  {venue['code']} selling: {', '.join(sorted(available)) or 'nothing'}")

    results = []
    for date_code in dates:
        if date_code not in available:
            log(f"  {venue['code']}-{date_code}: date not listed yet")
            results.append("none")
            continue
        results.append(check_date(session, venue, date_code, deadline))
    return results


# --------------------------------- Main -----------------------------
def main():
    started = time.monotonic()
    deadline = started + RUN_BUDGET_SECONDS

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

    session = new_session(deadline)

    results = []
    for i, venue in enumerate(VENUES):
        results.extend(check_venue(session, venue, live_dates, deadline))
        if i < len(VENUES) - 1:
            time.sleep(random.uniform(*GAP_BETWEEN_VENUES))

    elapsed = time.monotonic() - started
    log(f"Summary: {results.count('hit')} hit, {results.count('partial')} partial, "
        f"{results.count('none')} none, {results.count('blind')} blind "
        f"({REQUEST_COUNT} requests, {elapsed:.0f}s)")

    if results and all(r == "blind" for r in results):
        send_telegram("Paradise watcher could not read BookMyShow on ANY target "
                      "(blocked, or page structure changed). Not watching anything "
                      "right now — check the Action logs.")
        sys.exit(1)


if __name__ == "__main__":
    main()
