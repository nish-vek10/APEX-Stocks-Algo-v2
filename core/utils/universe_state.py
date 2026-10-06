# core/utils/universe_state.py
"""
Dynamic universe resolution (market-cap eligibility, refreshed daily).

tools/refresh_universe.py writes state/universe_active.json from Finviz:
tickers currently passing USA + market cap >= $300M + REIT exclusion.

Rules:
  - NEW ENTRIES / SIGNALS: only tickers in the eligible set. A ticker whose
    cap falls below $300M is ignored until it re-qualifies.
  - OPEN POSITIONS: always kept in the data set (cache refresh + Stage-9 exit
    monitoring) even if ineligible. Never orphan a held position.
  - Stale/missing active file (> MAX_AGE_DAYS) -> fall back to the static
    production.yaml universe.tickers list.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple

logger = logging.getLogger("universe_state")

MAX_AGE_DAYS = 10


def active_file(root: Path) -> Path:
    return Path(root) / "state" / "universe_active.json"


def load_active(root: Path) -> Optional[Dict[str, Any]]:
    f = active_file(root)
    if not f.exists():
        return None
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        asof = datetime.fromisoformat(d["asof_utc"])
        age = (datetime.now(timezone.utc) - asof).total_seconds() / 86400.0
        if age > MAX_AGE_DAYS:
            logger.warning("universe_active.json is %.1f days old (> %d) -- using static yaml list.", age, MAX_AGE_DAYS)
            return None
        return d
    except Exception as exc:
        logger.warning("universe_active.json unreadable (%s) -- using static yaml list.", exc)
        return None


def held_tickers(root: Path) -> Set[str]:
    f = Path(root) / "state" / "positions.json"
    if not f.exists():
        return set()
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        items = d.values() if isinstance(d, dict) else d
        return {
            str(p["ticker"]).strip().upper()
            for p in items
            if isinstance(p, dict) and p.get("status", "open") == "open" and p.get("ticker")
        }
    except Exception:
        return set()


def resolve_universe(prod_cfg: Dict[str, Any], root: Path) -> Tuple[Set[str], Set[str]]:
    """
    Returns (eligible, data_set).
      eligible : tickers allowed to generate NEW signals/entries
      data_set : eligible + currently-held (cache + exit monitoring)
    """
    ucfg = prod_cfg.get("universe", {})
    excluded = {str(t).strip().upper() for t in ucfg.get("excluded_tickers", [])}
    active = load_active(root)
    if active is not None:
        base = {str(t).strip().upper() for t in active.get("eligible", [])}
    else:
        base = {str(t).strip().upper() for t in ucfg.get("tickers", [])}
    eligible = base - excluded
    data_set = eligible | held_tickers(root)
    return eligible, data_set
