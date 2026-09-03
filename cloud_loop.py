#!/usr/bin/env python3
"""
Cloud runner for GitHub Actions: polls both chains every few minutes for the
job's lifetime and keeps alert state in the repo so overlapping jobs and future
jobs never send the same alert twice.

Env:
  LOOP_MINUTES     how long this job keeps polling (default 50)
  POLL_MINUTES     minutes between polls (default 4)
  NTFY_TOPIC, NTFY_EMAIL, SMTP_USER, SMTP_PASSWORD, EMAIL_TO  override config.json
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

PT = timezone(timedelta(hours=-7))  # Pacific Daylight Time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import regal_watch as rw
import cinemark_watch as cw

DIGEST_PATH = HERE / "state_digest.json"
STATE_FILES = [rw.STATE_PATH, cw.STATE_PATH, DIGEST_PATH]


def digest_text(config: dict, regal: dict, cinemark: dict) -> str:
    """One-screen status of every watched movie on both chains."""
    lines = []
    for movie in config.get("movies", []):
        r = regal.get(movie, {})
        lines.append(f"{movie}")
        # --- Regal ---
        regal_lines = []
        for key, v in r.items():
            if not isinstance(v, dict):
                continue
            if v.get("on_sale"):
                regal_lines.append(f"Regal: TICKETS ON SALE, {v.get('showtimes', '?')} showtimes, first {v.get('dates', ['?'])[0]}")
            elif v.get("title"):
                regal_lines.append(f"Regal: on the board as \"{v['title']}\", no showtimes yet")
            elif v.get("listed"):
                regal_lines.append("Regal: listed on regmovies.com, not at your theatre yet")
        lines += regal_lines or ["Regal Hacienda Crossings: not listed yet, checking every 4 min"]
        # --- Cinemark ---
        c = cinemark.get(movie, {})
        if not c or c.get("listed") is False:
            lines.append("Cinemark: not listed yet")
        else:
            th = c.get("theatres", {})
            on = [n for n, t in th.items() if t.get("on_sale")]
            sched = [n for n, t in th.items() if t.get("showtimes")]
            if on:
                lines.append(f"Cinemark: TICKETS ON SALE at {len(on)} theatre(s) near {config.get('cinemark', {}).get('zip')}")
            elif sched:
                lines.append(f"Cinemark: scheduled at {len(sched)} theatre(s), tickets open {c.get('on_sale_date')}, release {c.get('release')}")
            else:
                lines.append(f"Cinemark: listed, release {c.get('release')}, tickets open {c.get('on_sale_date')}")
    lines.append(f"Checked {datetime.now(timezone.utc).astimezone(PT):%a %b %d %I:%M %p} PT")
    return "\n".join(lines)


def maybe_send_digest(config: dict, regal: dict, cinemark: dict) -> None:
    every = float(config.get("digest_every_minutes", 0) or 0)
    if every <= 0:
        return
    st = rw.load_json(DIGEST_PATH, {})
    last = st.get("last_sent")
    if last and (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() < every * 60 - 90:
        return
    body = digest_text(config, regal, cinemark)
    n = dict(config.get("notify", {}).get("ntfy", {}))
    n["priority"] = "low"  # shows in the tray without buzzing
    try:
        rw.notify_ntfy(n, "Hourly update: The Paradise", body, None)
        st["last_sent"] = datetime.now(timezone.utc).isoformat()
        rw.save_json(DIGEST_PATH, st)
        rw.log("digest sent:\n" + body)
    except Exception as e:  # noqa: BLE001
        rw.log(f"digest failed: {e}")


def sh(*args, check=True):
    return subprocess.run(args, cwd=HERE, check=check, capture_output=True, text=True)


def apply_env_overrides(config: dict) -> dict:
    n = config.setdefault("notify", {})
    n["windows_toast"] = False
    if os.environ.get("NTFY_TOPIC"):
        n.setdefault("ntfy", {})["topic"] = os.environ["NTFY_TOPIC"]
    if os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASSWORD"):
        e = n.setdefault("email", {})
        e["smtp_user"] = os.environ["SMTP_USER"]
        e["smtp_password"] = os.environ["SMTP_PASSWORD"]
        e["to"] = os.environ.get("EMAIL_TO") or e.get("to") or os.environ["SMTP_USER"]
    config["open_browser_when_tickets_open"] = False  # no browser in the cloud
    return config


def snapshot() -> dict:
    """State content minus the run timestamp, so we only commit when an alert actually happened."""
    out = {}
    for p in STATE_FILES:
        data = rw.load_json(p, None)
        if isinstance(data, dict):
            data = {k: v for k, v in data.items() if k != "_last_run"}
        out[p.name] = json.dumps(data, sort_keys=True)
    return out


def sync_state_from_git() -> None:
    """Pick up alerts another (overlapping) job may have sent since our last poll."""
    try:
        sh("git", "fetch", "origin", "main", check=False)
        for p in STATE_FILES:
            sh("git", "checkout", "origin/main", "--", p.name, check=False)  # missing file is fine
    except Exception as e:  # noqa: BLE001
        rw.log(f"state sync skipped: {e}")


def push_state() -> None:
    for attempt in range(4):
        sh("git", "add", *[p.name for p in STATE_FILES])
        r = sh("git", "commit", "-m", "watch: update alert state [skip ci]", check=False)
        if r.returncode != 0 and "nothing to commit" in (r.stdout + r.stderr):
            return
        r = sh("git", "push", check=False)
        if r.returncode == 0:
            rw.log("state pushed")
            return
        rw.log(f"push failed (attempt {attempt + 1}), rebasing: {r.stderr.strip()[:160]}")
        sh("git", "pull", "--rebase", "-X", "theirs", check=False)
    rw.log("WARNING: could not push state; a duplicate alert is possible")


def main() -> int:
    loop_minutes = float(os.environ.get("LOOP_MINUTES", "50"))
    poll_minutes = float(os.environ.get("POLL_MINUTES", "4"))
    # config.json (with your topic) stays on your PC and out of git; the public
    # repo carries config.cloud.json and the secrets come from GitHub Secrets.
    cfg_path = rw.CONFIG_PATH if rw.CONFIG_PATH.exists() else HERE / "config.cloud.json"
    config = apply_env_overrides(rw.load_json(cfg_path, {}))
    rw.log(f"cloud loop: {loop_minutes:.0f} min, poll every {poll_minutes:.0f} min, config {cfg_path.name}")
    sh("git", "config", "user.name", "regal-watch-bot", check=False)
    sh("git", "config", "user.email", "regal-watch-bot@users.noreply.github.com", check=False)

    deadline = time.time() + loop_minutes * 60
    while True:
        sync_state_from_git()
        before = snapshot()
        summaries = {}
        for name, mod in (("regal", rw), ("cinemark", cw)):
            try:
                summaries[name] = mod.check_once(config, announce=True) or {}
            except Exception as e:  # noqa: BLE001
                rw.log(f"{name} ERROR during check: {e!r}")
                summaries[name] = {}
        maybe_send_digest(config, summaries.get("regal", {}), summaries.get("cinemark", {}))
        if snapshot() != before:
            push_state()
        if time.time() + poll_minutes * 60 > deadline:
            break
        time.sleep(poll_minutes * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
