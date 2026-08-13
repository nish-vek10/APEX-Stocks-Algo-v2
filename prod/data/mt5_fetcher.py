# prod/data/mt5_fetcher.py
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pandas as pd

logger = logging.getLogger("mt5_fetcher")

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False

_TF_MAP = {
    "M1": 1, "M5": 5, "M15": 15, "M30": 30,
    "H1": 16385, "H4": 16388,
    "D1": 16408, "W1": 32769, "MN1": 49153,
}


def _tf_const(tf_str: str) -> int:
    if not MT5_AVAILABLE:
        return 16408
    mapping = {
        "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5,
        "M15": mt5.TIMEFRAME_M15, "M30": mt5.TIMEFRAME_M30,
        "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
        "D1": mt5.TIMEFRAME_D1, "W1": mt5.TIMEFRAME_W1,
        "MN1": mt5.TIMEFRAME_MN1,
    }
    return mapping.get(tf_str.upper(), mt5.TIMEFRAME_D1)


def fetch_ohlcv(
    mt5_symbol: str,
    timeframe: str = "D1",
    lookback_days: int = 300,
) -> pd.DataFrame:
    """
    Fetch OHLCV bars from MT5 for a symbol.
    Returns DataFrame with columns: date, open, high, low, close, volume.
    """
    if not MT5_AVAILABLE:
        raise RuntimeError("MetaTrader5 not installed.")

    tf = _tf_const(timeframe)
    utc_to = datetime.now(timezone.utc)
    utc_from = utc_to - timedelta(days=lookback_days + 10)

    rates = mt5.copy_rates_range(mt5_symbol, tf, utc_from, utc_to)
    if rates is None or len(rates) == 0:
        logger.warning(f"No data returned for {mt5_symbol}")
        return pd.DataFrame()

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.rename(columns={
        "time": "date",
        "open": "open",
        "high": "high",
        "low": "low",
        "close": "close",
        "tick_volume": "volume",
    })
    df = df[["date", "open", "high", "low", "close", "volume"]].copy()
    df["date"] = df["date"].dt.normalize()
    df = df.drop_duplicates("date").sort_values("date").reset_index(drop=True)
    logger.debug(f"Fetched {len(df)} bars for {mt5_symbol}")
    return df


def fetch_universe(
    symbol_map: Dict[str, str],
    timeframe: str = "D1",
    lookback_days: int = 300,
) -> Dict[str, pd.DataFrame]:
    """
    Fetch OHLCV for all symbols in the universe.
    symbol_map: {ticker: mt5_symbol}
    Returns {ticker: df}
    """
    results = {}
    for ticker, mt5_sym in symbol_map.items():
        if not mt5_sym:
            logger.warning(f"No MT5 symbol mapped for {ticker} — skipping.")
            continue
        try:
            df = fetch_ohlcv(mt5_sym, timeframe, lookback_days)
            if not df.empty:
                results[ticker] = df
        except Exception as exc:
            logger.error(f"Failed to fetch {ticker} ({mt5_sym}): {exc}")
    return results
