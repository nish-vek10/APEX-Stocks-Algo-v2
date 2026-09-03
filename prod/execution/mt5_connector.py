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

        # Custom terminal install path (e.g. multiple MT5 instances for different
        # brokers/EAs on one machine) -- MT5_TERMINAL_PATH in .env. If unset,
        # mt5.initialize() attaches to the default/already-running terminal.
        path = self._creds.get("path")
        initialized = mt5.initialize(path=path) if path else mt5.initialize()
        if not initialized:
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
        term = mt5.terminal_info()

        # Two separate "is algo trading actually allowed" gates, both
        # required for order_send() to work at all -- surfaced explicitly
        # so a misconfigured terminal (AutoTrading button off) or a
        # broker-side EA restriction is caught HERE, at startup, not as a
        # confusing order rejection hours later.
        terminal_algo_ok = bool(term.trade_allowed) if term else False
        account_algo_ok = bool(getattr(info, "trade_expert", True))
        account_trading_ok = bool(getattr(info, "trade_allowed", True))
        all_ok = terminal_algo_ok and account_algo_ok and account_trading_ok

        banner = (
            f"\n{'='*60}\n"
            f"MT5 CONNECTED\n"
            f"{'='*60}\n"
            f"  Account:        {info.login}\n"
            f"  Server:         {info.server}\n"
            f"  Company:        {getattr(term, 'company', 'n/a')}\n"
            f"  Currency:       {info.currency}\n"
            f"  Leverage:       1:{info.leverage}\n"
            f"  Balance:        {info.balance:,.2f}\n"
            f"  Equity:         {info.equity:,.2f}\n"
            f"  Trading mode:   {'DEMO' if getattr(info, 'trade_mode', 0) == 0 else 'LIVE/CONTEST'}\n"
            f"  AutoTrading (terminal):  {'ENABLED' if terminal_algo_ok else '*** DISABLED -- click AutoTrading button in MT5 ***'}\n"
            f"  Algo trading (account):  {'ALLOWED' if account_algo_ok else '*** BLOCKED BY BROKER ***'}\n"
            f"  Trading (account):       {'ALLOWED' if account_trading_ok else '*** BLOCKED (read-only account?) ***'}\n"
            f"  Overall status: {'READY TO TRADE' if all_ok else '*** NOT READY -- SEE ABOVE ***'}\n"
            f"{'='*60}"
        )
        # print() as well as logger.info() -- this is safety-critical
        # startup visibility (is the account actually able to place
        # orders right now), so it must never depend on logging
        # configuration being correct to be seen.
        print(banner)
        logger.info(banner)

        if not all_ok:
            logger.warning("MT5 connected but NOT ready to trade -- see banner above for which gate is blocking.")

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
