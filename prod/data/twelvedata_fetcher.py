# prod/data/twelvedata_fetcher.py
"""
APEX TwelveData OHLCV Fetcher

Replicates backtest data source exactly — same API, same schema.
Backtest used TwelveData via research/experiments/06_fetch_twelvedata_ohlcv_3y.py.

In production:
  - Primary: TwelveData (exact backtest parity, reliable)
  - Fallback: yfinance (free, fast, but minor price differences)

Rate limits (from .env):
  TD_CREDITS_PER_MIN=8   -> sleep 60/8 = 7.5s between calls
  TD_BATCH_SIZE=8        -> fetch up to 8 symbols per call

Usage:
  from prod.data.twelvedata_fetcher import fetch_universe_twelvedata
  universe_data = fetch_universe_twelvedata(tickers, lookback_days=300)
"""
from __future__ import annotations

import os
import time
import logging
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

logger = logging.getLogger("twelvedata_fetcher")

# Rate limit config (from .env)
CREDITS_PER_MIN = int(os.environ.get("TD_CREDITS_PER_MIN", "8"))
BATCH_SIZE      = int(os.environ.get("TD_BATCH_SIZE", "8"))
SLEEP_BETWEEN   = 60.0 / max(CREDITS_PER_MIN, 1)   # seconds between API calls

TD_INTERVAL  = os.environ.get("TD_INTERVAL", "1day")
TD_TIMEZONE  = os.environ.get("TD_TIMEZONE", "UTC")
TD_OUTPUTSIZE = int(os.environ.get("TD_OUTPUTSIZE", "5000"))


def _normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """
    Standardise TwelveData response to production schema:
      date, open, high, low, close, volume
    Matches normalize_ohlcv() from backtest 06_fetch_twelvedata_ohlcv_3y.py.
    """
    df = df.copy()

    # Normalize date column (TwelveData returns 'datetime' or index)
    if "datetime" in df.columns:
        df["date"] = pd.to_datetime(df["datetime"], errors="coerce")
        df = df.drop(columns=["datetime"])
    elif "date" not in df.columns:
        df = df.reset_index()
        if "datetime" in df.columns:
            df["date"] = pd.to_datetime(df["datetime"], errors="coerce")
            df = df.drop(columns=["datetime"])
        elif df.columns[0] != "date":
            df = df.rename(columns={df.columns[0]: "date"})
        df["date"] = pd.to_datetime(df["date"], errors="coerce")

    # Numeric OHLCV
    for col in ["open", "high", "low", "close", "volume"]:
        if col not in df.columns:
            df[col] = None
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    return df[["date", "open", "high", "low", "close", "volume"]]


def fetch_ticker_twelvedata(
    ticker: str,
    api_key: str,
    lookback_days: int = 300,
) -> Optional[pd.DataFrame]:
    """
    Fetch daily OHLCV for one ticker via TwelveData.
    Returns normalised DataFrame or None on failure.
    """
    try:
        from twelvedata import TDClient
    except ImportError:
        logger.warning("twelvedata package not installed. Run: pip install twelvedata")
        return None

    try:
        td = TDClient(apikey=api_key)
        ts = td.time_series(
            symbol=ticker,
            interval=TD_INTERVAL,
            outputsize=min(lookback_days + 50, TD_OUTPUTSIZE),   # buffer for warmup
            timezone=TD_TIMEZONE,
        )
        df_raw = ts.as_pandas()
        if df_raw is None or df_raw.empty:
            logger.warning(f"{ticker}: TwelveData returned empty DataFrame")
            return None

        df = _normalize_ohlcv(df_raw)
        logger.debug(f"{ticker}: fetched {len(df)} bars via TwelveData")
        return df

    except Exception as exc:
        logger.warning(f"{ticker}: TwelveData fetch error — {exc}")
        return None


