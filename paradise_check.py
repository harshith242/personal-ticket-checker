#!/usr/bin/env python3
"""
The Paradise watcher — SINGLE-RUN version for GitHub Actions.

Checks BOTH venues on BOTH dates:
    Allu Cinemas Kokapet (ALUC)  -> screen/format must mention DOLBY
    Prasads Multiplex    (PRHN)  -> screen/format must mention PCX or BARCO
    Dates: 23 Sep 2026 (premieres) and 24 Sep 2026 (release)
    Language: Telugu only

IMPORTANT — why each target gets its own browser:
Cloudflare 403s the Prasads page on ANY navigation after the first one in a
browser session. Testing showed the target page succeeds as the first
navigation (headless or headed), and fails as the second regardless of stealth
patches, warmed cookies, or real Chrome. So every venue/date runs in a fresh
browser with exactly one navigation, and retries relaunch rather than reload.

Reads BookMyShow's embedded `window.__INITIAL_STATE__` rather than scraping
text, because the movie list is React-virtualized: only visible cards exist in
the DOM, so text scraping silently misses films further down the page.

The trigger is The Paradise appearing in the required format — NOT the date
merely opening, since these dates are days away and already open for other
films.

Environment variables (set as GitHub repository Secrets):
  BOT_TOKEN        your bot token
  CHAT_ID          your chat id
  EXTRA_CHAT_IDS   optional, comma-separated extra recipients
  MOVIE_KEYWORD    optional override (default "paradise")
  LANG_KEYWORD     optional override (default "telugu"; "" = any language)
  TARGET_DATES     optional override, comma-separated YYYYMMDD
  IGNORE_FORMAT    "1" to alert on ANY matching show (for testing)
"""

import json
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv

load_dotenv()

# ------------------------------ Config ------------------------------
MOVIE_KEYWORD = (os.getenv("MOVIE_KEYWORD") or "paradise").lower()
LANG_KEYWORD  = (os.getenv("LANG_KEYWORD") if os.getenv("LANG_KEYWORD") is not None
                 else "telugu").lower()

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
GAP_BETWEEN_TARGETS = (6, 12)   # random seconds; avoid a burst of identical hits

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")

LAUNCH_ARGS = ["--disable-http2", "--disable-blink-features=AutomationControlled"]

HEADERS = {
    "Accept-Language": "en-IN,en;q=0.9",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Upgrade-Insecure-Requests": "1",
}

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
Object.defineProperty(navigator, 'languages', {get: () => ['en-IN','en']});
window.chrome = window.chrome || {runtime: {}};
"""

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


def send_telegram_photo(path, caption=""):
    if not (BOT_TOKEN and CHAT_ID):
        log("No Telegram credentials; skipping notification.")
        return
    try:
        with open(path, "rb") as f:
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
                          data={"chat_id": CHAT_ID, "caption": caption[:1024]},
                          files={"photo": f}, timeout=30)
    except Exception as e:
        log(f"Telegram photo failed: {e}")
        send_telegram(caption)


# -------------------------- State extraction ------------------------
EXTRACT_JS = """
() => {
  const S = window.__INITIAL_STATE__;
  if (!S) return null;
  for (const slice of Object.keys(S)) {
    const v = S[slice];
    const q = v && v.queries;
    if (!q) continue;
    const key = Object.keys(q).find(k => k.toLowerCase().includes('showtimesbyvenue'));
    if (!key) continue;
    const data = (q[key] || {}).data || {};
    if (!data.ShowDatesArray && !data.showDetailsTransformed) continue;
    return {
      servedDate: key.split('-').pop(),
      showDates: data.ShowDatesArray || [],
      events: (data.showDetailsTransformed || {}).Event || []
    };
  }
  return null;
}
"""


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
    Fresh browser, ONE navigation, extract, optionally screenshot, close.
    Returns 'hit', 'partial', 'none', or 'blind'.
    """
    url = url_for(venue, date_code)
    tag = f"{venue['code']}-{date_code}"
    fmt_label = "/".join(f.upper() for f in venue["formats"])
    log(f"{venue['name']} | {pretty_date(date_code)} | need {fmt_label}")

    for attempt in range(1, ATTEMPTS_PER_TARGET + 1):
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=LAUNCH_ARGS)
            try:
                ctx = browser.new_context(
                    user_agent=UA, viewport={"width": 1280, "height": 900},
                    locale="en-IN", timezone_id="Asia/Kolkata",
                    extra_http_headers=HEADERS)
                ctx.add_init_script(STEALTH_JS)
                page = ctx.new_page()

                # The one and only navigation in this session.
                resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
                status = resp.status if resp else 0

                state = None
                for _ in range(20):
                    state = page.evaluate(EXTRACT_JS)
                    if state:
                        break
                    page.wait_for_timeout(500)

                if not state:
                    log(f"  attempt {attempt}: status={status} "
                        f"title={page.title()[:40]!r} — no state")
                    if attempt == ATTEMPTS_PER_TARGET:
                        os.makedirs(OUT_DIR, exist_ok=True)
                        try:
                            with open(os.path.join(OUT_DIR, f"blind-{tag}.html"),
                                      "w", encoding="utf-8") as f:
                                f.write(page.content())
                            page.screenshot(path=os.path.join(OUT_DIR, f"blind-{tag}.png"),
                                            full_page=True)
                        except Exception:
                            pass
                        return "blind"
                    time.sleep(random.uniform(5, 10))
                    continue

                # --- we have state ---
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
                    msg = (f"\u26A0\uFE0F The Paradise is listed at {header}, but NOT "
                           f"in {fmt_label} yet:\n{format_shows(any_fmt)}\n{url}")
                    result = "partial"
                else:
                    log("  not listed yet")
                    return "none"

                # Scrolling is not navigation, so it's safe in this session.
                try:
                    for _ in range(12):
                        page.mouse.wheel(0, 900)
                        page.wait_for_timeout(250)
                    page.evaluate("window.scrollTo(0, 0)")
                    page.wait_for_timeout(500)
                except Exception as e:
                    log(f"  scroll failed (screenshot may be partial): {e}")

                shot = os.path.join(OUT_DIR, f"hit-{tag}.png")
                page.screenshot(path=shot, full_page=True)
                with open(os.path.join(OUT_DIR, f"hit-{tag}.html"), "w",
                          encoding="utf-8") as f:
                    f.write(page.content())
                send_telegram_photo(shot, caption=msg)
                log(f"  ALERT SENT ({result})")
                return result

            except Exception as e:
                log(f"  attempt {attempt} error: {e}")
                if attempt == ATTEMPTS_PER_TARGET:
                    return "blind"
                time.sleep(random.uniform(5, 10))
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    return "blind"


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
