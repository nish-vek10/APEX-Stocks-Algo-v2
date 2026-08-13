# prod/risk/circuit_breaker.py
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger("circuit_breaker")


class CircuitBreaker:
    """
    Monitors daily/weekly drawdown + consecutive losses.
    Halts trading if thresholds breached.
    State persisted to state/circuit_breaker.json.
    """

    def __init__(self, prod_cfg: Dict[str, Any], state_dir: Path) -> None:
        cb = prod_cfg.get("circuit_breakers", {})
        self.enabled = bool(cb.get("enabled", True))
        self.daily_halt_pct = float(cb.get("daily_drawdown_halt_pct", 0.03))
        self.weekly_halt_pct = float(cb.get("weekly_drawdown_halt_pct", 0.07))
        self.consec_loss_halt = int(cb.get("consecutive_loss_halt", 5))
        self._state_file = Path(state_dir) / "circuit_breaker.json"
        self._state = self._load()

    def _load(self) -> Dict[str, Any]:
        if self._state_file.exists():
            try:
                return json.loads(self._state_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {
            "halted": False,
            "halt_reason": "",
            "daily_start_equity": None,
            "weekly_start_equity": None,
            "week_start_date": None,
            "day_start_date": None,
            "consecutive_losses": 0,
            "trade_results": [],
        }

    def _save(self) -> None:
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        self._state_file.write_text(
            json.dumps(self._state, indent=2, default=str), encoding="utf-8"
        )

    def check(self, current_equity: float, today: date) -> Dict[str, Any]:
        """
        Run circuit breaker checks. Returns {allowed: bool, reason: str}.
        """
        if not self.enabled:
            return {"allowed": True, "reason": "circuit_breaker_disabled"}

        if self._state.get("halted"):
            return {"allowed": False, "reason": self._state.get("halt_reason", "halted")}

        today_str = str(today)
        week_start = _week_start(today)
        week_str = str(week_start)

        # Init daily baseline
        if self._state.get("day_start_date") != today_str:
            self._state["daily_start_equity"] = current_equity
            self._state["day_start_date"] = today_str
            self._save()

        # Init weekly baseline
        if self._state.get("week_start_date") != week_str:
            self._state["weekly_start_equity"] = current_equity
            self._state["week_start_date"] = week_str
            self._save()

        # Daily drawdown check
        daily_start = self._state.get("daily_start_equity") or current_equity
        daily_dd = (daily_start - current_equity) / daily_start if daily_start > 0 else 0
        if daily_dd >= self.daily_halt_pct:
            self._halt(f"daily_drawdown_{daily_dd:.2%}")
            return {"allowed": False, "reason": self._state["halt_reason"]}

        # Weekly drawdown check
        weekly_start = self._state.get("weekly_start_equity") or current_equity
        weekly_dd = (weekly_start - current_equity) / weekly_start if weekly_start > 0 else 0
        if weekly_dd >= self.weekly_halt_pct:
            self._halt(f"weekly_drawdown_{weekly_dd:.2%}")
            return {"allowed": False, "reason": self._state["halt_reason"]}

        # Consecutive losses
        if self._state.get("consecutive_losses", 0) >= self.consec_loss_halt:
            self._halt(f"consecutive_losses_{self._state['consecutive_losses']}")
            return {"allowed": False, "reason": self._state["halt_reason"]}

        return {"allowed": True, "reason": "ok"}

    def record_trade_result(self, pnl: float) -> None:
        """Record trade P&L and update consecutive loss counter."""
        if pnl < 0:
            self._state["consecutive_losses"] = self._state.get("consecutive_losses", 0) + 1
        else:
            self._state["consecutive_losses"] = 0
        self._state.setdefault("trade_results", []).append(pnl)
        self._save()

    def reset_halt(self) -> None:
        """Manually reset circuit breaker (e.g., after review)."""
        self._state["halted"] = False
        self._state["halt_reason"] = ""
        self._state["consecutive_losses"] = 0
        self._save()
        logger.info("Circuit breaker reset manually.")

    def _halt(self, reason: str) -> None:
        self._state["halted"] = True
        self._state["halt_reason"] = reason
        self._save()
        logger.critical(f"CIRCUIT BREAKER TRIPPED: {reason}")


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())
