# prod/execution/order_builder.py
from __future__ import annotations

import logging
import math
import time
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


def ensure_symbol_selected(mt5_symbol: str) -> bool:
    """
    Make sure `mt5_symbol` is visible in Market Watch before requesting a
    tick or symbol_info-dependent value from it.

    IC Markets (and MT5 generally) will return `symbol_info_tick() is None`
    for any symbol not currently added to Market Watch -- this is NOT the
    same as "symbol doesn't exist" and previously crashed live execution
    (e.g. BTU.NYSE-24: "No tick data for symbol") purely because the
    symbol had never been selected in this terminal session. Calling
    mt5.symbol_select(symbol, True) is idempotent and safe to call every
    time. Returns True if the symbol is (now) selected/visible.
    """
    if not MT5_AVAILABLE:
        return False
    info = mt5.symbol_info(mt5_symbol)
    if info is not None and info.visible:
        return True
    return bool(mt5.symbol_select(mt5_symbol, True))


def get_live_tick(mt5_symbol: str, retries: int = 3, delay_sec: float = 0.5) -> Optional[Any]:
    """
    Select `mt5_symbol` and fetch a tick with ask > 0, retrying briefly.

    A symbol freshly added to Market Watch this session (common for the
    "-24" extended-hours variants, which most of the universe's execution
    symbols are) can return a tick object with ask=0.0/bid=0.0 for the
    first fraction of a second while the broker's quote stream warms up --
    this is NOT the same as "no quotes available at all". Found
    2026-09-01: 7 of 21 successful signals that day (APAM, BRCB, GPOR, LDP,
    MSB, PATH, VTS -- ALL "-24" symbols) were rejected as no_live_quote on
    the very first tick check with no retry. Returns the tick object (with
    ask/bid > 0) on success, or None if still no valid quote after
    `retries` attempts.
    """
    if not MT5_AVAILABLE:
        return None
    ensure_symbol_selected(mt5_symbol)
    for attempt in range(retries):
        tick = mt5.symbol_info_tick(mt5_symbol)
        if tick is not None and float(tick.ask) > 0:
            return tick
        if attempt < retries - 1:
            time.sleep(delay_sec)
    return None


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


