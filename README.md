# Regal Watch

A small personal agent that watches Regal Cinemas for a movie and alerts you
the moment:

1. **Regal lists the movie** on its board (Coming Soon / Now Playing), and
2. **Tickets open at your theatre** (showtimes appear and are bookable), and
3. (optional) **new showtimes get added** later, e.g. more days or IMAX.

The "tickets open" alert includes direct seat-selection links for the best
showtimes (by your format / day / time preferences) and can open the booking
page in your browser automatically. You then pick seats and pay, which takes
about 20 seconds if you are already signed in to Regal.

It talks to the same public JSON endpoint the regmovies.com showtime grid uses,
so there is no login and nothing to break when the page layout changes.

## Setup (5 minutes)

```bash
cd regal-watch
python -m pip install requests curl_cffi
copy config.example.json config.json
```

Edit `config.json`:

- `movies`: titles to watch, e.g. `"Avengers: Doomsday"`. Check the match with
  `python regal_watch.py --find-movie avengers`.
- `theatres`: your theatre name / city / zip, e.g. `"Regal Aviation Mall"`.
  Check with `python regal_watch.py --find-theatre aviation`.
- `notify`: turn on at least one channel (see below).

Try it:

```bash
python regal_watch.py --test-notify    # make sure alerts reach you
python regal_watch.py --status         # what it sees right now, no alerts
python regal_watch.py                  # a real check; alerts if something changed
```

Then install it as a background job that runs every 10 minutes:

```bash
powershell -ExecutionPolicy Bypass -File .\setup_task.ps1
```

Everything it does is written to `regal_watch.log`. Alerts already sent are
remembered in `state.json`; delete it (or run `--reset`) to re-alert.

## Cinemark too

`cinemark_watch.py` does the same for Cinemark theatres near a zip code and
shares the config, alerts and log. Add to `config.json`:

```json
"cinemark": { "zip": "94588", "max_miles": 20, "theatres": [] }
```

It alerts three times per movie: when Cinemark lists it (with the official
on-sale date the movie page reveals), when showtimes get scheduled at nearby
theatres, and when those showtimes become bookable. `watch_all.py` runs both
watchers and is what the scheduled task executes.

```bash
python cinemark_watch.py --find-movie "paradise"
python cinemark_watch.py --status
```

## Running in the cloud (laptop off)

The repo ships a GitHub Actions workflow (`.github/workflows/watch.yml`) that
starts a 50-minute polling job every 30 minutes, so two jobs overlap and there
is never a gap. Each job polls every 4 minutes, so detection is under 5 minutes.
Public repos get unlimited free Actions minutes.

- `config.cloud.json` is the public config (no secrets).
- Add repository secrets: `NTFY_TOPIC` (required), and optionally `SMTP_USER`,
  `SMTP_PASSWORD`, `EMAIL_TO` for Gmail alerts.
- Alert state is committed back to the repo (`state*.json`) so jobs never
  repeat an alert.
- Regal's JSON API blocks datacenter IPs; the watcher automatically falls back
  to reading the theatre page, which carries the same board data. Cloud alerts
  therefore link to the theatre page for each date rather than to individual
  showtimes.
- Manual test: Actions tab, "Regal + Cinemark watch", Run workflow, set
  loop_minutes to 1.

## Price comparison

Every tickets-open alert includes the adult price incl. fee and a comparison:

- Regal: read live from Regal's ticket-type endpoint when the JSON API is
  reachable (your PC); in the cloud it falls back to the live price of a current
  standard show, or `regal_reference_adult_price` from the config.
- Cinemark: read from the public seat-map page of the best showtime.
- If Cinemark is cheaper the alert title starts with "CHEAPER THAN REGAL".
- AMC: amctheatres.com puts every automated visitor into a queue-it waiting
  room, so AMC cannot be checked automatically. Alerts include a manual AMC link
  (`amc_manual_link`).

## Hourly status push and Regal Unlimited

- `digest_every_minutes: 60` makes the cloud loop send a low-priority ntfy
  push once an hour summarising both chains (listed / scheduled / on sale /
  on-sale date). It never emails; email is reserved for real events.
- `regal_unlimited: true` (+ `regal_unlimited_members`, `regal_unlimited_fee`)
  tells the price logic that Regal costs you only the online fee per ticket,
  so Cinemark is never flagged as cheaper; alerts state the real comparison.

## Email without a password

In the cloud, `notify.github_issue.enabled` makes an alert open an issue on the
repo from the Actions bot. GitHub then emails the repo owner with the alert text
and the booking link. No SMTP credentials needed. Close the issue afterwards or
leave it; it is only a mail carrier.

## Notification channels

| Channel | Setup | Reaches your phone? |
|---|---|---|
| Windows toast | nothing, on by default | no (desktop popup) |
| **ntfy** (recommended) | install the ntfy app, subscribe to a topic name you invent, put it in `notify.ntfy.topic` | yes, free, no account |
| Email | Gmail: Google Account > Security > 2-Step Verification > App passwords, create one, put it in `notify.email.smtp_password` with your Gmail in `smtp_user` | yes |
| Telegram | create a bot with @BotFather, put `bot_token` + your `chat_id` | yes |

Pick a long random ntfy topic name (e.g. `regal-chetan-x7k2q9`) because topics
are public.

## Preferences

```json
"preferences": {
  "preferred_formats": ["IMAX", "RPX", "Dolby", "2D"],
  "avoid_formats": ["3D"],
  "preferred_days": ["Thu", "Fri", "Sat"],
  "preferred_hours": [17, 22]
}
```

These only rank which showtimes appear first in the alert and which one the
browser opens. Every showtime is still available on the theatre page.

## About "auto-book"

The agent gets you to the seat map instantly, but it stops before payment on
purpose. Completing a purchase needs your Regal login and card, and a script
that does that runs against Regal's terms and their Cloudflare bot checks, so
it would break at the worst possible moment. One tap on the alert and you are
on the seat picker, which is the fast part anyway.
