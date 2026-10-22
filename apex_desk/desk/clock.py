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


def status(now: Optional[datetime] = None) -> Dict:
    now = now or now_utc()
    ses = active_session(now)
    cut, opn = cutoff_dt(ses), open_dt(ses)
    secs = (cut - now).total_seconds()
    lon = london()
    return {
        "now_utc": now.isoformat(),
        "now_london": now.astimezone(lon).strftime("%a %d %b %Y %H:%M:%S"),
        "now_et": now.astimezone(ET).strftime("%a %d %b %Y %H:%M:%S"),
        "session_date": str(ses.date()),
        "open_utc": opn.astimezone(timezone.utc).isoformat(),
        "open_london": opn.astimezone(lon).strftime("%a %d %b %H:%M"),
        "cutoff_utc": cut.astimezone(timezone.utc).isoformat(),
        "cutoff_london": cut.astimezone(lon).strftime("%a %d %b %H:%M"),
        "seconds_to_cutoff": int(secs),
        "window_open": secs > 0,
    }


def window_open(entry_date, now: Optional[datetime] = None) -> bool:
    now = now or now_utc()
    return now < cutoff_dt(entry_date)
