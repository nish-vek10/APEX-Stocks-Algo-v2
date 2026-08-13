# prod/execution/order_builder.py
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("order_builder")

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False

# Filling mode bitmask constants
FILL_FOK = 1
FILL_IOC = 2
FILL_RETURN = 4

FILL_NAME_MAP = {
    "FOK": mt5.ORDER_FILLING_FOK if MT5_AVAILABLE else 0,
    "IOC": mt5.ORDER_FILLING_IOC if MT5_AVAILABLE else 1,
    "RETURN": mt5.ORDER_FILLING_RETURN if MT5_AVAILABLE else 2,
}


def resolve_filling_mode(
    mt5_symbol: str,
    symbol_map_cfg: Dict[str, Any],
    global_default: str = "IOC",
) -> int:
    """
    Resolve MT5 filling mode constant for a symbol.
    Priority: 1. Symbol override in yaml  2. Auto-detect from broker bitmask  3. Global fallback.
    Returns mt5.ORDER_FILLING_* constant (int).
    """
    # 1. Symbol override
    overrides = symbol_map_cfg.get("filling_overrides", {})
    if mt5_symbol in overrides:
        raw = overrides[mt5_symbol]
        if isinstance(raw, int):
            return _bitmask_to_const(raw)
        if isinstance(raw, str):
            return FILL_NAME_MAP.get(raw.upper(), FILL_NAME_MAP["IOC"])

    # 2. Auto-detect from broker
    if MT5_AVAILABLE:
        try:
            info = mt5.symbol_info(mt5_symbol)
            if info is not None:
                bitmask = int(info.filling_mode)
                if bitmask & FILL_FOK:
                    return mt5.ORDER_FILLING_FOK
                if bitmask & FILL_IOC:
                    return mt5.ORDER_FILLING_IOC
                if bitmask & FILL_RETURN:
                    return mt5.ORDER_FILLING_RETURN
        except Exception as exc:
            logger.warning(f"filling_mode auto-detect failed for {mt5_symbol}: {exc}")

    # 3. Global fallback
    fallback = symbol_map_cfg.get("global_filling_mode", global_default).upper()
    return FILL_NAME_MAP.get(fallback, FILL_NAME_MAP["IOC"])


def _bitmask_to_const(bitmask: int) -> int:
    if not MT5_AVAILABLE:
        return 1  # IOC index
    if bitmask & FILL_FOK:
        return mt5.ORDER_FILLING_FOK
    if bitmask & FILL_IOC:
        return mt5.ORDER_FILLING_IOC
    if bitmask & FILL_RETURN:
        return mt5.ORDER_FILLING_RETURN
    return mt5.ORDER_FILLING_IOC


def build_entry_request(
    mt5_symbol: str,
    shares: float,
    stop_price: float,
    magic_number: int,
    symbol_map_cfg: Dict[str, Any],
    comment: str = "APEX_ENTRY",
    deviation: int = 10,
) -> Dict[str, Any]:
    """
    Build MT5 order_send request dict for a long market entry.
    Uses native SL (stop_price) on the order itself.
    """
    if not MT5_AVAILABLE:
        raise RuntimeError("MetaTrader5 not installed.")

    tick = mt5.symbol_info_tick(mt5_symbol)
    if tick is None:
        raise ValueError(f"No tick data for symbol: {mt5_symbol}")

    price = tick.ask
    filling = resolve_filling_mode(mt5_symbol, symbol_map_cfg)

    return {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": mt5_symbol,
        "volume": float(shares),
        "type": mt5.ORDER_TYPE_BUY,
        "price": price,
        "sl": float(stop_price),
        "tp": 0.0,
        "deviation": deviation,
        "magic": magic_number,
        "comment": comment,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling,
    }


def build_close_request(
    mt5_symbol: str,
    mt5_ticket: int,
    volume: float,
    position_type: int,
    magic_number: int,
    symbol_map_cfg: Dict[str, Any],
    comment: str = "APEX_EXIT",
    deviation: int = 10,
) -> Dict[str, Any]:
    """
    Build MT5 order_send request dict to close an existing position.
    position_type: mt5.ORDER_TYPE_BUY (0) to close → sell
    """
    if not MT5_AVAILABLE:
        raise RuntimeError("MetaTrader5 not installed.")

    tick = mt5.symbol_info_tick(mt5_symbol)
    if tick is None:
        raise ValueError(f"No tick data for symbol: {mt5_symbol}")

    # Closing a buy = sell at bid
    close_type = mt5.ORDER_TYPE_SELL if position_type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
    price = tick.bid if position_type == mt5.ORDER_TYPE_BUY else tick.ask
    filling = resolve_filling_mode(mt5_symbol, symbol_map_cfg)

    return {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": mt5_symbol,
        "volume": float(volume),
        "type": close_type,
        "position": mt5_ticket,
        "price": price,
        "deviation": deviation,
        "magic": magic_number,
        "comment": comment,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling,
    }
