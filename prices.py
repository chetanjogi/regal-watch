"""
Ticket price lookup and cross-chain comparison (Regal vs Cinemark).

Regal:    /api/getTicketsForSession?theatreCode=..&vistaSession=<PerformanceId>
          returns ticket types with PriceInCents + booking fee, no login needed.
          Cloudflare blocks that API from datacenter IPs, so in the cloud we fall
          back to the reference price in config ("regal_reference_adult_price").
Cinemark: the public seat-map page lists every ticket type with price + fee.
AMC:      amctheatres.com puts every automated visitor into a queue-it waiting
          room, so AMC cannot be checked by this tool. The alert carries a manual
          AMC link instead.
"""
from __future__ import annotations

import json
import re

import regal_watch as rw
from regal_watch import log


def regal_adult_price(theatre_code: str, perf_id, config: dict) -> tuple[float | None, str]:
    """(total price for one adult incl. fee, source) for a Regal performance."""
    try:
        raw = rw.regal_get(f"{rw.BASE}/api/getTicketsForSession",
                           {"theatreCode": theatre_code, "vistaSession": perf_id, "cartId": "", "sessionToken": "false"})
        data = json.loads(raw)
        for t in data.get("Tickets", []):
            if re.search(r"child|senior|student|military|member", t.get("Description", ""), re.I):
                continue
            fee = (t.get("TicketBookingFee") or {}).get("TotalFeeInCents") or 0
            return round((t["PriceInCents"] + fee) / 100, 2), "live"
    except Exception as e:  # noqa: BLE001
        log(f"regal price lookup unavailable ({str(e)[:80]}); using reference price")
    ref = config.get("regal_reference_adult_price")
    return (float(ref), "usual") if ref else (None, "unknown")


def regal_reference(config: dict) -> tuple[float | None, str]:
    """Regal price to compare against when The Paradise is not on sale at Regal yet:
    the live price of any current show at the same theatre if reachable, else config."""
    try:
        code = str(config.get("theatres", ["0347"])[0])
        code = code if code.isdigit() else "0347"
        from datetime import date
        board = rw.get_showtimes(code, date.today())
        premium = re.compile(r"IMAX|RPX|4DX|ScreenX|3D|Dolby|VIP", re.I)
        for show in board.get("shows", []):
            for film in show.get("Film", []):
                for p in film.get("Performances", []):
                    attrs = " ".join(p.get("PerformanceAttributes", []))
                    if not p.get("StopSales") and p.get("PerformanceId") and not premium.search(attrs):
                        price, src = regal_adult_price(code, p["PerformanceId"], config)
                        if src == "live":
                            return price, "live, current shows"
                        raise RuntimeError("api blocked")
    except Exception:  # noqa: BLE001
        pass
    ref = config.get("regal_reference_adult_price")
    return (float(ref), "usual") if ref else (None, "unknown")


def cinemark_reference(config: dict) -> float | None:
    """Cinemark's price for The Paradise if it is already on sale (from its state file)."""
    try:
        import cinemark_watch as cw
        st = rw.load_json(cw.STATE_PATH, {})
        for v in st.values():
            if isinstance(v, dict) and v.get("adult_price"):
                return float(v["adult_price"])
            if isinstance(v, dict) and v.get("links"):
                return cinemark_adult_price(v["links"][0])
    except Exception:  # noqa: BLE001
        pass
    return None


def cinemark_adult_price(seatmap_url: str) -> float | None:
    import cinemark_watch as cw
    try:
        return cw.adult_price(cw.ticket_prices(seatmap_url))
    except Exception as e:  # noqa: BLE001
        log(f"cinemark price lookup failed: {e}")
        return None


def comparison_lines(regal: tuple[float | None, str], cinemark: float | None, config: dict) -> list[str]:
    """Human lines for an alert, e.g. 'Cinemark $17.74 vs Regal $20.99 (usual): Cinemark is $3.25 cheaper'."""
    lines = []
    rp, rsrc = regal
    if config.get("regal_unlimited"):
        fee = float(config.get("regal_unlimited_fee", 0.50))
        n = int(config.get("regal_unlimited_members", 1))
        lines.append(f"You have Regal Unlimited: Regal costs only the ~${fee:.2f} online fee per ticket "
                     f"(x{n} passes) instead of ${rp:.2f}." if rp else
                     f"You have Regal Unlimited: Regal costs only the ~${fee:.2f} online fee per ticket (x{n} passes).")
        if cinemark is not None:
            lines.append(f"Cinemark would be ${cinemark:.2f} per adult, so Regal is far cheaper for you if it gets the movie.")
        amc = config.get("amc_manual_link")
        if amc:
            lines.append(f"AMC blocks automated checks; look manually: {amc}")
        return lines
    if cinemark is not None and rp is not None:
        diff = round(rp - cinemark, 2)
        tag = "" if rsrc == "live" else f" ({rsrc} price)"
        if diff > 0:
            lines.append(f"Cinemark ${cinemark:.2f} vs Regal ${rp:.2f}{tag}: Cinemark is ${diff:.2f} cheaper per adult")
        elif diff < 0:
            lines.append(f"Regal ${rp:.2f}{tag} vs Cinemark ${cinemark:.2f}: Regal is ${-diff:.2f} cheaper per adult")
        else:
            lines.append(f"Same price at both: ${rp:.2f} per adult")
    elif cinemark is not None:
        lines.append(f"Cinemark adult ticket: ${cinemark:.2f} incl. fee")
    elif rp is not None:
        lines.append(f"Regal adult ticket: ${rp:.2f} incl. fee ({rsrc})")
    amc = config.get("amc_manual_link")
    if amc:
        lines.append(f"AMC blocks automated checks; look manually: {amc}")
    return lines


def cinemark_cheaper(regal: tuple[float | None, str], cinemark: float | None, config: dict | None = None) -> bool:
    if config and config.get("regal_unlimited"):
        return False  # Unlimited: Regal is ~$0.50 a ticket, nothing beats that
    rp, _ = regal
    return cinemark is not None and rp is not None and cinemark < rp
