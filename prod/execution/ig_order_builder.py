# prod/execution/ig_order_builder.py
"""
IG OTC position request builder.
Builds dicts for create_open_position / close_open_position calls.
Replaces mt5 order_builder for IG-mode execution.

IG OTC sizing note:
  - size = number of units/contracts (equivalent to shares for stock CFDs)
  - For most stock CFDs on IG: 1 unit = 1 share in the underlying currency
  - direction: "BUY" | "SELL"
  - stop_level: absolute price level for the stop (preferred over stop_distance)
"""
from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger("ig_order_builder")


def build_entry_request_ig(
    epic: str,
    size: float,
    stop_price: float,
    currency_code: str = "USD",
    order_type: str = "MARKET",
    expiry: str = "-",
    guaranteed_stop: bool = False,
    force_open: bool = True,
    comment: str = "APEX_ENTRY",
) -> Dict[str, Any]:
    """
    Build IG OTC create_open_position request dict for a long entry.

    Args:
        epic           : IG epic string (e.g. "CS.D.AAPL.CFD.IP")
        size           : Number of units/contracts (equivalent to shares)
        stop_price     : Absolute stop level (price), e.g. 148.50
        currency_code  : Account currency code
        order_type     : "MARKET" (default) | "LIMIT" | "QUOTE"
        expiry         : "-" for undated (DFB/rolling)
        guaranteed_stop: True = guaranteed stop (may attract premium)
        force_open     : True = open new position even if opposing position exists
        comment        : Deal reference prefix (IG appends a timestamp)

    Returns:
        Dict ready for ig_service.create_open_position(**request) or direct REST POST
    """
    if size <= 0:
        raise ValueError(f"Invalid size={size} for epic={epic}")
    if stop_price <= 0:
        raise ValueError(f"Invalid stop_price={stop_price} for epic={epic}")

    return {
        "epic": epic,
        "direction": "BUY",
        "size": float(size),
        "order_type": order_type,
        "expiry": expiry,
        "currency_code": currency_code,
        "force_open": force_open,
        "guaranteed_stop": guaranteed_stop,
        "stop_level": float(stop_price),    # absolute price stop
        "stop_distance": None,              # using stop_level, not distance
        "limit_level": None,
        "limit_distance": None,
        "quote_id": None,
        "level": None,                      # only needed for LIMIT/QUOTE orders
        "trailing_stop": False,
        "trailing_stop_increment": None,
        "_comment": comment,                # stored for logging; not sent to IG
    }


def build_close_request_ig(
    deal_id: str,
    epic: str,
    size: float,
    order_type: str = "MARKET",
    expiry: str = "-",
    comment: str = "APEX_EXIT",
) -> Dict[str, Any]:
    """
    Build IG OTC close_open_position request dict.

    Args:
        deal_id    : IG dealId of the open position to close
        epic       : IG epic string
        size       : Size to close (partial or full)
        order_type : "MARKET" (default)
        expiry     : "-" for undated
        comment    : Reason tag for logging

    Returns:
        Dict ready for ig_service.close_open_position(**request) or direct REST DELETE
    """
    if not deal_id:
        raise ValueError(f"deal_id is required to close a position (epic={epic})")
    if size <= 0:
        raise ValueError(f"Invalid close size={size} for deal_id={deal_id}")

    return {
        "deal_id": deal_id,
        "epic": epic,
        "direction": "SELL",             # closing a long = SELL
        "size": float(size),
        "order_type": order_type,
        "expiry": expiry,
        "level": None,                   # only needed for non-MARKET orders
        "quote_id": None,
        "_comment": comment,
    }
