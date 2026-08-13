# core/filters/spider_gate.py
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd


class SpiderGate:
    """
    Macro permission layer — wraps spider_gate_daily.parquet.
    Returns allow/block decision + risk multiplier for a given ticker/date.
    """

    def __init__(self, prod_cfg: Dict[str, Any], root: Path) -> None:
        self.enabled: bool = prod_cfg.get("spider_gate", {}).get("enabled", True)
        self._gate_df: Optional[pd.DataFrame] = None

        if self.enabled:
            gate_file = prod_cfg.get("spider_gate", {}).get("file", "")
            gate_path = root / gate_file if gate_file else None
            if gate_path and gate_path.exists():
                self._gate_df = pd.read_parquet(gate_path)
                self._gate_df["date"] = pd.to_datetime(self._gate_df["date"]).dt.normalize()
            else:
                import logging
                logging.getLogger("SpiderGate").warning(
                    f"Spider gate enabled but file not found: {gate_path}. Defaulting to ALLOW."
                )

    def check(
        self,
        spider_id: str,
        check_date: Any,
        spider_cfg: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Returns dict: {allowed: bool, reason: str, risk_mult: float}
        spider_id: sector spider ID (e.g. "XLK")
        check_date: date or datetime
        spider_cfg: spiders.yaml content
        """
        if not self.enabled:
            return {"allowed": True, "reason": "gate_disabled", "risk_mult": 1.0}

        if self._gate_df is None:
            return {"allowed": True, "reason": "gate_file_missing", "risk_mult": 1.0}

        ts = pd.Timestamp(check_date).normalize()
        mask = (self._gate_df["spider_id"] == spider_id) & (self._gate_df["date"] == ts)
        rows = self._gate_df[mask]

        if rows.empty:
            return {"allowed": True, "reason": "no_gate_data", "risk_mult": 1.0}

        row = rows.iloc[-1]
        allowed = bool(row.get("allowed", True))
        reason = str(row.get("reason", ""))
        risk_mult = float(row.get("risk_mult", 1.0))

        gate_rules = spider_cfg.get("gate_rules", {})
        allowed_stages = gate_rules.get("allowed_stages", [3, 4, 5, 6, 7])
        stage = int(row.get("sector_stage", 0))
        if stage not in allowed_stages:
            allowed = False
            reason = f"spider_stage_{stage}_blocked"

        return {"allowed": allowed, "reason": reason, "risk_mult": risk_mult}

    def portfolio_check(
        self,
        sector_spider_ids: list,
        check_date: Any,
        spider_cfg: Dict[str, Any],
        require_all: bool = False,
    ) -> Dict[str, Any]:
        """
        Aggregate gate check across multiple spiders.
        require_all=False: allow if ANY spider is green.
        require_all=True: allow only if ALL spiders are green.
        """
        results = {
            sid: self.check(sid, check_date, spider_cfg)
            for sid in sector_spider_ids
        }
        any_allowed = any(r["allowed"] for r in results.values())
        all_allowed = all(r["allowed"] for r in results.values())
        avg_mult = (
            sum(r["risk_mult"] for r in results.values()) / len(results)
            if results else 1.0
        )

        if require_all:
            allowed = all_allowed
        else:
            allowed = any_allowed

        return {
            "allowed": allowed,
            "risk_mult": avg_mult,
            "detail": results,
        }
