#!/usr/bin/env python3
"""
Cinemark Watch - sibling of regal_watch.py for Cinemark theatres near a zip code.

Cinemark's pages are server-rendered HTML, and the movie page reveals the
official on-sale date. A showtime that is scheduled but not yet on sale is
rendered as <p class="presale">; once tickets open it becomes a
<a class="showtime-link" href="/TicketSeatMap/?...">. That flip is the moment
this watcher is looking for.

Usage (shares config.json, alerts and log with regal_watch.py):
  python cinemark_watch.py            # one check
  python cinemark_watch.py --status   # dry run, no alerts, no state saved
  python cinemark_watch.py --find-movie "paradise"
"""
from __future__ import annotations

import argparse
import html as htmllib
import json
import re
import sys
import time
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path

import regal_watch as rw
from regal_watch import log, norm, regal_get as http_get, load_json, save_json, notify_all

HERE = Path(__file__).resolve().parent
STATE_PATH = HERE / "state_cinemark.json"
CM = "https://www.cinemark.com"
SEARCH_API = CM + "/umbraco/surface/showtimes/GetByMovieIdWithSearch"


# ----------------------------------------------------------------------------
# Cinemark data access
# ----------------------------------------------------------------------------
def all_movies() -> dict[str, str]:
    """slug -> title, from the Coming Soon and Now Playing listings (server-rendered)."""
    out: dict[str, str] = {}
    for page in ("coming-soon", "now-playing"):
        h = http_get(f"{CM}/movies/{page}")
        for slug, title in re.findall(r'href="/movies/([a-z0-9-]+)"[^>]*>\s*([^<]{2,120}?)\s*</a>', h):
            if slug in ("coming-soon", "now-playing", "featured", "cinearts", "cinemark-xd"):
                continue
            out.setdefault(slug, htmllib.unescape(title.strip()))
        time.sleep(1.0)
    return out


def find_movies(query: str, movies: dict[str, str]) -> list[tuple[str, str]]:
    q = norm(query)
    exact = [(s, t) for s, t in movies.items() if norm(t) == q]
    if exact:
        return exact
    return [(s, t) for s, t in movies.items() if re.search(rf"\b{re.escape(q)}\b", norm(t))]


def movie_details(slug: str) -> dict:
    h = http_get(f"{CM}/movies/{slug}")

    def var(name: str) -> str | None:
        m = re.search(rf'var\s+{name}\s*=\s*"?([^";]*)"?;', h)
        return m.group(1).strip() if m else None

    rel = re.search(r'<label>Release Date</label>\s*<p>\s*([A-Za-z]+ \d{1,2}, \d{4})', h)
    release = datetime.strptime(rel.group(1), "%B %d, %Y").date() if rel else None
    onsale_raw = var("publicOnSaleDate")
    onsale = None
    if onsale_raw:
        try:
            onsale = datetime.strptime(onsale_raw, "%m/%d/%Y %I:%M:%S %p")
        except ValueError:
            pass
    return {"slug": slug, "movie_id": var("currentMovieId"), "title": var("movieTitle"),
            "release": release, "onsale": onsale, "url": f"{CM}/movies/{slug}"}


def theatres_for(movie_id: str, zip_code: str, day: date) -> list[dict]:
    """Nearby theatres showing the movie on `day`, each with its showtimes."""
    h = http_get(SEARCH_API, {"cinemarkMovieId": movie_id, "searchText": zip_code,
                              "showDate": day.isoformat(), "allTheaters": "false"})
    out = []
    for block in re.split(r'<div class="theatreBlock\b', h)[1:]:
        name = re.search(r'<span class="trunc">([^<]+)</span>', block)
        slug = re.search(r'href="(/theatres/[^"]+)"', block)
        miles = re.search(r'([\d.]+)\s*miles', block)
        shows = []
        for st in re.finditer(r'<div class="showtime"[^>]*data-print-type-name="([^"]*)"[^>]*>(.*?)</div>', block, re.S):
            fmt, inner = st.group(1), st.group(2)
            link = re.search(r'href="([^"]*TicketSeatMap[^"]*)"', inner)
            t = re.sub(r"<[^>]+>", " ", inner)
            t = re.sub(r"\s+", " ", htmllib.unescape(t)).strip()
            tm = re.search(r"\d{1,2}:\d{2}\s*[ap]m", t, re.I)
            shows.append({
                "time": tm.group(0).lower().replace(" ", "") if tm else t[:10],
                "format": fmt,
                "on_sale": link is not None,
                "link": CM + htmllib.unescape(link.group(1)) if link else None,
                "date": day.isoformat(),
            })
        out.append({
            "name": htmllib.unescape(name.group(1)).strip() if name else "?",
            "url": CM + slug.group(1) if slug else CM,
            "miles": float(miles.group(1)) if miles else 9999.0,
            "showtimes": shows,
        })
    return out


