# tools/check_cache_freshness.py
"""
APEX Cache Freshness Spot-Check — read-only.

Dumps meta.json (last_date, status, rows) plus the actual last 3 rows of
the parquet cache for a specific list of tickers. Built to answer one
question: on 2026-09-16's 17:05 ET signal run, META/OKTA/ZS/EPAM/OSPN/PD/
PLMR/RBLX/RSG/SMMT/T all logged "SIGNAL: ... date=2026-09-14" -- two days
stale for a run happening the evening of 2026-09-16, after that day's
16:45 ET cache refresh should have already run. signal_date comes
straight from df.iloc[-1] in signal_generator.py, so if these tickers'
cached last row really is 2026-09-14, the cache itself never advanced --
not a signal-logic bug. This script checks the raw cache files directly
rather than guessing.

Touches nothing: read-only, no MT5, no state file writes.

Usage:
    python tools/check_cache_freshness.py TICKER [TICKER ...]
    python tools/check_cache_freshness.py META OKTA ZS EPAM OSPN PD PLMR RBLX RSG SMMT T
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "raw" / "prices_daily" / "twelvedata"
META_DIR = OUT_DIR / "meta"
PARQUETS_DIR = OUT_DIR / "parquets"


def check_ticker(ticker: str) -> None:
    print(f"\n=== {ticker} ===")

    meta_path = META_DIR / f"{ticker}.meta.json"
    if not meta_path.exists():
        print(f"  [!] No meta file: {meta_path}")
        return
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    print(f"  meta.json: last_date={meta.get('last_date')} status={meta.get('status')} "
          f"rows={meta.get('rows', '?')} fetched_at={meta.get('fetched_at', '?')}")

    parquet_path = PARQUETS_DIR / f"{ticker}.parquet"
    if not parquet_path.exists():
        print(f"  [!] No parquet file: {parquet_path}")
        return
    df = pd.read_parquet(parquet_path)
    if df.empty:
        print("  [!] Parquet is empty.")
        return
    df = df.sort_values("date")
    print(f"  parquet: {len(df)} rows, last 3 dates:")
    for _, row in df.tail(3).iterrows():
        print(f"    {row['date']} close={row['close']}")


def main() -> None:
    tickers = sys.argv[1:]
    if not tickers:
        print("Usage: python tools/check_cache_freshness.py TICKER [TICKER ...]")
        return
    for t in tickers:
        check_ticker(t.upper())


if __name__ == "__main__":
    main()
