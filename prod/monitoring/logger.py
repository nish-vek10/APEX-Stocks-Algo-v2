# prod/monitoring/logger.py
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


class RunLogger:
    """
    Structured JSONL run logger — one line per event.
    Writes to logs/run_YYYYMMDD.jsonl.
    """

    def __init__(self, log_dir: Path) -> None:
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        self._file = self._log_dir / f"run_{date_str}.jsonl"

    def _write(self, event_type: str, data: Dict[str, Any]) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event_type,
            **data,
        }
        with self._file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")

    def log_signal(self, signal: Dict[str, Any]) -> None:
        self._write("signal", {"ticker": signal.get("ticker"), "stage": signal.get("stage"),
                                "signal_date": str(signal.get("signal_date"))})

    def log_order_sent(self, ticker: str, result: Dict[str, Any]) -> None:
        # Full breadth of sizing/risk fields is written when present (MT5
        # path attaches sl/mt5_volume/actual_shares/target+actual risk
        # dollars/deviation_pct -- see orchestrator._execute_entry_mt5) so
        # managers/debugging can see exactly what was sent and why, without
        # cross-referencing multiple log lines. `reason`/`comment` captures
        # broker rejection detail for failed orders.
        self._write("order_sent", {
            "ticker": ticker,
            "retcode": result.get("retcode"),
            "success": result.get("success"),
            "volume": result.get("volume"),
            "price": result.get("price"),
            "sl": result.get("sl"),
            "reason": result.get("reason"),
            "comment": result.get("comment"),
            "mt5_volume": result.get("mt5_volume"),
            "actual_shares": result.get("actual_shares"),
            "target_risk_dollars": result.get("target_risk_dollars"),
            "actual_risk_dollars": result.get("actual_risk_dollars"),
            "deviation_pct": result.get("deviation_pct"),
        })

    def log_position_open(self, ticker: str, entry: float, stop: float, shares: float) -> None:
        self._write("position_open", {"ticker": ticker, "entry": entry,
                                       "stop": stop, "shares": shares})

    def log_position_close(self, ticker: str, exit_price: float,
                            pnl: float, pnl_r: float, reason: str) -> None:
        self._write("position_close", {"ticker": ticker, "exit": exit_price,
                                        "pnl": pnl, "pnl_r": pnl_r, "reason": reason})

    def log_gate_decision(self, ticker: str, allowed: bool, reason: str, mult: float) -> None:
        self._write("gate_decision", {"ticker": ticker, "allowed": allowed,
                                       "reason": reason, "risk_mult": mult})

    def log_circuit_breaker(self, reason: str) -> None:
        self._write("circuit_breaker", {"reason": reason})

    def log_error(self, ticker: str, error: str) -> None:
        self._write("error", {"ticker": ticker, "error": error})

    def log_run_summary(self, summary: Dict[str, Any]) -> None:
        self._write("run_summary", summary)
