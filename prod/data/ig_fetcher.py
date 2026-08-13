# prod/data/ig_fetcher.py
"""
Data fetcher for IG-mode APEX.

Strategy:
  - Historical OHLCV  → yfinance  (free, reliable, 300+ days, no rate limits)
  - Live bid/ask tick → IG REST API via IGConnector.get_live_price()

This separation keeps data ingestion clean and avoids IG's historical
rate limits (10 requests per minute on price history endpoint).

Schema output: date, open, high, low, close, volume  (identical to mt5_fetcher)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import pandas as pd

logger = logging.getLogger("ig_fetcher")

try:
    import yfinance as yf
    YF_AVAILABLE = True
except ImportError:
    YF_AVAILABLE = False
    logger.warning("yfinance not installed — pip install yfinance")


# ── Historical OHLCV via yfinance ──────────────────────────────────────────────

def fetch_ohlcv_yf(
    ticker: str,
    lookback_days: int = 300,
    interval: str = "1d",
) -> pd.DataFrame:
    """
    Fetch daily OHLCV from Yahoo Finance for a US equity ticker.

    Args:
        ticker       : Yahoo Finance ticker symbol (e.g. "AAPL")
        lookback_days: Number of calendar days to look back
        interval     : yfinance interval string — "1d" for daily

    Returns:
        DataFrame with columns: date, open, high, low, close, volume
        Sorted ascending, deduped, UTC-normalised dates.
    """
    if not YF_AVAILABLE:
        raise RuntimeError("yfinance not installed. Run: pip install yfinance")

    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=lookback_days + 10)

    try:
        raw = yf.download(
            ticker,
            start=start_dt.strftime("%Y-%m-%d"),
            end=end_dt.strftime("%Y-%m-%d"),
            interval=interval,
            auto_adjust=True,
            progress=False,
        )
    except Exception as exc:
        logger.error(f"yfinance download failed for {ticker}: {exc}")
        return pd.DataFrame()

    if raw is None or raw.empty:
        logger.warning(f"yfinance returned no data for {ticker}")
        return pd.DataFrame()

    # Reset index first so Date index becomes a regular column
    raw = raw.reset_index()

    # Flatten MultiIndex columns if present (yfinance >= 0.2), then lowercase all
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = [str(col[0]).lower() for col in raw.columns]
    else:
        raw.columns = [str(c).lower() for c in raw.columns]

    # Normalise date column name
    date_col = None
    for candidate in ("date", "datetime", "index"):
        if candidate in raw.columns:
            date_col = candidate
            break
    if date_col is None:
        logger.error(f"Cannot identify date column in yfinance output for {ticker}: {list(raw.columns)}")
        return pd.DataFrame()

    df = raw.rename(columns={date_col: "date"})
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)  # strip tz → naive UTC

    # Ensure required columns exist
    required = ["open", "high", "low", "close", "volume"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        logger.error(f"{ticker}: missing columns {missing} after normalise")
        return pd.DataFrame()

    df = df[["date", "open", "high", "low", "close", "volume"]].copy()
    df = df.dropna(subset=["close"])
    df = df.drop_duplicates("date").sort_values("date").reset_index(drop=True)

    logger.debug(f"yfinance: {len(df)} bars for {ticker}")
    return df


# ── Universe fetch ─────────────────────────────────────────────────────────────

def fetch_universe_ig(
    epic_map: Dict[str, str],
    lookback_days: int = 300,
) -> Dict[str, pd.DataFrame]:
    """
    Fetch OHLCV for all tickers in the universe via yfinance.
    epic_map: {ticker: ig_epic}  — only tickers are used for yfinance download.
    Returns: {ticker: df}  — same interface as mt5_fetcher.fetch_universe()
    """
    results: Dict[str, pd.DataFrame] = {}

    for ticker in epic_map.keys():
        try:
            df = fetch_ohlcv_yf(ticker, lookback_days)
            if not df.empty:
                results[ticker] = df
            else:
                logger.warning(f"No data returned for {ticker}")
        except Exception as exc:
            logger.error(f"Failed to fetch {ticker}: {exc}")

    logger.info(f"ig_fetcher: {len(results)}/{len(epic_map)} tickers fetched.")
    return results


# ── Live price via IG (called at execution time) ───────────────────────────────

def get_live_bid_ask(
    connector: Any,
    epic: str,
) -> Dict[str, float]:
    """
    Get current bid/ask for an epic from IG.
    connector: IGConnector instance (must be connected).
    Returns: {bid, ask, mid}
    """
    return connector.get_live_price(epic)
