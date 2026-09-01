# prod/state/state_manager.py
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("state_manager")


class StateManager:
    """
    Master state file for the production run.
    Tracks: last_run_date, pending_signals, pending_orders, run_mode.
    Persists to state/run_state.json.
    """

    def __init__(self, state_dir: Path) -> None:
        self._file = Path(state_dir) / "run_state.json"
        self._state = self._load()

    def _load(self) -> Dict[str, Any]:
        if self._file.exists():
            try:
                return json.loads(self._file.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {
            "last_signal_run": None,
            "last_execution_run": None,
            "pending_signals": [],
            "pending_orders": [],
            "pending_stage9_exits": [],
            "fired_signals": [],   # ["TICKER|YYYY-MM-DD", ...] -- see has_fired_signal()
            "run_count": 0,
        }

    def _save(self) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        self._file.write_text(
            json.dumps(self._state, indent=2, default=str), encoding="utf-8"
        )

    def set_pending_signals(self, signals: List[Dict[str, Any]]) -> None:
        self._state["pending_signals"] = signals
        self._state["last_signal_run"] = _now()
        self._save()
        logger.info(f"State: {len(signals)} pending signals saved.")

    def get_pending_signals(self) -> List[Dict[str, Any]]:
        return self._state.get("pending_signals", [])

    def clear_pending_signals(self) -> None:
        self._state["pending_signals"] = []
        self._save()

    def set_pending_orders(self, orders: List[Dict[str, Any]]) -> None:
        self._state["pending_orders"] = orders
        self._save()

    def get_pending_orders(self) -> List[Dict[str, Any]]:
        return self._state.get("pending_orders", [])

    def clear_pending_orders(self) -> None:
        self._state["pending_orders"] = []
        self._save()

    def set_pending_stage9_exits(self, tickers: List[str]) -> None:
        self._state["pending_stage9_exits"] = tickers
        self._save()

    def get_pending_stage9_exits(self) -> List[str]:
        return self._state.get("pending_stage9_exits", [])

    def clear_pending_stage9_exits(self) -> None:
        self._state["pending_stage9_exits"] = []
        self._save()

    def has_fired_signal(self, ticker: str, signal_date: str) -> bool:
        """
        True if (ticker, signal_date) has already been queued as a signal
        before -- prevents the same Stage 6/7 transition from being
        re-detected and re-traded on every re-run against an unchanged
        cache. SignalGenerator.generate() only ever inspects the LAST row
        of whatever data it's given, with zero memory of what it already
        signaled; as long as the TwelveData cache's last bar doesn't
        change, re-running --mode full/execution (a scheduler retry, a
        misfire re-trigger, or a manual re-run) would otherwise re-detect
        and re-enter the identical breakout repeatedly. Found 2026-09-01:
        DOCU (and others) opened, stopped out, then immediately re-opened
        within the same test session because nothing remembered its
        2026-08-31 signal had already fired.
        """
        key = f"{ticker}|{signal_date}"
        return key in set(self._state.get("fired_signals", []))

    def record_fired_signals(self, signals: List[Dict[str, Any]]) -> None:
        """Persist (ticker, signal_date) pairs as fired -- called right after
        set_pending_signals() so a later re-run can't replay the same day's
        transitions. Ledger is capped to the most recent 5000 entries to
        avoid unbounded growth over the life of the deployment."""
        existing = self._state.get("fired_signals", [])
        for s in signals:
            key = f"{s['ticker']}|{_date_str(s['signal_date'])}"
            if key not in existing:
                existing.append(key)
        self._state["fired_signals"] = existing[-5000:]
        self._save()

    def mark_execution_complete(self) -> None:
        self._state["last_execution_run"] = _now()
        self._state["run_count"] = self._state.get("run_count", 0) + 1
        self._save()

    def get_state(self) -> Dict[str, Any]:
        return dict(self._state)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _date_str(d: Any) -> str:
    """Normalize a signal_date (pd.Timestamp, date, or str) to YYYY-MM-DD."""
    s = str(d)
    return s[:10]
