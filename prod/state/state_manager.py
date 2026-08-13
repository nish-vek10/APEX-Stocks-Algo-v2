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

    def mark_execution_complete(self) -> None:
        self._state["last_execution_run"] = _now()
        self._state["run_count"] = self._state.get("run_count", 0) + 1
        self._save()

    def get_state(self) -> Dict[str, Any]:
        return dict(self._state)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
