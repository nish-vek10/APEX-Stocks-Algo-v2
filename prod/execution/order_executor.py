# prod/execution/order_executor.py
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("order_executor")

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False

# Retcodes that are transient (retry)
RETCODE_RETRY = {10004, 10006, 10007, 10018, 10023, 10024}

# Retcodes that are fatal (do not retry)
RETCODE_FATAL = {
    10009,  # TRADE_RETCODE_DONE — actually success
    10013,  # INVALID_REQUEST
    10014,  # INVALID_VOLUME
    10015,  # INVALID_PRICE
    10016,  # INVALID_STOPS
    10017,  # TRADE_DISABLED
    10019,  # NO_MONEY
    10025,  # REJECT
    10026,  # CANCEL
    10027,  # PLACED
    10030,  # ONLY_REAL
}

# Success retcodes
RETCODE_SUCCESS = {10008, 10009}


def send_order(
    request: Dict[str, Any],
    environment: str = "paper",
    max_retries: int = 3,
    retry_delay: float = 1.0,
) -> Dict[str, Any]:
    """
    Send an order to MT5.
    Paper mode: logs request, returns simulated success — NEVER calls mt5.order_send().
    Live mode: sends to broker with retry logic.
    Returns result dict with keys: retcode, order, volume, price, comment, success.
    """
    if environment == "paper":
        logger.info(
            f"[PAPER] ORDER SIMULATED — symbol={request.get('symbol')}, "
            f"volume={request.get('volume')}, action={request.get('action')}, "
            f"price={request.get('price')}, sl={request.get('sl')}"
        )
        return {
            "success": True,
            "retcode": 10009,
            "order": 0,
            "volume": request.get("volume", 0.0),
            "price": request.get("price", 0.0),
            "comment": "paper_trade",
            "request": request,
        }

    if not MT5_AVAILABLE:
        raise RuntimeError("MetaTrader5 not installed — cannot execute live orders.")

    last_result = None
    for attempt in range(1, max_retries + 1):
        result = mt5.order_send(request)

        if result is None:
            logger.error(f"order_send returned None — MT5 error: {mt5.last_error()}")
            if attempt < max_retries:
                time.sleep(retry_delay)
                continue
            return {"success": False, "retcode": -1, "comment": "null_result"}

        retcode = result.retcode

        if retcode in RETCODE_SUCCESS:
            logger.info(
                f"ORDER FILLED — symbol={request.get('symbol')}, "
                f"order={result.order}, volume={result.volume}, price={result.price}"
            )
            return {
                "success": True,
                "retcode": retcode,
                "order": result.order,
                "volume": result.volume,
                "price": result.price,
                "comment": result.comment,
                "request": request,
            }

        if retcode in RETCODE_FATAL:
            logger.error(
                f"ORDER FATAL retcode={retcode} ({result.comment}) — "
                f"symbol={request.get('symbol')}, no retry."
            )
            return {
                "success": False,
                "retcode": retcode,
                "comment": result.comment,
                "request": request,
            }

        if retcode in RETCODE_RETRY and attempt < max_retries:
            logger.warning(
                f"ORDER retcode={retcode} — attempt {attempt}/{max_retries}, retrying..."
            )
            time.sleep(retry_delay)
            last_result = result
            continue

        logger.error(f"ORDER FAILED retcode={retcode} ({result.comment})")
        return {
            "success": False,
            "retcode": retcode,
            "comment": getattr(result, "comment", ""),
            "request": request,
        }

    return {
        "success": False,
        "retcode": getattr(last_result, "retcode", -1),
        "comment": "max_retries_exceeded",
        "request": request,
    }


def confirm_fills(
    pending_orders: List[Dict[str, Any]],
    magic_number: int,
) -> Dict[int, Dict[str, Any]]:
    """
    Confirm fills for pending orders by querying MT5 open positions.
    Returns dict keyed by order ticket: {filled: bool, volume: float, price: float}
    """
    if not MT5_AVAILABLE:
        return {o.get("order", i): {"filled": True, "volume": 0, "price": 0}
                for i, o in enumerate(pending_orders)}

    positions = mt5.positions_get()
    if positions is None:
        logger.warning("positions_get() returned None")
        return {}

    pos_map = {p.ticket: p for p in positions}
    results: Dict[int, Dict[str, Any]] = {}

    for order in pending_orders:
        ticket = order.get("order", 0)
        if ticket in pos_map:
            p = pos_map[ticket]
            results[ticket] = {
                "filled": True,
                "volume": p.volume,
                "price": p.price_open,
                "sl": p.sl,
                "symbol": p.symbol,
            }
        else:
            results[ticket] = {"filled": False, "volume": 0.0, "price": 0.0}

    return results
