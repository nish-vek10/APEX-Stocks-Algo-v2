# apex_desk/desk/clock.py
"""Session / cut-off maths. Everything is derived from the NYSE open (09:30 ET), so UK/US DST mismatch weeks are automatic."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Dict, Optional
from zoneinfo import ZoneInfo

import pandas as pd

from desk import settings
import core.utils.trading_calendar as tc  # noqa: E402  (repo root added to sys.path by settings)

ET = ZoneInfo("America/New_York")


def london() -> ZoneInfo:
    return ZoneInfo(settings.cfg()["desk"]["display_tz"])


def now_utc() -> datetime:
    """DESK_FAKE_NOW (ISO, UTC) overrides the clock for demos/tests only."""
    import os
    f = os.environ.get("DESK_FAKE_NOW")
    if f:
        return datetime.fromisoformat(f).astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def open_dt(entry_date) -> datetime:
    d = pd.Timestamp(entry_date).date()
    return datetime.combine(d, time(9, 30), tzinfo=ET)


def cutoff_dt(entry_date) -> datetime:
    mins = int(settings.cfg()["timing"]["cutoff_minutes_before_open"])
    return open_dt(entry_date) - timedelta(minutes=mins)


def entry_date_for(signal_date) -> pd.Timestamp:
    """A signal formed at day T's close can only be entered at T's next trading-day open."""
    return tc.next_trading_day(signal_date)


def active_session(now: Optional[datetime] = None) -> pd.Timestamp:
    """The next US session whose open is still in the future (today if before 09:30 ET on a trading day)."""
    now = now or now_utc()
    now_et = now.astimezone(ET)
    d = pd.Timestamp(now_et.date())
    if tc.is_trading_day(d) and now_et < open_dt(d):
        return d
    return tc.next_trading_day(d)


def close_dt(entry_date) -> datetime:
    d = pd.Timestamp(entry_date).date()
    return datetime.combine(d, time(16, 0), tzinfo=ET)          # regular close; early-close days are not modelled


def scan_dt(d) -> datetime:
    hh, mm = map(int, settings.cfg()["timing"]["scan_time_et"].split(":"))
    return datetime.combine(pd.Timestamp(d).date(), time(hh, mm), tzinfo=ET)


def prev_trading_day(d) -> pd.Timestamp:
    d = pd.Timestamp(d).normalize() - pd.Timedelta(days=1)
    while not tc.is_trading_day(d):
        d -= pd.Timedelta(days=1)
    return d


def review_open_dt(entry_date) -> datetime:
    """The decision window for a session opens when the previous trading day's scan publishes the new batch."""
    return scan_dt(prev_trading_day(entry_date))


def next_batch_dt(now: Optional[datetime] = None) -> datetime:
    now = now or now_utc()
    d = pd.Timestamp(now.astimezone(ET).date())
    for _ in range(14):
        if tc.is_trading_day(d) and scan_dt(d) > now:
            return scan_dt(d)
        d += pd.Timedelta(days=1)
    return scan_dt(d)


def expected_signal_date(now: Optional[datetime] = None) -> pd.Timestamp:
    """Latest trading day whose end-of-day data is due: its day's scan time (17:20 ET) has passed. Candles newer than this cannot exist yet
    (e.g. on Monday during market hours the expected signal date is still Friday), so older data is only 'late' after this time."""
    now = now or now_utc()
    d = pd.Timestamp(now.astimezone(ET).date())
    for _ in range(14):
        if tc.is_trading_day(d) and scan_dt(d) <= now:
            return d
        d -= pd.Timedelta(days=1)
    return d


def data_late(have_signal_date: Optional[str], now: Optional[datetime] = None) -> Dict:
    """Is the latest published batch older than expected, by more than `late_grace_min` after the scan time? (drives the dashboard banner)"""
    now = now or now_utc()
    exp = expected_signal_date(now)
    grace = int(settings.cfg()["timing"].get("late_grace_min", 45))
    late = bool(have_signal_date) and pd.Timestamp(have_signal_date) < exp and now >= scan_dt(exp) + timedelta(minutes=grace)
    return {"late": late, "expected": str(exp.date()), "have": have_signal_date}


def market(now: Optional[datetime] = None) -> Dict:
    now = now or now_utc()
    d = pd.Timestamp(now.astimezone(ET).date())
    if tc.is_trading_day(d):
        o, c = open_dt(d), close_dt(d)
        if o <= now < c:
            return {"is_open": True, "open_utc": o.astimezone(timezone.utc).isoformat(), "close_utc": c.astimezone(timezone.utc).isoformat()}
        if now < o:
            return {"is_open": False, "next_open_utc": o.astimezone(timezone.utc).isoformat()}
    nd = tc.next_trading_day(d)
    return {"is_open": False, "next_open_utc": open_dt(nd).astimezone(timezone.utc).isoformat()}


def review_started(entry_date, now: Optional[datetime] = None) -> bool:
    return (now or now_utc()) >= review_open_dt(entry_date)


def status(now: Optional[datetime] = None) -> Dict:
    now = now or now_utc()
    ses = active_session(now)
    cut, opn = cutoff_dt(ses), open_dt(ses)
    rev = review_open_dt(ses)
    secs = (cut - now).total_seconds()
    lon = london()
    f = "%a %d-%m-%Y %H:%M"
    return {
        "now_utc": now.isoformat(),
        "now_london": now.astimezone(lon).strftime(f + ":%S"),
        "now_et": now.astimezone(ET).strftime(f + ":%S"),
        "session_date": str(ses.date()),
        "open_utc": opn.astimezone(timezone.utc).isoformat(),
        "open_london": opn.astimezone(lon).strftime(f),
        "cutoff_utc": cut.astimezone(timezone.utc).isoformat(),
        "cutoff_london": cut.astimezone(lon).strftime(f),
        "review_open_utc": rev.astimezone(timezone.utc).isoformat(),
        "next_batch_utc": next_batch_dt(now).astimezone(timezone.utc).isoformat(),
        "market": market(now),
        "seconds_to_cutoff": int(secs),
        "window_open": secs > 0 and now >= rev,
    }


def window_open(entry_date, now: Optional[datetime] = None) -> bool:
    """Decisions are allowed from the batch publication until the cut-off."""
    now = now or now_utc()
    return review_open_dt(entry_date) <= now < cutoff_dt(entry_date)
