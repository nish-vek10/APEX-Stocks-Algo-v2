# prod/positions/portfolio_tracker.py
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("portfolio_tracker")


class PortfolioTracker:
    """
    Tracks trade history + daily P&L journal.
    Persists to state/trade_log.json.
    Used for reporting, circuit breaker, and R-multiple tracking.
    """

    def __init__(self, state_dir: Path) -> None:
        self._file = Path(state_dir) / "trade_log.json"
        self._log: List[Dict[str, Any]] = self._load()

    def _load(self) -> List[Dict[str, Any]]:
        if self._file.exists():
            try:
                return json.loads(self._file.read_text(encoding="utf-8"))
            except Exception:
                return []
        return []

    def _save(self) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        self._file.write_text(
            json.dumps(self._log, indent=2, default=str), encoding="utf-8"
        )

    def record(self, trade: Dict[str, Any]) -> None:
        self._log.append(trade)
        self._save()
        logger.info(
            f"TRADE LOGGED: {trade.get('ticker')} | "
            f"pnl={trade.get('pnl_total', 0):.2f} | "
            f"R={trade.get('pnl_r', 0):.2f}"
        )

    def get_all(self) -> List[Dict[str, Any]]:
        return list(self._log)

    def summary(self) -> Dict[str, Any]:
        if not self._log:
            return {"trades": 0, "win_rate": 0.0, "avg_r": 0.0, "total_pnl": 0.0}

        wins = [t for t in self._log if t.get("pnl_total", 0) > 0]
        total_pnl = sum(t.get("pnl_total", 0) for t in self._log)
        avg_r = sum(t.get("pnl_r", 0) for t in self._log) / len(self._log)

        return {
            "trades": len(self._log),
            "wins": len(wins),
            "win_rate": round(len(wins) / len(self._log), 4),
            "avg_r": round(avg_r, 4),
            "total_pnl": round(total_pnl, 2),
        }