# ----------------------------------------------------------------------------
# the check
# ----------------------------------------------------------------------------
def check_once(config: dict, announce: bool = True) -> dict:
    cm = config.get("cinemark", {})
    zip_code = str(cm.get("zip", "")).strip()
    if not zip_code:
        log("cinemark: no zip in config, skipping")
        return {}
    max_miles = float(cm.get("max_miles", 20))
    wanted = [norm(t) for t in cm.get("theatres", [])]  # optional name filter
    prefs = config.get("preferences", {})
    state = load_json(STATE_PATH, {})
    summary: dict = {}

    listing = all_movies()
    for query in cm.get("movies") or config.get("movies", []):
        st = state.setdefault(query, {"listed": False, "scheduled": False, "on_sale": False, "links": []})
        hits = find_movies(query, listing)
        if not hits:
            log(f"cinemark: '{query}' not on Cinemark's board yet ({len(listing)} titles listed)")
            summary[query] = {"listed": False}
            continue

        slug, title = hits[0]
        d = movie_details(slug)
        onsale_txt = f"{d['onsale']:%a %b %d %I:%M %p}" if d["onsale"] else "not announced"
        if not st["listed"]:
            st["listed"] = True
            msg = (f"Cinemark lists \"{d['title'] or title}\". Release {d['release'] or '?'}; "
                   f"tickets go on sale {onsale_txt}. I'll keep watching theatres near {zip_code}.")
            log(msg)
            if announce:
                notify_all(config, f"Cinemark listed: {d['title'] or title}", msg, d["url"])

        # Which days to look at: release day and its eve (premieres), plus a couple after.
        days = []
        if d["release"]:
            days = [d["release"] + timedelta(days=k) for k in (-1, 0, 1, 2)]
        days = [x for x in days if x >= date.today()] or [date.today()]

        theatres: dict[str, dict] = {}
        for day in days:
            for th in theatres_for(d["movie_id"], zip_code, day):
                if th["miles"] > max_miles:
                    continue
                if wanted and not any(w in norm(th["name"]) for w in wanted):
                    continue
                agg = theatres.setdefault(th["name"], dict(th, showtimes=[]))
                agg["showtimes"] += th["showtimes"]
            time.sleep(1.5)

        for t in theatres.values():  # dedupe and put showtimes in chronological order
            seen = {}
            for s in t["showtimes"]:
                seen.setdefault((s["date"], s["time"]), s)  # same slot in two formats = one line
            t["showtimes"] = sorted(seen.values(), key=lambda s: (s["date"], hour_of(s["time"])))

        scheduled = {n: t for n, t in theatres.items() if t["showtimes"]}
        on_sale = {n: t for n, t in theatres.items() if any(s["on_sale"] for s in t["showtimes"])}
        summary[query] = {
            "title": d["title"] or title, "release": str(d["release"]), "on_sale_date": onsale_txt,
            "theatres": {n: {"miles": t["miles"], "showtimes": len(t["showtimes"]),
                             "on_sale": any(s["on_sale"] for s in t["showtimes"])} for n, t in theatres.items()},
        }

        if scheduled and not st["scheduled"]:
            st["scheduled"] = True
            lines = [f"\"{d['title'] or title}\" is scheduled at {len(scheduled)} Cinemark theatre(s) near {zip_code}, "
                     f"tickets open {onsale_txt}:"]
            for n, t in sorted(scheduled.items(), key=lambda kv: kv[1]["miles"]):
                lines.append(f"- {n} ({t['miles']} mi): " + ", ".join(f"{s['date'][5:]} {s['time']}" for s in t["showtimes"][:6]))
            body = "\n".join(lines)
            log(body)
            if announce:
                notify_all(config, f"Cinemark scheduled: {d['title'] or title}", body, d["url"])

        if on_sale:
            all_links = sorted({s["link"] for t in on_sale.values() for s in t["showtimes"] if s["link"]})
            new_links = [l for l in all_links if l not in st["links"]]
            ranked = []
            for n, t in sorted(on_sale.items(), key=lambda kv: kv[1]["miles"]):
                for s in t["showtimes"]:
                    if s["on_sale"]:
                        ranked.append((score(s, t, prefs), n, t, s))
            ranked.sort(key=lambda x: -x[0])
            best = ranked[0] if ranked else None
            best_link = best[3]["link"] if best else d["url"]
            if not st["on_sale"]:
                st["on_sale"] = True
                lines = [f"Tickets are OPEN for \"{d['title'] or title}\" at Cinemark near {zip_code}!", ""]
                for _, n, t, s in ranked[:6]:
                    lines.append(f"- {n} ({t['miles']} mi) {s['date']} {s['time']}  {s['link']}")
                body = "\n".join(lines)
                log(body)
                if announce:
                    notify_all(config, f"TICKETS OPEN (Cinemark): {d['title'] or title}", body, best_link)
                    if config.get("open_browser_when_tickets_open", True):
                        webbrowser.open(best_link)
                        log(f"opened browser at {best_link}")
            elif new_links and config.get("alert_on_new_showtimes", True):
                body = f"{len(new_links)} new Cinemark showtimes for \"{d['title'] or title}\":\n" + "\n".join(
                    f"- {n} {s['date']} {s['time']}  {s['link']}" for _, n, t, s in ranked if s["link"] in new_links)
                log(body)
                if announce:
                    notify_all(config, f"New Cinemark showtimes: {d['title'] or title}", body, best_link)
            else:
                log(f"cinemark: '{d['title'] or title}' on sale, nothing new ({len(all_links)} showtimes)")
            st["links"] = all_links
        elif scheduled:
            log(f"cinemark: '{d['title'] or title}' scheduled at {len(scheduled)} theatre(s) but not on sale yet "
                f"(opens {onsale_txt})")
        else:
            log(f"cinemark: '{d['title'] or title}' listed, no showtimes near {zip_code} yet (opens {onsale_txt})")

    state["_last_run"] = datetime.now().isoformat(timespec="seconds")
    if announce:
        save_json(STATE_PATH, state)
    return summary


