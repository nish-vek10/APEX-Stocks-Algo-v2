# tools/mt5_full_stock_catalogue.py
"""
APEX -- MT5 Full Stock CFD Catalogue Puller

Different job to mt5_symbol_inspector.py: that tool takes OUR ticker list and
guesses broker symbol names via suffix patterns (AAPL -> AAPL.NAS). This tool
instead pulls IC Markets' ENTIRE symbol universe straight from the terminal
(mt5.symbols_get(), no guessing) and filters it down to stock CFDs. This is
the ground-truth catalogue of everything IC Markets actually offers.

Why this matters: suffix-guessing can miss symbols that don't match the
expected pattern (this is likely why GOOGL/META/EA/WDC/ALAB came back
NOT FOUND in the previous inspector run -- they may exist under a different
name entirely, not just a different suffix). Pulling the full catalogue and
matching by base ticker sidesteps that.

Usage:
    python tools/mt5_full_stock_catalogue.py

Outputs (to tools/output/):
    mt5_stock_catalogue_latest.csv    -- full catalogue, one row per symbol
    mt5_stock_catalogue_latest.xlsx   -- same, formatted
    mt5_path_breakdown_latest.csv     -- diagnostic: symbol counts by broker
                                          category/path, so you can confirm
                                          the stock-CFD filter caught everything

After this runs once, tools/build_mt5_symbol_map.py can re-map the universe
against the cached catalogue any time production.yaml's ticker list changes
(e.g. after a Finviz refresh) WITHOUT reconnecting to MT5.
"""
from __future__ import annotations

import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "tools" / "output"
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

import os

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("mt5_catalogue")

try:
    import MetaTrader5 as mt5
except ImportError:
    logger.error("MetaTrader5 not installed. Run: pip install MetaTrader5")
    sys.exit(1)

try:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    OPENPYXL_AVAILABLE = True
except ImportError:
    OPENPYXL_AVAILABLE = False

# Known US-equity suffix patterns (same set as mt5_symbol_inspector.py) --
# used to identify base ticker + confirm a symbol is a US stock CFD.
# Non-greedy base group + optional trailing "-24" handles IC Markets' 24/5
# extended-hours variants (e.g. "WDC.NAS-24", "META.NAS-24"), which the
# original pattern silently failed to match, leaving base_ticker=NaN for
# ~74% of rows (5,417/7,275) -- including the only listing IC Markets has
# for WDC, ALAB, and META (META itself trades under base symbol MVRS.NAS
# in the standard-hours book; META.NAS-24 is the 24/5 variant).
_SUFFIX_RE = re.compile(r"^([A-Z][A-Z0-9\-]*?)(\.(NAS|NYSE|US|NYS|OTC|N|O|A))?(-24)?$")

# Keywords that indicate a stock/share CFD in the broker's path/description
# field (varies by broker -- IC Markets typically groups these under paths
# containing "Stock" or "Share"). Checked case-insensitively.
_STOCK_PATH_KEYWORDS = ("stock", "share", "equit")


def connect_mt5() -> bool:
    path = os.environ.get("MT5_TERMINAL_PATH", "") or None
    login = int(os.environ.get("MT5_LOGIN", "0") or "0")
    password = os.environ.get("MT5_PASSWORD", "")
    server = os.environ.get("MT5_SERVER", "")

    initialized = mt5.initialize(path=path) if path else mt5.initialize()
    if not initialized:
        logger.error(f"MT5 initialize() failed: {mt5.last_error()}")
        return False

    if login and password and server:
        ok = mt5.login(login=login, password=password, server=server, timeout=10000)
        if not ok:
            logger.error(f"MT5 login failed: {mt5.last_error()}")
            mt5.shutdown()
            return False

    info = mt5.account_info()
    if info:
        logger.info(f"Connected: account={info.login}, server={info.server}, equity={info.equity:.2f} {info.currency}")
    return True


def base_ticker(symbol_name: str) -> Optional[str]:
    """Strip known suffix from a broker symbol to recover the base ticker."""
    m = _SUFFIX_RE.match(symbol_name)
    return m.group(1) if m else None