def fetch_universe_twelvedata(
    tickers: List[str],
    lookback_days: int = 300,
    api_key: Optional[str] = None,
    fallback_to_yfinance: bool = True,
) -> Dict[str, pd.DataFrame]:
    """
    Fetch OHLCV for the full universe via TwelveData.
    Falls back to yfinance per-ticker if TwelveData fails and fallback is enabled.

    Rate limited: respects TD_CREDITS_PER_MIN (default 8/min).
    With 525 tickers at 8/min = ~66 minutes for a full fetch.
    Use cached parquets for daily production runs (see tools/build_td_cache.py).

    Returns: {ticker: DataFrame}
    """
    key = api_key or os.environ.get("TWELVEDATA_API_KEY", "").strip()
    if not key:
        logger.warning("TWELVEDATA_API_KEY not set. Falling back to yfinance.")
        if fallback_to_yfinance:
            from prod.data.ig_fetcher import fetch_universe_ig
            return fetch_universe_ig(tickers, lookback_days)
        return {}

    universe_data: Dict[str, pd.DataFrame] = {}
    failed: List[str] = []
    total = len(tickers)

    logger.info(f"Fetching {total} tickers via TwelveData ({CREDITS_PER_MIN}/min rate limit)")

    for i, ticker in enumerate(tickers, 1):
        df = fetch_ticker_twelvedata(ticker, key, lookback_days)

        if df is not None and not df.empty and len(df) >= 50:
            universe_data[ticker] = df
        else:
            failed.append(ticker)
            logger.warning(f"{ticker}: TwelveData failed ({i}/{total})")
            if fallback_to_yfinance:
                df_yf = _yfinance_fallback(ticker, lookback_days)
                if df_yf is not None:
                    universe_data[ticker] = df_yf
                    logger.info(f"{ticker}: yfinance fallback OK")

        if i < total:
            time.sleep(SLEEP_BETWEEN)

    logger.info(f"TwelveData fetch complete: {len(universe_data)}/{total} OK | {len(failed)} failed")
    return universe_data


# ── Cache-based fetch (primary path for daily production runs) ─────────────────

CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "raw" / "prices_daily" / "twelvedata"
PARQUETS_DIR = CACHE_DIR / "parquets"


def fetch_universe_from_cache(
    tickers: List[str],
    lookback_days: int = 300,
    fallback_to_live: bool = True,
    api_key: Optional[str] = None,
) -> Dict[str, pd.DataFrame]:
    """
    Fetch OHLCV for the universe from the local parquet cache built by
    tools/build_td_cache.py. This is the fast path used by the orchestrator
    for daily EOD signal runs -- avoids hitting TwelveData's 8 credits/min
    limit on every run (~2,900 tickers would take ~6 hours otherwise).

    Run `python tools/build_td_cache.py` after EOD close to keep the cache
    current before this is called.

    Falls back to a live single-ticker fetch (fetch_ticker_twelvedata) for
    any ticker missing from cache or with stale/partial data, if enabled.

    Returns: {ticker: DataFrame}  (trimmed to the most recent lookback_days rows)
    """
    results: Dict[str, pd.DataFrame] = {}
    missing: List[str] = []

    for ticker in tickers:
        parquet_path = PARQUETS_DIR / f"{ticker}.parquet"
        if not parquet_path.exists():
            missing.append(ticker)
            continue
        try:
            df = pd.read_parquet(parquet_path)
            if df.empty:
                missing.append(ticker)
                continue
            df = df.sort_values("date").tail(lookback_days + 60).reset_index(drop=True)
            results[ticker] = df
        except Exception as exc:
            logger.warning(f"{ticker}: failed to read cache parquet -- {exc}")
            missing.append(ticker)

    logger.info(f"twelvedata cache: {len(results)}/{len(tickers)} tickers loaded, {len(missing)} missing/stale")

    if missing and fallback_to_live:
        key = api_key or os.environ.get("TWELVEDATA_API_KEY", "").strip()
        if key:
            logger.info(f"Live-fetching {len(missing)} cache-miss tickers via TwelveData...")
            for i, ticker in enumerate(missing, 1):
                df = fetch_ticker_twelvedata(ticker, key, lookback_days)
                if df is not None and not df.empty:
                    results[ticker] = df
                if i < len(missing):
                    time.sleep(SLEEP_BETWEEN)

    return results


def _yfinance_fallback(ticker: str, lookback_days: int) -> Optional[pd.DataFrame]:
    """yfinance fallback for individual failed tickers."""
    try:
        import yfinance as yf
        raw = yf.download(ticker, period=f"{lookback_days}d", auto_adjust=True, progress=False)
        if raw is None or raw.empty:
            return None
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = [c[0].lower() for c in raw.columns]
        else:
            raw.columns = [c.lower() for c in raw.columns]
        raw = raw.reset_index()
        raw.rename(columns={"date": "date", "Date": "date"}, inplace=True)
        raw["date"] = pd.to_datetime(raw["date"])
        for col in ["open", "high", "low", "close", "volume"]:
            if col not in raw.columns:
                raw[col] = None
        return raw[["date", "open", "high", "low", "close", "volume"]].dropna(subset=["date"])
    except Exception:
        return None