def resolve_mt5_volume(
    mt5_symbol: str,
    target_shares: float,
    stop_distance: float,
    risk_dollars_target: float,
    deviation_warn_pct: float = 0.15,
    deviation_max_pct: float = 0.30,
) -> Dict[str, Any]:
    """
    Convert a broker-agnostic "shares" figure (from prod/risk/position_sizer.py,
    which assumes 1 unit = 1 underlying share -- correct for IG, NOT correct
    for MT5) into a valid MT5 lot/volume for this specific symbol, honoring
    the broker's actual contract_size / volume_step / volume_min / volume_max
    (queried live via mt5.symbol_info -- these vary per IC Markets CFD and
    are never hardcoded/assumed).

    This closes a real risk-sizing bug: previously `int(shares)` was passed
    straight through as the MT5 `volume` field with zero regard for lot
    step/contract size, meaning actual $ risk per trade could silently
    diverge from the configured 1% target by an arbitrary amount depending
    on the symbol's contract spec.

    Always rounds volume DOWN to the nearest volume_step -- never up, since
    rounding up would push realized risk above the 1% target.

    Returns dict:
      ok               : bool -- False means do not trade (skip, log reason)
      reason           : str  -- present when ok=False
      volume           : float -- MT5 lot volume to send (0.0 if not ok)
      actual_shares    : float -- volume * contract_size (use this as the
                          "shares" figure for position tracking / P&L, NOT
                          the raw MT5 lot count)
      actual_risk_dollars, target_risk_dollars, deviation_pct : for logging/
                          alerting when broker lot-stepping causes realized
                          risk to drift from the 1% target.
    """
    if not MT5_AVAILABLE:
        raise RuntimeError("MetaTrader5 not installed.")

    ensure_symbol_selected(mt5_symbol)
    info = mt5.symbol_info(mt5_symbol)
    if info is None:
        return {"ok": False, "reason": "no_symbol_info", "volume": 0.0}

    contract_size = float(getattr(info, "trade_contract_size", 1.0) or 1.0)
    volume_step = float(getattr(info, "volume_step", 0.01) or 0.01)
    volume_min = float(getattr(info, "volume_min", volume_step) or volume_step)
    volume_max = float(getattr(info, "volume_max", 1e9) or 1e9)

    raw_volume = target_shares / contract_size if contract_size > 0 else 0.0
    steps = math.floor(raw_volume / volume_step + 1e-9) if volume_step > 0 else 0
    volume = round(steps * volume_step, 8)

    if volume < volume_min:
        logger.info(
            f"{mt5_symbol}: sized volume {volume} below broker volume_min "
            f"{volume_min} (target_shares={target_shares}, contract_size="
            f"{contract_size}) -- skipping trade."
        )
        return {
            "ok": False, "reason": "below_volume_min", "volume": 0.0,
            "raw_volume": raw_volume, "volume_min": volume_min,
        }

    volume = min(volume, volume_max)
    actual_shares = volume * contract_size
    actual_risk_dollars = actual_shares * stop_distance
    deviation_pct = (
        abs(actual_risk_dollars - risk_dollars_target) / risk_dollars_target
        if risk_dollars_target > 0 else 0.0
    )

    if deviation_pct > deviation_max_pct:
        # Hard skip: lot granularity is too coarse for this symbol to
        # reasonably approximate the 1% risk target (e.g. volume_step=1000
        # on a low-priced CFD forces risk to swing by tens of percent per
        # step). Previously this only logged a WARNING and the trade still
        # executed with realized risk up to ~85% away from target -- a real
        # risk-sizing breach. Now the trade is skipped and alerted like any
        # other rejected order (see orchestrator.py's alert_order_rejected
        # call on result["success"]=False).
        logger.warning(
            f"{mt5_symbol}: SKIPPED -- MT5 lot-step rounding would move "
            f"realized risk {deviation_pct*100:.1f}% away from 1% target "
            f"(target=${risk_dollars_target:.2f}, actual=${actual_risk_dollars:.2f}, "
            f"volume={volume}, contract_size={contract_size}, "
            f"max_allowed={deviation_max_pct*100:.0f}%)."
        )
        return {
            "ok": False, "reason": "risk_deviation_too_high", "volume": 0.0,
            "deviation_pct": round(deviation_pct, 4),
            "target_risk_dollars": round(risk_dollars_target, 2),
            "actual_risk_dollars": round(actual_risk_dollars, 2),
        }

    if deviation_pct > deviation_warn_pct:
        logger.warning(
            f"{mt5_symbol}: MT5 lot-step rounding moved realized risk "
            f"{deviation_pct*100:.1f}% away from 1% target "
            f"(target=${risk_dollars_target:.2f}, actual=${actual_risk_dollars:.2f}, "
            f"volume={volume}, contract_size={contract_size})."
        )

    return {
        "ok": True,
        "volume": volume,
        "contract_size": contract_size,
        "volume_step": volume_step,
        "volume_min": volume_min,
        "actual_shares": round(actual_shares, 4),
        "actual_risk_dollars": round(actual_risk_dollars, 2),
        "target_risk_dollars": round(risk_dollars_target, 2),
        "deviation_pct": round(deviation_pct, 4),
    }


def build_entry_request(
    mt5_symbol: str,
    volume: float,
    stop_price: float,
    magic_number: int,
    symbol_map_cfg: Dict[str, Any],
    comment: str = "APEX_ENTRY",
    deviation: int = 10,
) -> Dict[str, Any]:
    """
    Build MT5 order_send request dict for a long market entry.

    `volume` must already be a broker-valid MT5 lot size (output of
    resolve_mt5_volume() -- rounded to volume_step, clamped to
    volume_min/max), NOT the raw broker-agnostic "shares" figure from
    position_sizer.py.

    Market order (TRADE_ACTION_DEAL + ORDER_TYPE_BUY) filled at current
    available ask -- executes immediately at whatever price MT5 returns,
    matching "execute at NYSE open with available price."

    `sl` = the signal's computed stop-loss, set as the order's native
    broker-side stop (enforced continuously by MT5/IC Markets, not
    dependent on this process staying alive or polling).

    `tp` is intentionally 0.0 (no take-profit). This is not an oversight --
    the validated backtest (ALGO-Stocks, PF 2.26 / E[R] 0.63) has NO fixed
    take-profit target anywhere in its exit logic. Exits are stop-loss,
    Stage 9 fade detection, or time stop only (see
    prod/orchestrator.py::_process_exits). Setting a TP here would be new,
    unvalidated strategy behavior.
    """
    if not MT5_AVAILABLE:
        raise RuntimeError("MetaTrader5 not installed.")

    tick = get_live_tick(mt5_symbol)
    if tick is None:
        raise ValueError(f"No tick data for symbol: {mt5_symbol} (not in Market Watch / no live quote after retries)")

    price = tick.ask
    filling = resolve_filling_mode(mt5_symbol, symbol_map_cfg)

    return {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": mt5_symbol,
        "volume": float(volume),
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

    tick = get_live_tick(mt5_symbol)
    if tick is None:
        raise ValueError(f"No tick data for symbol: {mt5_symbol} (not in Market Watch / no live quote after retries)")

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
