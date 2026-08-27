# prod/data/universe.py
from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger("universe")


def build_symbol_map(
    tickers: List[str],
    symbol_map_cfg: Dict[str, Any],
) -> Dict[str, str]:
    """
    Build {ticker: mt5_symbol} dict from config.
    Logs unmapped symbols.
    """
    symbols = symbol_map_cfg.get("symbols", {})
    result: Dict[str, str] = {}
    unmapped: List[str] = []

    for ticker in tickers:
        mt5_sym = symbols.get(ticker, "")
        if not mt5_sym:
            unmapped.append(ticker)
        else:
            result[ticker] = mt5_sym

    # Collapsed to one summary line -- previously logged one WARNING per
    # unmapped ticker (300-500+ lines every run for the scan-only universe
    # tail, drowning out real signal/execution log lines). These tickers
    # are signal-only by design (no IC Markets symbol available); this is
    # expected, not an error. Full list still available at DEBUG level.
    if unmapped:
        logger.info(
            f"{len(unmapped)}/{len(tickers)} tickers have no MT5 symbol mapping "
            f"(signal-only, no execution) -- run tools/mt5_symbol_inspector.py for details."
        )
        logger.debug(f"Unmapped tickers: {unmapped}")

    return result


def get_ticker_sector(
    ticker: str,
    spider_cfg: Dict[str, Any],
) -> str:
    """Return spider_id (sector) for a ticker based on spiders.yaml."""
    for spider in spider_cfg.get("spiders", []):
        if ticker in spider.get("tickers", []):
            return spider["id"]
    return ""


# ── IG-mode helpers ───────────────────────────────────────────────────────────

def build_epic_map(
    tickers: List[str],
    ig_epic_cfg: Dict[str, Any],
) -> Dict[str, str]:
    """
    Build {ticker: ig_epic} dict from config/ig_epic_map.yaml.
    Logs unmapped tickers. Used in IG-mode orchestrator.

    Args:
        tickers     : List of ticker strings from production.yaml universe.tickers
        ig_epic_cfg : Loaded ig_epic_map.yaml content

    Returns:
        Dict mapping ticker → IG epic string (e.g. "CS.D.AAPL.CFD.IP")
    """
    epics = ig_epic_cfg.get("epics", {})
    result: Dict[str, str] = {}

    for ticker in tickers:
        epic = epics.get(ticker, "")
        if not epic:
            logger.warning(
                f"Ticker '{ticker}' has no IG epic mapping in config/ig_epic_map.yaml. "
                "Run tools/ig_epic_inspector.py to discover correct epics for your account."
            )
        else:
            result[ticker] = epic

    return result


def get_ig_currency(ticker: str, ig_epic_cfg: Dict[str, Any]) -> str:
    """Return currency code for a ticker; falls back to 'USD'."""
    overrides = ig_epic_cfg.get("currency_codes", {})
    return overrides.get(ticker, overrides.get("default", "USD"))


def get_ig_expiry(ticker: str, ig_epic_cfg: Dict[str, Any]) -> str:
    """Return expiry string for a ticker; falls back to '-' (undated DFB)."""
    overrides = ig_epic_cfg.get("expiry", {})
    return overrides.get(ticker, overrides.get("default", "-"))
