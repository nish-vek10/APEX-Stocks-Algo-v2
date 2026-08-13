# tools/build_td_cache.py
"""
APEX -- TwelveData OHLCV Cache Builder

Builds/refreshes the local parquet cache used for EOD signal generation.
Method is an EXACT replica of ALGO-Stocks
research/experiments/06_fetch_twelvedata_ohlcv_3y.py — same batched
time_series() calls, same credit-based rate-limit spacing, same resumable
JSONL progress log. One deliberate difference:

  - Backtest script : fixed TD_START_DATE/TD_END_DATE window (frozen
                       historical snapshot, needed for reproducibility).
  - This script      : rolling window via `outputsize` (always the latest
                       N trading days up to now) — production needs current
                       data every day, not a snapshot frozen at backtest time.

Universe comes from config/production.yaml (after tools/build_scan_universe.py
--apply has been run), not the ALGO-Stocks trade_ready CSV.

Cache layout (identical paths/schema to ALGO-Stocks, for consistency):
  data/raw/prices_daily/twelvedata/parquets/{TICKER}.parquet
  data/raw/prices_daily/twelvedata/meta/{TICKER}.meta.json
  data/raw/prices_daily/twelvedata/_progress.jsonl
  data/raw/prices_daily/twelvedata/_errors.jsonl

Resumable + idempotent: safe to re-run daily. A ticker is skipped (zero API
credits spent) if its cached last_date is already within STALENESS_DAYS of
today. Everything else gets refetched.

Rate limit reality: TD_CREDITS_PER_MIN x TD_BATCH_SIZE from .env (default
8 x 8). At ~2,900 tickers and 8 credits/min, a FULL refresh takes ~6 hours.
This is a TwelveData plan limit, not something fixable in code — run this
right after EOD close (production.yaml scheduling.signal_time_et: 17:05 ET).
There's a 16h15m gap before next-day execution (09:31 ET) — a 6h batch
comfortably finishes with room to spare. Upgrading the TwelveData plan is
the only way to shorten this. (Both times are America/New_York local via
scheduler.py's DST-aware cron -- see scheduler.py docstring.)

Usage:
    python tools/build_td_cache.py                       # full run, skips fresh tickers
    python tools/build_td_cache.py --force                # ignore cache, refetch everyone
    python tools/build_td_cache.py --tickers AAPL,MSFT     # smoke test a subset
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

import pandas as pd
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

PRODUCTION_YAML = ROOT / "config" / "production.yaml"

OUT_DIR        = ROOT / "data" / "raw" / "prices_daily" / "twelvedata"
PARQUETS_DIR   = OUT_DIR / "parquets"
META_DIR       = OUT_DIR / "meta"
PROGRESS_JSONL = OUT_DIR / "_progress.jsonl"
ERRORS_JSONL   = OUT_DIR / "_errors.jsonl"
PARQUETS_DIR.mkdir(parents=True, exist_ok=True)
META_DIR.mkdir(parents=True, exist_ok=True)

API_KEY  = os.environ.get("TWELVEDATA_API_KEY", "").strip()
INTERVAL = os.environ.get("TD_INTERVAL", "1day").strip()
TZ       = os.environ.get("TD_TIMEZONE", "UTC").strip()
CREDITS_PER_MIN = int(os.environ.get("TD_CREDITS_PER_MIN", "8"))
BATCH_SIZE      = int(os.environ.get("TD_BATCH_SIZE", "8"))
OUTPUTSIZE      = int(os.environ.get("TD_OUTPUTSIZE", "5000"))
MIN_ROWS_OK     = int(os.environ.get("TD_MIN_ROWS_OK", "950"))

LOOKBACK_DAYS  = 300   # matches config/production.yaml universe.lookback_days
FETCH_BUFFER   = 60    # extra bars for indicator warmup (EMA200 etc.)
STALENESS_DAYS = 1     # cache considered fresh if last_date within N days of today


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_universe_tickers() -> List[str]:
    cfg = yaml.safe_load(PRODUCTION_YAML.read_text(encoding="utf-8"))
    return sorted({str(t).strip().upper() for t in cfg["universe"]["tickers"]})


def normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if "datetime" in df.columns:
        df["date"] = pd.to_datetime(df["datetime"], errors="coerce")
        df = df.drop(columns=["datetime"])
    elif "date" not in df.columns:
        for c in ("time", "timestamp", "index"):
            if c in df.columns:
                df["date"] = pd.to_datetime(df[c], errors="coerce")
                df = df.drop(columns=[c])
                break
    if "date" not in df.columns:
        raise KeyError(f"normalize_ohlcv: no date column found. cols={list(df.columns)}")

    for c in ["open", "high", "low", "close", "volume"]:
        if c not in df.columns:
            df[c] = None
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df = df.dropna(subset=["date"]).drop_duplicates("date").sort_values("date").reset_index(drop=True)
    return df[["date", "open", "high", "low", "close", "volume"]]


def is_cache_fresh(ticker: str) -> bool:
    meta_path = META_DIR / f"{ticker}.meta.json"
    if not meta_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        last_date = pd.to_datetime(meta.get("last_date"))
        if pd.isna(last_date):
            return False
        age_days = (datetime.now(timezone.utc).date() - last_date.date()).days
        return meta.get("status") in ("ok", "ok_short_history") and age_days <= STALENESS_DAYS
    except Exception:
        return False


def append_jsonl(path: Path, obj: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj) + "\n")


def chunk(lst: List[str], n: int) -> List[List[str]]:
    return [lst[i:i + n] for i in range(0, len(lst), n)]


def sleep_for_rate_limit(last_req_time: float, batch_credits: int) -> float:
    """Space calls so TD_CREDITS_PER_MIN is never exceeded (1 symbol ~= 1 credit)."""
    if batch_credits <= 0:
        return time.time()
    min_gap = 60.0 * (batch_credits / max(1, CREDITS_PER_MIN))
    now = time.time()
    gap = now - last_req_time
    if gap < min_gap:
        time.sleep(min_gap - gap)
    return time.time()


def _write_ticker(sym: str, sub: pd.DataFrame) -> str:
    sub = normalize_ohlcv(sub)
    out_path = PARQUETS_DIR / f"{sym}.parquet"
    sub.to_parquet(out_path, index=False)

    rows = len(sub)
    first_s = sub["date"].iloc[0].strftime("%Y-%m-%d") if rows else None
    last_s  = sub["date"].iloc[-1].strftime("%Y-%m-%d") if rows else None
    status  = "ok" if rows >= MIN_ROWS_OK else ("ok_short_history" if rows > 0 else "partial")

    meta = {
        "asof_utc": utc_now_iso(),
        "provider": "twelvedata",
        "ticker": sym,
        "interval": INTERVAL,
        "timezone": TZ,
        "rows": rows,
        "first_date": first_s,
        "last_date": last_s,
        "min_rows_ok": MIN_ROWS_OK,
        "status": status,
        "output": str(out_path),
    }
    (META_DIR / f"{sym}.meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    append_jsonl(PROGRESS_JSONL, {
        "asof_utc": utc_now_iso(), "status": status, "ticker": sym,
        "rows": rows, "first_date": first_s, "last_date": last_s,
    })
    return status


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="ignore cache freshness, refetch everyone")
    parser.add_argument("--tickers", type=str, default="", help="comma-separated smoke-test subset")
    args = parser.parse_args()

    if not API_KEY:
        raise RuntimeError("TWELVEDATA_API_KEY missing in .env")

    from twelvedata import TDClient

    tickers = load_universe_tickers()
    if args.tickers:
        forced = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        tickers = [t for t in tickers if t in forced] or forced

    remaining = tickers if args.force else [t for t in tickers if not is_cache_fresh(t)]

    outputsize = min(LOOKBACK_DAYS + FETCH_BUFFER, OUTPUTSIZE)
    batch_size_eff = min(BATCH_SIZE, CREDITS_PER_MIN) if BATCH_SIZE > CREDITS_PER_MIN else BATCH_SIZE

    print("\n=== APEX TwelveData Cache Build ===")
    print(f"[UNI]  universe={len(tickers)}  remaining={len(remaining)}  fresh/skipped={len(tickers) - len(remaining)}")
    print(f"[CFG]  batch={batch_size_eff} credits_per_min={CREDITS_PER_MIN}  outputsize={outputsize}  interval={INTERVAL}")
    print(f"[OUT]  {OUT_DIR}")

    if not remaining:
        print("\n[OK] Nothing to fetch -- cache already fresh. Exiting without API calls.")
        return

    est_minutes = len(remaining) / max(1, CREDITS_PER_MIN)
    print(f"[EST]  ~{est_minutes:.0f} min (~{est_minutes / 60:.1f} hr) at current rate limit\n")

    td = TDClient(apikey=API_KEY)
    batches = chunk(remaining, max(1, batch_size_eff))

    last_req_time = 0.0
    ok_n = partial_n = err_n = processed_n = 0
    total = len(remaining)

    for i, batch in enumerate(batches, start=1):
        last_req_time = sleep_for_rate_limit(last_req_time, batch_credits=len(batch))
        try:
            while True:
                try:
                    ts = td.time_series(
                        symbol=batch, interval=INTERVAL,
                        outputsize=outputsize, timezone=TZ, order="asc",
                    )
                    df = ts.as_pandas()
                    last_req_time = time.time()
                    break
                except Exception as e_req:
                    msg = str(e_req).lower()
                    if "out of api credits" in msg or "run out of api credits" in msg:
                        print(f"[RATE] minute credits hit; sleeping ~65s, retry batch {i}/{len(batches)}")
                        time.sleep(65)
                        last_req_time = time.time()
                        continue
                    raise

            if df is None or len(df) == 0:
                raise RuntimeError("Empty response")

            if isinstance(df.index, pd.MultiIndex):
                for sym in batch:
                    try:
                        if sym not in df.index.get_level_values(0):
                            raise KeyError(f"Symbol missing from batch response: {sym}")
                        sub = df.xs(sym, level=0).reset_index()
                        if "datetime" not in sub.columns and "date" not in sub.columns:
                            sub = sub.rename(columns={sub.columns[0]: "datetime"})
                        status = _write_ticker(sym, sub)
                        processed_n += 1
                        if status in ("ok", "ok_short_history"):
                            ok_n += 1
                        else:
                            partial_n += 1
                    except Exception as e_sym:
                        append_jsonl(ERRORS_JSONL, {
                            "asof_utc": utc_now_iso(), "ticker": sym,
                            "batch_i": i, "error": str(e_sym),
                        })
                        processed_n += 1
                        err_n += 1
            else:
                if len(batch) != 1:
                    raise RuntimeError("Non-multiindex response for batch > 1")
                sym = batch[0]
                sub = df.reset_index(drop=False)
                status = _write_ticker(sym, sub)
                processed_n += 1
                if status in ("ok", "ok_short_history"):
                    ok_n += 1
                else:
                    partial_n += 1

            if i % 5 == 0 or i == len(batches):
                pct = processed_n / total * 100.0
                print(f"[PROG] batch={i}/{len(batches)} done={processed_n}/{total} ({pct:.1f}%) "
                      f"ok={ok_n} partial={partial_n} err={err_n}")

        except Exception as e:
            for sym in batch:
                append_jsonl(ERRORS_JSONL, {
                    "asof_utc": utc_now_iso(), "ticker": sym,
                    "batch_i": i, "error": str(e),
                })
                processed_n += 1
                err_n += 1
            print(f"[WARN] batch {i}/{len(batches)} failed: {e}")

    print(f"\n[OK] Cache build complete. ok={ok_n} partial={partial_n} err={err_n}")
    print("Re-run anytime -- tickers already fresh are skipped (zero credits spent).")


if __name__ == "__main__":
    main()
