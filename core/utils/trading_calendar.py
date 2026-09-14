# core/utils/trading_calendar.py
"""
Single source of truth for the NYSE holiday calendar and trading-day math.

Previously this list was duplicated inline in tools/build_td_cache.py for
the cache-freshness check. Centralised here 2026-09-14 so orchestrator.py's
signal-staleness gate (see _is_signal_stale) uses the EXACT same calendar --
two independent hardcoded copies drifting apart silently would reintroduce
the same class of bug as the day-after-Labor-Day cache-staleness incident
(2026-09-08/09), just applied to trade entries instead of cache refreshes.

Extend this list yearly. `pd.bdate_range()` alone only excludes weekends,
never exchange holidays -- always diff against NYSE_HOLIDAYS on top of it.
"""
from __future__ import annotations

import pandas as pd

NYSE_HOLIDAYS = pd.to_datetime([
    # 2025
    "2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17", "2025-04-18",
    "2025-05-26", "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27",
    "2025-12-25",
    # 2026
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    # 2027
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
])


def is_trading_day(d: "pd.Timestamp | str") -> bool:
    """True if `d` is a NYSE trading day (not a weekend, not a holiday)."""
    ts = pd.Timestamp(d).normalize()
    if ts.dayofweek >= 5:  # Sat=5, Sun=6
        return False
    return ts not in NYSE_HOLIDAYS


def next_trading_day(d: "pd.Timestamp | str") -> pd.Timestamp:
    """
    Return the next NYSE trading day strictly after `d`.
    Used to compute a signal's ONLY valid entry date: a signal generated off
    day T's close is valid for execution at T's next trading day open, and
    nowhere else -- see orchestrator.py::_is_signal_stale.
    """
    ts = pd.Timestamp(d).normalize()
    nxt = ts + pd.Timedelta(days=1)
    while not is_trading_day(nxt):
        nxt += pd.Timedelta(days=1)
    return nxt


def trading_days_between(start: "pd.Timestamp | str", end: "pd.Timestamp | str") -> int:
    """
    Count NYSE trading days strictly between `start` (exclusive) and `end`
    (inclusive). Returns 0 if end <= start.
    """
    start_ts = pd.Timestamp(start).normalize()
    end_ts = pd.Timestamp(end).normalize()
    if end_ts <= start_ts:
        return 0
    business_days = pd.bdate_range(start=start_ts + pd.Timedelta(days=1), end=end_ts)
    return len(business_days.difference(NYSE_HOLIDAYS))