def ticket_prices(seatmap_url: str) -> list[dict]:
    """Ticket types and prices for one showtime, read from the (public) seat-map page.
    Returns e.g. [{'type': 'Adult', 'price': 15.75, 'fee': 1.99}, ...]."""
    t = http_get(seatmap_url)
    rows = []
    for m in re.finditer(r'<span class="fontbold">([^<]+)</span>.{0,1200}?data-ticket-list-price="([\d.]+)"'
                         r'.{0,400}?CalculatedListPrice[^>]*>\s*\$([\d.]+)\s*\+\s*\$([\d.]+)\s*Fee', t, re.S):
        rows.append({"type": htmllib.unescape(m.group(1)).strip(), "price": float(m.group(2)),
                     "fee": float(m.group(4))})
    return rows


def adult_price(rows: list[dict]) -> float | None:
    """Best guess at the regular adult price: first type that isn't child/senior/etc."""
    for r in rows:
        if not re.search(r"child|senior|student|military|member|club", r["type"], re.I):
            return round(r["price"] + r["fee"], 2)
    return round(rows[0]["price"] + rows[0]["fee"], 2) if rows else None


def hour_of(t: str) -> float:
    m = re.match(r"(\d{1,2}):(\d{2})(am|pm)", t)
    if not m:
        return 99.0
    return int(m.group(1)) % 12 + (12 if m.group(3) == "pm" else 0) + int(m.group(2)) / 60


def score(s: dict, t: dict, prefs: dict) -> float:
    v = -t["miles"] * 2
    fmt = s["format"].lower()
    for i, f in enumerate(prefs.get("preferred_formats", [])):
        if f.lower() in fmt:
            v += 100 - i * 10
    for f in prefs.get("avoid_formats", []):
        if f.lower() in fmt:
            v -= 200
    m = re.match(r"(\d{1,2}):(\d{2})(am|pm)", s["time"])
    if m:
        hour = int(m.group(1)) % 12 + (12 if m.group(3) == "pm" else 0) + int(m.group(2)) / 60
        lo, hi = prefs.get("preferred_hours", [17, 22])
        v += 30 if lo <= hour <= hi else -abs(hour - (lo + hi) / 2)
    days = [x.lower()[:3] for x in prefs.get("preferred_days", [])]
    if days and datetime.strptime(s["date"], "%Y-%m-%d").strftime("%a").lower() in days:
        v += 50
    return v


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(rw.CONFIG_PATH))
    ap.add_argument("--status", action="store_true", help="dry run: no alerts, no state saved")
    ap.add_argument("--find-movie", metavar="QUERY")
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()

    if args.find_movie:
        listing = all_movies()
        hits = find_movies(args.find_movie, listing)
        print(f"{len(hits)} match(es) among {len(listing)} Cinemark titles:")
        for slug, title in hits:
            d = movie_details(slug)
            print(f"  {title}  | id {d['movie_id']} | release {d['release']} | on sale {d['onsale']} | {d['url']}")
        return 0

    config = load_json(Path(args.config), None)
    if config is None:
        print(f"No config at {args.config}")
        return 2
    if args.reset and STATE_PATH.exists():
        STATE_PATH.unlink()
    try:
        summary = check_once(config, announce=not args.status)
        if args.status:
            print(json.dumps(summary, indent=2, default=str))
    except Exception as e:  # noqa: BLE001
        log(f"cinemark ERROR during check: {e!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
