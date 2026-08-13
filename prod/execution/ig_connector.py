# prod/execution/ig_connector.py
"""
IG Group REST API connector.
Manages session lifecycle: login → token refresh → logout.
Replaces MT5Connector for IG-based execution.

Requires:  trading_ig>=0.0.11  (pip install trading_ig)
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("ig_connector")

try:
    from trading_ig import IGService
    IG_AVAILABLE = True
except ImportError:
    IG_AVAILABLE = False
    logger.warning("trading_ig package not installed — running in stub mode. pip install trading_ig")


class IGConnector:
    """
    Manages IG REST API session lifecycle.

    Context manager usage:
        with IGConnector(credentials) as conn:
            equity = conn.get_equity()

    Credentials dict keys:
        identifier   : IG username (env: IG_IDENTIFIER)
        password     : IG password (env: IG_PASSWORD)
        api_key      : IG API key  (env: IG_API_KEY)
        acc_number   : IG account ID (env: IG_ACCOUNT_ID)
        acc_type     : "LIVE" | "DEMO"  (env: IG_ACC_TYPE)
    """

    def __init__(self, credentials: Dict[str, Any]) -> None:
        self._creds = credentials
        self._service: Optional[Any] = None
        self._connected: bool = False

    # ── Connection ─────────────────────────────────────────────────────────────
    def connect(self) -> None:
        if not IG_AVAILABLE:
            raise RuntimeError(
                "trading_ig not installed. Run: pip install trading_ig"
            )

        acc_type = self._creds.get("acc_type", "DEMO").upper()

        self._service = IGService(
            username=self._creds["identifier"],
            password=self._creds["password"],
            api_key=self._creds["api_key"],
            acc_type=acc_type,
            acc_number=self._creds.get("acc_number", ""),
        )

        self._service.create_session(version="3")
        self._connected = True

        info = self._get_account_row()
        if info is not None:
            balance_val = info.get("balance", "?")
            if isinstance(balance_val, dict):
                balance_val = balance_val.get("value", "?")
            logger.info(
                f"IG connected -- account={self._creds.get('acc_number')} "
                f"type={acc_type} "
                f"balance={balance_val}"
            )
        else:
            logger.info(f"IG connected -- account={self._creds.get('acc_number')} type={acc_type}")

    def disconnect(self) -> None:
        if self._service and self._connected:
            try:
                self._service.logout()
            except Exception as exc:
                logger.warning(f"IG logout warning: {exc}")
            self._connected = False
            self._service = None
            logger.info("IG disconnected.")

    # ── Account Info ───────────────────────────────────────────────────────────
    def _get_account_row(self) -> Optional[Dict[str, Any]]:
        """Fetch accounts list and return the active account row as dict."""
        if not self._service:
            return None
        try:
            accounts = self._service.fetch_accounts()
            # accounts is a DataFrame; find preferred account
            acc_id = self._creds.get("acc_number", "")
            if not accounts.empty:
                if acc_id and "accountId" in accounts.columns:
                    match = accounts[accounts["accountId"] == acc_id]
                    if not match.empty:
                        return match.iloc[0].to_dict()
                return accounts.iloc[0].to_dict()
        except Exception as exc:
            logger.warning(f"fetch_accounts error: {exc}")
        return None

    def get_equity(self) -> float:
        """Return current account equity (balance value)."""
        row = self._get_account_row()
        if row is None:
            return 0.0
        try:
            # trading_ig returns balance as nested dict: {'balance': {'value': X, ...}}
            bal = row.get("balance", {})
            if isinstance(bal, dict):
                return float(bal.get("value", 0.0))
            # Some versions return flat columns: 'balance_value'
            for key in ("balance_value", "equity", "balance"):
                if key in row:
                    v = row[key]
                    if isinstance(v, (int, float)):
                        return float(v)
        except Exception as exc:
            logger.warning(f"get_equity parse error: {exc}")
        return 0.0

    def get_balance(self) -> float:
        """Return available to deal (cash available)."""
        row = self._get_account_row()
        if row is None:
            return 0.0
        try:
            bal = row.get("balance", {})
            if isinstance(bal, dict):
                return float(bal.get("available", 0.0))
            # trading_ig v0.0.24: balance is flat float; check available column separately
            for key in ("available", "deposit", "balance"):
                if key in row:
                    v = row[key]
                    if isinstance(v, (int, float)):
                        return float(v)
        except Exception as exc:
            logger.warning(f"get_balance parse error: {exc}")
        return 0.0

    def get_live_price(self, epic: str) -> Dict[str, float]:
        """
        Fetch live bid/ask snapshot for an epic.
        Returns: {bid: float, ask: float, mid: float}
        """
        if not self._service:
            return {"bid": 0.0, "ask": 0.0, "mid": 0.0}
        try:
            market = self._service.fetch_market_by_epic(epic)
            snap = market.get("snapshot", {})
            bid = float(snap.get("bid", 0.0) or 0.0)
            ask = float(snap.get("offer", 0.0) or 0.0)
            mid = (bid + ask) / 2 if (bid > 0 and ask > 0) else (bid or ask)
            return {"bid": bid, "ask": ask, "mid": mid}
        except Exception as exc:
            logger.warning(f"get_live_price({epic}) error: {exc}")
            return {"bid": 0.0, "ask": 0.0, "mid": 0.0}

    # ── Service Access ─────────────────────────────────────────────────────────
    @property
    def service(self) -> Optional[Any]:
        """Direct access to IGService for advanced callers."""
        return self._service

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ── Context Manager ────────────────────────────────────────────────────────
    def __enter__(self) -> "IGConnector":
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.disconnect()
