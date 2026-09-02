#!/usr/bin/env python3
"""
Regal Watch - a tiny personal agent that watches Regal Cinemas for a movie
and tells you the moment (a) Regal lists it and (b) tickets open at YOUR theatre.

It uses the same public JSON endpoint the regmovies.com site uses for its
showtime grid, so there is no login, no HTML scraping, no CAPTCHA.

Usage:
  python regal_watch.py                 # one check (what the scheduler runs)
  python regal_watch.py --loop          # keep running, poll every N minutes
  python regal_watch.py --find-theatre "aviation"      # look up theatre codes
  python regal_watch.py --find-movie "avengers"        # look up film codes
  python regal_watch.py --test-notify   # send a test alert to every channel
  python regal_watch.py --status        # print current state without alerting
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import smtplib
import subprocess
import sys
import time
import webbrowser
from datetime import date, datetime
from email.mime.text import MIMEText
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
STATE_PATH = HERE / "state.json"
LOG_PATH = HERE / "regal_watch.log"

BASE = "https://www.regmovies.com"
SHOWTIMES_API = BASE + "/api/getShowtimes"
SELECT_TICKETS = "https://experience.regmovies.com/select-tickets?site={site}&id={perf}"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

session = requests.Session()  # used for ntfy / telegram posts only
session.headers.update({"User-Agent": UA, "Accept": "application/json,text/html"})

# Regal sits behind Cloudflare, which rejects Python's default TLS fingerprint
# (plain `requests` gets a 403 challenge page). curl_cffi impersonates Chrome's
# handshake and sails through; if it's missing we fall back to Windows' curl.exe.
try:
    from curl_cffi import requests as _cffi  # type: ignore
except ImportError:  # pragma: no cover
    _cffi = None


def regal_get(url: str, params: dict | None = None) -> str:
    """GET a Regal URL and return the body text, dodging the Cloudflare challenge."""
    if params:
        from urllib.parse import urlencode
        url = f"{url}?{urlencode(params)}"
    last = "no attempt"
    for attempt, backoff in enumerate((0, 30, 90), start=1):  # 429 = we asked too often; wait it out
        if backoff:
            log(f"Regal rate-limited us; waiting {backoff}s before retry {attempt}")
            time.sleep(backoff)
        if _cffi is not None:
            r = _cffi.get(url, impersonate="chrome", timeout=30)
            if r.status_code == 200:
                return r.text
            last = f"curl_cffi HTTP {r.status_code}"
            if r.status_code == 429:
                continue
            log(f"{last} for {url}; trying curl.exe")
        curl = shutil.which("curl") or r"C:\Windows\System32\curl.exe"
        proc = subprocess.run([curl, "-sS", "-L", "--max-time", "40", "-A", UA, "-w", "\n%{http_code}", url],
                              capture_output=True, timeout=60)
        out = proc.stdout.decode("utf-8", "replace")
        body, _, status = out.rpartition("\n")
        if proc.returncode == 0 and status == "200" and "Just a moment" not in body[:2000]:
            return body
        last = f"curl.exe rc={proc.returncode} HTTP {status or '?'} {proc.stderr.decode(errors='replace')[:120]}"
        if status != "429":
            break
    raise RuntimeError(f"Regal fetch failed for {url}: {last}")


# ----------------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------------
def log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode())
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


# ----------------------------------------------------------------------------
# Regal data access
# ----------------------------------------------------------------------------
def fetch_next_data() -> dict:
    """The homepage embeds the theatre list + movie feeds (Now Playing / Coming Soon)."""
    html = regal_get(BASE + "/")
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
    if not m:
        raise RuntimeError("Could not find __NEXT_DATA__ on regmovies.com homepage")
    return json.loads(m.group(1))["props"]["pageProps"]


def all_theatres(page: dict) -> list[dict]:
    return page.get("fullTheatreData") or []


def all_movies(page: dict) -> dict[str, dict]:
    """FilmCode -> {title, feed}. Collected from every feed on the homepage."""
    out: dict[str, dict] = {}
    feeds = []
    for key in ("searchFeed", "showData"):
        val = page.get(key)
        if isinstance(val, list):
            feeds += val
        elif isinstance(val, dict):
            feeds += [v for v in val.values() if isinstance(v, dict)]
    for feed in feeds:
        for entry in feed.get("MovieFeedEntries", []):
            mv = entry.get("Movie") or {}
            code = mv.get("FilmCode")
            if code and code not in out:
                out[code] = {"title": mv.get("Title"), "feed": feed.get("FeedName"), "code": code}
    return out


def find_theatres(page: dict, query: str) -> list[dict]:
    q = norm(query)
    hits = []
    for t in all_theatres(page):
        hay = norm(" ".join(str(t.get(k) or "") for k in ("name", "city", "state", "postal_code", "theatre_code")))
        if q in hay or q == norm(t.get("theatre_code", "")):
            hits.append(t)
    return hits


def find_movies(page: dict, query: str, fuzzy: bool = False) -> list[dict]:
    """Exact title first, then whole-word substring ('The Paradise' -> 'The Paradise (Telugu)').
    Fuzzy 'did you mean' matches are only for the interactive --find-movie lookup; the
    watcher never uses them, otherwise 'The Paradise' would latch onto 'The Uprising'."""
    q = norm(query)
    movies = list(all_movies(page).values())
    exact = [m for m in movies if norm(m["title"]) == q]
    if exact:
        return exact
    partial = [m for m in movies if re.search(rf"\b{re.escape(q)}\b", norm(m["title"]))]
    if partial or not fuzzy:
        return partial
    scored = [(difflib.SequenceMatcher(None, q, norm(m["title"])).ratio(), m) for m in movies]
    scored.sort(key=lambda x: -x[0])
    return [dict(m, title=m["title"] + "   (closest match, not exact)") for s, m in scored[:5] if s > 0.6]


THEATRE_PATHS: dict[str, str] = {}   # theatre_code -> path_name, filled by resolve_targets
_API_BLOCKED = os.environ.get("REGAL_FORCE_HTML") == "1"


def board_from_theatre_page(path_name: str) -> dict:
    """Same shape as the API answer, scraped from the server-rendered theatre page.
    Used when Cloudflare blocks the JSON API (it does for datacenter IPs such as
    GitHub Actions) but still serves the HTML pages."""
    cached = _PAGE_CACHE.get(path_name)
    if cached and time.time() - cached[0] < 240:  # one 2 MB page per theatre per run, not per date
        return json.loads(cached[1])
    html = regal_get(f"{BASE}/theatres/{path_name}")
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
    if not m:
        raise RuntimeError("theatre page without __NEXT_DATA__")
    pp = json.loads(m.group(1))["props"]["pageProps"]
    board = {
        "shows": pp.get("showtimes") or [],
        "futureShows": pp.get("futureShows") or [],
        "movies": pp.get("movies") or [],
        "datesWithShows": pp.get("datesWithShows") or [],
        "_source": "html",
    }
    _PAGE_CACHE[path_name] = (time.time(), json.dumps(board))
    return board


_PAGE_CACHE: dict[str, tuple[float, str]] = {}


def get_showtimes(theatre_code: str, day: date, ho_code: str = "") -> dict:
    global _API_BLOCKED
    params = {
        "theatres": theatre_code,
        "date": day.strftime("%Y-%m-%d"),
        "hoCode": ho_code,
        "ignoreCache": "false",
        "moviesOnly": "false",
    }
    if not _API_BLOCKED:
        try:
            return json.loads(regal_get(SHOWTIMES_API, params))
        except RuntimeError as e:
            if "HTTP 403" not in str(e):
                raise
            _API_BLOCKED = True
            log("Regal JSON API is blocked from this network; using theatre pages instead")
    path = THEATRE_PATHS.get(theatre_code)
    if not path:
        raise RuntimeError(f"no path_name known for theatre {theatre_code}")
    board = board_from_theatre_page(path)
    if ho_code:  # the page only renders today's grid; other days come back empty
        board["shows"] = [
            dict(s, Film=[f for f in s.get("Film", []) if f.get("MasterMovieCode") == ho_code])
            for s in board["shows"] if s.get("AdvertiseShowDate", "")[:10] == day.isoformat()
        ]
    return board


def parse_future_date(s: str) -> date:
    # API gives "9-3-2026" (M-D-YYYY)
    m, d, y = (int(x) for x in s.split("-"))
    return date(y, m, d)


def booking_link(path_name: str, theatre_code: str, perf_id, day: str) -> str:
    """Theatre page opened on that day with the showtime pre-selected (verified to load)."""
    y, m, d = day.split("-")
    return f"{BASE}/theatres/{path_name}?date={m}-{d}-{y}&site={theatre_code}&id={perf_id}"


def performances_for(theatre_code: str, path_name: str, ho_code: str, days: list[date]) -> list[dict]:
    """Flatten every performance of the film at the theatre across the given days."""
    perfs = []
    for day in days:
        data = get_showtimes(theatre_code, day, ho_code)
        for show in data.get("shows", []):
            for film in show.get("Film", []):
                for p in film.get("Performances", []):
                    show_day = show.get("AdvertiseShowDate", "")[:10]
                    perfs.append({
                        "title": film.get("Title"),
                        "film_code": film.get("MasterMovieCode"),
                        "theatre_code": show.get("TheatreCode"),
                        "date": show.get("AdvertiseShowDate", "")[:10],
                        "time": p.get("CalendarShowTime", "")[11:16],
                        "attrs": p.get("PerformanceAttributes", []),
                        "group": p.get("PerformanceGroup") or "2D",
                        "perf_id": p.get("PerformanceId"),
                        "sold_out": bool(p.get("StopSales")),
                        "link": booking_link(path_name, theatre_code, p.get("PerformanceId"), show_day),
                        "alt_link": SELECT_TICKETS.format(site=theatre_code, perf=p.get("PerformanceId")),
                    })
        time.sleep(1.5)  # be polite: Cloudflare challenges bursts of rapid API calls
    return perfs


# ----------------------------------------------------------------------------
# preference scoring: pick the showtime you'd most likely want
# ----------------------------------------------------------------------------
def score_perf(p: dict, prefs: dict) -> float:
    s = 0.0
    if p["sold_out"]:
        return -1e9
    fmts = [f.lower() for f in prefs.get("preferred_formats", [])]
    attrs = " ".join(p["attrs"]).lower() + " " + p["group"].lower()
    for i, f in enumerate(fmts):
        if f in attrs:
            s += 100 - i * 10
    for f in prefs.get("avoid_formats", []):
        if f.lower() in attrs:
            s -= 200
    days = [d.lower()[:3] for d in prefs.get("preferred_days", [])]
    if days:
        wd = datetime.strptime(p["date"], "%Y-%m-%d").strftime("%a").lower()
        if wd in days:
            s += 50
    hhmm = p["time"]
    if hhmm:
        hour = int(hhmm[:2]) + int(hhmm[3:]) / 60
        lo, hi = prefs.get("preferred_hours", [17, 22])
        if lo <= hour <= hi:
            s += 30
        else:
            s -= abs(hour - (lo + hi) / 2)
    # earlier dates slightly preferred
    s -= (datetime.strptime(p["date"], "%Y-%m-%d").date() - date.today()).days * 0.1
    return s


def best_perfs(perfs: list[dict], prefs: dict, n: int = 5) -> list[dict]:
    return sorted(perfs, key=lambda p: -score_perf(p, prefs))[:n]


# ----------------------------------------------------------------------------
# notifications
# ----------------------------------------------------------------------------
def notify_ntfy(cfg: dict, title: str, body: str, link: str | None) -> None:
    topic = cfg.get("topic")
    if not topic:
        return
    server = cfg.get("server", "https://ntfy.sh").rstrip("/")
    headers = {"Title": title, "Priority": str(cfg.get("priority", "high")), "Tags": "clapper,ticket"}
    if cfg.get("email"):  # ntfy.sh forwards the alert to this address too, no SMTP account needed
        headers["Email"] = cfg["email"]
    if link:
        headers["Click"] = link
        headers["Actions"] = f"view, Book now, {link}"
    r = session.post(f"{server}/{topic}", data=body.encode("utf-8"), headers=headers, timeout=20)
    r.raise_for_status()
    log(f"ntfy sent to {server}/{topic}")


TOAST_PS = r'''
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = @"
<toast activationType="protocol" launch="__LINK__" scenario="reminder">
  <visual><binding template="ToastGeneric">
    <text>__TITLE__</text><text>__BODY__</text>
  </binding></visual>
  <audio src="ms-winsoundevent:Notification.Looping.Alarm2" loop="false"/>
</toast>
"@
$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml($xml)
$toast = New-Object Windows.UI.Notifications.ToastNotification $doc
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Regal Watch").Show($toast)
'''


def notify_toast(title: str, body: str, link: str | None) -> None:
    if os.name != "nt":
        return

    def esc(s: str) -> str:
        return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                 .replace('"', "&quot;").replace("`", "``").replace("$", "`$"))

    ps = (TOAST_PS.replace("__TITLE__", esc(title))
                  .replace("__BODY__", esc(body[:400]))
                  .replace("__LINK__", esc(link or BASE)))
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       check=True, capture_output=True, timeout=30)
        log("windows toast shown")
    except Exception as e:  # noqa: BLE001
        log(f"toast failed: {e}")


def notify_email(cfg: dict, title: str, body: str, link: str | None) -> None:
    if not cfg.get("to") or not cfg.get("smtp_user") or not cfg.get("smtp_password"):
        return
    text = body + (f"\n\nBook: {link}" if link else "")
    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = title
    msg["From"] = cfg.get("from") or cfg["smtp_user"]
    msg["To"] = cfg["to"]
    with smtplib.SMTP(cfg.get("smtp_host", "smtp.gmail.com"), int(cfg.get("smtp_port", 587)), timeout=30) as s:
        s.starttls()
        s.login(cfg["smtp_user"], cfg["smtp_password"])
        s.send_message(msg)
    log(f"email sent to {cfg['to']}")


def notify_telegram(cfg: dict, title: str, body: str, link: str | None) -> None:
    if not cfg.get("bot_token") or not cfg.get("chat_id"):
        return
    text = f"{title}\n{body}" + (f"\n\nBook now: {link}" if link else "")
    r = session.post(f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage",
                     json={"chat_id": cfg["chat_id"], "text": text}, timeout=20)
    r.raise_for_status()
    log("telegram sent")


def notify_all(config: dict, title: str, body: str, link: str | None = None) -> None:
    n = config.get("notify", {})
    channels = {
        "ntfy": lambda: notify_ntfy(n.get("ntfy", {}), title, body, link),
        "toast": lambda: notify_toast(title, body, link) if n.get("windows_toast", True) else None,
        "email": lambda: notify_email(n.get("email", {}), title, body, link),
        "telegram": lambda: notify_telegram(n.get("telegram", {}), title, body, link),
    }
    for name, fn in channels.items():
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            log(f"{name} notification failed: {e}")


# ----------------------------------------------------------------------------
# the check itself
# ----------------------------------------------------------------------------
def resolve_targets(config: dict, page: dict) -> tuple[list[dict], list[dict]]:
    """Turn the human config (names) into codes. Returns (theatres, movies)."""
    theatres = []
    for t in config.get("theatres", []):
        if isinstance(t, dict) and t.get("theatre_code") and t.get("path_name"):
            theatres.append(t)
            continue
        hits = find_theatres(page, t["theatre_code"] if isinstance(t, dict) else str(t))
        if not hits:
            log(f"WARNING: theatre '{t}' not found; run --find-theatre")
            continue
        if len(hits) > 1:
            log(f"NOTE: '{t}' matched {len(hits)} theatres, using first: {hits[0]['name']} ({hits[0]['theatre_code']})")
        h = hits[0]
        theatres.append({"name": h["name"], "theatre_code": h["theatre_code"], "path_name": h["path_name"]})
    for t in theatres:
        THEATRE_PATHS[t["theatre_code"]] = t["path_name"]

    movies = []
    for m in config.get("movies", []):
        if isinstance(m, dict) and m.get("code"):
            movies.append({"title": m.get("title", m["code"]), "code": m["code"],
                           "query": m.get("title", m["code"]), "feed": "config"})
            continue
        query = m if isinstance(m, str) else m.get("title", "")
        hits = find_movies(page, query)
        if hits:
            movies.append({"title": hits[0]["title"], "code": hits[0]["code"], "query": query, "feed": hits[0]["feed"]})
        else:
            movies.append({"title": query, "code": None, "query": query, "feed": None})
    return theatres, movies


def match_codes(query: str, board_titles: dict[str, str]) -> list[str]:
    """Film codes on a theatre board whose title matches the query.
    'The Paradise' matches 'The Paradise (Telugu)' and 'The Paradise (Hindi)' alike."""
    q = norm(query)
    exact = [c for c, t in board_titles.items() if norm(t) == q]
    if exact:
        return exact
    return [c for c, t in board_titles.items() if re.search(rf"\b{re.escape(q)}\b", norm(t))]


def fmt_perf(p: dict) -> str:
    return f"- {p['date']} {p['time']}  {p['group']}  {p['link']}"


def check_once(config: dict, announce: bool = True) -> dict:
    page = fetch_next_data()
    theatres, movies = resolve_targets(config, page)
    state = load_json(STATE_PATH, {})
    prefs = config.get("preferences", {})
    today = date.today()
    summary: dict = {}

    # Each theatre's board lists every film that has showtimes there, including
    # regional-language releases that never appear on Regal's homepage feeds.
    boards = {th["theatre_code"]: get_showtimes(th["theatre_code"], today) for th in theatres}

    for mv in movies:
        key_movie = mv["query"]
        st = state.setdefault(key_movie, {"listed": False, "theatres": {}})
        homepage_code = mv["code"]
        tickets_opened_now = False

        for th in theatres:
            tcode = th["theatre_code"]
            board = boards[tcode]
            board_titles = {m["FilmCode"]: m["Title"] for m in board.get("movies", []) if m.get("FilmCode")}

            # --- which film code(s) does this movie map to at this theatre? ---
            codes = match_codes(key_movie, board_titles)
            if homepage_code and homepage_code in board_titles and homepage_code not in codes:
                codes.append(homepage_code)
            if not codes:
                summary.setdefault(key_movie, {})[tcode] = {"on_sale": False, "listed": bool(homepage_code)}
                continue

            for fcode in codes:
                title = board_titles[fcode]
                tst = st["theatres"].setdefault(f"{tcode}:{fcode}", {"on_sale": False, "dates": [], "perf_ids": []})
                fut = next((f for f in board.get("futureShows", []) if f.get("hoCode") == fcode), None)
                dates = sorted(parse_future_date(d["date"]) for d in (fut or {}).get("dates", []))
                # futureShows excludes the queried day itself, so check today's grid too
                if any(f.get("MasterMovieCode") == fcode
                       for s in board.get("shows", []) for f in s.get("Film", [])):
                    dates = sorted(set(dates) | {today})

                if not dates:
                    log(f"'{title}' @ {th['name']}: on the board, but no showtimes yet")
                    summary.setdefault(key_movie, {})[f"{tcode}:{fcode}"] = {"on_sale": False, "title": title}
                    continue

                perfs = performances_for(tcode, th["path_name"], fcode,
                                         dates[: int(config.get("max_days_to_scan", 7))])
                new_perf_ids = [p["perf_id"] for p in perfs if p["perf_id"] not in tst["perf_ids"]]
                top = best_perfs(perfs, prefs)
                if not top:
                    # API blocked (cloud): no per-showtime links, so link each date's page instead
                    top = [{"date": d.isoformat(), "time": "see page", "group": "",
                            "link": f"{BASE}/theatres/{th['path_name']}?date={d:%m-%d-%Y}",
                            "perf_id": None, "sold_out": False, "attrs": []}
                           for d in dates[:5]]
                best_link = top[0]["link"]
                summary.setdefault(key_movie, {})[f"{tcode}:{fcode}"] = {
                    "on_sale": True, "title": title, "dates": [d.isoformat() for d in dates],
                    "showtimes": len(perfs), "best": top[:3],
                }

                if not tst["on_sale"]:
                    # THE moment you've been waiting for
                    tickets_opened_now = True
                    lines = [f"Tickets are OPEN for \"{title}\" at {th['name']}!",
                             f"{len(perfs)} showtimes across {len(dates)} days (first: {dates[0]:%a %b %d}).", ""]
                    lines += [fmt_perf(p) for p in top]
                    body = "\n".join(lines)
                    log(body)
                    if announce:
                        notify_all(config, f"TICKETS OPEN: {title} @ {th['name']}", body, best_link)
                        if config.get("open_browser_when_tickets_open", True):
                            webbrowser.open(best_link)
                            log(f"opened browser at {best_link}")
                    tst["on_sale"] = True
                elif new_perf_ids and config.get("alert_on_new_showtimes", True):
                    added = [p for p in perfs if p["perf_id"] in new_perf_ids]
                    body = (f"{len(added)} new showtimes added for \"{title}\" at {th['name']}:\n"
                            + "\n".join(fmt_perf(p) for p in best_perfs(added, prefs)))
                    log(body)
                    if announce:
                        notify_all(config, f"New showtimes: {title} @ {th['name']}", body, best_link)
                else:
                    log(f"'{title}' @ {th['name']}: on sale, nothing new ({len(perfs)} showtimes)")

                tst["title"] = title
                tst["dates"] = [d.isoformat() for d in dates]
                tst["perf_ids"] = sorted({p["perf_id"] for p in perfs} | set(tst["perf_ids"]))
                tst["last_seen"] = datetime.now().isoformat(timespec="seconds")

        # --- "Regal listed it" alert: only when it showed up somewhere but tickets did not open yet ---
        if homepage_code and not st["listed"]:
            st["listed"] = True
            st["code"] = homepage_code
            st["title"] = mv["title"]
            if not tickets_opened_now:
                msg = (f"Regal has added \"{mv['title']}\" to its {mv['feed']} board. "
                       f"Tickets are not open at your theatre yet; I'll keep watching.")
                log(msg)
                if announce:
                    notify_all(config, f"Regal listed: {mv['title']}", msg,
                               f"{BASE}/movies/{slugify(mv['title'])}-{homepage_code}")
        if not homepage_code and not any(v.get("on_sale") or v.get("title") for v in summary.get(key_movie, {}).values()):
            log(f"'{key_movie}': not on Regal's board yet (homepage feeds: {len(all_movies(page))} titles; "
                f"theatre boards: {sum(len(b.get('movies', [])) for b in boards.values())} titles)")
            summary.setdefault(key_movie, {})["listed"] = False

    state["_last_run"] = datetime.now().isoformat(timespec="seconds")
    if announce:  # --status is a dry run: don't remember anything, so the real run still alerts
        save_json(STATE_PATH, state)
    return summary


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(CONFIG_PATH))
    ap.add_argument("--loop", action="store_true", help="poll forever instead of once")
    ap.add_argument("--interval", type=int, help="minutes between polls in --loop mode")
    ap.add_argument("--find-theatre", metavar="QUERY")
    ap.add_argument("--find-movie", metavar="QUERY")
    ap.add_argument("--test-notify", action="store_true")
    ap.add_argument("--status", action="store_true", help="check but do not send alerts or open the browser")
    ap.add_argument("--reset", action="store_true", help="forget previous alerts (re-alert on next run)")
    args = ap.parse_args()

    if args.find_theatre or args.find_movie:
        page = fetch_next_data()
        if args.find_theatre:
            hits = find_theatres(page, args.find_theatre)
            print(f"{len(hits)} theatre(s) match '{args.find_theatre}':")
            for t in hits:
                print(f"  {t['theatre_code']}  {t['name']}  - {t['address']}, {t['city']} {t['state']} {t['postal_code']}")
        if args.find_movie:
            hits = find_movies(page, args.find_movie, fuzzy=True)
            print(f"{len(hits)} movie(s) match '{args.find_movie}' (Regal lists {len(all_movies(page))} titles right now):")
            for m in hits:
                print(f"  {m['code']}  {m['title']}   [{m['feed']}]")
        return 0

    config = load_json(Path(args.config), None)
    if config is None:
        print(f"No config at {args.config}. Copy config.example.json to config.json and edit it.")
        return 2

    if args.reset and STATE_PATH.exists():
        STATE_PATH.unlink()
        print("state cleared")

    if args.test_notify:
        notify_all(config, "Regal Watch test", "If you can read this, alerts work. Tap to open Regal.", BASE)
        return 0

    interval = args.interval or int(config.get("poll_minutes", 10))
    while True:
        try:
            summary = check_once(config, announce=not args.status)
            if args.status:
                print(json.dumps(summary, indent=2, default=str))
        except Exception as e:  # noqa: BLE001
            log(f"ERROR during check: {e!r}")
        if not args.loop:
            return 0
        time.sleep(interval * 60)


if __name__ == "__main__":
    sys.exit(main())
