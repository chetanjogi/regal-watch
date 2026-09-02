"""Diagnostic: which Regal fetch strategies work from this machine/network?"""
import json
import re
import sys

from curl_cffi import requests as cr

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
API = "https://www.regmovies.com/api/getShowtimes?theatres=0347&date=2026-09-02&hoCode=&ignoreCache=false&moviesOnly=false"
THEATRE = "https://www.regmovies.com/theatres/regal-hacienda-crossings-0347"


def show(label, r):
    body = r.text
    nd = '__NEXT_DATA__' in body
    keys = ""
    if nd:
        m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', body, re.S)
        try:
            pp = json.loads(m.group(1))["props"]["pageProps"]
            keys = ",".join(list(pp.keys())[:8])
            fs = pp.get("futureShows")
            keys += f" | futureShows={len(fs) if isinstance(fs, list) else fs} movies={len(pp.get('movies') or [])} showtimes={str(pp.get('showtimes'))[:60]}"
        except Exception as e:  # noqa: BLE001
            keys = f"parse err {e}"
    print(f"{label:38s} HTTP {r.status_code} len {len(body):7d} cf={r.headers.get('cf-mitigated')} next_data={nd} {keys}")


for imp in ("chrome", "chrome131", "safari", "firefox"):
    try:
        s = cr.Session(impersonate=imp)
        show(f"API {imp} bare", s.get(API, timeout=30))
    except Exception as e:  # noqa: BLE001
        print(imp, "err", e)

s = cr.Session(impersonate="chrome")
home = s.get("https://www.regmovies.com/", timeout=30)
show("home chrome", home)
show("API after home (cookies)", s.get(API, timeout=30, headers={
    "Referer": THEATRE, "Accept": "application/json, text/plain, */*",
    "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}))
show("theatre page", s.get(THEATRE, timeout=30))
show("theatre page ?date", s.get(THEATRE + "?date=09-04-2026", timeout=30))
show("movie page", s.get("https://www.regmovies.com/movies/avengers-doomsday-HO00012935", timeout=30))
sys.exit(0)