def is_stock_cfd(sym) -> bool:
    """Heuristic: broker path/description mentions stock/share, or name matches
    a known US suffix pattern with a plausible ticker-like base."""
    path = (getattr(sym, "path", "") or "").lower()
    if any(kw in path for kw in _STOCK_PATH_KEYWORDS):
        return True
    # Fallback: suffix pattern match (catches brokers that don't set path/category)
    return base_ticker(sym.name) is not None and "." in sym.name


def main() -> None:
    if not connect_mt5():
        sys.exit(1)

    try:
        logger.info("Fetching full symbol list from MT5...")
        all_symbols = mt5.symbols_get()
        if not all_symbols:
            logger.error(f"symbols_get() returned nothing: {mt5.last_error()}")
            sys.exit(1)
        logger.info(f"Total symbols on account: {len(all_symbols)}")

        # Diagnostic: breakdown by top-level path so you can sanity-check the filter
        path_counts: dict[str, int] = {}
        for sym in all_symbols:
            top = (sym.path or "(no path)").split("\\")[0]
            path_counts[top] = path_counts.get(top, 0) + 1

        path_df = pd.DataFrame(
            sorted(path_counts.items(), key=lambda x: -x[1]),
            columns=["path_category", "symbol_count"],
        )

        stock_symbols = [s for s in all_symbols if is_stock_cfd(s)]
        logger.info(f"Filtered to {len(stock_symbols)} stock-CFD symbols")

        rows = []
        for s in stock_symbols:
            rows.append({
                "base_ticker": base_ticker(s.name) or "",
                "mt5_symbol": s.name,
                "path": s.path,
                "description": s.description,
                "currency_base": s.currency_base,
                "currency_profit": s.currency_profit,
                "contract_size": s.trade_contract_size,
                "volume_min": s.volume_min,
                "volume_step": s.volume_step,
                "trade_mode": s.trade_mode,
            })
        cat_df = pd.DataFrame(rows).sort_values("base_ticker").reset_index(drop=True)

        OUT_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

        for suffix in (ts, "latest"):
            cat_df.to_csv(OUT_DIR / f"mt5_stock_catalogue_{suffix}.csv", index=False)
            path_df.to_csv(OUT_DIR / f"mt5_path_breakdown_{suffix}.csv", index=False)

        if OPENPYXL_AVAILABLE:
            xlsx_path = OUT_DIR / "mt5_stock_catalogue_latest.xlsx"
            with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
                cat_df.to_excel(writer, sheet_name="Stock Catalogue", index=False)
                path_df.to_excel(writer, sheet_name="Path Breakdown", index=False)
            logger.info(f"Excel saved: {xlsx_path}")

        dupes = cat_df[cat_df.duplicated("base_ticker", keep=False)]

        print(f"\n{'─'*60}")
        print("  MT5 Full Stock CFD Catalogue")
        print(f"{'─'*60}")
        print(f"  Total symbols on account : {len(all_symbols)}")
        print(f"  Stock CFDs identified    : {len(cat_df)}")
        print(f"  Unique base tickers      : {cat_df['base_ticker'].nunique()}")
        print(f"  Duplicate base tickers   : {dupes['base_ticker'].nunique()}  (multiple listings/suffixes per ticker)")
        print(f"\n  Top path categories:")
        print(path_df.head(10).to_string(index=False))
        print(f"\n  Outputs:")
        print(f"    {OUT_DIR / 'mt5_stock_catalogue_latest.csv'}")
        print(f"    {OUT_DIR / 'mt5_stock_catalogue_latest.xlsx'}")
        print(f"    {OUT_DIR / 'mt5_path_breakdown_latest.csv'}")
        print(f"{'─'*60}\n")
        print("  -> Run tools/build_mt5_symbol_map.py to map this against config/production.yaml")
        print()

    finally:
        mt5.shutdown()
        logger.info("MT5 disconnected.")


if __name__ == "__main__":
    main()
