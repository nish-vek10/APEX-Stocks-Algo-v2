# prod/positions/position_manager.py
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

logger = logging.getLogger("position_manager")


class PositionManager:
    """
    Manages open position state — persists to state/positions.json.
    Tracks entry, stop, shares, R-multiple.
    Source of truth for exit decision logic.
    """

    def __init__(self, state_dir: Path) -> None:
        self._file = Path(state_dir) / "positions.json"
        self._positions: Dict[str, Dict[str, Any]] = self._load()

    def _load(self) -> Dict[str, Dict[str, Any]]:
        if self._file.exists():
            try:
                return json.loads(self._file.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save(self) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        self._file.write_text(
            json.dumps(self._positions, indent=2, default=str), encoding="utf-8"
        )

    def open_position(
        self,
        ticker: str,
        mt5_ticket: int,
        entry_price: float,
        stop_price: float,
        shares: float,
        signal: Dict[str, Any],
        mt5_symbol: str,
        mt5_volume: Optional[float] = None,
    ) -> None:
        """
        `shares`: underlying share-equivalent exposure -- used for $ P&L math
        on close (see close_position). For IG this equals the order size
        directly. For MT5 this is volume * contract_size (see
        order_builder.resolve_mt5_volume), NOT the raw MT5 lot count.

        `mt5_volume`: the actual MT5 lot volume sent to order_send, only set
        in MT5 mode. Needed separately from `shares` because closing an MT5
        position requires the broker's lot volume, not the share-equivalent
        figure. None for IG positions.
        """
        self._positions[ticker] = {
            "ticker": ticker,
            "mt5_symbol": mt5_symbol,
            "mt5_ticket": mt5_ticket,
            "entry_price": entry_price,
            "stop_price": stop_price,
            "shares": shares,
            "mt5_volume": mt5_volume,
            "stop_distance": entry_price - stop_price,
            "signal_date": str(signal.get("signal_date", "")),
            "entry_date": str(pd.Timestamp.now().date()),
            "days_held": 0,
            "status": "open",
        }
        self._save()
        logger.info(
            f"POSITION OPENED: {ticker} | ticket={mt5_ticket} | "
            f"entry={entry_price:.2f} | stop={stop_price:.2f} | shares={shares}"
            + (f" | mt5_volume={mt5_volume}" if mt5_volume is not None else "")
        )

    def close_position(
        self,
        ticker: str,
        exit_price: float,
        exit_reason: str,
    ) -> Optional[Dict[str, Any]]:
        pos = self._positions.get(ticker)
        if pos is None:
            logger.warning(f"close_position: {ticker} not in open positions.")
            return None

        stop_dist = pos.get("stop_distance", 0)
        pnl_per_share = exit_price - pos["entry_price"]
        pnl_total = pnl_per_share * pos["shares"]
        pnl_r = pnl_per_share / stop_dist if stop_dist > 0 else 0.0

        trade = {**pos, "exit_price": exit_price, "exit_reason": exit_reason,
                 "pnl_per_share": pnl_per_share, "pnl_total": pnl_total,
                 "pnl_r": pnl_r, "status": "closed"}

        del self._positions[ticker]
        self._save()

        logger.info(
            f"POSITION CLOSED: {ticker} | exit={exit_price:.2f} | "
            f"pnl={pnl_total:.2f} | R={pnl_r:.2f}R | reason={exit_reason}"
        )
        return trade

    def update_days_held(self) -> None:
        for ticker in self._positions:
            self._positions[ticker]["days_held"] = (
                self._positions[ticker].get("days_held", 0) + 1
            )
        self._save()

    def get_open(self) -> List[Dict[str, Any]]:
        return list(self._positions.values())

    def get_position(self, ticker: str) -> Optional[Dict[str, Any]]:
        return self._positions.get(ticker)

    def is_open(self, ticker: str) -> bool:
        return ticker in self._positions

    def count(self) -> int:
        return len(self._positions)
