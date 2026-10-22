"""Reads the GA work calendar from Google Calendar's secret iCal link and works out who is on shift.

Calendar convention: each shift is an event whose title contains the GA's name or one of the
aliases in team.json, e.g. "Riya - Front Desk" or "Desk: Riya, Aman". Recurring events work."""
import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from .config import GA, load_team, settings

log = logging.getLogger("virtual_ga.schedule")
TZ = ZoneInfo(settings.TIMEZONE)

_cache: dict = {"at": 0.0, "cal": None, "error": None}


def _calendar():
    import icalendar
    if not settings.GCAL_ICS_URL:
        return None
    fresh = time.time() - _cache["at"] < settings.SCHEDULE_CACHE_MINUTES * 60
    if fresh and _cache["cal"] is not None:
        return _cache["cal"]
    try:
        if settings.GCAL_ICS_URL.startswith("file://"):  # handy for testing
            raw = open(settings.GCAL_ICS_URL[7:], "rb").read()
        else:
            r = requests.get(settings.GCAL_ICS_URL, timeout=10)
            r.raise_for_status()
            raw = r.content
        _cache.update(at=time.time(), cal=icalendar.Calendar.from_ical(raw), error=None)
    except Exception as e:
        log.warning("Could not load calendar: %s", e)
        _cache["error"] = str(e)  # keep the last good copy if there is one
    return _cache["cal"]


def shifts_between(start: datetime, end: datetime) -> list[dict]:
    import recurring_ical_events
    cal = _calendar()
    if cal is None:
        return []
    team = load_team()
    out = []
    for ev in recurring_ical_events.of(cal).between(start, end):
        title = str(ev.get("SUMMARY", ""))
        s, e = ev.decoded("DTSTART"), ev.decoded("DTEND") if ev.get("DTEND") else None
        if not isinstance(s, datetime):  # all-day events are not shifts
            continue
        s = s.astimezone(TZ) if s.tzinfo else s.replace(tzinfo=TZ)
        e = (e.astimezone(TZ) if e.tzinfo else e.replace(tzinfo=TZ)) if isinstance(e, datetime) else s + timedelta(hours=1)
        gas = [g for g in team if g.matches(title)]
        out.append({"title": title, "start": s, "end": e, "gas": gas})
    return sorted(out, key=lambda x: x["start"])


def on_shift_now(now: datetime | None = None) -> list[GA]:
    now = now or datetime.now(TZ)
    seen, result = set(), []
    for sh in shifts_between(now, now + timedelta(minutes=1)):
        for g in sh["gas"]:
            if g.name not in seen:
                seen.add(g.name)
                result.append(g)
    return result


def next_shift(now: datetime | None = None) -> dict | None:
    now = now or datetime.now(TZ)
    upcoming = [s for s in shifts_between(now, now + timedelta(days=7)) if s["gas"] and s["start"] > now]
    return upcoming[0] if upcoming else None


def current_shift_end(now: datetime | None = None) -> datetime | None:
    """Latest end time among shifts covering `now`, for a message ticket's deadline."""
    now = now or datetime.now(TZ)
    active = [s for s in shifts_between(now, now + timedelta(minutes=1)) if s["gas"]]
    return max((s["end"] for s in active), default=None)


def status() -> dict:
    """For the admin page: is the calendar connected and what does today look like."""
    now = datetime.now(TZ)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today = shifts_between(start, start + timedelta(days=1))
    return {
        "connected": bool(settings.GCAL_ICS_URL) and _cache["cal"] is not None,
        "error": _cache["error"],
        "on_shift_now": [g.name for g in on_shift_now(now)],
        "today": [
            {"title": s["title"], "start": s["start"].strftime("%I:%M %p"), "end": s["end"].strftime("%I:%M %p"),
             "matched_gas": [g.name for g in s["gas"]]}
            for s in today
        ],
    }
