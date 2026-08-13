# prod/execution/mt5_connector.py
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("mt5_connector")

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False
    logger.warning("MetaTrader5 package not installed — running in stub mode.")


class MT5Connector:
    """
    Manages MT5 terminal connection lifecycle.
    Handles initialize / login / shutdown with error propagation.
    """

    def __init__(self, credentials: Dict[str, Any]) -> None:
        self._creds = credentials
        self._connected = False

    def connect(self) -> None:
        if not MT5_AVAILABLE:
            raise RuntimeError("MetaTrader5 package not installed.")

        if not mt5.initialize():
            raise ConnectionError(f"MT5 initialize() failed: {mt5.last_error()}")

        ok = mt5.login(
            login=self._creds["login"],
            password=self._creds["password"],
            server=self._creds["server"],
            timeout=self._creds.get("timeout_ms", 10000),
        )
        if not ok:
            mt5.shutdown()
            raise ConnectionError(f"MT5 login failed: {mt5.last_error()}")

        info = mt5.account_info()
        logger.info(
            f"MT5 connected — account={info.login}, "
            f"server={info.server}, equity={info.equity:.2f}"
        )
        self._connected = True

    def disconnect(self) -> None:
        if MT5_AVAILABLE and self._connected:
            mt5.shutdown()
            self._connected = False
            logger.info("MT5 disconnected.")

    def get_account_info(self) -> Optional[Any]:
        if not MT5_AVAILABLE or not self._connected:
            return None
        return mt5.account_info()

    def get_equity(self) -> float:
        info = self.get_account_info()
        return float(info.equity) if info else 0.0

    def get_balance(self) -> float:
        info = self.get_account_info()
        return float(info.balance) if info else 0.0

    @property
    def is_connected(self) -> bool:
        return self._connected

    def __enter__(self) -> "MT5Connector":
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.disconnect()
