# prod/risk/position_sizer.py
from __future__ import annotations

import logging
import math
from typing import Any, Dict, Optional

import pandas as pd

logger = logging.getLogger("position_sizer")


def compute_position_size(
    signal: Dict[str, Any],
    equity: float,
    prod_cfg: Dict[str, Any],
    risk_cfg: Dict[str, Any],
    feature_df: Optional[pd.DataFrame] = None,
    gate_risk_mult: float = 1.0,
) -> Dict[str, Any]:
    """
    Dispatch to active sizing mode. Gate multiplier applied on top.

    Args:
        signal: signal dict from SignalGenerator (must have entry_open, stop_price)
        equity: current account equity
        prod_cfg: production.yaml content
        risk_cfg: risk.yaml content
        feature_df: DataFrame with OHLCV + indicators (needed for volume_based, atr_dynamic)
        gate_risk_mult: spider gate risk multiplier (1.0 if gate disabled)

    Returns:
        dict: {shares, position_value, risk_dollars, stop_distance, mode, gate_mult}
    """
    mode = risk_cfg.get("active_mode", "equity_pct")
    entry_price = float(signal.get("entry_open", signal.get("close", 0.0)))
    stop_price = float(signal.get("stop_price", signal.get("stop_price_eod", 0.0)))

    if entry_price <= 0:
        logger.warning(f"Invalid entry_price={entry_price} for {signal.get('ticker')}")
        return _zero_result(mode, gate_risk_mult)

    stop_distance = entry_price - stop_price
    if stop_distance <= 0:
        logger.warning(
            f"Stop distance <= 0 for {signal.get('ticker')}: "
            f"entry={entry_price}, stop={stop_price}"
        )
        return _zero_result(mode, gate_risk_mult)

    glb = risk_cfg.get("global", {})
    max_shares = glb.get("max_shares", 10000)
    min_shares = glb.get("min_shares", 1)
    min_pos_val = glb.get("min_position_value", 100.0)

    if mode == "equity_pct":
        shares = _size_equity_pct(equity, stop_distance, risk_cfg, gate_risk_mult)
    elif mode == "fixed_dollar_risk":
        shares = _size_fixed_dollar_risk(stop_distance, risk_cfg, gate_risk_mult)
    elif mode == "fixed_lots":
        shares = _size_fixed_lots(risk_cfg, gate_risk_mult)
    elif mode == "atr_dynamic":
        shares = _size_atr_dynamic(equity, signal, risk_cfg, gate_risk_mult, feature_df)
    elif mode == "volume_based":
        shares = _size_volume_based(equity, entry_price, risk_cfg, gate_risk_mult, feature_df)
    else:
        logger.error(f"Unknown sizing mode: {mode}")
        return _zero_result(mode, gate_risk_mult)

    # Apply portfolio position cap
    port_cap = prod_cfg.get("portfolio", {}).get("max_single_position_pct", 0.20)
    max_by_port = math.floor((equity * port_cap) / entry_price) if entry_price > 0 else 0

    shares = int(min(shares, max_shares, max_by_port))
    shares = max(shares, 0)

    position_value = shares * entry_price
    if shares < min_shares or position_value < min_pos_val:
        logger.info(
            f"{signal.get('ticker')}: position too small "
            f"(shares={shares}, value={position_value:.2f}) — skipped."
        )
        return _zero_result(mode, gate_risk_mult)

    risk_dollars = shares * stop_distance

    return {
        "shares": shares,
        "position_value": round(position_value, 2),
        "risk_dollars": round(risk_dollars, 2),
        "stop_distance": round(stop_distance, 4),
        "mode": mode,
        "gate_mult": gate_risk_mult,
        "entry_price": entry_price,
        "stop_price": stop_price,
    }


def _size_equity_pct(
    equity: float,
    stop_distance: float,
    risk_cfg: Dict[str, Any],
    gate_mult: float,
) -> float:
    cfg = risk_cfg.get("equity_pct", {})
    risk_pct = float(cfg.get("risk_pct_per_trade", 0.01))
    risk_dollars = equity * risk_pct * gate_mult
    return risk_dollars / stop_distance if stop_distance > 0 else 0


def _size_fixed_dollar_risk(
    stop_distance: float,
    risk_cfg: Dict[str, Any],
    gate_mult: float,
) -> float:
    cfg = risk_cfg.get("fixed_dollar_risk", {})
    risk_dollars = float(cfg.get("risk_dollars", 200.0)) * gate_mult
    return risk_dollars / stop_distance if stop_distance > 0 else 0


def _size_fixed_lots(risk_cfg: Dict[str, Any], gate_mult: float) -> float:
    cfg = risk_cfg.get("fixed_lots", {})
    lots = float(cfg.get("lots", 10))
    apply_gate = bool(cfg.get("apply_gate_multiplier", True))
    return lots * gate_mult if apply_gate else lots


def _size_atr_dynamic(
    equity: float,
    signal: Dict[str, Any],
    risk_cfg: Dict[str, Any],
    gate_mult: float,
    feature_df: Optional[pd.DataFrame],
) -> float:
    cfg = risk_cfg.get("atr_dynamic", {})
    vol_target = float(cfg.get("vol_target_pct", 0.01))
    atr_scale = float(cfg.get("atr_scale_factor", 2.0))

    atr = float(signal.get("atr", 0.0))
    if atr <= 0 and feature_df is not None and not feature_df.empty:
        atr = float(feature_df.iloc[-1].get("atr", 0.0))

    if atr <= 0:
        logger.warning("ATR = 0 in atr_dynamic sizing — falling back to equity_pct")
        return _size_equity_pct(equity, signal.get("entry_open", 1.0) * 0.02, risk_cfg, gate_mult)

    risk_dollars = equity * vol_target * gate_mult
    return risk_dollars / (atr * atr_scale)


def _size_volume_based(
    equity: float,
    entry_price: float,
    risk_cfg: Dict[str, Any],
    gate_mult: float,
    feature_df: Optional[pd.DataFrame],
) -> float:
    cfg = risk_cfg.get("volume_based", {})
    adv_pct = float(cfg.get("adv_participation_pct", 0.01))
    lookback = int(cfg.get("lookback_days", 20))
    eq_pct_cap = float(cfg.get("equity_pct_cap", 0.10))

    avg_volume = 0.0
    if feature_df is not None and "volume" in feature_df.columns:
        avg_volume = float(feature_df["volume"].tail(lookback).mean())

    adv_based = avg_volume * adv_pct * gate_mult if avg_volume > 0 else 0
    equity_based = (equity * eq_pct_cap) / entry_price if entry_price > 0 else 0

    return min(adv_based, equity_based)


def _zero_result(mode: str, gate_mult: float) -> Dict[str, Any]:
    return {
        "shares": 0,
        "position_value": 0.0,
        "risk_dollars": 0.0,
        "stop_distance": 0.0,
        "mode": mode,
        "gate_mult": gate_mult,
        "entry_price": 0.0,
        "stop_price": 0.0,
    }
