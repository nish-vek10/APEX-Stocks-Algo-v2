# tools/export_ig_all_epics.py
"""
APEX — IG Full Epic Catalogue (all instruments, no filter)

Pulls every searchable instrument on your IG account via exhaustive
search sweeps. No filtering — all asset classes, all regions.

Output columns:
    epic            | IG epic string
    instrument_name | Full IG instrument name
    instrument_type | SHARES / CURRENCIES / INDICES / COMMODITIES / etc.
    currency        | Instrument currency
    market_status   | TRADEABLE / CLOSED / OFFLINE / etc.
    search_term     | Which search term discovered this instrument

Strategy:
  Phase 1 — A-Z          (26 searches  — quick test mode)
  Phase 2 — AA-ZZ        (676 searches — comprehensive sweep)
  Phase 3 — Asset-class  (~40 searches — catches non-alpha names)

Usage:
    # Quick test — Phase 1 only (26 searches, ~1 min)
    python tools/export_ig_all_epics.py --test

    # Full run — all phases (~12 min)
    python tools/export_ig_all_epics.py

    # Custom limit — first N results per search (default: all)
    python tools/export_ig_all_epics.py --full

Output: outputs/ig_all_epics_YYYYMMDD_HHMMSS.csv
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from core.utils.config_loader import load_production_config, resolve_ig_credentials
from prod.execution.ig_connector import IGConnector

OUTPUTS_DIR     = ROOT / "outputs" / "ig_epics"
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

RATE_LIMIT_SLEEP = 2.0   # IG non-trading: ~30 req/min → 2s between calls
MAX_RETRIES      = 3
RETRY_SLEEP      = 10.0
SEARCH_TIMEOUT   = 15.0  # seconds — kill stalled API calls
CHECKPOINT_EVERY = 50    # flush to CSV every N searches

LETTERS = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")

# Phase 1 (test mode): ~30 known search terms that reliably return IG results
_PHASE1_TEST = [
    "Apple", "Microsoft", "Amazon", "Google", "Tesla", "Meta", "Nvidia",
    "Gold", "Oil", "Silver", "Gas", "Copper", "Wheat", "Corn",
    "FTSE", "Dow", "Nasdaq", "S&P", "DAX", "Nikkei", "Hang Seng",
    "EUR", "USD", "GBP", "JPY", "AUD",
    "Bank", "Index", "ETF", "Fund",
]

# Phase 2: all 2-letter combos — comprehensive instrument name sweep
_PHASE2 = [a + b for a in LETTERS for b in LETTERS]  # AA..ZZ = 676 terms

# Phase 3: asset-class / region / financial terms (catches non-alpha names)
_PHASE3 = [
    # Asset classes
    "Index", "Indices", "Forex", "Currency", "Commodity",
    "Oil", "Gold", "Silver", "Gas", "Copper", "Platinum", "Palladium",
    "Wheat", "Corn", "Soybean", "Coffee", "Sugar", "Cotton",
    "Bond", "Rate", "Treasury", "Gilt",
    # Global regions
    "US", "UK", "EU", "Asia", "Japan", "China", "Germany", "France",
    "Italy", "Australia", "Canada", "Switzerland", "Hong Kong", "Singapore",
    "India", "Brazil", "South Africa", "Korea", "Taiwan", "Spain",
    # Market names
    "FTSE", "Dow", "Nasdaq", "S&P", "DAX", "Nikkei", "Hang Seng",
    "CAC", "IBEX", "MSCI", "Russell", "VIX",
    # Forex pairs
    "EUR", "GBP", "JPY", "AUD", "NZD", "CHF", "CAD", "SEK", "NOK",
    # Instrument types
    "ETF", "Fund", "Trust", "REIT", "ADR",
    # Numbers (indices like S&P 500, FTSE 100)
    "100", "200", "300", "400", "500", "600", "1000", "2000",
    # Common financial words
    "Bank", "Finance", "Capital", "Asset", "Market", "Futures",
    "Tech", "Energy", "Health", "Mining", "Retail", "Insurance",
]


def _search_with_retry(service, term: str) -> list[dict]:
    """Search with per-call timeout + retry. Cross-platform."""
    import concurrent.futures

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            future = ex.submit(service.search_markets, search_term=term)
            try:
                df = future.result(timeout=SEARCH_TIMEOUT)
            except concurrent.futures.TimeoutError:
                ex.shutdown(wait=False)  # abandon stuck thread, do NOT block
                print(f"  [{term}] attempt {attempt} TIMEOUT ({SEARCH_TIMEOUT}s)")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_SLEEP)
                    continue
                return []
            finally:
                ex.shutdown(wait=False)

            if df is None or (hasattr(df, "empty") and df.empty):
                return []
            return df.to_dict("records") if hasattr(df, "to_dict") else []

        except Exception as exc:
            err = repr(exc) or type(exc).__name__
            if attempt < MAX_RETRIES:
                print(f"  [{term}] attempt {attempt} error: {err[:80]} — retry in {RETRY_SLEEP}s")
                time.sleep(RETRY_SLEEP)
            else:
                print(f"  [{term}] failed after {MAX_RETRIES} attempts: {err[:80]}")
    return []


def main() -> None:
    parser = argparse.ArgumentParser(description="Export all IG epics to CSV")
    parser.add_argument("--test", action="store_true",
                        help="Quick test: Phase 1 only (A-Z, ~26 searches, ~1 min)")
    parser.add_argument("--full", action="store_true",
                        help="Full run: all 3 phases (~12 min)")
    args = parser.parse_args()

    # Build search term list
    if args.test:
        search_terms = _PHASE1_TEST
        print(f"\nTEST MODE — {len(search_terms)} known terms (~{len(search_terms) * RATE_LIMIT_SLEEP / 60:.1f} min)")
    else:
        search_terms = _PHASE2 + _PHASE3
        print(f"\nFULL MODE — {len(search_terms)} searches (~{len(search_terms) * RATE_LIMIT_SLEEP / 60:.0f} min)")

    prod_cfg = load_production_config(ROOT)
    creds    = resolve_ig_credentials(prod_cfg)

    print(f"Connecting to IG ({creds.get('acc_type', 'DEMO')})...")
    connector = IGConnector(creds)

    ts         = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    suffix     = "_test" if args.test else "_full"
    ckpt_path  = OUTPUTS_DIR / f"ig_all_epics{suffix}_{ts}_checkpoint.csv"
    fields     = ["ticker", "epic", "instrument_name", "instrument_type", "currency",
                  "market_status", "search_term"]

    results:    list[dict] = []
    seen_epics: set[str]   = set()
    total_raw   = 0
    t_start     = time.time()
    last_print  = t_start
    ckpt_count  = 0   # rows already flushed to checkpoint

    def _flush_checkpoint(force: bool = False) -> None:
        """Append new rows since last flush to checkpoint CSV."""
        nonlocal ckpt_count
        new_rows = results[ckpt_count:]
        if not new_rows:
            return
        write_header = not ckpt_path.exists()
        with ckpt_path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            if write_header:
                writer.writeheader()
            writer.writerows(new_rows)
        ckpt_count = len(results)
        if force:
            print(f"  ✓ checkpoint flushed → {ckpt_path.name}  ({ckpt_count} rows total)")

    with connector:
        service = connector.service
        print(f"Connected.\n{'─'*65}")
        print(f"  Checkpoint file : {ckpt_path.name}")
        print(f"  Flush every     : {CHECKPOINT_EVERY} searches\n{'─'*65}")

        for i, term in enumerate(search_terms, 1):
            time.sleep(RATE_LIMIT_SLEEP)
            rows = _search_with_retry(service, term)
            total_raw += len(rows)
            new_this = 0

            for row in rows:
                epic  = str(row.get("epic", "")).strip()
                name  = str(row.get("instrumentName", "")).strip()
                itype = str(row.get("instrumentType", "")).strip()
                curr  = str(row.get("currency", "N/A")).strip()
                status = str(row.get("marketStatus", "N/A")).strip()

                if not epic or epic in seen_epics:
                    continue

                parts  = epic.split(".")
                ticker = parts[2] if len(parts) >= 3 else ""

                seen_epics.add(epic)
                results.append({
                    "ticker":          ticker,
                    "epic":            epic,
                    "instrument_name": name,
                    "instrument_type": itype,
                    "currency":        curr,
                    "market_status":   status,
                    "search_term":     term,
                })
                new_this += 1

            # Checkpoint flush every N searches
            if i % CHECKPOINT_EVERY == 0:
                _flush_checkpoint(force=True)
            else:
                _flush_checkpoint(force=False)

            elapsed   = time.time() - t_start
            eta_sec   = (elapsed / i) * (len(search_terms) - i) if i > 0 else 0
            eta_min   = eta_sec / 60

            now = time.time()
            if new_this or (now - last_print) >= 30:
                last_print = now
                elapsed_str = f"{int(elapsed//60):02d}:{int(elapsed%60):02d}"
                eta_str     = f"{int(eta_min):02d}:{int(eta_sec%60):02d}"
                status_str  = f"+{new_this:4d}" if new_this else "  -- "
                print(
                    f"  [{i:4d}/{len(search_terms)}] {term:<8}  "
                    f"{status_str} new  |  total: {len(results):>5}  |  "
                    f"elapsed: {elapsed_str}  eta: {eta_str}"
                )

    # Final flush
    _flush_checkpoint(force=True)

    print(f"\nSweep complete.")
    print(f"  Searches run    : {len(search_terms)}")
    print(f"  Raw API results : {total_raw}")
    print(f"  Unique epics    : {len(results)}")

    # Sort by instrument type then name
    results.sort(key=lambda x: (x["instrument_type"], x["instrument_name"]))

    out_path = OUTPUTS_DIR / f"ig_all_epics{suffix}_{ts}_final.csv"

    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(results)

    # Summary by instrument type
    type_counts: dict[str, int] = {}
    for r in results:
        t = r["instrument_type"]
        type_counts[t] = type_counts.get(t, 0) + 1

    print(f"\n{'─'*50}")
    print(f"Breakdown by instrument type:")
    for itype, count in sorted(type_counts.items(), key=lambda x: -x[1]):
        print(f"  {itype:<30} {count:>5}")
    print(f"{'─'*50}")
    print(f"Total unique epics : {len(results)}")
    tradeable = sum(1 for r in results if r["market_status"] == "TRADEABLE")
    print(f"Tradeable now      : {tradeable}")
    print(f"Saved              : {out_path}")
    print(f"{'─'*50}\n")


if __name__ == "__main__":
    main()
